"""
GRT ALC — Bin-to-Bin Transfer: table names and schema self-heal.

The DDL lives in ONE place, backend/scripts/037_b2b_module.sql. This module
executes that file batch by batch (split on GO) instead of carrying its own
copy, so the migration and the self-heal cannot drift apart.

Every statement in the file is guarded by IF OBJECT_ID(...) IS NULL, so
ensure_tables() is cheap once the tables exist and safe to call from every
service entry point, the way the FA & CONS services call theirs.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import data_engine

# ── table names ─────────────────────────────────────────────────────────────
BIN_MASTER = "ARS_B2B_BIN_MASTER"
STORE_MASTER = "ARS_B2B_STORE_MASTER"
REQ = "ARS_B2B_REQ"
UPLOAD = "ARS_B2B_UPLOAD"
SETTING = "ARS_B2B_SETTING"
ART_MBQ = "ARS_B2B_ART_MBQ"
MBQ_BUILD = "ARS_B2B_MBQ_BUILD"
SESSION = "ARS_B2B_SESSION"
ALLOC = "ARS_B2B_ALLOC"
BIN_PLAN = "ARS_B2B_BIN_PLAN"

ALL_TABLES: List[str] = [BIN_MASTER, STORE_MASTER, REQ, UPLOAD, SETTING,
                         ART_MBQ, MBQ_BUILD, SESSION, ALLOC, BIN_PLAN]

# Read-only sources, the same two the Streamlit tool reads.
SRC_GRID = "ARS_GRID_MJ_VAR_ART"      # store stock: WERKS, ARTICLE_NUMBER, STK_TTL, SZ
SRC_CONT = "Master_CONT_SZ"           # size contribution: ST_CD, MAJ_CAT, SZ, CONT

# The Streamlit tool's own settings table. Read once, to seed ours, so both
# can run on identical settings while the port is being proven.
LEGACY_SETTING = "B2B_MBQ_SETTING"

_DDL_FILE = Path(__file__).resolve().parents[2] / "scripts" / "037_b2b_module.sql"
_GO = re.compile(r"^\s*GO\s*$", re.IGNORECASE | re.MULTILINE)

_lock = threading.Lock()
_ensured = False


def _batches() -> List[str]:
    sql = _DDL_FILE.read_text(encoding="utf-8")
    return [b.strip() for b in _GO.split(sql) if b.strip()]


def ensure_tables(force: bool = False) -> None:
    """Create any ARS_B2B_* table that is missing. Idempotent.

    Runs the DDL once per process; `force=True` runs it again, which is what
    Overview → Repair tables does after someone drops a table by hand.
    """
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
        logger.info(f"[b2b] schema ensured ({len(batches)} batches from {_DDL_FILE.name})")

    # Settings are part of a usable schema: seed them in the same pass.
    from app.services import b2b_settings
    b2b_settings.seed()


def table_exists(conn, name: str) -> bool:
    return bool(conn.execute(
        text("SELECT CASE WHEN OBJECT_ID(:n, 'U') IS NULL THEN 0 ELSE 1 END"),
        {"n": f"dbo.{name}"}).scalar())


def table_counts(skip: tuple = ()) -> Dict[str, Optional[int]]:
    """Row counts from partition metadata — instant even on the 19M-row
    demand table, where COUNT(*) is not.

    Metadata of a table inside another session's TRUNCATE waits for that
    transaction, so a table being rewritten is passed in `skip` and reported
    as None. It must be excluded BY NAME: filtering on its object_id still
    touches the locked row and waits (measured)."""
    names = [t for t in skip if t in ALL_TABLES]
    # Listed by name, which also keeps out the build's <table>_STAGE while it
    # is being filled.
    wanted = ", ".join(f"'{t}'" for t in ALL_TABLES if t not in names)
    with data_engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT t.name, SUM(p.rows)
              FROM sys.tables t
              JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
             WHERE t.name IN ({wanted})
             GROUP BY t.name
        """)).fetchall()
    have = {r[0]: int(r[1] or 0) for r in rows}
    return {t: (None if t in names else have.get(t, -1)) for t in ALL_TABLES}   # -1 = missing
