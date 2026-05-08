"""
Process Documentation endpoint.

Serves markdown SOP files from `backend/app/docs/processes/` to the frontend
Process page — with three live-enrichment features so docs stay in sync with
the running code:

  1. FRESHNESS: compares the modification time of source files listed in the
     frontmatter `source:` field against `last_reviewed`. If any source file
     is newer than the review date, the doc is flagged STALE.

  2. DIRECTIVES: markdown comments of the form
         <!-- @metric sql="SELECT ..." label="..." -->
         <!-- @source file="path.py" lines="10-50" -->
         <!-- @source file="path.py" symbol="fn_name" -->
     are replaced at render time with live data or code excerpts.

  3. CACHE: directive results are cached for 30 s to avoid hammering SQL on
     every doc view.

Frontmatter format:
    ---
    title: Human-readable title
    category: Allocation | Data Prep | ...
    order: 10
    source: backend/app/services/x.py, backend/app/api/v1/endpoints/y.py
    last_reviewed: YYYY-MM-DD
    ---
"""
import re
import time
import ast
from pathlib import Path
from datetime import datetime, timezone, date
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from loguru import logger

from app.api.v1.endpoints.auth import get_current_user
from app.models.rbac import User
from app.database.session import get_data_engine, get_system_engine

router = APIRouter(prefix="/process", tags=["Process Docs"])

DOCS_DIR     = Path(__file__).resolve().parents[3] / "docs" / "processes"
PROJECT_ROOT = Path(__file__).resolve().parents[4]   # D:/ars

# ─── Simple in-memory cache for directive resolution ────────────────────────
_CACHE_TTL  = 30.0  # seconds
_cache: Dict[str, Tuple[float, Any]] = {}


def _cached(key: str, compute):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    val = compute()
    _cache[key] = (now, val)
    return val


# ═══════════════════════════════════════════════════════════════════════════
# FRONTMATTER
# ═══════════════════════════════════════════════════════════════════════════

def _parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    header_block = text[3:end].strip()
    body = text[end + 4 :].lstrip("\n")
    meta: Dict[str, str] = {}
    for line in header_block.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, body


# ═══════════════════════════════════════════════════════════════════════════
# FRESHNESS CHECK
# ═══════════════════════════════════════════════════════════════════════════

def _source_paths_from_meta(source_field: str) -> List[str]:
    """`source:` field is comma-separated; strip trailing `:: fn` annotations."""
    items = []
    for raw in source_field.split(","):
        s = raw.strip()
        if not s:
            continue
        if "::" in s:
            s = s.split("::")[0].strip()
        items.append(s)
    return items


def _compute_freshness(meta: Dict[str, str], doc_path: Path) -> Dict[str, Any]:
    last_reviewed = meta.get("last_reviewed", "")
    try:
        reviewed_date = datetime.strptime(last_reviewed, "%Y-%m-%d").date()
    except Exception:
        reviewed_date = None

    source_files = _source_paths_from_meta(meta.get("source", ""))
    stale_files: List[Dict[str, str]] = []
    newest_mtime: Optional[datetime] = None

    for rel in source_files:
        # Normalize slashes + strip leading slash
        clean = rel.replace("\\", "/").lstrip("/")
        p = (PROJECT_ROOT / clean)
        if not p.exists():
            continue
        m = datetime.fromtimestamp(p.stat().st_mtime)
        if newest_mtime is None or m > newest_mtime:
            newest_mtime = m
        if reviewed_date and m.date() > reviewed_date:
            stale_files.append({
                "path":  clean,
                "mtime": m.isoformat(timespec="seconds"),
            })

    status = "unknown"
    if reviewed_date:
        status = "stale" if stale_files else "fresh"

    return {
        "status":              status,             # fresh | stale | unknown
        "last_reviewed":       last_reviewed,
        "source_newest_mtime": newest_mtime.isoformat(timespec="seconds") if newest_mtime else None,
        "stale_files":         stale_files,
        "doc_mtime":           datetime.fromtimestamp(doc_path.stat().st_mtime).isoformat(timespec="seconds"),
    }


# ═══════════════════════════════════════════════════════════════════════════
# DIRECTIVES — @metric + @source
# ═══════════════════════════════════════════════════════════════════════════

_DIRECTIVE_RE = re.compile(
    r"<!--\s*@(?P<kind>metric|source)\s+(?P<attrs>[^>]*?)-->",
    re.IGNORECASE,
)

_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


def _parse_attrs(s: str) -> Dict[str, str]:
    return {m.group(1).lower(): m.group(2) for m in _ATTR_RE.finditer(s)}


# Allow-list of SQL prefixes — protects against any directive doing DML.
_SAFE_SQL_PREFIXES = ("select", "with")


_SYSTEM_DB_TABLES = (
    "rbac_users", "rbac_roles", "rbac_permissions",
    "rbac_role_permissions", "rbac_user_roles",
    "rls_stores", "rls_user_store_access", "rls_user_region_access",
    "rls_user_category_access", "rls_column_restrictions",
    "rls_table_role_access", "table_settings",
    "audit_log", "export_settings", "export_jobs", "table_permissions",
    "upload_jobs", "data_change_log", "msa_storage_jobs",
    "sys_table_registry", "sys_column_registry",
)


def _pick_engine(db_hint: str, sql: str):
    """Pick the engine based on explicit hint, then fall back to detecting
    a known system-DB table name in the SQL. Default is the data engine."""
    hint = (db_hint or "").strip().lower()
    if hint in ("system", "claude"):
        return get_system_engine()
    if hint in ("data", "rep_data", "repdata"):
        return get_data_engine()
    lowered = sql.lower()
    for t in _SYSTEM_DB_TABLES:
        if re.search(rf"\b{t}\b", lowered):
            return get_system_engine()
    return get_data_engine()


def _resolve_metric(attrs: Dict[str, str]) -> str:
    sql   = (attrs.get("sql") or "").strip()
    label = attrs.get("label") or "metric"
    fmt   = attrs.get("format") or "scalar"   # scalar | table
    db    = attrs.get("db") or ""             # system | data (auto-detect if blank)

    if not sql:
        return f"_[metric error: sql missing]_"
    if not sql.lower().lstrip().startswith(_SAFE_SQL_PREFIXES):
        return f"_[metric blocked: only SELECT allowed]_"

    def run():
        try:
            eng = _pick_engine(db, sql)
            with eng.connect() as c:
                rows = c.execute(text(sql)).fetchall()
        except Exception as e:
            logger.warning(f"process @metric failed: {e}")
            return {"error": str(e)}
        if fmt == "table":
            return {"rows": [list(r) for r in rows], "columns": list(rows[0]._mapping.keys()) if rows else []}
        # scalar
        if not rows:
            return {"value": None}
        first = rows[0]
        v = first[0] if len(first) else None
        return {"value": v}

    data = _cached(f"metric::{db}::{sql}::{fmt}", run)

    if "error" in data:
        return f"> ⚠️ metric `{label}` failed: `{data['error']}`"

    if fmt == "table":
        cols = data.get("columns", [])
        rows = data.get("rows", [])
        if not rows:
            return f"_{label}: no rows_"
        header = "| " + " | ".join(str(c) for c in cols) + " |"
        sep    = "|" + "|".join(["---"] * len(cols)) + "|"
        body   = "\n".join("| " + " | ".join(str(x) if x is not None else "" for x in r) + " |" for r in rows[:50])
        return f"**{label}**\n\n{header}\n{sep}\n{body}"

    val = data.get("value")
    try:
        # pretty-format numbers
        if isinstance(val, (int, float)):
            pretty = f"{val:,}"
        else:
            pretty = "—" if val is None else str(val)
    except Exception:
        pretty = str(val)
    return f"**{label}:** `{pretty}`"


def _extract_symbol(source: str, name: str) -> Optional[str]:
    """Return the source text of the function / class named `name`."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    lines = source.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            start = (node.decorator_list[0].lineno if node.decorator_list else node.lineno) - 1
            end   = node.end_lineno
            return "\n".join(lines[start:end])
    return None


def _resolve_source(attrs: Dict[str, str]) -> str:
    file_rel = attrs.get("file") or ""
    if not file_rel:
        return "_[source error: file missing]_"
    clean = file_rel.replace("\\", "/").lstrip("/")
    p = PROJECT_ROOT / clean
    if not p.exists():
        return f"_[source error: `{clean}` not found]_"

    lines_spec = attrs.get("lines")   # e.g. "10-50"
    symbol     = attrs.get("symbol")  # e.g. "generate_listing"

    def run():
        try:
            text_ = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"_[source read failed: {e}]_"

        if symbol:
            snippet = _extract_symbol(text_, symbol)
            if snippet is None:
                return f"_[source error: symbol `{symbol}` not found in `{clean}`]_"
        elif lines_spec:
            try:
                a, b = lines_spec.split("-")
                a = int(a); b = int(b)
            except Exception:
                return f"_[source error: bad lines spec `{lines_spec}`]_"
            all_lines = text_.splitlines()
            snippet = "\n".join(all_lines[a - 1 : b])
        else:
            return f"_[source error: provide `lines` or `symbol`]_"

        lang = (p.suffix.lstrip(".") or "text").lower()
        header = f"`{clean}`"
        if symbol:
            header += f" — `{symbol}`"
        elif lines_spec:
            header += f" — lines {lines_spec}"
        return f"{header}\n\n```{lang}\n{snippet}\n```"

    return _cached(f"source::{clean}::{lines_spec}::{symbol}", run)


def _process_directives(body: str) -> Tuple[str, int]:
    """Replace directive HTML comments. Returns (new_body, count_replaced)."""
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        kind = m.group("kind").lower()
        attrs = _parse_attrs(m.group("attrs"))
        count += 1
        if kind == "metric":
            return _resolve_metric(attrs)
        if kind == "source":
            return _resolve_source(attrs)
        return m.group(0)

    return _DIRECTIVE_RE.sub(repl, body), count


# ═══════════════════════════════════════════════════════════════════════════
# DOC LIST / GET
# ═══════════════════════════════════════════════════════════════════════════

def _list_docs() -> List[Path]:
    if not DOCS_DIR.exists():
        return []
    return sorted(DOCS_DIR.glob("*.md"))


def _int_or(default: int, raw: str) -> int:
    try:
        return int(raw)
    except Exception:
        return default


def _doc_summary(path: Path) -> Dict:
    meta, _ = _parse_frontmatter(path.read_text(encoding="utf-8"))
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    fresh = _compute_freshness(meta, path)
    return {
        "name":          path.stem,
        "title":         meta.get("title", path.stem),
        "category":      meta.get("category", "General"),
        "order":         _int_or(999, meta.get("order", "999")),
        "source":        meta.get("source", ""),
        "last_reviewed": meta.get("last_reviewed", ""),
        "file_mtime":    mtime,
        "freshness":     fresh,
    }


@router.get("/list")
def list_processes(current_user: User = Depends(get_current_user)):
    """List every process doc with summary metadata + freshness."""
    docs = [_doc_summary(p) for p in _list_docs()]
    docs.sort(key=lambda d: (d["category"], d["order"], d["title"]))
    stale_count = sum(1 for d in docs if d["freshness"]["status"] == "stale")
    return {
        "success":     True,
        "data":        docs,
        "count":       len(docs),
        "stale_count": stale_count,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/get/{name}")
def get_process(
    name: str,
    resolve: bool = True,
    current_user: User = Depends(get_current_user),
):
    """Return markdown body + metadata for one process.

    When `resolve=true` (default), `@metric` and `@source` directives are
    expanded with live data at render time. Pass `resolve=false` to get
    the raw markdown (useful for editors).
    """
    safe = Path(name).name
    path = DOCS_DIR / f"{safe}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Process doc '{safe}' not found")

    raw = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(raw)

    resolved = 0
    if resolve:
        body, resolved = _process_directives(body)

    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    fresh = _compute_freshness(meta, path)

    return {
        "success": True,
        "data": {
            "name":               safe,
            "title":              meta.get("title", safe),
            "category":           meta.get("category", "General"),
            "order":              _int_or(999, meta.get("order", "999")),
            "source":             meta.get("source", ""),
            "last_reviewed":      meta.get("last_reviewed", ""),
            "file_mtime":         mtime,
            "content":            body,
            "freshness":          fresh,
            "directives_resolved": resolved,
            "server_time":        datetime.now(timezone.utc).isoformat(),
        },
    }


@router.get("/health")
def process_health(current_user: User = Depends(get_current_user)):
    """Cheap metric for the Process page's status strip."""
    docs = [_doc_summary(p) for p in _list_docs()]
    return {
        "success": True,
        "data": {
            "total_docs":  len(docs),
            "stale_docs":  sum(1 for d in docs if d["freshness"]["status"] == "stale"),
            "server_time": datetime.now(timezone.utc).isoformat(),
            "cache_size":  len(_cache),
        },
    }
