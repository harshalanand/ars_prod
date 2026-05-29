# Contribution % — Developer Reference

The Contribution % section is **the heart of the allocation rules**.
It decides what share of total category sales each article should get.
That percentage flows into every downstream allocation decision.

Four sub-pages: **Presets, Mappings, Execute, Review**.

## How the calc works in plain English

```
Sales history (Trend_*) + Stock (ET_STORE_STOCK)
                │
                ▼
       Group by major category × store
                │
                ▼
   For each preset (e.g. L30D, L18M):
       contribution[article] = sales[article] / sales[major_cat]
                │
                ▼
       Apply SSN mapping (which preset to use per scenario)
                │
                ▼
       Output: ARS_CONTRIB_RESULTS_<jobid>
                │
                ▼
       Allocation engine reads this table
```

## Files that matter

| Concern | File |
|---|---|
| Routes | `app/api/v1/endpoints/contrib.py` |
| Calc engine | `app/services/contrib_service.py` |
| Frontend pages | `frontend/src/pages/Contrib*.jsx` (Presets, Mappings, Execute, Review) |
| API helpers | `frontend/src/services/api.js` → `contribAPI` |

---

## 8.1 Presets — `ContribPresetsPage.jsx`

### What a preset is

A **named recipe** for a contribution calculation:

| Field | Example | What it controls |
|---|---|---|
| `name` | `L30D` | Display name |
| `months` | 1 | How many months of sales to include |
| `avg_days` | 30 | Days used for "average sale per day" |
| `kpi_type` | `L30D` / `L18M` / `L7D` | Trailing-window flavour |
| `description` | "Last 30 days, fast-moving SKUs" | Documentation |
| `seq` | 1 | Order in fallback chain |

### Example: create a preset that uses last 7 days

```bash
curl -X POST "$API/api/v1/contrib/presets" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "L7D_HOT",
    "months": 0,
    "avg_days": 7,
    "kpi_type": "L7D",
    "description": "Last 7 days only — for hot launches"
  }'
```

### Storage

Table: `ARS_CONTRIB_PRESETS` (data DB). One row per preset. Reorder by
updating `seq` on each row.

### Common change: add a new KPI flavour

1. Pick a label (e.g. `L90D`).
2. `contrib_service.py` → `_window_for_kpi` (or equivalent) — add a
   branch `if kpi_type == "L90D": days = 90`.
3. Test by creating a preset with that kpi_type.

---

## 8.2 Mappings — `ContribMappingsPage.jsx`

Two tabs: **SSN Mappings** and **Assignments**.

### SSN Mappings

Maps each SSN value (Sub Segment Number — e.g. `BASIC`, `FASHION`,
`SEASONAL`) to a preset, with a fallback if the primary preset has
no data for that SSN.

```
SSN = "FASHION" → primary: L30D, fallback: L18M
SSN = "BASIC"   → primary: L18M, fallback: L7D
```

Stored in `ARS_CONTRIB_SSN_MAPPINGS`.

### Assignments

Maps an **output column name** to an SSN mapping with a prefix template
and a target scope.

```
Output: STORE_CONTRIB_<MAJ_CAT>     ← uses SSN mapping "FASHION"  → Store
Output: COMPANY_CONTRIB_<MAJ_CAT>   ← uses SSN mapping "FASHION"  → Company
```

Stored in `ARS_CONTRIB_ASSIGNMENTS`. The `target` field is `Store`,
`Company`, or `Both`.

### Validation rules

The frontend doesn't currently validate these — but you should:

1. **No cycles** in fallback chain (`A → B → A` is bad).
2. **No duplicate prefix templates** with different SSNs.

### Common change: wire validation into the save endpoint

`contrib.py` → `POST /contrib/mappings/ssn` → before insert/update,
walk the fallback graph using DFS and reject cycles.

```python
def _has_cycle(mappings, start_ssn, seen=None):
    seen = seen or set()
    if start_ssn in seen:
        return True
    seen.add(start_ssn)
    fb = mappings.get(start_ssn, {}).get("fallback")
    return _has_cycle(mappings, fb, seen) if fb else False
```

---

## 8.3 Execute — `ContribExecutePage.jsx`

The button that actually runs Contribution %. Configure, click,
monitor.

### Configuration

| Field | Effect |
|---|---|
| `grouping_column` | What to group by (usually `MAJ_CAT`) |
| `major_categories` | Multi-select of MAJ_CATs to include |
| `preset_id` | Which preset to use (or fallback chain) |
| `target` | Store / Company / Both |
| `use_sequence` | If true, follow the SSN fallback chain. Adds ~30% overhead. |
| `save_to_db` | If true, persist `ARS_CONTRIB_RESULTS_<jobid>` |

### Execution path

```
POST /contrib/execute  →  create ARS_CONTRIB_JOBS row  →  spawn worker thread
                          ↓
                  worker reads sales, stock, presets
                          ↓
                  computes contribution per article
                          ↓
                  writes ARS_CONTRIB_RESULTS_<jobid>
                          ↓
                  updates job status, duration, row count
```

### Example: run from CLI

```bash
curl -X POST "$API/api/v1/contrib/execute" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "grouping_column": "MAJ_CAT",
    "major_categories": ["MENS_APP","WOMENS_APP"],
    "preset_id": 1,
    "target": "Both",
    "use_sequence": true,
    "save_to_db": true
  }'
```

Returns `{ job_id: "CTR_..." }`. Poll `GET /jobs/{job_id}` for status.

### Performance — this is the heavy one

A run over 100k SKUs × 300 stores can take 20–60 minutes. Why:

1. Sales scan: `Trend_*` is wide and grows daily.
2. Stock join: `ET_STORE_STOCK` × `vw_master_product`.
3. SSN cascade: each preset miss triggers a fallback look-up → second pass.
4. Per-store + per-company outputs doubles the writes.

### Mitigations

- **Pre-aggregate**: build a nightly `ARS_SALES_SUMMARY` at SLOC × GEN_ART × month grain. The execute job reads from there instead of raw `Trend_*`.
- **Window functions on the DB side**: replace pandas trailing-window math with `LAG / OVER` SQL — pushes work to the DB and avoids fetching every row.
- **Pause/resume**: not currently implemented but worth adding for very long jobs.
- **Limit per-job concurrency**: only one execute should run at a time
  to avoid thrashing the source tables.

---

## 8.4 Review — `ContribReviewPage.jsx`

Browse `ARS_CONTRIB_RESULTS_*` tables, filter, preview, download, delete.

### Why deletes fail sometimes

If another module (e.g. allocation engine) holds a foreign key into the
result table, `DROP TABLE` fails. Fix: cleanup-cascade — drop dependent
rows first, then the table.

### Example: clean up old results

```sql
-- Soft-delete result tables older than 30 days
DECLARE @sql NVARCHAR(MAX) = '';
SELECT @sql = @sql + 'DROP TABLE [' + name + ']; '
FROM sys.tables
WHERE name LIKE 'ARS_CONTRIB_RESULTS_%'
  AND create_date < DATEADD(day, -30, GETDATE());
EXEC sp_executesql @sql;
```

### Frontend optimisation

The Preview endpoint streams up to 1000 rows. For full data, route
the user to Export Data (which goes through the proper background-job
infra).

---

## Common changes — recipes

### Add a new contribution metric

1. `contrib_service.py` → extend the result schema (add a column like
   `STK_TURN_DAYS`).
2. Add the calculation in the service's main loop.
3. The Review page auto-discovers columns — no frontend change needed.

### Change which sales table is the source

1. `contrib_service.py` → look for the `_load_sales_data` function.
2. Change the `FROM Trend_*` clause to your new source.
3. Test that the join key still matches the master product view.

### Add a new SSN target (e.g. "Region")

1. `ARS_CONTRIB_ASSIGNMENTS` table → ensure `target` column allows the new
   value (it's a free-form NVARCHAR).
2. `contrib_service.py` → handle the new target in the output-writing
   loop. Currently `Store` writes one row per (store, article), `Company`
   writes one per (article). `Region` would group by a region master.
