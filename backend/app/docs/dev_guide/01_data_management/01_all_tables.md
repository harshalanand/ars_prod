# All Tables

The starting point for any data exploration. Lists every table the
current user can read, with row counts, last-modified date, and
shortcuts to view, edit, or export the data.

---

## What it does, in one sentence

Renders a catalog of every base table on the data DB, filtered by what
the user has permission to see, with at-a-glance stats so you don't
have to hunt for the right table by guessing names.

## Where it lives

| Layer | File | Role |
|---|---|---|
| Page | `frontend/src/pages/AllTablesPage.jsx` | The grid view |
| API | `backend/app/api/v1/endpoints/tables.py` | `GET /tables`, `GET /tables/{name}` |
| Service | `backend/app/services/table_mgmt_service.py` | Schema introspection |
| Permission | `DATA_VIEW` | Required to see anything |

## How a single page-load works (step-by-step)

```
┌── Browser ──┐
│  AllTables  │
│   Page.jsx  │
└──────┬──────┘
       │ 1. on mount: GET /api/v1/tables
       ▼
┌──────────────────────────────────────────────┐
│  endpoints/tables.py: list_tables()          │
│   - check user has DATA_VIEW                 │
│   - call table_mgmt_service.list_all()       │
└──────┬───────────────────────────────────────┘
       │ 2. service builds query
       ▼
┌──────────────────────────────────────────────┐
│  SELECT t.name, ISNULL(SUM(p.rows),0) AS cnt,│
│         t.create_date, t.modify_date         │
│  FROM sys.tables t                           │
│  LEFT JOIN sys.partitions p                   │
│    ON p.object_id = t.object_id              │
│   AND p.index_id IN (0,1)                    │
│  GROUP BY t.name, t.create_date, t.modify_date│
└──────┬───────────────────────────────────────┘
       │ 3. respond {data: [{table, rows, ...}]}
       ▼
┌── Browser ──┐
│  ag-Grid    │
│  renders    │
└─────────────┘
```

### Why we use `sys.partitions`, not `COUNT(*)`

`SELECT COUNT(*)` on a 50M-row table is a **full scan** — seconds per
table. We have 185+ tables. Multiply that out and the page would take
minutes to load.

`sys.partitions.rows` is the row count maintained by SQL Server in
metadata. It's **approximate** (lags by a few seconds) but **instant**.
For a "give me a feel for table sizes" view, instant beats accurate.

### How permission filtering works

The default behaviour is "show everything DATA_VIEW grants." If you
need finer control (per-table ACLs), the table list query needs an
extra JOIN against a permissions table. Today we don't have one — every
DATA_VIEW user sees every table.

---

## Logic deep-dive

### Why `index_id IN (0, 1)`

A SQL Server table is either a heap (no clustered index, `index_id=0`)
or a clustered table (`index_id=1`). Each table has exactly one of
these. Higher index IDs (2, 3, ...) are non-clustered indexes — those
have their own rows-per-index counts but are NOT actual rows. Including
them double-counts.

### Why we sum partitions instead of taking the first row

Big tables can be partitioned across multiple physical partitions (e.g.
by date). Each partition is one row in `sys.partitions`. We `SUM` to
get the total.

### What gets cached

Currently nothing — every page load queries the catalog. Caching is
fine here because the catalog rarely changes; in production add a
60-second in-memory cache:

```python
from functools import lru_cache
import time

_table_list_cache = (0, [])

def list_all(force_refresh=False):
    global _table_list_cache
    if not force_refresh and time.time() - _table_list_cache[0] < 60:
        return _table_list_cache[1]
    rows = _query_catalog()
    _table_list_cache = (time.time(), rows)
    return rows
```

---

## Common change: hide system tables from the list

`backend/app/services/table_mgmt_service.py` → in `list_all`, change
the WHERE clause to exclude system patterns:

```sql
WHERE t.name NOT LIKE 'sys%'
  AND t.name NOT LIKE 'spt_%'
  AND t.name NOT LIKE 'MSrep%'
  AND t.is_ms_shipped = 0
```

`is_ms_shipped` is a built-in flag for SQL Server's own tables. It's
the safest filter.

## Common change: only show tables the user has uploaded recently

Join with `audit_log` to get last-upload-by-user:

```sql
SELECT t.name, ISNULL(SUM(p.rows),0) AS rows,
       MAX(a.created_at) AS last_uploaded_by_me
FROM sys.tables t
LEFT JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0,1)
LEFT JOIN audit_log a
       ON a.table_name = t.name
      AND a.changed_by = :username
      AND a.action_type = 'BULK_UPLOAD'
GROUP BY t.name
```

## Common change: add a "schema" column to the grid

1. Add `t.schema_id` to the SELECT, JOIN to `sys.schemas`.
2. Backend returns `schema_name` in each row.
3. Frontend `AllTablesPage.jsx` → `columnDefs` → add a column for it.

---

## Performance reference

| Action | Time on 200-table DB | Why |
|---|---|---|
| Initial page load | ~200 ms | One catalog query |
| Refresh button | ~200 ms | No cache today; should be <10ms with 60s cache |
| Filtering in-grid | client-side, instant | ag-Grid's quickFilter |
| Sorting | client-side, instant | ag-Grid sort |
| Open table in editor | nav to /editor/<name> | Lazy-loaded route |

If the catalog query ever feels slow:

1. Check if it's actually slow with `SET STATISTICS TIME ON`.
2. Check if `sys.partitions` is bloated by orphaned indexes — defrag/rebuild fixes it.
3. Add the 60s cache.

---

## Example: list tables from CLI

```bash
TOKEN=$(curl -s -X POST "$API/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"superadmin","password":"Admin@12345"}' | jq -r .data.access_token)

curl -H "Authorization: Bearer $TOKEN" "$API/api/v1/tables" \
  | jq '.data[] | select(.rows > 1000000) | {table, rows}'
```

Lists every table with more than 1M rows — handy for spotting which
tables to focus on for indexing/archival.

---

## Adding a new "stat" column to the catalog (advanced)

Suppose you want to show the **total disk space used per table**.

### Step 1 — query

```sql
SELECT t.name,
       ISNULL(SUM(p.rows),0) AS rows,
       SUM(au.total_pages) * 8 AS total_kb
FROM sys.tables t
LEFT JOIN sys.partitions p
       ON p.object_id = t.object_id AND p.index_id IN (0,1)
LEFT JOIN sys.allocation_units au
       ON au.container_id = p.partition_id
GROUP BY t.name
```

### Step 2 — backend service

`table_mgmt_service.py` → `list_all` → return the new column.

### Step 3 — backend route

`tables.py` → confirm the new field is in the response schema (or just
loose JSON if you're not strictly typing).

### Step 4 — frontend

`AllTablesPage.jsx` → `columnDefs` → push:

```jsx
{
  field: 'total_kb',
  headerName: 'Size',
  valueFormatter: ({ value }) =>
    value > 1_000_000 ? `${(value / 1_000_000).toFixed(1)} GB`
    : value > 1_000   ? `${(value / 1_000).toFixed(1)} MB`
    : `${value} KB`,
  cellStyle: { textAlign: 'right' },
}
```

That's all. Refresh the page; the new column appears.
