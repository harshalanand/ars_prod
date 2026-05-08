"""
Data Checklist API
Table: ARS_CHECKLIST (System DB - Rep_data)
  id | table_name | display_name | group_name | sort_order | is_active
  | last_checked_at | created_at | updated_at

Tracks tables/reports the user needs to keep updated for the allocation process.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from loguru import logger

from app.database.session import get_data_engine
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user
from app.models.rbac import User

router = APIRouter(prefix="/checklist", tags=["Checklist"])
TABLE  = "ARS_CHECKLIST"


# ── Schemas ──────────────────────────────────────────────────────────────────
class ChecklistItemCreate(BaseModel):
    table_name:   str
    display_name: Optional[str] = None
    group_name:   Optional[str] = None

class ChecklistItemUpdate(BaseModel):
    display_name: Optional[str] = None
    group_name:   Optional[str] = None
    sort_order:   Optional[int] = None
    is_active:    Optional[bool] = None

class ReorderItem(BaseModel):
    id:         int
    sort_order: int

class ReorderRequest(BaseModel):
    items: List[ReorderItem]


# ── DDL helper ───────────────────────────────────────────────────────────────
def _run(conn, sql, params=None):
    if params:
        conn.execute(text(sql), params)
    else:
        conn.execute(text(sql))
    conn.commit()


def _ensure_table(engine):
    with engine.connect() as c:
        _run(c, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{TABLE}')
            BEGIN
                CREATE TABLE {TABLE} (
                    id              INT IDENTITY(1,1) PRIMARY KEY,
                    table_name      NVARCHAR(200) NOT NULL,
                    display_name    NVARCHAR(200) NULL,
                    group_name      NVARCHAR(100) NULL,
                    sort_order      INT           NOT NULL DEFAULT 0,
                    is_active       BIT           NOT NULL DEFAULT 1,
                    last_checked_at DATETIME      NULL,
                    created_at      DATETIME      NOT NULL DEFAULT GETDATE(),
                    updated_at      DATETIME      NOT NULL DEFAULT GETDATE(),
                    CONSTRAINT UQ_{TABLE}_table_name UNIQUE (table_name)
                )
            END
        """)
        # Migration: add columns if missing
        for col, dtype in [('last_checked_at','DATETIME NULL'),('group_name','NVARCHAR(100) NULL')]:
            _run(c, f"""
                IF NOT EXISTS (
                    SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_NAME='{TABLE}' AND COLUMN_NAME='{col}'
                )
                BEGIN
                    ALTER TABLE {TABLE} ADD {col} {dtype}
                END
            """)


# ── Live metadata helpers ────────────────────────────────────────────────────
def _table_exists(conn, tbl: str) -> bool:
    cnt = conn.execute(text("""
        SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :tbl
        UNION ALL
        SELECT COUNT(*) FROM INFORMATION_SCHEMA.VIEWS  WHERE TABLE_NAME = :tbl
    """), {"tbl": tbl}).fetchall()
    return any(r[0] > 0 for r in cnt)


def _row_count(conn, tbl: str) -> Optional[int]:
    try:
        # Use partition stats for fast approximate count (avoids full table scan)
        cnt = conn.execute(text("""
            SELECT SUM(p.rows) FROM sys.partitions p
            JOIN sys.tables t ON p.object_id = t.object_id
            WHERE t.name = :tbl AND p.index_id IN (0, 1)
        """), {"tbl": tbl}).scalar()
        if cnt is not None:
            return int(cnt)
        # Fallback for views
        return conn.execute(text(f"SELECT COUNT_BIG(*) FROM [{tbl}]")).scalar()
    except Exception:
        return None


def _available_tables(conn) -> List[str]:
    rows = conn.execute(text("""
        SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_TYPE = 'BASE TABLE' AND TABLE_NAME <> :excl
        UNION
        SELECT TABLE_NAME FROM INFORMATION_SCHEMA.VIEWS
        ORDER BY TABLE_NAME
    """), {"excl": TABLE}).fetchall()
    return [r[0] for r in rows]


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/items", response_model=APIResponse)
def get_checklist(current_user: User = Depends(get_current_user)):
    de = get_data_engine()
    try:
        _ensure_table(de)
    except Exception as e:
        raise HTTPException(500, detail=f"DB setup failed: {e}")

    with de.connect() as conn:
        rows = conn.execute(text(
            f"SELECT id, table_name, display_name, group_name, sort_order, is_active, "
            f"last_checked_at, created_at, updated_at "
            f"FROM {TABLE} ORDER BY group_name ASC, sort_order ASC, id ASC"
        )).fetchall()

        items = []
        dropped_ids = []
        for r in rows:
            tbl = r[1]
            exists = _table_exists(conn, tbl)
            if not exists:
                # Auto-remove checklist items for dropped tables
                dropped_ids.append(r[0])
                continue
            items.append({
                "id":              r[0],
                "table_name":      tbl,
                "display_name":    r[2] or tbl,
                "group_name":      r[3] or "Ungrouped",
                "sort_order":      r[4],
                "is_active":       bool(r[5]),
                "last_checked_at": r[6].isoformat() if r[6] else None,
                "created_at":      r[7].isoformat() if r[7] else None,
                "updated_at":      r[8].isoformat() if r[8] else None,
                "table_exists":    exists,
                "row_count":       _row_count(conn, tbl) if exists else None,
            })

        # Clean up items for tables that no longer exist
        if dropped_ids:
            for did in dropped_ids:
                conn.execute(text(f"DELETE FROM {TABLE} WHERE id = :id"), {"id": did})
            conn.commit()
            logger.info(f"Auto-removed {len(dropped_ids)} checklist items for dropped tables")

    # Collect distinct groups for the frontend
    groups = sorted(set(i["group_name"] for i in items))

    return APIResponse(success=True,
        message=f"{len(items)} checklist item(s)",
        data={"items": items, "total": len(items), "groups": groups})


@router.get("/available-tables", response_model=APIResponse)
def get_available_tables(current_user: User = Depends(get_current_user)):
    de = get_data_engine()
    try:
        _ensure_table(de)
    except Exception as e:
        raise HTTPException(500, detail=f"DB setup failed: {e}")

    with de.connect() as conn:
        added = {r[0] for r in conn.execute(text(
            f"SELECT table_name FROM {TABLE}"
        )).fetchall()}
        all_tbls = _available_tables(conn)
        # Also return existing groups for the dropdown
        groups = [r[0] for r in conn.execute(text(
            f"SELECT DISTINCT group_name FROM {TABLE} WHERE group_name IS NOT NULL ORDER BY group_name"
        )).fetchall()]

    return APIResponse(success=True, message="OK",
        data={"tables": [t for t in all_tbls if t not in added], "groups": groups})


@router.post("/items", response_model=APIResponse)
def add_item(payload: ChecklistItemCreate,
             current_user: User = Depends(get_current_user)):
    de = get_data_engine()
    _ensure_table(de)
    with de.connect() as conn:
        mx = conn.execute(text(f"SELECT ISNULL(MAX(sort_order),0) FROM {TABLE}")).scalar()
        conn.execute(text(f"""
            INSERT INTO {TABLE} (table_name, display_name, group_name, sort_order, is_active, created_at, updated_at)
            VALUES (:tn, :dn, :gn, :so, 1, GETDATE(), GETDATE())
        """), {"tn": payload.table_name,
               "dn": payload.display_name or payload.table_name,
               "gn": payload.group_name or None,
               "so": mx + 1})
        conn.commit()
    return APIResponse(success=True,
        message=f"'{payload.table_name}' added to checklist.")


@router.put("/items/{item_id}", response_model=APIResponse)
def update_item(item_id: int, payload: ChecklistItemUpdate,
                current_user: User = Depends(get_current_user)):
    de = get_data_engine()
    _ensure_table(de)
    sets, params = [], {"id": item_id}
    if payload.display_name is not None:
        sets.append("display_name=:dn"); params["dn"] = payload.display_name
    if payload.group_name is not None:
        sets.append("group_name=:gn"); params["gn"] = payload.group_name or None
    if payload.sort_order is not None:
        sets.append("sort_order=:so"); params["so"] = payload.sort_order
    if payload.is_active is not None:
        sets.append("is_active=:ia"); params["ia"] = 1 if payload.is_active else 0
    if not sets:
        return APIResponse(success=True, message="Nothing to update.")
    sets.append("updated_at=GETDATE()")
    with de.connect() as conn:
        conn.execute(text(f"UPDATE {TABLE} SET {', '.join(sets)} WHERE id=:id"), params)
        conn.commit()
    return APIResponse(success=True, message="Updated.")


@router.put("/reorder", response_model=APIResponse)
def reorder(payload: ReorderRequest,
            current_user: User = Depends(get_current_user)):
    de = get_data_engine()
    _ensure_table(de)
    with de.connect() as conn:
        for item in payload.items:
            conn.execute(text(
                f"UPDATE {TABLE} SET sort_order=:so, updated_at=GETDATE() WHERE id=:id"
            ), {"so": item.sort_order, "id": item.id})
        conn.commit()
    return APIResponse(success=True,
        message=f"{len(payload.items)} item(s) reordered.")


@router.post("/stamp/{table_name}", response_model=APIResponse)
def stamp_checked(table_name: str,
                  current_user: User = Depends(get_current_user)):
    de = get_data_engine()
    _ensure_table(de)
    with de.connect() as conn:
        affected = conn.execute(text(
            f"UPDATE {TABLE} SET last_checked_at=GETDATE(), updated_at=GETDATE() "
            f"WHERE table_name=:tn"
        ), {"tn": table_name}).rowcount
        conn.commit()
    if affected == 0:
        return APIResponse(success=True,
            message=f"'{table_name}' not in checklist — skipped.")
    return APIResponse(success=True,
        message=f"'{table_name}' marked as checked.")


@router.delete("/items/{item_id}", response_model=APIResponse)
def delete_item(item_id: int,
                current_user: User = Depends(get_current_user)):
    de = get_data_engine()
    _ensure_table(de)
    with de.connect() as conn:
        conn.execute(text(f"DELETE FROM {TABLE} WHERE id=:id"), {"id": item_id})
        conn.commit()
    return APIResponse(success=True, message="Removed from checklist.")
