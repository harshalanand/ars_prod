"""
Migration 013 — ARS_PEND_ALC schema enrichment.

Adds BDC tracking, mode, source, and article-detail columns to the
existing ARS_PEND_ALC table. All ALTER TABLE … ADD statements are
idempotent (checks INFORMATION_SCHEMA before adding each column).
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from loguru import logger
from sqlalchemy import text
from app.database.session import get_data_engine

TABLE = "ARS_PEND_ALC"

NEW_COLS = [
    ("ALLOC_MODE",     "NVARCHAR(10)   NULL"),
    ("SOURCE",         "NVARCHAR(20)   NULL"),
    ("GEN_ART_NUMBER", "NVARCHAR(30)   NULL"),
    ("CLR",            "NVARCHAR(20)   NULL"),
    ("BDC_QTY",        "FLOAT          NOT NULL DEFAULT 0"),
    ("LAST_BDC_AT",    "DATETIME       NULL"),
    ("DO_NUMBER",      "NVARCHAR(100)  NULL"),
    ("DO_UPLOADED_AT", "DATETIME       NULL"),
    ("REMARKS",        "NVARCHAR(500)  NULL"),
]

NEW_INDEXES = [
    ("IX_ARS_PEND_ALC_mode",
     f"ON dbo.{TABLE} (ALLOC_MODE, IS_CLOSED)"),
    ("IX_ARS_PEND_ALC_source",
     f"ON dbo.{TABLE} (SOURCE, IS_CLOSED)"),
]


def run():
    engine = get_data_engine()
    with engine.connect() as conn:
        # 1. Ensure table exists
        exists = conn.execute(text(
            f"SELECT CASE WHEN OBJECT_ID('dbo.{TABLE}','U') IS NULL THEN 0 ELSE 1 END"
        )).scalar()
        if not exists:
            logger.warning(f"[013] {TABLE} does not exist — run migration 012 first")
            return

        # 2. Add missing columns
        for col_name, col_def in NEW_COLS:
            already = conn.execute(text("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                 WHERE TABLE_NAME = :t AND COLUMN_NAME = :c
            """), {"t": TABLE, "c": col_name}).scalar() or 0
            if already:
                logger.info(f"[013] {col_name} already exists — skip")
                continue
            conn.execute(text(
                f"ALTER TABLE dbo.{TABLE} ADD [{col_name}] {col_def}"
            ))
            logger.info(f"[013] Added column {col_name}")

        conn.commit()

        # 3. Back-fill ALLOC_MODE / SOURCE for existing rows
        conn.execute(text(f"""
            UPDATE dbo.{TABLE}
               SET ALLOC_MODE = 'AUTO',
                   SOURCE     = 'AUTO'
             WHERE ALLOC_MODE IS NULL
        """))
        conn.commit()
        logger.info("[013] Back-filled ALLOC_MODE/SOURCE for existing rows")

        # 4. Add indexes
        for idx_name, idx_def in NEW_INDEXES:
            already = conn.execute(text("""
                SELECT COUNT(*) FROM sys.indexes
                 WHERE name = :n AND object_id = OBJECT_ID('dbo.' + :t)
            """), {"n": idx_name, "t": TABLE}).scalar() or 0
            if already:
                continue
            try:
                conn.execute(text(
                    f"CREATE INDEX {idx_name} {idx_def}"
                ))
                logger.info(f"[013] Created index {idx_name}")
            except Exception as e:
                logger.warning(f"[013] Index {idx_name} skipped: {e}")
        conn.commit()

    logger.info("[013] Migration complete")


if __name__ == "__main__":
    run()
