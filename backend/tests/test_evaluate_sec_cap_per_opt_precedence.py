"""Precedence tests for `_evaluate_sec_cap_per_opt` (2026-07-13).

Covers the two-pass contract introduced by
docs/superpowers/specs/2026-07-13-sec-cap-hard-block-precedence-design.md:

    Pass 1 (veto scan, order-independent)  >  Pass 2 (Primary-first breach).

A hard-block on ANY applicable grid must veto the OPT even when an earlier
grid would admit it via the Primary overshoot allowance. A grid that does
not apply to the MAJ_CAT (GH=0) must NOT veto.

Pure pandas fixtures, no DB.
"""
from collections import defaultdict

import pandas as pd

from app.services.rule_engine_per_opt import _evaluate_sec_cap_per_opt


WERKS = "HO10"
MAJ_CAT = "L_JEANS"


def _grain(*extras):
    """Grain tuple as the function builds it: (WERKS, MAJ_CAT, *extras)."""
    return tuple([WERKS, MAJ_CAT] + [str(e) for e in extras])


def _make_state(grids, *, budgets=None, ceilings=None, configured=None,
                hard_block=None, gh_applies=None, running=None):
    """Build a minimal sec_cap_state matching build_sec_cap_state's shape."""
    g_names = [g for g, _ in grids]
    zero = lambda: {g: {} for g in g_names}

    def _fill(src, default):
        out = {g: {} for g in g_names}
        for g in g_names:
            out[g] = dict((src or {}).get(g, {}))
        return out

    state = {
        "grids":            grids,
        "budgets":          _fill(budgets, {}),
        "ceilings":         _fill(ceilings, {}),
        "stks":             zero(),
        "mbqs":             zero(),
        "configured":       _fill(configured, {}),
        "hard_block":       _fill(hard_block, {}),
        "running":          {g: defaultdict(float, (running or {}).get(g, {})) for g in g_names},
        "gh_applies":       dict(gh_applies or {}),
        "cont_pcts":        zero(),
        "resolved_growths": zero(),
        "band_matched":     zero(),
        "matrix_enabled":   False,
    }
    return state


def _opt_rows(**extra_cols):
    row = {"MAJ_CAT": MAJ_CAT}
    row.update(extra_cols)
    return pd.DataFrame([row])


# Grid metas ------------------------------------------------------------------
MJ = ("MJ", {"extras": [], "is_primary": True, "cap_pct": 110.0, "cap_factor": 1.10})
MJ_FIT = ("MJ_FIT", {"extras": ["FIT"], "is_primary": False, "cap_pct": 130.0, "cap_factor": 1.30})
MJ_YARN = ("MJ_M_YARN_02", {"extras": ["M_YARN_02"], "is_primary": False, "cap_pct": 130.0, "cap_factor": 1.30})


# =============================================================================
# 5.1 — the observed bug: veto shadows a Primary overshoot admit
# =============================================================================
def test_downstream_veto_beats_primary_overshoot_admit():
    """MJ breaches by an overshoot-eligible margin (16 vs 14, overshoot 2 <=
    8), but MJ_M_YARN_02 vetoes the OD grain. The OPT must be BLOCKED, not
    admitted via overshoot."""
    state = _make_state(
        [MJ, MJ_FIT, MJ_YARN],
        budgets={"MJ": {_grain(): 14.0}, "MJ_FIT": {_grain("REG"): 40.0}},
        ceilings={"MJ": {_grain(): 14.0}, "MJ_FIT": {_grain("REG"): 40.0}},
        configured={"MJ": {_grain(): True}, "MJ_FIT": {_grain("REG"): True}},
        hard_block={"MJ_M_YARN_02": {_grain("OD"): ["SEC_CAP_MBQ_ZERO[MJ_M_YARN_02]"]}},
        gh_applies={"MJ_FIT": {MAJ_CAT: True}, "MJ_M_YARN_02": {MAJ_CAT: True}},
    )
    info, participating = _evaluate_sec_cap_per_opt(
        state, _opt_rows(FIT="REG", M_YARN_02="OD"), WERKS, intended_ship=16.0
    )
    assert info is not None
    assert info["action"] == "block"
    assert info["grid"] == "MJ_M_YARN_02"
    assert info["hard_block_reasons"] == ["SEC_CAP_MBQ_ZERO[MJ_M_YARN_02]"]
    # Pass 2 never ran → no overshoot narrative, running not advanced.
    assert "overshoot_admit" not in info
    assert participating == []


# =============================================================================
# 5.2 — overshoot still admits when there is NO downstream veto
# =============================================================================
def test_primary_overshoot_still_admits_without_veto():
    state = _make_state(
        [MJ, MJ_FIT, MJ_YARN],
        budgets={"MJ": {_grain(): 14.0}, "MJ_FIT": {_grain("REG"): 40.0},
                 "MJ_M_YARN_02": {_grain("DNM_SLD"): 1272.0}},
        ceilings={"MJ": {_grain(): 14.0}, "MJ_FIT": {_grain("REG"): 40.0},
                  "MJ_M_YARN_02": {_grain("DNM_SLD"): 1272.0}},
        configured={"MJ": {_grain(): True}, "MJ_FIT": {_grain("REG"): True},
                    "MJ_M_YARN_02": {_grain("DNM_SLD"): True}},
        hard_block={},  # no veto anywhere
        gh_applies={"MJ_FIT": {MAJ_CAT: True}, "MJ_M_YARN_02": {MAJ_CAT: True}},
    )
    info, participating = _evaluate_sec_cap_per_opt(
        state, _opt_rows(FIT="REG", M_YARN_02="DNM_SLD"), WERKS, intended_ship=16.0
    )
    assert info is not None
    assert info["action"] == "override"
    assert info["grid"] == "MJ"
    assert info.get("overshoot_admit") is True
    # MJ participated (it was the deciding grid).
    assert ("MJ", _grain()) in participating


# =============================================================================
# 5.3 — multi-grid veto composition
# =============================================================================
def test_multi_grid_veto_composition():
    state = _make_state(
        [MJ, MJ_FIT, MJ_YARN],
        budgets={"MJ": {_grain(): 14.0}},
        ceilings={"MJ": {_grain(): 14.0}},
        configured={"MJ": {_grain(): True}},
        hard_block={
            "MJ_FIT": {_grain("NA"): ["SEC_CAP_GRID_NULL[MJ_FIT]"]},
            "MJ_M_YARN_02": {_grain("OD"): ["SEC_CAP_MBQ_ZERO[MJ_M_YARN_02]"]},
        },
        gh_applies={"MJ_FIT": {MAJ_CAT: True}, "MJ_M_YARN_02": {MAJ_CAT: True}},
    )
    info, participating = _evaluate_sec_cap_per_opt(
        state, _opt_rows(FIT="NA", M_YARN_02="OD"), WERKS, intended_ship=16.0
    )
    assert info["action"] == "block"
    # Both reasons carried, first-encountered grid reported.
    assert set(info["hard_block_reasons"]) == {
        "SEC_CAP_GRID_NULL[MJ_FIT]", "SEC_CAP_MBQ_ZERO[MJ_M_YARN_02]"
    }
    assert info["grid"] == "MJ_FIT"  # earliest in grids order
    assert participating == []


# =============================================================================
# 5.4 — veto short-circuits Primary breach; running never advances
# =============================================================================
def test_veto_short_circuits_and_does_not_advance_running():
    state = _make_state(
        [MJ, MJ_YARN],
        budgets={"MJ": {_grain(): 14.0}},
        ceilings={"MJ": {_grain(): 14.0}},
        configured={"MJ": {_grain(): True}},
        hard_block={"MJ_M_YARN_02": {_grain("OD"): ["SEC_CAP_MBQ_ZERO[MJ_M_YARN_02]"]}},
        gh_applies={"MJ_M_YARN_02": {MAJ_CAT: True}},
    )
    info, participating = _evaluate_sec_cap_per_opt(
        state, _opt_rows(M_YARN_02="OD"), WERKS, intended_ship=16.0
    )
    assert info["action"] == "block"
    assert "PRIMARY_CAP" not in str(info.get("hard_block_reasons"))
    assert info.get("primary_block") is None
    # participating empty → caller advances running for no grid.
    assert participating == []


# =============================================================================
# GH=0 edge — a non-applicable grid must NOT veto
# =============================================================================
def test_gh_zero_grid_cannot_veto():
    """MJ_M_YARN_02 carries a hard-block on the OD grain, but GH=0 for this
    MAJ_CAT means the grid does not apply — it must be skipped in Pass 1, so
    the OPT falls through to Pass 2 and is admitted via MJ overshoot."""
    state = _make_state(
        [MJ, MJ_YARN],
        budgets={"MJ": {_grain(): 14.0}},
        ceilings={"MJ": {_grain(): 14.0}},
        configured={"MJ": {_grain(): True}},
        hard_block={"MJ_M_YARN_02": {_grain("OD"): ["SEC_CAP_MBQ_ZERO[MJ_M_YARN_02]"]}},
        gh_applies={"MJ_M_YARN_02": {MAJ_CAT: False}},  # GH=0 → not applicable
    )
    info, participating = _evaluate_sec_cap_per_opt(
        state, _opt_rows(M_YARN_02="OD"), WERKS, intended_ship=16.0
    )
    assert info is not None
    assert info["action"] == "override"   # not vetoed → Primary overshoot admit
    assert info["grid"] == "MJ"
    assert info.get("overshoot_admit") is True
