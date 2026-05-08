# Data Editor

Spreadsheet-style editor for any table — view, edit cells inline, add
rows, delete rows. The "no SQL needed" alternative to running ad-hoc
UPDATEs.

---

## What it does

Opens a paginated, filterable view over any table (subject to permission),
tracks per-cell edits client-side, and on Save commits each change as
a row-level UPDATE/INSERT/DELETE through `DirectUpdateEngine`.

## Files

| Layer | File |
|---|---|
| Page | `frontend/src/pages/DataEditorPage.jsx` |
| API | `app/api/v1/endpoints/data_ops.py` (editor routes) |
| Engine | `app/services/upsert_engine.py` → `class DirectUpdateEngine` |
| Permission | `DATA_EDITOR` (and `TABLE_ALTER` for some destructive ops) |

## End-to-end flow

```
1. User picks a table → frontend GETs schema (cols, types, PKs)
2. User applies pre-load filters → GET /editor/<table>/data?<filters>
3. ag-Grid renders with PK columns highlighted gold, non-PK editable
4. User edits a cell:
     - Cell turns green (pending change)
     - "Unsaved changes: 3" indicator updates
5. User clicks Save:
     - PUT /editor/<table>/rows  body=[{pk:..., changes:{...}}, ...]
     - Backend loops, calling DirectUpdateEngine.update_record per row
     - Each row: SELECT current → diff → UPDATE only changed cols → audit row
6. User adds a new row by filling all PK columns:
     - Auto-saves on PK completion
     - POST /editor/<table>/rows
7. User selects rows + Delete:
     - Confirm dialog
     - DELETE /editor/<table>/rows  body=[pk1, pk2, ...]
```

## DirectUpdateEngine — what it does

```python
# upsert_engine.py
class DirectUpdateEngine:
    def update_record(self, table_name, primary_key_columns, primary_key_values,
                      updates, changed_by, ip_address=None, user_agent=None):
        # 1. Fetch current row
        old = SELECT * FROM <table> WHERE <pk_conditions>

        # 2. Detect actual changes
        actual_changes = {col: v for col, v in updates.items()
                          if str(old[col]) != str(v)}
        if not actual_changes:
            return {"changed": False}

        # 3. UPDATE only changed columns
        UPDATE <table>
        SET col1 = :upd_col1, col2 = :upd_col2
        WHERE <pk_conditions>

        # 4. Audit row
        audit.log_update(table=..., record_pk=..., old_data=...,
                         new_data=actual_changes, changed_columns=[...])

        return {"changed": True, "changed_columns": [...]}
```

## Why per-row UPDATE?

For 1-100 row edits (the common case), per-row UPDATE is fine and
simple. For >100 row edits, this becomes a bottleneck — each UPDATE
is a separate round-trip.

### Switching to bulk MERGE for big batches

If your editor sees frequent batch edits, add a fast path:

```python
def update_records_bulk(self, table_name, pks, updates_per_pk, changed_by):
    # Build a #temp with the desired state
    # Run one MERGE to apply all updates
    # Audit per-row from the MERGE OUTPUT clause
    ...
```

Use `UpsertEngine.upsert(...)` directly with a small DataFrame —
that's literally what the bulk upsert is for.

---

## Optimistic locking — the missing safety net

Today: if Alice and Bob both open the same row, Alice saves first,
then Bob saves — Bob silently overwrites Alice. There's no warning.

### How to add it

Add a `rowversion` (or `timestamp`) column to editable tables:

```sql
ALTER TABLE <table> ADD row_version ROWVERSION;
```

Frontend includes the row_version when GET-ing each row. On save:

```python
UPDATE <table>
SET col = :v, row_version_was = row_version
WHERE pk = :pk
  AND row_version = :original_row_version

if affected == 0:
    raise HTTPException(409, "Row was modified by another user since you loaded it.")
```

Frontend on 409: re-fetch the row, show a diff dialog, let user
re-apply changes.

---

## Cascade filters (the dropdowns)

When you filter by `WERKS=DH24`, the next filter dropdown should only
show values present in that subset.

```python
# data_ops.py
GET /editor/<table>/distinct?col=SLOC&WERKS=DH24

SELECT DISTINCT [SLOC]
FROM [<table>]
WHERE [WERKS] = :v
ORDER BY [SLOC]
```

### Why it can be slow

A 50M-row table with 10k unique SLOCs returns fast (the index makes
DISTINCT almost free), but a wide filter chain over a 50M table
without supporting indexes can be seconds.

### Mitigations

- 5-minute cache per (table, col, parent-filters-hash).
- Server-side typeahead — let the user type 3 chars before fetching.
- For most editor cases, capping pre-load to 10k rows is enough that
  even client-side dropdown rendering is fast.

---

## Add row workflow

```jsx
// DataEditorPage.jsx
const newRow = {}
const isComplete = primaryKeyCols.every(pk => newRow[pk] != null && newRow[pk] !== '')
if (isComplete) saveNewRow(newRow)
```

Backend:

```python
@router.post("/editor/{table}/rows")
def add_row(table, payload, db = Depends(get_data_db),
            current_user = Depends(get_current_user)):
    pk_values = payload.primary_key_values
    new_data  = payload.new_data
    # Insert; on PK conflict return 409
    INSERT INTO <table> (cols) VALUES (...)
    audit.log_insert(...)
```

## Delete workflow

```python
@router.delete("/editor/{table}/rows")
def delete_rows(table, payload, ...):
    for pk_values in payload.primary_key_values_list:
        old = SELECT * FROM <table> WHERE <pk_conditions>
        DELETE FROM <table> WHERE <pk_conditions>
        audit.log_delete(table, record_pk=..., old_data=old, ...)
```

`DirectUpdateEngine.delete_records` already does this in a loop. Each
deletion gets its own audit row with the full pre-delete snapshot.

---

## Common changes

### Recipe: per-table read-only mode

Some tables you want to display but not edit:

1. Add a `read_only` flag in `ARS_TABLE_METADATA` (or wherever you keep
   table metadata).
2. Frontend hides the edit cursor; backend rejects PUT/POST/DELETE
   for read-only tables with 403.

### Recipe: cell-level permissions

A planner can edit `QTY` but not `STORE_GRADE`. Maintain a table:

```sql
CREATE TABLE editor_column_permissions (
    role NVARCHAR(50),
    table_name NVARCHAR(200),
    column_name NVARCHAR(200),
    can_edit BIT
);
```

Backend rejects updates to columns the user's role can't edit. Frontend
greys them out on load.

### Recipe: undo / redo (per-session)

Maintain an in-memory undo stack on the frontend. Each "save" pushes a
delta `{table, pk, old_values, new_values}` onto the stack. Undo
applies the inverse delta.

---

## Performance reference

| Scenario | Performance |
|---|---|
| Initial table load (1k rows, 20 cols) | ~200 ms |
| Filter dropdown (cached) | <50 ms |
| Filter dropdown (uncached, 50M-row table) | 2–10 s |
| Save 1 cell | ~100 ms |
| Save 200 cells | ~20 s (per-row UPDATE) |
| Save 200 cells (bulk MERGE) | ~2 s |
| Delete 1 row | ~150 ms |
| Pagination next page (no filter change) | ~100 ms |

## Watch out for

- **Wide tables (>50 cols)**: ag-Grid renders all visible cells; >50 cols × 100 rows is fine, >50 cols × 10000 rows hurts. Add column virtualization (`columnVirtualization: true`).
- **NVARCHAR(MAX) cells**: Editing a 5MB cell freezes the browser. Add a "click to view full content" expander and edit in a textarea modal.
- **No transaction across rows**: Saving 100 cells means 100 transactions. If saves halfway through fail, the first 50 are committed and the last 50 aren't. Document this for the user.

---

## Audit trail

Every cell save → one `audit_log` row with:

```json
{
  "action_type": "UPDATE",
  "table_name": "<table>",
  "record_primary_key": "store=DH24|article=12345",
  "changed_columns": ["QTY","STK_TOTAL"],
  "old_data": {"QTY": 5,  "STK_TOTAL": 100},
  "new_data": {"QTY": 7,  "STK_TOTAL": 100},
  "changed_by": "<user>",
  "source": "UI",
  "created_at": "..."
}
```

This is the system of record. Don't bypass it.
