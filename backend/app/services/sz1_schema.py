"""
1SZ ALC — size allocation: table names and schema self-heal.

The DDL lives in ONE place, backend/scripts/039_sz1_module.sql. This module
executes that file batch by batch (split on GO), the same way b2b_schema runs
037, so the migration and the self-heal cannot drift apart. Every statement
is guarded by IF OBJECT_ID(...) IS NULL: cheap once the tables exist.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import data_engine

# ── the module's own tables ─────────────────────────────────────────────────
SZ_BASE = "ARS_SZ1_MSA_SZ_BASE"            # Step 1 — broken-size candidates
REQ_BASE = "ARS_SZ1_ST_MJ_SZ_REQ_BASE"     # Step 2 — store × MAJ_CAT × SZ need
LINES = "ARS_SZ1_ST_ART_REQ"               # Step 3 — store × article lines, per session
SESSION = "ARS_SZ1_SESSION"                # one row per Step 3 run
BUILD = "ARS_SZ1_BUILD"                    # one row per Step 1 / Step 2 build
SETTING = "ARS_SZ1_SETTING"                # Step 1 rule switches and values
GAP = "ARS_SZ1_GAP"                        # Step 1 zero-CONT gap, per build

ALL_TABLES: List[str] = [SZ_BASE, REQ_BASE, LINES, SESSION, BUILD, SETTING, GAP]

# ── read-only sources, exactly the ones the Streamlit tool reads ───────────
MSA_TOTAL = "ARS_MSA_TOTAL"
HIERARCHY = "ARS_GRID_HIERARCHY"
STORE_MASTER = "Master_ALC_INPUT_ST_MASTER"
GRID_MJ = "ARS_GRID_MJ"
GRID_VAR_ART = "ARS_GRID_MJ_VAR_ART"
CONT_SZ = "Master_CONT_SZ"
CALC_ST_MAJ_CAT = "ARS_CALC_ST_MAJ_CAT"     # I_ROD at store + MAJ_CAT
CALC_ST_ART = "ARS_CALC_ST_ART"             # I_ROD at store + option, overrides

SOURCES: List[str] = [MSA_TOTAL, HIERARCHY, STORE_MASTER, GRID_MJ, GRID_VAR_ART, CONT_SZ,
                      CALC_ST_MAJ_CAT, CALC_ST_ART]

_DDL_FILE = Path(__file__).resolve().parents[2] / "scripts" / "039_sz1_module.sql"
_GO = re.compile(r"^\s*GO\s*$", re.IGNORECASE | re.MULTILINE)

_lock = threading.Lock()
_ensured = False


def _batches() -> List[str]:
    sql = _DDL_FILE.read_text(encoding="utf-8")
    return [b.strip() for b in _GO.split(sql) if b.strip()]


def ensure_tables(force: bool = False) -> None:
    """Create any ARS_SZ1_* table that is missing. Idempotent; once per process
    unless forced (1SZ ALC → Repair tables)."""
    global _ensured
    if _ensured and not force:
        return
    with _lock:
        if _ensured and not force:
            return
        batches = _batches()
        with data_engine.begin() as conn:
            for b in batches:
                conn.execute(text(b))
        _ensured = True
        logger.info(f"[sz1] schema ensured ({len(batches)} batches from {_DDL_FILE.name})")


def table_counts(skip: tuple = ()) -> Dict[str, Optional[int]]:
    """Row counts of the module's tables and its sources, from partition
    metadata (instant even on the 12M-row grid). -1 = the table is missing;
    None = skipped because a build holds it (its metadata would wait for the
    build's TRUNCATE — excluded BY NAME, as b2b_schema found)."""
    names = ALL_TABLES + SOURCES
    wanted = ", ".join(f"'{t}'" for t in names if t not in skip)
    with data_engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT t.name, SUM(p.rows)
              FROM sys.tables t
              JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
             WHERE t.name IN ({wanted}) AND SCHEMA_NAME(t.schema_id) = 'dbo'
             GROUP BY t.name
        """)).fetchall()
    have = {r[0]: int(r[1] or 0) for r in rows}
    return {t: (None if t in skip else have.get(t, -1)) for t in names}
