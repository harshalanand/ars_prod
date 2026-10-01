"""
GRT ALC — Step 3: build the store × article demand table (ARS_B2B_ART_MBQ).

Ported from the Streamlit tool's build_derived.py. Every SQL statement that
computes a number is the tool's, unchanged apart from table names, so the same
inputs give the same rows and values. That is proved by running this build
over the tool's own B2B_* tables into a scratch table and comparing it with the
tool's B2B_ART_MBQ (backend/scripts/b2b_mbq_parity.py).

    NORM_DAYS   = SHORT_DAYS if SZ is a fast size (or a *MIX category), else LONG_DAYS
    ACC_D_EFF   = ACC_D × CONT_EFF                  the article's share of the category
    MBQ_RAW     = ACC_D_EFF + ACC_D_EFF / NORM_DAYS × SALE_COVER_DAYS
    MBQ         = MBQ_RAW, lifted to MBQ_MIN_WHEN_CONT when CONT_EFF > 0
    MBQ_ROUNDED = ROUND(MBQ, 0)
    SHORTFALL   = MAX(MBQ_ROUNDED − STK_TTL, 0)     what allocation may fill
    EXCESS      = MAX(STK_TTL − MBQ_ROUNDED, 0)

What is different, each on purpose:

  * All or nothing. The tool truncates, inserts and rebuilds indexes as
    separate commits, so a failure part-way leaves an empty table. Here rows
    go into a staging table, are checked there, and are switched in with one
    metadata-only ALTER TABLE ... SWITCH: a failed or cancelled build leaves
    the previous demand exactly as it was, and nobody reading the live table
    waits for more than that instant.
  * Checked before it counts. The build proves its own output — size shares
    sum to 1.00, SHORTFALL and EXCESS agree with MBQ_ROUNDED and STK_TTL — and
    is never switched in if either fails.
  * Faster and smaller. The table is a clustered columnstore written on 16
    threads (see BUILD_MAXDOP for the measurements).
  * Recorded. Each build is a row in ARS_B2B_MBQ_BUILD: the upload it read,
    every setting, when the stock grid last changed, seconds per step, a
    summary, and a forecast of the bin stock no store can receive, by reason.
  * A background job with progress and cancel, like the upload.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import data_engine
from app.services import b2b_schema as S
from app.services import b2b_settings

CO_CODE = "CO"                    # Master_CONT_SZ company-level fallback rows
CONT_MODES = ("HYBRID", "REQ", "MASTER")
SHARE_TOL = 0.00005               # a size mix must sum to 1.00 within this
MBQ_KEYS = [s["key"] for s in b2b_settings.SPECS if s["group"] == b2b_settings.MBQ]

# Inputs and output. The parity script swaps in the tool's B2B_* tables and a
# scratch output table; nothing else changes.
LIVE = {"bin": S.BIN_MASTER, "store": S.STORE_MASTER, "req": S.REQ,
        "grid": S.SRC_GRID, "cont": S.SRC_CONT, "out": S.ART_MBQ}

# The demand table is a clustered columnstore with a nonclustered primary key
# on (STORE_CODE, ART). Measured on 12.7M rows (upload 8, 30 Sep 2026):
#
#                              write    size     one category   one row
#   rowstore + 3 indexes       170 s    6.2 GB   0.65 s         —
#   columnstore, MAXDOP 16      75 s    0.9 GB   0.19 s         2 ms
#
# The insert is disk-bound: a rowstore B-tree insert is single-threaded and
# writes 3.8 GB, while a columnstore insert runs on every thread and writes a
# seventh of that. ROW and PAGE compression were slower still (228 s, 258 s):
# the serial insert then also compresses. The primary key is dropped before
# the insert and added after, because a nonclustered index forces a serial
# insert. The build is a rare, manual step, so it may use 16 of the 28 cores
# for about a minute; the server default is 8.
BUILD_MAXDOP = 16
# Rowstore-era indexes (build 1). Dropped if present; the columnstore needs none.
OLD_INDEXES = ("ART", "MAJ_CAT", "EXCESS")

TEMPS = ("#art", "#need", "#stk", "#acc", "#req_sz", "#cont_st", "#cont_tot",
         "#cont_co", "#cont_mix", "#cont_req")

OUT_COLS = ["STORE_CODE", "ST_NM", "RDC", "ART", "ART_NUM", "SEG", "DIV", "SUB_DIV", "MAJ_CAT",
            "BIN_SIZE", "SZ", "SZ_SOURCE", "SEASON", "CONT", "CONT_SOURCE", "CONT_EFF",
            "CONT_RULE", "ACC_D", "ACC_D_EFF", "NORM_DAYS", "SALE_COVER_DAYS", "MBQ_RAW",
            "MBQ", "MBQ_ROUNDED", "STK_TTL", "EXCESS", "SHORTFALL", "BIN_QTY", "BIN_COUNT",
            "BUILD_ID"]

# One build at a time, and never while an upload is loading (see start_build).
_job: Dict[str, Any] = {"id": None, "thread": None, "cancel": None, "cursor": None, "phase": None}
_job_lock = threading.Lock()


class Cancelled(Exception):
    pass


# ═══════════════════════════════════════════════════════════════════════════
#  settings → SQL
# ═══════════════════════════════════════════════════════════════════════════
def _on(v: Any) -> bool:
    return str(v).strip() in ("1", "True", "true")


def _list(v: Any) -> List[str]:
    return [s.strip().upper() for s in str(v or "").split(",") if s.strip()]


def plan(settings: Dict[str, str]) -> Dict[str, Any]:
    """The settings as the SQL uses them. Raises before anything is written."""
    p = {
        "short_days": int(float(settings["SHORT_DAYS"])),
        "long_days": int(float(settings["LONG_DAYS"])),
        "default_cover": int(float(settings["DEFAULT_SALE_COVER_DAYS"])),
        "short_sz": _list(settings["SHORT_SZ_LIST"]),
        "mix_rule": _on(settings["TREAT_MAJCAT_MIX_AS_SHORT"]),
        "stk_zero": _on(settings["TREAT_MISSING_STK_AS_ZERO"]),
        "apply_cont": _on(settings["CONT_APPLY"]),
        "mode": str(settings["CONT_SOURCE_MODE"]).strip().upper(),
        "full_sz": _list(settings["CONT_FULL_SZ_LIST"]),
        "fallback": float(settings["CONT_FALLBACK"] or 0),
        "mbq_min": float(settings["MBQ_MIN_WHEN_CONT"] or 0),
        "prune": _on(settings["MBQ_PRUNE_TO_REQ"]),
        "min_req": float(settings["MBQ_MIN_REQ_UNITS"] or 0),
    }
    if p["short_days"] <= 0 or p["long_days"] <= 0:
        raise ValueError("SHORT_DAYS and LONG_DAYS must both be greater than zero")
    if p["mode"] not in CONT_MODES:
        raise ValueError(f"CONT_SOURCE_MODE must be one of {', '.join(CONT_MODES)}")
    for k, name in (("fallback", "CONT_FALLBACK"), ("mbq_min", "MBQ_MIN_WHEN_CONT"),
                    ("min_req", "MBQ_MIN_REQ_UNITS"), ("default_cover", "DEFAULT_SALE_COVER_DAYS")):
        if p[k] < 0:
            raise ValueError(f"{name} must not be negative")
    return p


def _sz_list_sql(values: List[str]) -> str:
    return ", ".join("N'" + v.replace("'", "''") + "'" for v in values) or "N''"


def _short_sz_predicate(p) -> str:
    pred = f"UPPER(LTRIM(RTRIM(c.SZ))) IN ({_sz_list_sql(p['short_sz'])})"
    if p["mix_rule"]:
        # No SZ 'A_MIX' exists in the data; MIX lives in MAJ_CAT (MENS_MIX …).
        pred += " OR UPPER(RTRIM(c.MAJ_CAT)) LIKE N'%MIX'"
    return pred


def _cont_rule_sql(p) -> tuple:
    """(CONT_EFF expression, CONT_RULE expression) — the tool's _cont_rule_sql."""
    if not p["apply_cont"]:
        return "CONVERT(DECIMAL(18,6), 1)", "N'OFF'"
    if p["mode"] in ("REQ", "HYBRID"):
        eff = "CONVERT(DECIMAL(18,6), CASE WHEN c.CONT IS NULL THEN 0 ELSE c.CONT END)"
        if p["mode"] == "HYBRID":
            return eff, "ISNULL(c.CONT_RULE_SRC, N'NO_REQ_SZ')"
        return eff, "CASE WHEN c.CONT IS NULL THEN N'NO_REQ_SZ' ELSE N'REQ_SHARE' END"
    in_full = f"UPPER(LTRIM(RTRIM(c.SZ))) IN ({_sz_list_sql(p['full_sz'])})"
    eff = (f"CONVERT(DECIMAL(18,6), CASE WHEN {in_full} THEN 1 "
           f"WHEN c.CONT IS NULL THEN {p['fallback']} ELSE c.CONT END)")
    rule = (f"CASE WHEN {in_full} THEN N'FULL_SZ' WHEN c.CONT IS NULL THEN N'FALLBACK' "
            f"ELSE N'CONT' END")
    return eff, rule


def _cont_sql(p, t) -> Dict[str, str]:
    """Temp tables, join and select for the size share — one variant per mode."""
    co = CO_CODE.replace("'", "''")
    req_sz = f"""
    IF OBJECT_ID('tempdb..#req_sz') IS NOT NULL DROP TABLE #req_sz;
    SELECT  r.STORE_CODE, r.MAJ_CAT, ISNULL(r.[SIZE], N'') AS SZ_KEY,
            CONVERT(DECIMAL(18,6), SUM(CASE WHEN r.REQ > 0 THEN r.REQ ELSE 0 END)) AS REQ_SZ
    INTO #req_sz
    FROM dbo.[{t['req']}] r
    GROUP BY r.STORE_CODE, r.MAJ_CAT, ISNULL(r.[SIZE], N'');
"""
    master = f"""
    IF OBJECT_ID('tempdb..#cont_st') IS NOT NULL DROP TABLE #cont_st;
    SELECT m.ST_CD, m.MAJ_CAT, LTRIM(RTRIM(m.SZ)) AS SZ, CONVERT(DECIMAL(18,6), m.CONT) AS CONT
    INTO #cont_st
    FROM dbo.[{t['cont']}] m
    WHERE m.ST_CD <> N'{co}'
      AND EXISTS (SELECT 1 FROM dbo.[{t['store']}] s WHERE s.STORE_CODE = m.ST_CD);
    CREATE CLUSTERED INDEX IX_cont_st ON #cont_st (ST_CD, MAJ_CAT, SZ);

    IF OBJECT_ID('tempdb..#cont_tot') IS NOT NULL DROP TABLE #cont_tot;
    SELECT ST_CD, MAJ_CAT, SUM(CONT) AS TOT INTO #cont_tot FROM #cont_st GROUP BY ST_CD, MAJ_CAT;
    CREATE CLUSTERED INDEX IX_cont_tot ON #cont_tot (ST_CD, MAJ_CAT);

    IF OBJECT_ID('tempdb..#cont_co') IS NOT NULL DROP TABLE #cont_co;
    SELECT m.MAJ_CAT, LTRIM(RTRIM(m.SZ)) AS SZ, CONVERT(DECIMAL(18,6), m.CONT) AS CONT
    INTO #cont_co
    FROM dbo.[{t['cont']}] m
    WHERE m.ST_CD = N'{co}';
    CREATE CLUSTERED INDEX IX_cont_co ON #cont_co (MAJ_CAT, SZ);
"""
    if p["mode"] == "HYBRID":
        # Master where it states a positive share, REQ share for its gaps, a
        # size the store needs none of dropped to 0, renormalised to 1.00 per
        # store + category — keyed on the REQ/BIN size, the REQ cap's grain.
        build = req_sz + master + """
    IF OBJECT_ID('tempdb..#cont_mix') IS NOT NULL DROP TABLE #cont_mix;
    WITH base AS (
        SELECT  rs.STORE_CODE, rs.MAJ_CAT, rs.SZ_KEY,
                CONVERT(DECIMAL(18,6),
                    rs.REQ_SZ / NULLIF(SUM(rs.REQ_SZ) OVER (
                        PARTITION BY rs.STORE_CODE, rs.MAJ_CAT), 0)) AS REQ_SHARE
        FROM #req_sz rs
    ),
    withm AS (
        SELECT  b.*,
                COALESCE(CASE WHEN t.TOT > 0 THEN cs.CONT END, cc.CONT) AS M_CONT,
                CASE WHEN t.TOT > 0 AND cs.CONT IS NOT NULL THEN N'STORE'
                     WHEN cc.CONT IS NOT NULL                THEN N'CO'
                     ELSE NULL END AS M_SRC
        FROM base b
        LEFT JOIN #cont_tot t  ON t.ST_CD  = b.STORE_CODE AND t.MAJ_CAT  = b.MAJ_CAT
        LEFT JOIN #cont_st  cs ON cs.ST_CD = b.STORE_CODE AND cs.MAJ_CAT = b.MAJ_CAT
                              AND cs.SZ    = b.SZ_KEY
        LEFT JOIN #cont_co  cc ON cc.MAJ_CAT = b.MAJ_CAT AND cc.SZ = b.SZ_KEY
    ),
    pick AS (
        SELECT  w.*,
                CASE WHEN ISNULL(w.REQ_SHARE, 0) <= 0 THEN CONVERT(DECIMAL(18,6), 0)
                     WHEN w.M_CONT > 0               THEN w.M_CONT
                     ELSE w.REQ_SHARE END AS RAW_CONT,
                CASE WHEN ISNULL(w.REQ_SHARE, 0) <= 0 THEN NULL
                     WHEN w.M_CONT > 0               THEN w.M_SRC
                     ELSE N'REQ' END AS SRC
        FROM withm w
    )
    SELECT  STORE_CODE, MAJ_CAT, SZ_KEY,
            CONVERT(DECIMAL(18,6),
                RAW_CONT / NULLIF(SUM(RAW_CONT) OVER (
                    PARTITION BY STORE_CODE, MAJ_CAT), 0)) AS CONT,
            SRC AS CONT_SOURCE,
            CASE WHEN SRC IS NULL  THEN N'NO_REQ_SZ'
                 WHEN SRC = N'REQ' THEN N'REQ_FB_NORM'
                 ELSE N'MASTER_NORM' END AS CONT_RULE_SRC
    INTO #cont_mix
    FROM pick;
    CREATE CLUSTERED INDEX IX_cont_mix ON #cont_mix (STORE_CODE, MAJ_CAT, SZ_KEY);
"""
        join = """
        LEFT JOIN #cont_mix cm ON cm.STORE_CODE = c.STORE_CODE AND cm.MAJ_CAT = c.MAJ_CAT
                              AND cm.SZ_KEY = ISNULL(c.BIN_SIZE, N'')"""
        select = "cm.CONT AS CONT, cm.CONT_SOURCE AS CONT_SOURCE, cm.CONT_RULE_SRC AS CONT_RULE_SRC"
        mix = "#cont_mix"
    elif p["mode"] == "REQ":
        build = req_sz + """
    IF OBJECT_ID('tempdb..#cont_req') IS NOT NULL DROP TABLE #cont_req;
    SELECT  STORE_CODE, MAJ_CAT, SZ_KEY,
            CONVERT(DECIMAL(18,6),
                REQ_SZ / NULLIF(SUM(REQ_SZ) OVER (PARTITION BY STORE_CODE, MAJ_CAT), 0)) AS CONT
    INTO #cont_req
    FROM #req_sz;
    CREATE CLUSTERED INDEX IX_cont_req ON #cont_req (STORE_CODE, MAJ_CAT, SZ_KEY);
"""
        join = """
        LEFT JOIN #cont_req cr ON cr.STORE_CODE = c.STORE_CODE AND cr.MAJ_CAT = c.MAJ_CAT
                              AND cr.SZ_KEY = ISNULL(c.BIN_SIZE, N'')"""
        select = "cr.CONT AS CONT, CASE WHEN cr.CONT IS NOT NULL THEN N'REQ' END AS CONT_SOURCE"
        mix = "#cont_req"
    else:
        build = master
        join = """
        LEFT JOIN #cont_tot t  ON t.ST_CD  = c.STORE_CODE AND t.MAJ_CAT  = c.MAJ_CAT
        LEFT JOIN #cont_st  cs ON cs.ST_CD = c.STORE_CODE AND cs.MAJ_CAT = c.MAJ_CAT
                              AND cs.SZ = c.SZ
        LEFT JOIN #cont_co  cc ON cc.MAJ_CAT = c.MAJ_CAT AND cc.SZ = c.SZ"""
        select = """COALESCE(CASE WHEN t.TOT > 0 THEN cs.CONT END, cc.CONT) AS CONT,
                CASE WHEN t.TOT > 0 AND cs.CONT IS NOT NULL THEN N'STORE'
                     WHEN cc.CONT IS NOT NULL                THEN N'CO'
                     ELSE NULL END AS CONT_SOURCE"""
        mix = None                       # MASTER does not renormalise
    return {"build": build, "join": join, "select": select, "mix": mix}


def _steps(p, t, build_id: Optional[int]) -> List[Dict[str, Any]]:
    """The build as ordered steps. Rows go into <out>_STAGE, never into the
    live table; _swap_sql puts them live once they have passed _verify."""
    cont = _cont_sql(p, t)
    pred = _short_sz_predicate(p)
    cont_eff, cont_rule = _cont_rule_sql(p)
    stk = "COALESCE(k.STK_TTL, 0)" if p["stk_zero"] else "k.STK_TTL"
    combo_from = (f"""
        FROM #need n
        JOIN #art a ON a.MAJ_CAT = n.MAJ_CAT AND a.SZ_KEY = n.SZ_KEY
        JOIN dbo.[{t['store']}] s ON s.STORE_CODE = n.STORE_CODE""" if p["prune"] else f"""
        FROM dbo.[{t['store']}] s
        CROSS JOIN #art a""")
    out = t["out"]
    stage = f"{out}_STAGE"
    bid = "NULL" if build_id is None else int(build_id)
    return [
        {"key": "art", "label": "Reading articles from Bin Master", "pct": 5, "sql": f"""
    SET NOCOUNT ON;
    IF OBJECT_ID('tempdb..#art') IS NOT NULL DROP TABLE #art;
    SELECT  ART, TRY_CONVERT(BIGINT, ART) AS ART_NUM,
            MAX(SEG) AS SEG, MAX(DIV) AS DIV, MAX(SUB_DIV) AS SUB_DIV, MAX(MAJ_CAT) AS MAJ_CAT,
            MAX([SIZE]) AS BIN_SIZE, ISNULL(MAX([SIZE]), N'') AS SZ_KEY, MAX(SEASON) AS SEASON,
            SUM(QTY) AS BIN_QTY, COUNT(DISTINCT BIN) AS BIN_COUNT
    INTO #art
    FROM dbo.[{t['bin']}]
    GROUP BY ART;
    CREATE CLUSTERED INDEX IX_art_cat ON #art (MAJ_CAT, SZ_KEY);
    CREATE INDEX IX_art_num ON #art (ART_NUM);"""},

        {"key": "need", "label": "Reading what each store requires", "pct": 10, "sql": f"""
    SET NOCOUNT ON;
    IF OBJECT_ID('tempdb..#need') IS NOT NULL DROP TABLE #need;
    SELECT  r.STORE_CODE, r.MAJ_CAT, ISNULL(r.[SIZE], N'') AS SZ_KEY,
            CONVERT(DECIMAL(18,4), SUM(r.REQ)) AS REQ_UNITS
    INTO #need
    FROM dbo.[{t['req']}] r
    JOIN dbo.[{t['store']}] s ON s.STORE_CODE = r.STORE_CODE     -- the Store Master gate
    GROUP BY r.STORE_CODE, r.MAJ_CAT, ISNULL(r.[SIZE], N'')
    HAVING SUM(r.REQ) >= {p['min_req']};
    CREATE CLUSTERED INDEX IX_need ON #need (MAJ_CAT, SZ_KEY);

    IF OBJECT_ID('tempdb..#acc') IS NOT NULL DROP TABLE #acc;
    SELECT  STORE_CODE, MAJ_CAT, MAX(ACC_D) AS ACC_D, MAX(SALE_COVER_DAYS) AS SALE_COVER_DAYS
    INTO #acc
    FROM dbo.[{t['req']}]
    GROUP BY STORE_CODE, MAJ_CAT;                                 -- size is discarded here
    CREATE CLUSTERED INDEX IX_acc ON #acc (STORE_CODE, MAJ_CAT);"""},

        {"key": "stk", "label": "Reading store stock from the grid", "pct": 20, "sql": f"""
    SET NOCOUNT ON;
    IF OBJECT_ID('tempdb..#stk') IS NOT NULL DROP TABLE #stk;
    SELECT  g.[WERKS] AS STORE_CODE, g.[ARTICLE_NUMBER] AS ART_NUM,
            SUM(CONVERT(DECIMAL(18,4), g.[STK_TTL])) AS STK_TTL, MAX(g.[SZ]) AS SZ
    INTO #stk
    FROM dbo.[{t['grid']}] g
    WHERE EXISTS (SELECT 1 FROM dbo.[{t['store']}] s WHERE s.STORE_CODE = g.[WERKS])
      AND EXISTS (SELECT 1 FROM #art a WHERE a.ART_NUM = g.[ARTICLE_NUMBER])
    GROUP BY g.[WERKS], g.[ARTICLE_NUMBER];                       -- not unique in the grid
    CREATE CLUSTERED INDEX IX_stk ON #stk (STORE_CODE, ART_NUM);"""},

        {"key": "cont", "label": f"Working out size shares ({p['mode']})", "pct": 40,
         "sql": "SET NOCOUNT ON;\n" + cont["build"], "mix": cont["mix"]},

        {"key": "stage", "label": "Preparing an empty staging table", "pct": 45, "sql": f"""
    SET NOCOUNT ON;
    DROP TABLE IF EXISTS dbo.[{stage}];
    SELECT TOP 0 * INTO dbo.[{stage}] FROM dbo.[{out}];         -- same columns, so SWITCH fits
    CREATE CLUSTERED COLUMNSTORE INDEX [CCI_{stage}] ON dbo.[{stage}];"""},

        {"key": "write", "label": "Writing the demand rows", "pct": 50, "sql": f"""
    SET NOCOUNT ON;
    WITH combo AS (
        SELECT  s.STORE_CODE, s.ST_NM, s.RDC, a.ART, a.ART_NUM,
                a.SEG, a.DIV, a.SUB_DIV, a.MAJ_CAT, a.BIN_SIZE, a.SEASON,
                a.BIN_QTY, a.BIN_COUNT,
                COALESCE(NULLIF(LTRIM(RTRIM(k.SZ)), N''), a.BIN_SIZE) AS SZ,
                CASE WHEN NULLIF(LTRIM(RTRIM(k.SZ)), N'') IS NOT NULL
                     THEN N'GRID' ELSE N'BIN' END AS SZ_SOURCE,
                acc.ACC_D,
                COALESCE(acc.SALE_COVER_DAYS, {p['default_cover']}) AS SALE_COVER_DAYS,
                {stk} AS STK_TTL
        {combo_from}
        LEFT JOIN #acc acc ON acc.STORE_CODE = s.STORE_CODE AND acc.MAJ_CAT = a.MAJ_CAT
        LEFT JOIN #stk k   ON k.STORE_CODE  = s.STORE_CODE AND k.ART_NUM  = a.ART_NUM
    ),
    withcont AS (
        SELECT  c.*, {cont['select']}
        FROM combo c{cont['join']}
    ),
    calc AS (
        SELECT  c.*,
                CASE WHEN {pred} THEN {p['short_days']} ELSE {p['long_days']} END AS NORM_DAYS,
                {cont_eff} AS CONT_EFF,
                {cont_rule} AS CONT_RULE
        FROM withcont c
    ),
    scaled AS (
        SELECT calc.*, CONVERT(DECIMAL(18,6), CONVERT(DECIMAL(38,10), ACC_D) * CONT_EFF) AS ACC_D_EFF
        FROM calc
    ),
    raw AS (
        SELECT  scaled.*,
                CONVERT(DECIMAL(18,4),
                    CONVERT(DECIMAL(38,10), ACC_D_EFF)
                  + CONVERT(DECIMAL(38,10), ACC_D_EFF) / NORM_DAYS * SALE_COVER_DAYS) AS MBQ_RAW
        FROM scaled
    ),
    final AS (
        SELECT  raw.*,
                CASE WHEN MBQ_RAW IS NOT NULL AND CONT_EFF > 0 AND MBQ_RAW < {p['mbq_min']}
                     THEN CONVERT(DECIMAL(18,4), {p['mbq_min']})
                     ELSE MBQ_RAW END AS MBQ
        FROM raw
    ),
    rounded AS (
        SELECT final.*, CONVERT(INT, ROUND(MBQ, 0)) AS MBQ_RND   -- stock moves in whole units
        FROM final
    )
    INSERT INTO dbo.[{stage}] WITH (TABLOCK) ({', '.join(OUT_COLS)})
    SELECT  STORE_CODE, ST_NM, RDC, ART, ART_NUM, SEG, DIV, SUB_DIV, MAJ_CAT,
            BIN_SIZE, SZ, SZ_SOURCE, SEASON, CONT, CONT_SOURCE, CONT_EFF, CONT_RULE,
            ACC_D, ACC_D_EFF, NORM_DAYS, SALE_COVER_DAYS, MBQ_RAW, MBQ, MBQ_RND, STK_TTL,
            CASE WHEN STK_TTL IS NULL OR MBQ_RND IS NULL THEN NULL
                 ELSE CONVERT(DECIMAL(18,4), CASE WHEN STK_TTL - MBQ_RND > 0
                                                  THEN STK_TTL - MBQ_RND ELSE 0 END) END,
            CASE WHEN STK_TTL IS NULL OR MBQ_RND IS NULL THEN NULL
                 ELSE CONVERT(DECIMAL(18,4), CASE WHEN MBQ_RND - STK_TTL > 0
                                                  THEN MBQ_RND - STK_TTL ELSE 0 END) END,
            BIN_QTY, BIN_COUNT, {bid}
    FROM rounded
    OPTION (MAXDOP {BUILD_MAXDOP});"""},

        {"key": "index", "label": "Adding the primary key", "pct": 75, "sql": f"""
    SET NOCOUNT ON;
    ALTER TABLE dbo.[{stage}] ADD CONSTRAINT [PK_{stage}]
        PRIMARY KEY NONCLUSTERED (STORE_CODE, ART) WITH (MAXDOP = {BUILD_MAXDOP});"""},
    ]


def _swap_sql(out: str) -> str:
    """Replace the live table with the checked staging table in one short
    transaction: metadata only, so readers wait milliseconds, not minutes.
    The live table is first given the staging table's shape (columnstore,
    nonclustered key) — it is empty by then, so that is instant too."""
    stage = f"{out}_STAGE"
    return f"""
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    {' '.join(f"DROP INDEX IF EXISTS [IX_{out}_{n}] ON dbo.[{out}];" for n in OLD_INDEXES)}
    TRUNCATE TABLE dbo.[{out}];
    IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE object_id = OBJECT_ID(N'dbo.[{out}]') AND type = 5)
    BEGIN
        DECLARE @pk sysname = (SELECT name FROM sys.key_constraints
                                WHERE parent_object_id = OBJECT_ID(N'dbo.[{out}]') AND type = 'PK');
        IF @pk IS NOT NULL
            EXEC (N'ALTER TABLE dbo.[{out}] DROP CONSTRAINT [' + @pk + N']');
        CREATE CLUSTERED COLUMNSTORE INDEX [CCI_{out}] ON dbo.[{out}];
        ALTER TABLE dbo.[{out}] ADD CONSTRAINT [PK_{out}] PRIMARY KEY NONCLUSTERED (STORE_CODE, ART);
    END
    ALTER TABLE dbo.[{stage}] SWITCH TO dbo.[{out}];
    DROP TABLE dbo.[{stage}];"""


# ═══════════════════════════════════════════════════════════════════════════
#  the build itself
# ═══════════════════════════════════════════════════════════════════════════
def _exec(cur, sql: str) -> None:
    cur.execute(sql)
    while cur.nextset():
        pass


def _rows(cur, sql: str) -> List[tuple]:
    cur.execute(sql)
    return [tuple(r) for r in cur.fetchall()]


def _f(x) -> float:
    return float(x or 0)


def _check(code, level, title, detail="", *, count=None, units=None, sample=None):
    return {"code": code, "level": level, "title": title, "detail": detail,
            "count": count, "units": units, "sample": [str(s) for s in (sample or [])][:25]}


def build_into(t: Dict[str, str], settings: Dict[str, str], build_id: Optional[int] = None,
               progress: Callable[[str, int], None] = lambda m, p: None,
               cancelled: Callable[[], bool] = lambda: False,
               on_cursor: Callable[[Any], None] = lambda c: None,
               on_phase: Callable[[str], None] = lambda ph: None) -> Dict[str, Any]:
    """Build the demand table into t['out'] and prove it. Returns
    {rows, summary, checks, passed, steps}.

    Rows are written to <out>_STAGE and checked there. Only a build that
    passes is switched in, in one metadata-only transaction; until that
    moment the live table is untouched and readable. A failure, a cancel or
    a failed check drops the staging table and leaves the live one as it was."""
    p = plan(settings)
    stage = f"{t['out']}_STAGE"
    raw = data_engine.raw_connection()
    cur = raw.cursor()
    on_cursor(cur)
    steps: Dict[str, float] = {}
    mix = None
    broken = False
    try:
        for st in _steps(p, t, build_id):
            if cancelled():
                raise Cancelled()
            progress(st["label"], st["pct"])
            t0 = time.time()
            _exec(cur, st["sql"])
            raw.commit()                          # each step on its own; nothing live yet
            steps[st["key"]] = round(time.time() - t0, 1)
            mix = st.get("mix", mix)

        if cancelled():
            raise Cancelled()
        progress("Checking the result", 85)
        t0 = time.time()
        summary, checks, passed = _verify(cur, p, {**t, "out": stage}, mix)
        raw.commit()
        steps["verify"] = round(time.time() - t0, 1)
        if cancelled():
            raise Cancelled()
        if not passed:
            _exec(cur, f"DROP TABLE IF EXISTS dbo.[{stage}]")
            raw.commit()
            return {"rows": summary.get("rows", 0), "summary": summary, "checks": checks,
                    "passed": False, "steps": steps}

        progress("Switching the new demand in", 97)
        on_phase("write")
        t0 = time.time()
        _exec(cur, _swap_sql(t["out"]))
        raw.commit()
        steps["swap"] = round(time.time() - t0, 2)
        return {"rows": summary["rows"], "summary": summary, "checks": checks,
                "passed": True, "steps": steps}
    except BaseException:
        broken = True
        try:
            raw.rollback()
        except Exception:
            pass
        raise
    finally:
        on_cursor(None)
        on_phase(None)
        if broken:
            try:                                  # the staging table, on a fresh connection
                with data_engine.begin() as c:
                    c.execute(text(f"DROP TABLE IF EXISTS dbo.[{stage}]"))
            except Exception:
                pass
        try:
            if broken:
                raw.invalidate()                  # a cancelled statement: do not pool it
            else:                                 # temp tables live as long as the session
                for tmp in TEMPS:
                    cur.execute(f"IF OBJECT_ID('tempdb..{tmp}') IS NOT NULL DROP TABLE {tmp};")
                # NOCOUNT/XACT_ABORT are session settings: a pooled connection
                # left with NOCOUNT ON reports rowcount -1 to its next user.
                cur.execute("SET NOCOUNT OFF; SET XACT_ABORT OFF;")
                raw.commit()
        except Exception:
            try:
                raw.invalidate()                  # could not reset it: do not pool it
            except Exception:
                pass
        try:
            raw.close()
        except Exception:
            pass


def _verify(cur, p, t, mix: Optional[str]):
    """Prove the output inside the build's transaction. Integrity checks fail
    the build; the rest describe it."""
    out = t["out"]
    checks: List[Dict[str, Any]] = []

    # One scan: every total, per category and size-share rule, plus the
    # formula checks. Python adds the groups up.
    g = _rows(cur, f"""
        SELECT  MAJ_CAT, ISNULL(CONT_RULE, N'(none)'), ISNULL(CONT_SOURCE, N'(none)'),
                COUNT(*),
                SUM(CASE WHEN SHORTFALL > 0 THEN 1 ELSE 0 END),
                SUM(ISNULL(SHORTFALL, 0)),
                SUM(CASE WHEN EXCESS > 0 THEN 1 ELSE 0 END),
                SUM(ISNULL(EXCESS, 0)),
                SUM(CAST(ISNULL(MBQ_ROUNDED, 0) AS BIGINT)),
                SUM(ISNULL(STK_TTL, 0)),
                SUM(CASE WHEN ACC_D IS NULL THEN 1 ELSE 0 END),
                SUM(CASE WHEN STK_TTL IS NULL THEN 1 ELSE 0 END),
                SUM(CASE WHEN NORM_DAYS = {p['short_days']} THEN 1 ELSE 0 END),
                SUM(CASE WHEN ISNULL(MBQ_ROUNDED, 0) = 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN SZ_SOURCE = N'BIN' THEN 1 ELSE 0 END),
                SUM(CASE WHEN STK_TTL IS NOT NULL AND MBQ_ROUNDED IS NOT NULL AND (
                           SHORTFALL <> CASE WHEN MBQ_ROUNDED - STK_TTL > 0 THEN MBQ_ROUNDED - STK_TTL ELSE 0 END
                        OR EXCESS    <> CASE WHEN STK_TTL - MBQ_ROUNDED > 0 THEN STK_TTL - MBQ_ROUNDED ELSE 0 END)
                         THEN 1 ELSE 0 END),
                SUM(CASE WHEN MBQ IS NOT NULL AND MBQ_ROUNDED <> CONVERT(INT, ROUND(MBQ, 0)) THEN 1 ELSE 0 END),
                SUM(CASE WHEN SHORTFALL < 0 OR EXCESS < 0 OR MBQ < 0 THEN 1 ELSE 0 END),
                SUM(CASE WHEN (STK_TTL IS NULL OR MBQ_ROUNDED IS NULL)
                          AND (SHORTFALL IS NOT NULL OR EXCESS IS NOT NULL) THEN 1 ELSE 0 END)
          FROM dbo.[{out}]
         GROUP BY MAJ_CAT, CONT_RULE, CONT_SOURCE""")
    tot = {k: 0.0 for k in ("rows", "rows_short", "shortfall", "rows_excess", "excess", "mbq", "stk",
                            "no_acc_d", "no_stock_row", "short_days", "mbq_zero", "sz_from_bin",
                            "bad_gap", "bad_round", "negative", "bad_null")}
    cats: Dict[str, Dict[str, float]] = {}
    rules: Dict[str, int] = {}
    for r in g:
        vals = dict(zip(tot.keys(), [_f(x) for x in r[3:]]))
        for k, v in vals.items():
            tot[k] += v
        c = cats.setdefault(r[0] or "(none)", {"rows": 0, "rows_short": 0, "shortfall": 0, "excess": 0, "mbq": 0})
        for k in c:
            c[k] += vals[k]
        rk = f"{r[1]} · {r[2]}"
        rules[rk] = rules.get(rk, 0) + int(vals["rows"])

    # I2 — the output agrees with its own formula
    bad = {"SHORTFALL/EXCESS vs MBQ_ROUNDED − STK_TTL": tot["bad_gap"],
           "MBQ_ROUNDED vs ROUND(MBQ)": tot["bad_round"],
           "negative SHORTFALL, EXCESS or MBQ": tot["negative"],
           "SHORTFALL set where stock or target is unknown": tot["bad_null"]}
    nbad = int(sum(bad.values()))
    checks.append(_check("MBQ_FORMULA", "block" if nbad else "ok",
                         f"{nbad:,} row(s) disagree with the formula" if nbad
                         else "Every row agrees with the formula",
                         " · ".join(f"{k}: {int(v):,}" for k, v in bad.items() if v)
                         or "SHORTFALL = MAX(MBQ_ROUNDED − STK_TTL, 0) and EXCESS = MAX(STK_TTL − MBQ_ROUNDED, 0) on every row.",
                         count=nbad))

    # I1 — each store + category's size mix sums to 1.00
    if mix:
        r = _rows(cur, f"""
            SELECT COUNT(*), SUM(CASE WHEN ABS(s - 1) > {SHARE_TOL} THEN 1 ELSE 0 END), MAX(ABS(s - 1))
              FROM (SELECT SUM(CONT) s FROM {mix} GROUP BY STORE_CODE, MAJ_CAT) x
             WHERE s IS NOT NULL""")[0]
        groups, off, worst = int(r[0] or 0), int(r[1] or 0), _f(r[2])
        checks.append(_check("MBQ_SHARES", "block" if off else "ok",
                             f"{off:,} of {groups:,} store + category size mixes do not add up to 100%" if off
                             else f"All {groups:,} store + category size mixes add up to 100%",
                             f"Largest gap {worst:.6f}. Allowed {SHARE_TOL}.", count=off))
    else:
        checks.append(_check("MBQ_SHARES", "info", "Size shares are not renormalised in MASTER mode",
                             "MASTER reads Master_CONT_SZ as it is. HYBRID or REQ make each mix add up to 100%."))

    # Stock that was assumed rather than read
    r = _rows(cur, f"""
        SELECT COUNT(*), SUM(ISNULL(o.SHORTFALL, 0)), SUM(CAST(ISNULL(o.MBQ_ROUNDED, 0) AS BIGINT))
          FROM dbo.[{out}] o
         WHERE NOT EXISTS (SELECT 1 FROM #stk k WHERE k.STORE_CODE = o.STORE_CODE AND k.ART_NUM = o.ART_NUM)""")[0]
    miss, miss_sf, miss_mbq = int(r[0] or 0), _f(r[1]), _f(r[2])
    share = miss / tot["rows"] * 100 if tot["rows"] else 0
    if miss and p["stk_zero"]:
        checks.append(_check("MBQ_STOCK", "warn",
                             f"{share:.1f}% of rows have no stock row in the grid and count as zero stock",
                             f"{miss:,} store × article rows. Their whole target becomes shortfall: "
                             f"{miss_sf:,.0f} of the {tot['shortfall']:,.0f} units short rest on this "
                             f"assumption (TREAT_MISSING_STK_AS_ZERO = On).", count=miss, units=miss_sf))
    elif miss:
        checks.append(_check("MBQ_STOCK", "info",
                             f"{miss:,} rows have no stock row in the grid, so they are left out",
                             f"Their SHORTFALL is blank and allocation will not consider them "
                             f"({miss_mbq:,.0f} units of target). TREAT_MISSING_STK_AS_ZERO = Off.",
                             count=miss, units=miss_mbq))
    else:
        checks.append(_check("MBQ_STOCK", "ok", "Every row's stock was read from the grid"))

    if tot["no_acc_d"]:
        checks.append(_check("MBQ_ACC_D", "warn", f"{int(tot['no_acc_d']):,} rows have no ACC_D, so no target",
                             "Their REQ lines leave ACC_D blank for that store and category.",
                             count=int(tot["no_acc_d"])))

    # Bin stock no store can receive: articles with no demand row, by reason.
    # The same classes the tool writes as LEFT_REASON on its leftovers.
    left = _rows(cur, f"""
        WITH req_any AS (
            SELECT MAJ_CAT, ISNULL([SIZE], N'') SZ_KEY, SUM(CASE WHEN REQ > 0 THEN REQ ELSE 0 END) REQ_POS
              FROM dbo.[{t['req']}] GROUP BY MAJ_CAT, ISNULL([SIZE], N'')),
        req_master AS (
            SELECT r.MAJ_CAT, ISNULL(r.[SIZE], N'') SZ_KEY, SUM(CASE WHEN r.REQ > 0 THEN r.REQ ELSE 0 END) REQ_POS
              FROM dbo.[{t['req']}] r JOIN dbo.[{t['store']}] s ON s.STORE_CODE = r.STORE_CODE
             GROUP BY r.MAJ_CAT, ISNULL(r.[SIZE], N'')),
        req_cat AS (SELECT MAJ_CAT FROM dbo.[{t['req']}] GROUP BY MAJ_CAT),
        lost AS (
            SELECT a.MAJ_CAT, a.BIN_QTY,
                   CASE WHEN rc.MAJ_CAT IS NULL          THEN N'NOT_IN_REQ'
                        WHEN ISNULL(ra.REQ_POS, 0) = 0   THEN N'NO_SEASON_REQ'
                        WHEN ISNULL(rm.REQ_POS, 0) = 0   THEN N'NO_STORE_MASTER'
                        ELSE N'NO_STORE_NEED' END AS REASON
              FROM #art a
              LEFT JOIN req_cat rc    ON rc.MAJ_CAT = a.MAJ_CAT
              LEFT JOIN req_any ra    ON ra.MAJ_CAT = a.MAJ_CAT AND ra.SZ_KEY = a.SZ_KEY
              LEFT JOIN req_master rm ON rm.MAJ_CAT = a.MAJ_CAT AND rm.SZ_KEY = a.SZ_KEY
             WHERE NOT EXISTS (SELECT 1 FROM dbo.[{out}] o WHERE o.ART = a.ART))
        SELECT REASON, MAJ_CAT, COUNT(*), SUM(CAST(BIN_QTY AS BIGINT))
          FROM lost GROUP BY REASON, MAJ_CAT""")
    supply = _rows(cur, "SELECT COUNT(*), SUM(CAST(BIN_QTY AS BIGINT)) FROM #art")[0]
    bin_pcs = _f(supply[1])
    reasons: Dict[str, Dict[str, Any]] = {}
    for reason, cat, arts, pcs in left:
        x = reasons.setdefault(reason, {"articles": 0, "pcs": 0, "cats": []})
        x["articles"] += int(arts)
        x["pcs"] += int(pcs or 0)
        x["cats"].append((cat, int(pcs or 0)))
    lost_pcs = sum(x["pcs"] for x in reasons.values())
    WHY = {"NO_STORE_MASTER": ("warn", "wanted only by stores missing from Store Master — reload Store Master"),
           "NO_SEASON_REQ": ("info", "every store asked for zero of that category + size"),
           "NOT_IN_REQ": ("info", "the category is not in REQ at all (bags, hangers …)"),
           "NO_STORE_NEED": ("info", "wanted, but no single store asks for MBQ_MIN_REQ_UNITS of it")}
    if lost_pcs:
        checks.append(_check("MBQ_UNREACHABLE", "warn" if "NO_STORE_MASTER" in reasons else "info",
                             f"{lost_pcs:,} pcs of bin stock ({lost_pcs / bin_pcs * 100 if bin_pcs else 0:.1f}%) "
                             f"have no store that can receive them",
                             " · ".join(f"{k} {v['pcs']:,} pcs ({v['articles']:,} articles): {WHY.get(k, ('', ''))[1]}"
                                        for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]["pcs"])),
                             units=lost_pcs,
                             sample=[f"{k}: {c} ({q:,})" for k, v in reasons.items()
                                     for c, q in sorted(v["cats"], key=lambda z: -z[1])[:6]]))
    else:
        checks.append(_check("MBQ_UNREACHABLE", "ok", "Every article in Bin Master has at least one store row"))

    d = _rows(cur, f"SELECT COUNT(DISTINCT STORE_CODE), COUNT(DISTINCT ART) FROM dbo.[{out}]")[0]
    by_cat_supply = {r[0]: (int(r[1]), int(r[2] or 0)) for r in _rows(
        cur, "SELECT MAJ_CAT, COUNT(*), SUM(CAST(BIN_QTY AS BIGINT)) FROM #art GROUP BY MAJ_CAT")}
    categories = []
    for cat in sorted(set(cats) | set(by_cat_supply)):
        c = cats.get(cat, {"rows": 0, "rows_short": 0, "shortfall": 0, "excess": 0, "mbq": 0})
        arts, pcs = by_cat_supply.get(cat, (0, 0))
        categories.append({"maj_cat": cat, "rows": int(c["rows"]), "rows_short": int(c["rows_short"]),
                           "shortfall": round(c["shortfall"], 1), "excess": round(c["excess"], 1),
                           "mbq": int(c["mbq"]), "bin_articles": arts, "bin_pcs": pcs})
    categories.sort(key=lambda c: -c["shortfall"])

    summary = {
        "rows": int(tot["rows"]), "stores": int(d[0] or 0), "articles": int(d[1] or 0),
        "categories": len(cats), "rows_short": int(tot["rows_short"]),
        "shortfall": round(tot["shortfall"], 1), "rows_excess": int(tot["rows_excess"]),
        "excess": round(tot["excess"], 1), "mbq": int(tot["mbq"]), "stock": round(tot["stk"], 1),
        "no_acc_d": int(tot["no_acc_d"]), "no_stock_row": int(tot["no_stock_row"]),
        "grid_missing": miss, "short_days": int(tot["short_days"]), "mbq_zero": int(tot["mbq_zero"]),
        "sz_from_bin": int(tot["sz_from_bin"]), "bin_pcs": int(bin_pcs),
        "bin_articles": int(supply[0] or 0), "unreachable_pcs": int(lost_pcs),
        "unreachable": {k: {"articles": v["articles"], "pcs": v["pcs"]} for k, v in reasons.items()},
        "cont_rules": dict(sorted(rules.items(), key=lambda kv: -kv[1])),
        "by_category": categories,
        "mode": p["mode"], "prune": p["prune"], "stk_zero": p["stk_zero"],
    }
    passed = not any(c["level"] == "block" for c in checks)
    order = {"block": 0, "warn": 1, "info": 2, "ok": 3}
    checks.sort(key=lambda c: order.get(c["level"], 9))
    return summary, checks, passed


# ═══════════════════════════════════════════════════════════════════════════
#  job
# ═══════════════════════════════════════════════════════════════════════════
def _update(build_id: int, **cols) -> None:
    if not cols:
        return
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    with data_engine.begin() as conn:
        conn.execute(text(f"UPDATE dbo.{S.MBQ_BUILD} SET {sets} WHERE BUILD_ID = :id"),
                     {**cols, "id": build_id})


def running_build() -> Optional[int]:
    with _job_lock:
        t = _job["thread"]
        if t is not None and t.is_alive():
            return _job["id"]
        return None


def writing() -> Optional[int]:
    """The build id while a build holds the demand table in its transaction."""
    with _job_lock:
        return _job["id"] if _job["phase"] == "write" and _job["thread"] and _job["thread"].is_alive() else None


def _last_change(conn, table: str, with_kind: bool = False):
    """Last insert/update/delete on a table. The usage DMV is reset by any
    ALTER or index rebuild (seen on the grid, 30 Sep 11:13 and 14:32), so when
    it has nothing, the table's modify_date stands in — later than or equal to
    the true last data change, so the worst case is an unneeded "rebuild" note.
    with_kind=True also says which it was: 'data' or 'altered'."""
    r = conn.execute(text("""
        SELECT (SELECT MAX(last_user_update) FROM sys.dm_db_index_usage_stats
                 WHERE database_id = DB_ID() AND object_id = OBJECT_ID(:t)),
               (SELECT modify_date FROM sys.tables WHERE object_id = OBJECT_ID(:t))"""),
        {"t": f"dbo.{table}"}).fetchone()
    at, kind = (r[0], "data") if r[0] else (r[1], "altered")
    return (at, kind) if with_kind else at


def start_build(user: Optional[str]) -> int:
    from app.services import b2b_upload_service as U
    S.ensure_tables()
    settings = b2b_settings.effective()
    plan(settings)                                   # bad settings fail here, not in the job
    with U._admission:                               # shared with the upload's load
        busy = U.running_job()
        if busy and (U.get(busy) or {}).get("STATUS") == "LOADING":
            raise RuntimeError(f"Upload {busy} is loading. Build when it finishes.")
        mine = running_build()
        if mine:
            raise RuntimeError(f"Build {mine} is still running.")
        from app.services import b2b_alloc_service as A    # lazy: A imports this module
        if A.running_run():
            raise RuntimeError(f"Allocation session {A.running_run()} is reading the demand table. "
                               f"Build when it finishes.")
        last = U.last_loaded()
        if not last:
            raise ValueError("Nothing has been loaded yet. Load a workbook first.")
        used = {k: settings[k] for k in MBQ_KEYS}
        with data_engine.begin() as conn:
            grid_at = _last_change(conn, S.SRC_GRID)
            cont_at = _last_change(conn, S.SRC_CONT)
            build_id = conn.execute(text(f"""
                INSERT INTO dbo.{S.MBQ_BUILD}
                    (STATUS, UPLOAD_ID, SETTINGS_JSON, PROGRESS, PROGRESS_PCT, CREATED_BY,
                     GRID_UPDATED_AT, CONT_UPDATED_AT)
                OUTPUT INSERTED.BUILD_ID
                VALUES ('RUNNING', :u, :s, 'Starting', 0, :by, :g, :c)"""),
                {"u": last["UPLOAD_ID"], "s": json.dumps(used), "by": user,
                 "g": grid_at, "c": cont_at}).scalar()
        ev = threading.Event()
        t = threading.Thread(target=_run, args=(build_id, settings, ev, user),
                             name=f"b2b-mbq-{build_id}", daemon=True)
        with _job_lock:
            _job.update(id=build_id, thread=t, cancel=ev, cursor=None, phase=None)
        t.start()
    return build_id


def _run(build_id: int, settings: Dict[str, str], ev: threading.Event, user: Optional[str]) -> None:
    t0 = time.time()

    def progress(msg, pct):
        logger.info(f"[b2b mbq {build_id}] {msg}")
        _update(build_id, PROGRESS=msg[:300], PROGRESS_PCT=int(pct))

    def set_cursor(c):
        with _job_lock:
            _job["cursor"] = c

    def set_phase(ph):
        with _job_lock:
            _job["phase"] = ph

    try:
        res = build_into(LIVE, settings, build_id, progress=progress, cancelled=ev.is_set,
                         on_cursor=set_cursor, on_phase=set_phase)
        took = round(time.time() - t0, 1)
        s = res["summary"]
        common = dict(SUMMARY_JSON=json.dumps(s), CHECKS_JSON=json.dumps(res["checks"]),
                      CHECKS_PASSED=1 if res["passed"] else 0, STEPS_JSON=json.dumps(res["steps"]),
                      FINISHED_AT=datetime.now(), DURATION_SEC=took, PROGRESS_PCT=100)
        if res["passed"]:
            _update(build_id, STATUS="DONE", ROWS_BUILT=s["rows"], ROWS_SHORT=s["rows_short"],
                    ROWS_EXCESS=s["rows_excess"], SHORTFALL_TOTAL=s["shortfall"],
                    EXCESS_TOTAL=s["excess"],
                    PROGRESS=f"Built {s['rows']:,} rows in {took:,.0f}s", **common)
            logger.info(f"[b2b mbq {build_id}] done: {s['rows']:,} rows in {took}s by {user}")
        else:
            _update(build_id, STATUS="FAILED", ERROR="The build's own checks failed; it was rolled back.",
                    PROGRESS="Checks failed. Rolled back — the previous demand is unchanged.", **common)
    except Exception as e:
        took = round(time.time() - t0, 1)
        if ev.is_set():
            _update(build_id, STATUS="CANCELLED", FINISHED_AT=datetime.now(), DURATION_SEC=took,
                    PROGRESS="Cancelled. Nothing changed — the previous demand is kept.")
        else:
            logger.exception(f"[b2b mbq {build_id}] failed")
            _update(build_id, STATUS="FAILED", FINISHED_AT=datetime.now(), DURATION_SEC=took,
                    PROGRESS="Failed. Rolled back — the previous demand is unchanged.",
                    ERROR=f"{e}\n\n{traceback.format_exc()[-1500:]}")


def cancel(build_id: int) -> bool:
    with _job_lock:
        if _job["id"] != build_id or not (_job["thread"] and _job["thread"].is_alive()):
            return False
        _job["cancel"].set()
        cur = _job["cursor"]
    if cur is not None:
        try:
            cur.cancel()                  # interrupts the statement that is running now
        except Exception:
            pass
    return True


# ═══════════════════════════════════════════════════════════════════════════
#  reading builds
# ═══════════════════════════════════════════════════════════════════════════
def _row(r) -> Dict[str, Any]:
    d = dict(r._mapping)
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        elif isinstance(v, Decimal):
            d[k] = float(v)
    for k, name in (("SETTINGS_JSON", "settings"), ("SUMMARY_JSON", "summary"),
                    ("CHECKS_JSON", "checks"), ("STEPS_JSON", "steps")):
        if k in d:
            try:
                d[name] = json.loads(d[k]) if d[k] else None
            except ValueError:
                d[name] = None
            d.pop(k, None)
    return d


def get(build_id: int) -> Optional[Dict[str, Any]]:
    S.ensure_tables()
    with data_engine.connect() as conn:
        r = conn.execute(text(f"SELECT * FROM dbo.{S.MBQ_BUILD} WHERE BUILD_ID = :id"),
                         {"id": build_id}).fetchone()
    if r is None:
        return None
    d = _row(r)
    if d["STATUS"] == "RUNNING" and running_build() != build_id:
        _update(build_id, STATUS="FAILED", PROGRESS="Interrupted",
                ERROR="The server stopped while this build was running. It was never committed, "
                      "so the previous demand is unchanged.")
        return get(build_id)
    return d


def recent(limit: int = 10) -> List[Dict[str, Any]]:
    S.ensure_tables()
    with data_engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT TOP (:n) BUILD_ID, STATUS, UPLOAD_ID, ROWS_BUILT, ROWS_SHORT, SHORTFALL_TOTAL,
                   EXCESS_TOTAL, CHECKS_PASSED, PROGRESS, CREATED_BY, STARTED_AT, FINISHED_AT,
                   DURATION_SEC, ERROR
              FROM dbo.{S.MBQ_BUILD} ORDER BY BUILD_ID DESC"""), {"n": limit}).fetchall()
    return [_row(r) for r in rows]


def latest_done() -> Optional[Dict[str, Any]]:
    with data_engine.connect() as conn:
        r = conn.execute(text(f"""
            SELECT TOP 1 * FROM dbo.{S.MBQ_BUILD} WHERE STATUS = 'DONE' ORDER BY BUILD_ID DESC""")).fetchone()
    return _row(r) if r else None


def freshness(build: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Is the demand table still what the current data and settings would give?"""
    from app.services import b2b_upload_service as U
    if not build:
        return {"state": "none", "reasons": ["The demand table has not been built yet."]}
    reasons, notes = [], []
    last = U.last_loaded()
    if last and last["UPLOAD_ID"] != build.get("UPLOAD_ID"):
        reasons.append(f"Upload {last['UPLOAD_ID']} was loaded after this build (it read upload "
                       f"{build.get('UPLOAD_ID')}).")
    now = b2b_settings.effective()
    used = build.get("settings") or {}
    changed = [k for k in MBQ_KEYS if str(used.get(k)) != str(now.get(k))]
    if changed:
        reasons.append("Demand settings changed since: " + ", ".join(
            f"{k} {used.get(k)} → {now.get(k)}" for k in changed))
    with data_engine.connect() as conn:
        grid_at, kind = _last_change(conn, S.SRC_GRID, with_kind=True)
    built_grid = build.get("GRID_UPDATED_AT")            # stored to the second
    if grid_at and built_grid and grid_at.replace(microsecond=0).isoformat() > built_grid:
        notes.append(f"Store stock in {S.SRC_GRID} changed at {grid_at:%d %b %H:%M}, after this "
                     f"build read it. Rebuild to use today's stock." if kind == "data" else
                     f"{S.SRC_GRID} was altered at {grid_at:%d %b %H:%M}, after this build read it, "
                     f"so its stock may have changed. Rebuild to be sure.")
    return {"state": "stale" if reasons else ("aging" if notes else "fresh"),
            "reasons": reasons, "notes": notes, "changed_settings": changed,
            "grid_updated_at": grid_at.isoformat() if grid_at else None}


def status() -> Dict[str, Any]:
    """Everything the Build MBQ page shows."""
    S.ensure_tables()
    running = running_build()
    latest = latest_done()
    return {"latest": latest, "running": get(running) if running else None,
            "freshness": freshness(latest), "builds": recent(10),
            "settings": {k: v for k, v in b2b_settings.effective().items() if k in MBQ_KEYS}}


# ═══════════════════════════════════════════════════════════════════════════
#  browsing the demand table
# ═══════════════════════════════════════════════════════════════════════════
BROWSE_COLS = ["STORE_CODE", "ST_NM", "RDC", "ART", "MAJ_CAT", "BIN_SIZE", "SZ", "CONT_EFF",
               "CONT_RULE", "ACC_D", "NORM_DAYS", "SALE_COVER_DAYS", "MBQ", "MBQ_ROUNDED",
               "STK_TTL", "SHORTFALL", "EXCESS", "BIN_QTY"]
SORTS = {"shortfall": "SHORTFALL DESC", "excess": "EXCESS DESC", "mbq": "MBQ_ROUNDED DESC",
         "store": "STORE_CODE, ART", "article": "ART, STORE_CODE"}


def _not_writing():
    w = writing()
    if w:
        raise RuntimeError(f"Build {w} is rewriting the demand table. Try again when it finishes.")


@contextmanager
def _reader():
    """A connection that gives up after 5 s on a lock instead of hanging the
    page. The timeout is a session setting, so it is reset before the pooled
    connection goes back."""
    with data_engine.connect() as conn:
        conn.execute(text("SET LOCK_TIMEOUT 5000"))
        try:
            yield conn
        finally:
            conn.execute(text("SET LOCK_TIMEOUT -1"))


def browse(store: Optional[str] = None, art: Optional[str] = None, maj_cat: Optional[str] = None,
           only: Optional[str] = None, sort: str = "shortfall", page: int = 1, size: int = 50) -> Dict[str, Any]:
    _not_writing()
    where, params = [], {}
    if store:
        where.append("STORE_CODE = :store"); params["store"] = store.strip().upper()
    if art:
        where.append("ART = :art"); params["art"] = art.strip()
    if maj_cat:
        where.append("MAJ_CAT = :cat"); params["cat"] = maj_cat.strip()
    if only == "short":
        where.append("SHORTFALL > 0")
    elif only == "excess":
        where.append("EXCESS > 0")
    elif only == "zero":
        where.append("ISNULL(MBQ_ROUNDED, 0) = 0")
    w = ("WHERE " + " AND ".join(where)) if where else ""
    size = min(max(int(size), 1), 500)
    page = max(int(page), 1)
    terms = SORTS.get(sort, SORTS["shortfall"]).split(", ")
    for tie in ("STORE_CODE", "ART"):                    # a column may appear once in ORDER BY
        if tie not in [t.split()[0] for t in terms]:
            terms.append(tie)
    order = ", ".join(terms)
    key_cols = sorted({"STORE_CODE", "ART"} | {t.split()[0] for t in terms})
    with _reader() as conn:
        total = conn.execute(text(f"SELECT COUNT_BIG(*) FROM dbo.{S.ART_MBQ} {w}"), params).scalar()
        # Pick the page on the key and sort columns only, then fetch whole
        # rows by primary key: sorting 12.7M narrow rows takes 0.2 s, sorting
        # them with every column 2.4 s (measured on the columnstore).
        rows = conn.execute(text(f"""
            WITH pg AS (
                SELECT {', '.join(key_cols)} FROM dbo.{S.ART_MBQ} {w}
                 ORDER BY {order}
                 OFFSET :off ROWS FETCH NEXT :n ROWS ONLY)
            SELECT {', '.join(f'm.{c}' for c in BROWSE_COLS)}
              FROM pg JOIN dbo.{S.ART_MBQ} m ON m.STORE_CODE = pg.STORE_CODE AND m.ART = pg.ART
             ORDER BY {', '.join('pg.' + t for t in terms)}
            OPTION (RECOMPILE)   -- bound OFFSET/FETCH hide N from the top-N sort: 3.6 s → 0.25 s"""),
            {**params, "off": (page - 1) * size, "n": size}).fetchall()
    out = []
    for r in rows:
        out.append({k: (float(v) if isinstance(v, Decimal) else v) for k, v in r._mapping.items()})
    return {"total": int(total or 0), "page": page, "size": size, "rows": out}


def explain(store: str, art: str) -> Dict[str, Any]:
    """Walk one store × article through the build, in plain words."""
    _not_writing()
    store, art = (store or "").strip().upper(), (art or "").strip()
    if not store or not art:
        raise ValueError("Give a store code and an article")
    build = latest_done()
    used = (build or {}).get("settings") or b2b_settings.effective()
    p = plan({**b2b_settings.effective(), **used})
    steps: List[Dict[str, Any]] = []

    def step(label, value, detail="", ok=True):
        steps.append({"label": label, "value": value, "detail": detail, "ok": ok})

    with _reader() as conn:
        sm = conn.execute(text(f"SELECT ST_NM, RDC FROM dbo.{S.STORE_MASTER} WHERE STORE_CODE = :s"),
                          {"s": store}).fetchone()
        a = conn.execute(text(f"""
            SELECT MAX(MAJ_CAT), MAX([SIZE]), SUM(CAST(QTY AS BIGINT)), COUNT(DISTINCT BIN), COUNT(*)
              FROM dbo.{S.BIN_MASTER} WHERE ART = :a"""), {"a": art}).fetchone()
        row = conn.execute(text(f"SELECT * FROM dbo.{S.ART_MBQ} WHERE STORE_CODE = :s AND ART = :a"),
                           {"s": store, "a": art}).fetchone()
        cat, size = (a[0], a[1]) if a and a[4] else (None, None)
        req = None
        if cat:
            req = conn.execute(text(f"""
                SELECT SUM(REQ), MAX(ACC_D), COUNT(*) FROM dbo.{S.REQ}
                 WHERE STORE_CODE = :s AND MAJ_CAT = :c AND ISNULL([SIZE], N'') = ISNULL(:z, N'')"""),
                {"s": store, "c": cat, "z": size}).fetchone()
        grid = conn.execute(text(f"""
            SELECT SUM(CONVERT(DECIMAL(18,4), STK_TTL)), MAX(SZ), COUNT(*) FROM dbo.{S.SRC_GRID}
             WHERE WERKS = :s AND ARTICLE_NUMBER = TRY_CONVERT(BIGINT, :a)"""), {"s": store, "a": art}).fetchone()

    if not sm:
        step("Store in Store Master", "No", f"{store} is not in Store Master, so the build never looks at it. "
             "Its REQ can never be sent until it is added.", ok=False)
        return {"store": store, "art": art, "verdict": f"No demand: {store} is not in Store Master.", "steps": steps}
    step("Store in Store Master", f"Yes · {sm[0] or ''} · {sm[1] or 'no RDC'}")
    if not (a and a[4]):
        step("Article in Bin Master", "No", f"There is no GRT stock of {art} in any bin.", ok=False)
        return {"store": store, "art": art, "verdict": f"No demand row: {art} is not in Bin Master.", "steps": steps}
    step("Article in Bin Master", f"{int(a[2] or 0):,} pcs in {int(a[3]):,} bin(s)",
         f"Category {cat}, size {size or '(blank)'}.")
    req_units = float(req[0]) if req and req[0] is not None else None
    step("Store's REQ for this category + size",
         "none" if req_units is None else f"{req_units:,.2f}",
         f"{store} / {cat} / {size or '(blank)'}. The build only makes rows a store can take: "
         f"REQ of at least {p['min_req']:g} (MBQ_MIN_REQ_UNITS)." if p["prune"] else "Pruning is off.",
         ok=not p["prune"] or (req_units or 0) >= p["min_req"])

    if row is None:
        if p["prune"] and (req_units is None or req_units < p["min_req"]):
            why = (f"{store} asks for {'nothing' if req_units is None else f'{req_units:g}'} of {cat} size "
                   f"{size or '(blank)'}, below MBQ_MIN_REQ_UNITS = {p['min_req']:g}. The REQ cap would "
                   f"reject any unit anyway.")
        elif build is None:
            why = "The demand table has not been built yet."
        else:
            why = (f"Not in build {build['BUILD_ID']}. The store or article may have been loaded after it — "
                   f"rebuild.")
        return {"store": store, "art": art, "verdict": f"No demand row. {why}", "steps": steps}

    d = {k: (float(v) if isinstance(v, Decimal) else v) for k, v in row._mapping.items()}
    rule_words = {"MASTER_NORM": "from Master_CONT_SZ, rescaled so the sizes add to 100%",
                  "REQ_FB_NORM": "from this store's REQ mix (the master had none), rescaled to 100%",
                  "NO_REQ_SZ": "0 — the store asks for none of this size",
                  "REQ_SHARE": "this size's share of the store's REQ for the category",
                  "FULL_SZ": "1 — an all-size article carries the whole category",
                  "FALLBACK": f"{p['fallback']:g} — no Master_CONT_SZ row (CONT_FALLBACK)",
                  "CONT": "straight from Master_CONT_SZ", "OFF": "1 — CONT_APPLY is off"}
    step("ACC_D (category figure)", "blank" if d["ACC_D"] is None else f"{d['ACC_D']:,}",
         "From REQ, the same on every size of the category.", ok=d["ACC_D"] is not None)
    step("Size share (CONT_EFF)", f"{(d['CONT_EFF'] or 0) * 100:.2f}%",
         f"{d['CONT_RULE']}: {rule_words.get(d['CONT_RULE'], '')}. Source {d['CONT_SOURCE'] or '—'}.",
         ok=(d["CONT_EFF"] or 0) > 0)
    fast = d["NORM_DAYS"] == p["short_days"]
    step("Norm days", f"{d['NORM_DAYS']}",
         (f"Size {d['SZ']} is a fast size" if (d["SZ"] or "").upper() in p["short_sz"] else
          f"{d['MAJ_CAT']} is a MIX category") + " → SHORT_DAYS" if fast else
         f"Size {d['SZ']} is not in {', '.join(p['short_sz'])} → LONG_DAYS")
    step("Sale cover days", f"{d['SALE_COVER_DAYS']}", "From REQ where set, otherwise DEFAULT_SALE_COVER_DAYS.")
    if d["ACC_D"] is not None:
        eff = d["ACC_D_EFF"] or 0
        step("Target (MBQ)", f"{d['MBQ'] or 0:,.2f} → {d['MBQ_ROUNDED']}",
             f"{d['ACC_D']} × {d['CONT_EFF'] or 0:.4f} = {eff:.4f}; "
             f"{eff:.4f} + {eff:.4f} ÷ {d['NORM_DAYS']} × {d['SALE_COVER_DAYS']} = {d['MBQ_RAW'] or 0:.4f}"
             + (f"; lifted to {p['mbq_min']:g} (MBQ_MIN_WHEN_CONT)" if (d["MBQ"] or 0) > (d["MBQ_RAW"] or 0) else "")
             + "; rounded.")
    gnow = float(grid[0]) if grid and grid[2] else None
    stk_detail = ("No grid row: counted as 0 (TREAT_MISSING_STK_AS_ZERO = On)." if gnow is None and p["stk_zero"]
                  else "No grid row: stock unknown, so no shortfall (TREAT_MISSING_STK_AS_ZERO = Off)."
                  if gnow is None else f"From {S.SRC_GRID}.")
    if gnow is not None and d["STK_TTL"] is not None and abs(gnow - d["STK_TTL"]) > 1e-6:
        stk_detail += f" The grid now says {gnow:g} — it changed after the build."
    step("Store stock", "blank" if d["STK_TTL"] is None else f"{d['STK_TTL']:g}", stk_detail)
    sf = d["SHORTFALL"]
    step("Shortfall", "blank" if sf is None else f"{sf:g}",
         "MAX(target − stock, 0). What allocation may send, still capped by the store's REQ for the "
         "category + size and by what the bins hold.", ok=bool(sf and sf > 0))

    if d["ACC_D"] is None:
        verdict = "Target is blank: REQ gives no ACC_D for this store and category."
    elif (d["CONT_EFF"] or 0) == 0:
        verdict = f"Target is zero: the size share is 0 ({d['CONT_RULE']})."
    elif sf is None:
        verdict = "No shortfall: the store's stock is unknown (no grid row)."
    elif sf > 0:
        verdict = f"Short by {sf:g}: target {d['MBQ_ROUNDED']}, stock {d['STK_TTL']:g}."
    else:
        verdict = f"Not short: stock {d['STK_TTL']:g} already covers the target of {d['MBQ_ROUNDED']}."
    return {"store": store, "art": art, "verdict": verdict, "steps": steps, "row": d,
            "build_id": d.get("BUILD_ID")}
