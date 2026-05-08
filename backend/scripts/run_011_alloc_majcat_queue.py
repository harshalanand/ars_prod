"""
Migration script: Create ARS_ALLOC_MAJCAT_QUEUE on the Data DB (rep_data).

Backs the parallel allocation feature (Phase 1 / Phase 2). One row per
(BATCH_ID, MAJ_CAT). Workers claim rows via UPDATE WITH (UPDLOCK, READPAST).

Run from: backend directory
Usage:    python scripts/run_011_alloc_majcat_queue.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from app.database.session import data_engine


DDL = """
IF OBJECT_ID('dbo.ARS_ALLOC_MAJCAT_QUEUE','U') IS NULL
CREATE TABLE dbo.ARS_ALLOC_MAJCAT_QUEUE (
    BATCH_ID         NVARCHAR(50)   NOT NULL,
    MAJ_CAT          NVARCHAR(50)   NOT NULL,
    OPT_COUNT        INT            NOT NULL,
    STATUS           NVARCHAR(20)   NOT NULL DEFAULT 'PENDING',
    WORKER_ID        INT            NULL,
    ATTEMPTS         INT            NOT NULL DEFAULT 0,
    PICKED_AT        DATETIME       NULL,
    COMPLETED_AT     DATETIME       NULL,
    SHIP_QTY         FLOAT          NULL,
    HOLD_QTY         FLOAT          NULL,
    ROWS_AFFECTED    INT            NULL,
    DURATION_SEC     FLOAT          NULL,
    ERROR_MSG        NVARCHAR(2000) NULL,
    ALLOCATION_MODE  NVARCHAR(20)   NULL,
    CREATED_AT       DATETIME       NOT NULL DEFAULT GETDATE(),
    CONSTRAINT PK_ARS_ALLOC_MAJCAT_QUEUE PRIMARY KEY (BATCH_ID, MAJ_CAT)
);
"""

INDEXES = [
    ("IX_ARS_ALLOC_MAJCAT_QUEUE_status",
     "ON dbo.ARS_ALLOC_MAJCAT_QUEUE (BATCH_ID, STATUS, OPT_COUNT DESC)"),
    ("IX_ARS_ALLOC_MAJCAT_QUEUE_created",
     "ON dbo.ARS_ALLOC_MAJCAT_QUEUE (CREATED_AT DESC) "
     "INCLUDE (STATUS, ALLOCATION_MODE)"),
]


def run_migration():
    raw = data_engine.raw_connection()
    raw.autocommit = True
    try:
        cur = raw.cursor()
        cur.execute(DDL)
        print("OK  ARS_ALLOC_MAJCAT_QUEUE table ensured")

        for idx_name, idx_def in INDEXES:
            try:
                cur.execute(
                    f"IF NOT EXISTS (SELECT 1 FROM sys.indexes "
                    f"WHERE name='{idx_name}') "
                    f"CREATE INDEX {idx_name} {idx_def}"
                )
                print(f"OK  index {idx_name}")
            except Exception as e:
                print(f"WARN index {idx_name}: {e}")
        cur.close()
        print("\nMigration 011 completed successfully.")
    finally:
        raw.close()


if __name__ == "__main__":
    run_migration()
