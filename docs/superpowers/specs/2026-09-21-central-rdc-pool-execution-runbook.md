# Central RDC Pool — End-to-End Execution Runbook

**Version:** 1.0
**Date:** 2026-09-21
**Status:** pre-implementation — no code written
**Reads with:** `2026-09-21-central-rdc-pool-implementation-plan.md` (the *what*)
**This document is the *how* and the *in what order*.** Every stage has an exit criterion and a
rollback. Nothing in a later stage may start until the previous stage's exit criterion is green.

---

## 0. Correction to the implementation plan

> **§1.3 of the plan says: `ALTER TABLE ARS_ALLOC_WORKING ADD SRC_RDC`. That is wrong.**
>
> `ARS_ALLOC_WORKING` is **dropped and recreated by `SELECT … INTO` on every run**
> (`rule_engine_new.py:829-867`). An `ALTER TABLE` would be wiped by the next Generate.
>
> `SRC_RDC` and `SRC_SPLIT_CNT` must be added **inside that SELECT list**, exactly like the
> existing `CAST(NULL AS NVARCHAR(20)) AS ALLOC_WAVE`:
>
> ```sql
> CAST(NULL AS NVARCHAR(20)) AS SRC_RDC,
> CAST(NULL AS INT)          AS SRC_SPLIT_CNT,
> ```
>
> `ARS_ALLOC_PARKED` / `ARS_ALLOC_HISTORY` still pick both up through the column reconcile —
> that part of the plan holds.

---

## 1. The flag must be resolved ONCE per run

`business_rules.rule_flag()` reads a module-level cache with a **30-second TTL**
(`business_rules.py:280`). A long Generate spans many cache refreshes. If Part 3.55 and
Part 8.37 each call `rule_flag('ALC_RDC_CENTRAL_POOL')` independently, an admin toggling the
switch mid-run produces a **half-clubbed, half-split run** — the worst possible state.

**Rule: resolve once at run start, pass it down, never re-read.**

```python
# listing.py, immediately after the session RUNNING row is inserted (~:697)
_CENTRAL_POOL = (req.rdc_mode == "all"
                 and rule_flag("ALC_RDC_CENTRAL_POOL", default=False))
```

Stamp it into `ARS_RUN_PARAMS_AUDIT` alongside `rdc_mode` (`listing.py:962`) so every run
records which engine it used. Thread it into `rule_engine_pandas` → `rule_engine_new` →
`rule_engine_per_opt` as an explicit argument. **No site below calls `rule_flag` again.**

---

# STAGE 0 — PRE-FLIGHT (no code)

Everything here must be done **before** the first line is written, because two of the items
cannot be recreated afterwards.

| # | Task | Why | Owner |
|---|---|---|---|
| 0.1 | **Capture the golden baseline.** Run one full `Own` session on production data. Park it, approve it. Record `SESSION_ID`. Export all four tables to `docs/baselines/<sid>/`: `ARS_ALLOC_WORKING`, `ARS_ALLOC_HISTORY`, `ARS_PEND_ALC`, `ARS_NL_TBL_HOLD_TRACKING` | **V5 is impossible without it.** Once code changes, the "before" state is gone | Dev |
| 0.2 | Capture the same for one `Cross` session | V13 | Dev |
| 0.3 | Quantify defect D-1: count `ARS_LISTING` rows in the last `All RDCs` session whose `MSA_FNL_Q` equals the *other* RDC's figure | Evidence for management that the problem is real | Dev |
| 0.4 | **Decide O1** — can picking execute a two-source line for one store-size? | If no, default becomes `SINGLE_STRICT` and the whole split-walk is dead code | Supply chain |
| 0.5 | **Decide O2** — confirm BR-RDC-11 (holds never split, short hold is reduced) | Avoids a production PK migration | Supply chain |
| 0.6 | **Decide O5** — seed order for the two `'*'` rows in `ARS_RDC_FALLBACK` | Needed before Phase 1 test | Business owner |
| 0.7 | Confirm `allow_multi_parked` is **OFF** in production today | See Gap G3 below | Dev |
| 0.8 | Create branch `feat/central-rdc-pool` off `ars_v3` | | Dev |

**Exit criterion:** baselines exported and checksummed; O1, O2, O5 answered in writing.

**Rollback:** n/a — nothing changed.

---

# STAGE 1 — FOUNDATION (deployable alone, zero behaviour change)

This stage creates tables and a settings screen. It touches **no allocation code path**. It can
sit in production for weeks with no effect.

### 1.1 Database

| # | Task | File |
|---|---|---|
| 1.1.1 | `ensure_rdc_fallback_tables(conn)` — `ARS_RDC_FALLBACK` + `_LOG` DDL per plan §1.1 | new `app/services/rdc_fallback_service.py` |
| 1.1.2 | Seed the two `'*'` rows from O5 | same |
| 1.1.3 | `ensure_alloc_rdc_split_table(conn)` — DDL + 2 indexes per plan §1.2 | new `app/services/rdc_split_service.py` |
| 1.1.4 | Seed 5 business rules; `ALC_RDC_CENTRAL_POOL` **inactive** | `business_rules.py` `_SEED` list |

Idempotent inline DDL, matching `pend_alc_service.ensure_pend_alc_table`. No Alembic —
`backend/scripts/migrations/` holds only ad-hoc SQL in this project.

### 1.2 API

| # | Task | File |
|---|---|---|
| 1.2.1 | `GET /api/v1/rdc-fallback` · `PUT /{own_rdc}` · `GET /log` · `POST /reset` | new `app/api/v1/endpoints/rdc_fallback.py` |
| 1.2.2 | Register router | `app/api/v1/router.py` (~:66, ~:109) |
| 1.2.3 | `preference_order(store_rdc, live_rdcs)` resolver + unit tests | `rdc_fallback_service.py` |

SUPER_ADMIN gate, matching `business_rules.py`.

### 1.3 Frontend

| # | Task | File |
|---|---|---|
| 1.3.1 | `RdcFallbackPage.jsx` — chip rows, `✱ untagged` row, 2-RDC notice, change history (plan §4.3) | new |
| 1.3.2 | Lazy import | `App.jsx:81` |
| 1.3.3 | Route `settings/rdc-fallback`, `superadminOnly` | `App.jsx:262` |
| 1.3.4 | Sidebar entry below Business Rules | `Sidebar.jsx:167` |

**Exit criterion:** an admin can open Settings → RDC Fallback, reorder chips, save, and see the
change in the history table. A full `Own` Generate run produces output **identical to the 0.1
baseline** (it must — nothing in the run path changed).

**Rollback:** remove the route + sidebar entry. The two tables are inert; leave them.

---

# STAGE 2 — ENGINE

First stage that can change allocation output. Guarded by `_CENTRAL_POOL` throughout.

| # | Task | Site |
|---|---|---|
| 2.1 | Resolve `_CENTRAL_POOL` once; stamp to run-params audit | `listing.py` ~:697, :962 |
| 2.2 | Thread the flag through the orchestrator | `rule_engine_pandas.py` → `_new` → `_per_opt` |
| 2.3 | Club `MSA_FNL_Q` — gate `_rdc_select`, `_rdc_group`, `_rdc_join` **together** | `listing.py:1630-1654` |
| 2.4 | Club `VAR_COUNT` / `VAR_FNL_COUNT` — same three fragments | `listing.py:1662-1700` |
| 2.5 | Add `SRC_RDC` / `SRC_SPLIT_CNT` to the `SELECT … INTO` list (**§0 correction**) | `rule_engine_new.py:829-867` |
| 2.6 | Drop `L.RDC = V.RDC` from the pool-build join when clubbing | `rule_engine_new.py:873-874` |
| 2.7 | `POOL_KEYS` 6-key → 5-key, passed in not hard-coded | `rule_engine_per_opt.py:63` |
| 2.8 | **NEW Part 8.37** — split pass per plan §3 | `listing.py` between :3423 and :3425 |
| 2.9 | Part 8.37 starts with `DELETE FROM ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID = :sid` | idempotent re-run (Gap G4) |
| 2.10 | Part 8.55 hold release cascades to split rows (working + parked) | `listing.py:3556` |

### Parallelism — no new contention

Allocation runs `parallel_workers` threads, one per MAJ_CAT. `POOL_KEYS` **still contains
`MAJ_CAT`** after clubbing, so two workers can never touch the same pool row. Clubbing removes
the RDC dimension only. **No locking change is required.**

The split pass is the opposite: its ledger is global. **Part 8.37 runs once, single-threaded,
after every MAJ_CAT worker has finished.**

**Exit criterion:** V3, V4, V5, V12, V13 green. `Own` output byte-identical to baseline 0.1 with
the switch both ON and OFF.

**Rollback:** set `ALC_RDC_CENTRAL_POOL` inactive. Every gate collapses to today's path.

---

# STAGE 3 — DOWNSTREAM 🔴

**Without this stage the feature is actively harmful**: the allocation is correct and the
shipment is wrong. The BDC file names the store's own warehouse regardless of where the stock
was actually taken from.

| # | Task | Site |
|---|---|---|
| 3.1 | `rdc_expr = ISNULL(SPL.SRC_RDC, ISNULL(M.[RDC], H.[WERKS]))` + LEFT JOIN to the split table | `pend_alc_service.py:2304` |
| 3.2 | `COALESCE(NULLIF(H.[RDC],''), S.[RDC])` in `_load_open_holds` | `msa_service.py:94` |
| 3.3 | Same in `_load_typed_hold` | `msa_service.py:211` |
| 3.4 | Approve-time hold stamp prefers `SRC_RDC` over `MAX(SM.[RDC])` | `parked_history.py:~1620` |
| 3.5 | Split-table lifecycle: reject delete, revert delete, TTL purge, `_AFFECTED_TABLES` | `parked_history.py:98` + reject/revert/purge |
| 3.6 | **Consumer audit of `ARS_PEND_ALC.RDC`** — see Gap G2 | 11 endpoints + 6 stored procs |

Every expression is `ISNULL`/`COALESCE`-shaped with today's value as the fallback, so Own and
Cross are inert **by inspection**, not only by test.

**Exit criterion:** V14 (cross-ship pend lands on `SRC_RDC`, BDC names that warehouse) and V15
(cross-RDC hold deducts from the sourcing RDC next MSA run) green. G2 audit signed off.

**Rollback:** switch off. With no split rows the `ISNULL` chains return today's values.

---

# STAGE 4 — UI & REPORTS

| # | Task | Site |
|---|---|---|
| 4.1 | `RDC Sourcing` panel — policy radios, max-split, central-pool badge | `ListingPage.jsx:2797` |
| 4.2 | Third payload branch `else if (rdcMode === 'all')` — Own/Cross branches untouched | `ListingPage.jsx:1489` |
| 4.3 | Untagged-store advisory banner, **all three modes** | `ListingPage.jsx:2797` |
| 4.4 | Post-run `RDC Split` results block | `ListingPage.jsx` |
| 4.5 | Cockpit summary — **add** pick-by-`SRC_RDC`, **keep** demand-by-store-RDC | `listing.py:5113` |
| 4.6 | Four reports per plan §2.8 | `reports.py` |
| 4.7 | **Alloc Review RDC pivot** — see Gap G1 | `AlcReviewPage.jsx` |

**Exit criterion:** an ops user can run All RDCs and export a picking requirement without
touching SQL.

**Rollback:** the panel renders only under All RDCs; switch off hides the feature's effect.

---

# STAGE 5 — TEST & SIGN-OFF

| # | Check | Gate |
|---|---|---|
| V3 | No RDC over-drawn: `Σ(SHIP+HOLD)` per `(SRC_RDC, VAR_ART, SZ)` ≤ that RDC's MSA `FNL_Q` | blocking |
| V4 | Conservation: `Σ split.SHIP = Σ working.SHIP`; same for HOLD | blocking |
| V5 | **4-table** row-for-row diff of `Own` vs baseline 0.1 | blocking |
| V6 | Untagged fallback: tags blanked → identical quantities, `PREF_TIER='2'` | blocking |
| V9 | N=4 fixture, line needs 3 sources → capped at 2, remainder reduced + stamped | blocking |
| V11 | `ARS_RDC_FALLBACK`: full order (`1F`) · partial appends · empty → tier 3 · `'*'` drives untagged | blocking |
| V12 | `Own` with switch ON vs OFF → identical | blocking |
| V13 | `Cross` with switch ON vs OFF → identical (vs baseline 0.2) | blocking |
| V14 | Cross-ship pend lands on `SRC_RDC`; BDC names that warehouse | blocking |
| V15 | Cross-RDC hold deducts from the sourcing RDC in next MSA run | blocking |
| V16 | **Re-run idempotency** — Generate twice on one session → no duplicate split rows | blocking |
| V17 | **Performance** — Part 8.37 completes within budget (Gap G5) | blocking |
| V7 | `SPLIT_ALWAYS` vs `SINGLE_PREFERRED` → identical per-RDC totals | regression |
| V8 | Determinism — run the pass twice → identical `SRC_RDC` | regression |
| V10 | Cap cannot bind at N=2 → zero reduced SHIP lines | regression |
| V2 | Fill-rate improvement, live parallel run | business sign-off |

Unit tests alongside `backend/tests/test_alloc_round_stamp_per_opt.py` (the only test pattern in
this repo — 2 files today, so the harness is thin; budget time for fixtures).

---

# STAGE 6 — PARALLEL RUN (switch still OFF for routine work)

1. Pick one live session. Run it `Own` — this is the production run that ships.
2. Re-run the same store/MAJ_CAT set with `ALC_RDC_CENTRAL_POOL` ON, in a **non-parked**
   session, purely to measure.
3. Compare: `Σ SHIP_QTY`, unmet demand, residual stock, cross-ship %, split lines, reduced lines.
4. Present the delta. Warehouse confirms it can physically execute a two-source line.
5. Repeat for at least 3 sessions across different MAJ_CAT mixes.

**Exit criterion:** V2 delta accepted in writing by management; warehouse confirms O1 and O3.

---

# STAGE 7 — CUTOVER

1. Announce the change; the default cockpit mode is already `All RDCs`, so the *behaviour*
   changes the moment the switch flips.
2. Flip `ALC_RDC_CENTRAL_POOL` to active.
3. Watch the first three runs: reduced-lines count must be 0 for SHIP; cross-ship % plausible;
   `Σ split = Σ working`.
4. `Own` stays available for single-RDC operations — do not remove it.

**Rollback:** flip the switch off. Runs revert to today's behaviour on the next Generate. No
deploy, no migration, no data repair — this is the entire reason for G2 in the plan.

---

# STAGE 8 — POST-IMPLEMENTATION (required by `CLAUDE.md`)

| # | Task | File |
|---|---|---|
| 8.1 | Update FSD + `## Recorded rules` | `frontend/public/docs/manual/listing.md` |
| 8.2 | Register 2 new tables + 4 new columns — **hardcoded Python list**, not auto-generated | `app/api/v1/endpoints/data_dictionary.py` |
| 8.3 | Re-run screenshots (cockpit + new settings page) | `tools/manual/REFRESH.md` |
| 8.4 | Sync the extracts | `.claude/agents/ars_flow_kb/`, `docs/RULE_MASTER.md` |
| 8.5 | Release note | `ReleaseNotesPage.jsx` |

---

# 9. WHAT IS STILL MISSING

Seven gaps found while writing this runbook. None are in the implementation plan.

## G1 — Alloc Review's RDC pivot becomes ambiguous 🟠

`AlcReviewPage.jsx` pivots MAJ_CAT × **RDC** at several drill levels (`:98`, `:254`, `:324`,
`:400`). That RDC is the **store's** RDC. After clubbing, a reviewer looking at "RDC" sees
*demand by store's RDC* while the warehouse is picking by *source RDC* — two different numbers
under the same column header.

**Needs a decision:** add a `SRC_RDC` toggle to the pivot, or relabel the existing column
`Store RDC`. My recommendation: relabel now (cheap, honest), add the toggle in a follow-up.
**Not costed in the plan — add to Stage 4.**

## G2 — `ARS_PEND_ALC.RDC` has 11 consumers 🔴

Changing that column's meaning from *store's RDC* to *source RDC* ripples into:

`activity_log.py` · `ars_dashboard.py` · `bdc.py` · `gap_report.py` · `grid_builder.py` ·
`hold_dashboard.py` · `maintenance.py` · `pend_alc.py` · `reports.py` · `upload.py` ·
`data_dictionary.py` — plus 6 stored procs in `backend/sql/`.

Most *want* the source RDC (that is the point). But **"most" is not an engineering answer.**
Each consumer must be read and classified: *wants source* / *wants store* / *doesn't care*.
Any that want the store's RDC must join the store master explicitly instead of relying on the
column.

**This is a half-day audit and it is a blocking prerequisite for Stage 3.** It is entirely
absent from the plan.

## G3 — Concurrent parked runs can double-allocate 🔴

Stock is only reserved when a run is **approved** (`write_pend_alc`). Two runs that are parked
but not yet approved both see full stock.

Today with two RDCs and `Own`, two simultaneous runs naturally draw from **different pools** —
the isolation is accidental but real. **Clubbing destroys it.** Two parked All-RDCs sessions
would both allocate the same pieces.

Production currently runs single-parked (409 guard at `listing.py:603`), so this is latent —
**until someone turns on `allow_multi_parked`.**

**Required:** block `allow_multi_parked` while `ALC_RDC_CENTRAL_POOL` is active, with a clear
error message. One `if`, but it must be written, and task 0.7 must confirm the current setting.

## G4 — Re-running Generate duplicates split rows 🟠

`ARS_ALLOC_RDC_SPLIT` is `SESSION_ID`-keyed and durable. `ARS_ALLOC_WORKING` is dropped and
rebuilt each run. A re-run of the same session would hit the PK or leave stale rows from the
previous attempt.

**Required:** Part 8.37 opens with `DELETE FROM ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID = :sid`.
Added as task 2.9 and test V16.

## G5 — No performance budget for the split pass 🟠

The pass walks every allocation line (~8,400 in the reference run) × the preference order,
maintaining an in-memory ledger, then bulk-inserts one row per line-source. It runs inside the
Generate request thread, between Part 8.36 and Part 8.4.

**Nobody has set a budget.** If it adds 90 seconds to every run, ops will notice.

**Required:** a target (suggest **≤ 15 s for 10,000 lines**), a benchmark in Stage 5 as V17, and
a decision to bulk-insert via `executemany` / a staging table rather than row-by-row.

## G6 — No rollback plan was written 🟠

The plan has a phase table but no rollback. Now covered per-stage above. The short version:
**stages 1-4 all roll back by flipping `ALC_RDC_CENTRAL_POOL` off** — no deploy, no migration,
no data repair. That property is worth protecting: **no change may be written that cannot be
undone by the switch.** Make it a code-review rule.

## G7 — RLS behaviour on a cross-RDC picklist is undefined 🟡

`rls_stores` restricts users to a set of stores. The RDC-wise picking requirement is keyed on
**warehouse**, not store, and a single RDC's picklist spans stores the requesting user may not
be entitled to see.

**Open question for the business owner:** should the picking report be RDC-scoped (warehouse
staff see their own warehouse's full picklist regardless of store RLS), or store-scoped
(filtered, and therefore incomplete as a picking document)?

A picklist that is silently incomplete is worse than no picklist. **This needs an answer before
Stage 4.**

---

## 10. Revised open decisions

| # | Question | Owner | Needed by |
|---|---|---|---|
| O1 | Can picking execute a two-source line for one store-size? | Supply chain | **Stage 0** |
| O2 | Confirm BR-RDC-11 — holds never split; a short hold is reduced | Supply chain | **Stage 0** |
| O5 | Seed order for the two `'*'` rows | Business owner | **Stage 0** |
| O3 | Inter-warehouse transfer document, or STO direct from the sourcing RDC? | Supply chain / SAP | Stage 6 |
| O4 | Target date to clean untagged stores in `Master_ALC_INPUT_ST_MASTER` | Business owner | Stage 7 |
| **G1** | Alloc Review pivot — relabel, or add a source-RDC toggle? | Product owner | Stage 4 |
| **G7** | Picking report — RDC-scoped or store-RLS-scoped? | Business owner | Stage 4 |

---

## 11. Effort summary

| Stage | Content | Risk |
|---|---|---|
| 0 | Baselines + 3 decisions | none — but **irreversible if skipped** |
| 1 | 2 tables, 1 API, 1 page | none — deployable alone |
| 2 | Club + split pass | medium — switch-protected |
| 3 | 3 downstream files + **G2 audit** | **highest** — touches the live picking pipeline |
| 4 | Cockpit, reports, G1 | low |
| 5 | 17 checks, 10 blocking | — |
| 6 | 3 parallel runs | none — measurement only |
| 7 | Flip the switch | reversible in one click |
| 8 | Docs, dictionary, screenshots | none |

**Critical path: Stage 0 → 1 → 2 → 3 → 5 → 6 → 7.** Stage 4 can run parallel to Stage 3.
