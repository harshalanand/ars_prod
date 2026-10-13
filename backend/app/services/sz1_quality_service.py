"""
1SZ ALC — master data quality report (read only). Spec: manual/sz1_alloc.md §C.

Step 2's size share (Listing Level 2 ladder, scaled to 100 %) works around
several defects in the masters it reads. This report lists them so the owners
of Master_CONT_SZ and ARS_GRID_MJ can fix them at the source:

  A  store curves with A / NA > 0 beside real sizes  (the old rule halved them)
  B  store curves whose real sizes do not add to 100 %  (overlapping / partial)
  C  CO curves with the same problems
  D  duplicate Master_CONT_SZ keys  (the 1SZ SQL takes MAX)
  E  listed store × category with no ARS_GRID_MJ row  (budget 0) + why
  F  Step 2 store × category with no size curve at all  (NO_CURVE)

Everything here is a SELECT WITH (NOLOCK); nothing is written. The findings
are worked out together (≈ 2 s on 1.85M master rows), kept in process for
5 minutes, and the tables filter / sort / page / export from that copy, so a
filter never re-reads the master.
"""
from __future__ import annotations

import csv
import io
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from sqlalchemy import text

from app.database.session import data_engine
from app.services import sz1_schema as S
from app.services.sz1_base_service import FULL_OPT_SQL, jsonable, q

CACHE_SECONDS = 300
SAMPLE = 200                    # rows that come with the summary
PAGE_MAX = 500
DISTINCT_TOP = 300
TOL = 0.005                     # a real-size curve is "100 %" within ±0.5 %

_lock = threading.Lock()
_cache: Dict[str, Any] = {"at": 0.0, "data": None}


# ── helpers ─────────────────────────────────────────────────────────────────
def _rows(conn, sql: str, params: Optional[dict] = None) -> List[Dict[str, Any]]:
    res = conn.execute(text(sql), params or {})
    return [{k: jsonable(v) for k, v in r._mapping.items()} for r in res]


def _exists(conn, table: str) -> bool:
    return conn.execute(text("SELECT OBJECT_ID(:t, 'U')"), {"t": f"dbo.{table}"}).scalar() is not None


def _natkey(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(s or "").upper())]


def _r4(v) -> Optional[float]:
    return None if v is None else round(float(v), 4)


_RANGE = re.compile(r"^(\d+)\s*-\s*(\d+)\s*([A-Z]*)$")


def _overlaps(sizes: List[str]) -> List[str]:
    """Combined sizes 'n-mY' that sit next to 'nY' or 'mY' of the same curve,
    e.g. 14-15Y beside 14Y / 15Y — the same children counted twice."""
    have = {s.upper() for s in sizes}
    out = []
    for s in sizes:
        m = _RANGE.match(s.upper())
        if not m:
            continue
        lo, hi, suf = m.group(1), m.group(2), m.group(3)
        parts = [p for p in (lo + suf, hi + suf) if p in have]
        if parts:
            out.append(f"{s} + {' / '.join(parts)}")
    return out


def _parse_sizes(raw: Optional[str]) -> List[Tuple[str, float]]:
    """'SZ=0.1234|SZ=…' (STRING_AGG) → [(SZ, value)] in natural size order."""
    out = []
    for part in (raw or "").split("|"):
        if "=" in part:
            sz, v = part.rsplit("=", 1)
            try:
                out.append((sz, float(v)))
            except ValueError:
                pass
    return sorted(out, key=lambda x: _natkey(x[0]))


def _fmt_sizes(pairs: List[Tuple[str, float]]) -> str:
    return ", ".join(f"{sz} {v:.4f}".rstrip("0").rstrip(".") for sz, v in pairs)


# ── A / B / C — the curves ──────────────────────────────────────────────────
def _curves(conn) -> Dict[str, Any]:
    """One row per curve (ST_CD × MAJ_CAT) that has any curve problem. Duplicate
    keys grouped by MAX, as the 1SZ SQL does."""
    lo, hi = 1 - TOL, 1 + TOL
    data = _rows(conn, f"""
        WITH d AS (
            SELECT ST_CD, MAJ_CAT, SZ, MAX(CONT) AS CONT, MAX(SUB_DIV) AS SUB_DIV
              FROM {q(S.CONT_SZ)} WITH (NOLOCK) GROUP BY ST_CD, MAJ_CAT, SZ),
        c AS (
            SELECT ST_CD, MAJ_CAT, MAX(SUB_DIV) AS SUB_DIV,
                   SUM(CASE WHEN SZ NOT IN ({FULL_OPT_SQL}) AND CONT > 0 THEN CONT ELSE 0 END) AS REAL_TOTAL,
                   COUNT(CASE WHEN SZ NOT IN ({FULL_OPT_SQL}) AND CONT > 0 THEN 1 END) AS REAL_SIZES,
                   COUNT(CASE WHEN SZ NOT IN ({FULL_OPT_SQL}) THEN 1 END) AS REAL_ROWS,
                   MAX(CASE WHEN SZ = 'A' THEN CONT END) AS A_CONT,
                   MAX(CASE WHEN SZ = 'NA' THEN CONT END) AS NA_CONT
              FROM d GROUP BY ST_CD, MAJ_CAT),
        f AS (
            SELECT * FROM c
             WHERE ((ISNULL(A_CONT, 0) > 0 OR ISNULL(NA_CONT, 0) > 0) AND (REAL_ROWS > 0 OR ST_CD = 'CO'))
                OR (REAL_SIZES > 0 AND (REAL_TOTAL > {hi} OR REAL_TOTAL < {lo}))
                OR (ST_CD = 'CO' AND REAL_SIZES = 0)),
        sm AS (SELECT ST_CD, MAX(ST_NM) AS ST_NM, MAX(RDC) AS RDC, MAX(LISTING) AS LISTING
                 FROM {q(S.STORE_MASTER)} WITH (NOLOCK) GROUP BY ST_CD),
        hz AS (SELECT MAJ_CAT, MAX(SZ_APPLICABLE) AS SZ_APPLICABLE FROM {q(S.HIERARCHY)} WITH (NOLOCK) GROUP BY MAJ_CAT),
        sb AS (SELECT DISTINCT MAJ_CAT FROM {q(S.SZ_BASE)} WITH (NOLOCK))
        SELECT f.ST_CD, sm.ST_NM, sm.RDC, sm.LISTING, f.SUB_DIV, f.MAJ_CAT, hz.SZ_APPLICABLE,
               CASE WHEN sb.MAJ_CAT IS NULL THEN 'N' ELSE 'Y' END AS IN_SZ_BASE,
               f.REAL_TOTAL, f.REAL_SIZES, f.REAL_ROWS, f.A_CONT, f.NA_CONT,
               (SELECT STRING_AGG(CAST(d.SZ AS NVARCHAR(MAX)) + '=' + CONVERT(VARCHAR(24), CAST(d.CONT AS DECIMAL(18,6))), '|')
                  FROM d WHERE d.ST_CD = f.ST_CD AND d.MAJ_CAT = f.MAJ_CAT AND d.CONT > 0) AS SIZES_RAW
          FROM f
          LEFT JOIN sm ON sm.ST_CD = f.ST_CD
          LEFT JOIN hz ON hz.MAJ_CAT = f.MAJ_CAT
          LEFT JOIN sb ON sb.MAJ_CAT = f.MAJ_CAT""")
    # whole-master context for the KPI captions
    ctx = conn.execute(text(f"""
        SELECT COUNT(*) AS N_ROWS,
               SUM(CASE WHEN ST_CD <> 'CO' AND SZ IN ({FULL_OPT_SQL}) AND CONT > 0 THEN 1 ELSE 0 END) AS ST_ANA_ROWS
          FROM {q(S.CONT_SZ)} WITH (NOLOCK)""")).fetchone()
    a, b, c = [], [], []
    skip = {"A": 0, "B": 0, "C": 0}     # same problem in a category with SZ_APPLICABLE = 'N': Step 2
    for r in data:                      # splits its sizes evenly (EVEN_N) and never reads the curve
        pairs = _parse_sizes(r.pop("SIZES_RAW"))
        real = [(sz, v) for sz, v in pairs if sz.upper() not in ("A", "NA")]
        ov = _overlaps([sz for sz, _ in real])
        tot = float(r["REAL_TOTAL"] or 0)
        ana = (r["A_CONT"] or 0) + (r["NA_CONT"] or 0)
        base = {"ST_CD": r["ST_CD"], "ST_NM": r["ST_NM"], "RDC": r["RDC"],
                "LISTED": "Y" if r["LISTING"] == 1 else "N", "SUB_DIV": r["SUB_DIV"], "MAJ_CAT": r["MAJ_CAT"],
                "SZ_APPLICABLE": r["SZ_APPLICABLE"], "IN_1SZ": r["IN_SZ_BASE"]}
        is_co = r["ST_CD"] == "CO"
        has_ana = ana > 0 and (int(r["REAL_ROWS"] or 0) > 0 or is_co)
        off = int(r["REAL_SIZES"] or 0) > 0 and (tot > 1 + TOL or tot < 1 - TOL)
        if r["SZ_APPLICABLE"] == "N":
            if is_co:
                skip["C"] += 1
            else:
                skip["A"] += int(has_ana)
                skip["B"] += int(off)
            continue
        if is_co:
            issues = []
            if has_ana:
                issues.append("A / NA > 0 beside real sizes" if r["REAL_SIZES"] else "A / NA only — no real size > 0")
            elif not r["REAL_SIZES"]:
                issues.append("No real size > 0 — no CO curve")
            if off:
                issues.append(f"Real sizes add to {tot * 100:.1f} %")
            if ov:
                issues.append("Overlapping sizes")
            c.append({"MAJ_CAT": r["MAJ_CAT"], "SUB_DIV": r["SUB_DIV"], "SZ_APPLICABLE": r["SZ_APPLICABLE"],
                      "IN_1SZ": r["IN_SZ_BASE"], "ISSUE": "; ".join(issues), "REAL_SIZES": r["REAL_SIZES"],
                      "REAL_TOTAL_PCT": round(tot * 100, 2), "A_CONT": _r4(r["A_CONT"]), "NA_CONT": _r4(r["NA_CONT"]),
                      "OVERLAP": "Y" if ov else "N", "SIZES": _fmt_sizes(pairs)})
            continue
        if has_ana:
            halved = int(r["REAL_SIZES"] or 0) > 0
            a.append({**base,
                      "ISSUE": "Real sizes were halved" if halved else "Real sizes all 0 — CO curve used",
                      "REAL_SIZES": r["REAL_SIZES"], "REAL_TOTAL": _r4(tot), "A_CONT": _r4(r["A_CONT"]),
                      "NA_CONT": _r4(r["NA_CONT"]),
                      "OLD_SHARE_PCT": round(tot / (tot + ana) * 100, 2) if halved else 0.0,
                      "SIZES": _fmt_sizes(pairs)})
        if off:
            b.append({**base, "DIRECTION": "Over 100 %" if tot > 1 else "Under 100 %",
                      "REAL_TOTAL_PCT": round(tot * 100, 2), "REAL_SIZES": r["REAL_SIZES"],
                      "OVERLAP": "Y" if ov else "N", "OVERLAP_SIZES": "; ".join(ov) or None,
                      "SIZES": _fmt_sizes(real)})
    a.sort(key=lambda r: (r["ISSUE"] != "Real sizes were halved", _natkey(r["ST_CD"]), r["MAJ_CAT"]))
    b.sort(key=lambda r: (-abs(r["REAL_TOTAL_PCT"] - 100), _natkey(r["ST_CD"]), r["MAJ_CAT"]))
    c.sort(key=lambda r: r["MAJ_CAT"])
    return {"A": a, "B": b, "C": c, "skip": skip, "master_rows": int(ctx[0] or 0), "st_ana_rows": int(ctx[1] or 0)}


# ── D — duplicate keys ──────────────────────────────────────────────────────
def _dupes(conn) -> Dict[str, Any]:
    """Same ST_CD + MAJ_CAT + SZ more than once. With the primary key on those
    three columns (case-insensitive, trailing spaces ignored) an exact repeat
    cannot exist, so only a leading space, tab or non-breaking space can split
    one key into two: look for those first (one cheap scan) and group only then."""
    pk = conn.execute(text(f"""
        SELECT COUNT(*) FROM sys.indexes i
          JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
          JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
         WHERE i.object_id = OBJECT_ID('dbo.{S.CONT_SZ}') AND (i.is_primary_key = 1 OR i.is_unique = 1)
           AND c.name IN ('ST_CD', 'MAJ_CAT', 'SZ') AND ic.key_ordinal BETWEEN 1 AND 3
         GROUP BY i.index_id HAVING COUNT(*) = 3 AND MAX(ic.key_ordinal) = 3""")).fetchall()
    odd = "[ ' + NCHAR(9) + NCHAR(160) + ']%"
    dirty = 1
    if pk:
        dirty = conn.execute(text(f"""
            SELECT COUNT(*) FROM {q(S.CONT_SZ)} WITH (NOLOCK)
             WHERE ST_CD LIKE N'{odd}' OR MAJ_CAT LIKE N'{odd}' OR SZ LIKE N'{odd}'
                OR ST_CD LIKE N'%' + NCHAR(160) OR MAJ_CAT LIKE N'%' + NCHAR(160) OR SZ LIKE N'%' + NCHAR(160)""")).scalar()
    rows_: List[Dict[str, Any]] = []
    if dirty:
        norm = lambda c: f"UPPER(LTRIM(RTRIM(REPLACE(REPLACE({c}, NCHAR(160), ' '), NCHAR(9), ' '))))"
        rows_ = _rows(conn, f"""
            WITH k AS (SELECT {norm('ST_CD')} AS K_ST, {norm('MAJ_CAT')} AS K_MC, {norm('SZ')} AS K_SZ,
                              ST_CD, MAJ_CAT, SZ, CONT
                         FROM {q(S.CONT_SZ)} WITH (NOLOCK)),
            g AS (SELECT K_ST, K_MC, K_SZ, COUNT(*) AS ROWS_, MAX(CONT) AS MAX_CONT, MIN(CONT) AS MIN_CONT
                    FROM k GROUP BY K_ST, K_MC, K_SZ HAVING COUNT(*) > 1)
            SELECT g.K_ST AS ST_CD, g.K_MC AS MAJ_CAT, g.K_SZ AS SZ, g.ROWS_ AS DUP_ROWS,
                   g.MIN_CONT, g.MAX_CONT AS USED_CONT,
                   (SELECT STRING_AGG('[' + k.ST_CD + '|' + k.MAJ_CAT + '|' + k.SZ + ']=' + CONVERT(VARCHAR(24), k.CONT), '; ')
                      FROM k WHERE k.K_ST = g.K_ST AND k.K_MC = g.K_MC AND k.K_SZ = g.K_SZ) AS RAW_ROWS
              FROM g ORDER BY g.K_ST, g.K_MC, g.K_SZ""")
    return {"rows": rows_, "pk": bool(pk), "dirty": int(dirty or 0) if pk else None}


# ── E — grid coverage ───────────────────────────────────────────────────────
def _pair_facts(conn, keys: List[List[str]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Per [RDC, ST_CD, MAJ_CAT] (sent as JSON, so each is an index seek): does the
    store hold stock rows of the category (ARS_GRID_MJ_VAR_ART PK), and what did
    the latest Step 2 build read for it (UQ RDC + ST_CD + MAJ_CAT + SZ)."""
    if not keys:
        return {}
    import json
    res = conn.execute(text(f"""
        WITH k AS (SELECT RDC, ST_CD, MAJ_CAT FROM OPENJSON(:j)
                     WITH (RDC NVARCHAR(50) '$[0]', ST_CD NVARCHAR(50) '$[1]', MAJ_CAT NVARCHAR(255) '$[2]'))
        SELECT k.ST_CD, k.MAJ_CAT,
               CASE WHEN EXISTS (SELECT 1 FROM {q(S.GRID_VAR_ART)} v WITH (NOLOCK)
                                  WHERE v.WERKS = k.ST_CD AND v.MAJ_CAT = k.MAJ_CAT) THEN 1 ELSE 0 END AS HAS_STK,
               r.N, r.NOGRID, r.MJ_DISP, r.MJ_MBQ
          FROM k
         OUTER APPLY (SELECT COUNT(*) AS N,
                             MAX(CASE WHEN q2.MJ_STK IS NULL AND q2.MJ_DISP IS NULL AND q2.MJ_SAL_PD IS NULL THEN 1 ELSE 0 END) AS NOGRID,
                             MAX(q2.MJ_DISP) AS MJ_DISP, MAX(q2.MJ_MBQ) AS MJ_MBQ
                        FROM {q(S.REQ_BASE)} q2 WITH (NOLOCK)
                       WHERE q2.RDC = k.RDC AND q2.ST_CD = k.ST_CD AND q2.MAJ_CAT = k.MAJ_CAT) r"""),
        {"j": json.dumps(keys)})
    return {(r.ST_CD, r.MAJ_CAT): {k: jsonable(v) for k, v in r._mapping.items()} for r in res}


def _grid(conn, b2: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    bid = b2["BUILD_ID"] if b2 else None
    # the pairs (fast: grid PK seek), then the reasons from small sets joined here —
    # one combined SQL statement took ~90 s on HOPC866 against ~2 s split up
    pairs = _rows(conn, f"""
        WITH st AS (SELECT ST_CD, MAX(ST_NM) AS ST_NM, MAX(RDC) AS RDC
                      FROM {q(S.STORE_MASTER)} WITH (NOLOCK) WHERE LISTING = 1 GROUP BY ST_CD),
        sbr AS (SELECT MAJ_CAT, RDC, SUM(FNL_Q) AS FNL_Q FROM {q(S.SZ_BASE)} WITH (NOLOCK) GROUP BY MAJ_CAT, RDC),
        mc AS (SELECT MAJ_CAT, SUM(FNL_Q) AS FNL_Q,
                      STRING_AGG(CAST(RDC AS NVARCHAR(MAX)), ', ') WITHIN GROUP (ORDER BY RDC) AS RDCS
                 FROM sbr GROUP BY MAJ_CAT)
        SELECT st.RDC, st.ST_CD, st.ST_NM, mc.MAJ_CAT, mc.RDCS AS STOCKED_IN, CAST(mc.FNL_Q AS INT) AS SZ_BASE_FNL_Q
          FROM st CROSS JOIN mc
         WHERE NOT EXISTS (SELECT 1 FROM {q(S.GRID_MJ)} g WITH (NOLOCK)
                            WHERE g.WERKS = st.ST_CD AND g.MAJ_CAT = mc.MAJ_CAT)
         ORDER BY st.RDC, st.ST_CD, mc.MAJ_CAT""")
    msa = {(r[0], r[1]) for r in conn.execute(text(
        f"SELECT DISTINCT RDC, MAJ_CAT FROM {q(S.MSA_TOTAL)} WITH (NOLOCK)"))}
    facts = _pair_facts(conn, [[p["RDC"], p["ST_CD"], p["MAJ_CAT"]] for p in pairs])
    for p in pairs:
        fx = facts.get((p["ST_CD"], p["MAJ_CAT"])) or {}
        r = fx if fx.get("N") else None
        p["OWN_RDC_IN_MSA"] = "Y" if (p["RDC"], p["MAJ_CAT"]) in msa else "N"
        p["STORE_HAS_STOCK_ROWS"] = "Y" if fx.get("HAS_STK") else "N"
        state = ("not in the build" if r is None else "no grid row then either" if r["NOGRID"]
                 else "had a grid row then")
        p["STEP2_BUILD"] = f"#{bid}: {state}" if bid else state
        p["STEP2_MJ_DISP"] = None if r is None or r["NOGRID"] else r["MJ_DISP"]
        p["STEP2_MJ_MBQ"] = None if r is None or r["NOGRID"] else r["MJ_MBQ"]
    meta = conn.execute(text(f"""
        SELECT (SELECT COUNT(*) FROM (SELECT DISTINCT ST_CD FROM {q(S.STORE_MASTER)} WITH (NOLOCK) WHERE LISTING = 1) x) AS LISTED,
               (SELECT COUNT(*) FROM (SELECT DISTINCT MAJ_CAT FROM {q(S.SZ_BASE)} WITH (NOLOCK)) x) AS CATS,
               (SELECT COUNT(*) FROM (SELECT DISTINCT s.ST_CD FROM {q(S.STORE_MASTER)} s WITH (NOLOCK)
                                      WHERE s.LISTING = 1 AND NOT EXISTS (SELECT 1 FROM {q(S.GRID_MJ)} g WITH (NOLOCK)
                                                                          WHERE g.WERKS = s.ST_CD)) x) AS NOT_IN_GRID,
               (SELECT SUM(p.rows) FROM sys.partitions p WHERE p.object_id = OBJECT_ID('dbo.{S.GRID_MJ}') AND p.index_id IN (0, 1)) AS GRID_ROWS,
               (SELECT modify_date FROM sys.tables WHERE object_id = OBJECT_ID('dbo.{S.GRID_MJ}')) AS GRID_DDL_AT,
               (SELECT MAX(last_user_update) FROM sys.dm_db_index_usage_stats
                 WHERE database_id = DB_ID() AND object_id = OBJECT_ID('dbo.{S.GRID_MJ}')) AS GRID_DML_AT,
               (SELECT MAX(last_user_update) FROM sys.dm_db_index_usage_stats
                 WHERE database_id = DB_ID() AND object_id = OBJECT_ID('dbo.{S.MSA_TOTAL}')) AS MSA_DML_AT""")).fetchone()
    then = None
    if bid:
        t = conn.execute(text(f"""
            SELECT COUNT(DISTINCT ST_CD + '|' + MAJ_CAT), COUNT(DISTINCT ST_CD), COUNT(DISTINCT MAJ_CAT)
              FROM {q(S.REQ_BASE)} WITH (NOLOCK)
             WHERE MJ_STK IS NULL AND MJ_DISP IS NULL AND MJ_SAL_PD IS NULL""")).fetchone()
        then = {"pairs": int(t[0] or 0), "stores": int(t[1] or 0), "cats": int(t[2] or 0)}

    # groupings
    by_store: Dict[str, Dict[str, Any]] = {}
    by_cat: Dict[str, Dict[str, Any]] = {}
    for p in pairs:
        s = by_store.setdefault(p["ST_CD"], {"RDC": p["RDC"], "ST_CD": p["ST_CD"], "ST_NM": p["ST_NM"],
                                             "MISSING_CATS": 0, "CATS": []})
        s["MISSING_CATS"] += 1
        s["CATS"].append(p["MAJ_CAT"])
        c = by_cat.setdefault(p["MAJ_CAT"], {"MAJ_CAT": p["MAJ_CAT"], "STOCKED_IN": p["STOCKED_IN"],
                                             "SZ_BASE_FNL_Q": p["SZ_BASE_FNL_Q"], "MISSING_STORES": 0,
                                             "DH24_STORES": 0, "DW01_STORES": 0, "OTHER_RDC_STORES": 0,
                                             "NEW_SINCE_STEP2": 0})
        c["MISSING_STORES"] += 1
        k = f"{p['RDC']}_STORES" if p["RDC"] in ("DH24", "DW01") else "OTHER_RDC_STORES"
        c[k] += 1
        if "had a grid row" in p["STEP2_BUILD"]:
            c["NEW_SINCE_STEP2"] += 1
    listed = int(meta[0] or 0)
    for s in by_store.values():
        s["CATS"] = ", ".join(sorted(s["CATS"]))
    for c in by_cat.values():
        c["SHARE_OF_LISTED_PCT"] = round(c["MISSING_STORES"] / listed * 100, 1) if listed else None
    stores = sorted(by_store.values(), key=lambda r: (-r["MISSING_CATS"], _natkey(r["ST_CD"])))
    cats = sorted(by_cat.values(), key=lambda r: (-r["MISSING_STORES"], r["MAJ_CAT"]))

    # diagnosis
    n = len(pairs)
    f = lambda v: f"{int(v):,}"
    diag: List[str] = []
    if n:
        rdcs = {}
        for p in pairs:
            rdcs[p["RDC"]] = rdcs.get(p["RDC"], 0) + 1
        split = " · ".join(f"{k}: {f(v)}" for k, v in sorted(rdcs.items(), key=lambda x: -x[1]))
        diag.append(f"{f(n)} listed store × category pairs at {f(len(stores))} of {f(listed)} listed stores and "
                    f"{f(len(cats))} of {f(meta[1] or 0)} Step 1 categories have no ARS_GRID_MJ row ({split}).")
        diag.append("No listed store is missing from the grid entirely." if not meta[2]
                    else f"{f(meta[2])} listed store(s) have no ARS_GRID_MJ row at all.")
        neither = sum(1 for p in pairs if p["OWN_RDC_IN_MSA"] == "N" and p["STORE_HAS_STOCK_ROWS"] == "N")
        other_only = sum(1 for p in pairs if p["RDC"] not in (p["STOCKED_IN"] or ""))
        # the other side of the rule: grid rows (listed stores × Step 1 categories) that meet neither condition
        breaks = conn.execute(text(f"""
            WITH st AS (SELECT ST_CD, MAX(RDC) AS RDC FROM {q(S.STORE_MASTER)} WITH (NOLOCK) WHERE LISTING = 1 GROUP BY ST_CD),
            mc AS (SELECT DISTINCT MAJ_CAT FROM {q(S.SZ_BASE)} WITH (NOLOCK)),
            msa AS (SELECT DISTINCT RDC, MAJ_CAT FROM {q(S.MSA_TOTAL)} WITH (NOLOCK))
            SELECT COUNT(*) FROM {q(S.GRID_MJ)} g WITH (NOLOCK)
              JOIN st ON st.ST_CD = g.WERKS JOIN mc ON mc.MAJ_CAT = g.MAJ_CAT
             WHERE NOT EXISTS (SELECT 1 FROM msa WHERE msa.RDC = st.RDC AND msa.MAJ_CAT = g.MAJ_CAT)
               AND NOT EXISTS (SELECT 1 FROM {q(S.GRID_VAR_ART)} v WITH (NOLOCK)
                                WHERE v.WERKS = g.WERKS AND v.MAJ_CAT = g.MAJ_CAT)""")).scalar() or 0
        rule = ("every listed store × category that has a grid row meets one of the two" if not breaks else
                f"only {f(breaks)} grid row(s) meet neither")
        diag.append(f"{f(neither)} of the {f(n)} pairs are categories the store's own RDC does not carry in ARS_MSA_TOTAL "
                    "and the store holds no stock of (no ARS_GRID_MJ_VAR_ART row). The grid builds a store × category "
                    f"row only when the store's RDC carries the category or the store stocks it — {rule}. "
                    "So this is the grid's scope, not lost data. Business Rule GRID_ROW_SCOPE = COMPANY (Grid "
                    "Builder, 12 Oct 2026) builds these rows at company level from the next grid build (Run All); "
                    "they still get budget 0 until the store × category has DISP_Q in Master_ALC_INPUT_ST_MAJ_CAT.")
        diag.append(f"{f(other_only)} pairs are categories that 1SZ holds only in the other RDC: Step 2 offers every "
                    "category of ANY RDC to every listed store (for the All RDC pool), so those stores reach categories "
                    "their own grid never covers and get a budget of 0.")
        if then is not None:
            had = sum(1 for p in pairs if "had a grid row" in p["STEP2_BUILD"])
            both = sum(1 for p in pairs if "no grid row then" in p["STEP2_BUILD"])
            disp = sum(1 for p in pairs if "had a grid row" in p["STEP2_BUILD"] and (p["STEP2_MJ_DISP"] or 0) > 0)
            mbq = sum((p["STEP2_MJ_MBQ"] or 0) for p in pairs if "had a grid row" in p["STEP2_BUILD"])
            diag.append(f"Step 2 build #{bid} ({str(b2.get('FINISHED_AT') or '')[:16].replace('T', ' ')}) saw "
                        f"{f(then['pairs'])} pairs without a grid row ({f(then['stores'])} stores, {f(then['cats'])} categories). "
                        f"Today {f(both)} of those are still missing and {f(then["pairs"] - both)} are no longer missing (a grid row again, or the category left Step 1); "
                        f"{f(had)} pairs that had a grid row in that build have none now ({f(disp)} of them with display "
                        f"space, {f(mbq)} pcs of MBQ).")
        ddl, dml, msa = meta[4], meta[5], meta[6]
        last = max([x for x in (ddl, dml) if x], default=None)
        if last:
            diag.append(f"ARS_GRID_MJ was last rebuilt / changed {str(last)[:16]} ({f(meta[3] or 0)} rows now)"
                        + (f"; ARS_MSA_TOTAL last changed {str(msa)[:16]}" if msa else "")
                        + (f" — both after Step 2 build #{bid}. The grid followed the MSA's category scope, so the "
                           "missing set moved with it." if bid and b2.get("FINISHED_AT") and str(last) > str(b2["FINISHED_AT"]).replace("T", " ") else "."))
        own_gone = sum(1 for p in pairs if p["RDC"] in (p["STOCKED_IN"] or ""))
        if own_gone:
            diag.append(f"{f(own_gone)} pairs are categories Step 1 holds in the store's OWN RDC but that RDC's MSA no "
                        "longer carries (Step 1 was built from an older MSA) — a Step 1 rebuild drops them.")
        most = [c["MAJ_CAT"] for c in cats if listed and c["MISSING_STORES"] > listed / 2]
        if most:
            diag.append(f"Missing for more than half the listed stores: {', '.join(most)}.")
    else:
        diag.append("Every listed store has an ARS_GRID_MJ row for every Step 1 category.")
    return {"rows": pairs, "by_store": stores, "by_cat": cats, "diagnosis": diag,
            "stores": len(stores), "cats": len(cats), "then": then}


# ── F — no size curve at all ────────────────────────────────────────────────
def _no_curve(conn) -> Dict[str, Any]:
    """Step 2 store × MAJ_CAT (real sizes, size-applicable) whose store rows and
    CO rows give no real size a CONT > 0 — checked against today's master."""
    cte = f"""
        WITH r AS (SELECT RDC, ST_CD, MAJ_CAT, MAX(ST_NM) AS ST_NM, MAX(SZ_APPLICABLE) AS SZ_APPLICABLE,
                          MAX(MJ_MBQ) AS MJ_MBQ, MAX(MJ_SRT) AS MJ_SRT, MAX(CONT_SRC) AS STEP2_CONT_SRC
                     FROM {q(S.REQ_BASE)} WITH (NOLOCK) WHERE SZ NOT IN ({FULL_OPT_SQL})
                    GROUP BY RDC, ST_CD, MAJ_CAT
                   HAVING ISNULL(MAX(SZ_APPLICABLE), 'Y') <> 'N'),
        cv AS (SELECT DISTINCT ST_CD, MAJ_CAT FROM {q(S.CONT_SZ)} WITH (NOLOCK)
                WHERE SZ NOT IN ({FULL_OPT_SQL}) AND CONT > 0),
        x AS (SELECT r.*, CASE WHEN EXISTS (SELECT 1 FROM cv WHERE cv.ST_CD = 'CO' AND cv.MAJ_CAT = r.MAJ_CAT)
                               THEN 1 ELSE 0 END AS HAS_CO
                FROM r WHERE NOT EXISTS (SELECT 1 FROM cv WHERE cv.ST_CD = r.ST_CD AND cv.MAJ_CAT = r.MAJ_CAT))"""
    data = _rows(conn, cte + f"""
        SELECT RDC, ST_CD, ST_NM, MAJ_CAT, SZ_APPLICABLE, HAS_CO, MJ_MBQ, MJ_SRT, STEP2_CONT_SRC FROM x""")
    none = [r for r in data if not r["HAS_CO"]]
    co = [r for r in data if r["HAS_CO"]]
    rows_: List[Dict[str, Any]] = []
    if none:
        cats = sorted({r["MAJ_CAT"] for r in none})
        names = ", ".join(f":c{i}" for i in range(len(cats)))
        p = {f"c{i}": c for i, c in enumerate(cats)}
        sizes: Dict[str, List[str]] = {}
        for mc, sz in conn.execute(text(f"""SELECT DISTINCT MAJ_CAT, SZ FROM {q(S.REQ_BASE)} WITH (NOLOCK)
                                            WHERE MAJ_CAT IN ({names}) AND SZ NOT IN ({FULL_OPT_SQL})"""), p):
            sizes.setdefault(mc, []).append(sz)
        cnt = {(r[0], r[1]): int(r[2]) for r in conn.execute(text(f"""
            SELECT ST_CD, MAJ_CAT, COUNT(*) FROM {q(S.CONT_SZ)} WITH (NOLOCK)
             WHERE MAJ_CAT IN ({names}) GROUP BY ST_CD, MAJ_CAT"""), p)}
        for r in none:
            rows_.append({"RDC": r["RDC"], "ST_CD": r["ST_CD"], "ST_NM": r["ST_NM"], "MAJ_CAT": r["MAJ_CAT"],
                          "SZ_APPLICABLE": r["SZ_APPLICABLE"],
                          "STEP2_SIZES": ", ".join(sorted(sizes.get(r["MAJ_CAT"], []), key=_natkey)),
                          "MJ_MBQ": r["MJ_MBQ"], "MJ_SRT": r["MJ_SRT"], "STEP2_CONT_SRC": r["STEP2_CONT_SRC"],
                          "STORE_MASTER_ROWS": cnt.get((r["ST_CD"], r["MAJ_CAT"]), 0),
                          "CO_MASTER_ROWS": cnt.get(("CO", r["MAJ_CAT"]), 0)})
        rows_.sort(key=lambda r: (-(r["MJ_SRT"] or 0), r["RDC"], _natkey(r["ST_CD"]), r["MAJ_CAT"]))
    return {"rows": rows_, "co_pairs": len(co), "co_stores": len({r["ST_CD"] for r in co})}


# ── the report ──────────────────────────────────────────────────────────────
TEXT = {
    "A": ("Store curves with A / NA beside real sizes",
          "The curve's total then counts the whole-option A / NA row (usually 1.0) as if it were a size. The old Step 2 "
          "rule divided by that total and so halved every real size; a curve whose real sizes are all 0 gave them no "
          "share at all. Step 2 now leaves A / NA out of the divisor and falls back to CO — a work-around, not a fix.",
          "In Master_CONT_SZ set A and NA to 0 (or delete those rows) for these store × MAJ_CAT curves, keeping the "
          "real sizes adding to 1.0. Where every real size is 0, upload the store's real size curve."),
    "B": ("Store curves whose real sizes do not add to 100 %",
          "A size share is the size's CONT divided by the curve's real-size total. Step 2 scales each curve back to "
          "100 %, so the shares are right, but the master itself is wrong: overlapping sizes (14-15Y next to 14Y / 15Y) "
          "count the same children twice; partial curves leave sizes out.",
          "Fix the curve so its real sizes add to 1.0: drop the combined size or the single sizes it overlaps "
          "(OVERLAP = Y), or add the missing sizes to a partial curve."),
    "C": ("CO curves with the same problems",
          "The CO curve is the fallback for every store that has no curve of its own; an A / NA value, a missing real "
          "curve or a total off 100 % there reaches every such store.",
          "Correct the CO rows in Master_CONT_SZ for these categories: A / NA = 0 beside real sizes, real sizes "
          "adding to 1.0, and a real curve for size-applicable categories."),
    "D": ("Duplicate Master_CONT_SZ keys",
          "Two rows for one store + MAJ_CAT + size (also spelled apart only by case or spaces) make the curve "
          "ambiguous; the 1SZ SQL takes the larger value (MAX).",
          "Keep one row per ST_CD + MAJ_CAT + SZ, spelled exactly like the store master and the MSA."),
    "E": ("Listed store × category with no grid row",
          "Step 2 takes a store's MAJ_CAT display, sales and stock from ARS_GRID_MJ. With no row the store's MBQ — "
          "and so every size budget — is 0, and 1SZ sends it nothing in that category.",
          "If these stores should receive these categories, the grid must build their store × MAJ_CAT rows (display "
          "and sales plan) even when their own RDC does not stock the category; otherwise accept the 0 budget. "
          "See the diagnosis below."),
    "F": ("Store × category with no size curve at all",
          "Neither the store's own rows nor the CO rows give a real size a CONT > 0, so Step 2 gives every size 0 "
          "(CONT_SRC NO_CURVE) and the store gets nothing in that category.",
          "Upload a CO size curve for the category (it covers every store without its own), or the store's curve."),
}

COLS = {
    "A": ["ST_CD", "ST_NM", "RDC", "LISTED", "SUB_DIV", "MAJ_CAT", "IN_1SZ", "ISSUE", "REAL_SIZES", "REAL_TOTAL",
          "A_CONT", "NA_CONT", "OLD_SHARE_PCT", "SIZES"],
    "B": ["ST_CD", "ST_NM", "RDC", "LISTED", "SUB_DIV", "MAJ_CAT", "IN_1SZ", "DIRECTION", "REAL_TOTAL_PCT",
          "REAL_SIZES", "OVERLAP", "OVERLAP_SIZES", "SIZES"],
    "C": ["MAJ_CAT", "SUB_DIV", "SZ_APPLICABLE", "IN_1SZ", "ISSUE", "REAL_SIZES", "REAL_TOTAL_PCT", "A_CONT",
          "NA_CONT", "OVERLAP", "SIZES"],
    "D": ["ST_CD", "MAJ_CAT", "SZ", "DUP_ROWS", "MIN_CONT", "USED_CONT", "RAW_ROWS"],
    "E": ["RDC", "ST_CD", "ST_NM", "MAJ_CAT", "STOCKED_IN", "SZ_BASE_FNL_Q", "OWN_RDC_IN_MSA", "STORE_HAS_STOCK_ROWS",
          "STEP2_BUILD", "STEP2_MJ_DISP", "STEP2_MJ_MBQ"],
    "F": ["RDC", "ST_CD", "ST_NM", "MAJ_CAT", "SZ_APPLICABLE", "STEP2_SIZES", "MJ_MBQ", "MJ_SRT", "STEP2_CONT_SRC",
          "STORE_MASTER_ROWS", "CO_MASTER_ROWS"],
}
KEYS = tuple(TEXT)


def _load() -> Dict[str, Any]:
    t0 = time.perf_counter()
    timings: Dict[str, float] = {}

    def timed(name: str, fn: Callable[[], Any]) -> Any:
        t = time.perf_counter()
        try:
            return fn()
        finally:
            timings[name] = round(time.perf_counter() - t, 2)

    with data_engine.connect() as conn:
        missing = [t for t in (S.CONT_SZ, S.STORE_MASTER, S.GRID_MJ) if not _exists(conn, t)]
        if missing:
            raise RuntimeError(f"Source table(s) missing: {', '.join(missing)}")
        have_base = _exists(conn, S.SZ_BASE) and _exists(conn, S.REQ_BASE) and _exists(conn, S.BUILD)
        if not have_base:
            raise RuntimeError("The 1SZ ALC tables are not created yet — open 1SZ ALC once (Repair tables).")
        b = _rows(conn, f"""SELECT TOP 1 BUILD_ID, FINISHED_AT FROM {q(S.BUILD)} WITH (NOLOCK)
                             WHERE STEP = 2 AND STATUS = 'DONE' ORDER BY BUILD_ID DESC""")
        b2 = b[0] if b else None
        b1 = _rows(conn, f"""SELECT TOP 1 BUILD_ID, FINISHED_AT FROM {q(S.BUILD)} WITH (NOLOCK)
                              WHERE STEP = 1 AND STATUS = 'DONE' ORDER BY BUILD_ID DESC""")
        checked_at = conn.execute(text("SELECT SYSDATETIME()")).scalar()
        cur = timed("A_B_C", lambda: _curves(conn))
        dup = timed("D", lambda: _dupes(conn))
        grid = timed("E", lambda: _grid(conn, b2))
        nc = timed("F", lambda: _no_curve(conn))

    a, bb, c = cur["A"], cur["B"], cur["C"]
    halved = sum(1 for r in a if r["ISSUE"] == "Real sizes were halved")
    over = sum(1 for r in bb if r["DIRECTION"] == "Over 100 %")
    ovl = sum(1 for r in bb if r["OVERLAP"] == "Y")
    f = lambda v: f"{int(v):,}"
    sk = cur["skip"]
    na = lambda k: f" · {f(sk[k])} more in SZ_APPLICABLE = N categories (curve not used, left out)" if sk[k] else ""
    stat = {
        "A": dict(count=len(a), units=halved, unit_label="halved",
                  x=f"{f(halved)} with real sizes > 0 (were halved) · {f(len(a) - halved)} with every real size 0"
                    + na("A")),
        "B": dict(count=len(bb), units=ovl, unit_label="overlap",
                  x=f"{f(over)} over 100 % ({f(ovl)} with overlapping sizes) · {f(len(bb) - over)} under 100 %" + na("B")),
        "C": dict(count=len(c), x=(f"{f(len(c))} CO categories" if c else "CO curves of size-applicable categories are clean")
                  + na("C")),
        "D": dict(count=len(dup["rows"]),
                  x=(f"{f(sum(r['DUP_ROWS'] for r in dup['rows']))} rows" if dup["rows"]
                     else f"none in {f(cur['master_rows'])} rows" + (" — the primary key blocks exact repeats"
                                                                      if dup["pk"] else ""))),
        "E": dict(count=len(grid["rows"]), units=grid["stores"], unit_label="stores",
                  x=f"{f(grid['stores'])} stores · {f(grid['cats'])} categories · budget 0"),
        "F": dict(count=len(nc["rows"]),
                  x=(f"{f(nc['co_pairs'])} more pairs have no store curve and use the CO curve")),
    }
    rows_by = {"A": a, "B": bb, "C": c, "D": dup["rows"], "E": grid["rows"], "F": nc["rows"]}
    items = {}
    for k in KEYS:
        title, why, fix = TEXT[k]
        items[k] = {"key": k, "title": title, "why": why, "fix": fix, "cols": COLS[k], "rows": rows_by[k], **stat[k]}
    items["E"].update(by_store=grid["by_store"], by_cat=grid["by_cat"], diagnosis=grid["diagnosis"])
    return {"checked_at": jsonable(checked_at), "items": items, "timings": timings,
            "seconds": round(time.perf_counter() - t0, 2),
            "step1_build": b1[0] if b1 else None, "step2_build": b2,
            "master_rows": cur["master_rows"], "st_ana_rows": cur["st_ana_rows"], "skipped_not_size_applicable": sk}


def _data(refresh: bool = False) -> Dict[str, Any]:
    with _lock:
        if refresh or _cache["data"] is None or time.monotonic() - _cache["at"] > CACHE_SECONDS:
            _cache["data"] = _load()
            _cache["at"] = time.monotonic()
        return _cache["data"]


def summary(refresh: bool = False) -> Dict[str, Any]:
    d = _data(refresh)
    items = []
    for k in KEYS:
        it = d["items"][k]
        items.append({**{x: v for x, v in it.items() if x != "rows"}, "sample_rows": it["rows"][:SAMPLE]})
    age = time.monotonic() - _cache["at"]
    return {"checked_at": d["checked_at"], "items": items, "timings": d["timings"], "seconds": d["seconds"],
            "step1_build": d["step1_build"], "step2_build": d["step2_build"], "master_rows": d["master_rows"],
            "st_ana_rows": d["st_ana_rows"], "skipped_not_size_applicable": d["skipped_not_size_applicable"],
            "cache_seconds": CACHE_SECONDS, "age_seconds": round(age)}


# ── filter / sort / page the cached rows (same filter shape as sz1_filter) ──
def _item(key: str) -> Dict[str, Any]:
    key = (key or "").upper()
    if key not in KEYS:
        raise ValueError(f"Unknown finding {key}; one of {', '.join(KEYS)}")
    return _data()["items"][key]


def _numeric(rows: List[Dict[str, Any]], col: str) -> bool:
    seen = False
    for r in rows[:200]:
        v = r.get(col)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False
        seen = True
    return seen


def _expr(v, t: str, num: bool) -> bool:
    t = str(t).strip()
    if not t:
        return True
    if num:
        x = None if v is None else float(v)
        m = re.match(r"^(>=|<=|<>|>|<|=)\s*(-?\d+(\.\d+)?)$", t)
        if m:
            if x is None:
                return False
            y = float(m.group(2))
            return {">": x > y, "<": x < y, ">=": x >= y, "<=": x <= y, "=": x == y, "<>": x != y}[m.group(1)]
        m = re.match(r"^(-?\d+(\.\d+)?)\s*-\s*(-?\d+(\.\d+)?)$", t)
        if m:
            return x is not None and float(m.group(1)) <= x <= float(m.group(3))
        if re.match(r"^-?\d+(\.\d+)?$", t):
            return x is not None and x == float(t)
    return t.upper() in ("" if v is None else str(v)).upper()


def _key(v) -> str:
    return "\0blank" if v is None or v == "" else str(v)


def _apply(rows: List[Dict[str, Any]], cols: List[str], cf: Dict[str, Any]) -> List[Dict[str, Any]]:
    tests = []
    for col, spec in (cf or {}).items():
        if col not in cols or not isinstance(spec, dict):
            continue
        if isinstance(spec.get("values"), list):
            want = {_key(v) for v in spec["values"]}
            tests.append(lambda r, c=col, w=want: _key(r.get(c)) in w)
        elif spec.get("expr") not in (None, ""):
            num = _numeric(rows, col)
            tests.append(lambda r, c=col, e=spec["expr"], n=num: _expr(r.get(c), e, n))
    return [r for r in rows if all(t(r) for t in tests)] if tests else rows


def _sort(rows: List[Dict[str, Any]], cols: List[str], ob: Optional[str], od: Optional[str]) -> List[Dict[str, Any]]:
    if not ob or ob not in cols:
        return rows
    num = _numeric(rows, ob)
    desc = str(od).lower() == "desc"
    have = [r for r in rows if r.get(ob) not in (None, "")]
    blank = [r for r in rows if r.get(ob) in (None, "")]
    have.sort(key=(lambda r: float(r[ob])) if num else (lambda r: _natkey(r[ob])), reverse=desc)
    return have + blank                                   # blanks last either way


def rows(key: str, cf: Optional[Dict[str, Any]] = None, ob: Optional[str] = None, od: Optional[str] = None,
         page: int = 1, size: int = 200) -> Dict[str, Any]:
    it = _item(key)
    data = _sort(_apply(it["rows"], it["cols"], cf), it["cols"], ob, od)
    size = min(max(int(size), 1), PAGE_MAX)
    page = max(int(page), 1)
    off = (page - 1) * size
    return {"key": it["key"], "cols": it["cols"], "rows": data[off:off + size], "total": len(data),
            "page": page, "size": size, "checked_at": _data()["checked_at"]}


def distinct(key: str, col: str, cf: Optional[Dict[str, Any]] = None, search: Optional[str] = None) -> Dict[str, Any]:
    it = _item(key)
    if col not in it["cols"]:
        raise ValueError(f"Unknown column {col}")
    data = _apply(it["rows"], it["cols"], {k: v for k, v in (cf or {}).items() if k != col})
    num = _numeric(it["rows"], col)
    counts: Dict[str, Dict[str, Any]] = {}
    s = (search or "").strip().upper()
    for r in data:
        v = r.get(col)
        if s and s not in ("" if v is None else str(v)).upper():
            continue
        k = _key(v)
        e = counts.get(k)
        if e:
            e["n"] += 1
        else:
            counts[k] = {"v": None if k == "\0blank" else v, "n": 1}
    vals = list(counts.values())
    if num:
        vals.sort(key=lambda x: (x["v"] is None, float(x["v"]) if x["v"] is not None else 0))
    else:
        vals.sort(key=lambda x: (-x["n"], _natkey(x["v"])))
    return {"values": vals[:DISTINCT_TOP], "distinct": len(vals), "more": len(vals) > DISTINCT_TOP, "numeric": num}


def export_csv(key: str, cf: Optional[Dict[str, Any]] = None, ob: Optional[str] = None,
               od: Optional[str] = None) -> Iterator[bytes]:
    it = _item(key)
    data = _sort(_apply(it["rows"], it["cols"], cf), it["cols"], ob, od)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(it["cols"])
    for i, r in enumerate(data, 1):
        w.writerow(["" if r.get(c) is None else r.get(c) for c in it["cols"]])
        if i % 5000 == 0:
            yield buf.getvalue().encode("utf-8")
            buf.seek(0); buf.truncate(0)
    if buf.tell():
        yield buf.getvalue().encode("utf-8")


def export_name(key: str) -> str:
    it = _item(key)
    return f"1SZ_QUALITY_{it['key']}_{re.sub(r'[^A-Za-z0-9]+', '_', it['title']).strip('_').upper()[:40]}.csv"
