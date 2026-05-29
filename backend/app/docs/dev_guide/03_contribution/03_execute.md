# Contribution % — Execute

The button that actually runs the contribution % calculation. Heaviest
job in the system — minutes to an hour, depending on data volume.

---

## What "execute" produces

`ARS_CONTRIB_RESULTS_<jobid>` table with columns the assignments
defined (e.g. `STORE_CONTRIB_MENS_APP`, `COMPANY_CONTRIB_MENS_APP`),
one row per (store, article) for store-target columns and one row per
(article) for company-target columns.

## Files

| Layer | File |
|---|---|
| Page | `frontend/src/pages/ContribExecutePage.jsx` |
| API | `app/api/v1/endpoints/contrib.py` (`/contrib/execute`) |
| Service | `app/services/contrib_service.py` |
| Permission | `CONTRIB_EXECUTE` |

## Run configuration

| Field | Effect |
|---|---|
| `grouping_column` | The category dimension (typically `MAJ_CAT`) |
| `major_categories` | Multi-select — which MAJ_CATs to include |
| `preset_id` | Default preset (used if SSN mappings don't override) |
| `target` | `Store` / `Company` / `Both` |
| `use_sequence` | Follow SSN fallback chain (adds ~30% time) |
| `save_to_db` | Persist `ARS_CONTRIB_RESULTS_<jobid>` |

## End-to-end flow

```
POST /contrib/execute  →  create ARS_CONTRIB_JOBS row
                       →  return { job_id: "CTR_..." }

Worker thread (single-job queue):
  ┌────────────────────────────────────────┐
  │ 1. Read SSN mappings + assignments     │
  │ 2. Read presets                         │
  │ 3. For each major category:             │
  │      a. Pull sales window per kpi_type  │
  │      b. Pull stock from MSA outputs     │
  │      c. JOIN with master + SSN          │
  │      d. Compute contribution per article│
  │      e. Apply SSN fallbacks (if seq)    │
  │      f. Write to result table per row   │
  │ 4. Optional: write company-level rollup │
  │ 5. Update job status: completed         │
  └────────────────────────────────────────┘
```

## Calculation logic

```python
# For each (major_cat, article):
sales_per_window = SUM(sales) over kpi window   # from Trend_*
total_sales_in_cat = SUM(sales) over all articles in cat
contribution[article] = sales_per_window[article] / total_sales_in_cat

# Output per assignment:
for assignment in assignments:
    col_name = resolve_template(assignment.output_column_template, major_cat)
    if assignment.target in ('Store', 'Both'):
        # Per-store breakdown
        write_per_store(col_name, contribution_per_store)
    if assignment.target in ('Company', 'Both'):
        # Aggregate
        write_company(col_name, contribution_total)
```

## Example: trigger from CLI

```bash
JOB=$(curl -s -X POST "$API/api/v1/contrib/execute" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "grouping_column": "MAJ_CAT",
    "major_categories": ["MENS_APP","WOMENS_APP"],
    "preset_id": 1,
    "target": "Both",
    "use_sequence": true,
    "save_to_db": true
  }' | jq -r .data.job_id)

echo "Job: $JOB"
# Poll
while true; do
  S=$(curl -s -H "Authorization: Bearer $TOKEN" "$API/api/v1/jobs/$JOB" | jq -r .data.status)
  echo "$S"; [ "$S" = "completed" ] || [ "$S" = "failed" ] && break
  sleep 5
done
```

## Why this is the heavy one

| Step | Cost |
|---|---|
| Sales window scan | 1-3 min on `Trend_Sales_Daily` (50M+ rows) |
| Stock JOIN | 30 s on MSA outputs |
| SSN cascade | per-article look-up, 2× rounds when fallback fires |
| Per-store + per-company outputs | doubles the writes |

For 100k SKUs × 300 stores × 5 majcats, expect 20-60 min.

## Performance fixes — actually impactful

### Fix 1: pre-aggregate sales

Build a nightly `ARS_SALES_SUMMARY` at SLOC × GEN_ART × month grain.
The execute job reads from there instead of raw `Trend_*`.

```sql
CREATE TABLE ARS_SALES_SUMMARY (
  REPORT_MONTH DATE,
  WERKS NVARCHAR(10),
  GEN_ART NVARCHAR(50),
  SALES_AMT FLOAT,
  QTY FLOAT,
  PRIMARY KEY (REPORT_MONTH, WERKS, GEN_ART)
);

-- Refresh nightly:
TRUNCATE TABLE ARS_SALES_SUMMARY;
INSERT INTO ARS_SALES_SUMMARY
SELECT DATEFROMPARTS(YEAR(REPORT_DATE), MONTH(REPORT_DATE), 1),
       WERKS, GEN_ART, SUM(SALES_AMT), SUM(QTY)
FROM Trend_Sales_Daily
WHERE REPORT_DATE >= DATEADD(year, -2, GETDATE())
GROUP BY DATEFROMPARTS(YEAR(REPORT_DATE), MONTH(REPORT_DATE), 1),
         WERKS, GEN_ART;
```

Job runtime drops from 60 min → 5 min.

### Fix 2: SQL-side window functions instead of pandas

`contrib_service` currently fetches raw rows and computes trailing
windows in pandas. Pushing to SQL via `LAG / OVER`:

```sql
SELECT WERKS, GEN_ART,
       SUM(SALES_AMT) OVER (
         PARTITION BY WERKS, GEN_ART
         ORDER BY REPORT_DATE
         ROWS BETWEEN 30 PRECEDING AND CURRENT ROW
       ) AS L30D_SALES
FROM Trend_Sales_Daily
WHERE REPORT_DATE >= DATEADD(day, -45, GETDATE());
```

Avoids transferring all rows to Python.

### Fix 3: parallelise per major category

Each MAJ_CAT is independent. Run them on separate workers:

```python
with ThreadPoolExecutor(max_workers=4) as pool:
    futures = [pool.submit(run_for_cat, cat) for cat in major_cats]
    for f in as_completed(futures):
        results.append(f.result())
```

4× speedup typically.

### Fix 4: skip use_sequence for ad-hoc runs

Sequence mode (SSN fallback chain) is for production-grade runs.
For "test this preset" quickly, leave it off.

## Common changes

### Recipe: add a new contribution metric

E.g. `STK_TURN_DAYS`. Steps:

1. `contrib_service.py` → output schema → add the column.
2. Compute in the per-article loop:
   ```python
   df['STK_TURN_DAYS'] = df['STK_TTL'] / df['PER_DAY_SALE'].replace(0, np.nan)
   ```
3. Frontend Review page auto-discovers columns — no change.

### Recipe: change which sales table is the source

`contrib_service._load_sales_data` → swap `Trend_Sales_Daily` for your
new source. Validate the join key (usually `WERKS`, `MATNR`).

### Recipe: pause/resume support

Today jobs run to completion or fail. To support pause:

1. Add `paused_at` column on `ARS_CONTRIB_JOBS`.
2. Worker checks `cancel_check + pause_check` between major_cats.
3. On pause: save progress (cursor / cat completed list) into the job
   row; release the worker.
4. On resume: same endpoint with `?from=<cat>` re-enters the loop.

Useful for long jobs that run during business hours and need to step
out of the way during a peak.

## Live log streaming

The Execute page polls `GET /jobs/<id>` every 2-5s for status.
For verbose log lines, add an SSE endpoint:

```python
@router.get("/contrib/jobs/{job_id}/stream")
async def stream_logs(job_id):
    async def event_generator():
        while True:
            new_logs = SELECT * FROM contrib_job_logs
                        WHERE job_id=:id AND ts > :last_ts
                        ORDER BY ts
            for log in new_logs:
                yield f"data: {json.dumps(log)}\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

Frontend:

```jsx
const es = new EventSource(`/api/v1/contrib/jobs/${jobId}/stream`)
es.onmessage = e => addLogLine(JSON.parse(e.data))
```

Beats polling for verbose, fast-changing logs.
