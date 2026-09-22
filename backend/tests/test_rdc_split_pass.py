"""
Central RDC Pool — unit tests for the Part 8.37 split pass.

Spec: docs/superpowers/specs/2026-09-21-central-rdc-pool-allocation-brd-fsd-v1.5.md
      §B6.3.2 (algorithm), §B6.4 (preference chain), §B10 (worked examples),
      §B11 (invariants), §B15 (verification plan).

These drive `_walk_lines` directly with a synthetic fixture, so the blocking
gates that do not need a live Generate are provable offline:

    V3   no warehouse is ever over-drawn          (I-3, BR-RDC-04)
    V4   conservation: tagged == allocated        (I-1, I-2)
    V7   SPLIT_ALWAYS vs SINGLE_PREFERRED totals
    V8   determinism
    V9   the MAX_SPLIT cap binds and stamps
    V10  the cap cannot bind at N=2
    V15  a hold is never split                    (I-12, BR-RDC-12)

    run with:  python -m pytest tests/test_rdc_split_pass.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.rdc_split_service import (  # noqa: E402
    SINGLE_PREFERRED, SINGLE_STRICT, SPLIT_ALWAYS,
    _draw, _walk_lines, parse_priority, preference_order,
)

A, B, C, D = "DW01", "DH24", "RDC_C", "RDC_D"


def line(werks, store_rdc, var_art, sz, ship=0.0, hold=0.0,
         maj_cat="M_TEES_HS", gen_art=1110116457, clr="OFF_WHT"):
    """One ARS_ALLOC_WORKING row in the tuple order _walk_lines consumes."""
    return (werks, store_rdc, maj_cat, gen_art, clr, var_art, sz, ship, hold)


def walk(lines, avail, live, priority=None, policy=SINGLE_PREFERRED, cap=2):
    return _walk_lines(lines, dict(avail), list(live),
                       list(priority or []), policy, cap)


def total_tagged(stats):
    return stats["ship_tagged"] + stats["hold_tagged"]


# ---------------------------------------------------------------------------
# §B6.4 — the preference chain
# ---------------------------------------------------------------------------
def test_tier1_own_warehouse_first():
    order, tier = preference_order(A, [A, B], [A, B])
    assert order[0] == A and tier == "1"
    order, tier = preference_order(B, [A, B], [A, B])
    assert order[0] == B and tier == "1", "own RDC must come first even when the priority list disagrees"


def test_tier2_untagged_store_uses_priority():
    for tag in ("", None, "ALL", "NOT_A_RDC"):
        order, tier = preference_order(tag, [A, B], [A, B])
        assert order == [A, B] and tier == "2"


def test_tier3_no_priority_defers_to_availability():
    order, tier = preference_order("", [], [A, B])
    assert order is None and tier == "3", "tier 3 is evaluated per line, not up front"


def test_priority_parse_is_a_preference_not_a_filter():
    # '>' separator (the registry cannot validate a comma inside a value)
    assert parse_priority("DW01>DH24", [A, B]) == [A, B]
    assert parse_priority("DH24>DW01", [A, B]) == [B, A]
    # a comma is still tolerated for a hand-edited legacy value
    assert parse_priority("DH24,DW01", [A, B]) == [B, A]
    # a live RDC missing from the rule is APPENDED, never excluded
    assert parse_priority("DH24", [A, B]) == [B, A]
    # an RDC named in the rule but not live is dropped
    assert parse_priority("DW01>GONE>DH24", [A, B]) == [A, B]
    # unset -> tier 3
    assert parse_priority(None, [A, B]) == []


def test_new_warehouse_can_never_be_silently_excluded():
    """A third RDC goes live and nobody updates the rule."""
    order = parse_priority("DW01>DH24", [A, B, C])
    assert C in order, "an unlisted live warehouse must still be sourceable"
    assert order == [A, B, C]


# ---------------------------------------------------------------------------
# §B10 E1 — the reference case, end to end
# ---------------------------------------------------------------------------
def _e1_fixture():
    #        S(5001)  M(5002)  L(5003)
    # DW01     10        0        5
    # DH24      0       40       20
    avail = {
        (A, 5001, "S"): 10, (A, 5002, "M"): 0,  (A, 5003, "L"): 5,
        (B, 5001, "S"): 0,  (B, 5002, "M"): 40, (B, 5003, "L"): 20,
    }
    lines = [
        line("HS11", A, 5001, "S", ship=4),
        line("HS11", A, 5002, "M", ship=8),
        line("HS11", A, 5003, "L", ship=6),
        line("HS12", A, 5001, "S", ship=4),
        line("HS12", A, 5002, "M", ship=8),
        line("HS12", A, 5003, "L", ship=12),
        line("HP04", B, 5001, "S", ship=2),
        line("HP04", B, 5002, "M", ship=10),
        line("HP04", B, 5003, "L", ship=6),
    ]
    return lines, avail


def test_e1_matches_the_spec_worked_example():
    lines, avail = _e1_fixture()
    s = walk(lines, avail, [A, B], [A, B])

    assert s["ship_tagged"] == 60, "E1 ships 60 of 62 from the clubbed pool"
    assert s["by_rdc"][A]["ship"] == 14, "DW01 picks 14 (spec E1.5)"
    assert s["by_rdc"][B]["ship"] == 46, "DH24 picks 46 (spec E1.5)"
    assert s["split_lines"] == 1, "only HP04's L line splits (spec E1.3 #9)"
    assert s["reduced_lines"] == 0, "the default config is pure tagging for shipments"
    assert len(s["_emit"]) == 10, "9 alloc lines -> 10 split rows, one line split in two"


def test_e1_split_line_is_own_warehouse_first():
    lines, avail = _e1_fixture()
    s = walk(lines, avail, [A, B], [A, B])
    rows = {k: v for k, v in s["_emit"].items() if k[0] == "HP04" and k[2] == "L"}
    assert len(rows) == 2, "HP04's L line is sourced from both warehouses"
    by_rdc = {k[3]: v["SHIP_QTY"] for k, v in rows.items()}
    assert by_rdc[B] == 2, "own warehouse (DH24) contributes what it has first"
    assert by_rdc[A] == 4, "the shortfall comes from DW01"


def test_e1_cross_ship_flag_is_set_correctly():
    lines, avail = _e1_fixture()
    s = walk(lines, avail, [A, B], [A, B])
    for (werks, _v, _sz, rdc), row in s["_emit"].items():
        expect = 0 if rdc == row["STORE_RDC"] else 1
        assert row["IS_CROSS"] == expect, f"{werks}/{rdc} IS_CROSS wrong"
    # HS11 M, HS11 L, HS12 M, HS12 L (DW01 stores served by DH24)
    # + HP04 S (DH24 store served by DW01) + HP04 L (split, part from DW01)
    assert s["cross_lines"] == 6


# ---------------------------------------------------------------------------
# V3 / V4 — the two invariants that gate release
# ---------------------------------------------------------------------------
def test_v3_no_warehouse_is_ever_overdrawn():
    lines, avail = _e1_fixture()
    ledger = dict(avail)
    _walk_lines(lines, ledger, [A, B], [A, B], SINGLE_PREFERRED, 2)
    for key, bal in ledger.items():
        assert bal >= -1e-9, f"{key} went negative: {bal}"


def test_v4_conservation_tagged_equals_allocated():
    lines, avail = _e1_fixture()
    s = walk(lines, avail, [A, B], [A, B])
    allocated = sum(l[7] + l[8] for l in lines)
    assert total_tagged(s) + s["reduced_qty"] == allocated, (
        "every allocated piece is either tagged to a warehouse or explicitly reduced"
    )


def test_v4_holds_conserve_too():
    avail = {(A, 5002, "M"): 3, (B, 5002, "M"): 9}
    lines = [
        line("HS11", A, 5002, "M", ship=5, hold=2),
        line("HP04", B, 5002, "M", ship=4, hold=1),
    ]
    s = walk(lines, avail, [A, B], [A, B])
    assert s["ship_tagged"] == 9 and s["hold_tagged"] == 3
    assert total_tagged(s) == 12, "spec E3: the 12 allocated pieces consume the 12 available"


def test_ship_and_hold_share_one_ledger():
    """Spec E3 — two independent passes would each see the full balance and
    together book 12 pieces from a warehouse holding 9."""
    avail = {(A, 5002, "M"): 3, (B, 5002, "M"): 9}
    lines = [line("HS11", A, 5002, "M", ship=5, hold=2),
             line("HP04", B, 5002, "M", ship=4, hold=1)]
    ledger = dict(avail)
    _walk_lines(lines, ledger, [A, B], [A, B], SINGLE_PREFERRED, 2)
    assert ledger[(B, 5002, "M")] >= 0, "DH24 must not be double-booked"
    assert sum(ledger.values()) == 0, "all 12 pieces consumed exactly"


# ---------------------------------------------------------------------------
# V15 — BR-RDC-12, a hold is never split
# ---------------------------------------------------------------------------
def test_v15_hold_is_never_split_even_under_split_always():
    """The hold tracker PK is (WERKS, VAR_ART, SZ, ALLOC_TYPE) — two source
    rows for one hold would collide. So a hold no single warehouse can cover
    is sourced from the largest and REDUCED."""
    avail = {(A, 5002, "M"): 3, (B, 5002, "M"): 4}
    lines = [line("HS11", A, 5002, "M", hold=5)]
    for policy in (SPLIT_ALWAYS, SINGLE_PREFERRED, SINGLE_STRICT):
        s = walk(lines, avail, [A, B], [A, B], policy=policy, cap=4)
        rows = [k for k in s["_emit"] if k[0] == "HS11"]
        assert len(rows) == 1, f"{policy}: a hold must land on exactly one warehouse"
        assert s["hold_tagged"] == 4, f"{policy}: largest single source (DH24=4) takes it"
        assert s["reduced_lines"] == 1 and s["reduced_qty"] == 1
        assert "RDC_HOLD_SHORT" in s["_reductions"][0]["remark"]


def test_hold_prefers_own_warehouse_when_it_can_cover():
    avail = {(A, 5002, "M"): 9, (B, 5002, "M"): 9}
    s = walk([line("HS11", A, 5002, "M", hold=4)], avail, [A, B], [A, B])
    assert list(s["_emit"])[0][3] == A, "own warehouse first for holds too"
    assert s["reduced_lines"] == 0


def test_cross_warehouse_hold_is_still_allowed():
    """D2 permits a cross-RDC hold. Only SPLITTING one is forbidden."""
    avail = {(A, 5002, "M"): 0, (B, 5002, "M"): 9}
    s = walk([line("HS11", A, 5002, "M", hold=4)], avail, [A, B], [A, B])
    assert list(s["_emit"])[0][3] == B
    assert s["hold_tagged"] == 4 and s["reduced_lines"] == 0


# ---------------------------------------------------------------------------
# V9 / V10 — the MAX_SPLIT cap
# ---------------------------------------------------------------------------
def test_v9_cap_binds_at_four_warehouses_and_stamps_the_line():
    """A line needing three sources under a cap of 2 is reduced and stamped."""
    avail = {(A, 7001, "M"): 4, (B, 7001, "M"): 3,
             (C, 7001, "M"): 2, (D, 7001, "M"): 1}
    s = walk([line("HX01", A, 7001, "M", ship=10)], avail,
             [A, B, C, D], [A, B, C, D], policy=SPLIT_ALWAYS, cap=2)
    assert s["split_lines"] == 1
    assert len(s["_emit"]) == 2, "at most two warehouses per line"
    assert s["ship_tagged"] == 7, "4 from DW01 + 3 from DH24"
    assert s["reduced_lines"] == 1 and s["reduced_qty"] == 3
    assert "RDC_SPLIT_CAPPED" in s["_reductions"][0]["remark"]


def test_v10_cap_of_two_cannot_bind_with_two_warehouses():
    lines, avail = _e1_fixture()
    s = walk(lines, avail, [A, B], [A, B], cap=2)
    assert s["reduced_lines"] == 0, "with 2 RDCs a cap of 2 can never bind"


def test_max_split_one_is_equivalent_to_single_strict():
    avail = {(A, 7001, "M"): 4, (B, 7001, "M"): 3}
    capped = walk([line("HX01", A, 7001, "M", ship=6)], avail,
                  [A, B], [A, B], policy=SPLIT_ALWAYS, cap=1)
    strict = walk([line("HX01", A, 7001, "M", ship=6)], avail,
                  [A, B], [A, B], policy=SINGLE_STRICT, cap=2)
    assert capped["ship_tagged"] == strict["ship_tagged"] == 4
    assert capped["reduced_qty"] == strict["reduced_qty"] == 2


def test_capped_walk_maximises_fill_within_the_cap():
    """With the cap binding, candidates after 'own' are ordered by
    availability descending so the cap buys the most pieces it can."""
    avail = {(A, 7001, "M"): 1, (B, 7001, "M"): 2,
             (C, 7001, "M"): 9, (D, 7001, "M"): 3}
    s = walk([line("HX01", A, 7001, "M", ship=10)], avail,
             [A, B, C, D], [A, B, C, D], policy=SPLIT_ALWAYS, cap=2)
    assert s["ship_tagged"] == 10, "own(1) + largest other(9) fills the line"
    assert s["reduced_lines"] == 0


# ---------------------------------------------------------------------------
# §B6.3 — the three policies (spec E2)
# ---------------------------------------------------------------------------
def _e2_fixture():
    avail = {(A, 5003, "L"): 5, (B, 5003, "L"): 20}
    lines = [line("HS11", A, 5003, "L", ship=6),
             line("HS12", A, 5003, "L", ship=12),
             line("HP04", B, 5003, "L", ship=6)]
    return lines, avail


def test_e2_split_always():
    lines, avail = _e2_fixture()
    s = walk(lines, avail, [A, B], [A, B], policy=SPLIT_ALWAYS)
    assert s["ship_tagged"] == 24 and s["split_lines"] == 1
    assert s["by_rdc"][A]["ship"] == 5 and s["by_rdc"][B]["ship"] == 19


def test_e2_single_preferred_defers_the_split():
    lines, avail = _e2_fixture()
    s = walk(lines, avail, [A, B], [A, B], policy=SINGLE_PREFERRED)
    assert s["ship_tagged"] == 24, "the full 24 still dispatches"
    assert s["split_lines"] == 1, "with exactly one split line, as SPLIT_ALWAYS"
    assert s["by_rdc"][A]["ship"] == 4 and s["by_rdc"][B]["ship"] == 20


def test_e2_single_strict_loses_a_piece():
    lines, avail = _e2_fixture()
    s = walk(lines, avail, [A, B], [A, B], policy=SINGLE_STRICT)
    assert s["split_lines"] == 0, "strict never splits"
    assert s["ship_tagged"] == 23, "HP04's 6 cannot be covered whole -> 5 shipped"
    assert s["reduced_lines"] == 1 and s["reduced_qty"] == 1
    assert "RDC_SINGLE_SHORT" in s["_reductions"][0]["remark"]


def test_v7_both_non_strict_policies_dispatch_the_same_total():
    lines, avail = _e2_fixture()
    a = walk(lines, avail, [A, B], [A, B], policy=SPLIT_ALWAYS)
    b = walk(lines, avail, [A, B], [A, B], policy=SINGLE_PREFERRED)
    assert a["ship_tagged"] == b["ship_tagged"]
    assert a["split_lines"] == b["split_lines"]


# ---------------------------------------------------------------------------
# §B10 E4 — the untagged store
# ---------------------------------------------------------------------------
def test_e4_untagged_store_is_served_and_uses_the_fallback_order():
    avail = {(A, 5003, "L"): 15, (B, 5003, "L"): 20}
    lines = [line("HS11", A, 5003, "L", ship=10),
             line("HS12", "", 5003, "L", ship=12),
             line("HP04", B, 5003, "L", ship=4),
             line("HP09", "ALL", 5003, "L", ship=3)]
    s = walk(lines, avail, [A, B], [A, B])
    assert s["ship_tagged"] == 29, "an untagged store never loses stock"
    assert s["by_tier"].get("2") == 2, "HS12 and HP09 resolve at tier 2"
    hp09 = [v for k, v in s["_emit"].items() if k[0] == "HP09"]
    assert hp09[0]["SRC_RDC"] == A, "tier 2 follows the priority order DW01>DH24"
    assert hp09[0]["PREF_TIER"] == "2"


def test_reversing_the_priority_only_changes_a_contested_line():
    """Spec E4 — the tier matters only where BOTH warehouses could serve."""
    avail = {(A, 5003, "L"): 15, (B, 5003, "L"): 20}
    ln = [line("HP09", "ALL", 5003, "L", ship=3)]
    fwd = walk(ln, avail, [A, B], [A, B])
    rev = walk(ln, avail, [A, B], [B, A])
    assert list(fwd["_emit"].values())[0]["SRC_RDC"] == A
    assert list(rev["_emit"].values())[0]["SRC_RDC"] == B
    assert fwd["ship_tagged"] == rev["ship_tagged"] == 3, "the store gets 3 either way"


# ---------------------------------------------------------------------------
# V8 — determinism, and the tier-3 fallback
# ---------------------------------------------------------------------------
def test_v8_the_pass_is_deterministic():
    lines, avail = _e1_fixture()
    runs = [walk(lines, avail, [A, B], [A, B]) for _ in range(3)]
    sig = [sorted((k, round(v["SHIP_QTY"], 6)) for k, v in r["_emit"].items())
           for r in runs]
    assert sig[0] == sig[1] == sig[2], "same inputs must give the same SRC_RDC"


def test_tier3_takes_the_largest_available_first():
    avail = {(A, 5003, "L"): 5, (B, 5003, "L"): 20}
    s = walk([line("HX01", "", 5003, "L", ship=3)], avail, [A, B], priority=[])
    row = list(s["_emit"].values())[0]
    assert row["SRC_RDC"] == B, "no priority configured -> largest available"
    assert row["PREF_TIER"] == "3"


# ---------------------------------------------------------------------------
# edge cases (§B12)
# ---------------------------------------------------------------------------
def test_option_held_at_only_one_warehouse_cannot_split():
    avail = {(A, 5002, "M"): 0, (B, 5002, "M"): 50}
    s = walk([line("HS11", A, 5002, "M", ship=8)], avail, [A, B], [A, B])
    assert s["split_lines"] == 0 and s["by_rdc"][B]["ship"] == 8


def test_hold_only_line_emits_no_ship_quantity():
    avail = {(A, 5002, "M"): 10, (B, 5002, "M"): 10}
    s = walk([line("HS11", A, 5002, "M", ship=0, hold=3)], avail, [A, B], [A, B])
    row = list(s["_emit"].values())[0]
    assert row["SHIP_QTY"] == 0 and row["HOLD_QTY"] == 3


def test_ship_and_hold_to_the_same_warehouse_merge_into_one_row():
    """The split PK is (SESSION_ID, WERKS, VAR_ART, SZ, SRC_RDC, ALLOC_TYPE) —
    a second row for the same key would collide."""
    avail = {(A, 5002, "M"): 20}
    s = walk([line("HS11", A, 5002, "M", ship=5, hold=2)], avail, [A], [A])
    assert len(s["_emit"]) == 1
    row = list(s["_emit"].values())[0]
    assert row["SHIP_QTY"] == 5 and row["HOLD_QTY"] == 2


def test_nothing_available_anywhere_reduces_the_whole_line():
    avail = {(A, 5002, "M"): 0, (B, 5002, "M"): 0}
    s = walk([line("HS11", A, 5002, "M", ship=8)], avail, [A, B], [A, B])
    assert s["ship_tagged"] == 0 and s["reduced_qty"] == 8
    assert not s["_emit"], "no split row when nothing was tagged"


def test_exact_fit_leaves_no_residual():
    avail = {(A, 5002, "M"): 8}
    s = walk([line("HS11", A, 5002, "M", ship=8)], avail, [A], [A])
    assert s["ship_tagged"] == 8 and s["reduced_lines"] == 0


def test_zero_quantity_line_is_ignored():
    avail = {(A, 5002, "M"): 10}
    s = walk([line("HS11", A, 5002, "M", ship=0, hold=0)], avail, [A], [A])
    assert not s["_emit"] and s["reduced_lines"] == 0


def test_draw_never_returns_more_than_requested():
    avail = {(A, 1, "M"): 100, (B, 1, "M"): 100}
    taken, short = _draw(dict(avail), 1, "M", 7, [A, B], [A, B],
                         SINGLE_PREFERRED, 2)
    assert sum(n for _r, n in taken) == 7 and short == 0
