---
title: Listing
tags: [ars, listing, allocation]
updated: 2026-07-17
---

# Listing

Stage 4 of the [[Pipeline Overview|pipeline]] — the staging + hand-off layer between [[MSA Stock Calculation|MSA]]/[[Grid Builder|Grid]] and the [[Rule Engine (per_opt)|allocator]]. `POST /listing/generate` runs one long transaction in numbered Parts.

- **Source:** `backend/app/api/v1/endpoints/listing.py` (~277 KB), `backend/app/services/listing_sessions.py`.
- **Outputs:** `ARS_LISTING` (one row per OPT), `ARS_LISTING_WORKING` (eligible rows only — engine input), `ARS_STORE_RANKING`, `ARS_LISTED_OPT`, `ARS_ALLOC_WORKING`, `ARS_LISTING_SESSIONS`.

## Session model
- **`ARS_LISTING_SESSIONS`** — one header per `/generate`. `SESSION_ID` = timestamp string, also the `batch_id`. `start_session` attaches a per-session loguru sink (`logs/listing_sessions/<sid>.log`); `end_session` is cancel-safe; `kill_session` does cooperative-cancel + SQL SPID KILL. `PARKED_STATUS` ∈ PARKED / SKIPPED_ERROR / SKIPPED_EMPTY / PARKED_PARTIAL.
- Data tables are **dropped+recreated every run** — hence the concurrency guard.
- **working → parked → approved:** live `*_WORKING` tables → Part 8.4 snapshots to `*_PARKED` (tagged by session) → [[Review and Approve|Approve]] promotes to `*_HISTORY`.

## Build parts (inside `_generate_listing_impl`)
| Part | What it does |
|------|--------------|
| 1 | Grid data (existing stock) → `IS_NEW=0` into `ARS_LISTING` |
| 2 | MSA-missing options → `IS_NEW=1` |
| 3.5–3.5c | ACS_D + ALC_D, LISTING flag, I_ROD, CLR_MIN/MAX, FOCUS caps, AUTO_GEN_ART_SALE, AGE |
| **3.54** | `RL_HOLD_QTY` from `ARS_NL_TBL_HOLD_TRACKING` (prior-run hold, pool-scoped) — **must precede 3.6** |
| 3.55 | `MSA_FNL_Q` (typed by `alloc_type`), `VAR_COUNT`/`VAR_FNL_COUNT` |
| **3.6** | **OPT_TYPE classification** (see below) |
| **4 pre-resolve** | **MP-resolve**: ALTER + populate `FAB/MACRO_MVGR/MICRO_MVGR/M_VND_CD/RNG_SEG…` onto `ARS_LISTING` in ONE join to `vw_master_product`. Origin of sec-cap grid extras (invariant 4). *(No function literally named `_resolve_mp_cols` — it's this inline block.)* |
| 4a–4e | per-grid column joins, `PER_OPT_SALE`, `OPT_MBQ`/`OPT_REQ`/`OPT_MBQ_WH`/`MAX_DAILY_SALE`, `ART_EXCESS`, per-grid REQ |
| **6** | Store ranking → `ARS_STORE_RANKING` (`W_SCORE`, `ST_RANK`, `MANUAL_ST_PRIORITY` pin) |
| **6.6** | `ELIG_FLAG`/`ELIG_REASON` materialization |
| **7** | project `ELIG_FLAG=1` → `ARS_LISTING_WORKING`; growth lift; `GH_`/`H_`/`PRI_CT%`/`SEC_CT%`/`ALLOC_FLAG` |
| **8** | Rule engine dispatch → park snapshots (8.4) → OPT_STATUS/TBL_LISTED_DATE (8.5) → hold schema (8.6); stamp `ALLOC_TYPE` (8.35) |

## OPT_TYPE classification (Part 3.6, first-match-wins)
`SUPPLY_OK = MSA_FNL_Q>0 OR RL_HOLD_QTY>0`; `ADEQUATE = STK_TTL ≥ threshold×ACS_D`.
1. **MIX(a)** — `NOT ADEQUATE AND MSA_FNL_Q=0 AND RL_HOLD_QTY=0` (nothing to send)
2. **MIX(b)** — `VAR_COUNT>0 AND (VAR_FNL_COUNT/VAR_COUNT < size_threshold OR VAR_FNL_COUNT < min_size_count)` (poor colour fill)
3. **RL** — `(ADEQUATE OR RL_HOLD_QTY>0) AND MSA_FNL_Q>0`
4. **TBC** — `0 < STK_TTL < threshold×ACS_D AND SUPPLY_OK`
5. **TBL** — `STK_TTL ≤ 0 AND SUPPLY_OK`
6. ELSE → **MIX**

## Eligibility gate (Part 6.6, AND of all, first failure named)
`NOT_LISTED` → `NO_STOCK` → `NO_DEMAND` → `NO_DISPLAY` → `TBL_SIZE_LT_60` → `OK`. Only `ELIG_FLAG=1` rows reach `ARS_LISTING_WORKING`.

## Run cockpit → engine params (defaults)
`stock_threshold_pct` 0.6 · `size_threshold` 0.6 · `min_size_count` 3 · `excess_multiplier` 2.0 · `hold_days` 0 (TBL-only) · `age_threshold` 15 · `default_acs_d` 18 · `req_weight`/`fill_weight` 0.4/0.6 · `mj_req_growth_pct` 100 (MJ+grid growth) · `rl/tbc/tbl_mbq_cap_pct` & `_mj_req_cap_pct` (downward caps) · `pri_ct_check_rl/tbc` false · `rl/tbc_dispatch_mode` COMPLETE · `apply_sec_cap_in_normal` true · `allocation_mode` **per_opt** (only value) · **`alloc_type` FRESH|GRT (required, 422 if missing)** · `cont_fallback_mode` P4_UNIFORM · hold-suppression `skip_hold_upc`/`apply_hold_seg_app`/`apply_hold_seg_gm`.

## Key endpoints
- **Run:** `POST /generate`, `/retry-failed` (re-dispatch FAILED MAJ_CATs only; Parts 1–7 not re-run; recovers `alloc_type`+hold toggles), `/cancel-batch`.
- **Progress:** `/alloc-progress?batch_id=`, `/alloc-batches`, `/active-job`.
- **Parked:** `/parked-runs`, `/parked-runs/{sid}` + `/approve` + `/reject`, `/parked-runs/purge`.
- **History/sessions:** `/alloc-history`, `/listing-history`, `/sessions/*` (+ `/log`, `/kill`).
- **Preview/report:** `/summary`, `/opt-summary`, `/var-summary`, `/store-by-majcat`, `/contribution`, `/export`.

## Approve flow
`approve_parked_run` → `parked_history.approve_parked`. **Idempotent** (`{already_approved:true}`). On a fresh approve, fires two report events `listing.approved` + `pendalc.approved` (never blocks the approve). Promotion mechanics → [[Pending Allocation and Hold]].

## Gotchas
- Two concurrency guards: `has_running_session()`→409, `has_pending_parked()`→409 unless `allow_multi_parked` (AppSettings) or ADMIN.
- `allocation_mode` hard-400 for anything but `per_opt`. E-01 pool pre-check (400 if MSA has no rows of the requested `alloc_type`).
- `allow_multi_parked` **request field is ignored** — authoritative value from AppSettings.
- Background thread reconstructs `GenerateRequest` from a dict; `parallel_workers` clamped 2–8.
- **`listing_allocator.py` is DELETED** — the manual/KB E1–E7 "allocator" section is stale; that logic is now in [[Rule Engine (per_opt)]].

## Cross-links
[[MSA Stock Calculation]] · [[Grid Builder]] · [[Rule Engine (per_opt)]] · [[Pending Allocation and Hold]] · [[Fresh-GRT Allocation]] · [[Review and Approve]].
