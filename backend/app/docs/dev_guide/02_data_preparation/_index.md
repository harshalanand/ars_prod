# Data Preparation — Developer Reference

The Data Preparation section produces the calculated tables that
downstream allocation logic depends on. Five pages: **MSA Stock
Calculation, BDC Creation, Grid Builder, Lookup Art Master, Listing**.

If MSA / Grid Builder / Listing are stale, every allocation is wrong.
This is the layer to understand deeply.

## Dependency graph

```
SAP RFC pipeline ──► ET_STORE_STOCK
                  ──► ET_MSA_STK
                          │
                          ▼
              VW_ET_MSA_STK_WITH_MASTER ◄── vw_master_product
                          │
        ┌─────────────────┴────────────────┐
        ▼                                   ▼
   MSA Calculation                    Grid Builder
   (ARS_MSA_TOTAL,                   (ARS_GRID_*  one
    ARS_MSA_GEN_ART,                  output table per grid)
    ARS_MSA_VAR_ART)                       │
        │                                   ▼
        └────────────► Listing ─────► PER_OPT_SALE
                          │
                          ▼
                      Allocations
```

Run them roughly in this order. Skip a step and the next one operates
on stale data.

---

## 7.1 MSA Stock Calculation — `frontend/src/pages/MSAStockCalculationPage.jsx`

### What it does

Turns raw stock + pending allocations into three "ready" tables:

| Output table | Grain | Used by |
|---|---|---|
| `ARS_MSA_TOTAL` | Store × MAJ_CAT | High-level allocation rules |
| `ARS_MSA_GEN_ART` | Store × MAJ_CAT × GEN_ART | Article-level decisions |
| `ARS_MSA_VAR_ART` | Store × MAJ_CAT × GEN_ART × Variant | Final pick lists |

(`ST_CD` was renamed to `RDC` across all three in April 2026.)

### The 9-step algorithm — `app/services/msa_service.py`

```python
1. filter SLOC                    # Keep only configured/active SLOCs
2. normalize values               # Trim, uppercase, fill NA → 'NA' / 0
3. fill missing dimensions        # ISNULL ensures no NULL hierarchy
4. SEG = [APP, GM]                 # Apparel + General Merchandise
5. pivot by SLOC                   # Each SLOC becomes a column
6. merge MASTER_ALC_PEND          # Join in pending allocations
7. FNL_Q = max(STK - PEND, 0)     # Final qty available
8. generate color variants        # Explode VAR_ART per CLR
9. aggregate                       # Roll up to TOTAL / GEN_ART / VAR_ART
```

### Files

| File | Purpose |
|---|---|
| `app/services/msa_service.py` | The 9-step algorithm |
| `app/api/v1/endpoints/msa_stock.py` | HTTP routes (`/columns`, `/distinct`, `/calculate`) |
| `frontend/src/pages/MSAStockCalculationPage.jsx` | The page |
| `app/api/v1/endpoints/pipeline.py` | Parallel-MSA orchestrator (replaces 20-machine Excel) |

### Example: change the SEG list to add NB (non-branded)

`msa_service.py` → look for `SEG = ['APP', 'GM']` → add `'NB'`. Make
sure the source data actually has SEG=NB rows; otherwise the new
segment yields empty pivots.

### Example: trigger a calc from script

```bash
curl -X POST "$API/api/v1/msa-stock/calculate" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "date": "2026-04-29",
    "filters": {"SLOC": ["V01","V02_FRESH"]},
    "threshold": 1
  }'
```

### Performance gotchas

- `SELECT DISTINCT CAST([DATE] AS DATE)` in the page-load query is **non-sargable** — drop the cast or query `[DATE]` directly. This single change cuts page-open time from ~30s to <1s.
- `SELECT TOP 1 *` to discover columns forces full view materialisation. Use `INFORMATION_SCHEMA.COLUMNS` instead.
- For large SLOC lists, run MSA as a background job (`pipeline.py` does this).

---

## 7.2 BDC Creation — `frontend/src/pages/BDCCreationPage.jsx`

### What it does

BDC = Business Document Creation. You upload a delivery document
(usually from SAP), parse it, and turn it into an allocation
sequence. Each row maps an article × store × quantity.

### Files

| File | Purpose |
|---|---|
| `frontend/src/pages/BDCCreationPage.jsx` | UI — upload, sheet picker, sequence list |
| `app/api/v1/endpoints/bdc.py` | All BDC routes |
| `app/services/bdc_service.py` | File parsing, sequence building, save logic |

### Workflow

```
Upload .xlsx → pick sheet → preview → parse to sequence
                                         │
                          ┌──────────────┴──────────────┐
                          ▼                              ▼
                 Edit Delivery Orders        Save → ARS_ALLOCATION_MASTER
                 (adjust quantities)              + ARS_ALLOCATION_DETAIL
                                                  + audit row
```

### Example: run a BDC sheet from CLI

```bash
curl -X POST "$API/api/v1/bdc/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@delivery.xlsx" -F "sheet=Sheet1"
```

### Common change: add a new BDC column to the parser

`bdc_service.py` → `parse_bdc_sheet` → extend `expected_columns` map
and add the column to the output sequence schema. Update the frontend
column-picker too.

### Performance

Large BDCs (>50k rows) are slow because the parser builds a Python
list of dicts in memory before bulk-inserting. For files this big,
add a streaming-parse path that writes to a temp table directly.

---

## 7.3 Grid Builder — `frontend/src/pages/GridBuilderPage.jsx`

Already covered in detail in `03_how_to_add_a_grid.md`. Quick recap:

```
Grid metadata in ARS_GRID_BUILDER ──► run ──► output table per grid
                                            (ARS_GRID_* with PIVOT
                                             on active SLOCs)
```

### Settings that matter

- `GRID_RUN_PARALLELISM` (config.py) — workers in Run All Active
- `GRID_INSERT_CHUNK_SIZE` — rows per chunked INSERT (default 250k)
- `AUTO_RESOLVE_LOG_BACKUP_WAIT` — auto-flip FULL→SIMPLE if log truncation is blocked

### Where each grid plugs into the calc pipeline

When a grid is marked `use_for_opt_sale=true`, its `MBQ` and `OPT_CNT`
columns feed back into the Listing module's `PER_OPT_SALE` calculation.
Only one grid can be the source of truth at a time.

---

## 7.4 Lookup Art Master — `frontend/src/pages/LookupArtMasterPage.jsx`

### What it does

Take any user-supplied file with article codes, enrich it by joining to
`vw_master_product`, and return the enriched file.

### Files

| File | Purpose |
|---|---|
| `frontend/src/pages/LookupArtMasterPage.jsx` | UI |
| `app/api/v1/endpoints/lookup_art_master.py` | Routes |
| `app/services/lookup_art_master_service.py` | JOIN logic |

### Workflow

```
Upload file → pick join key (article column) → pick master columns to pull
            → preview matched/unmatched → download enriched CSV
```

### Example: enrich a file with descriptions and division

```bash
# Upload returns a temp_id
curl -X POST "$API/api/v1/lookup-art-master/upload" \
  -F "file=@articles.csv" \
  -H "Authorization: Bearer $TOKEN"

# Then join
curl -X POST "$API/api/v1/lookup-art-master/join" \
  -H "Content-Type: application/json" \
  -d '{
    "temp_id": "<from-upload>",
    "join_key": "article_code",
    "master_columns": ["DESCRIPTION","DIVISION","BRAND"]
  }'
```

### Performance

- Cap user files at ~50k rows. Bigger files should go through Export
  Data with the master view JOINed on the SQL side.
- The JOIN uses `WITH (NOLOCK)` because `vw_master_product` rarely
  changes during the day.

---

## 7.5 Listing — `frontend/src/pages/ListingPage.jsx`

### What it does

Decides which articles are "listed" (eligible for allocation) at which
stores, with attributes like rank, division, brand. Output is the
`PER_OPT_SALE` calc table that allocation logic reads.

### Files

| File | Purpose |
|---|---|
| `frontend/src/pages/ListingPage.jsx` | UI — filters, contribution view, run panel |
| `app/api/v1/endpoints/listing.py` | Routes |
| `app/services/listing_service.py` | Listing logic — eligibility + ranking |
| `app/services/grid_calculations.py` | `calculate_per_day_sale` — feeds listing |

### Two run modes

| Mode | What it does |
|---|---|
| **Generate** | Build the listing master from MSA + grid outputs |
| **Run** | Apply listing rules to today's allocation candidates |

### Example: trigger generation as a job

```bash
curl -X POST "$API/api/v1/listing/generate" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{ "maj_cats": ["MENS_APP","WOMENS_APP"] }'
```

The job appears in Jobs Dashboard with `status=running`, gets
heartbeat updates, and can be cancelled.

### Common change: tweak the listing rank formula

`listing_service.py` → look for the `_compute_rank` function. The
default ranks by sales contribution × stock availability. To favour
freshness, multiply by `EXP(-age_days / 30)`.

---

## Cross-cutting performance guidance

1. **Cache the heavy stuff.** Page-load discoveries (column lists, distinct values, dates) repeat on every reload. A 5-minute in-memory cache on the service layer pays for itself many times over.
2. **Materialise repeated views.** `VW_ET_MSA_STK_WITH_MASTER` is queried in 3+ places per page load. Build it as a nightly-refreshed table and the page is instant.
3. **Pre-aggregate where possible.** Contribution % does the same `SUM` over the same windows daily — pre-compute at SLOC × GEN_ART grain in a refreshed table.
4. **Use background jobs for anything > 30s.** Foreground = browser hangs, support tickets, frustrated users.
