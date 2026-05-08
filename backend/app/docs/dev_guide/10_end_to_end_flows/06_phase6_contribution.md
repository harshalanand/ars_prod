# Phase 6 — Contribution % Execution

Sales contribution decides what share of category demand each article
should get. The output is the per-article percentage that drives
proportional allocation in Phase 7.

---

## Goal

For each (store, article) — or (article) at company level — compute
the share of category sales it represents, using the right preset (via
SSN mapping cascade). Write the result to `ARS_CONTRIB_RESULTS_<jobid>`.

## Inputs

- `ARS_MSA_GEN_ART` (article-level stock)
- `Trend_Sales_Daily` (or pre-aggregated `ARS_SALES_SUMMARY`)
- `ARS_CONTRIB_PRESETS` (calculation recipes)
- `ARS_CONTRIB_SSN_MAPPINGS` + `ARS_CONTRIB_ASSIGNMENTS` (SSN cascade)

## Output

- One result table per job: `ARS_CONTRIB_RESULTS_<jobid>` with
  per-(store, article) and/or per-(article) contribution columns.
- Job row in `ARS_CONTRIB_JOBS` with status, duration, row count.

---

## Master flowchart

```
                    POST /contrib/execute
                            │
                            ▼
              create ARS_CONTRIB_JOBS row
                            │
                            ▼
                  enqueue background job
                            │
                            ▼  (worker thread picks it up)
   ┌────────────────────────────────────────────────────┐
   │  contrib_service.run_job(spec):                    │
   │                                                     │
   │  1. Read presets, SSN mappings, assignments        │
   │                                                     │
   │  2. For each major_cat in spec.major_categories:   │
   │       a. Pull sales window per kpi_type from       │
   │          Trend_Sales_Daily (or ARS_SALES_SUMMARY)  │
   │       b. Pull stock from ARS_MSA_GEN_ART           │
   │       c. JOIN with vw_master_product to get SSN    │
   │       d. Compute contribution per article:         │
   │             share = sales[a] / total_in_cat        │
   │       e. Apply SSN mapping:                        │
   │             primary preset; fallback if no data    │
   │       f. Write result rows per assignment:         │
   │             - Store target → one per (store,art)   │
   │             - Company target → one per (art)       │
   │                                                     │
   │  3. Update job: completed, total_rows, duration    │
   └────────────────────────────────────────────────────┘
                            │
                            ▼
              ARS_CONTRIB_RESULTS_<jobid> ready
                            │
                            ▼
              Allocation Engine (Phase 7) reads it
```

---

## Step-by-step

### Step 1 — Configuration discovery

```python
presets = SELECT * FROM ARS_CONTRIB_PRESETS ORDER BY seq
ssn_mappings = SELECT * FROM ARS_CONTRIB_SSN_MAPPINGS
assignments = SELECT * FROM ARS_CONTRIB_ASSIGNMENTS
```

### Step 2 — Per-major-cat loop

For `major_cat = 'MENS_APP'`:

```python
# Pull sales for the KPI window of the default preset
window_start = today - timedelta(days=preset.avg_days)
sales_30d = SELECT WERKS, GEN_ART, SUM(SALES_AMT) AS sales
            FROM Trend_Sales_Daily
            WHERE REPORT_DATE BETWEEN :start AND :end
              AND MAJ_CAT = :cat
            GROUP BY WERKS, GEN_ART

stock = SELECT WERKS, GEN_ART, STK_TTL
        FROM ARS_MSA_GEN_ART
        WHERE MAJ_CAT = :cat

candidates = sales_30d.merge(stock).merge(master[['ARTICLE_NUMBER','SSN']])

# Total sales per (store, cat) for the contribution denominator
candidates['cat_total'] = candidates.groupby(['WERKS','MAJ_CAT'])['sales'].transform('sum')
candidates['contribution'] = candidates['sales'] / candidates['cat_total']
```

### Step 3 — SSN cascade

For each row, look up its SSN in mappings; pick primary preset; if
the article has no sales in the primary preset's window, try fallback.

```python
def apply_ssn(row):
    mapping = ssn_lookup.get(row['SSN'])
    if not mapping: return None  # no mapping = no contribution computed

    primary_share = compute_contribution(row, mapping.primary_preset)
    if primary_share is not None:
        return primary_share

    if mapping.fallback_preset_id:
        return compute_contribution(row, mapping.fallback_preset)

    return None

candidates['final_share'] = candidates.apply(apply_ssn, axis=1)
```

### Step 4 — Output writing

For each assignment in `ARS_CONTRIB_ASSIGNMENTS`:

```python
for asn in assignments:
    col_name = asn.output_column_template.replace('<MAJ_CAT>', major_cat)
    if asn.target in ('Store', 'Both'):
        # per-(store, article)
        row_per_store = candidates[['WERKS','GEN_ART']].copy()
        row_per_store[col_name] = candidates['final_share'] * 100   # as percent
        write_to_result_table(row_per_store)
    if asn.target in ('Company', 'Both'):
        # roll up to per-(article)
        company = candidates.groupby('GEN_ART')['final_share'].sum().reset_index()
        company[col_name] = company['final_share'] * 100
        write_to_result_table(company.drop(columns=['final_share']))
```

### Step 5 — Persist

```sql
TRUNCATE TABLE ARS_CONTRIB_RESULTS_<jobid>;
INSERT INTO ARS_CONTRIB_RESULTS_<jobid> (...) VALUES (...);
```

Update job row:
```sql
UPDATE ARS_CONTRIB_JOBS
SET status='completed', completed_at=GETDATE(),
    total_rows=:n, duration_ms=:d
WHERE job_id=:id;
```

---

## Examples

### Run for two MAJ_CATs

```bash
JOB=$(curl -s -X POST "$API/api/v1/contrib/execute" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "grouping_column":"MAJ_CAT",
    "major_categories":["MENS_APP","WOMENS_APP"],
    "preset_id":1,
    "target":"Both",
    "use_sequence":true,
    "save_to_db":true
  }' | jq -r .data.job_id)

# Poll
while true; do
  S=$(curl -s -H "Authorization: Bearer $TOKEN" "$API/api/v1/jobs/$JOB" | jq -r .data.status)
  echo $S; [ "$S" = "completed" ] && break
  sleep 10
done

# Inspect output
curl -s -H "Authorization: Bearer $TOKEN" \
  "$API/api/v1/contrib/result-tables" \
  | jq '.data[] | select(.table | contains("CTR_'$JOB'")) | {table, rows}'
```

### Verify a single article's contribution

```sql
SELECT WERKS, GEN_ART, STORE_CONTRIB_MENS_APP
FROM ARS_CONTRIB_RESULTS_CTR_abc123def0
WHERE GEN_ART = '12345'
ORDER BY STORE_CONTRIB_MENS_APP DESC;
```

---

## Performance — this is the heavy phase

| Step | Time (typical) |
|---|---|
| Read presets/mappings | <100 ms |
| Per-cat sales scan (no pre-agg) | 1-3 min |
| Per-cat sales scan (with `ARS_SALES_SUMMARY`) | 5-15 s |
| Stock JOIN | 30 s |
| SSN cascade | 1-3 min for full cascade |
| Output writing | 30-120 s per cat |
| **Total** | **15-60 min** depending on data |

### Speedup recipes

1. **Pre-aggregate** — refresh `ARS_SALES_SUMMARY` nightly at month
   grain. Worker runtime drops 60 min → 5 min.
2. **Window functions** — push trailing-window math to SQL with
   `LAG / OVER`. Avoids fetching all rows to Python.
3. **Per-cat parallelism** — run each MAJ_CAT on its own thread
   (4 cats parallel = ~4× speedup).
4. **Skip use_sequence for ad-hoc runs** — saves the cascade overhead.

---

## What can go wrong

### Failure A — Job stays "running" with no progress

Worker thread crashed mid-execution. Mark stale:

```sql
UPDATE ARS_CONTRIB_JOBS
SET status='failed', completed_at=GETDATE(),
    error_message='Worker died — auto-cleared'
WHERE status='running'
  AND started_at < DATEADD(minute, -120, GETDATE());
```

### Failure B — Result table missing columns

If a new assignment was added but never run, prior result tables
won't have the new column. Allocation engine that expects it must
guard with `COLUMNS` introspection or `COL_LENGTH('table','col')`.

### Failure C — SSN cycle in mappings

Result: stack overflow or infinite loop. Already addressed in
contribution mappings docs — add cycle detection on save.

---

## When this phase is healthy

- Job completes with `status='completed'`.
- `total_rows > 0` and reasonable for the configuration.
- Result table has a column for every assignment that targets the run.
- Allocation engine (next phase) finds the table and reads
  contribution numbers cleanly.
