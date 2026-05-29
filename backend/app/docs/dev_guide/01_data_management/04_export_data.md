# Export Data

The mirror of Upload Data — pull data **out** of any table with column
selection, filters, and CSV/XLSX format.

---

## What it does

Builds a parameterised SELECT against the chosen table, runs it as a
background job, writes the results to a file on disk, and serves a
download link.

## Files

| Layer | File |
|---|---|
| Page | `frontend/src/pages/ExportPage.jsx` |
| Routes | `app/api/v1/endpoints/data_ops.py` (export-related) |
| Service | `app/services/export_service.py` (or split inside data_ops) |
| Permission | `DATA_EXPORT` |

## End-to-end flow

```
1. User selects table → frontend fetches /api/v1/tables/<name>/columns
2. User picks columns + filters + format
3. POST /api/v1/data-ops/export-jobs (or similar) returns {job_id}
4. Backend worker reads the job spec:
     SELECT <cols> FROM <table>
     WHERE <filter1> AND <filter2> AND ...
   Streams rows in chunks → writes to:
     {LOCAL_EXPORT_DIR}/<job_id>_<table>.csv
6. Job row updates: status=running → completed (with file path)
7. Frontend polls /api/v1/jobs/<job_id>; when completed, shows download
8. GET /api/v1/data-ops/export-jobs/<job_id>/download streams the file
9. (Optional) Auto-cleanup after 7 days
```

## Filter operators

| Operator | SQL |
|---|---|
| `equals` | `[col] = :v` |
| `notEqual` | `[col] <> :v` |
| `contains` | `[col] LIKE '%' + :v + '%'` |
| `startsWith` | `[col] LIKE :v + '%'` |
| `endsWith` | `[col] LIKE '%' + :v` |
| `greaterThan` | `[col] > :v` |
| `lessThan` | `[col] < :v` |
| `between` | `[col] BETWEEN :a AND :b` |
| `in` | `[col] IN (:v1, :v2, ...)` |
| `blank` | `[col] IS NULL OR [col] = ''` |
| `notBlank` | `[col] IS NOT NULL AND [col] <> ''` |

All operators use parameter binding — no SQL injection risk.

## Cascading filter dropdowns

When a user picks "WERKS = DH24", the values dropdown for the next
filter (e.g. SLOC) should only show SLOCs that exist in DH24. This is
implemented as:

```sql
SELECT DISTINCT [SLOC]
FROM [<table>]
WHERE [WERKS] = :v   -- prior filter applied
ORDER BY [SLOC]
```

### Performance gotcha

On a 50M-row table, a fresh DISTINCT scan per dropdown click is slow.
Cache for 5 minutes:

```python
@lru_cache(maxsize=512)
def _distinct_cached(table, column, parent_filters_hash, ttl_bucket):
    ...
```

`ttl_bucket = int(time.time() / 300)` rotates the cache every 5 min.

---

## Splitting big exports

```python
# Pseudo-code in the worker
chunk_idx = 0
file_idx = 0
ROWS_PER_FILE = 1_000_000   # XLSX limit
with engine.connect() as conn:
    cursor = conn.execution_options(stream_results=True).execute(text(sql))
    writer = open_writer(f"{prefix}_{file_idx:03d}.csv")
    rows_in_file = 0
    for row in cursor:
        writer.writerow(row)
        rows_in_file += 1
        if rows_in_file >= ROWS_PER_FILE:
            writer.close()
            file_idx += 1
            writer = open_writer(f"{prefix}_{file_idx:03d}.csv")
            rows_in_file = 0
```

Frontend offers a zip of all files for download.

## Format choice

| Format | Use when | Why |
|---|---|---|
| **CSV** | >100k rows | Streaming, fast, no row limit |
| **XLSX** | ≤100k rows + needs formatting | Excel-friendly, slow to write |

## Streaming SELECT

For exports >1M rows, never materialise the full result in Python
memory. Use `execution_options(stream_results=True)` so the cursor
fetches in chunks:

```python
cursor = conn.execution_options(stream_results=True).execute(text(sql))
for row in cursor:
    write_row(row)
```

Without this flag, SQLAlchemy fetches all rows into a list before
returning.

## Examples

### CLI: export pending allocations for a single store

```bash
TOKEN=...
JOB=$(curl -s -X POST "$API/api/v1/data-ops/export-jobs" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "table": "ARS_pend_alc",
    "columns": ["RDC","ST_CD","MATNR","QTY","MAJ_CAT"],
    "filters": [
      {"column":"ST_CD","op":"equals","value":"DH24"}
    ],
    "format": "csv"
  }' | jq -r .data.job_id)

# poll
while true; do
  STATUS=$(curl -s -H "Authorization: Bearer $TOKEN" \
    "$API/api/v1/jobs/$JOB" | jq -r .data.status)
  echo "$STATUS"
  [ "$STATUS" = "completed" ] && break
  [ "$STATUS" = "failed" ] && exit 1
  sleep 2
done

curl -OJ -H "Authorization: Bearer $TOKEN" \
  "$API/api/v1/data-ops/export-jobs/$JOB/download"
```

## Common change: add an "Auto-delete after 7 days"

`upload_job_service` already auto-deletes uploaded files after
processing. Mirror it for exports:

1. Add a daily cron (or schedule task) that walks `LOCAL_EXPORT_DIR`,
   deletes files older than 7 days, and updates the job rows
   (`file_path = NULL`, `error_message = 'expired'`).
2. The download endpoint should return 410 Gone when the file is
   expired, so the frontend shows a friendly message.

## Common change: prevent "select *" by default

It's tempting to UI a "no columns selected = all columns" shortcut.
Don't. It encourages users to export wider-than-needed data, slowing
exports and ballooning storage. Force at least one explicit column
selection.
