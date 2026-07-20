# Merge Rules — ARS Manual

## BRD — Why this exists

Range/MRP tiers (`RNG_SEG` ∈ E / V / P / SP) and other hierarchy dimensions are often **finer than the budget needs to be**. If a store's budget for value-tier (`V`) options is computed strictly at the `V` grain and comes up just short, a perfectly good economy-tier (`E`) option can be skipped even though the two tiers are commercially interchangeable. Merge Rules fix this: they collapse near-equal source values into a shared merged bucket (e.g. `E` and `V` → `EV`), so the grid budget is pooled across the merged tiers and good options stop getting starved.

Category / range planners own the rule set through a CRUD screen (bulk upload supported). Each rule is a flat mapping `(source_col, source_value) → target_value` plus an aggregation function. The system derives a merged master table from the parent contribution master automatically whenever rules change, so downstream grid math reads the merged grain with zero code changes.

Business outcome: budgets are allocated at the grain the business actually merchandises at, not the raw dimension grain — fewer artificially skipped options, cleaner grid coverage. Pipeline position: **Grid Builder / Merge Rules configuration → LISTING build (grids join the merged masters) → allocation.** Merge Rules is a configuration + derived-master layer that sits upstream of the listing grid joins.

## FSD — How it works

`ARS_MERGE_RULES` holds the flat rule set. Any write (create / update / delete / bulk) validates, upserts, then refreshes the affected `Master_CONT_MERGE_<col>` derived table and re-derives the `MERGE_<col>` columns in `ARS_GRID_HIERARCHY`. The grid pivot materializes `MERGE_<col>` via a generated `CASE` expression from the same rules.

### Inputs & outputs

| Direction | Table | Grain |
|---|---|---|
| Consumed / produced | `ARS_MERGE_RULES` | one row per `(source_col, source_value)` — UNIQUE |
| Consumed | `Master_CONT_<col>` (parent) | `ST_CD × MAJ_CAT × <col>` with `CONT` (contribution) |
| Produced | `Master_CONT_MERGE_<col>` (derived) | `ST_CD × MAJ_CAT × MERGE_<col>` with aggregated `CONT` |
| Produced (side-effect) | `ARS_GRID_HIERARCHY.MERGE_<col>` | re-derived via `_populate_merge_columns` |

`ARS_MERGE_RULES` shape: `rule_id`, `source_col`, `source_value`, `target_value`, `agg` (`SUM`/`AVG`/`MAX`/`MIN`, default `SUM`), `active`, `created_at`, `modified_at`, `modified_by`. UNIQUE constraint `UQ_ARS_MERGE_RULES (source_col, source_value)`; filtered index on `source_col WHERE active=1`.

Derived table shape: `ST_CD NVARCHAR(50)`, `MAJ_CAT NVARCHAR(100)`, `MERGE_<col> NVARCHAR(200)`, `CONT FLOAT`, `derived_at DATETIME`. PK `(ST_CD, MAJ_CAT, MERGE_<col>)`.

### Rules & invariants

- **One row per source value.** UNIQUE `(source_col, source_value)`; writes upsert on that key. A source value absent from the rules passes through the `ELSE` branch unchanged (identity merge).
- **One agg per source_col.** All active rows for a `source_col` MUST agree on `agg`. Enforced at create (`create_rule`), update, and bulk (both within-batch and against existing active rows). `get_agg` falls back to `SUM` and logs a warning if it ever finds mixed values.
- **`source_col` is the parent side, never the derived side.** A `source_col` starting with `MERGE_` is rejected (400) — you map `RNG_SEG`, not `MERGE_RNG_SEG`.
- **RNG_SEG = MRP tier (inv 6).** The canonical merge is near-equal MRP tiers, e.g. `E→EV, V→EV` so economy+value share one budget bucket. `RNG_SEG` ∈ {E, V, P, SP}.
- **Derivation never alters an existing derived table** — it TRUNCATE+INSERTs. The table is created (with schema above) only if missing.
- **Best-effort refresh.** Derived-master and `ARS_GRID_HIERARCHY` refreshes are wrapped so a failure logs a warning but never fails the rule CRUD API call.
- **Merging pools budget, it does not create it.** The derived `CONT` is the aggregate of the parent `CONT` over the merged bucket; a `SUM` agg must reconcile against the parent total (see gate below).

### Formulas

Merge CASE (materialized both in the grid pivot via `build_case_expr` and in the derived-table INSERT):
```
MERGE_<col> = CASE [<col>]
                WHEN 'E' THEN 'EV'
                WHEN 'V' THEN 'EV'
                ... (one WHEN per active rule) ...
              ELSE [<col>]            -- unmapped values pass through unchanged
              END
```

Derived-master refresh (`refresh_derived_for_source_col`, single txn TRUNCATE + INSERT):
```
INSERT Master_CONT_MERGE_<col> (ST_CD, MAJ_CAT, MERGE_<col>, CONT, derived_at)
SELECT ST_CD, MAJ_CAT,
       <MERGE_CASE> AS MERGE_<col>,
       <agg>(TRY_CAST(CONT AS FLOAT)) AS CONT,      -- agg = SUM|AVG|MAX|MIN
       GETDATE()
FROM   Master_CONT_<col>
WHERE  ST_CD IS NOT NULL AND MAJ_CAT IS NOT NULL AND <col> IS NOT NULL
GROUP BY ST_CD, MAJ_CAT, <MERGE_CASE>
```

### Validation gates — validate first, then create

1. **Validate the rule payload before writing.** `source_col` non-empty and not `MERGE_*`; `agg` ∈ {SUM, AVG, MAX, MIN}; `source_value` and `target_value` required. Bulk preflight validates *every* row (and rejects within-batch duplicates on `(source_col, source_value)`) before opening a connection — atomic all-or-nothing.
2. **Validate agg consistency before committing.** Reject if the incoming agg conflicts with any existing active row's agg for that `source_col` (single-rule and bulk both check). Guarantees the derived `<agg>(CONT)` is well-defined.
3. **Validate the parent master before deriving.** `refresh_derived_for_source_col` skips (not errors) when `Master_CONT_<col>` is absent or there are no active rules; errors when the parent is missing any of `{ST_CD, MAJ_CAT, <col>, CONT}`.
4. **Reconcile CONT total after deriving (SUM only).** After INSERT, compare parent `SUM(CONT)` vs derived `SUM(CONT)`; drift > 0.01 emits a warning — the usual cause is a `source_value` missing from the rules that slipped through the `ELSE` branch. Meaningful only when `agg=SUM`.
5. **Refresh grid hierarchy after every rule change.** `_refresh_grid_hierarchy_merge_cols` re-derives the `MERGE_<col>` columns in `ARS_GRID_HIERARCHY` so the next listing build joins the up-to-date merged grain (best-effort).

### Key columns

| Column | Meaning | Formula / source |
|---|---|---|
| `source_col` | Parent dimension being merged (e.g. `RNG_SEG`) | user input; never `MERGE_*` |
| `source_value` | Raw dimension value (e.g. `E`) | user input; unique with `source_col` |
| `target_value` | Merged bucket (e.g. `EV`) | user input → `MERGE_<col>` |
| `agg` | Aggregation of parent `CONT` | one per `source_col`; SUM default |
| `active` | Rule participates in derivation | BIT; filtered index |
| `MERGE_<col>` | Derived merged dimension | `CASE` from active rules, ELSE passthrough |
| `CONT` (derived) | Merged contribution | `<agg>(TRY_CAST(parent.CONT))` grouped by merged key |
| `derived_at` | Last derivation timestamp | `GETDATE()` at refresh |

## Recorded rules
<!-- dated appendable bullets -->
- 2026-07-17 (from live DB) — Live `ARS_MERGE_RULES` = 4 active rows on `source_col=RNG_SEG`, `agg=SUM`: `E→EV`, `V→EV`, `P→PSP`, `SP→PSP`. So `MERGE_RNG_SEG` ∈ {EV, PSP}. The grid `MJ_MERGE_RNG_SEG` (Primary) consumes the merged buckets via `Master_CONT_MERGE_RNG_SEG`; raw `MJ_RNG_SEG` still exists as a Secondary sec-cap grid. Do NOT confuse this dimension-merge feature with the rule-engine OPT_TYPE classification (RL/TBC/TBL) — different subsystem (`rule_engine_per_opt.py`).
- 2026-07-09 — Manual dossier created from source. CRUD + agg-conflict guards in `merge_rules.py` (`create_rule` line 118, bulk preflight line 287); derivation + CONT reconciliation in `derived_masters.refresh_derived_for_source_col` (line 216); grid-pivot CASE in `build_case_expr` (line 160). `MERGE_<col>` grid-hierarchy refresh via `grid_builder._populate_merge_columns`.
