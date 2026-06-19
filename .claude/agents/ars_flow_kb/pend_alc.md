# Pending Allocation — rules & details

## Source files
- `backend/app/services/pend_alc_service.py`
- `backend/app/services/alloc_queue.py`
- `backend/app/services/alloc_cancellation.py`
- `backend/app/api/v1/endpoints/pend_alc.py`

## Concepts
- **PEND**: quantity already committed but not yet shipped/consumed. Subtracted from `STK` in MSA Step 9: `FNL_Q = max(STK - PEND - HOLD, 0)`.
- **Universe contribution (NEW, June 2026)**: open `ARS_PEND_ALC` rows (`IS_CLOSED=0`, `PEND_QTY>0`) contribute their `(RDC, GEN_ART_NUMBER)` to the MSA universe at Step 6 (`msa_service._load_universe`). The Step 6 backfill then pulls every VAR_ART of that GEN_ART from `vw_master_product` into `msa_pivot` so the Step 7 PEND merge always has a row to land on. Net effect: `SUM(MSA_TOTAL.PEND_QTY) == SUM(ARS_PEND_ALC.PEND_QTY WHERE IS_CLOSED=0)` after every full MSA Generate.
- **Alloc queue**: pending allocations awaiting approval / dispatch (`alloc_queue.py`).
- **Cancellation / revert**: removes a pending allocation and restores stock state (`alloc_cancellation.py`).

## Lifecycle
`queued → approved → dispatched | cancelled (reverted)`

## Critical: revert correctness
- A reverted allocation must restore `PEND` to its pre-allocation value at the OPT grain.
- Recent commits (`fecb6f9 pending update`, `3c7693b correct pending and do update and approve revert process`, `c06d051 fix pending do`) indicate this is an active correctness area — read git log for context before editing.

## Eight write paths to ARS_MSA_TOTAL / VAR_ART / GEN_ART
Only path #1 ever changes `STK_QTY` or inserts rows. Paths #2–#8 are UPDATE-only and assume the `(RDC, ARTICLE)` row exists — that's why the universe-anchored Step 6 backfill matters.

| # | Function | Trigger | What it writes |
|---|---|---|---|
| 1 | `msa_result_storage.store_results` → `_store_table_data` × 3 | MSA Generate (`POST /api/v1/msa/calculate`, `msa_job_service`) | TRUNCATE + INSERT all 3 tables; then auto-chains #7 + #8 |
| 2 | `adjust_msa_after_pend_insert` (`pend_alc_service.py:3518`) | Any PEND_ALC INSERT (manual upload, CSV bulk, approve_parked) | PEND_QTY/FNL_Q on affected `(RDC, ARTICLE)` keys; rolls TOTAL → VAR_ART → GEN_ART |
| 3 | `apply_pend_alc_delta(sign=±1)` (`pend_alc_service.py:4803`) | PEND_ALC mutation with explicit sign (revert=-1, insert=+1) | PEND_QTY/FNL_Q in all 3 tables for the delta rows |
| 4 | `apply_pend_alc_delta_by_session` (`pend_alc_service.py:5088`) | Approve Parked / session-scoped revert | Same as #3, scoped via `ARS_ALLOC_HISTORY.SESSION_ID` |
| 5 | `apply_hold_clear` (`pend_alc_service.py:3741`) | Hold Dashboard "Clear" | HOLD_QTY/FNL_Q in all 3 tables for closed hold rows |
| 6 | `apply_hold_revise` (`pend_alc_service.py:4057`) | Hold Dashboard "Revise" | HOLD_QTY/FNL_Q in all 3 tables for revised hold rows |
| 7 | `bootstrap_msa_pend_sync` (`pend_alc_service.py:5099`) | (a) end of #1, (b) post-revert (`revert_operation`), (c) post-grid-build (`grid_builder.py:2021`) | Full-table reseed of PEND_QTY/FNL_Q from `ARS_PEND_ALC` scan |
| 8 | `bootstrap_msa_hold_sync` (`pend_alc_service.py:5193`) | (a) end of #1, (b) after `approve_parked` PEND delta (session-scoped, `parked_history.py:762`) | Reseed HOLD_QTY/FNL_Q from `ARS_NL_TBL_HOLD_TRACKING` scan (full or scoped) |

### Invariants across all paths
- `FNL_Q = max(STK_QTY - PEND_QTY - HOLD_QTY, 0)` is recomputed inside every UPDATE.
- Rollup direction is always TOTAL → VAR_ART → GEN_ART (per-(RDC,color) sums).
- Only path #1 inserts rows. Universe-anchored Step 6 (msa_service) is what guarantees these rows exist for every open PEND/HOLD.

## Invariants relevant here
- OPT uniqueness (invariant 1)
- MBQ sparseness (invariant 3) — applies when pend rows feed back into sec-cap calcs
- Universe-anchored MSA (June 2026): every open PEND row contributes its `(RDC, GEN_ART_NUMBER)` to the MSA universe; `_load_universe` is invoked from `msa_service.calculate()` Step 6. See `ars_flow_kb/msa.md`.

## Recorded rules
<!-- ars_flow appends dated bullets below. One rule per line. -->
- 2026-06-13 — `ARS_PEND_ALC` has NO unique constraint on `(SESSION_ID, RDC, ST_CD, ARTICLE_NUMBER, ALLOC_MODE)` — only PK on `ID`. The docstring at `pend_alc_service.py:16` claims that grain but `write_pend_alc` enforces it with `NOT EXISTS`, and `write_manual_pend_alc` does not enforce it at all. Why: duplicate manual rows for the same key silently double-count after `apply_pend_alc_delta` aggregates by `(rdc, art)`.
- 2026-06-13 — Manual upload accepts payload with no required Pydantic validation (`pend_alc.py:2222`): negative `alloc_qty` is silently dropped by `write_manual_pend_alc:2361` (`> 0` filter), but blank `rdc`/`article_number`/orphan articles (not in `vw_master_product`) are inserted and become MSA orphans (their delta UPDATE matches 0 rows → MSA stays stale until next bootstrap). Why: confirms #6 (orphan article) in the prior audit.
- 2026-06-13 — Multi-chunk DO upload via `/do-update` (sync) uses `merge_payload_lists=["pend_updates","history_updates","auto_history_closes"]` for read-modify-write of the ops_log payload, but the `/do-update-async` worker (`_do_run_job`) aggregates all chunks inside one job and writes the ops_log ONCE with `is_first=True`. The sync path is multi-request; async is single-request. Why: revert behaviour is identical at end-state but only the async path is atomic — interleaved sync DO uploads can leave the ops_log row stuck on a previous chunk's summary if a later chunk crashes between `apply_do_deductions` and `log_operation_upsert`.
- 2026-06-13 — `apply_pend_alc_delta(sign=+1)` aggregates the temp table by `(rdc, art)` only (`pend_alc_service.py:4946`), so the MSA delta is correct even if duplicate manual rows arrive. But the ROLLUP grid pass uses `_build_rollup_delta_sql` which does its own per-grain aggregation — duplicate rows here would still be summed. Confirms no double-deduction in MSA, but verify grids when manual upload has dup rows.
- 2026-06-13 — Adhoc close `apply_adhoc_close` (`pend_alc_service.py:2878`) does NOT call `apply_pend_alc_delta(sign=-1)` or `bootstrap_msa_pend_sync` after closing PEND rows. Result: `MSA_TOTAL.PEND_QTY` and grid `PEND_ALC` stay inflated until the next full MSA Generate or any other op that triggers a bootstrap. Listing/allocation run between an adhoc close and the next MSA Generate will see lower FNL_Q than reality (held back qty that no longer pends). Why: matches `apply_hold_clear` pattern but contradicts the rest of the lifecycle — manual / approve / revert paths all sync MSA.
- 2026-06-13 — Adhoc close with blank `ST_CD` is interpreted as "any-store" (`pend_alc_service.py:2961`: `(u.st_cd = '' OR ISNULL(P.ST_CD,'') = u.st_cd)`). A user uploading a CSV with an empty `ST_CD` column (very common when copy-pasting from Excel) will silently close EVERY open row for `(RDC, ARTICLE)` across every store. Same wildcard semantics apply to BDC_HISTORY cancellation. No confirmation prompt or scope-check.
- 2026-06-13 — `apply_do_deductions` FIFO partition (`pend_alc_service.py:3371,3376`) is `PARTITION BY P.RDC, ISNULL(P.ST_CD,''), P.ARTICLE_NUMBER` — it does NOT include `ALLOC_MODE`. A DO arriving for an (RDC, ST_CD, ART) that has both a RL row and a TBC row will FIFO-consume the older one regardless of which mode the SAP DO came from. Cross-attribution risk confirmed: a TBC allocation can be settled by an RL DO (and vice versa), which silently re-categorises the lifecycle.
- 2026-06-13 — `update_bdc_history_with_do` routes a `do_qty=0` row as a CANCEL but the same routing keys (`alloc_no` > `st_cd` > global) are used. A user-supplied `allocation_number` with `do_qty=0` cancels exactly that BDC. With only `(rdc, art)` and `do_qty=0` the cancel scope is global — every OPEN BDC for that (RDC, ART) is flipped to CANCELLED. Confirms wildcard-cancel risk in `pend_alc_service.py:2772-2774`.
- 2026-06-13 — `ARS_PEND_ALC` has legacy `MATNR` (bigint) and `QTY` (int) columns that are never written or read by current code. They survived a schema migration. Safe to ignore but the table layout is misleading.
- 2026-06-13 — `_check_adhoc_close_revert` (`pend_alc_service.py:3011`) gates revert on `P.LAST_DO_AT > op_date OR P.LAST_BDC_AT > op_date`, but `apply_adhoc_close` does NOT touch `LAST_DO_AT`/`LAST_BDC_AT`. So if no NEW downstream BDC/DO arrives, the revert always passes — even if the BDC_HISTORY row that was CANCELLED here has been re-generated by a fresh `/bdc-generate` (which creates a new OPEN row at a new ID). Reverting will then restore STATUS='OPEN' on the OLD history row, leaving TWO OPEN history rows for the same (RDC, ST_CD, ART) — the `_NO_OPEN_BDC_PREDICATE` filter sees both and silently fails to re-emit any further BDC.
