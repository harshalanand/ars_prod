# Cap-validation uses MBQ only (drop MBQ_WH excess from TBL eligibility checks)

**Date:** 2026-06-30
**Status:** Approved — implementation in same commit
**Area:** rule_engine_per_opt.py (per-OPT mode only)

## Problem

Primary-cap and sec-cap eligibility checks for TBL OPTs currently compare the
WH-inflated dispatch quantity (`SZ_MBQ_WH`-derived = display + RDC-hold) against
a ceiling that is itself MBQ-only (`MBQ_ORIG × cap_pct`). That asymmetry causes
TBL OPTs to be skipped for "exceeding" a ceiling on the strength of units that
go to the RDC hold buffer, not the store display.

Observed example — session `20260629_154555_543`, `WERKS='hb20'`,
`GEN_ART_NUMBER=1121116625`, `CLR='onn'`:

    SKIPPED by primary-cap | grid=MJ
        | stock 265 + already_shipped_this_run 36 + this_OPT 84 = 385
        | would exceed ceiling 317 (= MBQ_ORIG 289 x 110%) by 68
        | intended_ship=84 -> final_ship=0 | pool_untouched=true

Both `this_OPT` (84 = `want_ship_pak` + `want_hold_pak`) and `already_shipped`
(36 = prior OPTs' `round_ship` + `round_hold`) absorb the RDC-hold portion.
Stock (265) is store-side and already MBQ-honest — MBQ_WH lives at RDC, not
at the store.

## Rule (operator-stated, 2026-06-30)

- `MBQ` = display + (max_daily_sale × ACS_D days of cover) — the real store need.
- `MBQ_WH` = MBQ + (sale_per_day × hold_days) — the *extra* sits at RDC, not at
  the store, and only differs from MBQ for **TBL OPTs on first-time dispatch**.
- `MJ_REQ` already deducts only against MBQ (not MBQ_WH).
- Eligibility validations (primary-cap, sec-cap, all grids) MUST compare
  against MBQ only. The MBQ_WH excess is invisible to every cap check.

## Fix

Two single-line changes, both in
[backend/app/services/rule_engine_per_opt.py](../../../backend/app/services/rule_engine_per_opt.py),
TBL-only by effect (RL / TBC `opt_need` is already MBQ-only and their
`round_hold` is zero, so the same lines compile down to identical behavior
for them — no carve-out needed).

### Change A — Step 5c.6, `intended_ship_sc`

Before (~line 928):

    intended_ship_sc = float(opt_need.sum())

After:

    if ot == 'TBL' and want_ship_pak is not None:
        intended_ship_sc = float(want_ship_pak.sum())
    else:
        intended_ship_sc = float(opt_need.sum())

`want_ship_pak` is built at step 5c.5 (~line 774) and carries the pak-rounded
**MBQ-only** target (`need_ship`-derived). `opt_need` for TBL = `want_ship_pak
+ want_hold_pak` — the latter is the MBQ_WH excess and must not enter cap
arithmetic.

### Change B — Step 5g.1, `actual_moved` counter advance

Before (~line 1091):

    if sec_cap_state is not None and participating_grids:
        actual_moved = float(round_ship.sum()) + (
            float(round_hold.sum()) if ot == 'TBL' else 0.0
        )

After:

    if sec_cap_state is not None and participating_grids:
        actual_moved = float(round_ship.sum())

`round_hold` for TBL is the RDC-hold draw — by the rule above it is invisible
to cap arithmetic, so the running counter never absorbs it. `round_ship` is
already the MBQ-only display draw for both TBL (`want_ship_pak` clamped to
pool) and RL / TBC (`effective_ship`, with `from_hold` double-count already
removed by the 2026-06-30 fix).

### Comment touch-up

The block comment at 5g.1 ("We count TBL hold reservations because they
reduce future dispatch headroom in the grain.") is now wrong and gets
replaced with the new invariant statement.

## Out of scope

- **TBL dispatch math.** `round_ship + round_hold` still ships from pool at
  first dispatch — only the *visibility* to cap arithmetic changes.
- **`MJ_STK_TTL` / stock LHS.** MBQ_WH is at RDC, not at the store, so the
  store-side STK_TTL is already MBQ-honest.
- **Skip-remark template.** The numbers it prints will simply be smaller and
  MBQ-honest; no code change to the format.
- **`rule_engine_new.py`** (legacy SQL/Stage-C path). Search for
  `round_hold.sum` / `intended_ship_sc` returns no matches — this fix is
  per-OPT-mode-only.

## Verification

Replay session `20260629_154555_543` with the fix.

For each TBL OPT that today emits `SKIPPED by primary-cap` or
`SKIPPED by sec-cap`:

1. `intended_ship` in the new remark equals `want_ship_pak.sum()` (display
   only), strictly ≤ the old inflated value.
2. The `already_shipped_this_run` term in the new remark for every *later*
   OPT at the same grain is strictly ≤ the old value (drops by the sum of
   prior OPTs' `round_hold`).
3. OPTs whose new LHS now fits under the ceiling ship the **same**
   `round_ship + round_hold` as they would have shipped pre-fix had the cap
   not blocked them. Dispatch behavior under the cap is unchanged.
4. OPTs whose new LHS still breaches the ceiling still SKIP — but the new
   inequality message accurately reflects the display-side breach.

## Risk

Very low. Two pure-Python lines inside an already-isolated per-OPT loop.
TBL-only by effect. No data/SQL change. No interface change. No new column
or table.

## References

- Supersedes the TBL half of `project_sec_cap_from_hold_double_count_fix.md`
  (2026-06-30) — that fix removed the RL/TBC `from_hold` double-count;
  this fix removes the TBL `round_hold` cap absorption.
- Related: `project_rl_tbc_ship_ceiling_fix.md` (2026-06-26) — RL/TBC
  ship-ceiling enforcement, still required.
- Related: `2026-06-30-sec-cap-empty-grid-hard-block-design.md` — sibling
  spec from same date covering the hard-block branch.
