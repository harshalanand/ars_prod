"""
Merge Rules API
===============
CRUD on ARS_MERGE_RULES — the flat (source_col, source_value, target_value, agg)
mapping that drives MERGE_<col> hierarchy resolution and Master_CONT_MERGE_<col>
derivation.

Endpoints:
  GET    /merge-rules                  – list all rules
  GET    /merge-rules/source-cols      – distinct active source_col values
  POST   /merge-rules                  – upsert one rule
  PUT    /merge-rules/{rule_id}        – update one rule
  DELETE /merge-rules/{rule_id}        – delete one rule
  POST   /merge-rules/refresh/{src}    – manually refresh Master_CONT_MERGE_<src>
"""
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from loguru import logger

from app.database.session import get_data_engine
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user
from app.models.rbac import User
from app.services import derived_masters as dm


router = APIRouter(prefix="/merge-rules", tags=["Merge Rules"])


# ── Schemas ─────────────────────────────────────────────────────────────────
class MergeRulePayload(BaseModel):
    source_col:   str
    source_value: str
    target_value: str
    agg:          str = "SUM"
    active:       bool = True


class MergeRuleUpdate(BaseModel):
    target_value: Optional[str] = None
    agg:          Optional[str] = None
    active:       Optional[bool] = None


class BulkPayload(BaseModel):
    rules: List[MergeRulePayload]
    refresh_after: bool = True   # rebuild derived masters for affected source_cols


# ── Helpers ─────────────────────────────────────────────────────────────────
def _validate_agg(agg: str) -> str:
    a = (agg or "SUM").upper().strip()
    if a not in dm.VALID_AGG:
        raise HTTPException(400, f"agg must be one of {sorted(dm.VALID_AGG)}, got {agg!r}")
    return a


def _validate_source_col(col: str) -> str:
    c = (col or "").strip()
    if not c:
        raise HTTPException(400, "source_col is required")
    if c.upper().startswith(dm.MERGE_COL_PREFIX):
        raise HTTPException(400, "source_col must NOT start with MERGE_ — that's the derived side")
    return c


def _refresh_grid_hierarchy_merge_cols() -> None:
    """
    Re-derive MERGE_<X> columns in ARS_GRID_HIERARCHY from their parent column
    via the updated ARS_MERGE_RULES. Best-effort: failures don't break the API.
    """
    try:
        from app.api.v1.endpoints.grid_builder import _populate_merge_columns
        _populate_merge_columns(get_data_engine())
    except Exception as e:
        logger.warning(f"ARS_GRID_HIERARCHY merge-col refresh failed: {e}")


# ── Endpoints ───────────────────────────────────────────────────────────────
@router.get("", response_model=APIResponse)
def list_rules(current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    with engine.connect() as conn:
        dm.ensure_rules_table(conn)
        rows = conn.execute(text(f"""
            SELECT rule_id, source_col, source_value, target_value, agg, active,
                   created_at, modified_at, modified_by
            FROM {dm.RULES_TABLE}
            ORDER BY source_col, source_value
        """)).fetchall()
    rules = [{
        "rule_id":      r[0],
        "source_col":   r[1],
        "source_value": r[2],
        "target_value": r[3],
        "agg":          r[4],
        "active":       bool(r[5]),
        "created_at":   r[6].isoformat() if r[6] else None,
        "modified_at":  r[7].isoformat() if r[7] else None,
        "modified_by":  r[8],
    } for r in rows]
    return APIResponse(success=True, data={"rules": rules, "total": len(rules)})


@router.get("/source-cols", response_model=APIResponse)
def list_source_cols(current_user: User = Depends(get_current_user)):
    """Distinct source_col values that currently have ≥ 1 active rule."""
    engine = get_data_engine()
    with engine.connect() as conn:
        cols = dm.list_active_source_cols(conn)
    return APIResponse(success=True, data={"source_cols": cols})


@router.post("", response_model=APIResponse)
def create_rule(payload: MergeRulePayload, current_user: User = Depends(get_current_user)):
    src = _validate_source_col(payload.source_col)
    agg = _validate_agg(payload.agg)
    if not payload.source_value or not payload.target_value:
        raise HTTPException(400, "source_value and target_value are required")

    engine = get_data_engine()
    with engine.connect() as conn:
        dm.ensure_rules_table(conn)
        # Enforce: all active rows for one source_col share the same agg
        existing_agg = conn.execute(text(
            f"SELECT TOP 1 agg FROM {dm.RULES_TABLE} "
            f"WHERE source_col = :c AND active = 1"
        ), {"c": src}).scalar()
        if existing_agg and existing_agg.upper() != agg:
            raise HTTPException(
                400,
                f"agg conflict: existing active rules for {src} use {existing_agg!r}, "
                f"new rule uses {agg!r}. All rows of a source_col must share one agg.",
            )

        # Upsert by (source_col, source_value) — UNIQUE constraint enforces this
        existing = conn.execute(text(
            f"SELECT rule_id FROM {dm.RULES_TABLE} "
            f"WHERE source_col = :c AND source_value = :v"
        ), {"c": src, "v": payload.source_value}).scalar()

        if existing:
            conn.execute(text(f"""
                UPDATE {dm.RULES_TABLE}
                SET target_value = :tv, agg = :a, active = :act,
                    modified_at = GETDATE(), modified_by = :u
                WHERE rule_id = :rid
            """), {"tv": payload.target_value, "a": agg, "act": 1 if payload.active else 0,
                   "u": current_user.username, "rid": existing})
            rule_id = existing
            action = "updated"
        else:
            conn.execute(text(f"""
                INSERT INTO {dm.RULES_TABLE}
                    (source_col, source_value, target_value, agg, active, modified_by)
                VALUES (:c, :v, :tv, :a, :act, :u)
            """), {"c": src, "v": payload.source_value, "tv": payload.target_value,
                   "a": agg, "act": 1 if payload.active else 0, "u": current_user.username})
            rule_id = conn.execute(text(f"""
                SELECT rule_id FROM {dm.RULES_TABLE}
                WHERE source_col = :c AND source_value = :v
            """), {"c": src, "v": payload.source_value}).scalar()
            action = "created"
        conn.commit()

        # Refresh derived master (best-effort; doesn't fail the API call)
        refresh = None
        try:
            refresh = dm.refresh_derived_for_source_col(conn, src)
        except Exception as e:
            logger.warning(f"derived refresh failed for {src}: {e}")

    _refresh_grid_hierarchy_merge_cols()
    return APIResponse(
        success=True,
        message=f"Rule {action}",
        data={"rule_id": rule_id, "derived_refresh": refresh},
    )


@router.put("/{rule_id}", response_model=APIResponse)
def update_rule(rule_id: int, payload: MergeRuleUpdate,
                current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    with engine.connect() as conn:
        dm.ensure_rules_table(conn)
        row = conn.execute(text(
            f"SELECT source_col, source_value, target_value, agg, active "
            f"FROM {dm.RULES_TABLE} WHERE rule_id = :r"
        ), {"r": rule_id}).fetchone()
        if not row:
            raise HTTPException(404, f"Rule {rule_id} not found")
        src = row[0]

        sets, params = [], {"r": rule_id, "u": current_user.username}
        if payload.target_value is not None:
            sets.append("target_value = :tv"); params["tv"] = payload.target_value
        if payload.agg is not None:
            agg = _validate_agg(payload.agg)
            sets.append("agg = :a"); params["a"] = agg
        if payload.active is not None:
            sets.append("active = :act"); params["act"] = 1 if payload.active else 0
        if not sets:
            raise HTTPException(400, "no fields to update")

        sets.append("modified_at = GETDATE()")
        sets.append("modified_by = :u")
        conn.execute(text(
            f"UPDATE {dm.RULES_TABLE} SET {', '.join(sets)} WHERE rule_id = :r"
        ), params)
        conn.commit()

        refresh = None
        try:
            refresh = dm.refresh_derived_for_source_col(conn, src)
        except Exception as e:
            logger.warning(f"derived refresh failed for {src}: {e}")

    _refresh_grid_hierarchy_merge_cols()
    return APIResponse(success=True, message="Rule updated",
                       data={"rule_id": rule_id, "derived_refresh": refresh})


@router.delete("/{rule_id}", response_model=APIResponse)
def delete_rule(rule_id: int, current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    with engine.connect() as conn:
        dm.ensure_rules_table(conn)
        row = conn.execute(text(
            f"SELECT source_col FROM {dm.RULES_TABLE} WHERE rule_id = :r"
        ), {"r": rule_id}).fetchone()
        if not row:
            raise HTTPException(404, f"Rule {rule_id} not found")
        src = row[0]

        conn.execute(text(f"DELETE FROM {dm.RULES_TABLE} WHERE rule_id = :r"),
                     {"r": rule_id})
        conn.commit()

        refresh = None
        try:
            refresh = dm.refresh_derived_for_source_col(conn, src)
        except Exception as e:
            logger.warning(f"derived refresh failed for {src}: {e}")

    _refresh_grid_hierarchy_merge_cols()
    return APIResponse(success=True, message="Rule deleted",
                       data={"derived_refresh": refresh})


@router.post("/refresh/{source_col}", response_model=APIResponse)
def refresh_derived(source_col: str, current_user: User = Depends(get_current_user)):
    """Manual refresh of Master_CONT_MERGE_<source_col> from its parent."""
    src = _validate_source_col(source_col)
    engine = get_data_engine()
    with engine.connect() as conn:
        result = dm.refresh_derived_for_source_col(conn, src)
    _refresh_grid_hierarchy_merge_cols()
    return APIResponse(success=True, data=result)


@router.post("/bulk", response_model=APIResponse)
def bulk_upsert(payload: BulkPayload,
                current_user: User = Depends(get_current_user)):
    """
    Upsert many rules in one shot. Used by the bulk-upload UI.

    - Validates every row before writing (atomic preflight).
    - Upserts by UNIQUE (source_col, source_value).
    - Refreshes Master_CONT_MERGE_<col> once per affected source_col (if refresh_after).
    - Returns per-source counts + per-source refresh result.
    """
    if not payload.rules:
        raise HTTPException(400, "rules array is empty")
    if len(payload.rules) > 5000:
        raise HTTPException(400, f"too many rules in one batch: {len(payload.rules)} (max 5000)")

    # ── Preflight: validate every row before opening a connection ──
    seen: set = set()
    errors: List[Dict] = []
    cleaned: List[Dict] = []
    agg_by_src: Dict[str, str] = {}

    for idx, r in enumerate(payload.rules):
        try:
            src = _validate_source_col(r.source_col)
            agg = _validate_agg(r.agg)
        except HTTPException as e:
            errors.append({"row": idx + 1, "error": e.detail}); continue

        if not r.source_value or not r.target_value:
            errors.append({"row": idx + 1, "error": "source_value and target_value required"}); continue

        key = (src.upper(), r.source_value)
        if key in seen:
            errors.append({"row": idx + 1, "error": f"duplicate within batch: {src}/{r.source_value}"}); continue
        seen.add(key)

        prior = agg_by_src.get(src)
        if prior and prior != agg:
            errors.append({
                "row": idx + 1,
                "error": f"agg conflict within batch for {src}: {prior!r} vs {agg!r}",
            }); continue
        agg_by_src[src] = agg

        cleaned.append({
            "source_col":   src,
            "source_value": r.source_value,
            "target_value": r.target_value,
            "agg":          agg,
            "active":       bool(r.active),
        })

    if errors:
        raise HTTPException(400, {"message": "validation failed", "errors": errors})

    engine = get_data_engine()
    with engine.connect() as conn:
        dm.ensure_rules_table(conn)

        # Reject if any source_col's incoming agg conflicts with existing active rows
        for src, agg in agg_by_src.items():
            existing_agg = conn.execute(text(
                f"SELECT TOP 1 agg FROM {dm.RULES_TABLE} "
                f"WHERE source_col = :c AND active = 1"
            ), {"c": src}).scalar()
            if existing_agg and existing_agg.upper() != agg:
                raise HTTPException(
                    400,
                    f"agg conflict for {src}: existing active rules use {existing_agg!r}, "
                    f"batch uses {agg!r}",
                )

        inserted = updated = 0
        for row in cleaned:
            existing = conn.execute(text(
                f"SELECT rule_id FROM {dm.RULES_TABLE} "
                f"WHERE source_col = :c AND source_value = :v"
            ), {"c": row["source_col"], "v": row["source_value"]}).scalar()
            if existing:
                conn.execute(text(f"""
                    UPDATE {dm.RULES_TABLE}
                    SET target_value = :tv, agg = :a, active = :act,
                        modified_at = GETDATE(), modified_by = :u
                    WHERE rule_id = :rid
                """), {"tv": row["target_value"], "a": row["agg"],
                       "act": 1 if row["active"] else 0,
                       "u": current_user.username, "rid": existing})
                updated += 1
            else:
                conn.execute(text(f"""
                    INSERT INTO {dm.RULES_TABLE}
                        (source_col, source_value, target_value, agg, active, modified_by)
                    VALUES (:c, :v, :tv, :a, :act, :u)
                """), {"c": row["source_col"], "v": row["source_value"],
                       "tv": row["target_value"], "a": row["agg"],
                       "act": 1 if row["active"] else 0,
                       "u": current_user.username})
                inserted += 1
        conn.commit()

        refreshes: Dict[str, Dict] = {}
        if payload.refresh_after:
            for src in agg_by_src.keys():
                try:
                    refreshes[src] = dm.refresh_derived_for_source_col(conn, src)
                except Exception as e:
                    logger.warning(f"bulk: derived refresh failed for {src}: {e}")
                    refreshes[src] = {"status": "error", "reason": str(e)}

    if payload.refresh_after:
        _refresh_grid_hierarchy_merge_cols()
    return APIResponse(
        success=True,
        message=f"Bulk: {inserted} inserted, {updated} updated across {len(agg_by_src)} dimension(s)",
        data={
            "inserted":   inserted,
            "updated":    updated,
            "total":      inserted + updated,
            "dimensions": sorted(agg_by_src.keys()),
            "derived_refresh": refreshes,
        },
    )
