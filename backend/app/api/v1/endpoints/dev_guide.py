"""
Developer Guide endpoint — auto-introspecting documentation for the ARS app.

Why this exists:
    The Process page (process_docs.py) is for end-user planners — "click here,
    upload that file". Developers need a different view: where does this route
    live, what does its service do, which file should I edit. Maintaining that
    by hand goes stale within a sprint.

    This endpoint INTROSPECTS the running app on every request (with a small
    TTL cache) so the answer is always current:

      - Routes      — pulled from the FastAPI app's router stack at runtime
      - Services    — discovered by walking app/services/*.py
      - Pages       — discovered by walking frontend/src/pages/*.jsx
      - Tables      — read from INFORMATION_SCHEMA on the data DB
      - Files       — read on demand, sandboxed to the repo
      - Recent      — `git log --since=14.days` from the repo

    Optional layer: markdown notes per process under
    backend/app/docs/dev_guide/*.md (one .md = one process). The frontend
    surfaces these alongside the live data, but they are NOT required —
    if a developer never writes one, the page still works.

Endpoints:
    GET /api/v1/dev-guide/index             — full structured index (cached 30s)
    GET /api/v1/dev-guide/file?path=...     — file source, sandboxed
    GET /api/v1/dev-guide/note/{slug}       — load a markdown note (optional)
    GET /api/v1/dev-guide/notes             — list available markdown notes
    POST /api/v1/dev-guide/refresh          — invalidate the cache
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from loguru import logger

from app.api.v1.endpoints.auth import get_current_user
from app.models.rbac import User
from app.database.session import get_data_engine


router = APIRouter(prefix="/dev-guide", tags=["Developer Guide"])

# ─── Paths ───────────────────────────────────────────────────────────────────
# __file__ = backend/app/api/v1/endpoints/dev_guide.py
# parents[4] = backend/  ;  parents[5] = repo root
_HERE         = Path(__file__).resolve()
BACKEND_ROOT  = _HERE.parents[4]
REPO_ROOT     = _HERE.parents[5]
FRONTEND_ROOT = REPO_ROOT / "frontend"
SERVICES_DIR  = BACKEND_ROOT / "app" / "services"
ENDPOINTS_DIR = BACKEND_ROOT / "app" / "api" / "v1" / "endpoints"
PAGES_DIR     = FRONTEND_ROOT / "src" / "pages"
NOTES_DIR     = BACKEND_ROOT / "app" / "docs" / "dev_guide"
NOTES_DIR.mkdir(parents=True, exist_ok=True)


# ─── In-memory cache (invalidated on refresh, on file mtime change) ─────────
_CACHE_TTL_SEC = 30.0
_cache: Dict[str, Tuple[float, Any]] = {}


def _cache_get(key: str):
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < _CACHE_TTL_SEC:
        return hit[1]
    return None


def _cache_put(key: str, value: Any) -> Any:
    _cache[key] = (time.time(), value)
    return value


def _cache_clear() -> None:
    _cache.clear()


# ─── Sandbox: only allow reads under these roots ─────────────────────────────
_ALLOWED_READ_ROOTS = (
    BACKEND_ROOT / "app",
    BACKEND_ROOT / "main.py",
    FRONTEND_ROOT / "src",
)


def _resolve_sandboxed(rel_path: str) -> Path:
    """Resolve `rel_path` against repo root and reject anything outside the
    allowlisted roots. Prevents `../../etc/passwd` type traversal."""
    try:
        target = (REPO_ROOT / rel_path).resolve()
    except Exception as e:
        raise HTTPException(400, f"Invalid path: {e}")
    for root in _ALLOWED_READ_ROOTS:
        try:
            target.relative_to(root.resolve())
            return target
        except ValueError:
            continue
    # main.py is a file, not a dir — handle separately
    if target == (BACKEND_ROOT / "main.py").resolve():
        return target
    raise HTTPException(403, f"Path outside the allowlisted roots: {rel_path}")


# ─── Route introspection ─────────────────────────────────────────────────────
def _introspect_routes(app) -> List[Dict[str, Any]]:
    """Walk every registered FastAPI route and return what a dev would want
    to see: HTTP method, path, tag, summary, the source file:line of the
    endpoint function, and a short docstring snippet."""
    out: List[Dict[str, Any]] = []
    seen: set = set()  # (method, path) — dedupes from sub-mounts
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        endpoint = getattr(route, "endpoint", None)
        if not path or not methods or endpoint is None:
            continue
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            key = (method, path)
            if key in seen:
                continue
            seen.add(key)

            tags = list(getattr(route, "tags", []) or [])
            summary = getattr(route, "summary", "") or ""
            description = getattr(route, "description", "") or ""

            # Source location of the underlying function
            file_path = ""
            line_no = 0
            try:
                src_file = endpoint.__code__.co_filename
                line_no = endpoint.__code__.co_firstlineno
                file_path = str(Path(src_file).resolve().relative_to(REPO_ROOT)).replace("\\", "/")
            except Exception:
                pass

            doc = (endpoint.__doc__ or "").strip()
            # Keep just the first paragraph for the index payload
            first_para = doc.split("\n\n", 1)[0] if doc else ""

            out.append({
                "method": method,
                "path": path,
                "tags": tags,
                "summary": summary or first_para[:120],
                "description_excerpt": (description or first_para)[:400],
                "file": file_path,
                "line": line_no,
                "function": getattr(endpoint, "__name__", ""),
            })
    out.sort(key=lambda r: (r["path"], r["method"]))
    return out


# ─── Service introspection ───────────────────────────────────────────────────
def _introspect_services() -> List[Dict[str, Any]]:
    """One row per service file. Reads the module docstring and the top
    public class names + their docstrings via ast (no import side effects)."""
    out: List[Dict[str, Any]] = []
    if not SERVICES_DIR.exists():
        return out
    for path in sorted(SERVICES_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except Exception as e:
            out.append({
                "name": path.stem,
                "file": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
                "module_doc": "",
                "classes": [],
                "functions": [],
                "error": f"parse failed: {e}",
            })
            continue

        module_doc = (ast.get_docstring(tree) or "").strip()
        classes: List[Dict[str, Any]] = []
        functions: List[Dict[str, Any]] = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                classes.append({
                    "name": node.name,
                    "line": node.lineno,
                    "doc": (ast.get_docstring(node) or "").strip()[:300],
                    "methods": [
                        n.name for n in node.body
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and not n.name.startswith("_")
                    ][:25],
                })
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                functions.append({
                    "name": node.name,
                    "line": node.lineno,
                    "doc": (ast.get_docstring(node) or "").strip()[:200],
                })

        out.append({
            "name": path.stem,
            "file": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
            "module_doc": module_doc[:500],
            "classes": classes,
            "functions": functions[:30],
        })
    return out


# ─── Frontend page discovery ─────────────────────────────────────────────────
def _introspect_pages() -> List[Dict[str, Any]]:
    """List frontend pages with a guess at the route, derived from filename
    + a regex over App.jsx so the path stays accurate even when devs rename
    components. Falls back to a slugified filename if no route is found."""
    out: List[Dict[str, Any]] = []
    if not PAGES_DIR.exists():
        return out

    # Build component → route map from App.jsx if present
    component_to_route: Dict[str, str] = {}
    app_jsx = FRONTEND_ROOT / "src" / "App.jsx"
    if app_jsx.exists():
        try:
            text_app = app_jsx.read_text(encoding="utf-8")
            # capture: <Route path="x" element={<ComponentName ... />}/>
            for m in re.finditer(
                r'path=["\']([^"\']+)["\']\s+element=\{[^}]*<\s*(\w+)\b',
                text_app,
            ):
                component_to_route.setdefault(m.group(2), m.group(1))
        except Exception:
            pass

    for path in sorted(PAGES_DIR.glob("*.jsx")):
        comp = path.stem
        route = component_to_route.get(comp, "")
        # Pull first leading comment / first H1 in JSX as a hint
        hint = ""
        try:
            head = path.read_text(encoding="utf-8")[:2000]
            for m in re.finditer(r"<h1[^>]*>([^<]{3,120})</h1>", head, flags=re.I):
                hint = m.group(1).strip()
                break
            if not hint:
                m = re.search(r"//\s*(.{4,140})", head)
                if m:
                    hint = m.group(1).strip()
        except Exception:
            pass

        out.append({
            "component": comp,
            "file": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
            "route": route,
            "hint": hint,
        })
    return out


# ─── Endpoint file index (one row per *_router file) ─────────────────────────
def _introspect_endpoint_files() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not ENDPOINTS_DIR.exists():
        return out
    for path in sorted(ENDPOINTS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
            module_doc = (ast.get_docstring(tree) or "").strip()
        except Exception:
            module_doc = ""
        out.append({
            "name": path.stem,
            "file": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
            "module_doc": module_doc[:400],
        })
    return out


# ─── DB schema (table list with row counts) ──────────────────────────────────
def _introspect_tables() -> List[Dict[str, Any]]:
    """Pull table list from INFORMATION_SCHEMA + approximate row counts from
    sys.partitions (avoids COUNT(*) full scans)."""
    sql = """
    SELECT t.name AS table_name,
           ISNULL(SUM(CASE WHEN p.index_id IN (0,1) THEN p.rows ELSE 0 END), 0) AS row_count,
           MIN(t.create_date) AS created_at,
           MAX(t.modify_date) AS modified_at
    FROM sys.tables t
    LEFT JOIN sys.partitions p ON t.object_id = p.object_id
    GROUP BY t.name
    ORDER BY t.name
    """
    out: List[Dict[str, Any]] = []
    try:
        eng = get_data_engine()
        with eng.connect() as conn:
            for row in conn.execute(text(sql)):
                out.append({
                    "table": row[0],
                    "rows": int(row[1] or 0),
                    "created_at": str(row[2]) if row[2] else None,
                    "modified_at": str(row[3]) if row[3] else None,
                })
    except Exception as e:
        logger.warning(f"dev_guide: schema introspection failed: {e}")
    return out


# ─── Recent git activity ─────────────────────────────────────────────────────
def _introspect_git(days: int = 14, limit: int = 60) -> List[Dict[str, Any]]:
    """`git log` for the last N days, capped at `limit` commits, with the
    list of files touched per commit. Helps a dev see what changed lately."""
    if not (REPO_ROOT / ".git").exists():
        return []
    try:
        out = subprocess.check_output(
            ["git", "log",
             f"--since={days}.days",
             f"-n", str(limit),
             "--name-only",
             "--pretty=format:%H%x09%an%x09%ad%x09%s", "--date=iso-strict"],
            cwd=str(REPO_ROOT),
            stderr=subprocess.DEVNULL,
            timeout=8,
        ).decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"dev_guide: git log failed: {e}")
        return []

    commits: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for line in out.splitlines():
        if not line:
            if current:
                commits.append(current)
                current = None
            continue
        if "\t" in line and current is None:
            parts = line.split("\t", 3)
            if len(parts) == 4:
                sha, author, date, subject = parts
                current = {"sha": sha[:10], "author": author, "date": date, "subject": subject, "files": []}
                continue
        if current is not None:
            current["files"].append(line.replace("\\", "/"))
    if current:
        commits.append(current)
    return commits


# ─── Notes (optional markdown layer) ─────────────────────────────────────────
def _slug_to_title(slug: str) -> str:
    """`01_data_management` → `Data Management` (strip leading-digit prefix
    and underscores, title-case words)."""
    base = re.sub(r"^\d+[_\-]", "", slug)
    return base.replace("_", " ").replace("-", " ").strip().title()


def _read_first_h1(path: Path) -> Optional[str]:
    try:
        head = path.read_text(encoding="utf-8")[:2000]
        m = re.search(r"^#\s+(.+)$", head, flags=re.M)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return None


def _note_record(path: Path, slug: str) -> Dict[str, Any]:
    title = _read_first_h1(path) or _slug_to_title(path.stem)
    return {
        "slug": slug,
        "title": title,
        "file": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "modified_at": time.strftime("%Y-%m-%d %H:%M",
                                     time.localtime(path.stat().st_mtime)),
    }


def _list_notes() -> List[Dict[str, Any]]:
    """Backward-compatible flat list — every .md under NOTES_DIR (recursive),
    each entry uses a forward-slash slug (e.g. 'data-management/upload_data').
    Section overview files named `_index.md` use the parent folder slug."""
    out: List[Dict[str, Any]] = []
    if not NOTES_DIR.exists():
        return out
    for path in sorted(NOTES_DIR.rglob("*.md")):
        rel = path.relative_to(NOTES_DIR).with_suffix("")
        # `_index.md` in a folder represents the section overview — its slug
        # is the folder slug; the file collapses into the parent.
        if path.name == "_index.md":
            slug = "/".join(part for part in rel.parts[:-1])
        else:
            slug = "/".join(rel.parts)
        if not slug:
            continue
        out.append(_note_record(path, slug))
    return out


def _build_tree() -> List[Dict[str, Any]]:
    """Walk NOTES_DIR and produce a hierarchical tree of folders + notes the
    frontend can render directly. Each section folder becomes a node with a
    `children` array; loose .md files at the root sit alongside.

    Naming conventions:
      - Folder name `01_data_management` becomes a section titled "Data Management".
      - File `_index.md` inside a folder = the section's own overview note;
        its slug is the folder path itself.
      - Numeric prefixes (`01_`, `02_`) drive sort order and are stripped from titles.
    """
    if not NOTES_DIR.exists():
        return []

    def walk(dir_path: Path, prefix: str = "") -> List[Dict[str, Any]]:
        nodes: List[Dict[str, Any]] = []
        # Sort: folders and files together by name (numeric-prefix friendly)
        entries = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        for entry in entries:
            if entry.name.startswith(".") or entry.name == "__pycache__":
                continue
            if entry.is_dir():
                slug = (prefix + entry.name).replace("\\", "/")
                idx_file = entry / "_index.md"
                title = (
                    _read_first_h1(idx_file) if idx_file.exists()
                    else _slug_to_title(entry.name)
                )
                children = walk(entry, prefix=slug + "/")
                node = {
                    "type": "folder",
                    "slug": slug,
                    "title": title,
                    "has_index": idx_file.exists(),
                    "children": children,
                }
                if idx_file.exists():
                    stat = idx_file.stat()
                    node["modified_at"] = time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)
                    )
                    node["file"] = str(idx_file.relative_to(REPO_ROOT)).replace("\\", "/")
                nodes.append(node)
            elif entry.suffix.lower() == ".md" and entry.name != "_index.md":
                slug = (prefix + entry.stem).replace("\\", "/")
                rec = _note_record(entry, slug)
                rec["type"] = "note"
                nodes.append(rec)
        return nodes

    return walk(NOTES_DIR)


# ─── Endpoints ───────────────────────────────────────────────────────────────
@router.get("/index")
def dev_guide_index(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Return the full developer guide index. Cached for 30s.
    Auto-rebuilt from the running app — no stale doc to maintain."""
    cached = _cache_get("index")
    if cached is not None:
        return cached

    app = request.app
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "repo_root": str(REPO_ROOT).replace("\\", "/"),
        "routes": _introspect_routes(app),
        "endpoint_files": _introspect_endpoint_files(),
        "services": _introspect_services(),
        "pages": _introspect_pages(),
        "tables": _introspect_tables(),
        "git_recent": _introspect_git(),
        "notes": _list_notes(),
        "notes_tree": _build_tree(),
        "stats": {},
    }
    payload["stats"] = {
        "route_count":   len(payload["routes"]),
        "service_count": len(payload["services"]),
        "page_count":    len(payload["pages"]),
        "table_count":   len(payload["tables"]),
        "commit_count":  len(payload["git_recent"]),
        "note_count":    len(payload["notes"]),
    }
    return _cache_put("index", payload)


@router.get("/file")
def dev_guide_file(
    path: str = Query(..., description="repo-relative path"),
    current_user: User = Depends(get_current_user),
):
    """Return the source of a file (sandboxed). Used by the file viewer."""
    target = _resolve_sandboxed(path)
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "File not found")
    try:
        text_src = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(415, "Binary file not supported")
    return {
        "path": path.replace("\\", "/"),
        "size_bytes": len(text_src.encode("utf-8")),
        "lines": text_src.count("\n") + 1,
        "modified_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(target.stat().st_mtime)),
        "content": text_src,
    }


@router.get("/note/{slug:path}")
def dev_guide_note(slug: str, current_user: User = Depends(get_current_user)):
    """Return a markdown note. Slug may include `/` for nested folders, e.g.
    `data-management/upload_data`. A folder slug returns its `_index.md`.
    Sandboxed: only `[A-Za-z0-9_\\-/]+` and must resolve under NOTES_DIR."""
    if not re.match(r"^[A-Za-z0-9_\-/]+$", slug) or ".." in slug:
        raise HTTPException(400, "Invalid slug")

    # Resolve: try `<slug>.md` first, then `<slug>/_index.md` for folders.
    candidates = [
        NOTES_DIR / f"{slug}.md",
        NOTES_DIR / slug / "_index.md",
    ]
    target: Optional[Path] = None
    for c in candidates:
        try:
            resolved = c.resolve()
            resolved.relative_to(NOTES_DIR.resolve())
        except (ValueError, OSError):
            continue
        if resolved.exists() and resolved.is_file():
            target = resolved
            break

    if target is None:
        raise HTTPException(404, "Note not found")

    return {
        "slug": slug,
        "title": _read_first_h1(target) or _slug_to_title(target.stem),
        "modified_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(target.stat().st_mtime)),
        "content": target.read_text(encoding="utf-8"),
    }


@router.get("/notes")
def dev_guide_notes(current_user: User = Depends(get_current_user)):
    return {"notes": _list_notes()}


@router.get("/tree")
def dev_guide_tree(current_user: User = Depends(get_current_user)):
    """Return the hierarchical note tree (folders + notes)."""
    return {"tree": _build_tree()}


@router.post("/refresh")
def dev_guide_refresh(current_user: User = Depends(get_current_user)):
    _cache_clear()
    return {"ok": True, "message": "Dev guide cache cleared"}
