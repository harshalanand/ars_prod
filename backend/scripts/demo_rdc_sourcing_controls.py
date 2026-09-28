#!/usr/bin/env python
"""
What each control in the RDC SOURCING panel actually does.

The panel appears in the cockpit ONLY when RDC Scope = All RDCs, because that
is the only mode where the run gets a choice about which warehouse ships a
line. Own and Cross always ship from the store's own warehouse.

Every table below is produced by rdc_split_service._walk_lines — the same
function Part 8.37 runs — so these are real outcomes, not illustrations.

    python scripts/demo_rdc_sourcing_controls.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.rdc_split_service import (          # noqa: E402
    SINGLE_PREFERRED, SINGLE_STRICT, SPLIT_ALWAYS,
    _walk_lines, parse_priority,
)

A, B, C, D = "DW01", "DH24", "RDC_C", "RDC_D"


def line(store, store_rdc, var, sz, ship=0.0, hold=0.0):
    return (store, store_rdc, "LS_M_CAP", 1130140930, "BLK",
            var, sz, ship, hold)


def walk(lines, avail, live, priority, policy, cap):
    ledger = dict(avail)
    s = _walk_lines(lines, ledger, list(live), list(priority), policy, cap)
    return s, ledger


def show(s, live, indent="      "):
    rows = sorted(s["_emit"].items(), key=lambda x: (x[0][0], x[0][3]))
    for (w, v, sz, rdc), e in rows:
        own = e["STORE_RDC"]
        tag = "own" if rdc == own else "CROSS"
        print(f"{indent}{w} {sz:<4} <- {rdc:<6} {e['SHIP_QTY']:>4.0f}  {tag}")
    picks = "  ".join(f"{r}={s['by_rdc'][r]['ship']:.0f}" for r in live)
    print(f"{indent}picks: {picks}   split lines={s['split_lines']}"
          f"   shipped={s['ship_tagged']:.0f}")
    for r in s["_reductions"]:
        print(f"{indent}! REDUCED {r['WERKS']} {r['SZ']}: {r['alloc']:.0f} -> "
              f"{r['tagged']:.0f}   {r['remark']}")


# ---------------------------------------------------------------------------
def control_1_split_policy():
    print("=" * 76)
    print("CONTROL 1 — Split policy      (the three radio buttons)")
    print("=" * 76)
    print("""
  Decides what happens when NO single warehouse can cover a whole line.

  Stock for size L:   DW01 = 5     DH24 = 20
  Three stores need:  HH15 (own DW01) 6 · HX55 (own DW01) 12 · HN40 (own DH24) 6
                      total demand 24, total stock 25
""")
    avail = {(A, 7003, "L"): 5, (B, 7003, "L"): 20}
    lines = [line("HH15", A, 7003, "L", ship=6),
             line("HX55", A, 7003, "L", ship=12),
             line("HN40", B, 7003, "L", ship=6)]
    for policy, label in ((SPLIT_ALWAYS, "Split whenever short"),
                          (SINGLE_PREFERRED, "Single source preferred  [DEFAULT]"),
                          (SINGLE_STRICT, "Strict single source")):
        print(f"  ---- {label} ----")
        s, _ = walk(lines, avail, [A, B], [A, B], policy, 2)
        show(s, [A, B])
        print()
    print("""  Reading it:
    Split whenever short     — takes what the first warehouse has, then tops up.
                               Ships all 24. Splits the FIRST short line.
    Single source preferred  — looks for one warehouse that can cover the whole
                               line; only splits as a last resort. Ships all 24,
                               but defers the split to the LAST line that cannot
                               be covered whole.
    Strict single source     — never splits. HN40's 6 cannot be covered whole by
                               either warehouse, so the line is REDUCED to 5 and
                               one piece of real demand is lost.

  Both non-strict policies dispatch the same total with the same number of
  split lines. They differ only in WHICH line splits.
""")


def control_2_max_split():
    print("=" * 76)
    print("CONTROL 2 — Max warehouses / line      (the 1 / 2 / 3 / 4 selector)")
    print("=" * 76)
    print("""
  Caps how many warehouses may be combined to fill ONE store-size — i.e. how
  many separate pick documents one line can generate.

  Shown with FOUR warehouses so the cap can actually bind.
  Stock for size M:   DW01 = 4   DH24 = 3   RDC_C = 2   RDC_D = 1
  One store needs 10 (own DW01).   Total stock = 10.
""")
    avail = {(A, 7001, "M"): 4, (B, 7001, "M"): 3,
             (C, 7001, "M"): 2, (D, 7001, "M"): 1}
    lines = [line("HX01", A, 7001, "M", ship=10)]
    for cap in (1, 2, 3, 4):
        note = "  (= Strict single source)" if cap == 1 else (
            "  [DEFAULT]" if cap == 2 else "")
        print(f"  ---- cap = {cap}{note} ----")
        s, _ = walk(lines, avail, [A, B, C, D], [A, B, C, D], SPLIT_ALWAYS, cap)
        show(s, [A, B, C, D])
        print()
    print("""  Reading it:
    Every warehouse added lets the line reach further, so less demand is lost —
    but each one is another pick document and another dispatch for one store.
    cap=1 is exactly Strict single source.

  With only TWO warehouses live a cap of 2 can never bind, which is why the
  panel shows the note 'Only 2 warehouses live — a cap cannot bind.'
""")


def control_3_fallback_order():
    print("=" * 76)
    print("CONTROL 3 — Fallback order      (read-only; Settings -> Business Rules)")
    print("=" * 76)
    print("""
  The store's OWN warehouse is always tried first. This setting only decides
  the order of the REST — and for a store with no RDC tag, the whole order.

  Stock for size S:   DW01 = 15   DH24 = 20   (both can serve)
  HP09 is UNTAGGED and needs 3.
""")
    avail = {(A, 7002, "S"): 15, (B, 7002, "S"): 20}
    lines = [line("HP09", "", 7002, "S", ship=3)]
    for raw in ("DW01>DH24", "DH24>DW01"):
        pri = parse_priority(raw, [A, B])
        print(f"  ---- ALC_RDC_PRIORITY = {raw} ----")
        s, _ = walk(lines, avail, [A, B], pri, SINGLE_PREFERRED, 2)
        show(s, [A, B])
        print()
    print("""  Reading it:
    The store receives 3 pieces either way — only the SOURCE changes. The order
    matters exactly where two warehouses could both serve the line; where only
    one has stock, availability decides and the setting is irrelevant.

    It is a PREFERENCE, never a filter: a live warehouse missing from the list
    is appended automatically and can never be silently excluded.
""")


def control_4_tagged_store():
    print("=" * 76)
    print("CONTROL 4 — why a TAGGED store barely notices the fallback order")
    print("=" * 76)
    print("""
  Same stock, but the store is tagged to DH24. Own always wins first.
""")
    avail = {(A, 7002, "S"): 15, (B, 7002, "S"): 20}
    lines = [line("HN40", B, 7002, "S", ship=3)]
    for raw in ("DW01>DH24", "DH24>DW01"):
        pri = parse_priority(raw, [A, B])
        print(f"  ---- ALC_RDC_PRIORITY = {raw} ----")
        s, _ = walk(lines, avail, [A, B], pri, SINGLE_PREFERRED, 2)
        show(s, [A, B])
        print()
    print("""  Identical both times: HN40's own warehouse DH24 has the stock, so the
  fallback order is never consulted. It only comes into play once own runs short.
""")


def control_5_holds():
    print("=" * 76)
    print("CONTROL 5 — 'Holds are always sourced from a single warehouse'")
    print("=" * 76)
    print("""
  Not a choice — a constraint. ARS_NL_TBL_HOLD_TRACKING is keyed on
  (WERKS, VAR_ART, SZ, ALLOC_TYPE), so two source rows for one hold would
  collide on the primary key.

  Stock for size M:   DW01 = 3   DH24 = 4
  HH15 (own DW01) needs a HOLD of 5 — neither warehouse can cover it.
""")
    avail = {(A, 8001, "M"): 3, (B, 8001, "M"): 4}
    lines = [line("HH15", A, 8001, "M", hold=5)]
    for policy, label in ((SPLIT_ALWAYS, "Split whenever short"),
                          (SINGLE_PREFERRED, "Single source preferred")):
        print(f"  ---- panel set to: {label} ----")
        s, _ = walk(lines, avail, [A, B], [A, B], policy, 4)
        rows = [(k[3], v["HOLD_QTY"]) for k, v in s["_emit"].items()]
        for rdc, q in rows:
            print(f"      HH15 M  hold {q:.0f} at {rdc}")
        for r in s["_reductions"]:
            print(f"      ! REDUCED HOLD {r['alloc']:.0f} -> {r['tagged']:.0f}"
                  f"   {r['remark']}")
        print()
    print("""  The policy setting is IGNORED for holds. Whatever the panel says, the hold
  lands on the largest single warehouse (DH24, 4) and the remaining 1 is dropped
  and stamped. A hold is only a reservation, so reducing it is safe — unlike
  reducing a shipment.

  Cross-warehouse holds ARE allowed; only splitting one is not.
""")


def main() -> int:
    print("RDC SOURCING — the panel that appears when RDC Scope = All RDCs")
    print("Produced by the real rdc_split_service._walk_lines.\n")
    control_1_split_policy()
    control_2_max_split()
    control_3_fallback_order()
    control_4_tagged_store()
    control_5_holds()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
