"""
Store Stock - SLOC Settings API
Table: ARS_STORE_SLOC_SETTINGS (System DB)
  id INT IDENTITY PK | sloc NVARCHAR(50) UNIQUE | kpi NVARCHAR(200) | status NVARCHAR(20) | created_at | updated_at
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, validator
from sqlalchemy import text
from loguru import logger

from app.database.session import get_data_engine
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user
from app.models.rbac import User

router       = APIRouter(prefix="/store-stock", tags=["Store Stock"])
TABLE        = "ARS_STORE_SLOC_SETTINGS"
OLD_TABLE    = "ARS_SLOC_SETTINGS"
VALID_STATUS = {"Active", "Inactive"}


# ── Schemas ──────────────────────────────────────────────────────────────────
class SlocSetting(BaseModel):
    sloc:   str
    kpi:    Optional[str] = None
    status: str = "Active"
    @validator("status")
    def _chk(cls, v):
        if v not in VALID_STATUS: raise ValueError("status must be Active or Inactive")
        return v

class BulkUpdateItem(BaseModel):
    sloc:   str
    kpi:    Optional[str] = None
    status: str = "Active"
    @validator("status")
    def _chk(cls, v):
        if v not in VALID_STATUS: raise ValueError("status must be Active or Inactive")
        return v

class BulkUpdateRequest(BaseModel):
    items: List[BulkUpdateItem]


# ── DDL helpers (each step in its own batch – SQL Server parse-time safety) ──
def _run(conn, sql, params=None):
    """Execute one SQL batch and commit."""
    if params:
        conn.execute(text(sql), params)
    else:
        conn.execute(text(sql))
    conn.commit()

def _ensure_table(engine):
    """
    Auto-create / auto-migrate ARS_STORE_SLOC_SETTINGS.
    Each ALTER/CREATE is a separate execute() so SQL Server never compiles
    a batch that references columns which don't exist yet.
    """
    with engine.connect() as c:

        # ── Step 1: rename old table (nested IF is required – SQL Server has no IF…AND) ──
        _run(c, f"""
            IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{OLD_TABLE}')
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{TABLE}')
                BEGIN
                    EXEC sp_rename '{OLD_TABLE}', '{TABLE}'
                END
            END
        """)

        # ── Step 2: create fresh if still missing ────────────────────────────
        _run(c, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{TABLE}')
            BEGIN
                CREATE TABLE {TABLE} (
                    id         INT IDENTITY(1,1) PRIMARY KEY,
                    sloc       NVARCHAR(50)  NOT NULL,
                    kpi        NVARCHAR(200) NULL,
                    status     NVARCHAR(20)  NOT NULL DEFAULT 'Active',
                    created_at DATETIME      NOT NULL DEFAULT GETDATE(),
                    updated_at DATETIME      NOT NULL DEFAULT GETDATE(),
                    CONSTRAINT UQ_{TABLE}_sloc UNIQUE (sloc)
                )
            END
        """)

        # ── Step 3: add status column if missing (NULL first, set default after) ──
        _run(c, f"""
            IF NOT EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME='{TABLE}' AND COLUMN_NAME='status'
            )
            BEGIN
                ALTER TABLE {TABLE} ADD status NVARCHAR(20) NULL
            END
        """)

        # ── Step 4: copy is_active→status via dynamic SQL (avoids parse-time error) ──
        _run(c, f"""
            IF EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME='{TABLE}' AND COLUMN_NAME='is_active'
            )
            BEGIN
                EXEC('UPDATE {TABLE} SET status = CASE WHEN is_active=1 THEN ''Active'' ELSE ''Inactive'' END')
            END
        """)

        # ── Step 5: fill any remaining NULLs in status ───────────────────────
        _run(c, f"UPDATE {TABLE} SET status='Active' WHERE status IS NULL")

        # ── Step 6: drop DEFAULT constraint on is_active FIRST ───────────────
        # SQL Server blocks DROP COLUMN when a DEFAULT constraint exists.
        # Find the auto-generated constraint name and drop it via dynamic SQL.
        _run(c, f"""
            IF EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME='{TABLE}' AND COLUMN_NAME='is_active'
            )
            BEGIN
                DECLARE @con NVARCHAR(256)
                SELECT @con = dc.name
                FROM sys.default_constraints dc
                JOIN sys.columns col
                  ON dc.parent_object_id = col.object_id
                 AND dc.parent_column_id = col.column_id
                JOIN sys.tables t ON col.object_id = t.object_id
                WHERE t.name = '{TABLE}' AND col.name = 'is_active'
                IF @con IS NOT NULL
                    EXEC('ALTER TABLE {TABLE} DROP CONSTRAINT [' + @con + ']')
            END
        """)

        # ── Step 7: now drop the column safely ───────────────────────────────
        _run(c, f"""
            IF EXISTS (
                SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME='{TABLE}' AND COLUMN_NAME='is_active'
            )
            BEGIN
                ALTER TABLE {TABLE} DROP COLUMN is_active
            END
        """)


def _find_date_column(engine) -> Optional[str]:
    """Return the date column in ET_STORE_STOCK — by type first, then by name."""
    try:
        with engine.connect() as conn:
            # 1) Try columns with date/datetime type
            row = conn.execute(text("""
                SELECT TOP 1 COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = 'ET_STORE_STOCK'
                  AND DATA_TYPE IN ('date','datetime','datetime2','smalldatetime')
                ORDER BY ORDINAL_POSITION
            """)).fetchone()
            if row:
                return row[0]

            # 2) Try well-known date column names (any type — could be varchar)
            row = conn.execute(text("""
                SELECT TOP 1 COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = 'ET_STORE_STOCK'
                  AND UPPER(COLUMN_NAME) IN ('DATE','BUDAT','ERDAT','REPORT_DATE',
                       'POSTING_DATE','DOC_DATE','CREATED_DATE','UPDATED_AT')
                ORDER BY ORDINAL_POSITION
            """)).fetchone()
            if row:
                return row[0]

        return None
    except Exception as e:
        logger.warning(f"_find_date_column error: {e}")
        return None


def _safe_iso(val) -> Optional[str]:
    """Convert a date value (could be date, datetime, or string) to ISO string."""
    if val is None:
        return None
    if hasattr(val, 'isoformat'):
        return val.isoformat()
    # Try parsing string dates
    s = str(val).strip()
    if not s:
        return None
    try:
        from datetime import datetime
        for fmt in ('%Y-%m-%d', '%Y%m%d', '%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d %H:%M:%S'):
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                continue
    except Exception:
        pass
    return s  # Return as-is if can't parse


def _fetch_distinct_slocs(data_engine) -> List[dict]:
    """Return list of {sloc, report_date} from ET_STORE_STOCK."""
    try:
        date_col = _find_date_column(data_engine)
        with data_engine.connect() as conn:
            if date_col:
                rows = conn.execute(text(
                    f"SELECT sloc, MAX([{date_col}]) AS report_date "
                    f"FROM ET_STORE_STOCK WITH (NOLOCK) GROUP BY sloc ORDER BY sloc ASC"
                )).fetchall()
                slocs = [{"sloc": str(r[0]), "report_date": _safe_iso(r[1])}
                         for r in rows if r[0] is not None]
            else:
                rows = conn.execute(text(
                    "SELECT DISTINCT sloc FROM ET_STORE_STOCK WITH (NOLOCK) ORDER BY sloc ASC"
                )).fetchall()
                slocs = [{"sloc": str(r[0]), "report_date": None}
                         for r in rows if r[0] is not None]

        # Add PEND_ALC as virtual SLOC if ARS_pend_alc table exists
        with data_engine.connect() as conn:
            pend_exists = conn.execute(text(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='ARS_pend_alc'"
            )).scalar()
            existing = {s["sloc"] for s in slocs}
            if pend_exists and 'PEND_ALC' not in existing:
                slocs.append({"sloc": "PEND_ALC", "report_date": None})

        return slocs
    except Exception as e:
        logger.error(f"ET_STORE_STOCK query failed: {e}")
        raise


def _fetch_saved(engine) -> dict:
    with engine.connect() as conn:
        rows = conn.execute(text(
            f"SELECT id, sloc, kpi, status, created_at, updated_at FROM {TABLE}"
        )).fetchall()
    return {
        str(r[1]): {
            "id": r[0], "sloc": str(r[1]), "kpi": r[2],
            "status": r[3] if r[3] in VALID_STATUS else "Active",
            "created_at": r[4], "updated_at": r[5],
        }
        for r in rows
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/sloc-settings", response_model=APIResponse)
def get_sloc_settings(current_user: User = Depends(get_current_user)):
    de = get_data_engine()   # ARS_STORE_SLOC_SETTINGS lives in Rep_data

    try:
        _ensure_table(de)
    except Exception as e:
        logger.error(f"_ensure_table failed: {e}")
        raise HTTPException(500, detail=f"DB schema setup failed: {e}")

    try:
        slocs = _fetch_distinct_slocs(de)
    except Exception as e:
        raise HTTPException(500, detail=f"Failed to read ET_STORE_STOCK: {e}")

    try:
        saved = _fetch_saved(de)
    except Exception as e:
        raise HTTPException(500, detail=f"Failed to read {TABLE}: {e}")

    # slocs is now a list of {"sloc": ..., "report_date": ...}
    sloc_dates = {s["sloc"]: s["report_date"] for s in slocs}

    result = []
    for entry in slocs:
        s = entry["sloc"]
        if s in saved:
            result.append({**saved[s], "report_date": entry["report_date"], "is_new": False})
        else:
            result.append({"id": None, "sloc": s, "kpi": None, "status": "New",
                           "report_date": entry["report_date"],
                           "created_at": None, "updated_at": None, "is_new": True})

    new_count = sum(1 for r in result if r["is_new"])

    # Global max date across all SLOCs for freshness alert
    all_dates = [e["report_date"] for e in slocs if e["report_date"]]
    data_date = max(all_dates) if all_dates else None

    return APIResponse(success=True,
        message=f"Loaded {len(result)} SLOC entries ({new_count} new)",
        data={"items": result, "total": len(result), "data_date": data_date})


@router.post("/sync", response_model=APIResponse)
def sync_slocs(current_user: User = Depends(get_current_user)):
    """Check for new SLOCs in ET_STORE_STOCK (read-only, no DB writes).
    New SLOCs are shown in the GET response as 'New' status until the user
    explicitly saves them via Save Changes."""
    de = get_data_engine()
    try:
        _ensure_table(de)
    except Exception as e:
        raise HTTPException(500, detail=f"DB schema setup failed: {e}")

    try:
        slocs = _fetch_distinct_slocs(de)
    except Exception as e:
        raise HTTPException(500, detail=f"Failed to read ET_STORE_STOCK: {e}")

    saved     = _fetch_saved(de)
    new_slocs = [s["sloc"] for s in slocs if s["sloc"] not in saved]

    return APIResponse(success=True,
        message=f"Sync check complete. {len(new_slocs)} new SLOC(s) found.",
        data={"new_count": len(new_slocs), "new_slocs": new_slocs})


@router.put("/sloc-settings/{sloc}", response_model=APIResponse)
def update_sloc_setting(sloc: str, payload: SlocSetting,
                        current_user: User = Depends(get_current_user)):
    de = get_data_engine()   # ARS_STORE_SLOC_SETTINGS lives in Rep_data
    try: _ensure_table(de)
    except Exception as e: raise HTTPException(500, detail=str(e))

    with de.connect() as conn:
        conn.execute(text(f"""
            IF EXISTS (SELECT 1 FROM {TABLE} WHERE sloc=:sloc)
                UPDATE {TABLE} SET kpi=:kpi, status=:status, updated_at=GETDATE() WHERE sloc=:sloc
            ELSE
                INSERT INTO {TABLE}(sloc,kpi,status,created_at,updated_at)
                VALUES(:sloc,:kpi,:status,GETDATE(),GETDATE())
        """), {"sloc": sloc, "kpi": payload.kpi, "status": payload.status})
        conn.commit()
    return APIResponse(success=True, message=f"SLOC '{sloc}' updated.",
                       data={"sloc": sloc, "kpi": payload.kpi, "status": payload.status})


@router.put("/sloc-settings", response_model=APIResponse)
def bulk_update(payload: BulkUpdateRequest, current_user: User = Depends(get_current_user)):
    de = get_data_engine()   # ARS_STORE_SLOC_SETTINGS lives in Rep_data
    try: _ensure_table(de)
    except Exception as e: raise HTTPException(500, detail=str(e))

    with de.connect() as conn:
        for item in payload.items:
            conn.execute(text(f"""
                IF EXISTS (SELECT 1 FROM {TABLE} WHERE sloc=:sloc)
                    UPDATE {TABLE} SET kpi=:kpi, status=:status, updated_at=GETDATE() WHERE sloc=:sloc
                ELSE
                    INSERT INTO {TABLE}(sloc,kpi,status,created_at,updated_at)
                    VALUES(:sloc,:kpi,:status,GETDATE(),GETDATE())
            """), {"sloc": item.sloc, "kpi": item.kpi, "status": item.status})
        conn.commit()
    return APIResponse(success=True, message=f"{len(payload.items)} SLOC(s) updated.",
                       data={"updated_count": len(payload.items)})
