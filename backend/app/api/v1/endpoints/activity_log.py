"""
Daily Activity Log API
======================
Turns raw `audit_log` rows into human-readable *daily pointers* — one bullet per
(ARS module × action) — so a superadmin/developer can review "what did I do in
ARS today" and mark each pointer YES / NO / (pending) as a validation.

Endpoints (superadmin only):
- GET  /activity-log?date=YYYY-MM-DD[&changed_by=...]  → pointers + saved validation
- POST /activity-log/validate                          → upsert a YES/NO validation

Validations persist in `ars_daily_activity_validation` (auto-created on first use,
in the system/Claude DB alongside audit_log). Keyed by (date, item_key, user) so
each reviewer keeps their own YES/NO answers.
"""
import os
import subprocess
from pathlib import Path
from datetime import datetime, timedelta, date as date_cls

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user
from app.models.rbac import User

router = APIRouter(prefix="/activity-log", tags=["Daily Activity Log"])


# ---------------------------------------------------------------------------
# Module mapping — turn a raw table_name into a friendly ARS "subject"
# ---------------------------------------------------------------------------
# Longest-prefix wins. Keeps pointers grouped by the module a non-technical
# reviewer recognises ("MSA", "Pending Allocation") rather than a table name.
_MODULE_MAP = [
    ("ARS_MSA",                 "MSA Stock Calculation"),
    ("MSA_CALCULATION",         "MSA Stock Calculation"),
    ("ARS_PEND_ALC",            "Pending Allocation"),
    ("ARS_NL_TBL_HOLD",         "Hold Process"),
    ("ARS_HOLD",                "Hold Process"),
    ("MASTER_ALC_INPUT",        "Listing / Store Master"),
    ("ARS_LISTING",             "Listing & Allocation"),
    ("ARS_LISTED",              "Listing & Allocation"),
    ("ALLOC_HEADER",            "Allocation"),
    ("ALLOC_DETAIL",            "Allocation"),
    ("ARS_ALLOC",               "Allocation"),
    ("CONT_PRESETS",            "Contribution %"),
    ("ARS_CONT",                "Contribution %"),
    ("GRID",                    "Grid Builder"),
    ("MERGE",                   "Merge Rules"),
    ("ARS_CHECKLIST",           "Data Checklist"),
    ("RBAC_",                   "Users / Roles (RBAC)"),
    ("RLS_",                    "Row-Level Security"),
    ("ARS_MSA_SLOC_SETTINGS",   "MSA SLOC Settings"),
    ("STORE_STOCK",             "Store Stock"),
    ("STORE_SALES",             "Store Sales"),
    ("RETAIL_GEN_ARTICLE",      "Article Master"),
    ("RETAIL_VARIANT_ARTICLE",  "Article Master"),
]

# Friendly verb per audit action_type.
_ACTION_VERB = {
    "INSERT": "Added records to",
    "UPDATE": "Updated",
    "DELETE": "Deleted records from",
    "UPSERT": "Upserted (add/update) records in",
    "BULK_UPLOAD": "Bulk-uploaded to",
    "SCHEMA_CHANGE": "Changed the structure of",
    "TRUNCATE": "Cleared",
}


def _module_for(table_name: str) -> str:
    if not table_name:
        return "Other"
    up = table_name.upper()
    best = None
    for prefix, label in _MODULE_MAP:
        if up.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), label)
    return best[1] if best else table_name


# ---------------------------------------------------------------------------
# Dev-changes source — turn git commits + files changed *today* into pointers.
# This captures development work (code + docs) that never lands in audit_log.
# Fail-open: any git error just yields no dev pointers.
# ---------------------------------------------------------------------------
# Substring → ARS subject, first match wins (order matters, specific first).
_PATH_MODULE = [
    ("services/pend_alc_service",  "Pending Allocation"),
    ("endpoints/pend_alc",         "Pending Allocation"),
    ("docs/manual/pendalc",        "Pending Allocation (docs)"),
    ("_st_priority",               "Listing: Store Priority"),
    ("services/listing",           "Listing & Allocation"),
    ("endpoints/listing",          "Listing & Allocation"),
    ("docs/manual/listing",        "Listing & Allocation (docs)"),
    ("rule_engine",                "Rule Engine / Allocation"),
    ("services/msa",               "MSA Stock Calculation"),
    ("endpoints/msa",              "MSA Stock Calculation"),
    ("docs/manual/msa",            "MSA Stock Calculation (docs)"),
    ("ars_flow_kb",                "Rule Documentation"),
    ("rule_master",                "Rule Documentation"),
    ("data_dictionary",            "Data Dictionary"),
    ("data_export",                "Data Export"),
    ("activity_log",               "Daily Activity Log"),
    ("grid_builder",               "Grid Builder"),
    ("merge_rules",                "Merge Rules"),
    ("hold",                       "Hold Process"),
    ("parked_history",             "Parked / History"),
    ("parallel_pipeline",          "MSA Pipeline"),
    ("scripts/",                   "DB Migrations / Scripts"),
]


def _module_for_path(path: str) -> str:
    pl = path.replace("\\", "/").lower()
    for frag, label in _PATH_MODULE:
        if frag in pl:
            return label
    if pl.endswith(".md") or "/docs/" in pl:
        return "Documentation"
    if pl.endswith((".jsx", ".tsx", ".js", ".ts", ".css")):
        return "Frontend / UI"
    return "Other Code Changes"


def _repo_root() -> Path:
    # Allow override; else derive: .../backend/app/api/v1/endpoints/activity_log.py
    env = os.getenv("ARS_REPO_ROOT")
    if env and Path(env).exists():
        return Path(env)
    return Path(__file__).resolve().parents[5]


def _git(root: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True,
            text=True, timeout=15,
        )
        return out.stdout if out.returncode == 0 else ""
    except Exception:
        return ""


def _dev_pointers(target: date_cls):
    """Build raw pointers from git for one day. Returns list of dicts shaped like
    the audit pointers so the caller can format/validate them uniformly."""
    root = _repo_root()
    if not (root / ".git").exists():
        return []

    start = datetime(target.year, target.month, target.day)
    end = start + timedelta(days=1)

    # file_path -> {"dt": datetime, "note": commit subject or "working tree"}
    files: dict = {}

    # 1) Files in commits authored on the target day (time = commit time).
    log = _git(root, "log",
               f"--since={start:%Y-%m-%d 00:00:00}",
               f"--until={end:%Y-%m-%d 00:00:00}",
               "--name-only", "--pretty=format:__C__%ct|%s")
    cur_dt, cur_msg = None, ""
    for line in log.splitlines():
        if line.startswith("__C__"):
            try:
                ts, msg = line[5:].split("|", 1)
                cur_dt = datetime.fromtimestamp(int(ts))
                cur_msg = msg.strip()
            except Exception:
                cur_dt, cur_msg = None, ""
        elif line.strip() and cur_dt:
            files.setdefault(line.strip(), {"dt": cur_dt, "note": cur_msg})

    # 2) Working-tree changes (uncommitted) whose file mtime is on the target day.
    status = _git(root, "status", "--porcelain", "-uall")
    for line in status.splitlines():
        if not line.strip():
            continue
        rel = line[3:].strip().strip('"')
        if " -> " in rel:                      # renames: "old -> new"
            rel = rel.split(" -> ", 1)[1]
        fp = root / rel
        try:
            if not fp.is_file():
                continue
            mt = datetime.fromtimestamp(fp.stat().st_mtime)
        except Exception:
            continue
        if start <= mt < end:
            files[rel] = {"dt": mt, "note": "working tree (uncommitted)"}

    # Group files → module subjects.
    groups: dict = {}
    for rel, meta in files.items():
        subj = _module_for_path(rel)
        g = groups.setdefault(subj, {
            "subject": subj, "files": [], "first_at": meta["dt"],
            "last_at": meta["dt"], "notes": set(),
        })
        g["files"].append(rel)
        if meta["dt"] < g["first_at"]:
            g["first_at"] = meta["dt"]
        if meta["dt"] > g["last_at"]:
            g["last_at"] = meta["dt"]
        if meta["note"]:
            g["notes"].add(meta["note"])

    pointers = []
    for g in groups.values():
        pointers.append({
            "item_key": f"{g['subject']}|CODE",
            "subject": g["subject"],
            "action_type": "CODE",
            "verb": "Worked on",
            "tables": set(sorted(g["files"])),   # reuse the 'tables' slot for files
            "sources": {"dev"},
            "users": set(),
            "events": len(g["files"]),
            "rows": len(g["files"]),
            "first_at": g["first_at"],
            "last_at": g["last_at"],
            "extra_note": " · ".join(sorted(g["notes"]))[:400],
        })
    return pointers


def _ensure_validation_table(db: Session) -> None:
    """Create the validation table if it doesn't exist (idempotent)."""
    db.execute(text("""
        IF NOT EXISTS (
            SELECT 1 FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_NAME = 'ars_daily_activity_validation'
        )
        BEGIN
            CREATE TABLE ars_daily_activity_validation (
                id            BIGINT IDENTITY(1,1) PRIMARY KEY,
                activity_date DATE          NOT NULL,
                item_key      NVARCHAR(300) NOT NULL,
                item_summary  NVARCHAR(1000) NULL,
                status        NVARCHAR(10)  NOT NULL DEFAULT 'pending',
                note          NVARCHAR(1000) NULL,
                validated_by  NVARCHAR(100) NOT NULL,
                validated_at  DATETIME      NOT NULL DEFAULT GETDATE(),
                CONSTRAINT UQ_daily_activity_val UNIQUE (activity_date, item_key, validated_by)
            );
            CREATE INDEX IX_daily_activity_val_date
                ON ars_daily_activity_validation (activity_date, validated_by);
        END
    """))
    db.commit()


@router.get("", response_model=APIResponse)
async def get_daily_activity(
    date: date_cls = Query(..., description="Day to summarise (YYYY-MM-DD)"),
    changed_by: str = Query(None, description="Filter to one user; default = current user"),
    all_users: bool = Query(False, description="Include every user's activity, not just yours"),
    source: str = Query("both", description="audit | dev | both"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Build review-ready daily pointers from two sources — `audit` (in-app actions
    recorded in audit_log) and `dev` (code/doc changes from git for the day) —
    merged with any saved YES/NO validation for the current reviewer.
    """
    if "SUPER_ADMIN" not in set(current_user.role_codes):
        raise HTTPException(status_code=403, detail="Superadmin only")

    _ensure_validation_table(db)

    src = (source or "both").lower()
    start = datetime(date.year, date.month, date.day)
    end = start + timedelta(days=1)

    # Resolve the user filter: explicit changed_by > (mine, default) > all users.
    user_filter = None if all_users else (changed_by or current_user.username)

    pointers: dict = {}

    # ---- Source A: audit_log (in-app actions) ----
    if src in ("audit", "both"):
        sql = """
            SELECT table_name, action_type, source, changed_by,
                   COUNT(*)          AS n_events,
                   SUM(ISNULL(row_count, 1)) AS n_rows,
                   MIN(changed_at)   AS first_at,
                   MAX(changed_at)   AS last_at
            FROM audit_log
            WHERE changed_at >= :start AND changed_at < :end
            {user_clause}
            GROUP BY table_name, action_type, source, changed_by
        """.format(user_clause=("AND changed_by = :uf" if user_filter else ""))
        params = {"start": start, "end": end}
        if user_filter:
            params["uf"] = user_filter
        try:
            rows = db.execute(text(sql), params).fetchall()
        except Exception as e:
            rows = []
        for r in rows:
            module = _module_for(r.table_name)
            key = f"{module}|{r.action_type}"
            p = pointers.get(key)
            if not p:
                p = {
                    "item_key": key, "subject": module, "action_type": r.action_type,
                    "tables": set(), "sources": set(), "users": set(),
                    "events": 0, "rows": 0,
                    "first_at": r.first_at, "last_at": r.last_at,
                }
                pointers[key] = p
            p["tables"].add(r.table_name)
            if r.source:
                p["sources"].add(r.source)
            if r.changed_by:
                p["users"].add(r.changed_by)
            p["events"] += int(r.n_events or 0)
            p["rows"] += int(r.n_rows or 0)
            if r.first_at and (p["first_at"] is None or r.first_at < p["first_at"]):
                p["first_at"] = r.first_at
            if r.last_at and (p["last_at"] is None or r.last_at > p["last_at"]):
                p["last_at"] = r.last_at

    # ---- Source B: git dev changes (code/docs touched today) ----
    if src in ("dev", "both"):
        for p in _dev_pointers(date):
            pointers[p["item_key"]] = p

    # Load saved validations for this reviewer + day.
    val_rows = db.execute(text("""
        SELECT item_key, status, note
        FROM ars_daily_activity_validation
        WHERE activity_date = :d AND validated_by = :u
    """), {"d": date, "u": current_user.username}).fetchall()
    validations = {v.item_key: {"status": v.status, "note": v.note} for v in val_rows}

    def _fmt(dt):
        return dt.strftime("%H:%M") if dt else None

    items = []
    for p in pointers.values():
        is_dev = p["action_type"] == "CODE"
        verb = p.get("verb") or _ACTION_VERB.get(p["action_type"], p["action_type"].title() + " in")
        unit = "file(s)" if is_dev else "row(s)"
        summary = f"{verb} {p['subject']} — {p['rows']} {unit}"
        if not is_dev:
            summary += f" across {p['events']} action(s)"
        summary += f", {_fmt(p['first_at'])}–{_fmt(p['last_at'])}"
        if p.get("extra_note"):
            summary += f" · {p['extra_note']}"
        v = validations.get(p["item_key"], {"status": "pending", "note": None})
        items.append({
            "item_key": p["item_key"],
            "subject": p["subject"],
            "action_type": p["action_type"],
            "correction": f"{verb} {p['subject']}",
            "summary": summary,
            "tables": sorted(p["tables"]),
            "sources": sorted(p["sources"]),
            "users": sorted(p["users"]),
            "events": p["events"],
            "rows": p["rows"],
            "first_at": _fmt(p["first_at"]),
            "last_at": _fmt(p["last_at"]),
            "status": v["status"],
            "note": v["note"],
        })

    # Order: earliest activity first, then biggest.
    items.sort(key=lambda i: (i["first_at"] or "99:99", -i["rows"]))

    counts = {"yes": 0, "no": 0, "pending": 0}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1

    return APIResponse(data={
        "date": date.isoformat(),
        "user_filter": user_filter or "ALL",
        "total_pointers": len(items),
        "total_rows": sum(i["rows"] for i in items),
        "status_counts": counts,
        "pointers": items,
    })


class ValidateIn(BaseModel):
    date: date_cls
    item_key: str
    status: str            # 'yes' | 'no' | 'pending'
    note: str | None = None
    item_summary: str | None = None


@router.post("/validate", response_model=APIResponse)
async def validate_pointer(
    payload: ValidateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upsert the current reviewer's YES/NO validation for one pointer."""
    if "SUPER_ADMIN" not in set(current_user.role_codes):
        raise HTTPException(status_code=403, detail="Superadmin only")

    status_val = (payload.status or "pending").lower()
    if status_val not in ("yes", "no", "pending"):
        raise HTTPException(status_code=400, detail="status must be yes/no/pending")

    _ensure_validation_table(db)

    db.execute(text("""
        MERGE ars_daily_activity_validation AS tgt
        USING (SELECT :d AS activity_date, :k AS item_key, :u AS validated_by) AS src
            ON  tgt.activity_date = src.activity_date
            AND tgt.item_key      = src.item_key
            AND tgt.validated_by  = src.validated_by
        WHEN MATCHED THEN
            UPDATE SET status = :s, note = :n, item_summary = :sum, validated_at = GETDATE()
        WHEN NOT MATCHED THEN
            INSERT (activity_date, item_key, item_summary, status, note, validated_by, validated_at)
            VALUES (:d, :k, :sum, :s, :n, :u, GETDATE());
    """), {
        "d": payload.date, "k": payload.item_key, "u": current_user.username,
        "s": status_val, "n": payload.note, "sum": payload.item_summary,
    })
    db.commit()

    return APIResponse(message="Validation saved", data={
        "item_key": payload.item_key, "status": status_val,
    })
