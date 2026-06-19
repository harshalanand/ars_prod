# Grid Builder — rules & details

## Source files
- `backend/app/services/grid_calculations.py`
- `backend/app/api/v1/endpoints/grid_builder.py`

## Grid taxonomy
- **Primary grid**: `MJ_RNG_SEG` — at `MAJ_CAT` × `RNG_SEG` (MRP tier).
- **Sec-cap grids**: `MJ_FAB`, `MJ_MICRO_MVGR` (and similar `MJ_<dim>` variants).
- `RNG_SEG` values: `E` / `V` / `P` / `SP` (MRP tier).

## Required dimensions on every grid row (for sec-cap propagation)
`FAB`, `MACRO_MVGR`, `MICRO_MVGR`, `M_VND_CD`, `RNG_SEG`.
**If any are dropped between listing → listed → alloc, sec-cap silently loses grids** (invariant 4).

## Growth % application
- Applied at `MAJ_CAT` + grid level only.
- **Never per OPT_TYPE** (invariant 2). Holds for main and fallback paths.

## Invariants relevant here
- Growth at MJ+grid only (invariant 2)
- Sec-cap grid extras must propagate (invariant 4)
- RNG_SEG = MRP tier (invariant 6)

## Recorded rules
<!-- ars_flow appends dated bullets below. One rule per line. -->

- **2026-06-13** — `_ensure_hierarchy_table` is **ADD-ONLY**. Deleting a grid or moving it to Inactive does **not** drop the corresponding column from `ARS_GRID_HIERARCHY`; data survives. Physical column order is no longer reshuffled by reorder/deactivate (consumers reference columns by name, not ordinal). Why: a single accidental Delete used to destroy a column and all its values; recovery required re-creating the grid AND backfilling. How to apply: when reviewing CRUD code paths in `grid_builder.py`, do not reintroduce the old DROP/REBUILD logic; if reorder needs to update physical order it must do so without dropping anything.
- **2026-06-13** — `POST /grid-builder/hierarchy/compact?dry_run=true|false` is the only sanctioned way to drop orphan columns from `ARS_GRID_HIERARCHY`. Default is `dry_run=true` (reports `kept` / `orphans` without altering). Pass `dry_run=false` to execute. Includes `MERGE_<X>` orphans (when parent `X` is gone). Why: makes the destructive cleanup an explicit admin act rather than a side-effect of a grid CRUD click. How to apply: never auto-call this from CRUD; surface it as an explicit operator action.
- **2026-06-13** — Routine `DBCC SHRINKFILE` after Run-All was **removed** from `_do_run_all_background`; the `_shrink_db_files` helper was deleted. Why: routine shrink is a SQL Server anti-pattern (causes index fragmentation, file just regrows next run). How to apply: don't reintroduce SHRINKFILE in any hot path. Size the log file once in SQL config; if a one-off space crunch hits, run shrink manually in a maintenance window.
- **2026-06-13** — Stage S5 `_insert_missing_msa_rows` is now **skipped when `grid.pivot_only=1`**. Why: `ARS_MSA_TOTAL` is at MAJ_CAT grain, so for article-grain pivot tables (`ARS_GRID_MJ_GEN_ART`, `ARS_GRID_MJ_VAR_ART`) it could only produce `(GEN_ART_NUMBER='NA', CLR='NA', ARTICLE_NUMBER='NA')` placeholder rows that pollute downstream consumers. How to apply: non-pivot grids (Primary + Secondary) still get synthetic injection. If an article-grain grid is later marked `pivot_only=0`, S5 will run again — confirm that's intended before flipping the flag.
- **2026-06-13** — `bootstrap_msa_pend_sync` at the end of Run-All is **kept** (safety net to re-seed `PEND_QTY`/`FNL_Q` in `ARS_MSA_TOTAL/VAR_ART/GEN_ART` from current `ARS_PEND_ALC` even if the incremental delta-rollup missed something). Why: cheap UPDATE on 3 tables, prevents drift after a heavy rebuild. How to apply: keep this call; do not gate it behind a flag.
