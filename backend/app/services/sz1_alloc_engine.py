"""
1SZ ALC — Step 3 allocation engine. Pure pandas: no database access, so it can
be run side by side with the Streamlit tool's own art_store.allocate() on the
same candidate frame (scripts/sz1_parity.py).

Ported from 1SZ ALLOC/art_store.py (2026-09-25). The arithmetic is the tool's,
statement for statement; only progress / cancel hooks were added. Spec:
frontend/public/docs/manual/sz1_alloc.md §8.3–8.4.

    add_priority   ST_SEQ inside each MAJ_CAT + SZ (weighted | mj_srt | sequence)
    allocate       whole packs, round by round, under the nested limits:
                     warehouse stock ≥ 1 pack
                     other-RDC store: SURPLUS_QTY left ≥ 1 pack
                     store's article total + pack ≤ round target
                     size budget MJ_SZ_SRT left ≥ 1 pack
                     full option (SZ A / NA) only: MJ_SRT left ≥ 1 pack
"""
from __future__ import annotations

import math
import re
from typing import Callable, Dict, List, Optional, Tuple

import pandas as pd

# I_ROD defaults, mirroring the ARS listing pipeline.
IROD_DEFAULT = 1        # missing or 0 means one round
IROD_AMIX_FLOOR = 2     # CLR or SZ 'A' / 'A_MIX' never gets less than this
IROD_CAP = 12           # guards against a bad master value turning into a long run

# Sizes that mark a complete option rather than a size.
FULL_OPT_SIZES = ("NA", "A")

# A pack is indivisible. False = the whole pack must fit inside MJ_SZ_SRT, so
# the size budget is never exceeded (the tool's setting).
HALF_PACK_BUDGET = False

MAX_STORES = 2000       # keeps the IN-list well inside SQL Server's 2,100-parameter limit

PRIORITY_MODES = {
    "weighted": "Balanced — mix of total shortage and size shortage (recommended)",
    "mj_srt": "Biggest total shortage first",
    "sequence": "My own store order",
}
STOCK_BASES = {
    "initial": "Full stock — ignore earlier runs (default)",
    "balance": "Remaining stock — what the latest run left behind",
}
STOCK_COLUMN = {"initial": "FNL_Q", "balance": "BAL_FNL_Q"}
# The two warehouse modes a run offers (FSD §8.3, 2026-10-09 revision).
#   all   every RDC's stock of an article is clubbed into ONE pool; stores of
#         every RDC compete for it in priority order; each pack is then drawn
#         from the store's own RDC when it still holds a whole pack, else from
#         the RDC holding the most — so a line served from two RDCs is split
#         into one row per source RDC. No home reserve: the pool is shared.
#   same  a store draws only from its own RDC (each RDC its own pool).
CROSS_RDC_MODES = {
    "all": "All RDCs — pool every warehouse's stock, allocate, split by source",
    "same": "Own RDC only — each store from its home warehouse",
}
# The Streamlit tool's other three modes. Still accepted by the engine and the
# SQL so sz1_parity.py can prove them against the tool; not offered on a run.
LEGACY_CROSS_RDC_MODES = {
    "home_first": "Own warehouse first, then top up from the other",
    "pooled": "Both warehouses as separate pools — whichever has more stock wins",
    "fallback": "Other warehouse only for items the home warehouse does not carry",
}
# The app always runs "none" (only ART_MBQ grows with the round); the other
# two are accepted for parity with sessions the tool recorded under them.
ROUND_ENVELOPES = {
    "none": "Fixed — only the article target grows per round",
    "size": "Raise the size limit per round",
    "both": "Raise the size and the category limit per round",
}


class Cancelled(Exception):
    """Raised inside allocate() when the run is cancelled."""


# ── store lists ─────────────────────────────────────────────────────────────
def parse_store_codes(text: str) -> List[str]:
    """Split a pasted list on any sensible separator, uppercase and dedupe."""
    if not text:
        return []
    seen: Dict[str, None] = {}
    for part in re.split(r"[\s,;|]+", str(text).strip()):
        code = part.strip().upper()
        if code:
            seen.setdefault(code, None)
    return list(seen)


def parse_sequence(text: str) -> Dict[str, int]:
    """'ST_CD' or 'ST_CD,seq' lines into {store: order}."""
    order: Dict[str, int] = {}
    for line in str(text or "").splitlines():
        parts = [p for p in line.replace(",", " ").replace(";", " ").split() if p]
        if not parts:
            continue
        code = parts[0].strip().upper()
        if not code:
            continue
        if len(parts) > 1 and parts[1].lstrip("-").isdigit():
            order[code] = int(parts[1])
        else:
            order.setdefault(code, len(order) + 1)
    return order


# ── store priority ──────────────────────────────────────────────────────────
def add_priority(cand: pd.DataFrame, mode: str, sequence: Optional[Dict[str, int]] = None,
                 weight_mj: float = 0.5) -> pd.DataFrame:
    """Attach ST_SEQ, the order stores are served in, ranked inside each
    MAJ_CAT + SZ (where stores actually compete).

    sequence  the uploaded order, the same for every group; unlisted stores last
    mj_srt    MJ_SRT descending, MJ_SZ_SRT only breaking exact ties
    weighted  weight_mj on the MJ_SRT rank, the rest on the MJ_SZ_SRT rank
    """
    out = cand.copy()
    if mode == "sequence":
        seq = {k.upper(): v for k, v in (sequence or {}).items()}
        fallback = max(seq.values(), default=0) + 1
        out["ST_SEQ"] = out["ST_CD"].str.upper().map(seq).fillna(fallback).astype(int)
        return out

    grp = ["MAJ_CAT", "SZ"]
    if mode == "mj_srt":
        out["_score"] = (out.groupby(grp)["MJ_SRT"].rank(ascending=False, method="min") * 1000
                         + out.groupby(grp)["MJ_SZ_SRT"].rank(ascending=False, method="min"))
    elif mode == "weighted":
        w = min(max(float(weight_mj), 0.0), 1.0)
        r_mj = out.groupby(grp)["MJ_SRT"].rank(ascending=False, method="min")
        r_sz = out.groupby(grp)["MJ_SZ_SRT"].rank(ascending=False, method="min")
        out["_score"] = w * r_mj + (1.0 - w) * r_sz
    else:
        raise ValueError(f"mode must be one of {tuple(PRIORITY_MODES)}")

    # a store keeps one rank per group, however many articles it has there
    per_store = (out.groupby(grp + ["ST_CD"], as_index=False)["_score"].min()
                 .sort_values(grp + ["_score", "ST_CD"]))
    per_store["ST_SEQ"] = per_store.groupby(grp).cumcount() + 1
    return out.drop(columns="_score").merge(per_store[grp + ["ST_CD", "ST_SEQ"]],
                                            on=grp + ["ST_CD"], how="left")


# ── allocation ──────────────────────────────────────────────────────────────
def pack_round(qty: float, pack: float) -> float:
    """A quantity snapped to whole packs, halves rounded up — the way T-SQL
    ROUND() builds ART_SRT (Python's round() is half-to-even and lost a pack
    on exact half-pack boundaries)."""
    if qty <= 0 or pack <= 0:
        return 0.0
    return math.floor(qty / pack + 0.5) * pack


def allocate(cand: pd.DataFrame, round_envelope: str = "none", cross_rdc: str = "same",
             progress: Optional[Callable[[dict], None]] = None,
             cancelled: Optional[Callable[[], bool]] = None,
             protect_other: bool = False, one_source: bool = False) -> pd.DataFrame:
    """Issue whole packs round by round under the nested budgets.

    Every store takes round 1 before any store starts round 2. In round r a
    store's cumulative ceiling for an article is packround(r × ART_MBQ − ART_STK),
    which at r = 1 is ART_SRT. Real sizes run all their rounds before any full
    option (SZ A / NA) starts, so a full option only fills what MJ_SRT has
    left after the real sizes. Within a round, articles go most-stock first
    (home rows first under home_first); for each article, stores are visited
    in ST_SEQ order, one pack each, cycling until no store can take a pack.
    """
    if round_envelope not in ROUND_ENVELOPES:
        raise ValueError(f"round_envelope must be one of {tuple(ROUND_ENVELOPES)}")

    work = cand.copy()
    work["ALLOC_QTY"] = 0.0

    # Decimals from the driver would not compare against a float pack size.
    numeric = ["ART_FNL_Q", "PAK_SZ", "ART_MBQ", "ART_STK", "ART_SRT", "ART_SRT_ROD", "I_ROD",
               "MJ_SZ_MBQ", "MJ_SZ_STK", "MJ_SZ_SRT", "MJ_MBQ", "MJ_STK", "MJ_SRT"]
    for col in numeric:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0.0)
    work["I_ROD"] = work["I_ROD"].clip(lower=1, upper=IROD_CAP)

    # Admission is on the full entitlement, not on round 1.
    live = work[work["ART_SRT_ROD"] > 0]
    if live.empty:
        work["ALC_I_ROD"] = 0
        work["PACKS"] = 0
        work["UNMET_QTY"] = work["ART_SRT_ROD"]
        work["BAL_ART_FNL_Q"] = work["ART_FNL_Q"]
        work["REM_MJ_SZ_SRT"] = work["MJ_SZ_SRT"]
        return work

    if cross_rdc == "all":
        per_src, last_round = _allocate_pool(live, round_envelope, progress, cancelled,
                                             protect_other=protect_other, one_source=one_source)
        return _finish(work, per_src, last_round)

    # Stock is per SOURCE warehouse: the same article in both RDCs is two pools.
    stock = live.groupby(["SRC_RDC", "ARTICLE_NUMBER"])["ART_FNL_Q"].max().to_dict()

    # What another RDC's stores may take; absent column = nothing held back.
    if "SURPLUS_QTY" in live.columns:
        surplus_left = live.groupby(["SRC_RDC", "ARTICLE_NUMBER"])["SURPLUS_QTY"].max().astype(float).to_dict()
    else:
        surplus_left = None

    sz_grain = live.groupby(["ST_CD", "MAJ_CAT", "SZ"])[["MJ_SZ_MBQ", "MJ_SZ_STK", "MJ_SZ_SRT"]].max()
    sz_allow = sz_grain["MJ_SZ_SRT"].to_dict()
    sz_used: Dict[tuple, float] = {}

    live = live.assign(IS_FULL_OPT=live["SZ"].astype(str).str.strip().str.upper()
                       .isin(FULL_OPT_SIZES).astype(int))
    mj_allow = live.groupby(["ST_CD", "MAJ_CAT"])["MJ_SRT"].max().to_dict()
    mj_used: Dict[tuple, float] = {}
    given: Dict[tuple, float] = {}       # (store, article) -> total, the entitlement cap
    per_src: Dict[tuple, float] = {}     # (src rdc, store, article) -> shipped
    last_round: Dict[tuple, int] = {}

    sort_cols = ["MAJ_CAT", "SZ", "ART_FNL_Q", "ARTICLE_NUMBER"]
    sort_asc = [True, True, False, True]
    if cross_rdc == "home_first" and "IS_HOME" in live.columns:
        sort_cols = ["IS_HOME"] + sort_cols
        sort_asc = [False] + sort_asc
    sort_cols = ["IS_FULL_OPT"] + sort_cols
    sort_asc = [True] + sort_asc
    live = live.sort_values(sort_cols, ascending=sort_asc)

    max_round = int(live["I_ROD"].max() or 1)
    passes = [(full, rnd) for full in (0, 1) for rnd in range(1, max_round + 1)]
    for pi, (full, rnd) in enumerate(passes, start=1):
        entitled = live[(live["I_ROD"] >= rnd) & (live["IS_FULL_OPT"] == full)]
        if entitled.empty:
            continue
        if progress:
            progress({"pass": pi, "passes": len(passes), "round": rnd, "full_opt": bool(full),
                      "units": float(sum(per_src.values()))})

        if round_envelope in ("size", "both"):
            for key, row in sz_grain.iterrows():
                sz_allow[key] = max(rnd * row["MJ_SZ_MBQ"] - row["MJ_SZ_STK"], 0.0)

        for gi, ((rdc, article), rows) in enumerate(
                entitled.groupby(["SRC_RDC", "ARTICLE_NUMBER"], sort=False)):
            if cancelled and gi % 500 == 0 and cancelled():
                raise Cancelled()
            recs = list(rows.sort_values(["ST_SEQ", "ART_SRT_ROD"],
                                         ascending=[True, False]).itertuples(index=False))
            while True:
                served = False
                for r in recs:
                    pack = float(r.PAK_SZ or 0)
                    if pack <= 0 or stock.get((rdc, article), 0) < pack:
                        break
                    at_home = bool(getattr(r, "IS_HOME", True))
                    if not at_home and surplus_left is not None:
                        if surplus_left.get((rdc, article), 0.0) < pack:
                            continue
                    key = (r.ST_CD, article)
                    target = pack_round(rnd * float(r.ART_MBQ) - float(r.ART_STK), pack)
                    if given.get(key, 0) + pack > target:
                        continue
                    sz_key = (r.ST_CD, r.MAJ_CAT, r.SZ)
                    sz_left = sz_allow.get(sz_key, 0) - sz_used.get(sz_key, 0)
                    need = pack / 2 if HALF_PACK_BUDGET else pack
                    if sz_left < need:
                        continue
                    mj_key = (r.ST_CD, r.MAJ_CAT)
                    if r.IS_FULL_OPT and (mj_allow.get(mj_key, 0) - mj_used.get(mj_key, 0)) < need:
                        continue
                    given[key] = given.get(key, 0) + pack
                    mj_used[mj_key] = mj_used.get(mj_key, 0) + pack
                    if not at_home and surplus_left is not None:
                        surplus_left[(rdc, article)] = surplus_left.get((rdc, article), 0.0) - pack
                    src_key = (rdc, r.ST_CD, article)
                    per_src[src_key] = per_src.get(src_key, 0) + pack
                    stock[(rdc, article)] -= pack
                    sz_used[sz_key] = sz_used.get(sz_key, 0) + pack
                    last_round[key] = rnd
                    served = True
                if not served:
                    break

    return _finish(work, per_src, last_round)


def _allocate_pool(live: pd.DataFrame, round_envelope: str,
                   progress: Optional[Callable[[dict], None]],
                   cancelled: Optional[Callable[[], bool]],
                   protect_other: bool = False, one_source: bool = False
                   ) -> Tuple[Dict[tuple, float], Dict[tuple, int]]:
    """All-RDC mode: one pool per article across every warehouse.

    The candidate frame has one row per store × article × source RDC; here the
    unit of allocation is the store × article (its entitlement, target and
    size budget are store-level and identical on each of its rows). The
    article's pool is every RDC's stock; a pack never ships half from two
    warehouses. Same rounds, order and limits as the per-RDC engine.

    Where a pack comes from:
      default        the store's own RDC while it holds a whole pack, else the
                     RDC holding the most — a line served from two RDCs comes
                     back as two rows, one per source.
      protect_other  another RDC's stock may be drawn only from its SURPLUS_QTY
                     (stock less what that RDC's own listed short stores need,
                     manual §D) — its own stores are never robbed.
      one_source     a store's line keeps the RDC it started from while that
                     RDC holds a whole pack; the first pack goes to the home
                     RDC if it can cover the whole remaining need, else to an
                     RDC that can, else home, else the one with the most — so a
                     line is split only when one warehouse cannot cover it.
    """
    stock = live.groupby(["SRC_RDC", "ARTICLE_NUMBER"])["ART_FNL_Q"].max().to_dict()
    rdcs_of: Dict[str, List[str]] = {}
    for (rdc, art) in stock:
        rdcs_of.setdefault(art, []).append(rdc)
    surplus_left: Optional[Dict[tuple, float]] = None
    if protect_other and "SURPLUS_QTY" in live.columns:
        surplus_left = live.groupby(["SRC_RDC", "ARTICLE_NUMBER"])["SURPLUS_QTY"].max().astype(float).to_dict()
    line_src: Dict[tuple, str] = {}           # (store, article) -> the RDC its line draws from
    line_need: Dict[tuple, float] = {}        # what that line still expects from it
    committed: Dict[tuple, float] = {}        # (rdc, article) -> Σ line_need of the lines on it

    sz_grain = live.groupby(["ST_CD", "MAJ_CAT", "SZ"])[["MJ_SZ_MBQ", "MJ_SZ_STK", "MJ_SZ_SRT"]].max()
    sz_allow = sz_grain["MJ_SZ_SRT"].to_dict()
    sz_used: Dict[tuple, float] = {}
    mj_allow = live.groupby(["ST_CD", "MAJ_CAT"])["MJ_SRT"].max().to_dict()
    mj_used: Dict[tuple, float] = {}
    given: Dict[tuple, float] = {}
    per_src: Dict[tuple, float] = {}
    last_round: Dict[tuple, int] = {}

    # one unit per store × article; the pool's stock orders the articles
    units = (live.groupby(["ST_CD", "ARTICLE_NUMBER"], as_index=False)
             .agg(RDC=("RDC", "first"), MAJ_CAT=("MAJ_CAT", "first"), SZ=("SZ", "first"),
                  PAK_SZ=("PAK_SZ", "max"), ART_MBQ=("ART_MBQ", "max"), ART_STK=("ART_STK", "max"),
                  ART_SRT_ROD=("ART_SRT_ROD", "max"), I_ROD=("I_ROD", "max"), ST_SEQ=("ST_SEQ", "min")))
    pool = live.groupby(["SRC_RDC", "ARTICLE_NUMBER"])["ART_FNL_Q"].max().groupby("ARTICLE_NUMBER").sum()
    units["POOL_Q"] = units["ARTICLE_NUMBER"].map(pool).fillna(0.0)
    units["IS_FULL_OPT"] = units["SZ"].astype(str).str.strip().str.upper().isin(FULL_OPT_SIZES).astype(int)
    units = units.sort_values(["IS_FULL_OPT", "MAJ_CAT", "SZ", "POOL_Q", "ARTICLE_NUMBER"],
                              ascending=[True, True, True, False, True])

    def avail(src: str, home: str, article: str) -> float:
        """What a store of `home` may still draw from `src`."""
        q = stock.get((src, article), 0)
        if src != home and surplus_left is not None:
            q = min(q, surplus_left.get((src, article), 0.0))
        return q

    def free(x: str, home: str, article: str) -> float:
        """Available from x once the other lines already drawing on it are counted."""
        return avail(x, home, article) - committed.get((x, article), 0.0)

    def source(home: str, article: str, pack: float, key: tuple, need: float) -> Optional[str]:
        ok = [x for x in rdcs_of.get(article, []) if avail(x, home, article) >= pack]
        if not ok:
            return None
        if one_source:
            stuck = line_src.get(key)
            if stuck in ok:
                return stuck
            covers = [x for x in ok if free(x, home, article) >= need]
            if home in covers:
                return home
            if covers:
                return max(covers, key=lambda x: (free(x, home, article), x))
        if home in ok:
            return home
        return max(ok, key=lambda x: (avail(x, home, article), x))

    def take(key: tuple, src: str, article: str, pack: float, want: float) -> None:
        """Book a pack on the line's source; a new or changed source takes over the line's need."""
        old = line_src.get(key)
        if old != src:
            if old is not None:
                committed[(old, article)] = committed.get((old, article), 0.0) - line_need.get(key, 0.0)
            line_src[key] = src
            line_need[key] = want
            committed[(src, article)] = committed.get((src, article), 0.0) + want
        used = min(pack, line_need.get(key, 0.0))
        line_need[key] = line_need.get(key, 0.0) - used
        committed[(src, article)] = committed.get((src, article), 0.0) - used

    max_round = int(units["I_ROD"].max() or 1)
    passes = [(full, rnd) for full in (0, 1) for rnd in range(1, max_round + 1)]
    for pi, (full, rnd) in enumerate(passes, start=1):
        entitled = units[(units["I_ROD"] >= rnd) & (units["IS_FULL_OPT"] == full)]
        if entitled.empty:
            continue
        if progress:
            progress({"pass": pi, "passes": len(passes), "round": rnd, "full_opt": bool(full),
                      "units": float(sum(per_src.values()))})
        if round_envelope in ("size", "both"):
            for key, row in sz_grain.iterrows():
                sz_allow[key] = max(rnd * row["MJ_SZ_MBQ"] - row["MJ_SZ_STK"], 0.0)

        for gi, (article, rows) in enumerate(entitled.groupby("ARTICLE_NUMBER", sort=False)):
            if cancelled and gi % 500 == 0 and cancelled():
                raise Cancelled()
            recs = list(rows.sort_values(["ST_SEQ", "ART_SRT_ROD"], ascending=[True, False])
                        .itertuples(index=False))
            while True:
                served = False
                for r in recs:
                    pack = float(r.PAK_SZ or 0)
                    if pack <= 0:
                        continue
                    key = (r.ST_CD, article)
                    target = pack_round(rnd * float(r.ART_MBQ) - float(r.ART_STK), pack)
                    if given.get(key, 0) + pack > target:
                        continue
                    sz_key = (r.ST_CD, r.MAJ_CAT, r.SZ)
                    sz_left = sz_allow.get(sz_key, 0) - sz_used.get(sz_key, 0)
                    need = pack / 2 if HALF_PACK_BUDGET else pack
                    if sz_left < need:
                        continue
                    mj_key = (r.ST_CD, r.MAJ_CAT)
                    mj_left = mj_allow.get(mj_key, 0) - mj_used.get(mj_key, 0)
                    if r.IS_FULL_OPT and mj_left < need:
                        continue
                    want = min(target - given.get(key, 0), sz_left, mj_left if r.IS_FULL_OPT else sz_left)
                    src = source(r.RDC, article, pack, key, want)
                    if src is None:
                        if surplus_left is None:        # no RDC holds a whole pack for anyone any more
                            break
                        continue                        # nothing this store may draw; others still may
                    given[key] = given.get(key, 0) + pack
                    mj_used[mj_key] = mj_used.get(mj_key, 0) + pack
                    sz_used[sz_key] = sz_used.get(sz_key, 0) + pack
                    per_src[(src, r.ST_CD, article)] = per_src.get((src, r.ST_CD, article), 0) + pack
                    stock[(src, article)] -= pack
                    if src != r.RDC and surplus_left is not None:
                        surplus_left[(src, article)] = surplus_left.get((src, article), 0.0) - pack
                    if one_source:
                        take(key, src, article, pack, want)
                    last_round[key] = rnd
                    served = True
                if not served:
                    break
    return per_src, last_round


def _finish(work: pd.DataFrame, per_src: Dict[tuple, float], last_round: Dict[tuple, int]) -> pd.DataFrame:
    """Per-row results from what each source warehouse shipped."""
    work["ALLOC_QTY"] = [per_src.get((r.SRC_RDC, r.ST_CD, r.ARTICLE_NUMBER), 0.0)
                         for r in work.itertuples(index=False)]
    work["ALC_I_ROD"] = [last_round.get((r.ST_CD, r.ARTICLE_NUMBER), 0)
                         for r in work.itertuples(index=False)]
    work["PACKS"] = (work["ALLOC_QTY"] / work["PAK_SZ"].replace(0, pd.NA)).fillna(0).round().astype(int)

    # One entitlement per store + article, served by up to two rows (one per
    # source warehouse): both rows carry the store-level shortfall.
    st_taken = work.groupby(["ST_CD", "ARTICLE_NUMBER"])["ALLOC_QTY"].transform("sum")
    work["UNMET_QTY"] = (work["ART_SRT_ROD"] - st_taken).clip(lower=0)
    art_taken = work.groupby(["SRC_RDC", "ARTICLE_NUMBER"])["ALLOC_QTY"].transform("sum")
    work["BAL_ART_FNL_Q"] = work["ART_FNL_Q"] - art_taken
    sz_taken = work.groupby(["ST_CD", "MAJ_CAT", "SZ"])["ALLOC_QTY"].transform("sum")
    work["REM_MJ_SZ_SRT"] = (work["MJ_SZ_SRT"] - sz_taken).clip(lower=0)
    return work
