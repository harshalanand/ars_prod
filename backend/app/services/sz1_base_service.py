"""
1SZ ALC — Step 1 (Generate SZ Base) and Step 2 (Generate ST/MJ/SZ req).

Step 1 → ARS_SZ1_MSA_SZ_BASE: the broken-size candidates in each warehouse,
         one row per RDC + article, chosen from ARS_MSA_TOTAL by rules R1–R4.
Step 2 → ARS_SZ1_ST_MJ_SZ_REQ_BASE: every listed store's need per MAJ_CAT and
         size; MJ_SZ_SRT is the size budget Step 3 may never exceed.

The selection SQL is the Streamlit tool's (msa_base.py / req_base.py,
2026-09-24), with only the table names changed. Spec:
frontend/public/docs/manual/sz1_alloc.md §8.1–8.2.

A build runs as a background job and replaces the table's rows in ONE
transaction (TRUNCATE, INSERT, then re-apply the latest session's balances),
so a failed build leaves the previous one in place. Only one 1SZ ALC job —
a build or a Step 3 run — runs at a time; see `job_slot()`.

This module also owns the balance columns (BAL_FNL_Q, REM_MJ_SZ_SRT): they
live on the two base tables, and both a build and a run end by re-applying them.
Since phase 3 (2026-10-12) they are LIVE: free stock from ARS_MSA_TOTAL now and
room net of open ARS_PEND_ALC now, less every PARKED session — see
live_free_sql / live_pend_sql / parked_sessions, shared with the approval.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import text

from app.database.session import data_engine
from app.services import sz1_schema as S

FULL_OPT_SQL = "'NA', 'A'"         # sizes that mark a complete option, not a size
CONT_DEFAULT = "9.99"              # CONT_PCT_SZ_REV where the share is 0 or unknown


def q(name: str) -> str:
    return f"[dbo].[{name}]"


# ═══════════════════════════════════════════════════════════════════════════
#  Step 1 — SZ Base
# ═══════════════════════════════════════════════════════════════════════════
BASE_CTE = """
WITH stock AS (                  -- one row per RDC + variant article, free stock only
    SELECT  m.RDC, m.MAJ_CAT, m.GEN_ART_NUMBER, m.CLR, m.ARTICLE_NUMBER, m.SZ,
            CASE WHEN MIN(m.ALLOC_TYPE) = MAX(m.ALLOC_TYPE) THEN MIN(m.ALLOC_TYPE)
                 ELSE MIN(m.ALLOC_TYPE) + '+' + MAX(m.ALLOC_TYPE) END AS ALLOC_TYPE,
            SUM(m.STK_QTY)     AS STK_QTY,
            SUM(m.PEND_QTY)    AS PEND_QTY,
            SUM(m.HOLD_QTY)    AS HOLD_QTY,
            SUM(m.FNL_Q)       AS FNL_Q,
            COALESCE(MAX(NULLIF(m.PAK_SZ, 0)), 1) AS PAK_SZ   -- largest real pack, else 1
    FROM    {src} m
    WHERE   m.FNL_Q > 0
    GROUP BY m.RDC, m.MAJ_CAT, m.GEN_ART_NUMBER, m.CLR, m.ARTICLE_NUMBER, m.SZ
),
co AS (                          -- company size curve: Master_CONT_SZ has stores + 'CO', no RDC
    SELECT MAJ_CAT, SZ, MAX(TRY_CAST(CONT AS FLOAT)) AS CONT
    FROM   {cont} WHERE ST_CD = 'CO' GROUP BY MAJ_CAT, SZ
),
sto AS (                         -- the stores' rows for the MAJ_CAT + size: present? averaged
    SELECT MAJ_CAT, SZ, AVG(ISNULL(TRY_CAST(CONT AS FLOAT), 0)) AS CONT
    FROM   {cont} WHERE ST_CD <> 'CO' GROUP BY MAJ_CAT, SZ
),
live AS (                        -- + the article's size share at company level (2026-10-09):
    SELECT  s.*,                 --   CO value · else the stores' average · else 0 = no CONT in the master
            CAST(CASE WHEN c.CONT > 0  THEN c.CONT * 100
                      WHEN st.CONT > 0 THEN st.CONT * 100
                      ELSE 0 END AS DECIMAL(9,4)) AS ART_CONT_PCT,
            CASE WHEN c.CONT > 0  THEN 'CO'
                 WHEN st.CONT > 0 THEN 'STORE_AVG'                     -- CO lacks the size, stores have it
                 WHEN c.SZ IS NULL AND st.SZ IS NULL THEN 'NOT_IN_MASTER'  -- no row at all
                 ELSE 'ZERO_IN_MASTER' END AS CONT_SRC                 -- rows exist, every one 0
    FROM    stock s
    LEFT JOIN co  c  ON c.MAJ_CAT = s.MAJ_CAT AND c.SZ = s.SZ
    LEFT JOIN sto st ON st.MAJ_CAT = s.MAJ_CAT AND st.SZ = s.SZ
),
opt AS (                         -- OPT = MAJ_CAT + GEN_ART_NUMBER + CLR, per RDC
    SELECT  RDC, MAJ_CAT, GEN_ART_NUMBER, CLR,
            COUNT(DISTINCT ARTICLE_NUMBER) AS VAR_COUNT,
            COUNT(DISTINCT SZ)             AS SZ_COUNT,
            SUM(FNL_Q)                     AS OPT_FNL,
            CAST(SUM(ART_CONT_PCT) AS DECIMAL(9,4)) AS OPT_CONT_PCT   -- curve share the live sizes still cover
    FROM    live
    GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR
),
cat AS (                         -- category total, per RDC
    SELECT RDC, MAJ_CAT, SUM(FNL_Q) AS CAT_FNL
    FROM   live
    GROUP BY RDC, MAJ_CAT
),
scored AS (                      -- the rules, with the saved values (sz1_settings)
    SELECT  l.RDC, l.MAJ_CAT, h.SZ_APPLICABLE, l.GEN_ART_NUMBER, l.CLR,
            l.MAJ_CAT + '|' + l.GEN_ART_NUMBER + '|' + l.CLR AS OPT_KEY,
            l.ARTICLE_NUMBER, l.SZ, l.ALLOC_TYPE,
            l.STK_QTY, l.PEND_QTY, l.HOLD_QTY, l.FNL_Q, l.PAK_SZ,
            o.VAR_COUNT, o.SZ_COUNT, o.OPT_FNL, c.CAT_FNL,
            l.ART_CONT_PCT, o.OPT_CONT_PCT, l.CONT_SRC,
            CASE WHEN h.SZ_APPLICABLE = 'Y' AND l.SZ NOT IN ('A', 'NA') AND {r1}
                 THEN 1 ELSE 0 END AS R1_LOW_VAR,
            CASE WHEN h.SZ_APPLICABLE = 'Y' AND o.SZ_COUNT = 1 AND l.SZ IN ('A', 'NA') AND {r2}
                 THEN 1 ELSE 0 END AS R2_SINGLE_A,
            CASE WHEN {r3} THEN 1 ELSE 0 END AS R3_CAT_LE200,
            CASE WHEN {r4} THEN 1 ELSE 0 END AS R4_OPT_LE20,
            -- zero company CONT: the whole option, else this size (A / NA exempt unless set)
            CASE WHEN h.SZ_APPLICABLE = 'Y' AND ({gap_full_opt} OR l.SZ NOT IN ('A', 'NA')) AND o.OPT_CONT_PCT = 0
                      THEN 'OPT_NO_CONT'
                 WHEN h.SZ_APPLICABLE = 'Y' AND ({gap_full_opt} OR l.SZ NOT IN ('A', 'NA')) AND l.ART_CONT_PCT = 0
                      THEN 'SZ_NO_CONT' END AS GAP_TYPE
    FROM    live l
    JOIN    opt o ON o.RDC = l.RDC AND o.MAJ_CAT = l.MAJ_CAT
                 AND o.GEN_ART_NUMBER = l.GEN_ART_NUMBER AND o.CLR = l.CLR
    JOIN    cat c ON c.RDC = l.RDC AND c.MAJ_CAT = l.MAJ_CAT
    LEFT JOIN {hier} h ON h.MAJ_CAT = l.MAJ_CAT
),
selected AS (                    -- size-applicable, and at least one rule fired
    SELECT  RDC, MAJ_CAT, SZ_APPLICABLE, GEN_ART_NUMBER, CLR, OPT_KEY,
            ARTICLE_NUMBER, SZ, ALLOC_TYPE,
            STK_QTY, PEND_QTY, HOLD_QTY, FNL_Q, PAK_SZ,
            FNL_Q AS BAL_FNL_Q,
            VAR_COUNT, SZ_COUNT, OPT_FNL, CAT_FNL,
            ART_CONT_PCT, OPT_CONT_PCT, CONT_SRC,
            R1_LOW_VAR, R2_SINGLE_A, R3_CAT_LE200, R4_OPT_LE20,
            STUFF(
                CASE WHEN R1_LOW_VAR   = 1 THEN ',R1' ELSE '' END +
                CASE WHEN R2_SINGLE_A  = 1 THEN ',R2' ELSE '' END +
                CASE WHEN R3_CAT_LE200 = 1 THEN ',R3' ELSE '' END +
                CASE WHEN R4_OPT_LE20  = 1 THEN ',R4' ELSE '' END, 1, 1, '') AS RULES
    FROM    scored
    WHERE   SZ_APPLICABLE = 'Y'
      AND   R1_LOW_VAR + R2_SINGLE_A + R3_CAT_LE200 + R4_OPT_LE20 > 0
      AND   {gap_keep}               -- zero-CONT rows are a gap, not a candidate
),
gaps AS (                        -- every zero-CONT size-applicable article, for the gap report
    SELECT  RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, OPT_KEY, ARTICLE_NUMBER, SZ, FNL_Q, PAK_SZ,
            VAR_COUNT, OPT_FNL, CAT_FNL, ART_CONT_PCT, OPT_CONT_PCT, CONT_SRC, GAP_TYPE,
            STUFF(
                CASE WHEN R1_LOW_VAR   = 1 THEN ',R1' ELSE '' END +
                CASE WHEN R2_SINGLE_A  = 1 THEN ',R2' ELSE '' END +
                CASE WHEN R3_CAT_LE200 = 1 THEN ',R3' ELSE '' END +
                CASE WHEN R4_OPT_LE20  = 1 THEN ',R4' ELSE '' END, 1, 1, '') AS RULES_IF_KEPT,
            CAST(CASE WHEN {gap_excluding} AND R1_LOW_VAR + R2_SINGLE_A + R3_CAT_LE200 + R4_OPT_LE20 > 0
                      THEN 1 ELSE 0 END AS BIT) AS EXCLUDED
    FROM    scored
    WHERE   SZ_APPLICABLE = 'Y' AND GAP_TYPE IS NOT NULL
)
"""
GAP_COLUMNS = ("RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, OPT_KEY, ARTICLE_NUMBER, SZ, FNL_Q, PAK_SZ, "
               "VAR_COUNT, OPT_FNL, CAT_FNL, ART_CONT_PCT, OPT_CONT_PCT, CONT_SRC, GAP_TYPE, RULES_IF_KEPT, EXCLUDED")
BASE_COLUMNS = ("RDC, MAJ_CAT, SZ_APPLICABLE, GEN_ART_NUMBER, CLR, OPT_KEY, "
                "ARTICLE_NUMBER, SZ, ALLOC_TYPE, STK_QTY, PEND_QTY, HOLD_QTY, FNL_Q, PAK_SZ, "
                "BAL_FNL_Q, VAR_COUNT, SZ_COUNT, OPT_FNL, CAT_FNL, "
                "ART_CONT_PCT, OPT_CONT_PCT, CONT_SRC, "
                "R1_LOW_VAR, R2_SINGLE_A, R3_CAT_LE200, R4_OPT_LE20, RULES")


def rule_sql(settings: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """The four rule conditions as SQL, from validated settings (numbers only
    reach the SQL after sz1_settings.clean). An off rule is `1 = 0`."""
    from app.services import sz1_settings
    s = sz1_settings.clean(settings or {}, sz1_settings.get_all())    # overrides on the saved values
    off = "1 = 0"
    r1 = (f"o.OPT_CONT_PCT <= {float(s['R1_OPT_CONT_MAX'])}" if s["R1_BASIS"] == "cont"
          else f"o.VAR_COUNT <= {int(s['R1_VAR_MAX'])}")
    cap = f"o.OPT_FNL <= {float(s['OPT_FNL_MAX'])}"
    return {"r1": r1 if s["R1_ON"] else off,
            "r2": (cap if s["R2_CAP_ON"] else "1 = 1") if s["R2_ON"] else off,
            "r3": f"c.CAT_FNL <= {float(s['R3_CAT_FNL_MAX'])}" if s["R3_ON"] else off,
            "r4": cap if s["R4_ON"] else off,
            "gap_full_opt": "1 = 1" if s["GAP_INCL_FULL_OPT"] else "1 = 0",
            "gap_keep": "GAP_TYPE IS NULL" if s["GAP_EXCLUDE"] else "1 = 1",
            "gap_excluding": "1 = 1" if s["GAP_EXCLUDE"] else "1 = 0"}


def base_sql(final: str, settings: Optional[Dict[str, Any]] = None) -> str:
    """The Step 1 CTE chain followed by `final`, which reads `selected`.
    `settings` = rule values to use (default: the saved ones)."""
    return BASE_CTE.format(src=q(S.MSA_TOTAL), hier=q(S.HIERARCHY), cont=q(S.CONT_SZ),
                           **rule_sql(settings)) + "\n" + final


# ═══════════════════════════════════════════════════════════════════════════
#  Step 2 — ST/MJ/SZ req
# ═══════════════════════════════════════════════════════════════════════════
REQ_CTE = """
WITH stores AS (
    SELECT ST_CD, ST_NM, RDC
    FROM   {store}
    WHERE  LISTING = 1
),
cat AS (                      -- MAJ_CAT attributes; one value each in ARS_MSA_TOTAL
    SELECT MAJ_CAT, MIN(SEG) AS SEG, MIN(DIV) AS DIV, MIN(SUB_DIV) AS SUB_DIV, MIN(SSN) AS SSN
    FROM   {msa}
    GROUP BY MAJ_CAT
),
combo AS (                    -- the MAJ_CAT + SZ pairs Step 1 kept in ANY RDC: under
    SELECT DISTINCT MAJ_CAT, SZ   -- All RDC a store may be served a size only the other
    FROM {base}                   -- warehouse holds (the tool used the store's own RDC only)
),
szc AS (                      -- real sizes per RDC + MAJ_CAT, 'NA' / 'A' excluded
    SELECT RDC, MAJ_CAT,
           COUNT(DISTINCT CASE WHEN SZ NOT IN ({full_opt}) THEN SZ END) AS SZ_COUNT
    FROM {msa} GROUP BY RDC, MAJ_CAT
),
var_art AS (                  -- size-level store stock
    SELECT WERKS, MAJ_CAT, SZ, SUM(STK_TTL) AS SZ_STK
    FROM {varart} GROUP BY WERKS, MAJ_CAT, SZ
),{pend_ctes}
cont_st AS (                  -- store size curve (grouped: a duplicate row cannot multiply the grid)
    SELECT ST_CD, MAJ_CAT, SZ, MAX(CONT) AS CONT
    FROM {cont} WHERE ST_CD <> 'CO' GROUP BY ST_CD, MAJ_CAT, SZ
),
cont_tot AS (                 -- store curve total: spots a missing curve, and is the divisor
    SELECT ST_CD, MAJ_CAT, SUM(CONT) AS TOT
    FROM cont_st {tot_where} GROUP BY ST_CD, MAJ_CAT
),
cont_co AS (                  -- company fallback curve
    SELECT MAJ_CAT, SZ, MAX(CONT) AS CONT
    FROM {cont} WHERE ST_CD = 'CO' GROUP BY MAJ_CAT, SZ
),
cont_co_tot AS (
    SELECT MAJ_CAT, SUM(CONT) AS TOT FROM cont_co {tot_where} GROUP BY MAJ_CAT
),
calc AS (
    SELECT  s.RDC, s.ST_CD, s.ST_NM,
            c.SEG, c.DIV, c.SUB_DIV,
            k.MAJ_CAT, c.SSN, k.SZ,
            h.SZ_APPLICABLE,
            CAST({share_case} AS DECIMAL(9,4)) AS CONT_PCT_SZ,
            {src_case} AS CONT_SRC,
            z.SZ_COUNT,
            CAST(CASE WHEN k.SZ IN ({full_opt}) THEN ISNULL(m.STK_TTL, 0)
                      ELSE ISNULL(v.SZ_STK, 0) END AS DECIMAL(18,2)) AS MJ_SZ_STK,
            CAST(m.STK_TTL AS DECIMAL(18,2)) AS MJ_STK,
            CAST({mj_pend} AS DECIMAL(18,2)) AS MJ_PEND,          -- open pending to the store (phase 3)
            CAST({mj_sz_pend} AS DECIMAL(18,2)) AS MJ_SZ_PEND,
            CAST(m.DISP_Q  AS DECIMAL(18,2)) AS MJ_DISP,
            CAST(m.SAL_PD  AS DECIMAL(18,4)) AS MJ_SAL_PD,
            CAST(m.ALC_D   AS DECIMAL(18,2)) AS ALC_D,
            CAST(m.ACS_D   AS DECIMAL(18,2)) AS ACS_D,
            -- MBQ = DISP + SAL_PD × ALC_D, rounded; 0 when there is no display space
            CAST(CASE WHEN ISNULL(m.DISP_Q, 0) = 0 THEN 0
                      ELSE ROUND(m.DISP_Q
                                 + (ISNULL(m.SAL_PD, 0) * ISNULL(m.ALC_D, 0)), 0)
                 END AS DECIMAL(18,2)) AS MJ_MBQ
    FROM    stores s
    CROSS JOIN combo k
    LEFT JOIN cat  c ON c.MAJ_CAT = k.MAJ_CAT
    LEFT JOIN {hier} h ON h.MAJ_CAT = k.MAJ_CAT
    LEFT JOIN {gridmj} m ON m.WERKS = s.ST_CD AND m.MAJ_CAT = k.MAJ_CAT
    LEFT JOIN szc      z ON z.RDC = s.RDC AND z.MAJ_CAT = k.MAJ_CAT
    LEFT JOIN cont_st  p ON p.ST_CD = s.ST_CD AND p.MAJ_CAT = k.MAJ_CAT AND p.SZ = k.SZ
    LEFT JOIN cont_tot t ON t.ST_CD = s.ST_CD AND t.MAJ_CAT = k.MAJ_CAT
    LEFT JOIN cont_co  o ON o.MAJ_CAT = k.MAJ_CAT AND o.SZ = k.SZ
    LEFT JOIN cont_co_tot ot ON ot.MAJ_CAT = k.MAJ_CAT
    LEFT JOIN var_art  v ON v.WERKS = s.ST_CD AND v.MAJ_CAT = k.MAJ_CAT AND v.SZ = k.SZ{pend_join}
),
lvl2 AS (
    SELECT  RDC, ST_CD, ST_NM, SEG, DIV, SUB_DIV, MAJ_CAT, SSN, SZ, SZ_APPLICABLE,
            CONT_PCT_SZ, CONT_SRC, SZ_COUNT,
            CAST(CASE WHEN CONT_PCT_SZ IS NULL OR CONT_PCT_SZ = 0
                      THEN {cont_default} ELSE CONT_PCT_SZ END
                 AS DECIMAL(9,4)) AS CONT_PCT_SZ_REV,
            MJ_STK, MJ_PEND, MJ_SZ_PEND, MJ_DISP, MJ_SAL_PD,
            CAST(MJ_DISP / NULLIF(MJ_SAL_PD, 0) AS DECIMAL(18,2)) AS MJ_STR,
            ALC_D, ACS_D, MJ_MBQ,
            CAST(CASE WHEN MJ_MBQ - MJ_STK > 0 THEN MJ_MBQ - MJ_STK ELSE 0 END
                 AS DECIMAL(18,2)) AS MJ_SRT,
            CAST(CASE WHEN MJ_STK - MJ_MBQ > 0 THEN MJ_STK - MJ_MBQ ELSE 0 END
                 AS DECIMAL(18,2)) AS MJ_EXC,
            MJ_SZ_STK
    FROM    calc
),
lvl3 AS (
    SELECT  *,
            CAST(MJ_DISP * CONT_PCT_SZ_REV / 100.0 AS DECIMAL(18,2)) AS MJ_SZ_DISP,
            CAST(MJ_SAL_PD * CONT_PCT_SZ_REV / 100.0 AS DECIMAL(18,4)) AS MJ_SZ_SAL_PD
    FROM    lvl2
),
lvl4 AS (
    SELECT  *,
            CAST({sz_mbq} AS DECIMAL(18,2)) AS MJ_SZ_MBQ
    FROM    lvl3
),
grid AS (
    SELECT  RDC, ST_CD, ST_NM, SEG, DIV, SUB_DIV, MAJ_CAT, SSN, SZ, SZ_APPLICABLE,
            CONT_PCT_SZ, CONT_PCT_SZ_REV, CONT_SRC, SZ_COUNT,
            MJ_STK, MJ_DISP, MJ_SAL_PD, MJ_STR, ALC_D, ACS_D,
            MJ_MBQ, MJ_SRT, MJ_EXC,
            MJ_SZ_STK, MJ_SZ_DISP, MJ_SZ_SAL_PD, MJ_SZ_MBQ,
            CAST(CASE WHEN MJ_SZ_MBQ - MJ_SZ_STK > 0 THEN MJ_SZ_MBQ - MJ_SZ_STK
                      ELSE 0 END AS DECIMAL(18,2)) AS MJ_SZ_SRT,
            CAST(CASE WHEN MJ_SZ_MBQ - MJ_SZ_STK > 0 THEN MJ_SZ_MBQ - MJ_SZ_STK
                      ELSE 0 END AS DECIMAL(18,2)) AS REM_MJ_SZ_SRT,
            MJ_PEND, MJ_SZ_PEND,
            CAST(CASE WHEN MJ_SZ_STK - MJ_SZ_MBQ > 0 THEN MJ_SZ_STK - MJ_SZ_MBQ
                      ELSE 0 END AS DECIMAL(18,2)) AS MJ_SZ_EXC
    FROM    lvl4
)
"""
REQ_COLUMNS = ("RDC, ST_CD, ST_NM, SEG, DIV, SUB_DIV, MAJ_CAT, SSN, SZ, SZ_APPLICABLE, "
               "CONT_PCT_SZ, CONT_PCT_SZ_REV, CONT_SRC, SZ_COUNT, "
               "MJ_STK, MJ_DISP, MJ_SAL_PD, MJ_STR, ALC_D, ACS_D, MJ_MBQ, MJ_SRT, MJ_EXC, "
               "MJ_SZ_STK, MJ_SZ_DISP, MJ_SZ_SAL_PD, MJ_SZ_MBQ, MJ_SZ_SRT, REM_MJ_SZ_SRT, MJ_SZ_EXC, "
               "MJ_PEND, MJ_SZ_PEND")


# Step 2 size share (manual §C). "level2" (2026-10-12, the business rule) is the
# ARS listing Level 2 ladder scaled to 100 %: the store's own curve, the company
# (CO) curve only when the store has no curve for the MAJ_CAT, a size missing
# from the curve in use gets 0, and A / NA never count in a divisor (a store
# curve with A = 1.0 used to halve every real size). No 9.99 default, and the
# size MBQ is worked out from unrounded parts. "tool" is the Streamlit tool's
# rule, kept only so scripts/sz1_parity.py can still prove the port.
SHARE_RULES = {
    "level2": {
        "tot_where": "WHERE SZ NOT IN ({full_opt}) AND CONT > 0",
        "share_case": """CASE
                WHEN k.SZ IN ({full_opt})      THEN 100.0
                WHEN h.SZ_APPLICABLE = 'N'     THEN 100.0 / NULLIF(z.SZ_COUNT, 0)
                WHEN ISNULL(t.TOT, 0) > 0      THEN CASE WHEN p.CONT > 0 THEN p.CONT * 100.0 / t.TOT ELSE 0 END
                WHEN ISNULL(ot.TOT, 0) > 0     THEN CASE WHEN o.CONT > 0 THEN o.CONT * 100.0 / ot.TOT ELSE 0 END
                ELSE 0
            END""",
        "src_case": """CASE
                WHEN k.SZ IN ({full_opt})      THEN 'FULL_OPT'
                WHEN h.SZ_APPLICABLE = 'N'     THEN 'EVEN_N'
                WHEN ISNULL(t.TOT, 0) > 0      THEN CASE WHEN p.CONT > 0 THEN 'STORE' ELSE 'STORE_SIZE_MISSING' END
                WHEN ISNULL(ot.TOT, 0) > 0     THEN CASE WHEN o.CONT > 0 THEN 'CO' ELSE 'CO_SIZE_MISSING' END
                ELSE 'NO_CURVE'
            END""",
        "cont_default": "0",
        "sz_mbq": """CASE WHEN ISNULL(MJ_DISP, 0) * ISNULL(CONT_PCT_SZ_REV, 0) = 0 THEN 0
                      ELSE ROUND(MJ_DISP * CONT_PCT_SZ_REV / 100.0
                                 + ISNULL(MJ_SAL_PD, 0) * CONT_PCT_SZ_REV / 100.0 * ISNULL(ALC_D, 0), 0)
                 END""",
    },
    "tool": {
        "tot_where": "",
        "share_case": """CASE
                WHEN k.SZ IN ({full_opt})
                     THEN 100.0
                WHEN h.SZ_APPLICABLE = 'N'
                     THEN 100.0 / NULLIF(z.SZ_COUNT, 0)
                WHEN t.ST_CD IS NULL
                     THEN 100.0 / NULLIF(z.SZ_COUNT, 0)
                WHEN ISNULL(t.TOT, 0) = 0
                     THEN CASE WHEN o.CONT IS NULL
                               THEN 100.0 / NULLIF(z.SZ_COUNT, 0)
                               ELSE o.CONT * 100.0 / NULLIF(ot.TOT, 0) END
                WHEN p.CONT IS NOT NULL
                     THEN p.CONT * 100.0 / t.TOT
                WHEN o.CONT IS NOT NULL
                     THEN o.CONT * 100.0 / NULLIF(ot.TOT, 0)
                ELSE 0
            END""",
        "src_case": """CASE
                WHEN k.SZ IN ({full_opt})  THEN 'FULL_OPT'
                WHEN h.SZ_APPLICABLE = 'N' THEN 'EVEN_N'
                WHEN t.ST_CD IS NULL       THEN 'EVEN_NO_CURVE'
                WHEN ISNULL(t.TOT, 0) = 0  THEN CASE WHEN o.CONT IS NULL
                                                     THEN 'EVEN_CO_MISSING'
                                                     ELSE 'CO' END
                WHEN p.CONT IS NOT NULL    THEN 'STORE'
                WHEN o.CONT IS NOT NULL    THEN 'CO_SIZE'
                ELSE 'STORE_MISSING'
            END""",
        "cont_default": CONT_DEFAULT,
        "sz_mbq": """CASE WHEN ISNULL(MJ_SZ_DISP, 0) = 0 THEN 0
                      ELSE ROUND(MJ_SZ_DISP
                                 + (ISNULL(MJ_SZ_SAL_PD, 0) * ISNULL(ALC_D, 0)), 0)
                 END""",
    },
}
DEFAULT_SHARE_RULE = "level2"


def req_sql(final: str, share_rule: str = DEFAULT_SHARE_RULE) -> str:
    """The Step 2 CTE chain followed by `final`, which reads `grid`."""
    if share_rule not in SHARE_RULES:
        raise ValueError(f"share_rule must be one of {tuple(SHARE_RULES)}")
    parts = {k: v.replace("{full_opt}", FULL_OPT_SQL) for k, v in SHARE_RULES[share_rule].items()}
    parts.update(_pend_parts(share_rule == "level2"))
    return REQ_CTE.format(store=q(S.STORE_MASTER), msa=q(S.MSA_TOTAL), base=q(S.SZ_BASE),
                          hier=q(S.HIERARCHY), gridmj=q(S.GRID_MJ), cont=q(S.CONT_SZ),
                          varart=q(S.GRID_VAR_ART), full_opt=FULL_OPT_SQL, **parts) + "\n" + final


def _pend_parts(net: bool) -> Dict[str, str]:
    """MJ_PEND / MJ_SZ_PEND: the store's open pending (ARS_PEND_ALC) at build
    time, for INFORMATION only — the grid's STK_TTL already includes it
    (STK_TTL = store SLOCs + hub + PEND_ALC, and Pend ALC adds every approval
    to it live), so the shortage is MBQ − STK_TTL and pending is never
    subtracted a second time. The tool rule (sz1_parity.py) keeps them 0."""
    if not net:
        return {"pend_ctes": "", "pend_join": "", "mj_pend": "0", "mj_sz_pend": "0"}
    return {
        "pend_ctes": f"""
pend_sz AS (                  -- open pending to the store per MAJ_CAT + size (ARS_PEND_ALC, IS_CLOSED = 0)
    {live_pend_sql('size')}
),
pend_cat AS (                 -- the same per MAJ_CAT, all sizes
    {live_pend_sql('cat')}
),""",
        "pend_join": """
    LEFT JOIN pend_cat pc ON pc.ST_CD = s.ST_CD AND pc.MAJ_CAT = k.MAJ_CAT
    LEFT JOIN pend_sz  ps ON ps.ST_CD = s.ST_CD AND ps.MAJ_CAT = k.MAJ_CAT AND ps.SZ = k.SZ""",
        "mj_pend": "ISNULL(pc.PEND_Q, 0)",
        "mj_sz_pend": f"CASE WHEN k.SZ IN ({FULL_OPT_SQL}) THEN ISNULL(pc.PEND_Q, 0) ELSE ISNULL(ps.PEND_Q, 0) END",
    }


# ═══════════════════════════════════════════════════════════════════════════
#  helpers
# ═══════════════════════════════════════════════════════════════════════════
def jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


class _Now:
    """A column value meaning SYSDATETIME() — the DB server's clock, the same one
    the STARTED_AT / CREATED_AT defaults use (the app host's clock drifts)."""


NOW = _Now()


def set_clause(cols: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """`SET a = :a, b = SYSDATETIME()` and the params, for an UPDATE."""
    sets = ", ".join(f"{k} = SYSDATETIME()" if v is NOW else f"{k} = :{k}" for k, v in cols.items())
    return sets, {k: v for k, v in cols.items() if v is not NOW}


def rows(conn, sql: str, params: Optional[dict] = None) -> List[Dict[str, Any]]:
    res = conn.execute(text(sql), params or {})
    return [{k: jsonable(v) for k, v in r._mapping.items()} for r in res]


# ═══════════════════════════════════════════════════════════════════════════
#  the one-job-at-a-time slot shared by builds and Step 3 runs
# ═══════════════════════════════════════════════════════════════════════════
_job: Dict[str, Any] = {"kind": None, "id": None, "thread": None, "cancel": None}
_job_lock = threading.Lock()        # guards _job
_admission = threading.Lock()       # held from "is the slot free?" until the thread is registered

JOB_WORDS = {"build1": "Step 1 build", "build2": "Step 2 build", "run": "Session"}


# Several backend processes can share the database (the main API, a dev
# server, a test API), and each only sees its own threads. So a job is
# "running" when ANY process's row says RUNNING; a RUNNING row is taken as
# orphaned (its process died) only once it is older than any real job takes.
BUILD_STALE_MIN = 30        # a build takes seconds to a few minutes
RUN_STALE_MIN = 120         # a 2,000-store run takes minutes


def _local_running() -> Optional[Dict[str, Any]]:
    with _job_lock:
        t = _job["thread"]
        if t is not None and t.is_alive():
            return {"kind": _job["kind"], "id": _job["id"]}
    return None


def running() -> Optional[Dict[str, Any]]:
    """{kind: 'build1' | 'build2' | 'run', id} of the live job — in this
    process or, from its RUNNING row, in another one — or None."""
    live = _local_running()
    if live:
        return live
    try:
        with data_engine.connect() as conn:
            r = conn.execute(text(f"""
                SELECT TOP 1 STEP, BUILD_ID FROM {q(S.BUILD)}
                 WHERE STATUS = 'RUNNING' AND DATEDIFF(MINUTE, STARTED_AT, SYSDATETIME()) < {BUILD_STALE_MIN}
                 ORDER BY BUILD_ID DESC""")).fetchone()
            if r:
                return {"kind": f"build{r[0]}", "id": r[1]}
            r = conn.execute(text(f"""
                SELECT TOP 1 SESSION_ID FROM {q(S.SESSION)}
                 WHERE STATUS = 'RUNNING' AND DATEDIFF(MINUTE, CREATED_AT, SYSDATETIME()) < {RUN_STALE_MIN}
                 ORDER BY CREATED_AT DESC""")).fetchone()
            if r:
                return {"kind": "run", "id": r[0]}
    except Exception:  # noqa: BLE001 — tables not created yet
        pass
    return None


@contextmanager
def job_slot(kind: str):
    """Claim the module's job slot or fail with the job that holds it — in
    any process. The caller registers its thread with `start_job` (and writes
    its RUNNING row) before leaving the block."""
    with _admission:
        live = running()
        if live:
            raise RuntimeError(f"{JOB_WORDS.get(live['kind'], 'A job')} {live['id']} is still running. "
                               "Wait for it to finish.")
        yield


def start_job(kind: str, job_id: Any, target, args: tuple) -> threading.Event:
    ev = threading.Event()
    t = threading.Thread(target=target, args=(*args, ev), name=f"sz1-{kind}-{job_id}", daemon=True)
    with _job_lock:
        _job.update(kind=kind, id=job_id, thread=t, cancel=ev)
    t.start()
    return ev


def cancel_job(kind: str, job_id: Any) -> bool:
    with _job_lock:
        if _job["kind"] != kind or str(_job["id"]) != str(job_id) or not (_job["thread"] and _job["thread"].is_alive()):
            return False
        _job["cancel"].set()
    return True


# ═══════════════════════════════════════════════════════════════════════════
#  balances — live netting (phase 3, manual §J P2)
# ═══════════════════════════════════════════════════════════════════════════
# A finished run is PARKED (legacy rows say DONE and mean the same): it holds
# what it allocated, inside 1SZ only, until it is approved into ARS_PEND_ALC
# (APPROVED) or discarded (DISCARDED). Any number can be parked at once.
#
#   stock a run may use (BAL_FNL_Q), per RDC + article
#       = max(live FNL_Q − Σ ALLOC_QTY of every parked session, 0)
#       live FNL_Q = Σ ARS_MSA_TOTAL.FNL_Q now — already net of every approved
#       pending (listing and 1SZ), because Pend ALC moves MSA live.
#   size budget a run may use (REM_MJ_SZ_SRT), per store + MAJ_CAT + SZ
#       = max(MJ_SZ_MBQ − MJ_SZ_STK − live open pending − Σ parked, 0)
#   full-option cap, per store + MAJ_CAT
#       = max(MJ_MBQ − MJ_STK − live open pending − Σ parked, 0)
#
# Open pending = ARS_PEND_ALC rows with IS_CLOSED = 0, any SOURCE, PEND_QTY
# (= ALLOC − DO). Its size comes from the article in ARS_MSA_TOTAL (one SZ per
# article, the same spelling Step 1 / Step 2 key on); A / NA rows of Step 2
# take the store's whole-category pending. The approval re-check
# (sz1_approve_service) uses these same helpers.
PARKED_STATUSES = ("PARKED", "DONE")            # DONE = a pre-phase-3 finished run, same meaning
PARKED_SQL = "('PARKED', 'DONE')"
CLOSED_STATUSES = ("APPROVED", "DISCARDED")
PEND_ALC = "ARS_PEND_ALC"


def article_size_sql() -> str:
    """(ARTICLE_NUMBER, SZ, MAJ_CAT) — one row per article, from ARS_MSA_TOTAL.
    It holds every article with open pending (9,284 of 9,284 on 12 Oct 2026),
    one SZ per article, spelled the way Step 1 / Step 2 key on.
    (VW_MASTER_PRODUCT / MASTER_PRODUCT: 3.8 M rows, ARTICLE_NUMBER BIGINT
    with no index, and a few sizes spelled differently from MSA.)"""
    return (f"SELECT ARTICLE_NUMBER, MIN(SZ) AS SZ, MIN(MAJ_CAT) AS MAJ_CAT "
            f"FROM {q(S.MSA_TOTAL)} GROUP BY ARTICLE_NUMBER")


def live_free_sql(where: str = "") -> str:
    """SELECT (RDC, ARTICLE_NUMBER, FREE_Q): free warehouse stock NOW —
    max(Σ ARS_MSA_TOTAL.FNL_Q, 0) per RDC + article. `where` = an optional
    extra condition on alias m (e.g. "m.ARTICLE_NUMBER IN (...)")."""
    w = f"WHERE {where}" if where else ""
    return f"""SELECT m.RDC, m.ARTICLE_NUMBER,
               CAST(CASE WHEN SUM(m.FNL_Q) > 0 THEN SUM(m.FNL_Q) ELSE 0 END AS DECIMAL(18,2)) AS FREE_Q
          FROM {q(S.MSA_TOTAL)} m {w}
         GROUP BY m.RDC, m.ARTICLE_NUMBER"""


def live_pend_sql(grain: str = "size", where: str = "") -> str:
    """SELECT open pending to stores NOW (ARS_PEND_ALC, IS_CLOSED = 0, any
    SOURCE): grain "size" → (ST_CD, MAJ_CAT, SZ, PEND_Q); grain "cat" →
    (ST_CD, MAJ_CAT, PEND_Q). MAJ_CAT and SZ are the article's in
    ARS_MSA_TOTAL (the pending row's own MAJ_CAT when the article is not
    there; its SZ is then NULL and matches no size row). PEND_Q is floored at
    0. `where` = an optional extra condition on alias p."""
    if grain not in ("size", "cat"):
        raise ValueError("grain must be size or cat")
    keys = "p.ST_CD, COALESCE(a.MAJ_CAT, p.MAJ_CAT)" + (", a.SZ" if grain == "size" else "")
    cols = "p.ST_CD, COALESCE(a.MAJ_CAT, p.MAJ_CAT) AS MAJ_CAT" + (", a.SZ" if grain == "size" else "")
    more = f" AND {where}" if where else ""
    return f"""SELECT {cols},
               CAST(CASE WHEN SUM(p.PEND_QTY) > 0 THEN SUM(p.PEND_QTY) ELSE 0 END AS DECIMAL(18,2)) AS PEND_Q
          FROM {q(PEND_ALC)} p
          LEFT JOIN ({article_size_sql()}) a ON a.ARTICLE_NUMBER = p.ARTICLE_NUMBER
         WHERE p.IS_CLOSED = 0 AND p.ST_CD IS NOT NULL{more}
         GROUP BY {keys}"""


def parked_sessions(conn, include: Optional[str] = None) -> List[str]:
    """Every session that holds stock: STATUS PARKED or legacy DONE, newest
    first. `include` = a session being committed in the same transaction
    (its header is still RUNNING) — counted as parked."""
    ids = [str(r[0]) for r in conn.execute(text(f"""
        SELECT SESSION_ID FROM {q(S.SESSION)}
         WHERE STATUS IN {PARKED_SQL} ORDER BY SESSION_AT DESC, SESSION_ID DESC"""))]
    if include and str(include) not in ids:
        ids.insert(0, str(include))
    return [i for i in ids if i.isdigit()]


def balance_sessions(conn) -> List[str]:
    """Kept for existing callers: the sessions netted off the balances — now
    every parked session (parked_sessions)."""
    return parked_sessions(conn)


def parked_scope(ids: List[str]) -> str:
    """WHERE clause on ARS_SZ1_ST_ART_REQ for these sessions (ids are the
    generated DDMMYYHHMM[SS] digits; anything else is dropped)."""
    ids = [str(i) for i in ids if str(i).isdigit()]
    return ("WHERE SESSION_ID IN (" + ", ".join(f"'{i}'" for i in ids) + ")") if ids else "WHERE 1 = 0"


_scope = parked_scope                            # the old private name


_TAKEN_KEYS = {"rdc_art": "SRC_RDC, ARTICLE_NUMBER", "size": "ST_CD, MAJ_CAT, SZ", "cat": "ST_CD, MAJ_CAT"}


def parked_taken_sql(ids: List[str], grain: str) -> str:
    """SELECT what these sessions allocated, as TAKEN: grain "rdc_art"
    (SRC_RDC, ARTICLE_NUMBER), "size" (ST_CD, MAJ_CAT, SZ) or "cat" (ST_CD, MAJ_CAT)."""
    keys = _TAKEN_KEYS[grain]
    return (f"SELECT {keys}, SUM(ALLOC_QTY) AS TAKEN FROM {q(S.LINES)} {parked_scope(ids)} "
            f"AND ALLOC_QTY > 0 GROUP BY {keys}")


def _pos(expr: str) -> str:
    return f"CASE WHEN {expr} > 0 THEN {expr} ELSE 0 END"


# The live figures as SQL on the base tables. Aliases: b = ARS_SZ1_MSA_SZ_BASE,
# r = ARS_SZ1_ST_MJ_SZ_REQ_BASE. live_joins_* add the LEFT JOINs the *_EXPR
# read, with one parked-session list baked in.
def live_joins_base(ids: List[str], alias: str = "b") -> str:
    return (f"\n LEFT JOIN ({live_free_sql()}) lf ON lf.RDC = {alias}.RDC "
            f"AND lf.ARTICLE_NUMBER = {alias}.ARTICLE_NUMBER"
            f"\n LEFT JOIN ({parked_taken_sql(ids, 'rdc_art')}) pt ON pt.SRC_RDC = {alias}.RDC "
            f"AND pt.ARTICLE_NUMBER = {alias}.ARTICLE_NUMBER")


LIVE_FREE_EXPR = _pos("ISNULL(lf.FREE_Q, 0) - ISNULL(pt.TAKEN, 0)")      # b.RDC + article


def live_stock_sql(grain: str = "size") -> str:
    """SELECT the stores' stock NOW from the grid (pending included — Pend ALC
    adds every approval to STK_TTL at once): grain "size" →
    (WERKS, MAJ_CAT, SZ, STK) from ARS_GRID_MJ_VAR_ART; "cat" → (WERKS, MAJ_CAT,
    STK) from ARS_GRID_MJ. Limited to the categories Step 1 kept."""
    cats = f"MAJ_CAT IN (SELECT DISTINCT MAJ_CAT FROM {q(S.SZ_BASE)})"
    if grain == "size":
        return (f"SELECT WERKS, MAJ_CAT, SZ, SUM(STK_TTL) AS STK FROM {q(S.GRID_VAR_ART)} WITH (NOLOCK) "
                f"WHERE {cats} GROUP BY WERKS, MAJ_CAT, SZ")
    if grain == "cat":
        return (f"SELECT WERKS, MAJ_CAT, SUM(STK_TTL) AS STK FROM {q(S.GRID_MJ)} WITH (NOLOCK) "
                f"WHERE {cats} GROUP BY WERKS, MAJ_CAT")
    raise ValueError("grain must be size or cat")


def live_joins_req(ids: List[str], cat: bool = True, size: bool = True) -> str:
    """size → lsc, lss, pts (what LIVE_ROOM_EXPR reads); cat → lsc, ptc (LIVE_MJ_SRT_EXPR)."""
    j = f"\n LEFT JOIN ({live_stock_sql('cat')}) lsc ON lsc.WERKS = r.ST_CD AND lsc.MAJ_CAT = r.MAJ_CAT"
    if size:
        j += (f"\n LEFT JOIN ({live_stock_sql('size')}) lss ON lss.WERKS = r.ST_CD AND lss.MAJ_CAT = r.MAJ_CAT "
              f"AND lss.SZ = r.SZ"
              f"\n LEFT JOIN ({parked_taken_sql(ids, 'size')}) pts ON pts.ST_CD = r.ST_CD "
              f"AND pts.MAJ_CAT = r.MAJ_CAT AND pts.SZ = r.SZ")
    if cat:
        j += (f"\n LEFT JOIN ({parked_taken_sql(ids, 'cat')}) ptc ON ptc.ST_CD = r.ST_CD "
              f"AND ptc.MAJ_CAT = r.MAJ_CAT")
    return j


# A Step 2 row's store stock NOW: its size's, or for A / NA the whole
# category's (as MJ_SZ_STK = MJ_STK at build). Pending to the store is inside
# it, including lines DO has closed but the next grid build has not yet seen
# in the store's own stock — so nothing in transit is given twice.
LIVE_STK_SZ_EXPR = (f"(CASE WHEN r.SZ IN ({FULL_OPT_SQL}) THEN ISNULL(lsc.STK, 0) "
                    f"ELSE ISNULL(lss.STK, 0) END)")
LIVE_ROOM_EXPR = _pos(f"ISNULL(r.MJ_SZ_MBQ, 0) - {LIVE_STK_SZ_EXPR} - ISNULL(pts.TAKEN, 0)")   # live_joins_req
LIVE_MJ_SRT_EXPR = _pos("ISNULL(r.MJ_MBQ, 0) - ISNULL(lsc.STK, 0) - ISNULL(ptc.TAKEN, 0)")      # live_joins_req(cat=True)


def push_balances(conn, pending: Any = None) -> List[str]:
    """Recompute BAL_FNL_Q and REM_MJ_SZ_SRT with the live rules above: live
    free stock / live room less every parked session. Absolute, so
    idempotent. `pending` = a run being committed in this same transaction
    (its session id, or a tuple starting with it): counted as parked.
    Returns the parked session ids used."""
    sid = (pending[0] if isinstance(pending, (tuple, list)) else pending) if pending else None
    ids = parked_sessions(conn, include=sid)
    conn.execute(text(f"""
        UPDATE b SET BAL_FNL_Q = {LIVE_FREE_EXPR}
          FROM {q(S.SZ_BASE)} b {live_joins_base(ids)}"""))
    conn.execute(text(f"""
        UPDATE r SET REM_MJ_SZ_SRT = {LIVE_ROOM_EXPR}
          FROM {q(S.REQ_BASE)} r {live_joins_req(ids, cat=False)}"""))
    return ids


def balance_drift(conn) -> List[str]:
    """Base tables whose balance column disagrees with the live figures (the
    rules push_balances applies). Empty when all is in step. A move in
    ARS_MSA_TOTAL / ARS_PEND_ALC since the last push shows up here too."""
    ids = parked_sessions(conn)
    out = []
    bad = conn.execute(text(f"""
        SELECT COUNT(*) FROM {q(S.SZ_BASE)} b {live_joins_base(ids)}
         WHERE ABS(ISNULL(b.BAL_FNL_Q, -1) - {LIVE_FREE_EXPR}) > 0.005""")).scalar()
    if bad:
        out.append(f"Step 1 table: {bad:,} article(s) whose remaining stock (BAL_FNL_Q) is off")
    bad = conn.execute(text(f"""
        SELECT COUNT(*) FROM {q(S.REQ_BASE)} r {live_joins_req(ids, cat=False)}
         WHERE ABS(ISNULL(r.REM_MJ_SZ_SRT, -1) - {LIVE_ROOM_EXPR}) > 0.005""")).scalar()
    if bad:
        out.append(f"Step 2 table: {bad:,} store × size row(s) whose remaining shortage (REM_MJ_SZ_SRT) is off")
    return out


def repair_balances(user: Optional[str]) -> List[str]:
    with job_slot("balances"):
        with data_engine.begin() as conn:
            ids = push_balances(conn)
    logger.info(f"[sz1] balances re-applied by {user} from sessions {ids}")
    return ids


# ═══════════════════════════════════════════════════════════════════════════
#  previews — nothing is written
# ═══════════════════════════════════════════════════════════════════════════
def preview_base(top: int = 20, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The Step 1 selection with the saved rule values, or `settings` laid
    over them (Preview with these values — nothing is saved or written)."""
    from app.services import sz1_settings
    S.ensure_tables()
    used = sz1_settings.clean(settings or {}, sz1_settings.get_all())
    with data_engine.connect() as conn:
        sample = rows(conn, base_sql(f"SELECT TOP ({int(top)}) * FROM selected "
                                     "ORDER BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, SZ", used))
        by_rdc = rows(conn, base_sql("""
            SELECT RDC, COUNT(*) AS ARTICLES, COUNT(DISTINCT OPT_KEY) AS OPTIONS, SUM(FNL_Q) AS FNL_Q,
                   SUM(R1_LOW_VAR) AS R1, SUM(R2_SINGLE_A) AS R2, SUM(R3_CAT_LE200) AS R3, SUM(R4_OPT_LE20) AS R4
              FROM selected GROUP BY RDC ORDER BY RDC""", used))
        by_rule = rows(conn, base_sql("""
            SELECT 'R1' AS RULES, SUM(R1_LOW_VAR) AS ARTICLES, SUM(CASE WHEN R1_LOW_VAR = 1 THEN FNL_Q END) AS FNL_Q FROM selected
            UNION ALL SELECT 'R2', SUM(R2_SINGLE_A), SUM(CASE WHEN R2_SINGLE_A = 1 THEN FNL_Q END) FROM selected
            UNION ALL SELECT 'R3', SUM(R3_CAT_LE200), SUM(CASE WHEN R3_CAT_LE200 = 1 THEN FNL_Q END) FROM selected
            UNION ALL SELECT 'R4', SUM(R4_OPT_LE20), SUM(CASE WHEN R4_OPT_LE20 = 1 THEN FNL_Q END) FROM selected
            UNION ALL SELECT 'All rules', COUNT(*), SUM(FNL_Q) FROM selected
            UNION ALL SELECT 'Gap excluded', SUM(CAST(EXCLUDED AS INT)), SUM(CASE WHEN EXCLUDED = 1 THEN FNL_Q END) FROM gaps
            UNION ALL SELECT 'Gap total', COUNT(*), SUM(FNL_Q) FROM gaps""", used))
        live = rows(conn, f"SELECT RDC, COUNT(DISTINCT ARTICLE_NUMBER) AS LIVE_ARTICLES, SUM(FNL_Q) AS LIVE_FNL_Q "
                          f"FROM {q(S.MSA_TOTAL)} WHERE FNL_Q > 0 GROUP BY RDC ORDER BY RDC")
    return {"sample": sample, "by_rdc": by_rdc, "by_rule": by_rule, "live": live, "settings": used}


def preview_req(top: int = 20) -> Dict[str, Any]:
    S.ensure_tables()
    with data_engine.connect() as conn:
        if not conn.execute(text(f"SELECT TOP 1 1 FROM {q(S.SZ_BASE)}")).fetchone():
            raise ValueError("Step 1 (SZ Base) is empty. Build it first — Step 2 takes its MAJ_CAT + size pairs from it.")
        sample = rows(conn, req_sql(f"SELECT TOP ({int(top)}) * FROM grid ORDER BY RDC, ST_CD, MAJ_CAT, SZ"))
        by_rdc = rows(conn, req_sql("""
            SELECT RDC, COUNT(*) AS ROWS_, COUNT(DISTINCT ST_CD) AS STORES, COUNT(DISTINCT MAJ_CAT) AS MAJ_CATS,
                   COUNT(DISTINCT MAJ_CAT + '|' + SZ) AS COMBOS,
                   SUM(CASE WHEN MJ_SZ_SRT > 0 THEN 1 ELSE 0 END) AS SHORT_ROWS, SUM(MJ_SZ_SRT) AS MJ_SZ_SRT
              FROM grid GROUP BY RDC ORDER BY RDC"""))
    return {"sample": sample, "by_rdc": by_rdc}


# ═══════════════════════════════════════════════════════════════════════════
#  builds
# ═══════════════════════════════════════════════════════════════════════════
def _summary_base(conn) -> Dict[str, Any]:
    return {
        "by_rdc": rows(conn, f"""
            SELECT RDC, COUNT(*) AS ARTICLES, COUNT(DISTINCT OPT_KEY) AS OPTIONS,
                   COUNT(DISTINCT MAJ_CAT) AS MAJ_CATS, SUM(FNL_Q) AS FNL_Q,
                   SUM(CAST(R1_LOW_VAR AS INT)) AS R1, SUM(CAST(R2_SINGLE_A AS INT)) AS R2,
                   SUM(CAST(R3_CAT_LE200 AS INT)) AS R3, SUM(CAST(R4_OPT_LE20 AS INT)) AS R4
              FROM {q(S.SZ_BASE)} GROUP BY RDC ORDER BY RDC"""),
        "by_rules": rows(conn, f"""
            SELECT RULES, COUNT(*) AS ARTICLES, SUM(FNL_Q) AS FNL_Q
              FROM {q(S.SZ_BASE)} GROUP BY RULES ORDER BY COUNT(*) DESC"""),
        "by_var_count": rows(conn, f"""
            SELECT VAR_COUNT, COUNT(*) AS ARTICLES, SUM(FNL_Q) AS FNL_Q
              FROM {q(S.SZ_BASE)} GROUP BY VAR_COUNT ORDER BY VAR_COUNT"""),
        "by_cont_src": rows(conn, f"""
            SELECT CONT_SRC, COUNT(*) AS ARTICLES, SUM(FNL_Q) AS FNL_Q,
                   MIN(OPT_CONT_PCT) AS MIN_PCT, MAX(OPT_CONT_PCT) AS MAX_PCT
              FROM {q(S.SZ_BASE)} GROUP BY CONT_SRC ORDER BY COUNT(*) DESC"""),
    }


def _summary_req(conn) -> Dict[str, Any]:
    return {
        "by_rdc": rows(conn, f"""
            SELECT RDC, COUNT(*) AS ROWS_, COUNT(DISTINCT ST_CD) AS STORES, COUNT(DISTINCT MAJ_CAT) AS MAJ_CATS,
                   COUNT(DISTINCT SZ) AS SIZES, SUM(CASE WHEN MJ_SZ_SRT > 0 THEN 1 ELSE 0 END) AS SHORT_ROWS,
                   SUM(MJ_SZ_SRT) AS MJ_SZ_SRT
              FROM {q(S.REQ_BASE)} GROUP BY RDC ORDER BY RDC"""),
        "by_cont_src": rows(conn, f"""
            SELECT CONT_SRC, COUNT(*) AS ROWS_, MIN(CONT_PCT_SZ) AS MIN_PCT, MAX(CONT_PCT_SZ) AS MAX_PCT
              FROM {q(S.REQ_BASE)} GROUP BY CONT_SRC ORDER BY COUNT(*) DESC"""),
        "totals": rows(conn, f"""
            SELECT SUM(MJ_STK) AS MJ_STK, SUM(MJ_MBQ) AS MJ_MBQ, SUM(MJ_SRT) AS MJ_SRT,
                   SUM(MJ_SZ_STK) AS MJ_SZ_STK, SUM(MJ_SZ_MBQ) AS MJ_SZ_MBQ,
                   SUM(MJ_SZ_SRT) AS MJ_SZ_SRT, SUM(MJ_SZ_EXC) AS MJ_SZ_EXC
              FROM {q(S.REQ_BASE)}""")[0],
    }


def msa_recon(conn, rules: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Where every piece of ARS_MSA_TOTAL went in Step 1, so the CONT gap can
    be checked against the complete MSA stock: all rows → free stock (FNL_Q > 0)
    → size-applicable → has CONT / gap / A-NA exempt → SZ Base."""
    total = rows(conn, f"""
        SELECT COUNT(DISTINCT RDC + '|' + ARTICLE_NUMBER) AS ARTICLES, SUM(STK_QTY) AS STK_QTY, SUM(FNL_Q) AS FNL_Q,
               COUNT(DISTINCT CASE WHEN FNL_Q > 0 THEN RDC + '|' + ARTICLE_NUMBER END) AS LIVE_ARTICLES,
               SUM(CASE WHEN FNL_Q > 0 THEN FNL_Q ELSE 0 END) AS LIVE_FNL_Q
          FROM {q(S.MSA_TOTAL)}""")[0]
    lines = rows(conn, base_sql("""
        SELECT CASE WHEN ISNULL(SZ_APPLICABLE, '') <> 'Y'          THEN '1 Not size-applicable (out of 1SZ)'
                    WHEN GAP_TYPE = 'OPT_NO_CONT'                  THEN '3 Gap: option has no CONT'
                    WHEN GAP_TYPE = 'SZ_NO_CONT'                   THEN '4 Gap: size has no CONT'
                    WHEN SZ IN ('A', 'NA') AND OPT_CONT_PCT = 0    THEN '5 A / NA option (exempt from gap)'
                    ELSE '2 Has CONT' END AS BUCKET,
               COUNT(*) AS ARTICLES, SUM(FNL_Q) AS FNL_Q,
               SUM(CASE WHEN R1_LOW_VAR + R2_SINGLE_A + R3_CAT_LE200 + R4_OPT_LE20 > 0 THEN 1 ELSE 0 END) AS RULE_FIRES
          FROM scored
         GROUP BY CASE WHEN ISNULL(SZ_APPLICABLE, '') <> 'Y'       THEN '1 Not size-applicable (out of 1SZ)'
                    WHEN GAP_TYPE = 'OPT_NO_CONT'                  THEN '3 Gap: option has no CONT'
                    WHEN GAP_TYPE = 'SZ_NO_CONT'                   THEN '4 Gap: size has no CONT'
                    WHEN SZ IN ('A', 'NA') AND OPT_CONT_PCT = 0    THEN '5 A / NA option (exempt from gap)'
                    ELSE '2 Has CONT' END
         ORDER BY 1""", rules))
    for r in lines:
        r["BUCKET"] = r["BUCKET"][2:]
    return {"total": total, "buckets": lines}


def _update_build(build_id: int, **cols) -> None:
    sets, params = set_clause(cols)
    with data_engine.begin() as conn:
        conn.execute(text(f"UPDATE {q(S.BUILD)} SET {sets} WHERE BUILD_ID = :id"), {**params, "id": build_id})


def start_build(step: int, user: Optional[str]) -> int:
    if step not in (1, 2):
        raise ValueError("step must be 1 or 2")
    S.ensure_tables()
    kind = f"build{step}"
    with job_slot(kind):
        # validate first, then create (manual §A): a block refuses the build,
        # warnings are stamped on its summary
        from app.services import sz1_readiness
        ready = sz1_readiness.check(kind)
        if not ready["ok"]:
            raise ValueError("Cannot build: " + " ".join(ready["block"]))
        if step == 2:
            with data_engine.connect() as conn:
                if not conn.execute(text(f"SELECT TOP 1 1 FROM {q(S.SZ_BASE)}")).fetchone():
                    raise ValueError("Step 1 (SZ Base) is empty. Build it first.")
        with data_engine.begin() as conn:
            bid = conn.execute(text(f"""
                INSERT INTO {q(S.BUILD)} (STEP, STATUS, PROGRESS, CREATED_BY)
                OUTPUT INSERTED.BUILD_ID VALUES (:s, 'RUNNING', 'Starting', :by)"""),
                {"s": step, "by": user}).scalar()
        start_job(kind, bid, _build, (bid, step, user, ready["warn"]))
    return int(bid)


def _build(bid: int, step: int, user: Optional[str], warns: List[str], ev: threading.Event) -> None:
    t0 = time.time()
    table = S.SZ_BASE if step == 1 else S.REQ_BASE
    try:
        _update_build(bid, PROGRESS="Selecting and writing rows (one transaction)")
        from app.services import sz1_settings
        rules = sz1_settings.get_all() if step == 1 else None
        with data_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {q(table)}"))
            if step == 1:
                conn.execute(text(base_sql(
                    f"INSERT INTO {q(S.SZ_BASE)} (BUILD_ID, {BASE_COLUMNS})\n"
                    f"SELECT :bid, {BASE_COLUMNS} FROM selected", rules)), {"bid": bid})
                # the zero-CONT gap of the same build, in the same transaction
                conn.execute(text(f"TRUNCATE TABLE {q(S.GAP)}"))
                conn.execute(text(base_sql(
                    f"INSERT INTO {q(S.GAP)} (BUILD_ID, {GAP_COLUMNS})\n"
                    f"SELECT :bid, {GAP_COLUMNS} FROM gaps", rules)), {"bid": bid})
            else:
                conn.execute(text(req_sql(
                    f"INSERT INTO {q(S.REQ_BASE)} (BUILD_ID, {REQ_COLUMNS})\n"
                    f"SELECT :bid, {REQ_COLUMNS} FROM grid")), {"bid": bid})
            n = conn.execute(text(f"SELECT COUNT(*) FROM {q(table)}")).scalar()
            # A rebuild resets the balance column to full; re-apply the latest
            # session so this table never disagrees with what was issued.
            sessions = push_balances(conn)
            summary = _summary_base(conn) if step == 1 else _summary_req(conn)
            summary["balance_sessions"] = sessions
            summary["readiness"] = warns            # warnings it was built with
            if step == 2:
                summary["share_rule"] = DEFAULT_SHARE_RULE
            if rules is not None:
                summary["rules"] = rules            # the values this build selected with
                summary["msa_recon"] = msa_recon(conn, rules)
                summary["gap"] = rows(conn, f"""
                    SELECT GAP_TYPE, COUNT(DISTINCT RDC + '|' + OPT_KEY) AS OPTIONS, COUNT(*) AS ARTICLES,
                           SUM(FNL_Q) AS FNL_Q, SUM(CAST(EXCLUDED AS INT)) AS EXCLUDED,
                           SUM(CASE WHEN EXCLUDED = 1 THEN FNL_Q END) AS EXCLUDED_FNL_Q
                      FROM {q(S.GAP)} GROUP BY GAP_TYPE ORDER BY GAP_TYPE""")
        _update_build(bid, STATUS="DONE", ERROR=None, ROWS_BUILT=int(n), SUMMARY_JSON=json.dumps(summary, default=str),
                      PROGRESS=f"{int(n):,} rows written", FINISHED_AT=NOW,
                      DURATION_SEC=round(time.time() - t0, 1))
        logger.info(f"[sz1] step {step} build {bid} by {user}: {n} rows in {time.time() - t0:.1f}s")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[sz1] step {step} build {bid} failed")
        _update_build(bid, STATUS="FAILED", PROGRESS="Failed — the previous build is unchanged",
                      ERROR=f"{e}\n{traceback.format_exc()}"[:8000], FINISHED_AT=NOW,
                      DURATION_SEC=round(time.time() - t0, 1))


def _build_row(r) -> Dict[str, Any]:
    d = {k: jsonable(v) for k, v in r._mapping.items()}
    s = d.pop("SUMMARY_JSON", None)
    try:
        d["summary"] = json.loads(s) if s else None
    except ValueError:
        d["summary"] = None
    return d


def get_build(build_id: int) -> Optional[Dict[str, Any]]:
    S.ensure_tables()
    with data_engine.connect() as conn:
        r = conn.execute(text(f"SELECT * FROM {q(S.BUILD)} WHERE BUILD_ID = :id"), {"id": build_id}).fetchone()
    if r is None:
        return None
    d = _build_row(r)
    if d["STATUS"] == "RUNNING" and not _local_running():
        # Possibly another process's build: orphaned only once it is too old to be live.
        with data_engine.begin() as conn:
            hit = conn.execute(text(f"""
                UPDATE {q(S.BUILD)} SET STATUS = 'FAILED', PROGRESS = 'Interrupted', FINISHED_AT = SYSDATETIME(),
                       ERROR = 'The server stopped while this build was running. Its transaction never committed, so the previous build is unchanged.'
                 WHERE BUILD_ID = :id AND STATUS = 'RUNNING'
                   AND DATEDIFF(MINUTE, STARTED_AT, SYSDATETIME()) >= {BUILD_STALE_MIN}"""), {"id": build_id}).rowcount
        if hit:
            return get_build(build_id)
    return d


def latest_build(step: int, done_only: bool = True) -> Optional[Dict[str, Any]]:
    S.ensure_tables()
    with data_engine.connect() as conn:
        r = conn.execute(text(f"""
            SELECT TOP 1 * FROM {q(S.BUILD)} WHERE STEP = :s {"AND STATUS = 'DONE'" if done_only else ""}
             ORDER BY BUILD_ID DESC"""), {"s": step}).fetchone()
    return _build_row(r) if r is not None else None


def clear_builds(step: int, user: Optional[str]) -> int:
    """Delete a step's build-history rows, keeping the latest DONE build (its
    summary drives the page, the stale-settings check and Step 3's gates) and
    any build still running. The built table itself is not touched."""
    if step not in (1, 2):
        raise ValueError("step must be 1 or 2")
    S.ensure_tables()
    with data_engine.begin() as conn:
        n = conn.execute(text(f"""
            DELETE FROM {q(S.BUILD)}
             WHERE STEP = :s AND STATUS <> 'RUNNING'
               AND BUILD_ID <> ISNULL((SELECT MAX(BUILD_ID) FROM {q(S.BUILD)} WHERE STEP = :s AND STATUS = 'DONE'), -1)"""),
            {"s": step}).rowcount
    logger.info(f"[sz1] step {step} build history cleared by {user}: {n} row(s)")
    return int(n or 0)


def recent_builds(step: int, limit: int = 10) -> List[Dict[str, Any]]:
    S.ensure_tables()
    with data_engine.connect() as conn:
        res = conn.execute(text(f"""
            SELECT TOP (:n) BUILD_ID, STEP, STATUS, ROWS_BUILT, PROGRESS, CREATED_BY, STARTED_AT,
                   FINISHED_AT, DURATION_SEC, LEFT(ERROR, 400) AS ERROR
              FROM {q(S.BUILD)} WHERE STEP = :s ORDER BY BUILD_ID DESC"""), {"n": limit, "s": step})
        return [{k: jsonable(v) for k, v in r._mapping.items()} for r in res]


def _built_filter(step: int, rdc: Optional[str], maj_cat: Optional[str], store: Optional[str],
                  art: Optional[str] = None, sz: Optional[str] = None, rule: Optional[str] = None,
                  cont_src: Optional[str] = None, cf: Optional[Dict[str, Any]] = None,
                  ob: Optional[str] = None, od: Optional[str] = None):
    """(table, WHERE clause, params, ORDER BY) for reading a built Step 1 / 2 table.
    ob / od = a clicked column header and its direction."""
    if step not in (1, 2):
        raise ValueError("step must be 1 or 2")
    where, p = [], {}
    if rdc:
        where.append("RDC = :rdc"); p["rdc"] = rdc.strip().upper()
    if maj_cat:
        where.append("MAJ_CAT LIKE :mc"); p["mc"] = f"%{maj_cat.strip()}%"
    if store and step == 2:
        where.append("ST_CD = :st"); p["st"] = store.strip().upper()
    if art and step == 1:
        where.append("(ARTICLE_NUMBER = :art OR GEN_ART_NUMBER = :art)"); p["art"] = art.strip()
    if sz:
        where.append("SZ = :sz"); p["sz"] = sz.strip().upper()
    if rule and step == 1 and rule.strip().upper() in ("R1", "R2", "R3", "R4"):
        where.append("RULES LIKE :rule"); p["rule"] = f"%{rule.strip().upper()}%"
    if cont_src:
        where.append("CONT_SRC = :cs"); p["cs"] = cont_src.strip().upper()
    if cf:                                    # Excel-style column filters (sz1_filter)
        from app.services import sz1_filter
        more_w, more_p = sz1_filter.where(S.SZ_BASE if step == 1 else S.REQ_BASE, cf)
        where += more_w; p.update(more_p)
    table = S.SZ_BASE if step == 1 else S.REQ_BASE
    order = "RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, SZ" if step == 1 else "RDC, ST_CD, MAJ_CAT, SZ"
    if ob:
        from app.services import sz1_filter
        order = sz1_filter.order_by(table, ob, od, order)
    return table, ("WHERE " + " AND ".join(where)) if where else "", p, order


def sample(step: int, top: int = 200, rdc: Optional[str] = None, maj_cat: Optional[str] = None,
           store: Optional[str] = None, **more) -> List[Dict[str, Any]]:
    """Rows of a built table, filtered. Read only."""
    S.ensure_tables()
    table, w, p, order = _built_filter(step, rdc, maj_cat, store, **more)
    with data_engine.connect() as conn:
        return rows(conn, f"SELECT TOP ({min(max(int(top), 1), 2000)}) * FROM {q(table)} {w} ORDER BY {order}", p)


def count_rows(step: int, rdc: Optional[str] = None, maj_cat: Optional[str] = None,
               store: Optional[str] = None, **more) -> int:
    """How many rows of a built table match the filters (the browse shows the top ones)."""
    S.ensure_tables()
    table, w, p, _order = _built_filter(step, rdc, maj_cat, store, **more)
    with data_engine.connect() as conn:
        return int(conn.execute(text(f"SELECT COUNT_BIG(*) FROM {q(table)} WITH (NOLOCK) {w}"), p).scalar() or 0)


def export_csv(step: int, rdc: Optional[str] = None, maj_cat: Optional[str] = None,
               store: Optional[str] = None, **more):
    """Every row of a built Step 1 / 2 table (filtered the same way as the
    browse panel) as CSV, streamed in chunks — Step 2 is ~0.5 M rows. Read only."""
    import csv
    import io
    import re
    S.ensure_tables()
    table, w, p, order = _built_filter(step, rdc, maj_cat, store, **more)
    args = [p[m] for m in re.findall(r":(\w+)", w)]          # named → positional for the raw cursor
    w = re.sub(r":\w+", "?", w)
    raw = data_engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(f"SELECT * FROM {q(table)} WITH (NOLOCK) {w} ORDER BY {order}", *args)
        buf = io.StringIO()
        wr = csv.writer(buf)
        wr.writerow([d[0] for d in cur.description])
        while True:
            chunk = cur.fetchmany(10_000)
            if not chunk:
                break
            wr.writerows(chunk)
            yield buf.getvalue().encode("utf-8")
            buf.seek(0); buf.truncate(0)
        if buf.tell():
            yield buf.getvalue().encode("utf-8")
    finally:
        try:
            raw.close()
        except Exception:
            pass


def status() -> Dict[str, Any]:
    """Everything the Step 1 and Step 2 pages show: latest build of each, the
    running job, table sizes, and whether Step 2 is behind Step 1."""
    S.ensure_tables()
    b1, b2 = latest_build(1), latest_build(2)
    live = running()
    for step in (1, 2):                         # mark an orphaned RUNNING row as failed
        last = latest_build(step, done_only=False)
        if last and last["STATUS"] == "RUNNING":
            get_build(last["BUILD_ID"])
    notes: List[str] = []
    from app.services import sz1_settings
    rules = sz1_settings.get_all()
    if b1 and not sz1_settings.same((b1.get("summary") or {}).get("rules"), rules):
        notes.append(f"The rule settings changed after Step 1 build {b1['BUILD_ID']} (or it predates them). "
                     "Generate Step 1 again, then Step 2, to apply them.")
    if b1 and b2 and b2["BUILD_ID"] < b1["BUILD_ID"]:
        notes.append(f"Step 2 (build {b2['BUILD_ID']}) was built before the latest Step 1 (build {b1['BUILD_ID']}). "
                     "Rebuild Step 2 so its MAJ_CAT + size pairs match the candidates.")
    # While a job holds the base tables (a TRUNCATE takes a schema lock; a run
    # rewrites the balances) even metadata reads of them would wait, so they
    # are skipped and reported as busy.
    busy = (S.SZ_BASE,) if live and live["kind"] == "build1" else \
           (S.REQ_BASE,) if live and live["kind"] == "build2" else ()
    with data_engine.connect() as conn:
        drift = balance_drift(conn) if (b1 or b2) and not live else []
        bal = balance_sessions(conn)
    return {"step1": b1, "step2": b2, "running": live, "counts": S.table_counts(skip=busy), "rules": rules,
            "rules_meta": sz1_settings.meta(), "rules_default": sz1_settings.defaults(),
            "notes": notes, "drift": drift, "balance_sessions": bal}
