# Business Requirements Document (BRD)
## ARS V2 Retail — Fresh/GRT Allocation Selector & Selective Hold Control

**Document Version:** 1.0 (Draft)
**Date:** 2026-07-08
**Owner:** Akash Agarwal, Director — V2 Retail
**Author:** ARS Engineering
**Requested by:** Santosh Kumar
**Parent Document:** `BRD_ARS_V2.md`
**Scope:** MSA consumption, Listing/Allocation run parameters, TBL Hold process
**Engine Scope:** `rule_engine_per_opt.py` (production default), `rule_engine_pandas.py`, shared Stage A/B in `rule_engine_new.py`

---

## 1. Executive Summary

Today every allocation run draws from a single undifferentiated warehouse stock pool, and the TBL hold process unconditionally parks residual warehouse stock for every TBL option. The business needs:

1. **Pool selection per allocation run.** Warehouse stock is physically segregated into *fresh* bins (new-arrival SLOCs such as `V02_FRESH`) and *growth* bins (`V02_GRT`). An allocation run must declare whether it is a **Fresh** run or a **GRT** (growth) run and draw only from the corresponding pool. MSA is generated **once**; the pool is chosen at allocation time — the same MSA supports any number of Fresh/GRT runs.
2. **Selective hold suppression.** Parking TBL warehouse hold is wrong for certain targets:
   - **UPC (upcoming) stores** have not opened; held stock is locked for a store that cannot pull it.
   - **SEG-level control**: apparel (APP) and general merchandise (GM) have different replenishment cadences; the planner must be able to switch TBL hold on/off independently per SEG.

When hold is suppressed, the TBL option must behave exactly like RL/TBC: its warehouse budget equals its store budget (`OPT_MBQ_WH = OPT_MBQ`, no hold-days uplift) and no `HOLD_QTY` is written.

---

## 2. Glossary (delta to parent BRD)

| Term | Meaning |
|---|---|
| **Fresh pool** | Stock in SLOCs classified `FRESH` (e.g., `V02_FRESH`) — new-arrival inventory for first-line replenishment. |
| **GRT pool** | Stock in SLOCs classified `GRT` (e.g., `V02_GRT`) — growth/reserve inventory for expansion allocations. |
| **ALLOC_TYPE** | The pool a run consumes: `FRESH` or `GRT`. Mandatory per allocation run; stamped on every output row. |
| **ST_STATUS** | Store lifecycle status on `Master_ALC_INPUT_ST_MASTER`: `OLD`, `NEW`, `UPC` (upcoming — not yet trading). |
| **SEG** | Merchandise segment: `APP` (apparel) or `GM` (general merchandise). |
| **Hold suppression** | Per-run rule that prevents TBL warehouse hold creation for a defined store/SEG population. |
| **SLOC_TYPE** | New classification column on `ARS_MSA_SLOC_SETTINGS`: `FRESH` \| `GRT` — every SLOC belongs to exactly one pool (no shared/`BOTH` classification). |

---

## 3. Business Objectives

| ID | Objective | Success Measure |
|---|---|---|
| BO-F1 | Allocate fresh and growth stock as separate, intentional runs from one MSA. | Every allocation batch carries `ALLOC_TYPE`; pool used per article equals the classified SLOC column sum. |
| BO-F2 | Stop locking warehouse stock for stores that cannot receive it. | Zero `ARS_NL_TBL_HOLD_TRACKING` rows created for UPC stores when suppression is on. |
| BO-F3 | Give planners independent APP/GM control over TBL hold. | GM-only (or APP-only) hold runs verifiably produce `HOLD_QTY = 0` for the switched-off SEG. |
| BO-F4 | Preserve existing behaviour when no new option is exercised. | A run with default toggles and all SLOCs classified `BOTH` produces output identical to pre-change. |
| BO-F5 | Keep the audit trail intact. | `ALLOC_TYPE` traceable on `ARS_ALLOC_WORKING` / history for every run. |

---

## 4. Business Requirements

### 4.1 Fresh/GRT pool selection

| ID | Requirement |
|---|---|
| BR-01 | Each SLOC shall carry a business-maintained classification `SLOC_TYPE ∈ {FRESH, GRT}` in `ARS_MSA_SLOC_SETTINGS` — pools are mutually exclusive; no shared classification. A SLOC absent from the settings table is treated as `FRESH` (with a logged warning) until the business classifies it. |
| BR-02 | MSA generation remains a **single run over all SLOCs**; it is not split by pool. The MSA output must continue to expose per-SLOC stock columns. |
| BR-03 | Every allocation run **must** declare `alloc_type` (`FRESH` or `GRT`). There is no default; the UI must block submission until chosen. |
| BR-04 | The warehouse pool available to a run is: stock in SLOCs of the declared type, **minus pending and hold quantities of the same type** (typed deduction: a GRT approval reduces only the GRT pool; FRESH symmetric), floored at zero. Legacy pend/hold records without a type reduce both pools until they drain. |
| BR-05 | If the current MSA contains no SLOC columns of the requested type, the run must be rejected with a clear, actionable message (regenerate MSA including those SLOCs). |
| BR-06 | Sequential runs against one MSA must not double-allocate: the existing approve → MSA pending/hold re-sync remains the mechanism (no new machinery). |
| BR-07 | Every row written to `ARS_ALLOC_WORKING` (and flowing to history) shall carry the run's `ALLOC_TYPE`. |

### 4.2 Hold suppression

| ID | Requirement |
|---|---|
| BR-08 | The allocation run shall accept three independent toggles: **Skip hold for UPC stores** (default OFF), **Apply hold for APP** (default ON), **Apply hold for GM** (default ON). |
| BR-09 | A TBL row is *suppressed* when any of: (store `ST_STATUS = 'UPC'` AND skip-UPC is on) OR (`SEG = 'APP'` AND apply-APP is off) OR (`SEG = 'GM'` AND apply-GM is off). |
| BR-10 | A suppressed TBL row behaves like RL/TBC: `OPT_MBQ_WH = OPT_MBQ` (no hold-days uplift) and `HOLD_QTY = 0`. Ship quantity rules are unchanged. |
| BR-11 | Quantity not parked due to suppression remains in the run's pool and stays available to subsequent options in the same run. |
| BR-12 | Suppressed rows create no entries in `ARS_NL_TBL_HOLD_TRACKING` on approval. Non-suppressed rows follow the existing hold-tracking lifecycle unchanged. |
| BR-13 | Default toggle values (OFF / ON / ON) must reproduce current production behaviour exactly. |

### 4.3 Pool-aware approve chain

| ID | Requirement |
|---|---|
| BR-14 | Pending-allocation records created on approval shall carry the run's `ALLOC_TYPE`, so subsequent runs deduct pendings only from the matching pool. |
| BR-15 | TBL warehouse holds created on approval shall carry the run's `ALLOC_TYPE`. Hold draws (RL/TBC `FROM_HOLD`) are type-matched: a FRESH run may consume FRESH and legacy (untyped) holds only; GRT symmetric. |
| BR-16 | The two pools are disjoint by construction (exclusive SLOC classification). As a safety net against mis-attributed deductions (e.g., untyped SAP DO confirmations), each pool remains additionally capped by the total available quantity (`FNL_Q`) so aggregate over-allocation is impossible. |
| BR-17 | Grid budget updates on approval (`PEND_ALC`, `STK_TTL`) remain pool-agnostic — store-side budget capacity is independent of which warehouse pool ships. |
| BR-18 | Manually uploaded pending allocations (Manual Entry, bypassing approve) are untyped and reduce **both** pools — the planner placed that stock outside the pool system. An optional Fresh/GRT selector on Manual Entry is a candidate enhancement. |
| BR-19 | Reverting an approved session must restore the exact pool state: typed pend rows removed, typed holds restored including their type, MSA/grid deltas reversed. A revert followed by re-approve must be indistinguishable from the original approve. |

### 4.4 Pending-allocation operations (added 2026-07-09)

| ID | Requirement |
|---|---|
| BR-20 | Daily DO Entry: when uploaded DO rows carry no allocation/session reference, FIFO deduction remains the default. The user may choose a **session-wise** method — target session first (`SESSION_FIRST`) or target session exclusively (`SESSION_ONLY`) — selecting the session on the upload screen. Reverting a DO upload restores exact prior state regardless of method. |
| BR-21 | Reconciliation "Generate BDC" must be reliable under multi-worker deployment: job status must survive worker routing and restarts; a transient poll failure must not abort the operation view; a client-side failure must not silently strand stamped BDC rows (the next generate's message must say how many rows are blocked by open BDC history). |

---

## 5. Scope

**In scope**
- `ARS_MSA_SLOC_SETTINGS` creation with `SLOC_TYPE` and initial classification of the 29 live SLOCs.
- Pool computation at allocation time (Stage A listing universe + Stage B size-grain pool).
- New allocation-run parameters (`alloc_type` + 3 hold toggles) end-to-end: UI → API → engines.
- `ST_STATUS` and `SEG` availability on the allocation frame.
- Hold suppression in both production engines (`per_opt`, `pandas`).
- `ALLOC_TYPE` stamping on allocation output.

**In scope (added — pool-aware approve, §4.3)**
- `ALLOC_TYPE` marker on `ARS_PEND_ALC`, `ARS_NL_TBL_HOLD_TRACKING`, and allocation history.
- Type-matched hold consumption and typed pool deduction at allocation time.

**Out of scope**
- Splitting ledger *quantities* by pool (each row keeps one quantity; the type is a marker, not a split).
- Typed MSA columns — MSA `PEND_QTY`/`HOLD_QTY`/`FNL_Q` stay total-based; typed deduction is computed at run time.
- Hold-dashboard changes (type filter on views/mutations is a candidate enhancement).
- MSA generation logic and MSA UI (no change).
- Retro-tagging historical rows (legacy rows stay untyped and conservatively reduce both pools until drained).

---

## 6. Business Rules — Worked Example

Article A1, size M, RDC DH. SLOC stock: `V02_FRESH = 140`, `0042 = 20` (both classified FRESH), `V02_GRT = 90` (GRT). Legacy (untyped) pending = 10, hold = 5.

| Run | Pool computation | Available |
|---|---|---|
| FRESH | max((140 + 20) − 10 − 5, 0) | **145** |
| GRT | max(90 − 10 − 5, 0) | **75** |

Hold gate (FRESH run, skip-UPC ON, APP ON, GM ON), TBL `take_pool = 20`, `need_ship = 8`:

| Store | ST_STATUS / SEG | Suppressed? | SHIP | HOLD | Tracking row |
|---|---|---|---|---|---|
| S001 | OLD / APP | No | 8 | 12 | Yes |
| S003 | UPC / APP | Yes (UPC) | 8 | 0 | No |

Typed deduction after approve (BR-04/BR-14): suppose the **GRT** run above (pool 75) allocates 30 and is approved → `PEND(GRT) = 30`. On the next runs against the same MSA:

| Run | Pool computation | Available |
|---|---|---|
| FRESH (unchanged) | max(min(160 − 10 − 5, FNL_Q), 0) | **145** |
| GRT (typed deduction) | max(min(90 − (10+30) − 5, FNL_Q), 0) | **45** |

The GRT approval reduced only the GRT pool. (The pre-existing pend 10 / hold 5 are legacy untyped and reduce both.) BR-16 cap: `FNL_Q` (total available) also shrank by the approval, so even if a deduction were later mis-attributed to the wrong type (e.g., an untyped SAP DO), the aggregate can never exceed physical availability.

---

## 7. Assumptions & Constraints

| ID | Statement |
|---|---|
| AS-1 | `V02_FRESH` and `V02_GRT` SLOCs are authoritative physical fresh/growth bins; business will classify the remaining 27 SLOCs (seeded `FRESH` until then — GRT bins are the explicit exception). |
| AS-2 | Operators generate MSA including all SLOCs relevant to both pools; the BR-05 guard protects against partial MSAs. |
| AS-3 | Legacy (untyped) pend/hold rows reduce both pools until they drain via DO confirmation / hold closure; a transition period of slightly conservative pools is accepted. |
| AS-4 | `per_opt` is the production allocation mode; `pandas` remains selectable and must behave identically for these features. |
| CN-1 | No change to SAP interfaces (BDC/DO) — `ALLOC_TYPE` is internal metadata. |
| CN-2 | Backward compatibility (BR-13) is a hard release gate. |

---

## 8. Risks

| ID | Risk | Mitigation |
|---|---|---|
| RK-1 | Mis-classified SLOC diverts stock to the wrong pool. | Seeded defaults are conservative (`BOTH`); classification editable in `ARS_MSA_SLOC_SETTINGS`; pool composition visible per run in logs. |
| RK-2 | GRT run against an MSA generated with fresh-only SLOC selection silently allocates nothing. | BR-05 hard guard with explicit message. |
| RK-3 | Suppression logic diverges between the two engines. | Single shared suppression mask specification (FSD §6); parity test in acceptance suite. |
| RK-4 | Legacy untyped pend/hold temporarily under-states both pools during transition. | Accepted (AS-3); drains naturally; monitored via allocation fill-rate reporting. |
| RK-5 | Typed hold consumption strands legacy holds if type-matching is too strict. | Legacy NULL holds are drawable by both run types, consumed before typed holds (FSD FS-09). |
| RK-6 | The generic Upload Data page can write pend/hold/MSA tables with no sync, no operations log, and no type discipline — silent pool corruption is possible. | Pre-existing gap flagged to the owner (FSD IM-10); restrict the page's table allowlist as a separate governance action. |
| RK-7 | SAP DO confirmations carry no pool type; FIFO application may close a pend row of the other type when both are open for one (RDC, store, article). | Accepted — temporary mis-attribution reconciles at the next MSA generation (FSD IM-9); visible in the reconciliation report. |

---

## 9. Success Criteria (Acceptance Summary)

1. Migration seeds 29 SLOCs; `V02_FRESH → FRESH`, `V02_GRT → GRT`, rest `FRESH` (business refines).
2. One MSA supports consecutive FRESH and GRT runs with pool values matching §6 arithmetic (spot-checked by SQL on 5 articles).
3. Mandatory `alloc_type`: UI blocks Generate until selected; API rejects requests without it.
4. UPC suppression: all TBL rows for UPC stores show `HOLD_QTY = 0` and `OPT_MBQ_WH = OPT_MBQ`; no tracking rows on approve.
5. SEG suppression: switched-off SEG shows `HOLD_QTY = 0` on TBL rows; the other SEG is unaffected; symmetric when flipped.
6. Regression: default toggles + all-`FRESH` classification + FRESH run ⇒ output identical to pre-change baseline (SHIP/HOLD totals per scope).
7. Every output row stamped with the run's `ALLOC_TYPE`.
8. Typed deduction: approving a GRT run leaves the FRESH pool unchanged on the next run (and vice versa); aggregate allocation can never exceed total availability (FNL_Q cap).
9. Typed holds: a FRESH-created hold is drawable only by FRESH runs (legacy untyped holds by both).

---

*Detailed functional design, data model, API contract, and test cases: see `FSD_FRESH_GRT_HOLD_CONTROL.md`.*
