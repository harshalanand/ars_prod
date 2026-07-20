# Removal Plan — Per-OPT as the Only Allocation Engine

**Document Version:** 1.1 — **Phases 1-4 EXECUTED 2026-07-10** (phase 5 pending: rule_engine_new Stage C prune, module rename, run_012 script removal)
**Date:** 2026-07-10
**Author:** ARS Engineering
**Owner:** Akash Agarwal, Director — V2 Retail
**Requested by:** Santosh Kumar
**Scope:** Remove every allocation-engine code path except `per_opt`, so no run can silently use a divergent engine.

---

## 1. Why

On 2026-07-10, two runs with identical inputs produced different allocations purely because of the engine
selector: **pandas ALLOC = 7,259 vs per_opt/baseline ALLOC = 9,824** for `M_TEES_HS`
(sessions `20260710_123652_796` vs `20260710_130337_251`; baseline `20260709_141445_912`).
per_opt carries the current business rules (pak-align TBL SHIP/HOLD independently — commit `58ef3dc`;
SZ_MBQ × I_ROD ship cap — commit `2a8c87b`); the other engines do not.

**Live bug found during review (proves the risk):** `POST /listing/retry-failed`
(`listing.py:3514-3523`) never sets the engine switch — it inherits whatever `ARS_PER_OPT_MODE`
the previous `/generate` left in the process environment (`listing.py:2992/:3004`). A retry can therefore
**silently run the pandas band on a per_opt session**. Pinning per_opt as the only engine fixes this
class of bug permanently.

## 2. Structural constraint (read before deleting anything)

per_opt is **not standalone**:

- `allocation_mode='per_opt'` dispatches through **`rule_engine_pandas.py`'s orchestration**
  (`run_listing_and_allocation_pandas:549`): worker pool, writer queue thread (`:379`), table loads
  (`_load_tables:1355`), typed hold read (`:220-248`), sec-cap specs (`:741-822`), Stage D pak rounding
  (`:472`), result write-back (`:2896-3230`). The env-var switch swaps **only the band function**.
- Stage A/B (listing explode → `ARS_ALLOC_WORKING`) lives in **`rule_engine_new.py`** and is shared.

So the removal targets the **non-per_opt waterfall paths and selectors** — not whole files blindly.

---

## 3. Section A — REMOVE (verified unreferenced after this plan)

| # | Item | Location |
|---|---|---|
| A1 | Alloc-mode radios (Pandas in-memory / Per-OPT / Sequential fallback) + Order A/B radios; `allocationMode` (`:890`) and `execOrder` (`:895`) states; payload keys `allocation_mode`/`exec_order` (`:1357-1358`, `:1601-1602`); pandas/sequential `MODE_INFO` entries (`:2357-2360`, `:2379`, `:2431-2433`). **Keep** Workers + Writer-Q controls (`:1991-2033`). | `frontend/src/pages/ListingPage.jsx:1941-1990` + listed lines |
| A2 | Sequential dispatch branch (`:3040-3066`); env-var switch block setting/popping `ARS_PER_OPT_MODE`/`ARS_EXEC_ORDER` (`:2986-3005`); mode parse (`:2983`); dead `exec_order` field (`:165` — written to env at `:2996`, **read nowhere**); stale `"python_parallel"` fallback string (`:638`); sequential special-case in batch_id logic (`:642`); IM-3 guard's mode condition (`:601-613` — keep the hold-toggle validation, drop the mode check). | `backend/app/api/v1/endpoints/listing.py` |
| A3 | `RetryFailedRequest.allocation_mode` / `.exec_order` — already ignored by the handler today. | `listing.py:3434-3436` |
| A4 | Pandas-only waterfall: `_run_band` (`:2111-2440`); its `else` call site (`:1959-1967`); non-per_opt snapshot (`:1916-1917`); non-per_opt FNL_Q_REM recompute (`:1114-1132`); `_is_per_opt_mode` (`:85-86` — hard-pin its 6 branch sites `:752, :809, :1087, :1114, :1196, :1793` to the per_opt path); `_hold_suppress_arr` copy (`:89-119` — only caller was `_run_band:2336`; per_opt has its own at `rule_engine_per_opt.py:67`). | `backend/app/services/rule_engine_pandas.py` |
| A5 | Dead helper `_build_mbq_budget` (`:1627-1649`) — nothing calls it (only `_live_mbq_budget:1651` is used, at `:1922`). | `rule_engine_pandas.py` |
| A6 | **Whole files** — zero live imports (API-unreachable already; only self-refs, one docstring mention `alloc_queue.py:4`, docs/CHANGELOG): `rule_engine_parallel_python.py`, `rule_engine_parallel_sql.py`, `backend/sql/usp_ars_allocate_majcat.sql`. | `backend/app/services/`, `backend/sql/` |
| A7 | Legacy `rule_engine.py` and `listing_allocator.py` (zero live imports, grep-verified) + `if False` block `listing.py:2965-2978`. *Conservative option: keep the two files one release as reference-only; delete in Phase 2.* | `backend/app/services/`, `listing.py` |
| A8 | **Phase 2 only** — `rule_engine_new.py`'s own sequential waterfall, dead once A2+A6 land: entry `run_listing_and_allocation` (~`:100-263`), Stage C loop (~`:2625`), `_stage_c_run_band` (`:2764`), `_revalidate_after_band` (`:1168`), `_revalidate_cross_type` (`:1468`). | `backend/app/services/rule_engine_new.py` |

## 4. Section B — KEEP (prevents breakage)

| Item | Why |
|---|---|
| `rule_engine_pandas.py` **orchestration** (see §2 list) | It IS per_opt's runtime. Suggest renaming the module `rule_engine_runner.py` **later, in its own commit** (import sites: `listing.py:3008, :3514`; per_opt cross-refs are comments only). |
| **Post-pass sec-cap gate** `rule_engine_pandas.py:1195+` | per_opt's safety net when sec-cap spec build fails (`:795-800` fallback) — not pandas-only code. Deleting it would violate the sec-cap invariant. |
| `rule_engine_new.py` as **Stage A/B + shared-helper library** | `_stage_a_*`, `_stage_b_*`, `_stage_c_apply_opt_mj_req_gate` (called from `rule_engine_pandas.py:1082` — hard-pin `skip_tbl_branch=True`), `_discover_all_active_grids`, `_apply_sec_grid_cap_pre_gate`, `POOL_TABLE`, `_stage_d_reflect`, `_classify_alloc_reason`, constants. |
| `allocation_mode` request field | **Keep, but hard-400 on anything ≠ `'per_opt'`.** Explicit rejection beats accept-and-ignore: old scripts/replays fail loudly instead of silently running a renamed engine. |
| Session/queue mode columns (`listing_sessions.py:184/:329/:377`, `alloc_queue.py:103/:135`) | History/audit — keep, always stamped `'per_opt'`. |
| Workers + Writer-Q settings (`use_writer_queue`, `parallel_workers`) | Consumed by the kept orchestration (writer thread `:379`; worker pool). |
| `_SETTING_DEFAULTS` (`listing.py:396-429`) | No mode key persisted — nothing to do. |

## 5. Section C — Risks & sequencing

**Execute in this order** (reversing 3↔5 breaks imports):

1. Pin dispatch + add the `allocation_mode != 'per_opt'` → 400 guard (`listing.py`).
2. Frontend radio removal (A1).
3. Delete parallel/legacy files (A6, A7).
4. Prune `_run_band` + non-per_opt branches in `rule_engine_pandas.py` (A4, A5).
5. **Phase 2**: prune `rule_engine_new.py` Stage C (A8) + optional module rename.

| Risk | Mitigation |
|---|---|
| `/retry-failed` env-inheritance bug | Fixed by hard-pinning the mode — **verify one retry immediately after deploy**. |
| Replays of old `pandas`/`sequential` sessions' REQUEST_JSON now 400 | Intended; inform operators. |
| `skip_tbl_branch` hard-pin (`rule_engine_new.py:2225-2227`) | Retest the MJ_REQ gate — known TBL-vaporize regression note at `:1079-1081`. |
| Docs drift | Update in the same pass: `BRD_ARS_V2.md:9/:350/:413-463`, `FSD_FRESH_GRT_HOLD_CONTROL.md:326` (IM-3 wording), `frontend/public/docs/manual/listing.md:157`, `.claude/agents/ars_flow_kb/merge_rules.md:9-10`, `.claude/agents/rule_ars.md:15-16`, `.claude/agents/ars_flow.md:44`, `docs/RULE_MASTER.md`, `build_listing_alloc_doc.py:296/:1156`, memory note `per-opt-is-production-default` → "per_opt is the **only** engine". |

## 6. Invariant check

OPT uniqueness · growth at MJ+grid only · `*_MBQ=0` sparseness · sec-cap grid-extras propagation ·
ACS_D vs MAX_DAILY_SALE — **none touched** by the removals above; the one sec-cap gate that matters is
explicitly on the KEEP list (§4).

## 7. Exit criteria (after phases 1–4)

1. `python -m py_compile` on all touched files; frontend `vite build` clean.
2. One full per_opt regression run with the params of baseline `20260709_141445_912` →
   `M_TEES_HS` ALLOC = 9,824 / HOLD = 1,931 (unit-exact, as re-proven on 2026-07-10 by `20260710_130337_251`).
3. `POST /listing/generate` with `allocation_mode='pandas'` → HTTP 400.
4. One `/retry-failed` cycle runs the per_opt band (log shows per_opt, not `_run_band`).
