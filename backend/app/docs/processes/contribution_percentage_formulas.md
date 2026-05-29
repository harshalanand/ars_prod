---
title: Contribution Percentage — Working & Formula Reference
category: Data Prep / Technical
order: 51
source: backend/app/api/v1/endpoints/contrib.py
last_reviewed: 2026-05-28
---

# Contribution Percentage — Working & Formula Reference

End-to-end working of `backend/app/api/v1/endpoints/contrib.py`, with every formula
the pipeline computes. Companion to `contribution_percentage.md` (which is the
user guide).

---

## 1. Components

| Layer | Table | Purpose |
|---|---|---|
| Config | `Cont_presets` | Defines a "period" — months, avg_days, kpi_type, sequence order |
| Config | `Cont_mappings` | SSN → suffix lookup + fallback suffix list |
| Config | `Cont_mapping_assignments` | Wires a mapping to a target output column; one row can be `is_active` |
| Output | `Cont_Percentage_<GC>_<YYYY_MM>` | Store-level result table |
| Output | `Cont_Percentage_<GC>_CO_<YYYY_MM>` | Company-level result table |
| Jobs | `Cont_jobs` | Persistent job history (status, log, durations) |

`<GC>` = grouping column (e.g. `M_VND_CD`, `MACRO_MVGR`, `CLR`, `SZ`, …).
Valid grouping columns: `CLR, SZ, RNG_SEG, M_VND_CD, MACRO_MVGR, MICRO_MVGR, FAB, WEAVE_2, M_YARN_02`.

---

## 2. Pipeline overview

```
POST /contrib/execute
       │
       ▼
 _run_job(job_id)
  ├─ load master once  (master_avg_density, Master_STORE_PLAN APF)
  │
  ├─ for each selected preset (in sequence_order):
  │     _process_single_preset()
  │        1. SQL data query     → df_data    (store × MAJ_CAT × GC totals)
  │        2. SQL master CROSS JOIN → df_master  (cached across presets)
  │        3. merge df_master + df_data + avg_density + APF
  │        4. aggregate to company level
  │        5. _compute_kpis() on detail & aggregate
  │
  ├─ _combine_dataframes()  → wide frame, columns suffixed "|<preset>"
  │
  ├─ _apply_mapping_assignments()       → e.g. "AUTO CONT%" column
  ├─ _apply_auto_cont_derivations()     → "AUTO CONT% 2", "(FINAL)", "BGT (FINAL)"
  │
  ├─ store frame inherits BGT CONT% (FINAL) from company frame
  │     (_inherit_company_bgt_final)
  │
  ├─ if company source exists → _apply_store_contribution_chain()
  │     produces V-0015 columns (NAT, BGT, AUTO-1/-2, OLD/INT/INT-2/FINAL ST CONT%)
  │
  └─ save pickle (always) + save_to_db (optional)
```

Worker is a `threading.Thread`; multiple jobs can run in parallel.
Each job is persisted to `Cont_jobs` after every status change and auto-deleted
30 minutes after completion.

---

## 3. Source SQL — what `_process_single_preset` reads

### 3.1 Date filter (depends on preset `kpi_type`)

| kpi_type / preset name | SQL filter |
|---|---|
| `L7D` | `sal_stk.KPI = 'L7D'` |
| `L30D` | `sal_stk.KPI = 'L30D'` |
| anything else | `sal_stk.STOCK_DATE IN ('<months…>') AND sal_stk.KPI = 'L18M'` |

### 3.2 Data query — stock × product join (per preset)

```sql
SELECT ST_CD, MAJ_CAT, <GC>,
       AVG(OP_STK_Q), AVG(OP_STK_V),
       AVG(CL_STK_Q), AVG(CL_STK_V),
       AVG(SALE_Q),   AVG(SALE_V),
       AVG(GM_V)
FROM (
   SELECT STOCK_DATE, WERKS AS ST_CD, prod.MAJ_CAT, prod.<GC>,
          SUM(OP_STK_QTY)/1000  AS OP_STK_Q,
          SUM(OP_STK_VAL)/1e5   AS OP_STK_V,
          SUM(CL_STK_QTY)/1000  AS CL_STK_Q,
          SUM(CL_STK_VAL)/1e5   AS CL_STK_V,
          SUM(SALE_QTY)/1000    AS SALE_Q,
          SUM(SALE_VAL)/1e5     AS SALE_V,
          SUM(GM_VAL)/1e5       AS GM_V
   FROM dbo.COUNT_STOCK_DATA_18M sal_stk
   LEFT JOIN (
        SELECT ARTICLE_NUMBER AS MATNR, MAJ_CAT, <GC_expr> AS <GC>, SEG
        FROM dbo.VW_MASTER_PRODUCT
   ) prod ON sal_stk.MATNR = prod.MATNR
   WHERE MAJ_CAT IN (...)             -- if selected
     AND prod.SEG IN ('APP','GM')
     AND <date_filter>
   GROUP BY WERKS, STOCK_DATE, prod.MAJ_CAT, prod.<GC>
) t
GROUP BY ST_CD, MAJ_CAT, <GC>;
```

**Scaling note**: Q values are stored in **thousands** (÷1000), V values in
**lakhs** (÷100000). The KPI formulas multiply by `Q = 1000` and `V = 100000`
where required to reconstruct true units.

`<GC_expr>` is the grouping column wrapped in `COALESCE(NULLIF(<GC>,''),'NA')`
unless its SQL type is numeric.

### 3.3 Master hierarchy query (cached across presets in the same job)

```sql
SELECT B.ST_CD, B.ST_NM, A.<all hier cols>
FROM Master_HIER_<GC> A
CROSS JOIN dbo.Master_STORE_PLAN B
WHERE MAJ_CAT IN (...);
```

This is the big CROSS JOIN: every store × every hierarchy row, so every
(store, MAJ_CAT, GC) cell exists even when sales are zero.

### 3.4 Other masters

```sql
SELECT * FROM master_avg_density;
SELECT ST_CD, APF, STATUS, REF_ST_CD, REF_ST_NM, REF_GRP_NEW, REF_GRP_OLD
FROM Master_STORE_PLAN;
```

The merged dataframe joins on `(ST_CD, MAJ_CAT, <GC>)` and then on `MAJ_CAT`
(for AVG_DNSTY) and `ST_CD` (for APF).

---

## 4. KPI formulas — `_compute_kpis`

Constants:

```
Q = 1000
V = 100000
gr = 2 if grouping_column == 'M_VND_CD' else 1
group_cols = ['ST_CD', 'MAJ_CAT']    # store-level
             ['MAJ_CAT']             # company-level (no ST_CD)
```

`grp = df.groupby(group_cols)`.

### 4.1 Mean stock

```
0001_STK_Q = 0                                      if OP_STK_Q == 0 AND CL_STK_Q == 0
             (OP_STK_Q + CL_STK_Q) / 2              if both > 0
             (OP_STK_Q + CL_STK_Q) / 1              otherwise (only one side has data)

0001_STK_V = same shape on V
```

### 4.2 Display fixtures & area

```
FIX        = 0001_STK_Q × 1000 / max(AVG_DNSTY, 1)
DISP_AREA  = max( APF × FIX ,  1 if SALE_V > 0 else 0 )
```

### 4.3 GM% — gross-margin %

```
GM_%       = GM_V / SALE_V          (SALE_V → 1 when 0, so result is 0)
```

### 4.4 Per-day sales (Q and V)

```
pdsq = (SALE_Q / avg_days) × 1000   if SALE_Q > 0 else 0
pdsv = (SALE_V / avg_days) × 100000 if SALE_V > 0 else 0
```

### 4.5 Stock turnover & PSF productivity

```
STR        = 0                          if pdsq == 0
             0001_STK_Q / pdsq × 1000   otherwise

SALES PSF  = 0                          if DISP_AREA == 0
             pdsv / DISP_AREA           otherwise
```

### 4.6 Majcat means and achievement %

`sv_sum, da_sum, gv_sum` are the SUM of `SALE_V / DISP_AREA / GM_V` within
each (store, MAJ_CAT) group.

```
SALE_PSF_MJ      = (sv_sum × 100000 / da_sum) / avg_days     (0 if da_sum==0)
SALES_PSF_ACH%   = SALES PSF / SALE_PSF_MJ                   (0 if denom==0)

GM PSF           = (GM_V × 100000 / DISP_AREA) / avg_days    (0 if DISP_AREA==0)
GM_PSF_MJ        = (gv_sum × 100000 / da_sum) / avg_days     (0 if da_sum==0)
GM_PSF_ACH%      = max(GM PSF, 0) / max(GM_PSF_MJ, 0)        (0 if denom==0)
```

### 4.7 Contribution shares (this row's share of its (ST_CD, MAJ_CAT) group)

```
stk_sum    = SUM( 0001_STK_Q ) over rows in same group with 0001_STK_Q > 0
sal_sum    = SUM( SALE_V )     over rows in same group with SALE_V    > 0

STOCK_CONT%  = 0                          if 0001_STK_Q ≤ 0
               0001_STK_Q / stk_sum       otherwise

SALE_CONT%   = 0                          if SALE_V ≤ 0
               SALE_V / sal_sum           otherwise
```

So within any (ST_CD, MAJ_CAT) bucket, all positive-stock rows' `STOCK_CONT%`
sum to 1, and all positive-sale rows' `SALE_CONT%` sum to 1.

### 4.8 ALGO and INITIAL AUTO CONT% — the core contribution formula

```
gr             = 2 if grouping_column == 'M_VND_CD' else 1

algo_raw       = SALE_CONT% × (5.0 if SALE_CONT% < 0.05 else 3.0)
algo_adj       = SALE_CONT% × ( 1 + (GM_PSF_ACH% - 1) × gr )

ALGO           = min( algo_raw ,  max(algo_adj, 0) )

algo_sum            = SUM(ALGO) within (ST_CD, MAJ_CAT)
INITIAL AUTO CONT%  = 0                 if algo_sum == 0
                      ALGO / algo_sum   otherwise
```

Interpretation:
- `algo_raw` boosts very small contributors (×5 below 5%, else ×3) — long-tail
  protection so brand-new SKUs don't die on day one.
- `algo_adj` rewards/penalises by GM PSF achievement vs the MAJCAT average,
  doubled for vendor grouping (`gr=2`).
- The min/max sandwich keeps ALGO between 0 and `algo_raw`.
- `INITIAL AUTO CONT%` is just the normalised share, so it sums to 1 within
  each (ST_CD, MAJ_CAT) group.

All KPI columns are rounded to 2 decimals, `inf/-inf` become `NaN` and then
`0`.

---

## 5. Combine — wide frame across presets

`_combine_dataframes()` outer-merges every preset's frame on the shared keys.
Non-key columns are suffixed: `<col>|<preset>`.

```
Keys (store-level):   ST_CD, ST_NM, <hier cols…>, <GC>, AVG_DNSTY, <APF cols…>
Keys (company-level):                <hier cols…>, <GC>, AVG_DNSTY, <APF cols…>
```

Then a `Generated_Date` column is added.

---

## 6. Mapping assignments — `_apply_mapping_assignments`

Selection rule:

1. If any row in `Cont_mapping_assignments` has `is_active = 1` — only that
   row runs.
2. Else — all rows run (legacy fallback).

For each assignment `(col_name, mapping_name, prefix)`:

```
For each (SSN_value → [suffix1, suffix2, …]) in the mapping:
    candidate_cols = [ prefix + suffix    for each suffix that exists in df ]
    row_max        = MAX(candidate_cols)   per row, NaN-aware
    where SSN == SSN_value:  result[row] = row_max[row]

If row's SSN not in any mapping key, result = MAX( prefix + fallback_suffixes )
NaN → 0
df[col_name] = result
```

So the active assignment writes one new column (e.g. `"AUTO CONT%"`) whose
value is the **best `INITIAL AUTO CONT%` across the suffixes that match the
row's SSN bucket**.

---

## 7. Auto-cont derivations — `_apply_auto_cont_derivations`

Inputs:
- `input_col` = the active assignment's `col_name`, or the first existing
  `AUTO CONT%` / `AUTO SEG CONT%` column (case-insensitive fallback).
- `ACT_INACT` (defaults to True if missing).
- `MAJ_CAT`, optionally `RDC_CD`, optionally `MERCH_INPUT`.

### 7.1 AUTO CONT% 2

```
AUTO CONT% 2 = input_col   if ACT_INACT == 'ACT'  AND  input_col >= 0.01
               0           otherwise
```

### 7.2 AUTO CONT% (FINAL)

Group by `(RDC_CD, MAJ_CAT)` if `RDC_CD` exists, else by `MAJ_CAT` only.

```
grp_sum            = SUM(AUTO CONT% 2) per group
AUTO CONT% (FINAL) = 0                          if grp_sum == 0
                     AUTO CONT% 2 / grp_sum     otherwise
```

So within every group, FINAL values sum to 1.

### 7.3 BGT CONT% (FINAL) — merchant override path

Group selection:
- Store-level (`ST_CD` present) **and** `vendor_col` available  → `(MAJ_CAT, vendor_col)`
- Otherwise → `MAJ_CAT` only

```
merch_grp_sum = SUM(MERCH_INPUT) per group

BGT CONT% (FINAL) = MERCH_INPUT          if merch_grp_sum > 0   (merch decided)
                    AUTO CONT% (FINAL)   otherwise              (auto wins)
```

### 7.4 Store-table trimming

At store level the script then drops `AUTO CONT% 2`, `AUTO CONT% (FINAL)` and
the raw `input_col` from the output — the store table only keeps
`BGT CONT% (FINAL)`, which is replaced by the inherited company value next.

---

## 8. Store inherits company BGT CONT% (FINAL) — `_inherit_company_bgt_final`

Workflow guarantee: the **national merchant decision** propagates to every
store row.

Lookup priority:
1. The in-memory `df_company` from this same run (when `target='Both'`).
2. The latest `Cont_Percentage_<GC>_CO_<YYYY_MM>` table.
3. Nothing → keep store's locally-computed value, V-0015 chain is skipped.

```
merge df_store ⋈ company_bgt  on  (MAJ_CAT, vendor_col)
BGT CONT% (FINAL) = company_value if not null, else local value
```

Returns `(df, inherited: bool)` — the flag gates the V-0015 chain.

---

## 9. V-0015 store contribution chain — `_apply_store_contribution_chain`

Runs only when (a) `ST_CD` is in the frame and (b) a company source was
inherited above. Produces 13 columns matching the V-0015 store sheet.

### 9.1 Inputs (per row)

| Symbol | Source |
|---|---|
| `nat` | `BGT CONT% (FINAL)` inherited from company → renamed to `NAT CONT%` |
| `status` | `STATUS` column, upper-stripped: `OLD / NEW / OLD-1 / UPC / …` |
| `is_old, is_new, is_old1, is_upc` | bools from `status` |
| `ssn` | `SSN`, upper-stripped |
| `is_w_pw` | `ssn ∈ {W, PW}` |
| `is_sao` | `ssn ∈ {S, A, OC}` |
| `listing` | `LISTING` column (defaults to 1.0 if missing) |
| `l7d, l30d, ssn_tlm, ssn2` | `INITIAL AUTO CONT%\|<period>` columns matched by suffix (`L7D`, `L30D`, `SSNTLM`, `SSN2`) |
| `_sum_within(x)` | `x.groupby((ST_CD, MAJ_CAT)).transform('sum')` |

### 9.2 The 13 output columns

```
val_default = max(0, l7d, l30d, ssn_tlm, ssn2)
```

**1. `NAT CONT% @ MAJ`**
```
NAT CONT% @ MAJ = SUM(nat)  within (ST_CD, MAJ_CAT)
```

**2. `BGT CONT%`** — 50% OLD, 70% else
```
BGT CONT% = 0.50 × nat  if is_old
            0.70 × nat  otherwise
```

**3. `AUTO CONT%-1`** (OLD pipeline)
```
auto1_old   = max(ssn2, 0)         if is_w_pw
              max(l30d, 0)         if is_sao
              val_default          otherwise

AUTO CONT%-1 = auto1_old × listing   if is_old
               0                     otherwise
```

**4. `RMN AUTO`**
```
RMN AUTO = max(AUTO CONT%-1 − BGT CONT%, 0)
```

**5. `BGT CONT%@MAJ_CAT`, `RMN AUTO @ MAJCAT`**
```
BGT CONT%@MAJ_CAT        = SUM(BGT CONT%) within (ST_CD, MAJ_CAT)   = bgt_maj
RMN AUTO @ MAJCAT        = SUM(RMN AUTO)  within (ST_CD, MAJ_CAT)   = rmn_maj
```

**6. `ALGO`**
```
rmn_share = RMN AUTO / rmn_maj      (0 if rmn_maj == 0)
ALGO      = rmn_share × max(1 − bgt_maj, 0)
```

**7. `AUTO CONT%-2`** (OLD pipeline)
```
AUTO CONT%-2 = (ALGO + BGT CONT%) × listing   if is_old
               0                               otherwise
```

**8. `OLD ST CONT%`**
```
auto2_maj    = SUM(AUTO CONT%-2) within (ST_CD, MAJ_CAT)
OLD ST CONT% = AUTO CONT%-2 / auto2_maj   (0 if auto2_maj == 0)
```

**NEW-store pipeline** (internal — columns AR..BC in the Excel — not surfaced):

```
# Peer reference: among peer stores where
#   (peer.MAJ_CAT, peer.RNG_SEG, peer.REF_GRP_OLD) == (my.MAJ_CAT, my.RNG_SEG, my.REF_GRP_NEW)
#   AND peer.OLD ST CONT% > 0
# new_ref = average of those peer.OLD ST CONT%; 0 for OLD stores.

new_ref_maj = SUM(new_ref) within (ST_CD, MAJ_CAT)
algo_cont   = new_ref / new_ref_maj          (= AR — "ALGO CONT%")

# NEW AUTO-1 (AS):
new_auto1_old1 = max(ssn2, 0)  if is_w_pw
                 max(l30d, 0)  if is_sao
                 0             otherwise
new_auto1_new  = max(l7d, l30d)
new_auto1 = 0                       if is_old
            new_auto1_old1          if is_old1
            new_auto1_new           if is_new
            val_default             otherwise
new_auto1 *= listing

# NEW AUTO-2 (AT):
new_auto2 = (new_auto1 if new_auto1 > 0 else algo_cont × 0.5) × listing

# RMN AUTO new (AU):
rmn_new   = max(new_auto2 − BGT CONT%, 0)        (0 for OLD)

# ALGO new (AW):
rmn_new_maj   = SUM(rmn_new) within (ST_CD, MAJ_CAT)
rmn_new_share = rmn_new / rmn_new_maj            (0 if denom 0)
algo_new      = rmn_new_share × max(1 − bgt_maj, 0)   (0 for OLD)

# AUTO CONT%-2 new (AX):
auto2_new = (algo_new + BGT CONT%) × listing      (0 for OLD)

# NEW ST CONT% (AY):
auto2_new_maj = SUM(auto2_new) within (ST_CD, MAJ_CAT)
ay = auto2_new / auto2_new_maj                    (0 if denom 0)

# AZ:
az = 0           if is_old
     algo_cont   if is_upc
     ay          if ay > 0
     algo_cont   otherwise

# ALGO COINT% (BA):
az_maj = SUM(az) within (ST_CD, MAJ_CAT)
ba     = az / az_maj                              (0 if denom 0)

# BB:
bb = (BA if BA > 0 else NAT CONT%) × listing      (0 for OLD)

# NEW ST CONT% (BC):
bb_maj = SUM(bb) within (ST_CD, MAJ_CAT)
bc     = bb / bb_maj                              (0 if denom 0)
```

**9. `INT ST CONT%` (BD)** — OLD path vs NEW path
```
INT ST CONT% = OLD ST CONT%   if is_old
               bc             otherwise
```

**10. `INT-2 ST CONT%` (BE)** — 1% threshold
```
INT-2 ST CONT% = 0             if INT ST CONT% < 0.01
                 INT ST CONT%  otherwise
```

**11. `FINAL ST CONT%` (BF)** — normalised so each (ST_CD, MAJ_CAT) sums to 1
```
int2_maj      = SUM(INT-2 ST CONT%) within (ST_CD, MAJ_CAT)
FINAL ST CONT% = INT-2 ST CONT% / int2_maj    (0 if int2_maj == 0)
```

After this stage, `BGT CONT% (FINAL)` is dropped from the store frame
(replaced by `NAT CONT%`).

---

## 10. Save & download

Output table names:

```
Store-level:   Cont_Percentage_<safe_GC>_<YYYY_MM>
Company-level: Cont_Percentage_<safe_GC>_CO_<YYYY_MM>
```

`safe_GC = grouping_column.upper().replace(' ','_').replace('-','_')`.

Save path:
1. The compute thread always writes a pickle to
   `<tempdir>/contrib_jobs/<job_id>_{store,company}.pkl` (used for fast
   download).
2. If `save_to_db=true`, `_save_to_db` drops and recreates the table, then
   inserts via `pyodbc.fast_executemany` in 50k-row batches, with up to 3
   retries on connection-drop errors (10054, 08S01, …).
3. After 30 minutes the job is auto-deleted: pickle files removed, row in
   `Cont_jobs` deleted. Download still works after that via the DB table.

CSV split rules for downloads larger than 800 000 rows:
- SEG present AND DIV present → split by SEG, and within each non-GM SEG by
  DIV (GM gets one file).
- only DIV → split by DIV.
- otherwise → one CSV chunked at 800 000 rows.

---

## 11. Endpoint reference

| Method | Path | Purpose |
|---|---|---|
| GET | `/contrib/config/grouping-columns` | Allowed grouping columns present in `VW_MASTER_PRODUCT` |
| GET | `/contrib/config/ssn-values` | DISTINCT SSN values |
| GET | `/contrib/config/months` | DISTINCT `STOCK_DATE` for L18M (cached 5 min) |
| GET | `/contrib/config/majcats?grouping_column=` | MAJ_CATs in `Master_HIER_<GC>` where SEG ∈ {APP, GM} |
| GET / POST / DELETE / PUT-reorder | `/contrib/presets[…]` | Preset CRUD + reorder |
| GET / POST / DELETE | `/contrib/mappings[…]` | Mapping CRUD |
| GET / POST / DELETE | `/contrib/assignments[…]` | Assignment CRUD |
| POST | `/contrib/assignments/{id}/activate` | Make one assignment the active one (radio) |
| POST | `/contrib/assignments/clear-active` | Clear all `is_active` (legacy "all run" mode) |
| POST | `/contrib/execute` | Queue a pipeline job |
| GET | `/contrib/jobs` | List jobs (summary) |
| GET | `/contrib/jobs/{id}` | Full job with preview (≤200 rows) |
| POST | `/contrib/jobs/{id}/cancel` / `pause` / `resume` | Job control |
| GET | `/contrib/jobs/{id}/download/{store\|company}` | Download pickle or DB-backed CSV/ZIP |
| DELETE | `/contrib/jobs/{id}` | Delete job + temp files + DB row |
| GET | `/contrib/review/tables` | List `Cont_Percentage_*` tables |
| GET | `/contrib/review/preview/{table}?f_COL=v1,v2…&limit=` | Server-side-filtered preview |
| POST | `/contrib/review/export/{table}` | Start an export job |
| GET | `/contrib/review/download/{table}` | Download whole table |
| DELETE | `/contrib/review/tables/{table}` | Drop result table |

---

## 12. Glossary of derived columns

| Column | Where computed | Meaning |
|---|---|---|
| `0001_STK_Q / _V` | `_compute_kpis` | Average of OP/CL stock, both-zero-safe |
| `FIX` | `_compute_kpis` | Display fixtures (stock-Q / avg-density) |
| `DISP_AREA` | `_compute_kpis` | APF × FIX, floor 1 when there are sales |
| `GM_%` | `_compute_kpis` | Gross-margin % of value |
| `STR` | `_compute_kpis` | Stock-to-revenue (in days-equivalent) |
| `SALES PSF / GM PSF` | `_compute_kpis` | Productivity per square foot |
| `SALE_PSF_MJ / GM_PSF_MJ` | `_compute_kpis` | MAJCAT mean of the above |
| `SALES_PSF_ACH% / GM_PSF_ACH%` | `_compute_kpis` | Row's productivity vs MAJCAT mean |
| `STOCK_CONT% / SALE_CONT%` | `_compute_kpis` | Row's share within (ST_CD, MAJ_CAT) |
| `ALGO` | `_compute_kpis` | Raw algorithmic contribution (boost + GM adj) |
| `INITIAL AUTO CONT%` | `_compute_kpis` | Normalised ALGO → row's auto contribution share |
| `<col>\|<preset>` | `_combine_dataframes` | Same column from preset `<preset>` |
| `AUTO CONT%` (or assignment-named) | `_apply_mapping_assignments` | Best preset's INITIAL AUTO CONT% per SSN bucket |
| `AUTO CONT% 2` | `_apply_auto_cont_derivations` | Above, gated by ACT_INACT and ≥ 0.01 |
| `AUTO CONT% (FINAL)` | `_apply_auto_cont_derivations` | Normalised within (RDC_CD, MAJ_CAT) |
| `BGT CONT% (FINAL)` | `_apply_auto_cont_derivations` | MERCH_INPUT when its group sums > 0, else AUTO CONT% (FINAL) |
| `NAT CONT%` | `_apply_store_contribution_chain` | Renamed company `BGT CONT% (FINAL)` |
| `NAT CONT% @ MAJ` | store chain | `SUM(NAT CONT%)` per (ST_CD, MAJ_CAT) |
| `BGT CONT%` | store chain | 50% NAT for OLD stores, else 70% NAT |
| `AUTO CONT%-1 / -2` | store chain | OLD-store auto contribution before/after ALGO |
| `RMN AUTO` / `…@ MAJCAT` | store chain | Excess of AUTO over BGT |
| `ALGO` (store chain) | store chain | Share of remainder × residual budget |
| `OLD ST CONT%` | store chain | OLD-store normalised contribution |
| `INT ST CONT%` | store chain | OLD path or NEW path (BC) |
| `INT-2 ST CONT%` | store chain | INT with values < 1% zeroed |
| `FINAL ST CONT%` | store chain | INT-2 normalised per (ST_CD, MAJ_CAT) |

---

## 13. Operating notes

- Presets run in `sequence_order`; reorder via `PUT /contrib/presets/reorder`.
  The `L30D` preset is auto-seeded with `sequence_order = 0` so it always
  exists.
- `_read_sql_nolock` uses `READ UNCOMMITTED` and retries twice on
  10054/08S01/communication-link errors with a connection-pool reset.
- The CROSS JOIN result (`df_master`) is cached and reused across every
  preset in a job — biggest single optimisation in the pipeline.
- Numerics carried through pandas as `float32`; final DB columns are `FLOAT`
  to dodge SQL `22003` overflows.
- `_apply_store_contribution_chain` is **skipped** when no company source is
  available — this is intentional so the store table never carries V-0015
  columns that weren't computed against a real merchant decision.
