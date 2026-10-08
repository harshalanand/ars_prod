---
title: I_ROD-aware eligibility (OPT_REQ_ROD)
tags: [ars, listing, rule-engine, irod, br-16, fs-10]
updated: 2026-08-13
---

# BR-16 / FS-10 — I_ROD-aware option eligibility (`OPT_REQ_ROD`)

**Status:** implemented 2026-08-13 (branch `ars_v2`, dev `ARSDBPRO`). Both switches default **OFF** — deployed behaviour is identical to pre-FS-10 until one is turned on. Paired runs pending.

## Why

The option-level demand gate tested **one round**; `I_ROD` (how many dispatch rounds an option is entitled to) was applied **only inside the engine, at size grain**. So an option whose store stock covers round 1 but not its full entitlement was rejected as "no demand" and the engine never saw it — even though, had it been admitted, the existing round loop would have shipped it in round 2.

The manual already documented the intent (*"within an OPT, I_ROD rounds scale demand (`OPT_MBQ × N`)"*). The size-level target honoured it; the eligibility gate did not.

**Worked example** — `HS11 / 1241092810 / SKY_BLU`, `MAJ_CAT=LS_NAIL_PAINT`:

```
OPT_TYPE=RL   I_ROD=2   OPT_MBQ=9   STK_TTL=12   MSA_FNL_Q=1248
OPT_REQ_WH  = MAX(0, 9 − 12)  = 0    → NO_DEMAND / R05_REQ_POS → 0 alloc rows
OPT_REQ_ROD = MAX(0, 18 − 12) = 6    → admitted, ships in round 2
```

## Formula (Part 4c, `ARS_LISTING`)

```
rod_target  = OPT_MBQ_WH + (I_ROD−1) × OPT_MBQ   if OPT_TYPE = 'TBL'
            = I_ROD × OPT_MBQ                    otherwise
OPT_REQ_ROD = MAX(0, ROUND(rod_target − STK_TTL, 0))       # I_ROD = 0 counts as 1
```

| Element | Why |
|---|---|
| TBL counts the hold buffer **once** (`MBQ_WH + (I_ROD−1) × MBQ`) | Mirrors `rule_engine_per_opt.py:837`. `I_ROD × OPT_MBQ_WH` would multiply `hold_days` by the round count. Dev check: `HM25` TBL `I_ROD=2 MBQ=20 MBQ_WH=28` → **48**, not 56. |
| `NULLIF(I_ROD,0) → 1` | Many R05-blocked rows carry `I_ROD = 0`; otherwise they evaluate to a negative entitlement. |
| `MAX(0, …)` + `ROUND(…,0)` | Same clamp and rounding as `OPT_REQ` / `OPT_REQ_WH`. |

Computed immediately after the `OPT_REQ_WH` UPDATE — the earliest point where `OPT_MBQ`, `OPT_MBQ_WH`, `OPT_TYPE` (Part 3.6) and `I_ROD` (Part 3.5a) are all populated. Idempotent `ALTER TABLE … ADD`; the table is dropped and recreated every run, so **no migration script**.

## Three gates, one switch

| Layer | Where | Predicate |
|---|---|---|
| 1 | Part 6.6 `gate_demand` → `NO_DEMAND` | `OPT_REQ_ROD >= 1` |
| 2 | Stage A `R05_REQ_POS` (`rule_engine_new._stage_a_apply_rules`) | `OPT_REQ_ROD < 1` fires |
| 3 | `/create-final` extract filter | `OPT_REQ_ROD >= :min_req` |

⚠ **They must agree.** If Layer 1 admits a row Layer 2 rejects (or the reverse), Part 7 silently drops it whenever `shift_all_to_working` is off, because that shift filters `ELIG_FLAG = 1`. The shared helper `listing._irod_gate_on()` exists so every call site resolves the switch identically. Each layer falls back to `OPT_REQ_WH` when the column is absent, so pre-FS-10 snapshots stay readable.

**Reason codes are unchanged.** `NO_DEMAND` and `R05_REQ_POS` keep their strings and CASE-ladder positions — only the arithmetic tightens, from *"needs nothing in round 1"* to *"needs nothing across all I_ROD rounds"*.

## `NO_REQ` label alignment

`SZ_REQ` stays a 1-round figure. Without this, a newly admitted option shipping nothing because the pool is empty would be labelled `NO_REQ` instead of `NO_POOL_MSA` — the classifier tests `SZ_REQ` before the pool, pointing reviewers at demand when the problem is supply.

The `NO_REQ` branch (`rule_engine_pandas.py`, `rule_engine_new.py`) now uses the same I_ROD target as the `ALREADY_STOCKED` branch directly above it, making `NO_REQ` **unreachable by construction**: no I_ROD demand → `ALREADY_STOCKED`; anything else shipping zero → supply. Label-only, no quantity moves, and gated so a flag-off run stays byte-identical.

## Out of scope — deliberate

| Item | Reason |
|---|---|
| `OPT_REQ`, `OPT_REQ_WH` | Feed excess arithmetic and `OPT_PRIORITY_RANK` ordering. Redefining them silently reorders dispatch and rewrites every published report. |
| `MJ_REQ`, `MJ_MBQ`, R09 headroom, `*_mj_req_cap_pct`, sec-caps | Stay 1-round. Newly admitted options compete inside **today's** MAJ_CAT budget rather than enlarging it. Measure first. |
| `rule_engine_per_opt.py` | Correct as-is — band mask has no `SZ_REQ` filter, and a size receiving nothing in round 1 is never stamped `SKIPPED`, so an admitted row is live for round 2. |
| `SZ_REQ` / `SZ_REQ_WH` values | Only the `NO_REQ` label moved. |

## Configuration

| Switch | Where | Default |
|---|---|---|
| `RULE_R05_USE_IROD` | `rule_engine_new.py` flags block | `False` |
| `use_irod_eligibility` | `GenerateRequest`, per run | `False` |

OR'd. The per-run value is stamped into `ARS_RUN_PARAMS_AUDIT` + session `REQUEST_JSON` and threaded to the engine and the `/retry-failed` path. Rollback = set either to `False` and re-run; the column is additive and nullable.

## Propagation gotcha

`_FINAL_KEEP_COLS` must list `OPT_REQ_ROD` **explicitly** — the `_FINAL_KEEP_SUFFIX = {"_REQ"}` pattern does **not** match a name ending `_ROD`. The same trap sits in the SLOC auto-detector: `_ROD` isn't in `bad_suffixes` either, so the column must also join the `non_sloc` set or it gets misread as stock. Parked/history reconcile themselves via `_reconcile_history_columns()`.

## Static verification — dev `ARS_LISTING`, 2,728,342 rows

| Invariant | Result |
|---|---|
| `OPT_REQ_WH >= 1` but `OPT_REQ_ROD = 0` (regressions) | **0** ✅ |
| Admission differs at `I_ROD <= 1` | **0** ✅ |
| Admitted while `STK_TTL >= I_ROD × OPT_MBQ` (AC-3) | **0** ✅ |

| OPT_TYPE | sole-R05 blocked | newly admitted | `MSA_FNL_Q > 0` |
|---|---|---|---|
| **RL** | 108,080 | **17,226** | 16,565 |
| TBC | 15 | 0 | 0 |
| TBL | 500 | 0 | 0 |

TBC and TBL never reach this gate — their stock always clears the 1-round test.

**Still to run:** paired runs (flag off → on, same parameters) for AC-1 / AC-5 / AC-6 and the runtime delta (~+5%; engine cost scales with OPT count).

## Open items (business)

1. Should `MJ_REQ` / R09 headroom also become I_ROD-aware? Deferred pending paired-run measurement.
2. Do the ~2.3% of OPTs where the **size-level** gap exceeds the OPT-level gap justify a size-aware gate? It would need `ARS_GRID_MJ_VAR_ART` joined at listing time, since `SZ_MBQ` doesn't exist until Stage B — after admission.

## Cross-links

[[Listing]] · [[Rule Engine (per_opt)]] · [[Allocation Waterfall (Step by Step)]] · [[ARS Glossary]] · [[2026-08-13]]
