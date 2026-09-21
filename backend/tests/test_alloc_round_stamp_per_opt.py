"""FS-11 — ALLOC_ROUND / ALLOC_WAVE stamp rules in `_run_band_per_opt`.

`SHIP_QTY` accumulates across rounds; `ALLOC_ROUND` / `ALLOC_WAVE` are labels
that get overwritten. The post-TBL hold-release retry re-enters the band with
r=1 AFTER the real rounds have finished, so before this fix a no-op retry visit
dragged a genuine round-2 stamp back to 1 (session 20260812_131757_589,
HP04/1110116457: shipped 4+4 across two rounds, SHIP_QTY=8 correct, ALLOC_ROUND
read 1).

Two rules are enforced here:
  1. only rows that actually moved this pass are stamped;
  2. ALLOC_ROUND never decreases — MAX(existing, r).

Plus the retry's own wave label so its shipments stay distinguishable from
genuine round-1 ones.

Pure pandas fixtures, no DB.
"""
import numpy as np
import pandas as pd

from app.services.rule_engine_per_opt import POOL_KEYS, _run_band_per_opt


WERKS = "HP04"
RDC = "DH24"
MAJ_CAT = "M_TEES_HS"
GEN_ART = "1110116457"
CLR = "OFF_WHT"


SIZES = {"L": "1110116457003", "2XL": "1110116457005"}


def _alloc_df(sizes=(("L", 4.0),), sz_stk=0.0, i_rod=2.0, opt_type="RL"):
    """One OPT. `sizes` = ((SZ, SZ_MBQ), …) — the minimum the band needs."""
    return pd.DataFrame([{
        "WERKS": WERKS, "RDC": RDC, "MAJ_CAT": MAJ_CAT,
        "GEN_ART_NUMBER": GEN_ART, "CLR": CLR,
        "VAR_ART": SIZES[sz], "SZ": sz,
        "OPT_TYPE": opt_type, "IS_NEW": 0, "I_ROD": i_rod,
        "OPT_PRIORITY_RANK": 30.0, "ST_RANK": 1.0,
        "OPT_MBQ": sum(m for _, m in sizes), "SZ_MBQ": mbq, "SZ_MBQ_WH": mbq,
        "SZ_STK": sz_stk, "PAK_SZ": 1.0,
        "FNL_Q": 100.0, "POOL_CONSUMED": 0.0,
        "SHIP_QTY": 0.0, "HOLD_QTY": 0.0, "FROM_HOLD_QTY": 0.0,
        "ROUND_SHIP": 0.0, "ROUND_HOLD": 0.0,
        "ALLOC_QTY": 0.0, "ALLOC_STATUS": "PENDING", "SKIP_REASON": "",
        "ALLOC_REMARKS": "", "ALLOC_WAVE": "", "ALLOC_ROUND": 0.0,
    } for sz, mbq in sizes])


def _pool(**per_size):
    """_pool(L=100, **{'2XL': 1}) → pool_dict keyed the way the band keys it."""
    return {
        (RDC, MAJ_CAT, GEN_ART, CLR, SIZES[sz], sz): float(qty)
        for sz, qty in per_size.items()
    }


def _run(df, pool, r, wave_label=None, ot="RL"):
    _run_band_per_opt(df, pool, ot, r, wave_label=wave_label)


def _row(df, sz="L"):
    return df[df["SZ"] == sz].iloc[0]


def test_satisfied_size_keeps_round_two_when_a_sibling_size_pulls_the_retry_in():
    """The reported bug, in its real shape (HP04 / 1110116457).

    The stamp is applied per OPT (every size), while shipping is decided per
    size. So a size that finished in round 2 gets restamped whenever ANY
    sibling size still has need in a later pass — which is what the retry is.
    A single-size OPT would not reproduce it: with no residual need the whole
    OPT is skipped before the write-back.
    """
    df = _alloc_df(sizes=(("L", 4.0), ("2XL", 7.0)))

    # Round 1 — L fills, 2XL is pool-starved and takes 1 of the 7 it wants.
    _run(df, _pool(L=100, **{"2XL": 1}), 1)
    assert _row(df, "L")["SHIP_QTY"] == 4.0
    assert _row(df, "L")["ALLOC_ROUND"] == 1.0

    # Round 2 — L takes its second round; 2XL still has no pool.
    _run(df, _pool(L=100, **{"2XL": 0}), 2)
    assert _row(df, "L")["SHIP_QTY"] == 8.0        # 4 + 4, accumulated
    assert _row(df, "L")["ALLOC_ROUND"] == 2.0
    assert _row(df, "L")["ALLOC_WAVE"] == "RL_R2"

    # Released holds refill the pool; the retry re-enters at r=1. 2XL is still
    # hungry so the OPT is processed — but L is already at target and moves 0.
    _run(df, _pool(L=100, **{"2XL": 5}), 1, wave_label="RL_RETRY")

    assert _row(df, "2XL")["SHIP_QTY"] == 6.0      # 1 + 5, the retry delivered
    assert _row(df, "2XL")["ALLOC_WAVE"] == "RL_RETRY"

    assert _row(df, "L")["SHIP_QTY"] == 8.0        # untouched
    assert _row(df, "L")["ALLOC_ROUND"] == 2.0     # was 1.0 before the fix
    assert _row(df, "L")["ALLOC_WAVE"] == "RL_R2"  # was "RL_R1" before the fix


def test_retry_that_ships_carries_its_own_wave_label():
    """A retry that DOES deliver is recorded, and stays distinguishable."""
    df = _alloc_df()

    # Rounds 1 and 2 find an empty pool — nothing moves, nothing is stamped.
    _run(df, _pool(L=0), 1)
    _run(df, _pool(L=0), 2)
    assert _row(df)["SHIP_QTY"] == 0.0
    assert _row(df)["ALLOC_WAVE"] == ""
    assert _row(df)["ALLOC_ROUND"] == 0.0

    # Released holds refill the pool; the retry hands them over.
    _run(df, _pool(L=50), 1, wave_label="RL_RETRY")

    assert _row(df)["SHIP_QTY"] == 4.0
    assert _row(df)["ALLOC_WAVE"] == "RL_RETRY"   # not "RL_R1"
    assert _row(df)["ALLOC_ROUND"] == 1.0


def test_stamp_is_not_applied_to_rows_that_did_not_move():
    """A pass that reaches an OPT but ships zero leaves the labels alone."""
    df = _alloc_df()
    _run(df, _pool(L=0), 1)

    assert _row(df)["SHIP_QTY"] == 0.0
    assert _row(df)["ALLOC_WAVE"] == ""
    assert _row(df)["ALLOC_ROUND"] == 0.0


def test_alloc_round_never_decreases():
    """MAX(existing, r) — a lower round number can never overwrite a higher."""
    df = _alloc_df(sizes=(("L", 4.0),), i_rod=3.0)
    pool = _pool(L=100)

    _run(df, pool, 1)
    _run(df, pool, 3)
    assert _row(df)["ALLOC_ROUND"] == 3.0

    # A later pass at a lower round that still ships must not lower the stamp.
    df.loc[0, "SHIP_QTY"] = 0.0          # make round 2 have work to do again
    df.loc[0, "POOL_CONSUMED"] = 0.0
    _run(df, pool, 2)
    assert _row(df)["ALLOC_ROUND"] == 3.0
    assert _row(df)["ALLOC_WAVE"] == "RL_R2"     # wave reflects the last mover
