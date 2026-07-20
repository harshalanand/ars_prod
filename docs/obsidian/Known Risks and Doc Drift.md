---
title: Known Risks and Doc Drift
tags: [ars, risks, drift, maintenance]
updated: 2026-07-17
---

# Known Risks and Doc Drift

Findings from the code read (2026-07-17). Two lists: **correctness risks** (behaviour worth watching) and **doc drift** (stale docs/comments to fix).

## Correctness risks (open)
| # | Risk | Where |
|---|------|-------|
| 1 | `ARS_PEND_ALC` has PK on `ID` only — no unique on the logical grain; `write_manual_pend_alc` can double-count | [[Pending Allocation and Hold]] |
| 2 | Manual pend upload has weak validation — blank `rdc`/orphan articles become MSA orphans | `write_manual_pend_alc` |
| 3 | `apply_do_deductions` FIFO partition **excludes `ALLOC_MODE`** — a TBC DO can settle an RL row and silently re-categorise | `pend_alc_service.py` |
| 4 | Blank `ST_CD` in adhoc close / `do_qty=0` cancel / blank `SZ` in clear-revise = **wildcard scope** (guarded in API, not the service) | pend/hold service |
| 5 | Manual MSA rebuild is **not blocked** while a listing run is in progress — an in-flight rebuild can empty `ARS_MSA_TOTAL` mid-listing | [[MSA Stock Calculation]] |
| 6 | Listing MIX(b) tags supplied OPTs as MIX on colour-fill alone (candidate to split into a `POOR_COLOR_FILL` flag) | [[Listing]] |
| 7 | Bootstrap PEND/HOLD re-sync runs on the **synchronous** MSA store path but **not** the async job worker — auto-store vs manual-save can differ | `msa_job_service` |
| 8 | `SEC_CAP_MBQ_ZERO` hard-block (veto path) contradicts invariant 3 ("MBQ=0 = no constraint", honoured by the breach path). Deliberate 2026-06-30 change — confirm intended | [[Secondary-Grid Cap]] |
| 9 | Backup endpoint references undefined `settings.SQL_DATABASE`/`DATA_DATABASE` → `AttributeError` (broken path) | `settings.py` |
| 10 | Superadmin password is re-synced from env + force-unlocked on **every** startup — can't be changed via UI | `auth_service` |
| 11 | RLS `get_category_sql_filter` uses string interpolation, not bound params (injection surface if category values ever attacker-controlled) | `dependencies.py` |
| 12 | Hardcoded default secrets in `config.py` (`DB_PASSWORD`, `SUPER_ADMIN_PASSWORD`, placeholder `JWT_SECRET_KEY`) — must override in prod | `config.py` |
| 13 | `_check_adhoc_close_revert` can leave TWO OPEN BDC history rows (cancelled→regenerated→reverted) → `_NO_OPEN_BDC_PREDICATE` sees both, silently emits nothing | pend service |
| 14 | Report-scheduler `REPORT_SCHED_INTERVAL_SEC`/`REPORT_MAX_PARALLEL` keys not defined in `config.py` — always fall back to 30s/2 | `report_scheduler_service` |
| 15 | Mixed time bases: report scheduler uses UTC (`SYSUTCDATETIME`), some tables use local `GETDATE()` | codebase-wide |
| 16 | **MAJ_CAT `[A-Z]-` prefix inconsistency** — grid/MSA/listing carry some categories prefixed (`B-M_TEES_HS`), alloc/hold never do; prefixed+unprefixed coexist as distinct grid rows. Raw-MAJ_CAT joins silently drop ~0.4% of grid rows (found via the Grid Report — [[Reports]]). Detect with `LIKE '[A-Z]-%'`, strip `SUBSTRING(x,3,...)` — but only normalize deliberately (merging double-represents RDC totals). | grid/MSA/listing vs alloc/hold |

## Doc drift (stale — code is right)
| Doc | Says | Reality |
|-----|------|---------|
| `manual/grid.md` body | "MJ_RNG_SEG is the Primary grid" | Live Primary = `MJ` + `MJ_MERGE_RNG_SEG`; `MJ_RNG_SEG` is Secondary/sec-cap + the sole `use_for_opt_sale`. (Correction bullet appended to the dossier `## Recorded rules`.) |
| `msa_service.calculate()` docstring | 9-step flow | Actual flow is **12 steps** (adds universe backfill, ATT_TYP gate, row-per-type). |
| `rule_engine_per_opt.py` module docstring | "Activated by env var `ARS_PER_OPT_MODE=1`, defaults OFF" | per_opt is the ONLY engine, always on; env switch removed. |
| `docs/2026-07-02-cont-fallback-ladder.md` | lists 4 engines calling `_stage_b_fill_cont` | Only `rule_engine_pandas` + `rule_engine_new` survive; the other 2 are deleted. |
| `manual/listing.md` + KB | cite `listing_allocator.py:16` for E1–E7 | `listing_allocator.py` is **DELETED**; E1–E7 now in `rule_engine_per_opt.py`. |
| `parked_history.py` module docstring | 6 snapshot/promote tables | Only **4** targets configured (alloc, listing_working, listing, msa_total); MSA GEN_ART/VAR_ART not parked. |
| `ars_flow_kb/hold.md` | hold PK is 3-part `(WERKS,VAR_ART,SZ)` | Now **4-part** incl. `ALLOC_TYPE`. |
| KB "sec-cap examples" | `MJ_FAB`/`MJ_MICRO_MVGR` | Not currently `sec_cap_applicable`; live sec-cap grids = `MJ_RNG_SEG`/`MJ_FIT`/`MJ_M_YARN_02`. |
| KB "sec-cap dims (5)" | FAB/MACRO/MICRO/M_VND/RNG_SEG | Live set is broader: also MERGE_RNG_SEG, FIT, M_YARN_02, WEAVE_2, CLR. |
| `msa_result_storage.py:21` | "Main DB session, not Data DB" | Job worker passes `DataSessionLocal()`; all MSA tables are in Rep_Data. |

## Maintenance convention
Per `CLAUDE.md`: when engine logic changes, **update the matching `frontend/public/docs/manual/<module>.md` dossier first** (source of truth), then `docs/ARS_BRD_END_TO_END.md`, `docs/RULE_MASTER.md`, `ARS_DATA_DICTIONARY`, and the `.claude/agents/ars_flow_kb/` extracts. This vault is a derived companion — reconcile it too.

## Cross-links
[[ARS Work Log]] · every stage note.
