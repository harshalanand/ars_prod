"""
alloc_pool.py — Fresh/GRT typed warehouse-pool helpers.

Spec: docs/FSD_FRESH_GRT_HOLD_CONTROL.md (FS-01 SLOC classification,
FS-02 pool override). One MSA run carries per-SLOC pivot columns for ALL
SLOCs; each allocation run picks exactly one pool type ('FRESH' | 'GRT')
and recomputes, per MSA row:

    FNL_Q_EFF = max( min( Σ SLOC-cols(alloc_type) − PEND_T − HOLD_T,
                          FNL_Q ), 0 )

where PEND_T / HOLD_T are LIVE typed aggregates over the open pend/hold
ledgers. Untyped legacy rows (ALLOC_TYPE NULL or '') deduct from BOTH
pools (conservative — BR-04/TC-16). FNL_Q (the MSA total) stays as the
safety cap so no pool can ever exceed physical availability (BR-16).

Aggregate subqueries emitted by pend_agg_sql / hold_agg_sql expose
collision-free output columns (deliberate — they get LEFT JOINed into
statements full of unqualified MSA column references):

    grain 'var' → RDC_KEY, ART_KEY,           PEND_T / HOLD_T
    grain 'gen' → RDC_KEY, GEN_KEY, CLR_KEY,  PEND_T / HOLD_T

Both subqueries carry a single `:pool_alloc_type` bind parameter — the
caller passes {"pool_alloc_type": <'FRESH'|'GRT'>} on execute.
"""
from typing import Dict, List, Optional

from sqlalchemy import text
from loguru import logger

from app.utils.db_helpers import get_columns, table_exists

SLOC_SETTINGS_TABLE = "ARS_MSA_SLOC_SETTINGS"
PEND_TABLE = "ARS_PEND_ALC"
HOLD_TABLE = "ARS_NL_TBL_HOLD_TRACKING"
DEFAULT_ST_MASTER = "Master_ALC_INPUT_ST_MASTER"


class NoPoolColumnsError(Exception):
    """Raised when a table carries zero SLOC columns of the requested
    pool type (FSD error E-01 — surfaced by /listing/generate as HTTP 400)."""

    def __init__(self, alloc_type: str):
        self.alloc_type = alloc_type
        super().__init__(
            f"MSA contains no {alloc_type} SLOC columns. Regenerate MSA "
            f"including {alloc_type} SLOCs (see {SLOC_SETTINGS_TABLE})."
        )


def get_sloc_type_map(conn) -> Dict[str, str]:
    """{sloc: 'FRESH'|'GRT'} from ARS_MSA_SLOC_SETTINGS, active rows only.

    Missing table → {} with a warning (typed_sloc_cols will then raise
    NoPoolColumnsError, making the misconfiguration loud instead of
    silently allocating from an empty pool).
    """
    if not table_exists(conn, SLOC_SETTINGS_TABLE):
        logger.warning(
            f"[alloc_pool] {SLOC_SETTINGS_TABLE} not found — no SLOC can be "
            f"classified; run migration 019_rename_msa_sloc_settings.sql"
        )
        return {}
    rows = conn.execute(text(
        f"SELECT [sloc], [sloc_type] FROM [{SLOC_SETTINGS_TABLE}] "
        f"WHERE [is_active] = 1"
    )).fetchall()
    return {
        str(r[0]).strip(): str(r[1] or "FRESH").strip().upper()
        for r in rows if r[0] is not None and str(r[0]).strip()
    }


def typed_sloc_cols(conn, table: str, alloc_type: str,
                    type_map: Optional[Dict[str, str]] = None) -> List[str]:
    """SLOC pivot columns of `table` whose classified type == alloc_type.

    Intersects the table's INFORMATION_SCHEMA columns with the settings
    map (case-insensitive; returns the table's actual column casing).
    Classified SLOCs missing from the table are logged by name — that is
    the symptom of an MSA generated before the SLOC went live. Raises
    NoPoolColumnsError (E-01) when the intersection is empty.
    """
    if type_map is None:
        type_map = get_sloc_type_map(conn)
    at = str(alloc_type or "").strip().upper()
    upper_to_actual = {c.upper(): c for c in get_columns(conn, table)}

    pool_cols: List[str] = []
    missing: List[str] = []
    for sloc, styp in type_map.items():
        actual = upper_to_actual.get(sloc.upper())
        if actual is None:
            missing.append(sloc)
            continue
        if styp == at:
            pool_cols.append(actual)

    if missing:
        logger.warning(
            f"[alloc_pool] {len(missing)} classified SLOC(s) missing from "
            f"[{table}] — stale MSA vs {SLOC_SETTINGS_TABLE}? {sorted(missing)}"
        )
    if not pool_cols:
        raise NoPoolColumnsError(at)
    return pool_cols


def pool_stk_expr(cols: List[str], alias: str) -> str:
    """Σ of the typed SLOC columns as a SQL float expression.

    Empty column list → '0' (defensive fallback; callers normally raise
    NoPoolColumnsError before getting here).
    """
    if not cols:
        return "0"
    return "(" + " + ".join(
        f"ISNULL(TRY_CAST({alias}.[{c}] AS FLOAT), 0)" for c in cols
    ) + ")"


def fnl_q_eff_expr(pool_expr: str, fnl_expr: str,
                   pend_ref: str = "PT.[PEND_T]",
                   hold_ref: str = "HT.[HOLD_T]") -> str:
    """FS-02 pool formula as a SQL Server CASE expression (no LEAST/GREATEST):

        max( min( pool − PEND_T − HOLD_T, FNL_Q ), 0 )

    pend_ref / hold_ref are the joined aggregate columns (NULL-safe)."""
    net = f"({pool_expr} - ISNULL({pend_ref}, 0) - ISNULL({hold_ref}, 0))"
    inner_min = f"(CASE WHEN {net} < {fnl_expr} THEN {net} ELSE {fnl_expr} END)"
    return f"(CASE WHEN {inner_min} > 0 THEN {inner_min} ELSE 0 END)"


def _typed_match(alias: str) -> str:
    """Untyped/legacy matching — '' and NULL both mean legacy and deduct
    from every pool; typed rows only match their own run type."""
    return (f"({alias}.[ALLOC_TYPE] = :pool_alloc_type "
            f"OR {alias}.[ALLOC_TYPE] IS NULL "
            f"OR {alias}.[ALLOC_TYPE] = '')")


def pend_agg_sql(grain: str) -> str:
    """Typed open-pend aggregate subquery over ARS_PEND_ALC.

    Open row: not closed and remaining qty (ALLOC_QTY − DO_QTY) > 0 — the
    same source MSA generation bakes PEND_QTY from, so live reads cannot
    drift from the baked totals (FS-02).

    grain 'var' → (RDC_KEY, ART_KEY, PEND_T)
    grain 'gen' → (RDC_KEY, GEN_KEY, CLR_KEY, PEND_T)
    Bind param: :pool_alloc_type
    """
    qty = ("(ISNULL(TRY_CAST(P.[ALLOC_QTY] AS FLOAT), 0) "
           "- ISNULL(TRY_CAST(P.[DO_QTY] AS FLOAT), 0))")
    where = (f"ISNULL(P.[IS_CLOSED], 0) = 0 AND {qty} > 0 "
             f"AND {_typed_match('P')}")
    if grain == "var":
        keys = (
            "LTRIM(RTRIM(CAST(P.[RDC] AS NVARCHAR(50)))) AS RDC_KEY, "
            "LTRIM(RTRIM(CAST(P.[ARTICLE_NUMBER] AS NVARCHAR(30)))) AS ART_KEY"
        )
        group = ("LTRIM(RTRIM(CAST(P.[RDC] AS NVARCHAR(50)))), "
                 "LTRIM(RTRIM(CAST(P.[ARTICLE_NUMBER] AS NVARCHAR(30))))")
    elif grain == "gen":
        keys = (
            "LTRIM(RTRIM(CAST(P.[RDC] AS NVARCHAR(50)))) AS RDC_KEY, "
            "TRY_CAST(TRY_CAST(P.[GEN_ART_NUMBER] AS FLOAT) AS BIGINT) AS GEN_KEY, "
            "LTRIM(RTRIM(CAST(ISNULL(P.[CLR], '') AS NVARCHAR(200)))) AS CLR_KEY"
        )
        group = ("LTRIM(RTRIM(CAST(P.[RDC] AS NVARCHAR(50)))), "
                 "TRY_CAST(TRY_CAST(P.[GEN_ART_NUMBER] AS FLOAT) AS BIGINT), "
                 "LTRIM(RTRIM(CAST(ISNULL(P.[CLR], '') AS NVARCHAR(200))))")
    else:
        raise ValueError(f"pend_agg_sql: unknown grain {grain!r}")
    return (f"SELECT {keys}, SUM({qty}) AS PEND_T "
            f"FROM [{PEND_TABLE}] P WHERE {where} GROUP BY {group}")


def hold_agg_sql(grain: str, st_master_table: str = DEFAULT_ST_MASTER) -> str:
    """Typed open-hold aggregate subquery over ARS_NL_TBL_HOLD_TRACKING.

    Open row: IS_CLOSED = 0 and HOLD_REM > 0 (mirrors
    pend_alc_service.bootstrap_msa_hold_sync). RDC resolves from the row
    itself, falling back to the store master on H.WERKS = SM.ST_CD.

    grain 'var' → (RDC_KEY, ART_KEY, HOLD_T)
    grain 'gen' → (RDC_KEY, GEN_KEY, CLR_KEY, HOLD_T)
    Bind param: :pool_alloc_type
    """
    rdc = "COALESCE(NULLIF(H.[RDC], ''), SM.[RDC])"
    where = ("ISNULL(H.[IS_CLOSED], 0) = 0 AND ISNULL(H.[HOLD_REM], 0) > 0 "
             f"AND {_typed_match('H')}")
    if grain == "var":
        keys = (f"{rdc} AS RDC_KEY, "
                "LTRIM(RTRIM(CAST(H.[VAR_ART] AS NVARCHAR(30)))) AS ART_KEY")
        group = f"{rdc}, LTRIM(RTRIM(CAST(H.[VAR_ART] AS NVARCHAR(30))))"
    elif grain == "gen":
        keys = (
            f"{rdc} AS RDC_KEY, "
            "TRY_CAST(H.[GEN_ART_NUMBER] AS BIGINT) AS GEN_KEY, "
            "LTRIM(RTRIM(CAST(ISNULL(H.[CLR], '') AS NVARCHAR(200)))) AS CLR_KEY"
        )
        group = (f"{rdc}, TRY_CAST(H.[GEN_ART_NUMBER] AS BIGINT), "
                 "LTRIM(RTRIM(CAST(ISNULL(H.[CLR], '') AS NVARCHAR(200))))")
    else:
        raise ValueError(f"hold_agg_sql: unknown grain {grain!r}")
    return (f"SELECT {keys}, SUM(CAST(H.[HOLD_REM] AS FLOAT)) AS HOLD_T "
            f"FROM [{HOLD_TABLE}] H "
            f"LEFT JOIN [{st_master_table}] SM ON SM.[ST_CD] = H.[WERKS] "
            f"WHERE {where} GROUP BY {group}")
