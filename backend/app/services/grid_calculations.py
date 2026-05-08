"""
Grid Builder — Pre-Grid Calculations
======================================
All calculations done in ARS_CALC_ST_MAJ_CAT (master tables NOT modified).

PRIORITY RULE for CO_MAJ_CAT vs ST_MAJ_CAT:
  - CO_MAJ_CAT = company level → applies to ALL stores for that MAJ_CAT
  - ST_MAJ_CAT = store level   → applies to THAT store only
  - If BOTH have values → take MAX
  - CO values are applied first, then ST overrides/merges

STEPS:
  1. Create calc table (copy from ST_MAJ_CAT)
  2. Merge CO_MAJ_CAT values (apply to all stores)
  3. Apply defaults: LISTING, I_ROD, growth rates, CLR/ACS_D
  4. Calculate ALC_D (total sale days)
  5. Calculate SAL_PD (per day sale)

To change column names or logic → edit below and restart backend.
"""
from typing import List, Dict, Any
from sqlalchemy import text
from loguru import logger

from app.utils.db_helpers import (
    run_sql, table_exists, column_exists, ensure_column,
)


# ==========================================================================
# TABLE NAMES — Change here if your table names differ
# ==========================================================================
TABLES = {
    "ST_MAJ":   "Master_ALC_INPUT_ST_MAJ_CAT",
    "CO_MAJ":   "Master_ALC_INPUT_CO_MAJ_CAT",
    "ST_MAST":  "Master_ALC_INPUT_ST_MASTER",
    "CALC":     "ARS_CALC_ST_MAJ_CAT",
}

# ==========================================================================
# COLUMN NAMES — Change here if your column names differ
# ==========================================================================
# From ST_MASTER
COL_INT_DAYS    = "INT_DAYS"
COL_PRD_DAYS    = "PRD_DAYS"
COL_SL_CVR      = "SL_CVR"

# Sale calculation
COL_CM_SAL_Q    = "CM_SAL_Q"
COL_CM_REM_D    = "CM_REM_D"
COL_NM_SAL_D    = "NM_SAL_Q"
COL_NM_REM_D    = "NM_REM_D"

# Columns that need CO/ST merge + defaults
COL_LISTING     = "LISTING"
COL_I_ROD       = "I_ROD"
COL_MANUAL_DENSITY = "MANUAL_DENSITY"
COL_DISP_GR_DGR = "DISP_GR_DGR"
COL_LW_ACT_GR   = "LW_ACT_SL_GR_DGR"
COL_BGT_SL_GR   = "BGT_SL_GR_DGR"
COL_CLR_MIN     = "CLR_MIN"
COL_CLR_MAX     = "CLR_MAX"
COL_DPN         = "ACS_D"
COL_DISP_Q      = "DISP_Q"

# Output columns
COL_SAL_D       = "ALC_D"
COL_SAL_PD      = "SAL_PD"
COL_SRC         = "SALE_COVER_SRC"


# ==========================================================================
# HELPERS — delegating to shared db_helpers
# ==========================================================================
_run = run_sql
_exists = table_exists
_col_exists = column_exists
_ensure_col = ensure_column


# ==========================================================================
# PRIMARY KEYS
# ==========================================================================
REQUIRED_PKS = {
    "Master_ALC_INPUT_ST_MASTER":  ["ST_CD"],
    "Master_ALC_INPUT_ST_MAJ_CAT": ["ST_CD", "MAJ_CAT"],
    "Master_ALC_INPUT_CO_MAJ_CAT": ["MAJ_CAT"],
}

def ensure_primary_keys(conn) -> List[str]:
    logs = []
    for tbl, pk_cols in REQUIRED_PKS.items():
        if not _exists(conn, tbl): continue
        has_pk = conn.execute(text(
            "SELECT COUNT(*) FROM sys.key_constraints WHERE type='PK' AND OBJECT_NAME(parent_object_id)=:t"
        ), {"t": tbl}).scalar() > 0
        if has_pk: continue
        pk_list = ", ".join(f"[{c}]" for c in pk_cols)
        try:
            for c in pk_cols:
                try: _run(conn, f"ALTER TABLE [{tbl}] ALTER COLUMN [{c}] NVARCHAR(255) NOT NULL")
                except Exception: pass
            _run(conn, f"ALTER TABLE [{tbl}] ADD CONSTRAINT [PK_{tbl}] PRIMARY KEY ({pk_list})")
            logs.append(f"Added PK ({', '.join(pk_cols)}) to {tbl}")
        except Exception as e:
            logs.append(f"PK failed for {tbl}: {str(e)[:80]}")
    return logs


# ==========================================================================
# STEP 1: CREATE CALC TABLE (CO base × stores)
# ==========================================================================
def _step_create_calc(conn, steps):
    """CO_MAJ_CAT × all stores → ARS_CALC_ST_MAJ_CAT (base layer).

    Every store gets the CO-level values as a starting point.
    If CO_MAJ_CAT is missing, falls back to copying ST_MAJ_CAT directly.
    """
    CO_MAJ  = TABLES["CO_MAJ"]
    ST_MAST = TABLES["ST_MAST"]
    ST_MAJ  = TABLES["ST_MAJ"]
    CALC    = TABLES["CALC"]

    has_co   = _exists(conn, CO_MAJ)
    has_st   = _exists(conn, ST_MAJ)
    has_mast = _exists(conn, ST_MAST)

    if not has_co and not has_st:
        steps.append({"step": "Create calc table", "detail": f"Neither {CO_MAJ} nor {ST_MAJ} found", "status": "skip"})
        return False

    _run(conn, f"IF OBJECT_ID('{CALC}','U') IS NOT NULL DROP TABLE [{CALC}]")

    if has_co and has_mast:
        # ── CO × stores: every store gets CO-level values ────────────
        co_cols = [r[0] for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"
        ), {"t": CO_MAJ}).fetchall()]

        sel_parts = ["ST.[ST_CD]"]
        for c in co_cols:
            if c.upper() not in ("ST_CD", "UPLOAD_DATETIME"):
                sel_parts.append(f"CO.[{c}]")

        _run(conn, f"""
            SELECT {', '.join(sel_parts)}
            INTO [{CALC}]
            FROM [{ST_MAST}] ST WITH (NOLOCK)
            CROSS JOIN [{CO_MAJ}] CO WITH (NOLOCK)
            WHERE ST.[ST_CD] IS NOT NULL
        """)
        cnt = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}]")).scalar()
        steps.append({"step": "Create calc (CO base)", "detail": f"{cnt} rows from {CO_MAJ} × {ST_MAST}", "status": "ok"})
    else:
        # Fallback: copy ST_MAJ_CAT directly
        _run(conn, f"SELECT * INTO [{CALC}] FROM [{ST_MAJ}] WITH (NOLOCK)")
        cnt = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}]")).scalar()
        steps.append({"step": "Create calc (ST only)", "detail": f"{cnt} rows from {ST_MAJ}", "status": "ok"})

    # Ensure output columns
    _ensure_col(conn, CALC, COL_SAL_D)
    _ensure_col(conn, CALC, COL_SAL_PD)
    _ensure_col(conn, CALC, COL_SRC, "NVARCHAR(50)")

    return True


# ==========================================================================
# STEP 1b: FILL GAPS — ensure every CO_MAJ_CAT × store exists in CALC
# ==========================================================================
def _step_fill_co_gaps(conn, steps):
    """Insert missing (ST_CD, MAJ_CAT) rows from CO_MAJ_CAT × ST_MASTER.

    After Step 1 (which may have used ST fallback), cross-check:
      for each store in ST_MASTER × each MAJ_CAT in CO_MAJ_CAT,
      if the combo is missing in CALC → insert with CO defaults.
    This guarantees complete coverage even if ST_MAJ_CAT had fewer MAJ_CATs.
    """
    CALC    = TABLES["CALC"]
    CO_MAJ  = TABLES["CO_MAJ"]
    ST_MAST = TABLES["ST_MAST"]

    if not _exists(conn, CO_MAJ) or not _exists(conn, ST_MAST):
        return

    try:
        # Get CALC columns + CO columns for building the INSERT
        calc_cols_list = [r[0] for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = :t ORDER BY ORDINAL_POSITION"
        ), {"t": CALC}).fetchall()]
        co_cols = {r[0].upper(): r[0] for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"
        ), {"t": CO_MAJ}).fetchall()}

        # Build SELECT: ST_CD from stores, CO values for matching cols, NULL for the rest
        ins_sel = []
        for c in calc_cols_list:
            cu = c.upper()
            if cu == "ST_CD":
                ins_sel.append("ST.[ST_CD]")
            elif cu == "MAJ_CAT" and "MAJ_CAT" in co_cols:
                ins_sel.append(f"CO.[{co_cols['MAJ_CAT']}]")
            elif cu in co_cols:
                ins_sel.append(f"CO.[{co_cols[cu]}]")
            else:
                ins_sel.append("NULL")

        before = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}]")).scalar()
        _run(conn, f"""
            INSERT INTO [{CALC}] ({', '.join(f'[{c}]' for c in calc_cols_list)})
            SELECT {', '.join(ins_sel)}
            FROM [{ST_MAST}] ST WITH (NOLOCK)
            CROSS JOIN [{CO_MAJ}] CO WITH (NOLOCK)
            WHERE ST.[ST_CD] IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM [{CALC}] C
                  WHERE C.[ST_CD] = ST.[ST_CD] AND C.[MAJ_CAT] = CO.[MAJ_CAT]
              )
        """)
        after = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}]")).scalar()
        gap_rows = after - before
        if gap_rows > 0:
            steps.append({"step": "Fill CO gaps", "detail": f"{gap_rows} missing (store × MAJ_CAT) rows added from {CO_MAJ}", "status": "ok"})
        else:
            steps.append({"step": "Fill CO gaps", "detail": "No gaps — all CO × store combos present", "status": "ok"})
    except Exception as e:
        steps.append({"step": "Fill CO gaps", "detail": str(e)[:150], "status": "error"})


# ==========================================================================
# STEP 2: OVERLAY ST_MAJ_CAT VALUES (ST overrides CO base)
# ==========================================================================
def _step_overlay_st_values(conn, steps):
    """Cascade: CO provides defaults (step 1) → ST overrides per-store.

    For ALL matching columns between ST_MAJ_CAT and CALC:
      - If ST has data (non-null, non-empty) → use ST value
      - If ST has no data → keep CO base value from step 1

    Example: CO has I_ROD=2 → all stores get 2.
             Store X has I_ROD=3 in ST → store X gets 3 (not 2).

    Also: inserts ST rows for (ST_CD, MAJ_CAT) combos not in CALC
    (MAJ_CATs that exist in ST but not in CO).
    """
    CALC   = TABLES["CALC"]
    ST_MAJ = TABLES["ST_MAJ"]

    if not _exists(conn, ST_MAJ):
        steps.append({"step": "Overlay ST_MAJ_CAT", "detail": f"{ST_MAJ} not found", "status": "skip"})
        return

    # ── Column discovery ─────────────────────────────────────────
    calc_cols = {r[0].upper(): r[0] for r in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"
    ), {"t": CALC}).fetchall()}

    st_cols = {r[0].upper(): r[0] for r in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"
    ), {"t": ST_MAJ}).fetchall()}

    skip = {"ST_CD", "MAJ_CAT", "UPLOAD_DATETIME",
            COL_SAL_D.upper(), COL_SAL_PD.upper(), COL_SRC.upper()}

    # ── Add columns that exist in ST but not yet in CALC ─────────
    added_cols = []
    for cu, c_actual in st_cols.items():
        if cu not in skip and cu not in calc_cols:
            _ensure_col(conn, CALC, c_actual)
            calc_cols[cu] = c_actual
            added_cols.append(c_actual)
    if added_cols:
        steps.append({"step": "Add ST columns", "detail": f"{len(added_cols)} cols: {', '.join(added_cols)}", "status": "ok"})

    # ── Insert ST-only rows (MAJ_CATs not in CO) ─────────────────
    try:
        before = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}]")).scalar()
        all_calc = [r[0] for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = :t ORDER BY ORDINAL_POSITION"
        ), {"t": CALC}).fetchall()]

        ins_sel = []
        for c in all_calc:
            cu = c.upper()
            ins_sel.append(f"S.[{st_cols[cu]}]" if cu in st_cols else "NULL")

        _run(conn, f"""
            INSERT INTO [{CALC}] ({', '.join(f'[{c}]' for c in all_calc)})
            SELECT {', '.join(ins_sel)}
            FROM [{ST_MAJ}] S WITH (NOLOCK)
            WHERE NOT EXISTS (
                SELECT 1 FROM [{CALC}] C
                WHERE C.[ST_CD] = S.[ST_CD] AND C.[MAJ_CAT] = S.[MAJ_CAT]
            )
        """)
        after = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}]")).scalar()
        new_rows = after - before
        if new_rows > 0:
            steps.append({"step": "Insert ST-only rows", "detail": f"{new_rows} rows (MAJ_CATs not in CO)", "status": "ok"})
    except Exception as e:
        steps.append({"step": "Insert ST-only rows", "detail": str(e)[:150], "status": "error"})

    # ── Overlay: UPDATE all matching columns with ST values ──────
    overlaid = []
    for cu, st_actual in st_cols.items():
        if cu in skip or cu not in calc_cols:
            continue
        calc_actual = calc_cols[cu]
        try:
            _run(conn, f"""
                UPDATE C SET C.[{calc_actual}] = S.[{st_actual}]
                FROM [{CALC}] C
                INNER JOIN [{ST_MAJ}] S WITH (NOLOCK)
                    ON C.[ST_CD] = S.[ST_CD] AND C.[MAJ_CAT] = S.[MAJ_CAT]
                WHERE S.[{st_actual}] IS NOT NULL
                  AND LTRIM(RTRIM(CAST(S.[{st_actual}] AS NVARCHAR(MAX)))) NOT IN ('', '0')
            """)
            overlaid.append(calc_actual)
        except Exception as e:
            steps.append({"step": f"Overlay {calc_actual}", "detail": str(e)[:100], "status": "error"})

    steps.append({"step": "Overlay ST_MAJ_CAT",
                  "detail": f"{len(overlaid)} cols overridden: {', '.join(overlaid)}", "status": "ok"})


# ==========================================================================
# STEP 3: APPLY DEFAULTS
# ==========================================================================
def _step_defaults(conn, steps):
    """Apply default values for blank/null columns."""
    CALC = TABLES["CALC"]
    applied = []

    # 1. LISTING: blank/Y/null → 1, N → 0
    if _col_exists(conn, CALC, COL_LISTING):
        try:
            _run(conn, f"""
                UPDATE [{CALC}] SET [{COL_LISTING}] =
                    CASE
                        WHEN [{COL_LISTING}] IS NULL OR LTRIM(RTRIM(CAST([{COL_LISTING}] AS NVARCHAR(10)))) = '' THEN 1
                        WHEN UPPER(LTRIM(RTRIM(CAST([{COL_LISTING}] AS NVARCHAR(10))))) = 'Y' THEN 1
                        WHEN UPPER(LTRIM(RTRIM(CAST([{COL_LISTING}] AS NVARCHAR(10))))) = 'N' THEN 0
                        WHEN ISNUMERIC(CAST([{COL_LISTING}] AS NVARCHAR(10))) = 1 THEN CAST([{COL_LISTING}] AS INT)
                        ELSE 1
                    END
            """)
            applied.append(f"{COL_LISTING}: blank/Y/null→1, N→0")
        except Exception as e:
            steps.append({"step": f"Default {COL_LISTING}", "detail": str(e)[:100], "status": "error"})

    # 2. I_ROD: null/0 → 1
    if _col_exists(conn, CALC, COL_I_ROD):
        try:
            _run(conn, f"UPDATE [{CALC}] SET [{COL_I_ROD}] = 1 WHERE [{COL_I_ROD}] IS NULL OR [{COL_I_ROD}] = 0")
            applied.append(f"{COL_I_ROD}: null/0→1")
        except Exception as e:
            logger.debug(f"Default {COL_I_ROD}: {e}")

    # 3. Growth rates: DISP_GR_DGR, LW_ACT_SL_GR_DGR, BGT_SL_GR_DGR → default 1 if null/0
    for col in [COL_DISP_GR_DGR, COL_LW_ACT_GR, COL_BGT_SL_GR]:
        if _col_exists(conn, CALC, col):
            try:
                _run(conn, f"UPDATE [{CALC}] SET [{col}] = 1 WHERE [{col}] IS NULL OR [{col}] = 0")
                applied.append(f"{col}: null/0→1")
            except Exception as e:
                logger.debug(f"Default {col}: {e}")

    steps.append({"step": "Apply defaults", "detail": "; ".join(applied), "status": "ok"})


# ==========================================================================
# STEP 4: CALCULATE ALC_D (Total Sale Days)
# ==========================================================================
def _step_sal_d(conn, steps):
    """ALC_D = INT_DAYS + PRD_DAYS + SL_CVR (priority: ST_MAJ > CO_MAJ > ST_MASTER)."""
    CALC    = TABLES["CALC"]
    ST_MAST = TABLES["ST_MAST"]
    CO_MAJ  = TABLES["CO_MAJ"]

    if not _exists(conn, ST_MAST):
        steps.append({"step": "ALC_D", "detail": f"{ST_MAST} not found", "status": "skip"})
        return

    _ensure_col(conn, CALC, COL_SAL_D)
    _ensure_col(conn, CALC, COL_SRC, "NVARCHAR(50)")

    # Priority 3: ST_MASTER (base for all)
    try:
        _run(conn, f"""
            UPDATE C SET C.[{COL_SRC}]='ST_MASTER',
                C.[{COL_SAL_D}] = ISNULL(S.[{COL_INT_DAYS}],0)+ISNULL(S.[{COL_PRD_DAYS}],0)+ISNULL(S.[{COL_SL_CVR}],0)
            FROM [{CALC}] C
            INNER JOIN [{ST_MAST}] S WITH (NOLOCK) ON C.[ST_CD]=S.[ST_CD]
        """)
        cnt = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}] WHERE [{COL_SAL_D}]>0")).scalar()
        steps.append({"step": "ALC_D (ST_MASTER)", "detail": f"{cnt} rows", "status": "ok"})
    except Exception as e:
        steps.append({"step": "ALC_D (ST_MASTER)", "detail": str(e)[:150], "status": "error"})
        return

    # Priority 2: CO_MAJ_CAT override
    if _exists(conn, CO_MAJ) and _col_exists(conn, CO_MAJ, COL_SL_CVR):
        try:
            _run(conn, f"""
                UPDATE C SET C.[{COL_SRC}]='CO_MAJ_CAT',
                    C.[{COL_SAL_D}] = ISNULL(S.[{COL_INT_DAYS}],0)+ISNULL(S.[{COL_PRD_DAYS}],0)+ISNULL(CO.[{COL_SL_CVR}],0)
                FROM [{CALC}] C
                INNER JOIN [{ST_MAST}] S WITH (NOLOCK) ON C.[ST_CD]=S.[ST_CD]
                INNER JOIN [{CO_MAJ}] CO WITH (NOLOCK) ON C.[MAJ_CAT]=CO.[MAJ_CAT]
                WHERE CO.[{COL_SL_CVR}] IS NOT NULL AND CO.[{COL_SL_CVR}] > 0
            """)
            cnt = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}] WHERE [{COL_SRC}]='CO_MAJ_CAT'")).scalar()
            steps.append({"step": "ALC_D (CO_MAJ_CAT)", "detail": f"Override {cnt} rows", "status": "ok"})
        except Exception as e:
            steps.append({"step": "ALC_D (CO_MAJ_CAT)", "detail": str(e)[:150], "status": "error"})

    # Priority 1: ST_MAJ_CAT own SL_CVR (highest priority)
    if _col_exists(conn, CALC, COL_SL_CVR):
        try:
            _run(conn, f"""
                UPDATE C SET C.[{COL_SRC}]='ST_MAJ_CAT',
                    C.[{COL_SAL_D}] = ISNULL(S.[{COL_INT_DAYS}],0)+ISNULL(S.[{COL_PRD_DAYS}],0)+ISNULL(C.[{COL_SL_CVR}],0)
                FROM [{CALC}] C
                INNER JOIN [{ST_MAST}] S WITH (NOLOCK) ON C.[ST_CD]=S.[ST_CD]
                WHERE C.[{COL_SL_CVR}] IS NOT NULL AND C.[{COL_SL_CVR}] > 0
            """)
            cnt = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}] WHERE [{COL_SRC}]='ST_MAJ_CAT'")).scalar()
            steps.append({"step": "ALC_D (ST_MAJ_CAT)", "detail": f"Override {cnt} rows", "status": "ok"})
        except Exception as e:
            steps.append({"step": "ALC_D (ST_MAJ_CAT)", "detail": str(e)[:150], "status": "error"})


# ==========================================================================
# STEP 5: CALCULATE SAL_PD (Per Day Sale)
# ==========================================================================
def _step_sal_pd(conn, steps):
    CALC = TABLES["CALC"]
    needed = [COL_CM_SAL_Q, COL_CM_REM_D, COL_NM_SAL_D, COL_NM_REM_D, COL_SAL_D]
    missing = [c for c in needed if not _col_exists(conn, CALC, c)]
    if missing:
        steps.append({"step": "SAL_PD", "detail": f"Missing: {missing}", "status": "skip"})
        return
    _ensure_col(conn, CALC, COL_SAL_PD)
    try:
        _run(conn, f"""
            UPDATE [{CALC}] SET [{COL_SAL_PD}] =
                CASE
                    WHEN ISNULL([{COL_CM_REM_D}],0)=0 THEN 0
                    WHEN [{COL_CM_REM_D}] >= ISNULL([{COL_SAL_D}],0) THEN
                        CAST([{COL_CM_SAL_Q}] AS FLOAT) / [{COL_CM_REM_D}]
                    WHEN ISNULL([{COL_SAL_D}],0)=0 THEN 0
                    ELSE
                        CASE WHEN ISNULL([{COL_NM_REM_D}],0)=0 THEN
                            CAST([{COL_CM_SAL_Q}] AS FLOAT) / [{COL_CM_REM_D}]
                        ELSE
                            (CAST([{COL_CM_SAL_Q}] AS FLOAT)
                             + (CAST([{COL_NM_SAL_D}] AS FLOAT) / [{COL_NM_REM_D}])
                               * ([{COL_SAL_D}] - [{COL_CM_REM_D}])
                            ) / [{COL_SAL_D}]
                        END
                END
        """)
        cnt = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}] WHERE [{COL_SAL_PD}]>0")).scalar()
        steps.append({"step": "SAL_PD", "detail": f"{cnt} rows calculated", "status": "ok"})
    except Exception as e:
        steps.append({"step": "SAL_PD", "detail": str(e)[:150], "status": "error"})


# ==========================================================================
# ARS_CALC_ST_ART — Article-level calc table
# Cascade: CO_ART × stores (base) → ST_ART overlay (same as MAJ_CAT)
# Sources: MASTER_ALC_INPUT_CO_ART, Master_ALC_INPUT_ST_ART, MASTER_GEN_ART_SALE
# ==========================================================================

ART_TABLES = {
    "ST_ART":   "Master_ALC_INPUT_ST_ART",
    "CO_ART":   "MASTER_ALC_INPUT_CO_ART",
    "ART_SALE": "MASTER_GEN_ART_SALE",
    "CALC_ART": "ARS_CALC_ST_ART",
}

# Columns to DROP from ART calc (not needed at article level)
_ART_DROP_COLS = {"CORE", "AUTO", "HH_ART"}

# Article key column: CO_ART uses "10_DIGIT" (= GEN_ART_NUMBER)
_ART_KEY_ALIASES = ["GEN_ART_NUMBER", "10_DIGIT", "ART_NUMBER", "ARTICLE_NUMBER"]


def _find_art_key(conn, table: str):
    """Return the column name in `table` that represents the article number."""
    cols = [r[0] for r in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"
    ), {"t": table}).fetchall()]
    cols_upper = {c.upper(): c for c in cols}
    for alias in _ART_KEY_ALIASES:
        if alias in cols_upper:
            return cols_upper[alias]
    return None


def _step_ensure_sale_maj_cat(conn, steps):
    """Ensure MAJ_CAT exists in MASTER_GEN_ART_SALE (populate from vw_master_product)."""
    SALE_T = ART_TABLES["ART_SALE"]
    if not _exists(conn, SALE_T):
        return

    sale_cols = {r[0].upper() for r in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"
    ), {"t": SALE_T}).fetchall()}

    if "MAJ_CAT" in sale_cols:
        return  # already present

    if not _exists(conn, "vw_master_product"):
        steps.append({"step": "SALE MAJ_CAT", "detail": "vw_master_product not found", "status": "skip"})
        return

    try:
        _ensure_col(conn, SALE_T, "MAJ_CAT", "NVARCHAR(200)")
        _run(conn, f"""
            UPDATE S SET S.[MAJ_CAT] = MP.[MAJ_CAT]
            FROM [{SALE_T}] S
            INNER JOIN (
                SELECT [ARTICLE_NUMBER], MIN([MAJ_CAT]) AS [MAJ_CAT]
                FROM [vw_master_product] WITH (NOLOCK)
                WHERE [MAJ_CAT] IS NOT NULL
                GROUP BY [ARTICLE_NUMBER]
            ) MP ON TRY_CAST(S.[GEN_ART_NUMBER] AS BIGINT) = TRY_CAST(MP.[ARTICLE_NUMBER] AS BIGINT)
            WHERE S.[MAJ_CAT] IS NULL
        """)
        filled = conn.execute(text(f"SELECT COUNT(*) FROM [{SALE_T}] WHERE [MAJ_CAT] IS NOT NULL")).scalar()
        steps.append({"step": "SALE MAJ_CAT", "detail": f"MAJ_CAT added to {SALE_T} from vw_master_product ({filled} rows)", "status": "ok"})
    except Exception as e:
        steps.append({"step": "SALE MAJ_CAT", "detail": str(e)[:150], "status": "error"})


def _step_create_calc_art(conn, steps):
    """Step A1: CO_ART × all stores → ARS_CALC_ST_ART (base layer).

    Every store gets CO-level article values as a starting point.
    Falls back to copying ST_ART directly if CO_ART is missing.
    Drops CORE, AUTO, HH_ART columns (not needed at article level).
    """
    CO_ART  = ART_TABLES["CO_ART"]
    ST_ART  = ART_TABLES["ST_ART"]
    ST_MAST = TABLES["ST_MAST"]
    CALC    = ART_TABLES["CALC_ART"]

    has_co   = _exists(conn, CO_ART)
    has_st   = _exists(conn, ST_ART)
    has_mast = _exists(conn, ST_MAST)

    if not has_co and not has_st:
        steps.append({"step": "Create ART calc", "detail": "Neither CO_ART nor ST_ART found", "status": "skip"})
        return False

    _run(conn, f"IF OBJECT_ID('{CALC}','U') IS NOT NULL DROP TABLE [{CALC}]")

    if has_co and has_mast:
        co_key = _find_art_key(conn, CO_ART)
        if not co_key:
            steps.append({"step": "Create ART calc", "detail": f"No article key in {CO_ART}", "status": "skip"})
            return False

        co_cols = [r[0] for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"
        ), {"t": CO_ART}).fetchall()]
        co_cols_upper = {c.upper(): c for c in co_cols}

        sel_parts = ["ST.[ST_CD]"]
        if "MAJ_CAT" in co_cols_upper:
            sel_parts.append(f"CO.[{co_cols_upper['MAJ_CAT']}] AS [MAJ_CAT]")
        sel_parts.append(f"TRY_CAST(CO.[{co_key}] AS BIGINT) AS [GEN_ART_NUMBER]")
        if "CLR" in co_cols_upper:
            sel_parts.append(f"CO.[{co_cols_upper['CLR']}] AS [CLR]")

        handled = {"ST_CD", "MAJ_CAT", co_key.upper(), "CLR", "UPLOAD_DATETIME"} | _ART_DROP_COLS
        for c in co_cols:
            if c.upper() not in handled:
                sel_parts.append(f"CO.[{c}]")

        _run(conn, f"""
            SELECT {', '.join(sel_parts)}
            INTO [{CALC}]
            FROM [{ST_MAST}] ST WITH (NOLOCK)
            CROSS JOIN [{CO_ART}] CO WITH (NOLOCK)
            WHERE ST.[ST_CD] IS NOT NULL
        """)
        cnt = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}]")).scalar()
        steps.append({"step": "Create ART calc (CO base)", "detail": f"{cnt} rows from {CO_ART} × {ST_MAST}", "status": "ok"})
    elif has_st:
        _run(conn, f"SELECT * INTO [{CALC}] FROM [{ST_ART}] WITH (NOLOCK)")
        # Drop unwanted columns
        for dc in _ART_DROP_COLS:
            if _col_exists(conn, CALC, dc):
                try:
                    _run(conn, f"ALTER TABLE [{CALC}] DROP COLUMN [{dc}]")
                except Exception:
                    pass
        cnt = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}]")).scalar()
        steps.append({"step": "Create ART calc (ST only)", "detail": f"{cnt} rows from {ST_ART}", "status": "ok"})
    else:
        steps.append({"step": "Create ART calc", "detail": "No valid source", "status": "skip"})
        return False

    _ensure_col(conn, CALC, "ALC_D")
    _ensure_col(conn, CALC, "SAL_PD")
    _ensure_col(conn, CALC, "SALE_COVER_SRC", "NVARCHAR(50)")

    return True


def _step_fill_co_art_gaps(conn, steps):
    """Step A1b: Ensure every CO_ART × store exists in ARS_CALC_ST_ART."""
    CALC    = ART_TABLES["CALC_ART"]
    CO_ART  = ART_TABLES["CO_ART"]
    ST_MAST = TABLES["ST_MAST"]

    if not _exists(conn, CO_ART) or not _exists(conn, ST_MAST):
        return

    co_key = _find_art_key(conn, CO_ART)
    if not co_key:
        return

    try:
        calc_cols_list = [r[0] for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = :t ORDER BY ORDINAL_POSITION"
        ), {"t": CALC}).fetchall()]

        co_cols = {r[0].upper(): r[0] for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"
        ), {"t": CO_ART}).fetchall()}

        ins_sel = []
        for c in calc_cols_list:
            cu = c.upper()
            if cu == "ST_CD":
                ins_sel.append("ST.[ST_CD]")
            elif cu == "GEN_ART_NUMBER":
                ins_sel.append(f"TRY_CAST(CO.[{co_key}] AS BIGINT)")
            elif cu in co_cols:
                ins_sel.append(f"CO.[{co_cols[cu]}]")
            else:
                ins_sel.append("NULL")

        # Build NOT EXISTS join — match on ST_CD + MAJ_CAT + GEN_ART_NUMBER [+ CLR]
        exist_parts = ["C.[ST_CD] = ST.[ST_CD]",
                       f"C.[GEN_ART_NUMBER] = TRY_CAST(CO.[{co_key}] AS BIGINT)"]
        if "MAJ_CAT" in co_cols and "MAJ_CAT" in {c.upper() for c in calc_cols_list}:
            exist_parts.append("C.[MAJ_CAT] = CO.[MAJ_CAT]")
        if "CLR" in co_cols and "CLR" in {c.upper() for c in calc_cols_list}:
            exist_parts.append("C.[CLR] = CO.[CLR]")
        exist_cond = " AND ".join(exist_parts)

        before = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}]")).scalar()
        _run(conn, f"""
            INSERT INTO [{CALC}] ({', '.join(f'[{c}]' for c in calc_cols_list)})
            SELECT {', '.join(ins_sel)}
            FROM [{ST_MAST}] ST WITH (NOLOCK)
            CROSS JOIN [{CO_ART}] CO WITH (NOLOCK)
            WHERE ST.[ST_CD] IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM [{CALC}] C WHERE {exist_cond})
        """)
        after = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}]")).scalar()
        gap_rows = after - before
        if gap_rows > 0:
            steps.append({"step": "Fill CO_ART gaps", "detail": f"{gap_rows} missing (store × article) rows added", "status": "ok"})
        else:
            steps.append({"step": "Fill CO_ART gaps", "detail": "No gaps — all CO_ART × store combos present", "status": "ok"})
    except Exception as e:
        steps.append({"step": "Fill CO_ART gaps", "detail": str(e)[:150], "status": "error"})


def _step_overlay_st_art(conn, steps):
    """Step A2: Overlay ST_ART values — ST overrides CO base where ST has data.

    Same cascade as MAJ_CAT: auto-detects ALL matching columns,
    inserts ST-only rows, overlays non-null/non-blank ST values.
    Skips CORE, AUTO, HH_ART columns.
    """
    CALC   = ART_TABLES["CALC_ART"]
    ST_ART = ART_TABLES["ST_ART"]

    if not _exists(conn, ST_ART):
        steps.append({"step": "Overlay ST_ART", "detail": f"{ST_ART} not found", "status": "skip"})
        return

    calc_cols = {r[0].upper(): r[0] for r in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"
    ), {"t": CALC}).fetchall()}

    st_cols = {r[0].upper(): r[0] for r in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"
    ), {"t": ST_ART}).fetchall()}

    st_art_key = _find_art_key(conn, ST_ART) or "GEN_ART_NUMBER"
    skip = {"ST_CD", "MAJ_CAT", "GEN_ART_NUMBER", st_art_key.upper(),
            "CLR", "UPLOAD_DATETIME", "ALC_D", "SAL_PD", "SALE_COVER_SRC"} | _ART_DROP_COLS

    # ── Add ST-only columns to CALC (excluding dropped cols) ─────
    added_cols = []
    for cu, c_actual in st_cols.items():
        if cu not in skip and cu not in calc_cols:
            _ensure_col(conn, CALC, c_actual)
            calc_cols[cu] = c_actual
            added_cols.append(c_actual)
    if added_cols:
        steps.append({"step": "Add ST_ART columns", "detail": f"{len(added_cols)} cols: {', '.join(added_cols)}", "status": "ok"})

    # ── Build join condition ──────────────────────────────────────
    join_parts = ["C.[ST_CD] = S.[ST_CD]"]
    if "MAJ_CAT" in st_cols and "MAJ_CAT" in calc_cols:
        join_parts.append("C.[MAJ_CAT] = S.[MAJ_CAT]")
    join_parts.append(f"C.[GEN_ART_NUMBER] = TRY_CAST(S.[{st_art_key}] AS BIGINT)")
    if "CLR" in st_cols and "CLR" in calc_cols:
        join_parts.append("C.[CLR] = S.[CLR]")
    join_cond = " AND ".join(join_parts)

    # ── Insert ST-only rows (articles not in CO) ─────────────────
    try:
        before = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}]")).scalar()
        all_calc = [r[0] for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = :t ORDER BY ORDINAL_POSITION"
        ), {"t": CALC}).fetchall()]

        ins_sel = []
        for c in all_calc:
            cu = c.upper()
            if cu == "GEN_ART_NUMBER" and st_art_key.upper() != "GEN_ART_NUMBER":
                ins_sel.append(f"TRY_CAST(S.[{st_art_key}] AS BIGINT)")
            elif cu in st_cols:
                ins_sel.append(f"S.[{st_cols[cu]}]")
            else:
                ins_sel.append("NULL")

        _run(conn, f"""
            INSERT INTO [{CALC}] ({', '.join(f'[{c}]' for c in all_calc)})
            SELECT {', '.join(ins_sel)}
            FROM [{ST_ART}] S WITH (NOLOCK)
            WHERE NOT EXISTS (SELECT 1 FROM [{CALC}] C WHERE {join_cond})
        """)
        after = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}]")).scalar()
        new_rows = after - before
        if new_rows > 0:
            steps.append({"step": "Insert ST_ART-only rows", "detail": f"{new_rows} rows (articles not in CO)", "status": "ok"})
    except Exception as e:
        steps.append({"step": "Insert ST_ART-only rows", "detail": str(e)[:150], "status": "error"})

    # ── Overlay: UPDATE all matching columns with ST values ──────
    overlaid = []
    for cu, st_actual in st_cols.items():
        if cu in skip or cu not in calc_cols:
            continue
        calc_actual = calc_cols[cu]
        try:
            _run(conn, f"""
                UPDATE C SET C.[{calc_actual}] = S.[{st_actual}]
                FROM [{CALC}] C
                INNER JOIN [{ST_ART}] S WITH (NOLOCK) ON {join_cond}
                WHERE S.[{st_actual}] IS NOT NULL
                  AND LTRIM(RTRIM(CAST(S.[{st_actual}] AS NVARCHAR(MAX)))) NOT IN ('', '0')
            """)
            overlaid.append(calc_actual)
        except Exception as e:
            steps.append({"step": f"Overlay ART {calc_actual}", "detail": str(e)[:100], "status": "error"})

    steps.append({"step": "Overlay ST_ART",
                  "detail": f"{len(overlaid)} cols overridden: {', '.join(overlaid)}", "status": "ok"})


def _step_art_defaults(conn, steps):
    """Apply default values for ARS_CALC_ST_ART (mirrors MAJ_CAT defaults + FOCUS cols)."""
    CALC = ART_TABLES["CALC_ART"]
    applied = []

    # 1. LISTING: blank/Y/null → 1, N → 0
    if _col_exists(conn, CALC, COL_LISTING):
        try:
            _run(conn, f"""
                UPDATE [{CALC}] SET [{COL_LISTING}] =
                    CASE
                        WHEN [{COL_LISTING}] IS NULL OR LTRIM(RTRIM(CAST([{COL_LISTING}] AS NVARCHAR(10)))) = '' THEN 1
                        WHEN UPPER(LTRIM(RTRIM(CAST([{COL_LISTING}] AS NVARCHAR(10))))) = 'Y' THEN 1
                        WHEN UPPER(LTRIM(RTRIM(CAST([{COL_LISTING}] AS NVARCHAR(10))))) = 'N' THEN 0
                        WHEN ISNUMERIC(CAST([{COL_LISTING}] AS NVARCHAR(10))) = 1 THEN CAST([{COL_LISTING}] AS INT)
                        ELSE 1
                    END
            """)
            applied.append(f"{COL_LISTING}: blank/Y/null→1, N→0")
        except Exception as e:
            steps.append({"step": f"ART Default {COL_LISTING}", "detail": str(e)[:100], "status": "error"})

    # 2. I_ROD: null/0 → 1
    if _col_exists(conn, CALC, COL_I_ROD):
        try:
            _run(conn, f"UPDATE [{CALC}] SET [{COL_I_ROD}] = 1 WHERE [{COL_I_ROD}] IS NULL OR [{COL_I_ROD}] = 0")
            applied.append(f"{COL_I_ROD}: null/0→1")
        except Exception as e:
            logger.debug(f"ART Default {COL_I_ROD}: {e}")

    # 3. Growth rates: null/0 → 1
    for col in [COL_DISP_GR_DGR, COL_LW_ACT_GR, COL_BGT_SL_GR]:
        if _col_exists(conn, CALC, col):
            try:
                _run(conn, f"UPDATE [{CALC}] SET [{col}] = 1 WHERE [{col}] IS NULL OR [{col}] = 0")
                applied.append(f"{col}: null/0→1")
            except Exception as e:
                logger.debug(f"ART Default {col}: {e}")

    # 4. MANUAL_DENSITY: ≤0/null → 0
    if _col_exists(conn, CALC, COL_MANUAL_DENSITY):
        try:
            _run(conn, f"UPDATE [{CALC}] SET [{COL_MANUAL_DENSITY}] = 0 WHERE [{COL_MANUAL_DENSITY}] IS NULL OR [{COL_MANUAL_DENSITY}] <= 0")
            applied.append(f"{COL_MANUAL_DENSITY}: ≤0/null→0")
        except Exception as e:
            logger.debug(f"ART Default {COL_MANUAL_DENSITY}: {e}")

    # 5. ACS_D override: if MANUAL_DENSITY > 0, use it as ACS_D (article level)
    if _col_exists(conn, CALC, COL_MANUAL_DENSITY) and _col_exists(conn, CALC, COL_DPN):
        try:
            _run(conn, f"""
                UPDATE [{CALC}] SET [{COL_DPN}] = [{COL_MANUAL_DENSITY}]
                WHERE ISNULL(TRY_CAST([{COL_MANUAL_DENSITY}] AS FLOAT), 0) > 0
            """)
            cnt = conn.execute(text(
                f"SELECT COUNT(*) FROM [{CALC}] WHERE ISNULL(TRY_CAST([{COL_MANUAL_DENSITY}] AS FLOAT), 0) > 0"
            )).scalar()
            applied.append(f"ACS_D overridden by {COL_MANUAL_DENSITY} for {cnt} rows")
        except Exception as e:
            logger.debug(f"ART ACS_D override by {COL_MANUAL_DENSITY}: {e}")

    # 6. FOCUS_W_CAP / FOCUS_WO_CAP: Y → 1, else → 0
    for col in ("FOCUS_W_CAP", "FOCUS_WO_CAP"):
        if _col_exists(conn, CALC, col):
            try:
                _run(conn, f"""
                    UPDATE [{CALC}] SET [{col}] =
                        CASE
                            WHEN UPPER(LTRIM(RTRIM(CAST([{col}] AS NVARCHAR(10))))) = 'Y' THEN 1
                            ELSE 0
                        END
                """)
                applied.append(f"{col}: Y→1, else→0")
            except Exception as e:
                logger.debug(f"ART Default {col}: {e}")

    steps.append({"step": "ART defaults", "detail": "; ".join(applied), "status": "ok"})


def _step_art_sal_d(conn, steps):
    """Step A3: ALC_D for article level — from ARS_CALC_ST_MAJ_CAT (already cascaded).

    ART table has no SL_CVR column. ALC_D is pulled directly from
    ARS_CALC_ST_MAJ_CAT which already has the correct cascaded value.
    """
    CALC  = ART_TABLES["CALC_ART"]
    MAJ_T = TABLES["CALC"]   # ARS_CALC_ST_MAJ_CAT

    if not _col_exists(conn, CALC, "ST_CD") or not _col_exists(conn, CALC, "MAJ_CAT"):
        steps.append({"step": "ART ALC_D", "detail": "ST_CD or MAJ_CAT missing in ART calc", "status": "skip"})
        return
    if not _exists(conn, MAJ_T) or not _col_exists(conn, MAJ_T, COL_SAL_D):
        steps.append({"step": "ART ALC_D", "detail": f"{MAJ_T} or ALC_D not found", "status": "skip"})
        return

    _ensure_col(conn, CALC, "ALC_D")
    _ensure_col(conn, CALC, "SALE_COVER_SRC", "NVARCHAR(50)")

    try:
        _run(conn, f"""
            UPDATE C SET C.[SALE_COVER_SRC] = MJ.[{COL_SRC}],
                C.[ALC_D] = MJ.[{COL_SAL_D}]
            FROM [{CALC}] C
            INNER JOIN [{MAJ_T}] MJ WITH (NOLOCK)
                ON C.[ST_CD] = MJ.[ST_CD] AND C.[MAJ_CAT] = MJ.[MAJ_CAT]
            WHERE MJ.[{COL_SAL_D}] IS NOT NULL AND MJ.[{COL_SAL_D}] > 0
        """)
        cnt = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}] WHERE [ALC_D]>0")).scalar()
        steps.append({"step": "ART ALC_D", "detail": f"{cnt} rows from {MAJ_T} (ST_CD+MAJ_CAT)", "status": "ok"})
    except Exception as e:
        steps.append({"step": "ART ALC_D", "detail": str(e)[:150], "status": "error"})


def _step_art_sal_pd(conn, steps):
    """Step A4: SAL_PD for article level — joined from MASTER_GEN_ART_SALE (separate source).

    Formula mirrors MAJ_CAT SAL_PD. CM_REM_D / NM_REM_D come from ST_MAJ_CAT
    (via left join on ST_CD+MAJ_CAT), CM_SAL_Q / NM_SAL_Q come from ART_SALE
    (via join on ST_CD+GEN_ART_NUMBER[+CLR]).
    """
    CALC     = ART_TABLES["CALC_ART"]
    ART_SALE = ART_TABLES["ART_SALE"]
    ST_MAJ   = TABLES["ST_MAJ"]

    _ensure_col(conn, CALC, "SAL_PD")

    if not _exists(conn, ART_SALE):
        # Fallback: use same formula as MAJ_CAT if CM_SAL_Q columns exist in CALC
        needed = [COL_CM_SAL_Q, COL_CM_REM_D, COL_NM_SAL_D, COL_NM_REM_D, "ALC_D"]
        missing = [c for c in needed if not _col_exists(conn, CALC, c)]
        if missing:
            steps.append({"step": "ART SAL_PD", "detail": f"{ART_SALE} not found, fallback missing: {missing}", "status": "skip"})
            return
        try:
            _run(conn, f"""
                UPDATE [{CALC}] SET [SAL_PD] =
                    CASE
                        WHEN ISNULL([{COL_CM_REM_D}],0)=0 THEN 0
                        WHEN [{COL_CM_REM_D}] >= ISNULL([ALC_D],0) THEN
                            CAST([{COL_CM_SAL_Q}] AS FLOAT) / [{COL_CM_REM_D}]
                        WHEN ISNULL([ALC_D],0)=0 THEN 0
                        ELSE
                            CASE WHEN ISNULL([{COL_NM_REM_D}],0)=0 THEN
                                CAST([{COL_CM_SAL_Q}] AS FLOAT) / [{COL_CM_REM_D}]
                            ELSE
                                (CAST([{COL_CM_SAL_Q}] AS FLOAT)
                                 + (CAST([{COL_NM_SAL_D}] AS FLOAT) / [{COL_NM_REM_D}])
                                   * ([ALC_D] - [{COL_CM_REM_D}])
                                ) / [ALC_D]
                            END
                    END
            """)
            cnt = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}] WHERE [SAL_PD]>0")).scalar()
            steps.append({"step": "ART SAL_PD (fallback)", "detail": f"{cnt} rows", "status": "ok"})
        except Exception as e:
            steps.append({"step": "ART SAL_PD (fallback)", "detail": str(e)[:150], "status": "error"})
        return

    # Join ART_SALE for CM_SAL_Q/NM_SAL_Q and ST_MAJ_CAT for CM_REM_D/NM_REM_D
    if not _exists(conn, ART_SALE):
        steps.append({"step": "ART SAL_PD", "detail": f"{ART_SALE} not found", "status": "skip"})
        return

    sale_cols = {r[0].upper() for r in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"
    ), {"t": ART_SALE}).fetchall()}

    # ART_SALE must have ST_CD + GEN_ART_NUMBER + CM_SAL_Q
    required_sale = [COL_CM_SAL_Q.upper()]
    if not all(c in sale_cols for c in required_sale):
        steps.append({"step": "ART SAL_PD", "detail": f"{ART_SALE} missing {required_sale}", "status": "skip"})
        return

    # Build ART_SALE join
    sale_join_parts = []
    if "ST_CD" in sale_cols and _col_exists(conn, CALC, "ST_CD"):
        sale_join_parts.append("C.[ST_CD] = SA.[ST_CD]")
    if "GEN_ART_NUMBER" in sale_cols and _col_exists(conn, CALC, "GEN_ART_NUMBER"):
        sale_join_parts.append("C.[GEN_ART_NUMBER] = SA.[GEN_ART_NUMBER]")
    if "CLR" in sale_cols and _col_exists(conn, CALC, "CLR"):
        sale_join_parts.append("C.[CLR] = SA.[CLR]")
    if not sale_join_parts:
        steps.append({"step": "ART SAL_PD", "detail": "No join keys for ART_SALE", "status": "skip"})
        return
    sale_join = " AND ".join(sale_join_parts)

    # Build ST_MAJ_CAT join for REM_D values (may not be available)
    has_maj = _exists(conn, ST_MAJ)
    cm_rem_expr = f"ISNULL(MJ.[{COL_CM_REM_D}], 0)" if has_maj else "0"
    nm_rem_expr = f"ISNULL(MJ.[{COL_NM_REM_D}], 0)" if has_maj else "0"
    maj_join_clause = f"LEFT JOIN [{ST_MAJ}] MJ WITH (NOLOCK) ON C.[ST_CD] = MJ.[ST_CD] AND C.[MAJ_CAT] = MJ.[MAJ_CAT]" if has_maj else ""

    cm_sal = COL_CM_SAL_Q if COL_CM_SAL_Q.upper() in sale_cols else None
    nm_sal = COL_NM_SAL_D if COL_NM_SAL_D.upper() in sale_cols else None
    nm_sal_expr = f"SA.[{nm_sal}]" if nm_sal else f"SA.[{cm_sal}]"

    try:
        _run(conn, f"""
            UPDATE C SET C.[SAL_PD] =
                CASE
                    WHEN {cm_rem_expr}=0 THEN 0
                    WHEN {cm_rem_expr} >= ISNULL(C.[ALC_D],0) THEN
                        CAST(SA.[{cm_sal}] AS FLOAT) / {cm_rem_expr}
                    WHEN ISNULL(C.[ALC_D],0)=0 THEN 0
                    ELSE
                        CASE WHEN {nm_rem_expr}=0 THEN
                            CAST(SA.[{cm_sal}] AS FLOAT) / {cm_rem_expr}
                        ELSE
                            (CAST(SA.[{cm_sal}] AS FLOAT)
                             + (CAST({nm_sal_expr} AS FLOAT) / {nm_rem_expr})
                               * (C.[ALC_D] - {cm_rem_expr})
                            ) / C.[ALC_D]
                        END
                END
            FROM [{CALC}] C
            INNER JOIN [{ART_SALE}] SA WITH (NOLOCK) ON {sale_join}
            {maj_join_clause}
        """)
        cnt = conn.execute(text(f"SELECT COUNT(*) FROM [{CALC}] WHERE [SAL_PD]>0")).scalar()
        detail = f"{cnt} rows from {ART_SALE}"
        if has_maj:
            detail += f" (REM_D from {ST_MAJ})"
        steps.append({"step": "ART SAL_PD", "detail": detail, "status": "ok"})
    except Exception as e:
        steps.append({"step": "ART SAL_PD", "detail": str(e)[:150], "status": "error"})


# ==========================================================================
# STEP M: SAL_PD directly on MASTER_GEN_ART_SALE
# ==========================================================================
# Rationale: MASTER_GEN_ART_SALE carries the full planned-sales universe
# (~21L rows) while ARS_CALC_ST_ART only covers the ST_ART master sample.
# Listing's AUTO_GEN_ART_SALE needs option-level coverage, so compute
# per-day-sale in place on the master table and have listing join it.
#
# Formula mirrors MAJ_CAT SAL_PD:
#   - CM_SAL_Q / NM_SAL_Q  → from MASTER_GEN_ART_SALE (row itself)
#   - CM_REM_D / NM_REM_D / ALC_D → from ARS_CALC_ST_MAJ_CAT (ST_CD + MAJ_CAT)
# ==========================================================================
def _step_master_sale_sal_pd(conn, steps):
    SALE_T = "MASTER_GEN_ART_SALE"
    MAJ_T  = TABLES["CALC"]   # ARS_CALC_ST_MAJ_CAT

    if not _exists(conn, SALE_T):
        steps.append({"step": "MASTER SAL_PD", "detail": f"{SALE_T} not found", "status": "skip"})
        return
    if not _exists(conn, MAJ_T):
        steps.append({"step": "MASTER SAL_PD", "detail": f"{MAJ_T} not found", "status": "skip"})
        return

    sale_cols = {
        r[0].upper() for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME=:t"
        ), {"t": SALE_T}).fetchall()
    }
    needed_sale = {COL_CM_SAL_Q.upper(), COL_NM_SAL_D.upper(), "ST_CD", "MAJ_CAT"}
    missing = needed_sale - sale_cols
    if missing:
        steps.append({"step": "MASTER SAL_PD", "detail": f"{SALE_T} missing: {missing}", "status": "skip"})
        return

    maj_cols = {c.upper() for c in [COL_CM_REM_D, COL_NM_REM_D, COL_SAL_D] if _col_exists(conn, MAJ_T, c)}
    if not {COL_CM_REM_D.upper(), COL_SAL_D.upper()}.issubset(maj_cols):
        steps.append({"step": "MASTER SAL_PD", "detail": f"{MAJ_T} missing CM_REM_D or ALC_D", "status": "skip"})
        return

    _ensure_col(conn, SALE_T, COL_SAL_PD)

    try:
        _run(conn, f"""
            UPDATE S SET S.[{COL_SAL_PD}] =
                CASE
                    WHEN ISNULL(MJ.[{COL_CM_REM_D}],0)=0 THEN 0
                    WHEN MJ.[{COL_CM_REM_D}] >= ISNULL(MJ.[{COL_SAL_D}],0) THEN
                        CAST(S.[{COL_CM_SAL_Q}] AS FLOAT) / MJ.[{COL_CM_REM_D}]
                    WHEN ISNULL(MJ.[{COL_SAL_D}],0)=0 THEN 0
                    ELSE
                        CASE WHEN ISNULL(MJ.[{COL_NM_REM_D}],0)=0 THEN
                            CAST(S.[{COL_CM_SAL_Q}] AS FLOAT) / MJ.[{COL_CM_REM_D}]
                        ELSE
                            (CAST(S.[{COL_CM_SAL_Q}] AS FLOAT)
                             + (CAST(S.[{COL_NM_SAL_D}] AS FLOAT) / MJ.[{COL_NM_REM_D}])
                               * (MJ.[{COL_SAL_D}] - MJ.[{COL_CM_REM_D}])
                            ) / MJ.[{COL_SAL_D}]
                        END
                END
            FROM [{SALE_T}] S
            INNER JOIN [{MAJ_T}] MJ WITH (NOLOCK)
                ON S.[ST_CD] = MJ.[ST_CD] AND S.[MAJ_CAT] = MJ.[MAJ_CAT]
        """)
        cnt = conn.execute(text(f"SELECT COUNT(*) FROM [{SALE_T}] WHERE [{COL_SAL_PD}]>0")).scalar()
        steps.append({"step": "MASTER SAL_PD", "detail": f"{cnt} rows in {SALE_T}", "status": "ok"})
    except Exception as e:
        steps.append({"step": "MASTER SAL_PD", "detail": str(e)[:150], "status": "error"})


# ==========================================================================
# MAIN: Run all pre-grid calculations
# ==========================================================================
def calculate_per_day_sale(conn) -> List[Dict[str, Any]]:
    """
    Full pre-grid calculation pipeline:
      MAJ_CAT level:
        1. Copy Master → ARS_CALC_ST_MAJ_CAT
        2. Merge CO_MAJ_CAT values
        3. Apply defaults
        4. ALC_D (total sale days)
        5. SAL_PD (per day sale)
      MASTER_GEN_ART_SALE:
        M. SAL_PD computed in place (~21L rows — full option coverage
           for listing AUTO_GEN_ART_SALE; REM_D/ALC_D from MAJ_CAT calc)
      ART level (mirrors MAJ_CAT flow):
        A1. Copy ST_ART → ARS_CALC_ST_ART (if ST_ART empty, cross-join stores × CO_ART)
        A2. Merge CO_ART values (CO overrides ST where CO has data)
        A3. ALC_D (from ST_MASTER)
        A4. SAL_PD (JOIN MASTER_GEN_ART_SALE for CM_SAL_Q/NM_SAL_Q + ST_MAJ_CAT for CM_REM_D/NM_REM_D)
    """
    steps = []

    # ── Migration: rename MANUAL_MBQ → MANUAL_DENSITY in ART input tables only ──
    # MANUAL_DENSITY concept only applies at article level (not MAJ_CAT).
    # MAJ_CAT tables: drop column if exists (no longer used).
    # ART tables: rename MANUAL_MBQ → MANUAL_DENSITY.
    for tbl in ["Master_ALC_INPUT_CO_MAJ_CAT", "Master_ALC_INPUT_ST_MAJ_CAT"]:
        if _exists(conn, tbl) and _col_exists(conn, tbl, "MANUAL_MBQ"):
            try:
                _run(conn, f"ALTER TABLE [{tbl}] DROP COLUMN [MANUAL_MBQ]")
                steps.append({"step": "Drop column", "detail": f"{tbl}: MANUAL_MBQ dropped (not used at MAJ_CAT level)", "status": "ok"})
            except Exception:
                pass
    for tbl in ["MASTER_ALC_INPUT_CO_ART", "Master_ALC_INPUT_ST_ART"]:
        if _exists(conn, tbl) and _col_exists(conn, tbl, "MANUAL_MBQ"):
            try:
                _run(conn, f"EXEC sp_rename '[{tbl}].[MANUAL_MBQ]', 'MANUAL_DENSITY', 'COLUMN'")
                steps.append({"step": "Migrate column", "detail": f"{tbl}: MANUAL_MBQ → MANUAL_DENSITY", "status": "ok"})
            except Exception:
                pass

    # Ensure PKs
    for msg in ensure_primary_keys(conn):
        steps.append({"step": "Ensure PK", "detail": msg, "status": "ok"})

    # ── MAJ_CAT level ──
    if not _step_create_calc(conn, steps):
        return steps
    _step_fill_co_gaps(conn, steps)
    _step_overlay_st_values(conn, steps)
    _step_defaults(conn, steps)
    _step_sal_d(conn, steps)
    _step_sal_pd(conn, steps)

    # ── MASTER_GEN_ART_SALE: ensure MAJ_CAT + in-place SAL_PD ──
    _step_ensure_sale_maj_cat(conn, steps)
    _step_master_sale_sal_pd(conn, steps)

    # ── ART level (cascade: CO base → fill gaps → ST overlay) ──
    if _step_create_calc_art(conn, steps):
        _step_fill_co_art_gaps(conn, steps)
        _step_overlay_st_art(conn, steps)
        _step_art_defaults(conn, steps)
        _step_art_sal_d(conn, steps)
        _step_art_sal_pd(conn, steps)

    return steps
