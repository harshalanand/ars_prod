---
title: Data Upload / Import — How Numbers Get Into ARS
category: Data Management
order: 15
source: backend/app/api/v1/endpoints/upload.py, backend/app/api/v1/endpoints/data_ops.py
last_reviewed: 2026-04-20
---

# Data Upload / Import

> **═══ USER GUIDE ═══**

## Recent upload activity (live)

<!-- @metric db="system" format="table" sql="SELECT TOP 10 resource_id AS [Table], MAX(created_at) AS last_upload, MAX(username) AS uploader FROM audit_log WHERE action='UPLOAD_DATA' GROUP BY resource_id ORDER BY MAX(created_at) DESC" label="Last 10 tables uploaded to" -->

## In plain English

Before ARS can calculate anything, it needs **data** — store stock, sales, master articles, pending allocations, calendar, contribution. This page is about **loading that data**.

Two ways to load:

1. **File upload** — you drop a CSV or Excel through the UI. Good for day-to-day operations.
2. **Bulk upsert API** — another system pushes rows to ARS. Good for automated integrations.

## When to use each

| Scenario | Use |
|---|---|
| "I have a weekly stock file from SAP" | File upload |
| "IT has a nightly job that syncs master product data" | Bulk upsert API |
| "I need to load 10 million rows in one go" | Pipeline endpoint (see §3) |
| "I want to correct 5 rows in a table" | Data Editor page (not this doc) |

## Before you start

| Check | How |
|---|---|
| File format is CSV or Excel (.xlsx / .xls) | Look at the file extension |
| Column headers match the target table | Open the file, compare headers to the table schema via Data Management → All Tables |
| No stray "Total" or "Grand Total" rows | Remove summary rows — they'll pollute the table |
| Numbers are numbers (not formatted strings) | In Excel, check cells aren't stored as text with leading zeros |
| You have `DATA_UPLOAD` permission | Ask a Super Admin if not |

## Step-by-step: file upload

### 1. Go to Upload Data
Sidebar → **Data Management** → **Upload Data**. URL `/upload`.

### 2. Pick the target table
Dropdown lists tables you can upload to (filtered by permission). Examples:

- `ET_STORE_STOCK` — your weekly stock snapshot
- `ET_STORE_SALES` — weekly sales
- `MASTER_GEN_ART_SALE` — seasonal sale reference
- `MASTER_ALC_PEND` — pending allocations

### 3. Drop the file
Drag-and-drop or click Browse. The file is transferred to the server but not yet inserted.

### 4. Pick the mode
- **Append** — add rows without touching existing.
- **Upsert** — update rows where key columns match, insert otherwise.
- **Replace** — truncate the table and insert fresh. **Dangerous** — needs `DATA_ADMIN` permission.

### 5. Click Import / Upload
The server reads the file, infers column types (if creating the table), and inserts rows in batches of 1000. You'll see a progress spinner.

### 6. Check the result
- Success toast with row count.
- An audit row is written (`action='UPLOAD_DATA'`) so you have a trail.
- The target table is updated immediately.

## Step-by-step: bulk upsert via API

For integrations pushing rows from another system:

```http
POST /api/v1/data-ops/upsert
Authorization: Bearer <token>
Content-Type: application/json

{
  "table":       "ARS_CHECKLIST",
  "rows":        [ {WERKS: "HN14", MAJ_CAT: "M_TEES_HS", STATUS: "OK"}, ... ],
  "key_columns": ["WERKS", "MAJ_CAT"],
  "mode":        "upsert"
}
```

- `mode` options: `insert` (strict), `upsert` (merge), `replace` (truncate + insert — admin only).
- Max rows per request: 50,000. Chunk larger payloads client-side.
- Response: `{success: true, inserted: N, updated: M, duration_sec: X}`.

## Step-by-step: very large imports (Pipeline endpoint)

If you have millions of rows (e.g., a full historical sales export):

1. Use `/api/v1/pipeline/upload` instead of `/upload/file`.
2. The server stages the file, then dispatches chunks to worker processes.
3. Workers write into a staging table in parallel.
4. When all chunks finish, the staging table is swapped into the target with a rename.

This path avoids timeouts and memory issues. Use it for anything > ~1 M rows.

## How the schema is chosen

**If the target table exists:** columns must match. Extra columns in your file → error. Missing columns → NULL.

**If the target table doesn't exist and you tick "Create":**
ARS infers types from the file:

| File column looks like | Created as |
|---|---|
| Strings | `NVARCHAR(MAX)` |
| Whole numbers | `BIGINT` |
| Decimals | `FLOAT` |
| Dates | `DATETIME` |

`NVARCHAR(MAX)` is safe but slow. If the table will get millions of rows, pre-create it with tighter types via the **Create Table** page and then upload.

## A worked example

You have `store_stock_2026W14.csv` with 200k rows targeting `ET_STORE_STOCK`.

1. Upload page → pick `ET_STORE_STOCK` → drop file.
2. Server streams file to disk (~50 MB).
3. pandas reads the file.
4. Column check: headers match `ET_STORE_STOCK` schema → skip create.
5. Pre-insert cleanup: SLOC codes trimmed, negative stock clipped to 0.
6. 200 batches of 1000 rows → INSERT via `to_sql`.
7. Audit row written: `action=UPLOAD_DATA, resource=ET_STORE_STOCK, rows=200000`.
8. Success: `{"success": true, "rows": 200000, "duration_sec": 18.4}`.

Post-upload, `ET_STORE_STOCK` has 200k new rows. MSA run will now reflect the new stock.

## Common questions (FAQ)

**Q: I uploaded 200k rows but only 180k made it in. Why?**
Some rows were probably rejected silently by a database constraint (duplicates, type mismatch). Check the audit-log `details` or the backend log for skipped-row counts.

**Q: My upload says "Column mismatch". I thought I matched.**
Column **names** must match, including case and underscores. "Werks" vs "WERKS" vs "WERK_S" are three different things. Standardise and retry.

**Q: I uploaded to the wrong table. Undo?**
There is no "undo" button. Two options:
1. If you used `Replace` mode, previous data is gone — restore from backup (talk to DBA).
2. If you used `Append`, query the audit log for `UPLOAD_DATETIME` and delete rows by that marker.

**Q: Can I schedule uploads?**
Not built-in. Use the bulk upsert API from a cron job / Task Scheduler on your own machine.

**Q: Is there a row-count limit?**
File upload: practical limit is around 500k rows (single-process pandas). Larger → use Pipeline endpoint.

**Q: What happens if two people upload to the same table at the same time?**
Both succeed if the table allows concurrent inserts. If mode is `Replace`, the later one wins (the earlier one's rows are truncated). Avoid simultaneous replaces.

## Troubleshooting — "I see X"

| You see | Meaning | Fix |
|---|---|---|
| "Upload failed: column X missing" | File header or SQL table renamed | Align headers, or edit the target table schema. |
| Upload hangs > 30s on small file | Concurrent DDL locking the table | Check Jobs Dashboard and Data Editor for open operations; retry. |
| "Duplicate key" errors | Mode is `insert` and rows already exist | Switch to `upsert` or `replace`. |
| All rows import as NULL for a column | Column header mismatch (trailing space, case) | Clean the header row in the file. |

## Verification

```sql
-- Latest upload per table (across all users)
SELECT resource_id AS table_name,
       MAX(created_at) AS last_upload,
       MAX(username)   AS last_uploader
FROM audit_log
WHERE action = 'UPLOAD_DATA'
GROUP BY resource_id
ORDER BY last_upload DESC;

-- Row count timeline for one table
SELECT created_at, username,
       JSON_VALUE(details, '$.rows')      AS rows_loaded,
       JSON_VALUE(details, '$.filename')  AS file
FROM audit_log
WHERE action='UPLOAD_DATA' AND resource_id='ET_STORE_STOCK'
ORDER BY id DESC;

-- Did today's upload land?
SELECT COUNT(*) FROM ET_STORE_STOCK
WHERE UPLOAD_DATETIME >= CAST(GETDATE() AS DATE);
-- (only works if the table carries UPLOAD_DATETIME; not all do)
```

## Settings you can change

| Setting | Effect |
|---|---|
| Upload mode | `Append`, `Upsert`, `Replace` |
| Batch size (server-side) | Controls memory footprint during insert — dev-only toggle |

---

> **═══ TECHNICAL REFERENCE ═══**

## Behind the scenes — for developers

- File upload: `POST /api/v1/upload/file` → `upload.py`.
- Bulk upsert: `POST /api/v1/data-ops/upsert` → `data_ops.py`.
- Large-batch: `POST /api/v1/pipeline/*` → `pipeline.py` (chunks into workers).
- Audit: `audit_middleware.py` + inline `audit_log` insert.
- Schema inference: pandas dtype → SQL type map.

## How to update this doc

Update when a new file format is accepted, when a new upload mode lands, when batch size limits change, or when the Pipeline endpoint changes. Bump `last_reviewed`.
