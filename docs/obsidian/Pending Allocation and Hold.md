---
title: Pending Allocation and Hold
tags: [ars, pending, hold, bdc, do]
updated: 2026-07-17
---

# Pending Allocation and Hold

Stages 7 & 8 — the **feedback loop** that makes ARS stateful. Both commit at **Approve** and both net down [[MSA Stock Calculation|MSA]] on the next cycle: `FNL_Q = max(STK − PEND − HOLD, 0)`.

- **Source:** `pend_alc_service.py` (~274 KB), `pend_alc.py` (~180 KB, API), `parked_history.py`, `hold_dashboard.py`.

## Pending Allocation (`ARS_PEND_ALC`)
An approved session has *decided* to ship, but SAP hasn't cut the DO. Those units are **PENDING**; MSA subtracts them so the next run doesn't double-ship.

- **Grain:** `SESSION_ID × RDC × ST_CD × ARTICLE_NUMBER × ALLOC_MODE` (enforced by `NOT EXISTS` — **PK on `ID` only, no unique on the logical grain**). `RDC`=source WH (drives MSA), `ST_CD`=dest store; `PEND_QTY = ALLOC_QTY − DO_QTY` (persisted computed); `ALLOC_MODE` RL/TBC/TBL; `ALLOC_TYPE` FRESH/GRT; `BDC_QTY` (audit only); `SOURCE` AUTO/MANUAL.
- **Lifecycle:** `PENDING (IS_CLOSED=0) → [BDC stamped] → [DO drawn down] → CLOSED (DO_QTY≥ALLOC_QTY)`.
- **Auto write on Approve — `write_pend_alc`:** reads `ARS_ALLOC_HISTORY` for the session, joins store master (WERKS→RDC) + listing-working-history (`OPT_TYPE→ALLOC_MODE`). Idempotent (`NOT EXISTS`), once-only per session. Then `apply_pend_alc_delta_by_session(+1)` applies +PEND into MSA + all active `ARS_GRID_MJ*` immediately. **2026-07-16 fix:** the join now includes `MAJ_CAT` as a real GROUP BY key — previously an article listed under two MAJ_CATs fanned out `ALLOC_QTY` and inflated pending.
- **Manual upload — `write_manual_pend_alc`:** `SOURCE='MANUAL'`; weak validation (blank rdc / orphan articles become MSA orphans; no grain de-dup → can double-count). See [[Known Risks and Doc Drift]].

## Hold (`ARS_NL_TBL_HOLD_TRACKING`)
Ramp-up reservation for TBL/NL merch — part of the allocated qty is held back at the WH and released as sell-through justifies.

- **Grain:** `WERKS × VAR_ART × SZ × ALLOC_TYPE` (**4-part**, typed pools Jul 2026; legacy `''` sentinel — can't be NULL in a PK). `HOLD_REM` drives MSA; carries no session metadata (attribution lives in `_SNAPSHOT`/`_SNAPSHOT_SESSIONS`).
- **Step A (approve):** RL/TBC lines *consume* hold — decrement `HOLD_REM` by `FROM_HOLD_QTY`, **two-pass legacy-first** (drain `ALLOC_TYPE=''` then the run's typed row). Hits 0 → `IS_CLOSED=1`.
- **Step B (approve):** TBL lines *create* hold — `MERGE` on the 4-part key; matched+closed → re-open, matched+open → accumulate, unmatched → insert `OPT_STATUS='TBL'`.
- **Snapshot first:** `snapshot_hold_tracking` (scoped, idempotent) before Step A/B — revert refuses to touch the table without it.
- **Clear Hold / Revise Hold:** full/partial close or top-up; reason mandatory; re-aggregate MSA `HOLD_QTY`/`FNL_Q`; logged `HOLD_CLEAR`/`HOLD_REVISE`.
- **DO upload does NOT release hold** — decoupled; hold releases only via Step-A consumption or explicit Clear.
- **Hold suppression mask** (Fresh/GRT feature, lives in the *engines*): a TBL row is suppressed when `(skip_hold_upc AND ST_STATUS='UPC') OR (SEG='APP' AND !apply_app) OR (SEG='GM' AND !apply_gm)` → `OPT_MBQ_WH=OPT_MBQ`, `HOLD_QTY=0`, no tracking row. Must zero `round_hold` before pak-alignment.

## DO / BDC
- **BDC = Bulk Delivery Creation** (SAP-bound). Two producers: file-driven (`bdc.py /upload` → `ARS_ALLOCATION_MASTER`) and pending-driven (`pend_alc.py /bdc-generate` → stamps `BDC_QTY` on open `ARS_PEND_ALC` + writes `ARS_BDC_HISTORY`). Store selection priority: explicit `st_cd_list` > `target_date` schedule > all open stores.
- **Eligibility:** `IS_CLOSED=0 AND PEND_QTY>0 AND NOT EXISTS(open BDC for (RDC,ST_CD,ARTICLE))` — the **blocked-by-BDC** guard preventing double-send.
- **DO = Delivery Order** (SAP confirmation). `apply_do_deductions` increments `DO_QTY` across open rows; methods `FIFO` (default, but partition **excludes `ALLOC_MODE`** — a TBC DO can settle an RL row), `SESSION_FIRST`, `SESSION_ONLY`. Row hitting `PEND_QTY=0` closes + side-effect-closes its OPEN BDC (`OPEN→CONFIRMED`). **Never touches MSA `STK_QTY`** — next full Generate reconciles.
- **`ARS_STORE_BDC_SCHEDULE`** — per-store Mon–Sat flags; `get_stores_for_date` maps weekday → scheduled stores.
- **`ARS_ASYNC_JOBS`** — cross-worker/post-restart job store; in-memory dict authoritative for same-worker polls, DB write-through fallback for other workers.

## Parked history (`parked_history.py`)
"Parked" = a run's rows staged out of live tables into `*_PARKED` awaiting Approve/Reject; Approve promotes to `*_HISTORY`. Atomic promote of the 4 targets + `write_pend_alc` + hold Step A/B under `sp_getapplock` + once-only op gate. TTLs: PARKED 14d / REJECTED 30d / HISTORY 30d (0=forever, overridable in `app_settings.json`).

## Operations log & revert
`ARS_PEND_ALC_OPERATIONS` logs every mutation (APPROVE/DO/BDC/MANUAL/ADHOC_CLOSE/HOLD_CLEAR/HOLD_REVISE) and is **revertable** (gated by freshness checks). Revert of Approve deletes the session's PEND_ALC rows, applies −1 MSA/grid delta, restores hold from snapshot.

## Key endpoints (grouped)
- **Pending:** `POST /manual-upload`, `/do-update` (+`-async`), `/close-rows` (adhoc close).
- **BDC:** `GET /bdc-preview`, `POST /bdc-generate` (+`-async`), `/bdc-history*`, orphan recovery.
- **Schedule:** `GET/POST /schedule`, `/schedule/stores-for-date`, `/schedule/audit`.
- **Reconcile/read:** `/summary`, `/detail`, `/reco`, `/pend-vs-msa-gap`, `/sessions`.
- **Operations:** `/operations`, `/operations/{id}/preview-revert` + `/revert` (+`-async`).
- **Hold dashboard:** `/summary`, `/by-{store,rdc,article,status,age}`, `/timeline`, `/reconciliation`; `POST /clear-hold[-file]`, `/revise-hold[-file]`.

## Gotchas
- **`POLL_ERROR_TOLERANCE`** is a *frontend* concept (reco/DO pages tolerate transient poll 404s) — no backend constant.
- Blank-key wildcards: blank `ST_CD` in adhoc close / BDC cancel = every store for `(RDC,ARTICLE)`; `do_qty=0` cancel = global; blank `SZ` in clear/revise = every size. API guards with `confirm_close_all_stores`; the service does not.
- Adhoc close **now** calls `bootstrap_msa_pend_sync` (2026-07-08) — supersedes older "leaves MSA inflated" note.

## Cross-links
[[Listing]] / [[Review and Approve]] (the approve trigger) · [[MSA Stock Calculation]] (feedback) · [[Fresh-GRT Allocation]] (typed `ALLOC_TYPE` flow) · [[Report Generation Hub]] (ops feed reconciliation + on-approve reports).
