# Phase 1 — Data Ingestion (SAP → ARS)

The day starts here. Without this phase succeeding, every later phase
operates on yesterday's data — or worse, partial today's data mixed
with yesterday's leftovers.

---

## Goal

Refresh every fact and master table that ARS reads, with today's data
from SAP, before planners log in.

## Inputs

- SAP system producing nightly extracts.
- RFC pipeline workers (Cloudflare) connected to SAP.
- ARS data DB (`Rep_Data`) ready to receive bulk loads.

## Outputs (what later phases consume)

| Table | Type | Updated by | Used by |
|---|---|---|---|
| `ET_STORE_STOCK` | Fact | RFC ingest | Phase 4 (grids), Phase 7 (allocation) |
| `ET_MSA_STK` | Fact | RFC ingest | Phase 3 (MSA) |
| `MASTER_ALC_PEND` | Fact | RFC ingest | Phase 3 (MSA), Phase 7 |
| `vw_master_product` | Master view | SAP-managed | Almost every phase |
| `Trend_Sales_Daily` | Fact | RFC ingest | Phase 6 (contribution) |
| `Trend_Stock_Daily` | Fact | RFC ingest | Phase 6 |
| `ARS_STORE_SLOC_SETTINGS` | Config | Manual / sync | Phase 3, Phase 4 |
| `ARS_WAREHOUSE_STOCK` | Fact | RFC ingest | Phase 7 |

---

## Flowchart

```
                ┌─────────────────────────────────────┐
                │            SAP S/4HANA              │
                │  (source of truth — owned by SAP)   │
                └──────────────┬──────────────────────┘
                               │
                01:30 cron     │ RFC call: read materials,
                triggers       │ stock per store, sales,
                in CF Worker   │ pending allocations
                               │
                               ▼
                ┌─────────────────────────────────────┐
                │     Cloudflare Worker:              │
                │     v2-rfc-pipeline                 │
                │   (RFC client, paginates, retries)  │
                └──────────────┬──────────────────────┘
                               │
                               │ pushes parsed JSON
                               │ batches (5k rows each)
                               ▼
                ┌─────────────────────────────────────┐
                │    Cloudflare Worker:               │
                │    v2-sync-engine                   │
                │  (writes to Rep_Data via REST API)  │
                └──────────────┬──────────────────────┘
                               │
                               │ POST /api/v1/upload/async
                               │ payload = staged JSON file
                               ▼
                ┌─────────────────────────────────────┐
                │  ARS backend                         │
                │  upload_job_service                  │
                │  → upsert_engine._bulk_upsert        │
                │  → fast staging → UPDATE → INSERT    │
                └──────────────┬──────────────────────┘
                               │
                               │ commits per chunk
                               ▼
                ┌─────────────────────────────────────┐
                │  Rep_Data tables refreshed          │
                │  ET_STORE_STOCK, ET_MSA_STK, etc.   │
                └─────────────────────────────────────┘

        Status visible in Jobs Dashboard with progress + duration.
```

## End time SLA

- Last fact table commit must finish by **05:30** so Phase 2 can run at 06:30.
- If pipeline finishes after 06:30, Phase 2 still runs but uses yesterday's data; ops gets a Slack alert.

---

## Step-by-step breakdown

### Step 1 — RFC pull from SAP (~30-60 min)

#### What happens

The Cloudflare worker `v2-rfc-pipeline` (deployed at `…/v2-rfc-pipeline.workers.dev`) opens an RFC connection to SAP using the `arsadmin` SAP user. It calls a series of SAP function modules (e.g. `BAPI_MATERIAL_GET_LIST`, `BAPI_PLANT_STOCK_GET_LIST`) in batches of 5,000 records.

#### Where in code

- Cloudflare worker source: `cloudflare/v2-rfc-pipeline/src/index.ts` (in the v2-rfc repo, not this repo).
- Cron schedule: defined in `wrangler.toml` of that worker.

#### Example: trigger an ad-hoc pull

```bash
# Trigger via the universal MCP (or directly via Cloudflare worker URL)
curl -X POST "https://v2-rfc-pipeline.akash-bab.workers.dev/run" \
  -H "X-RFC-Key: v2-rfc-proxy-2026" \
  -H "Content-Type: application/json" \
  -d '{ "table": "ET_STORE_STOCK", "date": "2026-04-30" }'
```

#### Logic explained

Why batch in 5k? SAP RFC has a default 60-second response timeout. Pulling
all 50 million stock rows in one call would time out. 5k rows × ~100ms
per call = ~50 minutes for a typical day.

Why a worker instead of direct connection? Cloudflare's `v2-rfc-pipeline`
keeps a pooled SAP connection and rate-limits. If we hit SAP from many
ARS pods simultaneously, SAP gets unhappy.

### Step 2 — JSON staging file produced (~5 min)

#### What happens

The worker batches results into NDJSON (newline-delimited JSON) files
written to **R2 storage**. Each file is one logical table for one date.

```
r2://ars-rfc-staging/2026-04-30/ET_STORE_STOCK.ndjson    (≈ 50M rows × ~300 bytes = 15 GB)
r2://ars-rfc-staging/2026-04-30/ET_MSA_STK.ndjson         (≈ 50M rows × ~250 bytes = 12 GB)
r2://ars-rfc-staging/2026-04-30/MASTER_ALC_PEND.ndjson    (≈ 1M rows × ~400 bytes = 400 MB)
r2://ars-rfc-staging/2026-04-30/Trend_Sales_Daily.ndjson  (≈ 5M rows × ~200 bytes = 1 GB)
```

#### Why NDJSON

- **Streamable**: producer can write one line as it arrives; consumer can read one line at a time. No "load 15 GB into RAM" problem.
- **Resumable**: if the writer crashes, the file has clean line boundaries. The consumer knows the last good record and resumes.
- **Schema-less header**: each line is self-describing; new columns don't break the parser.

### Step 3 — Sync engine triggers ARS upload (~30-60 min)

#### What happens

`v2-sync-engine` worker reads the NDJSON file, splits it into chunks of
~100k rows, and calls the ARS `/api/v1/upload/async` endpoint with each
chunk. The ARS backend creates an `UploadJob`, queues it, and processes
it through `upsert_engine`.

#### Where in code

- Worker: `cloudflare/v2-sync-engine/src/index.ts`
- ARS endpoint: `backend/app/api/v1/endpoints/upload.py:upload_file_async`
- Engine: `backend/app/services/upsert_engine.py:_bulk_upsert`

#### Example: what the sync engine sends

```http
POST /api/v1/upload/async HTTP/1.1
Authorization: Bearer <service-token>
Content-Type: multipart/form-data

--boundary
Content-Disposition: form-data; name="file"; filename="ET_STORE_STOCK_chunk0042.csv"
Content-Type: text/csv

WERKS,MATNR,SLOC,PARTICULARS,PARTICULARS_VALUE,DATE
DH24,12345,V01,STK,150.0,2026-04-30
DH24,12345,V02_FRESH,STK,80.0,2026-04-30
...100k more rows...
--boundary
Content-Disposition: form-data; name="table_name"
ET_STORE_STOCK
--boundary
Content-Disposition: form-data; name="primary_key_columns"
WERKS,MATNR,SLOC,DATE
--boundary
Content-Disposition: form-data; name="mode"
upsert
```

ARS responds:

```json
{
  "success": true,
  "data": { "job_id": "UPL_abc123def0", "status": "queued" }
}
```

#### Logic explained

Why upsert instead of replace? Some SAP records arrive late (delayed
syncs from individual stores). Upsert lets us update what changed and
insert what's new without losing rows that were correct yesterday.

Why 100k chunks? Trade-off between two costs:
- **Smaller chunks** = more HTTP round-trips = more overhead.
- **Larger chunks** = bigger TDS payload to Azure SQL = more transaction-log pressure.

100k is the sweet spot for our Azure SQL tier. If we change tiers
(more log capacity), we can bump to 250k.

### Step 4 — ARS upsert engine processes the chunk (~1-2 min per chunk)

This is detailed in `01_data_management/03_upload_data.md` but the
short version:

1. Worker thread picks up the job from queue.
2. Reads the CSV with pandas (`dtype=str` to preserve leading zeros).
3. Cleans cells (blank → `__SKIP__`, `'-'` → `__NULL__`).
4. Pre-validates types (`TRY_CAST`-style), collects `validation_errors`.
5. **Fast bulk path**:
   - `CREATE TABLE #bulk_stage_<batch_id> ([col] NVARCHAR(4000) NULL, ...)`
   - Bulk-insert rows in batches of 20k via `cursor.fast_executemany = True`.
   - One big `UPDATE` (with `ROWLOCK`) for existing rows.
   - One big `INSERT` for new rows.
   - `DROP TABLE #stage`.
   - Audit log summary row.
6. Job row updated with status, durations, counts.

### Step 5 — Verification ping

The sync engine, after the last chunk completes, queries:

```sql
SELECT MAX(CAST([DATE] AS DATE)) AS max_date,
       COUNT(*) AS row_count
FROM ET_STORE_STOCK;
```

If `max_date` matches today and `row_count` is within ±5% of expected,
the worker emits a "OK" event to its log and exits. Otherwise it
fires an alert (Slack webhook in `nubo-marketing-dashboard` setup).

---

## Examples — what data actually looks like

### `ET_STORE_STOCK` typical row

```sql
SELECT TOP 3 WERKS, MATNR, SLOC, PARTICULARS, PARTICULARS_VALUE, [DATE]
FROM ET_STORE_STOCK
WHERE [DATE] = '2026-04-30';

-- WERKS  MATNR     SLOC      PARTICULARS  PARTICULARS_VALUE  DATE
-- DH24   12345678  V01       STK          150                2026-04-30
-- DH24   12345678  V02_FRESH STK          80                 2026-04-30
-- DH24   12345678  PEND_ALC  PEND         5                  2026-04-30
```

Each (WERKS, MATNR, SLOC, DATE, PARTICULARS) tuple is unique. SLOC =
storage location; PARTICULARS describes what the value is (STK, PEND,
SALE_QTY, etc.).

### `MASTER_ALC_PEND` typical row

```sql
SELECT TOP 3 RDC, MATNR, ST_CD, QTY, DATE
FROM MASTER_ALC_PEND;

-- RDC    MATNR     ST_CD  QTY  DATE
-- DH24   12345678  S001   3    2026-04-29
-- DH24   12345678  S002   2    2026-04-29
-- DH26   55555555  S041   10   2026-04-30
```

This is "what's already been promised" to a store but not yet shipped.
Phase 3 subtracts these from available stock.

---

## What can go wrong (and how to detect)

### Failure A — RFC pipeline timed out on SAP

**Symptom:** sync_engine logs show 0 chunks processed for some tables.
NDJSON files in R2 are smaller than expected.

**Detect:**
```bash
# List staged files
curl -H "Authorization: Bearer $TOKEN" \
  "$API/api/v1/maintenance/r2-staging/list?date=2026-04-30"
```

Compare with last week's sizes. If today's file is < 50% of normal,
SAP probably didn't return all rows.

**Fix:** Re-trigger the RFC pipeline for the affected table. See worker
logs at Cloudflare dashboard → `v2-rfc-pipeline` → Logs.

### Failure B — Upload jobs stuck in "queued"

**Symptom:** Jobs Dashboard shows jobs older than 30 min still queued.

**Detect:**
```sql
SELECT job_id, table_name, file_size, status,
       DATEDIFF(MINUTE, created_at, GETDATE()) AS age_min
FROM upload_jobs
WHERE status = 'queued'
ORDER BY created_at;
```

**Fix:** The worker thread crashed. Restart the backend; on startup it
should mark stale running jobs as failed (if recovery is wired) and
re-enable the worker for the queued ones.

### Failure C — Upload jobs failing with "transaction log full"

**Symptom:** error 9002 in error_message.

**Detect:**
```sql
SELECT log_reuse_wait_desc, used_log_space_in_percent
FROM sys.dm_db_log_space_usage;
```

If `log_reuse_wait_desc = 'ACTIVE_TRANSACTION'` and used_log_space is
>95%, there's a stuck transaction.

**Fix:** See `09_architecture/03_performance_playbook.md` → "9002 — log
full" recovery SQL. Kill the stuck SPID and let the platform clear the
log.

### Failure D — Data is fresh but seems wrong

**Symptom:** Phase 3 (MSA) produces unexpected output. Investigation
shows source data is internally inconsistent (e.g. SLOC has stock but
no master article record).

**Detect:**
```sql
-- Articles in stock but missing from master
SELECT TOP 20 STK.MATNR, STK.WERKS, STK.SLOC
FROM ET_STORE_STOCK STK
LEFT JOIN vw_master_product MP ON STK.MATNR = MP.ARTICLE_NUMBER
WHERE MP.ARTICLE_NUMBER IS NULL
  AND STK.[DATE] = '2026-04-30';
```

**Fix:** SAP master sync ran late or failed. Check the
`vw_master_product` last refresh:
```sql
SELECT MAX(LAST_MODIFIED) FROM vw_master_product;
```
If older than today, re-run the master sync.

---

## Performance benchmarks

| Operation | Typical time | Volume |
|---|---|---|
| RFC pull all stock | 30-60 min | ~50M rows |
| RFC pull pending allocations | 5 min | ~1M rows |
| RFC pull master | 10 min | ~500k rows |
| Sync to Rep_Data per chunk | 30-60s | 100k rows |
| Total Phase 1 wall time | 90-180 min | All tables |

If Phase 1 takes >3 hours, something is wrong:

1. Cloudflare worker hitting SAP RFC limit (check worker logs).
2. ARS backend overloaded (Jobs Dashboard shows queue > 20).
3. Azure SQL DTU pinned (check Azure portal).

---

## When this phase is healthy

You'll see in the Jobs Dashboard:
- 5-10 `UPL_*` jobs from the sync engine, all `completed`
- `started_at` between 03:00 and 05:00
- `completed_at` before 05:30
- Total `inserted_rows + updated_rows` matches expected daily volume
- 0 `error_rows` per job

When all five are true, Phase 1 succeeded. Move on to Phase 2.
