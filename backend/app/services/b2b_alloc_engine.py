"""
GRT ALC — Step 4: the allocation engine. Pure computation, no job state.

Ported from the Streamlit tool's allocate.py. Every rule that decides a
quantity or a bin is the tool's — ranking, the three caps, GREEDY and
ROUND_ROBIN (with the PROPORTIONAL / FLAT / NONE fair basis), the warehouse
rule, MAX_CONSUMPTION and BIN_ORDER picking — so the same inputs give the same
lines, line for line. That is proved by running the tool's own functions on
the same inputs (backend/scripts/b2b_alloc_parity.py).

Three caps, every line, every mode:

    1. store × article           ALLOC_QTY      ≤ SHORTFALL
    2. store × category × size   SUM(ALLOC_QTY) ≤ REQ
    3. article (per warehouse)   SUM(ALLOC_QTY) ≤ bin stock

What is different is only how the work is laid out, never what it decides:

  * One category per task, run on several processes. No cap crosses a
    category — an article belongs to one MAJ_CAT and one size (upload check
    G4 blocks anything else), the REQ cap is keyed on MAJ_CAT, and a bin
    holds one article — so a category carries all the state its decisions
    need, and the bins it draws from belong to no other category. The tool
    already relies on this for its MAJ_CAT chunks; here the chunks run at
    the same time.
  * Each task also picks its bins. The pick walk goes line by line in
    ALLOC_SEQ order, but only lines of the same article share bins, so the
    walk restricted to one category is the same walk.
  * ALLOC_SEQ is numbered once, centrally, over every line — the tool's
    _finalise — so it still reads as "who was served first across the run".
"""
from __future__ import annotations

import time
from bisect import bisect_left, insort
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from math import floor
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import text

PRIORITIES = ("SHORTFALL_DESC", "CONT_DESC", "MBQ_DESC")
FILL_MODES = ("GREEDY", "ROUND_ROBIN")
FAIR_BASES = ("PROPORTIONAL", "FLAT", "NONE")
BIN_PICKS = ("MAX_CONSUMPTION", "BIN_ORDER")
CROSS_RDCS = ("ANY", "HOME_FIRST", "SAME")

# Columns the decisions read. Descriptive ones (ST_NM, SEG …) are looked up per
# store / article at write time, as the tool does — carrying them on every
# candidate cost the tool 4.6 GiB.
CAND_COLS = ("STORE_CODE", "RDC", "ART", "MAJ_CAT", "BIN_SIZE", "SZ",
             "CONT", "MBQ_ROUNDED", "STK_TTL", "SHORTFALL", "BIN_QTY")

SORT_COL = {"SHORTFALL_DESC": "NEED", "CONT_DESC": "CONT", "MBQ_DESC": "MBQ_ROUNDED"}
SEQ_FIELD = {"SHORTFALL_DESC": "SHORTFALL", "CONT_DESC": "CONT", "MBQ_DESC": "MBQ_ROUNDED"}

STAT_KEYS = ("candidates", "blocked_no_req", "blocked_req_full", "blocked_art_empty",
             "below_min_qty", "rr_passes", "rr_reorders", "blocked_no_rdc",
             "cross_rdc_units", "chunks")


def new_stats() -> Dict[str, int]:
    return {k: 0 for k in STAT_KEYS}


# ═══════════════════════════════════════════════════════════════════════════
#  loading — every worker reads only its own category
# ═══════════════════════════════════════════════════════════════════════════
# `t` names the tables: bin, store, req, mbq, and `bin_rdc`, the SQL giving a
# bin's warehouse (ARS stores it as BIN_RDC; the tool parses the BIN prefix —
# the same value on prefixed bins).

def _raw(conn):
    """The pyodbc connection under a SQLAlchemy Connection, a pooled raw
    connection, or a pyodbc connection as it is."""
    fairy = getattr(conn, "connection", None)
    if fairy is not None and hasattr(fairy, "dbapi_connection"):
        return fairy.dbapi_connection                 # SQLAlchemy Connection
    if hasattr(conn, "dbapi_connection"):
        return conn.dbapi_connection                  # engine.raw_connection()
    return conn


def _frame(conn, sql: str, params: tuple, cols: List[str]) -> pd.DataFrame:
    """A raw-cursor read into a DataFrame. pd.read_sql adds a per-row cost
    that dominates at these sizes."""
    cur = _raw(conn).cursor()
    cur.arraysize = 50_000
    cur.execute(sql, *params)
    rows = cur.fetchall()
    cur.close()
    return pd.DataFrame.from_records([tuple(r) for r in rows], columns=cols)


def list_categories(conn, t: Dict[str, str]) -> List[Tuple[str, int]]:
    """Every category with a candidate, biggest first, so the longest tasks
    start first and the pool finishes evenly."""
    df = _frame(conn, f"""
        SELECT MAJ_CAT, COUNT(*) FROM dbo.[{t['mbq']}]
         WHERE SHORTFALL > 0 AND BIN_QTY > 0
         GROUP BY MAJ_CAT ORDER BY COUNT(*) DESC, MAJ_CAT""", (), ["MAJ_CAT", "N"])
    return [(r.MAJ_CAT, int(r.N)) for r in df.itertuples(index=False)]


def load_stores(conn, t: Dict[str, str]) -> Tuple[dict, dict]:
    df = _frame(conn, f"SELECT STORE_CODE, ST_NM, RDC FROM dbo.[{t['store']}]", (),
                ["STORE_CODE", "ST_NM", "RDC"])
    return dict(zip(df["STORE_CODE"], df["RDC"])), dict(zip(df["STORE_CODE"], df["ST_NM"]))


def supply_totals(conn, t: Dict[str, str]) -> Tuple[int, int]:
    """(Σ stock over every warehouse × article, Σ of the positive ones) — the
    tool's SUPPLY_UNITS and the base of its UNITS_LEFT."""
    rdc = t["bin_rdc"]
    cur = _raw(conn).cursor()
    cur.execute(f"""
        SELECT ISNULL(SUM(q), 0), ISNULL(SUM(CASE WHEN q > 0 THEN q ELSE 0 END), 0)
          FROM (SELECT SUM(QTY) q FROM dbo.[{t['bin']}] GROUP BY {rdc}, ART) x""")
    r = cur.fetchone()
    cur.close()
    return int(r[0]), int(r[1])


def load_part(conn, t: Dict[str, str], cat: str) -> Dict[str, Any]:
    """One category's supply, REQ caps, bins and article labels — the tool's
    load_inputs restricted to the category. An article belongs to one
    category (upload check G4), so this is exactly its slice."""
    rdc, b = t["bin_rdc"], t["bin"]
    supply = _frame(conn, f"""
        SELECT {rdc}, ART, SUM(QTY) FROM dbo.[{b}] WHERE MAJ_CAT = ?
         GROUP BY {rdc}, ART""", (cat,), ["BIN_RDC", "ART", "BIN_QTY"])
    caps = _frame(conn, f"""
        SELECT STORE_CODE, MAJ_CAT, [SIZE], SUM(REQ) FROM dbo.[{t['req']}] WHERE MAJ_CAT = ?
         GROUP BY STORE_CODE, MAJ_CAT, [SIZE]""", (cat,), ["STORE_CODE", "MAJ_CAT", "BIN_SIZE", "REQ"])
    # Ordered by BIN so BIN_ORDER reproduces the tool's walk exactly.
    bins = _frame(conn, f"""
        SELECT {rdc}, ART, BIN, SUM(QTY) FROM dbo.[{b}] WHERE MAJ_CAT = ?
         GROUP BY {rdc}, ART, BIN ORDER BY ART, BIN""", (cat,), ["BIN_RDC", "ART", "BIN", "QTY"])
    # Labels and the bin size: the tool reads them from the demand table, where
    # every row of an article carries its Bin Master MAX — so these are the
    # same values.
    attrs = _frame(conn, f"""
        SELECT ART, MAX(SEG), MAX(DIV), MAX(SUB_DIV), MAX(SEASON), MAX([SIZE]) FROM dbo.[{b}]
         WHERE MAJ_CAT = ? GROUP BY ART""", (cat,), ["ART", "SEG", "DIV", "SUB_DIV", "SEASON", "SIZE"])
    return {"supply": supply, "caps": caps, "bins": bins,
            "art_attrs": {r.ART: (r.SEG, r.DIV, r.SUB_DIV, r.SEASON) for r in attrs.itertuples(index=False)},
            "art_size": dict(zip(attrs["ART"], attrs["SIZE"]))}


def codes_are_ascii(conn, t: Dict[str, str]) -> bool:
    """Whether every store code and article survives a cast to VARCHAR, so the
    narrow read can send them as one byte a character."""
    cur = _raw(conn).cursor()
    cur.execute(f"""
        SELECT COUNT(*) FROM dbo.[{t['mbq']}]
         WHERE CAST(CAST(STORE_CODE AS VARCHAR(10)) AS NVARCHAR(10)) <> STORE_CODE
            OR CAST(CAST(ART AS VARCHAR(20)) AS NVARCHAR(20)) <> ART""")
    n = cur.fetchone()[0]
    cur.close()
    return n == 0


def load_candidates(conn, t: Dict[str, str], maj_cat: str, store_rdc: dict, art_size: dict,
                    ascii_codes: bool = True) -> pd.DataFrame:
    """One category's candidates, narrow: only what a decision reads.

    The database is across the network (HOPC866 from HOPC575), so the read is
    bytes-bound — measured with 8 readers: 93k rows/s for the full 11 columns,
    200k rows/s for codes as VARCHAR plus the three numbers. The rest is
    already known: MAJ_CAT is the task's, RDC is the store's in Store Master
    (the build copied it from there, and a newer load makes the build stale,
    which blocks the run), BIN_SIZE is the article's one Bin Master size. SZ
    and STK_TTL only appear on lines that receive stock, so enrich() fetches
    them for those lines at the end. DECIMALs come as FLOAT: a 6-dp DECIMAL
    maps to one distinct double, so the ranking is unchanged — all checked by
    the parity test."""
    s, a = ("CAST(STORE_CODE AS VARCHAR(10))", "CAST(ART AS VARCHAR(20))") if ascii_codes else ("STORE_CODE", "ART")
    df = _frame(conn, f"""
        SELECT {s}, {a}, CAST(CONT AS FLOAT), MBQ_ROUNDED, CAST(SHORTFALL AS FLOAT)
          FROM dbo.[{t['mbq']}]
         WHERE SHORTFALL > 0 AND BIN_QTY > 0 AND MAJ_CAT = ?""", (maj_cat,),
                ["STORE_CODE", "ART", "CONT", "MBQ_ROUNDED", "SHORTFALL"])
    df["RDC"] = df["STORE_CODE"].map(store_rdc)
    df["MAJ_CAT"] = maj_cat
    df["BIN_SIZE"] = df["ART"].map(art_size)
    df["SZ"] = None                                   # enrich() fills these on the lines
    df["STK_TTL"] = None
    df["BIN_QTY"] = None                              # only ever a filter, applied in SQL
    return df


def enrich(conn, t: Dict[str, str], lines: List[dict], picks: List[dict]) -> None:
    """SZ and STK_TTL for the lines that received stock, and their picks: one
    keyed join on the primary key instead of carrying both columns on every
    candidate. In place."""
    if not lines:
        return
    raw = _raw(conn)
    cur = raw.cursor()
    # tempdb's collation is not Rep_Data's; the keys must compare as the demand table's do.
    cur.execute("CREATE TABLE #want (STORE_CODE NVARCHAR(10) COLLATE DATABASE_DEFAULT NOT NULL, "
                "ART NVARCHAR(20) COLLATE DATABASE_DEFAULT NOT NULL, PRIMARY KEY (STORE_CODE, ART))")
    cur.fast_executemany = True
    keys = list({(r["STORE_CODE"], r["ART"]) for r in lines})
    for i in range(0, len(keys), 20_000):
        cur.executemany("INSERT INTO #want VALUES (?, ?)", keys[i:i + 20_000])
    cur.fast_executemany = False
    cur.execute(f"""SELECT m.STORE_CODE, m.ART, m.SZ, CAST(m.STK_TTL AS FLOAT)
                      FROM #want w JOIN dbo.[{t['mbq']}] m ON m.STORE_CODE = w.STORE_CODE AND m.ART = w.ART""")
    got = {(r[0], r[1]): (r[2], r[3]) for r in cur.fetchall()}
    cur.execute("DROP TABLE #want")
    raw.commit()
    cur.close()
    for r in lines + picks:
        sz, stk = got[(r["STORE_CODE"], r["ART"])]
        r["SZ"] = sz
        if "STK_TTL" in r:
            r["STK_TTL"] = stk


# ═══════════════════════════════════════════════════════════════════════════
#  allocation — the tool's allocate_rows, one category
# ═══════════════════════════════════════════════════════════════════════════
def pool_order(store_rdc, art, art_rdcs: dict, cross_rdc: str) -> tuple:
    """Warehouses this store may draw the article from, in draw order.
    Used by both allocation and the pick walk, so a quantity decided by one
    is always reachable by the other."""
    rdcs = art_rdcs.get(art)
    if not rdcs:
        return ()
    if cross_rdc == "ANY":
        return rdcs
    home = store_rdc in rdcs
    if cross_rdc == "SAME":
        return (store_rdc,) if home else ()
    if not home:                                   # HOME_FIRST
        return rdcs
    return (store_rdc,) + tuple(r for r in rdcs if r != store_rdc)


def _alloc_row(r, qty, art_qty, art_left_qty, cap_original, cap_left, key, seq_grp, cum,
               store_nm, art_attrs) -> dict:
    seg, div, sub_div, season = art_attrs.get(r.ART, (None, None, None, None))
    return {
        "STORE_CODE": r.STORE_CODE, "ST_NM": store_nm.get(r.STORE_CODE), "ART": r.ART,
        "SEG": seg, "DIV": div, "SUB_DIV": sub_div, "MAJ_CAT": r.MAJ_CAT,
        "BIN_SIZE": r.BIN_SIZE, "SZ": r.SZ, "SEASON": season,
        "CONT": None if pd.isna(r.CONT) else float(r.CONT),
        "MBQ_ROUNDED": None if pd.isna(r.MBQ_ROUNDED) else int(r.MBQ_ROUNDED),
        "STK_TTL": None if pd.isna(r.STK_TTL) else float(r.STK_TTL),
        "SHORTFALL": float(r.SHORTFALL),
        "ALLOC_SEQ": 0,                          # numbered centrally afterwards
        "ALLOC_SEQ_GRP": seq_grp,
        "ART_BIN_QTY": int(art_qty), "ART_BIN_LEFT": int(art_left_qty),
        "REQ_CAP": int(cap_original.get(key, 0)), "REQ_CAP_LEFT": int(cap_left.get(key, 0)),
        "ALLOC_CUM": int(cum), "ALLOC_QTY": int(qty),
        "RESIDUAL_SHORT": float(r.SHORTFALL) - qty,
        "RESIDUAL_SHORT_GRP": max(float(r.SHORTFALL) - cum, 0.0),
        "STILL_SENDABLE": min(float(r.SHORTFALL) - qty, float(cap_left.get(key, 0))),
    }


def prepare_work(chunk: pd.DataFrame, priority: str) -> pd.DataFrame:
    """NEED, drop the unservable, rank by ALLOC_PRIORITY (the tool's order)."""
    work = chunk.copy()
    work["NEED"] = work["SHORTFALL"].astype(float).apply(floor).astype(int)
    work = work[work["NEED"] > 0]
    return work.sort_values([SORT_COL[priority], "NEED", "CONT", "STORE_CODE", "ART"],
                            ascending=[False, False, False, True, True], na_position="last")


def _fill_greedy(work, art_total, art_left, art_rdcs, cap_left, cap_original, min_qty,
                 stats, cross_rdc, out, grp_seq, grp_cum, store_nm, art_attrs, trace=None):
    """One pass down the ranked list; each line takes everything it can.

    `trace` ({"key": (store, art)}) records what happened at that candidate's
    turn, for the "Why no stock?" walk. Off, it costs one comparison a row."""
    tk = trace["key"] if trace is not None else None
    for r in work.itertuples(index=False):
        hit = tk is not None and r.STORE_CODE == tk[0] and r.ART == tk[1]
        pools = pool_order(r.RDC, r.ART, art_rdcs, cross_rdc)
        avail = 0
        for p in pools:
            avail += art_left.get((p, r.ART), 0)
        if avail <= 0:
            if not pools:
                stats["blocked_no_rdc"] += 1
            else:
                stats["blocked_art_empty"] += 1
            if hit:
                _traced(trace, "NO_RDC" if not pools else "ART_EMPTY", r, pools, avail, None, art_total, out)
            continue
        key = (r.STORE_CODE, r.MAJ_CAT, r.BIN_SIZE)
        if key not in cap_original:
            stats["blocked_no_req"] += 1
            if hit:
                _traced(trace, "NO_REQ", r, pools, avail, None, art_total, out)
            continue
        room = cap_left.get(key, 0)
        if room <= 0:
            stats["blocked_req_full"] += 1
            if hit:
                _traced(trace, "REQ_FULL", r, pools, avail, (key, room, cap_original), art_total, out)
            continue
        qty = min(int(r.NEED), int(avail), int(room))
        if qty < min_qty:
            stats["below_min_qty"] += 1
            if hit:
                _traced(trace, "BELOW_MIN", r, pools, avail, (key, room, cap_original), art_total, out, qty=qty)
            continue
        if hit:
            _traced(trace, "SERVED", r, pools, avail, (key, room, cap_original), art_total, out, qty=qty)
        outstanding = qty
        for p in pools:
            if outstanding <= 0:
                break
            pk = (p, r.ART)
            take = min(outstanding, art_left.get(pk, 0))
            if take <= 0:
                continue
            art_left[pk] -= take
            outstanding -= take
            if p != r.RDC:
                stats["cross_rdc_units"] += take
        cap_left[key] = room - qty
        grp_seq[key] = grp_seq.get(key, 0) + 1
        grp_cum[key] = grp_cum.get(key, 0) + qty
        art_qty = sum(art_total.get((p, r.ART), 0) for p in pools)
        art_rem = sum(art_left.get((p, r.ART), 0) for p in pools)
        out.append(_alloc_row(r, qty, art_qty, art_rem, cap_original, cap_left, key,
                              grp_seq[key], grp_cum[key], store_nm, art_attrs))


def _traced(trace, outcome, r, pools, avail, cap, art_total, out, qty=None):
    """What the traced candidate met at its turn (GREEDY), and who was served
    before it on the same article and on the same REQ group."""
    key = (r.STORE_CODE, r.MAJ_CAT, r.BIN_SIZE)
    trace.update(
        mode="GREEDY", outcome=outcome, need=int(r.NEED), shortfall=float(r.SHORTFALL),
        pools=list(pools), avail=int(avail), qty=qty,
        stock=sum(v for (_p, a), v in art_total.items() if a == r.ART),
        room=None if cap is None else int(cap[1]),
        req_cap=None if cap is None else int(cap[2].get(key, 0)),
        before_same_art=[{"store": o["STORE_CODE"], "qty": o["ALLOC_QTY"], "shortfall": o["SHORTFALL"]}
                         for o in out if o["ART"] == r.ART],
        before_same_group=[{"art": o["ART"], "qty": o["ALLOC_QTY"]}
                           for o in out if (o["STORE_CODE"], o["MAJ_CAT"], o["BIN_SIZE"]) == key])


def _fill_round_robin(work, art_total, art_left, art_rdcs, cap_left, cap_original, min_qty,
                      stats, fair_basis, cross_rdc, out, grp_seq, grp_cum, store_nm, art_attrs,
                      trace=None):
    """Article by article, each wanting store takes min_qty per pass until the
    article is empty or nobody can take more. fair_basis re-orders the queue
    BETWEEN articles so the store just served drops back."""
    step = max(1, int(min_qty))
    per_art: Dict[str, list] = {}
    for rank, r in enumerate(work.itertuples(index=False)):
        per_art.setdefault(r.ART, []).append((r, rank))

    def _served(key):
        cap = cap_original.get(key, 0)
        if fair_basis == "PROPORTIONAL":
            return (cap - cap_left.get(key, 0)) / cap if cap else 0.0
        return float(cap - cap_left.get(key, 0))                     # FLAT

    def _art_left_all(art):
        return sum(art_left.get((p, art), 0) for p in art_rdcs.get(art, ()))

    granted: Dict[tuple, int] = {}
    for art, recs in per_art.items():
        if _art_left_all(art) < step:
            stats["blocked_art_empty"] += len(recs)
            continue
        live = []
        for r, rank in recs:
            key = (r.STORE_CODE, r.MAJ_CAT, r.BIN_SIZE)
            pools = pool_order(r.RDC, art, art_rdcs, cross_rdc)
            why = None
            if not pools:
                stats["blocked_no_rdc"] += 1
                why = "NO_RDC"
            elif key not in cap_original:
                stats["blocked_no_req"] += 1
                why = "NO_REQ"
            elif cap_left.get(key, 0) <= 0:
                stats["blocked_req_full"] += 1
                why = "REQ_FULL"
            elif int(r.NEED) < step:
                stats["below_min_qty"] += 1
                why = "BELOW_MIN"
            if trace is not None and (r.STORE_CODE, art) == trace["key"]:
                trace.update(mode="ROUND_ROBIN", screened=why or "LIVE", need=int(r.NEED),
                             shortfall=float(r.SHORTFALL), pools=list(pools),
                             room_at_start=int(cap_left.get(key, 0)),
                             req_cap=int(cap_original.get(key, 0)),
                             stock=sum(art_total.get((p, art), 0) for p in art_rdcs.get(art, ())),
                             group=key)
            if why:
                continue
            live.append((r, key, rank, pools))
        if fair_basis != "NONE" and len(live) > 1:
            live.sort(key=lambda x: (_served(x[1]), x[2]))
            stats["rr_reorders"] += 1
        while live and _art_left_all(art) >= step:
            stats["rr_passes"] += 1
            served = 0
            still = []
            for r, key, rank, pools in live:
                avail = sum(art_left.get((p, art), 0) for p in pools)
                if avail < step:
                    still.append((r, key, rank, pools))
                    continue
                room = cap_left.get(key, 0)
                gkey = (r.STORE_CODE, art)
                taken = granted.get(gkey, 0)
                if min(int(r.NEED) - taken, room) < step:
                    continue
                outstanding = step
                for p in pools:
                    if outstanding <= 0:
                        break
                    pk = (p, art)
                    take = min(outstanding, art_left.get(pk, 0))
                    if take <= 0:
                        continue
                    art_left[pk] -= take
                    outstanding -= take
                    if p != r.RDC:
                        stats["cross_rdc_units"] += take
                granted[gkey] = taken + step
                cap_left[key] = room - step
                served += step
                still.append((r, key, rank, pools))
            live = still
            if served == 0:
                break
    if trace is not None and trace.get("mode") == "ROUND_ROBIN":
        st, art = trace["key"]
        g = trace.get("group")
        trace.update(granted=granted.get((st, art), 0),
                     art_left=sum(art_left.get((p, art), 0) for p in art_rdcs.get(art, ())),
                     room_at_end=None if g is None else int(cap_left.get(g, 0)), step=step,
                     others=sorted(({"store": s_, "qty": q} for (s_, a_), q in granted.items()
                                    if a_ == art and s_ != st), key=lambda x: -x["qty"]))
    elif trace is not None and "mode" not in trace and trace["key"][1] in per_art:
        # The article was empty before the queue for it started: it never reached the screen.
        trace.update(mode="ROUND_ROBIN", screened="ART_EMPTY_AT_START")
    for r in work.itertuples(index=False):
        qty = granted.pop((r.STORE_CODE, r.ART), 0)
        if qty <= 0:
            continue
        key = (r.STORE_CODE, r.MAJ_CAT, r.BIN_SIZE)
        grp_seq[key] = grp_seq.get(key, 0) + 1
        grp_cum[key] = grp_cum.get(key, 0) + qty
        pools = pool_order(r.RDC, r.ART, art_rdcs, cross_rdc)
        art_qty = sum(art_total.get((p, r.ART), 0) for p in pools)
        art_rem = sum(art_left.get((p, r.ART), 0) for p in pools)
        out.append(_alloc_row(r, qty, art_qty, art_rem, cap_original, cap_left, key,
                              grp_seq[key], grp_cum[key], store_nm, art_attrs))


def seq_key(priority: str):
    """The tool's _finalise order: the priority signal descending, then
    SHORTFALL, CONT, STORE_CODE, ART. None sorts last."""
    field = SEQ_FIELD.get(priority, "SHORTFALL")

    def key(row):
        v, c = row.get(field), row.get("CONT")
        return (0 if v is None else -float(v), -float(row["SHORTFALL"]),
                0 if c is None else -float(c), row["STORE_CODE"], row["ART"])
    return key


# ═══════════════════════════════════════════════════════════════════════════
#  bins — the tool's split_to_bins, one category
# ═══════════════════════════════════════════════════════════════════════════
def _draw_max_consumption(pool: list, need: int) -> list:
    """Best fit (the smallest bin covering the whole line), else drain the
    largest bins first, clamping each take to what is still wanted."""
    taken = []
    idx = bisect_left(pool, need, key=lambda s: s[1])
    if idx < len(pool):
        slot = pool.pop(idx)
        slot[1] -= need
        taken.append((slot, need))
        if slot[1] > 0:
            insort(pool, slot, key=lambda s: s[1])
        return taken
    while need > 0 and pool:
        slot = pool.pop()
        take = slot[1] if slot[1] <= need else need
        slot[1] -= take
        need -= take
        taken.append((slot, take))
        if slot[1] > 0:
            insort(pool, slot, key=lambda s: s[1])
    return taken


def split_to_bins(alloc_rows: List[dict], bins: pd.DataFrame, store_rdc: dict,
                  bin_pick: str, cross_rdc: str) -> Tuple[List[dict], int]:
    """One pick row per (bin, article, store), walking the lines in ALLOC_SEQ
    order. `alloc_rows` must already be in that order."""
    max_con = bin_pick == "MAX_CONSUMPTION"
    bins_by_art: Dict[tuple, list] = {}
    art_rdcs: Dict[str, tuple] = {}
    for r in bins.itertuples(index=False):
        # slot = [BIN, qty left, qty held, warehouse]
        bins_by_art.setdefault((r.BIN_RDC, r.ART), []).append(
            [r.BIN, int(r.QTY or 0), int(r.QTY or 0), r.BIN_RDC])
    for rdc, art in sorted(bins_by_art):
        art_rdcs[art] = art_rdcs.get(art, ()) + (rdc,)
    if max_con:
        for pool in bins_by_art.values():
            pool.sort(key=lambda s: (s[1], s[0]))

    out, unfulfilled = [], 0
    pick_seq: Dict[tuple, int] = {}
    picked_tot: Dict[tuple, int] = {}
    for a in alloc_rows:
        need = int(a["ALLOC_QTY"])
        picks = []
        for p in pool_order(store_rdc.get(a["STORE_CODE"]), a["ART"], art_rdcs, cross_rdc):
            if need <= 0:
                break
            pool = bins_by_art.get((p, a["ART"]))
            if not pool:
                continue
            if max_con:
                got = _draw_max_consumption(pool, need)
                for _slot, take in got:
                    need -= take
                picks.extend(got)
            else:
                for slot in pool:
                    if need <= 0:
                        break
                    if slot[1] <= 0:
                        continue
                    take = min(need, slot[1])
                    slot[1] -= take
                    need -= take
                    picks.append((slot, take))
        for slot, take in picks:
            bkey = (slot[0], a["ART"])
            pick_seq[bkey] = pick_seq.get(bkey, 0) + 1
            picked_tot[bkey] = picked_tot.get(bkey, 0) + take
            out.append({
                "ROW_TYPE": "ALLOC", "BIN": slot[0], "ART": a["ART"],
                "STORE_CODE": a["STORE_CODE"], "ST_NM": a["ST_NM"],
                "STORE_RDC": store_rdc.get(a["STORE_CODE"]), "BIN_RDC": slot[3],
                "SEG": a["SEG"], "DIV": a["DIV"], "SUB_DIV": a["SUB_DIV"],
                "MAJ_CAT": a["MAJ_CAT"], "BIN_SIZE": a["BIN_SIZE"], "SZ": a["SZ"],
                "SEASON": a["SEASON"], "ALLOC_SEQ": a["ALLOC_SEQ"],
                "PICK_SEQ": pick_seq[bkey], "BIN_QTY": slot[2], "PICKED_QTY": 0,
                "QTY": take, "BIN_QTY_LEFT": slot[1], "STORE_ALLOC_QTY": int(a["ALLOC_QTY"]),
            })
        if need > 0:
            unfulfilled += need                  # impossible by construction; counted
    for r in out:
        r["PICKED_QTY"] = picked_tot.get((r["BIN"], r["ART"]), 0)
    return out, unfulfilled


# ═══════════════════════════════════════════════════════════════════════════
#  one category, end to end — the unit of parallel work
# ═══════════════════════════════════════════════════════════════════════════
def run_category(cand: pd.DataFrame, part: Dict[str, pd.DataFrame], opts: Dict[str, Any],
                 store_rdc: dict, store_nm: dict, art_attrs: dict,
                 trace: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Allocate one category and pick its bins. Returns its lines (ordered by
    the central key, ALLOC_SEQ still 0), its picks, stats, what each warehouse
    pool gave, and unfulfilled units."""
    stats = new_stats()
    supply, caps = part["supply"], part["caps"]
    art_total = {(r.BIN_RDC, r.ART): int(r.BIN_QTY or 0) for r in supply.itertuples(index=False)}
    art_left = dict(art_total)
    art_rdcs: Dict[str, tuple] = {}
    for rdc, art in sorted(art_total):
        art_rdcs[art] = art_rdcs.get(art, ()) + (rdc,)
    cap_left = {(r.STORE_CODE, r.MAJ_CAT, r.BIN_SIZE): int(floor(float(r.REQ or 0)))
                for r in caps.itertuples(index=False)}
    cap_original = dict(cap_left)
    out: List[dict] = []
    grp_seq: Dict[tuple, int] = {}
    grp_cum: Dict[tuple, int] = {}

    work = prepare_work(cand, opts["priority"])
    stats["candidates"] += int(len(work))
    stats["chunks"] += 1
    if len(work):
        if opts["fill_mode"] == "ROUND_ROBIN":
            _fill_round_robin(work, art_total, art_left, art_rdcs, cap_left, cap_original,
                              opts["min_qty"], stats, opts["fair_basis"], opts["cross_rdc"],
                              out, grp_seq, grp_cum, store_nm, art_attrs, trace=trace)
        else:
            _fill_greedy(work, art_total, art_left, art_rdcs, cap_left, cap_original,
                         opts["min_qty"], stats, opts["cross_rdc"], out, grp_seq, grp_cum,
                         store_nm, art_attrs, trace=trace)
    out.sort(key=seq_key(opts["priority"]))
    picks, unfulfilled = split_to_bins(out, part["bins"], store_rdc, opts["bin_pick"], opts["cross_rdc"])
    drawn = {k: art_total[k] - art_left[k] for k in art_total if art_total[k] != art_left[k]}
    return {"lines": out, "picks": picks, "stats": stats, "drawn": drawn, "unfulfilled": unfulfilled}


def _worker(task: Dict[str, Any]) -> Dict[str, Any]:
    """Process-pool entry point: read one category on this process's own
    connection, allocate it and pick its bins. Module level so a spawned
    process can import it."""
    t0 = time.time()
    from app.database.session import data_engine
    with data_engine.connect() as conn:
        part = load_part(conn, task["t"], task["cat"])
        cand = load_candidates(conn, task["t"], task["cat"], task["store_rdc"], part["art_size"],
                               task["ascii"])
    t1 = time.time()
    res = run_category(cand, part, task["opts"], task["store_rdc"], task["store_nm"], part["art_attrs"])
    res.update(cat=task["cat"], read_sec=round(t1 - t0, 2), run_sec=round(time.time() - t1, 2))
    return res


def allocate_all(t: Dict[str, str], opts: Dict[str, Any], workers: int = 8,
                 progress: Callable[[Dict[str, Any]], None] = lambda p: None,
                 cancelled: Callable[[], bool] = lambda: False,
                 only: Optional[List[str]] = None) -> Dict[str, Any]:
    """Every category, `workers` at a time, biggest first. Returns the lines
    (ALLOC_SEQ numbered), the picks, summed stats, supply totals and timings.
    `only` restricts the run to some categories (the parity test)."""
    from app.database.session import data_engine
    t0 = time.time()
    with data_engine.connect() as conn:
        cats = [c for c in list_categories(conn, t) if only is None or c[0] in set(only)]
        store_rdc, store_nm = load_stores(conn, t)
        supply_units, supply_pos = supply_totals(conn, t)
        ascii_codes = codes_are_ascii(conn, t)
    progress({"phase": "categories", "total": len(cats), "done": 0, "lines": 0, "units": 0,
              "cross": 0, "candidates": 0, "candidates_total": sum(n for _, n in cats)})

    lines: List[dict] = []
    picks: List[dict] = []
    stats = new_stats()
    drawn = 0
    unfulfilled = 0
    per_cat: List[Dict[str, Any]] = []
    done = 0
    units = 0
    cand_total = sum(n for _, n in cats)

    def absorb(res):
        nonlocal unfulfilled, done, drawn, units
        lines.extend(res["lines"])
        picks.extend(res["picks"])
        for k, v in res["stats"].items():
            stats[k] += v
        drawn += sum(res["drawn"].values())
        unfulfilled += res["unfulfilled"]
        done += 1
        got = sum(r["ALLOC_QTY"] for r in res["lines"])
        units += got
        per_cat.append({"cat": res["cat"], "candidates": res["stats"]["candidates"],
                        "lines": len(res["lines"]), "units": got,
                        "read_sec": res["read_sec"], "run_sec": res["run_sec"]})
        progress({"phase": "categories", "total": len(cats), "done": done, "cat": res["cat"],
                  "lines": len(lines), "units": units, "cross": stats["cross_rdc_units"],
                  "candidates": stats["candidates"], "candidates_total": cand_total})

    tasks = [{"cat": c, "t": t, "opts": opts, "store_rdc": store_rdc, "store_nm": store_nm,
              "ascii": ascii_codes} for c, _ in cats]
    n = max(1, min(int(workers), len(tasks) or 1))
    if n == 1:
        for tk in tasks:
            if cancelled():
                raise Cancelled()
            absorb(_worker(tk))
    else:
        ex = ProcessPoolExecutor(max_workers=n)
        try:
            futures = [ex.submit(_worker, tk) for tk in tasks]
            for f in as_completed(futures):
                if cancelled():
                    raise Cancelled()
                absorb(f.result())
        finally:
            ex.shutdown(wait=True, cancel_futures=True)

    finalise(lines, picks, opts["priority"])
    t_enrich = time.time()
    with data_engine.connect() as conn:
        enrich(conn, t, lines, picks)
    t_enrich = round(time.time() - t_enrich, 1)
    # The tool's UNITS_LEFT: every positive warehouse × article pool, less what
    # was drawn (a drawn pool is positive by construction).
    return {
        "lines": lines, "picks": picks, "stats": stats, "unfulfilled": unfulfilled,
        "supply_units": supply_units, "supply_left": supply_pos - drawn,
        "allocated_units": sum(r["ALLOC_QTY"] for r in lines),
        "stores_served": len({r["STORE_CODE"] for r in lines}),
        "arts_used": len({r["ART"] for r in lines}),
        "per_category": sorted(per_cat, key=lambda c: -c["candidates"]),
        "workers": n, "total_sec": round(time.time() - t0, 1), "enrich_sec": t_enrich,
        "ascii_codes": ascii_codes, "store_rdc": store_rdc,
    }


class Cancelled(Exception):
    pass


def finalise(lines: List[dict], picks: List[dict], priority: str) -> None:
    """Number ALLOC_SEQ over every line (the tool's _finalise order) and copy
    it onto each pick from its parent line. In place."""
    lines.sort(key=seq_key(priority))
    seq = {}
    for i, row in enumerate(lines, start=1):
        row["ALLOC_SEQ"] = i
        seq[(row["STORE_CODE"], row["ART"])] = i
    for p in picks:
        p["ALLOC_SEQ"] = seq[(p["STORE_CODE"], p["ART"])]
