"""
Migration 012 — Create ARS_PEND_ALC table.

Tracks approved-but-not-yet-DO'd allocation quantities.
Run once per environment; idempotent (IF NOT EXISTS guards).

Usage:
    python scripts/run_012_pend_alc.py
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.database.session import get_data_engine
from app.services.pend_alc_service import ensure_pend_alc_table
from loguru import logger


def main():
    engine = get_data_engine()
    logger.info("Migration 012: ensuring ARS_PEND_ALC table …")
    with engine.connect() as conn:
        ensure_pend_alc_table(conn)
    logger.info("Migration 012: done.")


if __name__ == "__main__":
    main()
