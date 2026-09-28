#!/usr/bin/env python
"""
Sourcing-direction matrix — every A/B combination, run through the REAL pass.

Nothing here is hand-drawn: every table is produced by calling
rdc_split_service._walk_lines, the same function Part 8.37 uses in a live run.

Demo 1  the FSD reference option on REAL warehouse stock
Demo 2  the full direction matrix — A->A, A->B, B->A, B->B, splits, fallback
Demo 3  holds — cross-warehouse allowed, splitting forbidden (BR-RDC-12)

    python scripts/step6_sourcing_matrix_demo.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.rdc_split_service import (          # noqa: E402
    SINGLE_PREFERRED, SPLIT_ALWAYS, _walk_lines, parse_priority,
)

A, B = "DW01", "DH24"          # A = DW01, B = DH24
LIVE = [A, B]
PRIORITY = parse_priority("DW01>DH24", LIVE)


def line(store, store_rdc, var, sz, ship=0.0, hold=0.0,
         mc="M_W_SHIRT_HS", ga=1110116457, clr="OFF_WHT"):
    return (store, store_rdc, mc, ga, clr, var, sz, ship, hold)


def show_stock(title, avail):
    print(f"\n  {title}")
    sizes = sorted({k[2] for k in avail})
    print("    %-8s %s" % ("RDC", "".join(f"{s:>8}" for s in sizes) + f"{'TOTAL':>9}"))
    for rdc in LIVE:
        row, tot = "", 0.0
        for s in sizes:
            q = sum(v for k, v in avail.items() if k[0] == rdc and k[2] == s)
            row += f"{q:>8.0f}"
            tot += q
        print(f"    {rdc:<8}{row}{tot:>9.0f}")


def run(title, lines, avail, policy=SINGLE_PREFERRED, cap=2, note=None):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)
    if note:
        print(f"  {note}")
    before = dict(avail)
    show_stock("warehouse stock BEFORE", before)

    print("\n  allocation to source:")
    print("    %-7s %-7s %-6s %-6s %-6s" % ("STORE", "BELONGS", "SZ", "SHIP", "HOLD"))
    for l in lines:
        print("    %-7s %-7s %-6s %-6.0f %-6.0f" % (l[0], l[1] or "(none)", l[6], l[7], l[8]))

    ledger = dict(avail)
    s = _walk_lines(lines, ledger, LIVE, PRIORITY, policy, cap)

    print("\n  RESULT — who ships what:")
    print("    %-7s %-7s %-6s %-6s %-6s %-6s %s"
          % ("STORE", "BELONGS", "SZ", "FROM", "SHIP", "HOLD", "DIRECTION"))
    for (w, v, sz, rdc), e in sorted(s["_emit"].items(), key=lambda x: (x[0][0], x[0][2], x[0][3])):
        store_rdc = e["STORE_RDC"] or "(none)"
        if not e["STORE_RDC"]:
            direction = f"untagged -> {rdc}  (tier {e['PREF_TIER']})"
        elif rdc == e["STORE_RDC"]:
            direction = f"{rdc} -> {rdc}   own"
        else:
            direction = f"{e['STORE_RDC']} store <- {rdc}   CROSS"
        print("    %-7s %-7s %-6s %-6s %-6.0f %-6.0f %s"
              % (w, store_rdc, sz, rdc, e["SHIP_QTY"], e["HOLD_QTY"], direction))

    show_stock("warehouse stock AFTER", ledger)

    alloc = sum(l[7] + l[8] for l in lines)
    tagged = s["ship_tagged"] + s["hold_tagged"]
    print(f"\n  allocated {alloc:.0f}  tagged {tagged:.0f}  reduced {s['reduced_qty']:.0f}"
          f"  -> conserved: {abs(tagged + s['reduced_qty'] - alloc) < 1e-6}")
    print(f"  pick by warehouse: "
          + "  ".join(f"{r}={s['by_rdc'][r]['ship']:.0f}" for r in LIVE))
    print(f"  split lines {s['split_lines']}   cross lines {s['cross_lines']}"
          f"   reduced lines {s['reduced_lines']}")
    for r in s["_reductions"]:
        print(f"    ! REDUCED {r['WERKS']} {r['SZ']} {r['draw']}: "
              f"{r['alloc']:.0f} -> {r['tagged']:.0f}   {r['remark']}")
    return s


# ---------------------------------------------------------------------------
def demo1_real():
    """REAL stock, REAL option: ARS_MSA_VAR_ART 2026-09-22.
    M_W_SHIRT_HS / 1110116457 / OFF_WHT — 220 pcs, ALL of it at DH24."""
    avail = {
        (B, 5001, "S"): 63, (B, 5002, "M"): 148, (B, 5003, "L"): 7,
        (B, 5004, "XL"): 1, (B, 5005, "2XL"): 1,
        (A, 5001, "S"): 0,  (A, 5002, "M"): 0,   (A, 5003, "L"): 0,
        (A, 5004, "XL"): 0, (A, 5005, "2XL"): 0,
    }
    lines = [
        line("HH15", A, 5001, "S",  ship=6),    # DW01 store — own has zero
        line("HH15", A, 5002, "M",  ship=10),
        line("HX55", A, 5003, "L",  ship=3),
        line("HN40", B, 5001, "S",  ship=8),    # DH24 store — own has it
        line("HN40", B, 5002, "M",  ship=12),
    ]
    run("DEMO 1 — REAL option on REAL stock:  M_W_SHIRT_HS / 1110116457 / OFF_WHT",
        lines, avail,
        note=("Live ARS_MSA_VAR_ART: DH24 holds all 220 pcs, DW01 holds ZERO.\n"
              "  Today every DW01 store is blocked from this option entirely.\n"
              "  NOTE the FSD calls this option M_TEES_HS; the live MAJ_CAT is M_W_SHIRT_HS."))


def demo2_matrix():
    """Every sourcing direction in one run."""
    avail = {
        (A, 7001, "S"): 10, (B, 7001, "S"): 10,   # both stocked
        (A, 7002, "M"): 0,  (B, 7002, "M"): 40,   # B only
        (A, 7003, "L"): 25, (B, 7003, "L"): 0,    # A only
        (A, 7004, "XL"): 4, (B, 7004, "XL"): 3,   # neither covers 6 alone
    }
    lines = [
        line("HH15", A, 7001, "S",  ship=4),   # A->A  own covers
        line("HN40", B, 7001, "S",  ship=4),   # B->B  own covers
        line("HH15", A, 7002, "M",  ship=8),   # A->B  own empty, cross
        line("HN40", B, 7003, "L",  ship=6),   # B->A  own empty, reverse cross
        line("HH15", A, 7004, "XL", ship=6),   # A->A+B  neither covers -> split
        line("HP09", "", 7001, "S", ship=2),   # untagged -> fallback order
    ]
    run("DEMO 2 — the full direction matrix (SPLIT_ALWAYS so splits are visible)",
        lines, avail, policy=SPLIT_ALWAYS,
        note="One line per direction: own-to-own both ways, cross both ways, a split, and an untagged store.")


def demo3_holds():
    """BR-RDC-12 — a hold may cross warehouses but may never be split."""
    avail = {(A, 8001, "M"): 3, (B, 8001, "M"): 9}
    lines = [
        line("HH15", A, 8001, "M", ship=5, hold=2),   # ship crosses, hold stays own
        line("HN40", B, 8001, "M", ship=4, hold=1),   # own empty by now -> hold crosses
    ]
    run("DEMO 3a — SHIP and HOLD share ONE ledger; a cross-warehouse hold is allowed",
        lines, avail,
        note="SHIP is tagged before HOLD on each line, both drawing the same balance.")

    avail2 = {(A, 8001, "M"): 3, (B, 8001, "M"): 4}
    lines2 = [line("HH15", A, 8001, "M", hold=5)]
    run("DEMO 3b — a hold NO single warehouse can cover is REDUCED, never split",
        lines2, avail2,
        note=("The hold tracker's PK is (WERKS, VAR_ART, SZ, ALLOC_TYPE) — two source\n"
              "  rows for one hold would collide, so the largest single warehouse takes it\n"
              "  and the remainder is dropped and stamped."))


def main() -> int:
    print("RDC sourcing matrix — every table below is produced by the real")
    print("rdc_split_service._walk_lines, the same code Part 8.37 runs.")
    print(f"A = {A}   B = {B}   fallback order = {'>'.join(PRIORITY)}")
    demo1_real()
    demo2_matrix()
    demo3_holds()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
