# OneSize — Data Sources, Formulas, Allocation

**Module:** Data Preparation → OneSize
**Code:** `backend/app/api/v1/endpoints/onesize.py` · `frontend/src/pages/OneSizePage.jsx`
**Route:** `GET /onesize` · API `POST /api/v1/onesize/run`, `GET /api/v1/onesize/export.csv`
**Side-effects:** **None.** Pure read-only. No `INSERT` / `UPDATE` / `DELETE`.

---

## 1. Purpose

OneSize identifies `(MAJCAT, GEN_ART, CLR)` combinations with **few sizes** (one-size-like items), enriches them with store / sales / stock / contribution / ranking data, computes per-size MBQ and requirement, then **allocates available warehouse stock** to stores in priority order.

Output: one row per `(ST_CD, MAJ_CAT, GEN_ART, CLR, SZ)` with the qty to ship.

---

## 2. Pipeline overview

```
┌──────────────────────┐
│ ARS_MSA_TOTAL        │  ← Latest sequence_id only, SSN ∈ user pick
└─────────┬────────────┘
          ▼
┌──────────────────────┐
│ MASTER_SZ_APPLICABLE │  ← keep MAJCATs with APPLICABLE='Y'
└─────────┬────────────┘
          ▼
   Group (MAJCAT, GEN_ART, CLR) → count, fnl_q_sum
          ▼
   Filter: count ≤ N · fnl_q_sum ≤ M · CLR≠'A' · SZ≠'A'
          ▼
   status = '1sz'
          ▼
┌──────────────────────────────────┐
│ Master_ALC_INPUT_ST_MASTER       │  stores + RDC + days
└─────────┬────────────────────────┘
          ▼  user RDC filter (optional)
          ▼  INNER JOIN on RDC
          ▼
┌──────────────────────────────────┐
│ Master_CONT_SZ                   │  cont per (ST_CD, MAJ_CAT, SZ)
└─────────┬────────────────────────┘
          ▼
┌──────────────────────────────────┐
│ ARS_CALC_ST_MAJ_CAT              │  DISP_Q, SAL_PD per (ST_CD, MAJ_CAT)
└─────────┬────────────────────────┘
          ▼   MBQ_SZ = (DISP_Q + SAL_PD × days) × cont
          ▼
┌──────────────────────────────────┐
│ ARS_GRID_MJ                      │  ACS_D per (ST_CD, MAJ_CAT)
└─────────┬────────────────────────┘
          ▼   var_art_disp = ACS_D × cont
          ▼
┌──────────────────────────────────┐
│ ARS_GRID_MJ_GEN_ART              │  L-7-DAYS-SALE per option
└─────────┬────────────────────────┘
          ▼   MAX_DAILY_SALE = MAX(L-7/7, SAL_PD)
          ▼   SALE_VAR_ART   = MAX_DAILY_SALE × cont
          ▼   MBQ_VAR        = (var_art_disp + SALE_VAR_ART) × days
          ▼                    (forced 0 when both inputs 0/NULL)
┌──────────────────────────────────┐
│ ARS_GRID_MJ_VAR_ART              │  STK_TTL per (ST_CD, MAJ_CAT, SZ)
└─────────┬────────────────────────┘
          ▼   REQ      = MAX(MBQ_SZ  − STK_TTL, 0)
          ▼   VAR_REQ  = MAX(MBQ_VAR − STK_TTL, 0)
          ▼
┌──────────────────────────────────┐
│ ARS_MSA_TOTAL (re-aggregated)    │  MSA_REMAIN = SUM(FNL_Q) per pool
└─────────┬────────────────────────┘
          ▼
┌──────────────────────────────────┐
│ ARS_STORE_RANKING                │  ST_RANK per (ST_CD, MAJ_CAT)
└─────────┬────────────────────────┘
          ▼   ALLOCATION (sequential by ST_RANK within pool):
          ▼     ALLOC = MIN( positive(VAR_REQ, REQ), remaining_pool )
          ▼     skip if both REQ and VAR_REQ are 0; stop if pool empty
          ▼
        RESULT  (one row per ST_CD × MAJ_CAT × GEN_ART × CLR × SZ)
```

---

## 3. Stage-by-stage data lineage

Stages map 1:1 to UI progress labels and backend logs.

### Stage 1 — `latest_sequence`
| | |
|---|---|
| Table | `ARS_MSA_TOTAL` |
| SQL | `SELECT MAX(sequence_id) FROM ARS_MSA_TOTAL` |

### Stage 2 — `load_msa`
| | |
|---|---|
| Table | `ARS_MSA_TOTAL` |
| Filter | `sequence_id = <latest>` AND `UPPER(TRIM(SSN)) IN (<ssn list>)` |
| SSN default | `['A', 'OC', 'S']` — UI multi-select |
| Required cols | `MAJCAT`, `GEN_ART`, `CLR`, `SZ`, `FNL_Q`, `SSN`, `sequence_id` |

### Stage 3 — `applicable_majcats` + `filter_to_applicable`
| | |
|---|---|
| Table | `MASTER_SZ_APPLICABLE` |
| Filter | `UPPER(TRIM(APPLICABLE)) = 'Y'` |
| Effect | Drops MSA rows whose `MAJCAT` isn't in the applicable set |

### Stage 4 — `group_and_count`
`count = transform("size")` over `(MAJCAT, GEN_ART, CLR)` — broadcast to every row.

### Stage 4b — `group_fnl_q_sum`
`fnl_q_sum = SUM(FNL_Q)` over `(MAJCAT, GEN_ART, CLR)` — broadcast.
`FNL_Q` = MSA's final available qty = `STK_QTY − PEND_QTY − HOLD_QTY ⌃ 0` (`msa_service.py:684`).

### Stage 5 — `filter_count`
Keep `count ≤ count_threshold` (UI, default **2**).

### Stage 5b — `filter_fnl_q_sum`
Drop rows where `fnl_q_sum > qty_threshold` (UI, default **50**).

### Stage 5c — `filter_placeholder`
Drop rows where `CLR='A'` OR `SZ='A'` (default-fill placeholders from `msa_service.py:479,485`).
Survivors → `status = '1sz'`.

### Stage 6 — `load_stores`
| | |
|---|---|
| Table | `Master_ALC_INPUT_ST_MASTER` |
| Required | `ST_CD` (or `WERKS`/`STORE_CODE`) |
| Optional | `ST_NM`, `RDC` (or `WAREHOUSE`/`HUB`/`WH_CD`), `SL_CVR`, `INT_DAYS`, `PRD_DAYS` |
| **`days` formula** | `SUM(ISNULL(SL_CVR,0)) + SUM(ISNULL(INT_DAYS,0)) + SUM(ISNULL(PRD_DAYS,0))` per store |
| Aggregation | `GROUP BY ST_CD, ST_NM, RDC` |

### Stage 7a — `apply_rdc_filter` (UI selection)
- Empty selection → all RDCs
- Non-empty → both MSA and stores reduced to selected RDCs before the join

### Stage 7b — `cross_join`
| | |
|---|---|
| Type | **INNER JOIN on normalized RDC** — a store only matches its own RDC's MSA rows |
| Fallback | `cartesian_fallback` when RDC missing on either side (logged as warning) |
| RDC safety | If `RDC` column is somehow absent post-merge, it's reconstructed from the join key |

### Stage 8 — `enrich_cont`
| | |
|---|---|
| Table | `Master_CONT_SZ` |
| Join | LEFT on `(ST_CD, MAJ_CAT, SZ)` — trim+upper normalized |
| Cols | `CONT` (or `CONTRIBUTION` / `CONT_PCT` / `CONT_PERCENT`) |

### Stage 9 — `enrich_calc_maj_cat`
| | |
|---|---|
| Table | `ARS_CALC_ST_MAJ_CAT` |
| Join | LEFT on `(ST_CD, MAJ_CAT)` |
| Cols | `DISP_Q` (or `DESP_Q`/`DISPATCH_Q`), `SAL_PD` (or `SAL_PER_DAY`) |

### Stage 9b — `enrich_grid_mj_base`
| | |
|---|---|
| Table | `ARS_GRID_MJ` (maj_cat-grain grid) |
| Join | LEFT on `(ST_CD = WERKS, MAJ_CAT)` |
| Cols | `ACS_D` (or `MJ_ACS_D` / `ACS_DAYS`) |
| Pre-agg | `MAX(ACS_D)` per `(store, maj_cat)` |

### Stage 9c — `compute_var_art_disp`
```
var_art_disp = ACS_D × cont           (NaN propagates if either is NULL)
```

### Stage 10 — `compute_mbq_sz`
```
MBQ_SZ = (DISP_Q + SAL_PD × days) × cont
```
- Inside parens: NaN → 0 (so missing inputs don't kill all of MBQ_SZ)
- `cont = NaN`: `MBQ_SZ = NaN` (no contribution → no meaningful MBQ)

### Stage 11 — `enrich_grid_mj`
| | |
|---|---|
| Table | `ARS_GRID_MJ_VAR_ART` |
| Join | LEFT on **2 keys** `(ST_CD = WERKS, ARTICLE_NUMBER)` |
| Output | `STK_TTL` (option-level stock — used by `VAR_REQ`) |
| Pre-agg | `SUM(STK_TTL)` per `(store, article)`, server-side |

### Stage 11b — `enrich_grid_mj_sz`
| | |
|---|---|
| Table | `ARS_GRID_MJ_VAR_ART` (same table, different grain) |
| Join | LEFT on **3 keys** `(ST_CD = WERKS, MAJ_CAT, SZ)` |
| Output | `STK_TTL_SZ` (size-level stock — used by `REQ`) |
| Pre-agg | `SUM(STK_TTL)` per `(store, maj_cat, sz)`, server-side |

### Stage 12 — `compute_req`
```
REQ = ROUND( MAX(MBQ_SZ − STK_TTL, 0),  0 )       # integer
```
- `STK_TTL = NaN` → treated as 0
- `MBQ_SZ = NaN` → `REQ = NaN`

### Stage 13 — `enrich_listing` (computes `MAX_DAILY_SALE`)
Mirrors Listing Part 4b + 4c, but computed **directly from upstream sources** so OneSize doesn't depend on Listing Generation having run.

```
SAL_PD             =  MASTER_GEN_ART_SALE.SAL_PD   (option-grain, strict; 0 where missing)
AUTO_GEN_ART_SALE  =  SAL_PD × cont                                        ← size-scaled baseline
L7_DAILY           =  ARS_GRID_MJ_GEN_ART.<L-7-DAYS-SALE-Q col>  ÷ 7.0
PER_OPT_SALE       =  ((OPT_GRID_MBQ − OPT_GRID_DISP_Q) / OPT_GRID_DISP_Q × ACS_D) / ALC_D
                      (fallback: SAL_PD when grid path yields 0)
AGE                =  MASTER_GEN_ART_AGE.AGE  (NULL → 0)
MAX_DAILY_SALE     =  AGE < 15  →  MAX(L7_DAILY, AUTO_GEN_ART_SALE, PER_OPT_SALE)
                      AGE ≥ 15  →  MAX(L7_DAILY, AUTO_GEN_ART_SALE) − PER_OPT_SALE   (clipped to ≥ 0)
```

**PER_OPT_SALE sources** (Listing Part 4b mirror, with `SAL_PD` fallback):
1. Active opt-sale grid name from `ARS_GRID_BUILDER` where `use_for_opt_sale=1` AND `status='ACTIVE'`
2. `<prefix>_MBQ` / `<prefix>_DISP_Q` from `ARS_GRID_<grid_name>` (option-grain — joined on 4 keys)
3. `ACS_D` from `ARS_GRID_MJ` (Stage 9b)
4. `ALC_D` from `ARS_CALC_ST_MAJ_CAT` (extended in Stage 9)

Guards (matching `listing.py:1518-1522` `CASE` statement):
- `OPT_GRID_DISP_Q ≤ 0` OR `ALC_D ≤ 0` → `PER_OPT_SALE = 0` (then triggers fallback)
- Negative result (over-stocked) → clipped to 0 (then triggers fallback)
- Rounded to 2 decimals

**Fallback rule:** after the grid math, any row with `PER_OPT_SALE ≤ 0` is replaced with `SAL_PD` (= `AUTO_GEN_ART_SALE`). Three diagnostic counters in the stage payload show how each row was filled:
- `per_opt_from_grid` — grid formula produced a positive value
- `per_opt_from_sal_pd` — fell back to `SAL_PD`
- `per_opt_still_zero` — both grid and `SAL_PD` were 0/NULL

| | |
|---|---|
| L-7 column detection | Pattern from `listing.py:1530-1539`: name contains `"L-7"`/`"L_7"` and `"SALE"` and `"7"` |
| L-7 join | LEFT on `(WERKS=ST_CD, MAJ_CAT, GEN_ART_NUMBER, CLR)`, normalized |
| Opt-grid join | LEFT on same 4 keys, against `ARS_GRID_<grid_name>` |
| Numeric coercion | `_norm_num_or_str` for `GEN_ART` (handles `'123'` vs `123` vs `'123.0'`) |
| All inputs NULL | `MAX_DAILY_SALE = NaN`; otherwise NaN → 0 inside the MAX |
| AGE filter (Listing's `rate_expr`) | **NOT** applied here — OneSize always includes PER_OPT_SALE in the MAX. Mature options naturally have PER_OPT_SALE ≈ 0 (MBQ ≈ DISP_Q). |

### Stage 14 — `compute_sale_var_art`
```
SALE_VAR_ART = MAX_DAILY_SALE × cont
```

### Stage 15 — `compute_mbq_var`
```
MBQ_VAR = ROUND( (MAX_DAILY_SALE × days) + (ACS_D × cont),  0 )
        = ROUND( (MAX_DAILY_SALE × days) +  var_art_disp,    0 )
```
Fixture-floor component is **size-scaled** by `cont` — `MBQ_VAR` now varies per size within the same option. `(ACS_D × cont)` is already in `cross` as `var_art_disp` (Stage 9c).

- `rate` ≡ `MAX_DAILY_SALE` (age-aware blend from Stage 13)
  - `AGE < 15`  → `MAX(L7/7, AUTO_GEN_ART_SALE, PER_OPT_SALE)` (new article)
  - `AGE ≥ 15` → `MAX(L7/7, AUTO_GEN_ART_SALE) − PER_OPT_SALE` (clipped ≥ 0)
- `days` — cover-days component (`SL_CVR + INT_DAYS + PRD_DAYS`)
- `var_art_disp` — `ACS_D × cont` (size-scaled fixture floor)

NaN handling: each component falls through to 0 via `fillna(0)`. So:
- `MAX_DAILY_SALE = 0/NULL` AND `ACS_D = 0/NULL` → `MBQ_VAR = 0`
- `MAX_DAILY_SALE = 0/NULL` AND `ACS_D = 16` → `MBQ_VAR = 16` (fixture floor only)
- `MAX_DAILY_SALE = 2.29, days = 9` AND `ACS_D = NULL` → `MBQ_VAR = 21` (rate side only)

`MBQ_VAR` is at **variant (option) grain** — same value for every size of the same `(GEN_ART, CLR)` since neither `MAX_DAILY_SALE` nor `ACS_D` varies by size. Per-size adjustment happens in `VAR_REQ` via the size-specific `STK_TTL`.

### Stage 16 — `compute_var_req`
```
VAR_REQ = ROUND( MAX(MBQ_VAR − STK_TTL, 0),  0 )      # integer
```

### Stage 17 — `enrich_msa_remain_and_rank`

**Part A — load pool (MSA_REMAIN)**
```sql
SELECT RDC, MAJCAT, GEN_ART, CLR, SZ,
       SUM(TRY_CAST(FNL_Q AS FLOAT)) AS MSA_REMAIN
FROM   ARS_MSA_TOTAL
WHERE  sequence_id = :sid AND SSN IN (...)        -- same as load_msa
GROUP BY RDC, MAJCAT, GEN_ART, CLR, SZ
```
LEFT-joined onto cross on 5-key `(RDC, MAJ_CAT, GEN_ART, CLR, SZ)`. Rounded to integer.

**Part B — load store ranks (ST_RANK)**
| | |
|---|---|
| Table | `ARS_STORE_RANKING` |
| Cols | `RANK` (or `RANKING` / `ST_RANK` / `RANK_NUM` / `STORE_RANK`) |
| Join | LEFT on `(ST_CD, MAJ_CAT)` — same store, different ranks per category |
| Duplicates | `MIN(rank)` per `(ST_CD, MAJ_CAT)` |
| Unranked stores | `ST_RANK = NaN` → sorted **last** (rank = +∞) |

### Stage 18 — `compute_allocation`

**STRICT RULE: BOTH `REQ > 0` AND `VAR_REQ > 0` required.** If either is 0 or NULL, the row is skipped and the pool is not depleted.

**Algorithm:**
```python
sort by (pool_key, ST_RANK ASC NULLS LAST, ST_CD ASC)
for each pool group:
    available = MSA_REMAIN[first row in group]    # all same per pool
    for each row in rank order:
        has_vr = VAR_REQ > 0
        has_rq = REQ > 0
        if not has_vr and not has_rq:
            ALLOC = 0;  DEMAND_SRC = "NONE";        continue
        if not has_vr:
            ALLOC = 0;  DEMAND_SRC = "NO_VAR_REQ";  continue   # pool untouched
        if not has_rq:
            ALLOC = 0;  DEMAND_SRC = "NO_REQ";      continue   # pool untouched
        if available <= 0:
            ALLOC = 0;  DEMAND_SRC = "POOL_EMPTY";  continue
        # Both REQ > 0 and VAR_REQ > 0
        demand_cap = MIN(VAR_REQ, REQ)
        cap = MIN(demand_cap, available)
        ALLOC = ROUND(cap, 0);  DEMAND_SRC = "BOTH"
        available -= ALLOC
        REMAIN_AFTER = available
```

**Key design points:**
| | |
|---|---|
| Both required | `REQ ≤ 0` OR `VAR_REQ ≤ 0` → no allocation. Pool stays intact. |
| Cap formula | When both > 0: cap = `MIN(VAR_REQ, REQ, MSA_REMAIN)`. |
| Tie-break | `ST_CD ASC` when two stores have the same rank |
| Pool grain | `(RDC, MAJ_CAT, GEN_ART, CLR, SZ)` |
| Statefulness | Yes — each row sees `available` decremented by earlier-ranked rows |
| Rounding | All outputs are integers (`ROUND(...,0)`) |

---

## 4. Tables used (quick reference)

| Table | DB | Grain | Used for |
|---|---|---|---|
| `ARS_MSA_TOTAL` | Rep_data | sequence × row | source data + `MSA_REMAIN` |
| `MASTER_SZ_APPLICABLE` | Rep_data | per MAJ_CAT | which MAJCATs qualify |
| `Master_ALC_INPUT_ST_MASTER` | Rep_data | per store | stores + RDC + `days` |
| `Master_CONT_SZ` | Rep_data | `(ST_CD, MAJ_CAT, SZ)` | `cont` |
| `ARS_CALC_ST_MAJ_CAT` | Rep_data | `(ST_CD, MAJ_CAT)` | `DISP_Q`, `SAL_PD` |
| `ARS_GRID_MJ` | Rep_data | `(WERKS, MAJ_CAT)` | `ACS_D` |
| `ARS_GRID_MJ_GEN_ART` | Rep_data | `(WERKS, MAJ_CAT, GEN_ART, CLR)` | `L-7-DAYS-SALE-Q` |
| `ARS_GRID_MJ_VAR_ART` | Rep_data | `(WERKS, MAJ_CAT, VAR_ART, SZ)` | `STK_TTL` |
| `ARS_STORE_RANKING` | Rep_data | `(ST_CD, MAJ_CAT)` | `ST_RANK` |

All resolved via case-insensitive `_resolve_col()`. Missing optional columns degrade to NULL; missing required columns raise HTTP 500 with the resolver's actual findings.

---

## 5. Output columns (display order)

| Column | Source | Type |
|---|---|---|
| `ST_CD` | Master_ALC_INPUT_ST_MASTER | str |
| `ST_NM` | Master_ALC_INPUT_ST_MASTER | str |
| `RDC` | Master_ALC_INPUT_ST_MASTER | str |
| `ST_RANK` | ARS_STORE_RANKING | float (NaN = unranked) |
| `days` | Master_ALC_INPUT_ST_MASTER | float (sum of 3 day cols) |
| `MAJCAT`, `GEN_ART`, `CLR`, `SZ` | ARS_MSA_TOTAL | str/int |
| `count`, `fnl_q_sum` | derived | int / float |
| `cont` | Master_CONT_SZ | float |
| `DISP_Q`, `SAL_PD` | ARS_CALC_ST_MAJ_CAT | float |
| `MBQ_SZ` | derived: `(DISP_Q + SAL_PD × days) × cont` | float |
| `ACS_D` | ARS_GRID_MJ | float |
| `var_art_disp` | derived: `ACS_D × cont` | float |
| `L7_DAILY` | derived: `ARS_GRID_MJ_GEN_ART.<L-7> / 7` | float |
| `AUTO_GEN_ART_SALE` | alias for `SAL_PD` | float |
| `MAX_DAILY_SALE` | derived: `MAX(L7_DAILY, AUTO_GEN_ART_SALE)` | float |
| `SALE_VAR_ART` | derived: `MAX_DAILY_SALE × cont` | float |
| `MBQ_VAR` | derived: `(var_art_disp + SALE_VAR_ART) × days` | **int** |
| `STK_TTL` | ARS_GRID_MJ_VAR_ART | float (sum) |
| `REQ` | derived: `MAX(MBQ_SZ − STK_TTL, 0)` | **int** |
| `VAR_REQ` | derived: `MAX(MBQ_VAR − STK_TTL, 0)` | **int** |
| `MSA_REMAIN` | derived: `SUM(FNL_Q)` per pool | **int** |
| `ALLOC` | derived: allocation algorithm | **int** |
| `REMAIN_AFTER` | pool size after this row was processed | **int** |
| `DEMAND_SRC` | `BOTH` / `VAR_REQ_ONLY` / `REQ_ONLY` / `NONE` / `POOL_EMPTY` | str |
| `status` | always `'1sz'` for OneSize-qualifying rows | str |
| `FNL_Q`, `STK_QTY`, `SSN` | ARS_MSA_TOTAL | float / float / str |
| _…rest…_ | every remaining MSA column | varies |

`id` and `sequence_id` are stripped before export.

---

## 6. UI controls (per run)

| Control | Backend param | Default | Effect |
|---|---|---|---|
| **Count threshold (≤)** | `count_threshold` | `2` | Keep rows with `count ≤ N` |
| **Sum(FNL_Q) ≤** | `qty_threshold` | `50.0` | Drop rows with `fnl_q_sum > M` |
| **RDC (optional)** | `rdc` (repeat) | `[]` (all) | Restricts MSA + stores before join |
| **SSN (season)** | `ssn` (repeat) | `['A','OC','S']` | `IN (...)` on the MSA load |
| **Execute** | `POST /run` | — | runs all 18 stages, returns 1000-row preview + diagnostics |
| **Export CSV** | `GET /export.csv` | — | streams full result |

Array params (`rdc`, `ssn`) use repeat-form serialization (`?rdc=DH24&rdc=DH25`) because FastAPI's `List[str]` parser ignores `?rdc[]=X`.

---

## 7. Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/onesize/run` | Run pipeline, return preview + stage diagnostics |
| `GET` | `/api/v1/onesize/export.csv` | Stream full result as CSV |
| `GET` | `/api/v1/onesize/rdcs` | List distinct RDCs (UI dropdown) |
| `GET` | `/api/v1/onesize/ssns` | List distinct SSNs from latest sequence (UI dropdown) |

All routes require `get_current_user` (no per-route permission gate).

---

## 8. `DEMAND_SRC` reference (per allocated row)

| Tag | Means |
|---|---|
| `BOTH` | Both `REQ` and `VAR_REQ` were > 0 — allocated `MIN(REQ, VAR_REQ, MSA_REMAIN)` |
| `NO_VAR_REQ` | `VAR_REQ ≤ 0` (but `REQ > 0`) — **skipped**. Pool not depleted. |
| `NO_REQ` | `REQ ≤ 0` (but `VAR_REQ > 0`) — **skipped**. Pool not depleted. |
| `NONE` | Both 0/NULL — skipped. Pool not depleted. |
| `POOL_EMPTY` | Both positive but pool was drained by higher-ranked stores |
| `BOTH_CAP_0` | Rare: both positive but `MIN` with available was 0 (e.g. demand 0.1 → rounds to 0) |

There is **no `VAR_REQ_ONLY` or `REQ_ONLY` tag** — under the strict rule, only `BOTH` triggers allocation.

---

## 9. Integer rounding

These columns are rounded to integer at compute time so the result CSV / preview shows clean numbers:
- `REQ`, `VAR_REQ`, `MBQ_VAR`
- `MSA_REMAIN` (after the pool merge)
- `ALLOC`, `REMAIN_AFTER` (after allocation loop)

`MBQ_SZ`, `var_art_disp`, `L7_DAILY`, `AUTO_GEN_ART_SALE`, `MAX_DAILY_SALE`, `SALE_VAR_ART`, `cont`, `STK_TTL` are kept as floats so you can audit the math.

Rounding happens **before** the allocation loop, so all sequential pool-drain math runs entirely on integers. No fractional residue (no `0.39999` in `REMAIN_AFTER`).

---

## 10. Diagnostics

Every stage emits a row in the `stages[]` array of the API response, surfaced in the UI progress panel and the backend log:

| Stage | Key fields |
|---|---|
| `load_msa` | `rows`, `ssn_filter` |
| `applicable_majcats` | `count` |
| `load_stores` | `stores`, `rdc_col`, `sl_cvr_col`, `int_days_col`, `prd_days_col`, `days_components` |
| `apply_rdc_filter` | `selected`, `msa_rows_in/out`, `stores_in/out` |
| `cross_join` | `mode` (`inner_on_rdc` / `cartesian_fallback`), `matched_rdcs`, `stores_per_matched_rdc`, `selected_rdcs` |
| `enrich_cont` | `lookup_rows`, `matched_rows`, `unmatched_rows` |
| `enrich_calc_maj_cat` | same + `DISP_Q`/`SAL_PD` resolved names |
| `enrich_grid_mj_base` | same for `ACS_D` |
| `compute_var_art_disp` | `computed_rows`, `null_rows`, formula |
| `compute_mbq_sz` | `computed_rows`, `null_rows`, formula |
| `enrich_grid_mj` | `lookup_rows`, `unique_keys`, `matched_rows` (STK_TTL) |
| `compute_req` | `rows_with_req`, `nonzero_req`, formula |
| `enrich_listing` | `auto_non_null`, `l7_source`, `l7_loaded_rows`, `l7_matched_rows`, `mds_picked_l7`, `mds_picked_auto`, `mds_both_null` |
| `compute_sale_var_art` | `computed_rows`, `null_rows` |
| `compute_mbq_var` | `computed_rows`, `forced_zero_rows`, formula |
| `compute_var_req` | `rows_with_var_req`, `nonzero_var_req` |
| `enrich_msa_remain_and_rank` | `pool_rows`, `pool_matched`, `rank_matched` |
| `compute_allocation` | `allocated_rows`, `src_both`, `src_var_only`, `src_req_only`, `skipped_no_demand`, `skipped_pool_empty`, `pools_drained`, `pools_partial`, `total_allocated` |

Loud backend breadcrumbs (`[onesize][<stage>][<checkpoint>] key=value, ...`) also print to stdout with `flush=True` so they appear live in the console.

---

## 11. Implementation notes

- **Pure read.** No writes anywhere. RCSI in `Rep_data` means readers never block writers.
- **Pandas in-memory.** Whole pipeline runs on the latest MSA sequence (~70k rows pre-filter). Allocation loop is numpy-per-group factorized; ~3500 pools × ~200 stores runs in a few seconds.
- **No background-job queue.** Synchronous. Axios timeout is 5 minutes (`api.js:14`).
- **CSV export re-runs the full pipeline** — it doesn't cache the preview run's frame. Different params between Execute and Export produce different CSVs.
- **Pre-aggregation at source.** `STK_TTL` from `ARS_GRID_MJ_VAR_ART` and `MSA_REMAIN` from `ARS_MSA_TOTAL` are both aggregated in SQL before the LEFT JOIN, so the join stays 1:1 with the result grain.
- **No dependency on Listing Generation.** `MAX_DAILY_SALE` is computed from upstream sources (`SAL_PD` on `ARS_CALC_ST_MAJ_CAT` + `L-7` on `ARS_GRID_MJ_GEN_ART`) — does **not** read from `ARS_LISTING`.
- **Allocation is stateful per pool** — can't vectorize. But the per-group loop is tight numpy with no pandas overhead.

---

## 12. Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| HTTP 404 `No data found in ARS_MSA_TOTAL` | No MSA save yet | Run MSA Stock Calculation first |
| HTTP 500 `MASTER_SZ_APPLICABLE missing required columns…` | Column names don't match resolver candidates | Rename or extend resolver |
| `cross_join.mode = cartesian_fallback` | RDC missing from `Master_ALC_INPUT_ST_MASTER` | Add RDC column or accept cartesian |
| `cont = NULL` rows everywhere | `Master_CONT_SZ` is empty or keys don't match | Verify table has data; check `enrich_cont.matched_rows` |
| `MAX_DAILY_SALE` empty for all rows | `SAL_PD` is empty AND L-7 fallback failed | Run Contribution calc to fill `SAL_PD`; check `enrich_listing.auto_non_null` |
| `MBQ_VAR = 0` everywhere even though `SAL_PD` and `cont` look fine | `var_art_disp = 0` because `ACS_D = 0` for that row | Verify `ARS_GRID_MJ.ACS_D > 0`; remember the rule is strict — **either** input being 0 zeros `MBQ_VAR` |
| `ALLOC = 0` everywhere | Either no demand exists OR pool always 0 OR ranking missing | Check `compute_allocation.src_*` counters and `enrich_msa_remain_and_rank.pool_matched` |
| Rows where `REQ = 0` AND `VAR_REQ = 0` get no allocation | Working as designed — both demand metrics say no need | Verify with `DEMAND_SRC = NONE` per row |
| Pool drained immediately for some stores | High-priority store(s) consumed full pool — expected | Check `REMAIN_AFTER` for the depleting row(s) |
