"""
Migration script: Deploy dbo.usp_ars_allocate_majcat (and helper procs) to
the Data DB. Required for SQL Parallel mode (Phase 2).

Idempotent: drops and recreates the procs every time. Safe to re-run after
editing the .sql file.

Run from: backend directory
Usage:    python scripts/run_012_deploy_alloc_proc.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.session import data_engine


SQL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "sql", "usp_ars_allocate_majcat.sql",
)


def split_on_go(sql: str):
    """Split a T-SQL script on GO batch separators (case-insensitive,
    line-by-line). pyodbc / SQLAlchemy can't EXEC multi-batch scripts
    in one call."""
    out, buf = [], []
    for line in sql.splitlines():
        if line.strip().upper() == "GO":
            if buf:
                out.append("\n".join(buf))
                buf = []
        else:
            buf.append(line)
    if buf:
        out.append("\n".join(buf))
    return [b for b in out if b.strip()]


def run():
    if not os.path.exists(SQL_PATH):
        raise FileNotFoundError(SQL_PATH)
    with open(SQL_PATH, encoding="utf-8") as f:
        script = f.read()

    batches = split_on_go(script)
    print(f"Deploying {len(batches)} batches from {SQL_PATH}...")

    raw = data_engine.raw_connection()
    raw.autocommit = True
    try:
        cur = raw.cursor()
        for i, b in enumerate(batches, 1):
            try:
                cur.execute(b)
                first = b.strip().splitlines()[0][:80]
                print(f"OK  batch {i}/{len(batches)}: {first} ...")
            except Exception as e:
                print(f"FAIL batch {i}/{len(batches)}: {e}")
                raise
        cur.close()
        print("\nDeploy complete.")
    finally:
        raw.close()


if __name__ == "__main__":
    run()
