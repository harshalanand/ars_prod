"""Diagnostic script for inspecting audit log entries.
Reads database connection from app_settings.json (UI-managed) via the
central engine, so it always targets the same server the running app uses."""
import sys
from sqlalchemy import text

from app.database.session import system_engine

batch_id = sys.argv[1] if len(sys.argv) > 1 else 'UST_30d514aa2c'

with system_engine.connect() as conn:
    print('data_change_log columns:')
    for row in conn.execute(text("""
        SELECT COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'data_change_log'
    """)):
        print(f'  {row[0]}: {row[1]}')

    print('\naudit_log columns:')
    for row in conn.execute(text("""
        SELECT COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'audit_log'
    """)):
        print(f'  {row[0]}: {row[1]}')

    print(f'\nLooking for batch_id: {batch_id}')
    result = conn.execute(text("""
        SELECT id, table_name, action_type, row_count, changed_by,
               changed_at, notes, changed_columns, batch_id
        FROM audit_log WHERE batch_id = :batch_id
    """), {"batch_id": batch_id})
    cols = result.keys()
    rows = result.fetchall()

    if rows:
        print('Audit log entry found:')
        for row in rows:
            for col, val in zip(cols, row):
                print(f'  {col}: {val}')
    else:
        print('No audit_log entry found for this batch_id')

    total = conn.execute(text('SELECT COUNT(*) FROM data_change_log')).scalar()
    print(f'\nTotal rows in data_change_log: {total}')
