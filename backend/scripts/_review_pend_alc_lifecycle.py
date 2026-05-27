"""One-off diagnostic for the rule_ars review of ARS_PEND_ALC lifecycle.

Read-only. NO mutations. Uses the project's SQLAlchemy data engine
(local HOPC560 SQL Server).

Sections:
  S1  Inventory of relevant tables + columns
  S2  STK_TTL drift across grid tables (claim 5)
  S3  PEND-only rows missing from MSA_GEN/VAR_ART (claim 1)
  S4  PEND-only rows missing from grid tables (claim 2)
  S5  Revert audit: have any ops been reverted, and are they sane?
  S6  Hold tracking — HOLD_REM totals vs ARS_NL_TBL_HOLD_TRACKING
"""
from __future__ import annotations
import sys
from sqlalchemy import text
from app.database.session import get_data_engine


def section(t):
    print()
    print("=" * 100)
    print(t)
    print("=" * 100)


def _table_exists(conn, name):
    return (conn.execute(text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :t"
    ), {"t": name}).scalar() or 0) > 0


def _col_exists(conn, table, col):
    return (conn.execute(text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME = :t AND COLUMN_NAME = :c"
    ), {"t": table, "c": col}).scalar() or 0) > 0


def _cols(conn, table):
    return [r[0] for r in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME = :t ORDER BY ORDINAL_POSITION"
    ), {"t": table}).fetchall()]


def main():
    eng = get_data_engine()
    with eng.connect() as conn:

        # ------------------------------------------------------------------
        section("S1  Inventory of relevant tables")
        # ------------------------------------------------------------------
        targets = [
            "ARS_PEND_ALC",
            "ARS_PEND_ALC_OPERATIONS",
            "ARS_NL_TBL_HOLD_TRACKING",
            "ARS_MSA_TOTAL",
            "ARS_MSA_GEN_ART",
            "ARS_MSA_VAR_ART",
            "ARS_GRID_MJ",
            "ARS_GRID_MJ_VAR_ART",
            "ARS_GRID_MJ_GEN_ART",
            "ARS_GRID_MJ_CLR",
            "ARS_GRID_MJ_FAB",
            "ARS_GRID_MJ_RNG_SEG",
            "ARS_GRID_MJ_MERGE_RNG_SEG",
            "ARS_GRID_MJ_MACRO_MVGR",
            "ARS_GRID_MJ_MICRO_MVGR",
            "ARS_GRID_MJ_M_VND_CD",
            "ARS_STORE_SLOC_SETTINGS",
            "ARS_GRID_BUILDER",
            "ET_STORE_STOCK",
            "vw_master_product",
        ]
        for t in targets:
            exists = _table_exists(conn, t)
            n = None
            if exists:
                try:
                    n = conn.execute(text(f"SELECT COUNT(*) FROM [{t}] WITH (NOLOCK)")).scalar()
                except Exception as e:
                    n = f"err: {str(e)[:60]}"
            print(f"  {t:32s}  exists={exists}  rows={n}")

        # PEND_ALC totals
        if _table_exists(conn, "ARS_PEND_ALC"):
            r = conn.execute(text("""
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN IS_CLOSED=0 THEN 1 ELSE 0 END) AS open_rows,
                    SUM(CASE WHEN IS_CLOSED=0 THEN PEND_QTY ELSE 0 END) AS open_qty,
                    SUM(CASE WHEN IS_CLOSED=1 THEN 1 ELSE 0 END) AS closed_rows,
                    COUNT(DISTINCT SESSION_ID) AS sessions,
                    MIN(APPROVED_AT) AS first_at,
                    MAX(APPROVED_AT) AS last_at
                FROM ARS_PEND_ALC WITH (NOLOCK)
            """)).fetchone()
            print()
            print(f"  ARS_PEND_ALC summary:")
            print(f"    total_rows   = {r[0]}")
            print(f"    open_rows    = {r[1]}    open_qty  = {(r[2] or 0):,.0f}")
            print(f"    closed_rows  = {r[3]}")
            print(f"    sessions     = {r[4]}    first={r[5]}  last={r[6]}")

        # ------------------------------------------------------------------
        section("S2  STK_TTL drift across grid tables")
        # ------------------------------------------------------------------
        grids = [t for t in targets if t.startswith("ARS_GRID_MJ") and _table_exists(conn, t)]
        print(f"  {'grid':35s} {'rows':>10s} {'sum_STK_TTL':>18s} "
              f"{'sum_PEND_ALC':>16s}  {'distinct_(WERKS,MAJ_CAT)':>26s}")
        grid_totals = []
        for g in grids:
            cols = _cols(conn, g)
            has_stk  = "STK_TTL" in cols
            has_pend = "PEND_ALC" in cols
            has_w    = "WERKS" in cols
            has_mj   = "MAJ_CAT" in cols
            rc = conn.execute(text(f"SELECT COUNT(*) FROM [{g}] WITH (NOLOCK)")).scalar() or 0
            stk = (conn.execute(text(
                f"SELECT SUM(TRY_CAST(STK_TTL AS FLOAT)) FROM [{g}] WITH (NOLOCK)"
            )).scalar() if has_stk else None) or 0.0
            pend = (conn.execute(text(
                f"SELECT SUM(TRY_CAST(PEND_ALC AS FLOAT)) FROM [{g}] WITH (NOLOCK)"
            )).scalar() if has_pend else None) or 0.0
            distinct_wm = None
            if has_w and has_mj:
                distinct_wm = conn.execute(text(
                    f"SELECT COUNT(*) FROM (SELECT DISTINCT WERKS, MAJ_CAT FROM [{g}] WITH (NOLOCK)) z"
                )).scalar() or 0
            print(f"  {g:35s} {rc:>10,} {stk:>18,.2f} {pend:>16,.2f}  {(distinct_wm or '-'):>26}")
            grid_totals.append((g, rc, stk, pend, distinct_wm))

        # Per (WERKS,MAJ_CAT) STK_TTL across MJ-grain grids (those whose hier = MAJ_CAT only,
        # or MAJ_CAT + attribute which can be rolled up). Compare totals.
        print()
        print("  Pairwise STK_TTL totals per (WERKS,MAJ_CAT) — drift vs ARS_GRID_MJ baseline:")
        if "ARS_GRID_MJ" in [g[0] for g in grid_totals] and _col_exists(conn, "ARS_GRID_MJ", "STK_TTL"):
            base_total = conn.execute(text(
                "SELECT SUM(TRY_CAST(STK_TTL AS FLOAT)) FROM ARS_GRID_MJ WITH (NOLOCK)"
            )).scalar() or 0.0
            print(f"    ARS_GRID_MJ baseline = {base_total:,.2f}")
            for g, rc, stk, pend, dwm in grid_totals:
                if g == "ARS_GRID_MJ":
                    continue
                drift = stk - base_total
                pct = (100.0 * drift / (base_total or 1))
                flag = " <-- drift" if abs(pct) > 0.001 else ""
                print(f"    {g:35s} sum={stk:>18,.2f}  drift={drift:>+14,.2f}  ({pct:+.2f}%){flag}")

        # ------------------------------------------------------------------
        section("S3  PEND-only rows missing from MSA outputs (claim 1)")
        # ------------------------------------------------------------------
        if (_table_exists(conn, "ARS_PEND_ALC")
                and _table_exists(conn, "ARS_MSA_GEN_ART")
                and _table_exists(conn, "ARS_MSA_VAR_ART")
                and _table_exists(conn, "ARS_MSA_TOTAL")):
            total_art_col = "ARTICLE_NUMBER" if _col_exists(conn, "ARS_MSA_TOTAL", "ARTICLE_NUMBER") else "VAR_ART"
            var_art_col   = "ARTICLE_NUMBER" if _col_exists(conn, "ARS_MSA_VAR_ART", "ARTICLE_NUMBER") else "VAR_ART"

            r = conn.execute(text(f"""
                SELECT COUNT(*)                AS pend_rows_missing_from_total,
                       SUM(P.PEND_QTY)         AS pend_qty_invisible_to_msa
                FROM ARS_PEND_ALC P WITH (NOLOCK)
                LEFT JOIN ARS_MSA_TOTAL T WITH (NOLOCK)
                  ON T.RDC = P.RDC
                 AND T.[{total_art_col}] = P.ARTICLE_NUMBER
                WHERE P.IS_CLOSED = 0
                  AND P.PEND_QTY > 0
                  AND T.RDC IS NULL
            """)).fetchone()
            print(f"  MSA_TOTAL missing-row count  = {r[0]}")
            print(f"  MSA_TOTAL invisible PEND_QTY = {(r[1] or 0):,.2f}")

            r = conn.execute(text(f"""
                SELECT COUNT(*)         AS pend_rows_missing_from_var,
                       SUM(P.PEND_QTY)  AS qty
                FROM ARS_PEND_ALC P WITH (NOLOCK)
                LEFT JOIN ARS_MSA_VAR_ART V WITH (NOLOCK)
                  ON V.RDC = P.RDC
                 AND V.[{var_art_col}] = P.ARTICLE_NUMBER
                WHERE P.IS_CLOSED = 0 AND P.PEND_QTY > 0 AND V.RDC IS NULL
            """)).fetchone()
            print(f"  MSA_VAR_ART missing-row count  = {r[0]}")
            print(f"  MSA_VAR_ART invisible PEND_QTY = {(r[1] or 0):,.2f}")

            r = conn.execute(text(f"""
                SELECT COUNT(*) AS missing_rows,
                       SUM(P.PEND_QTY) AS qty
                FROM (
                    SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, SUM(PEND_QTY) AS PEND_QTY
                    FROM ARS_PEND_ALC WITH (NOLOCK)
                    WHERE IS_CLOSED = 0 AND PEND_QTY > 0
                    GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR
                ) P
                LEFT JOIN ARS_MSA_GEN_ART G WITH (NOLOCK)
                  ON G.RDC = P.RDC
                 AND ISNULL(G.MAJ_CAT,'')        = ISNULL(P.MAJ_CAT,'')
                 AND ISNULL(G.GEN_ART_NUMBER,'') = ISNULL(P.GEN_ART_NUMBER,'')
                 AND ISNULL(G.CLR,'')            = ISNULL(P.CLR,'')
                WHERE G.RDC IS NULL
            """)).fetchone()
            print(f"  MSA_GEN_ART missing (RDC,MAJ,GEN,CLR) groups = {r[0]}")
            print(f"  MSA_GEN_ART invisible PEND_QTY               = {(r[1] or 0):,.2f}")

            print()
            print("  5 example rows present in ARS_PEND_ALC but missing from ARS_MSA_TOTAL:")
            ex = conn.execute(text(f"""
                SELECT TOP 5 P.RDC, P.MAJ_CAT, P.GEN_ART_NUMBER, P.CLR,
                       P.ARTICLE_NUMBER, P.PEND_QTY
                FROM ARS_PEND_ALC P WITH (NOLOCK)
                LEFT JOIN ARS_MSA_TOTAL T WITH (NOLOCK)
                  ON T.RDC = P.RDC AND T.[{total_art_col}] = P.ARTICLE_NUMBER
                WHERE P.IS_CLOSED = 0 AND P.PEND_QTY > 0 AND T.RDC IS NULL
                ORDER BY P.PEND_QTY DESC
            """)).fetchall()
            for row in ex:
                print(f"    RDC={row[0]} MJ={row[1][:20] if row[1] else None} "
                      f"GEN={row[2]} CLR={row[3]} ART={row[4]} PEND={row[5]}")

        # ------------------------------------------------------------------
        section("S4  PEND-only rows missing from grid tables (claim 2)")
        # ------------------------------------------------------------------
        # Test against ARS_GRID_MJ_VAR_ART (variant grain — most direct comparison).
        if _table_exists(conn, "ARS_GRID_MJ_VAR_ART") and _table_exists(conn, "ARS_PEND_ALC"):
            gvar_art = "ARTICLE_NUMBER" if _col_exists(conn, "ARS_GRID_MJ_VAR_ART", "ARTICLE_NUMBER") else "VAR_ART"
            r = conn.execute(text(f"""
                SELECT COUNT(*) AS missing_rows, SUM(P.PEND_QTY) AS qty
                FROM ARS_PEND_ALC P WITH (NOLOCK)
                LEFT JOIN ARS_GRID_MJ_VAR_ART V WITH (NOLOCK)
                  ON V.WERKS = P.ST_CD
                 AND V.[{gvar_art}] = P.ARTICLE_NUMBER
                WHERE P.IS_CLOSED = 0 AND P.PEND_QTY > 0 AND V.WERKS IS NULL
            """)).fetchone()
            print(f"  GRID_MJ_VAR_ART missing rows = {r[0]}")
            print(f"  GRID_MJ_VAR_ART invisible PEND_QTY = {(r[1] or 0):,.2f}")

            print()
            print("  5 example rows present in ARS_PEND_ALC but missing from ARS_GRID_MJ_VAR_ART:")
            ex = conn.execute(text(f"""
                SELECT TOP 5 P.ST_CD AS WERKS, P.MAJ_CAT, P.GEN_ART_NUMBER, P.CLR,
                       P.ARTICLE_NUMBER, P.PEND_QTY
                FROM ARS_PEND_ALC P WITH (NOLOCK)
                LEFT JOIN ARS_GRID_MJ_VAR_ART V WITH (NOLOCK)
                  ON V.WERKS = P.ST_CD AND V.[{gvar_art}] = P.ARTICLE_NUMBER
                WHERE P.IS_CLOSED = 0 AND P.PEND_QTY > 0 AND V.WERKS IS NULL
                ORDER BY P.PEND_QTY DESC
            """)).fetchall()
            for row in ex:
                print(f"    WERKS={row[0]} MJ={(row[1] or '')[:20]} "
                      f"GEN={row[2]} CLR={row[3]} ART={row[4]} PEND={row[5]}")

        if _table_exists(conn, "ARS_GRID_MJ_GEN_ART") and _table_exists(conn, "ARS_PEND_ALC"):
            ggen = "GEN_ART_NUMBER" if _col_exists(conn, "ARS_GRID_MJ_GEN_ART", "GEN_ART_NUMBER") else "GEN_ART"
            r = conn.execute(text(f"""
                SELECT COUNT(*) AS missing_rows, SUM(P.PEND_QTY) AS qty
                FROM (
                    SELECT ST_CD, MAJ_CAT, GEN_ART_NUMBER, CLR, SUM(PEND_QTY) AS PEND_QTY
                    FROM ARS_PEND_ALC WITH (NOLOCK)
                    WHERE IS_CLOSED = 0 AND PEND_QTY > 0
                    GROUP BY ST_CD, MAJ_CAT, GEN_ART_NUMBER, CLR
                ) P
                LEFT JOIN ARS_GRID_MJ_GEN_ART G WITH (NOLOCK)
                  ON G.WERKS = P.ST_CD
                 AND ISNULL(G.MAJ_CAT,'')        = ISNULL(P.MAJ_CAT,'')
                 AND ISNULL(G.[{ggen}],'')       = ISNULL(P.GEN_ART_NUMBER,'')
                 AND ISNULL(G.CLR,'')            = ISNULL(P.CLR,'')
                WHERE G.WERKS IS NULL
            """)).fetchone()
            print(f"  GRID_MJ_GEN_ART missing (WERKS,MJ,GEN,CLR) groups = {r[0]}")
            print(f"  GRID_MJ_GEN_ART invisible PEND_QTY = {(r[1] or 0):,.2f}")

        # ------------------------------------------------------------------
        section("S5  Revert audit (claim 4)")
        # ------------------------------------------------------------------
        if _table_exists(conn, "ARS_PEND_ALC_OPERATIONS"):
            r = conn.execute(text("""
                SELECT
                    COUNT(*) AS total_ops,
                    SUM(CASE WHEN REVERTED_AT IS NOT NULL THEN 1 ELSE 0 END) AS reverted_ops,
                    SUM(CASE WHEN OP_TYPE='MANUAL' THEN 1 ELSE 0 END) AS manual,
                    SUM(CASE WHEN OP_TYPE='DO'     THEN 1 ELSE 0 END) AS do_,
                    SUM(CASE WHEN OP_TYPE='BDC'    THEN 1 ELSE 0 END) AS bdc,
                    MIN(CREATED_AT) AS first_op,
                    MAX(CREATED_AT) AS last_op
                FROM ARS_PEND_ALC_OPERATIONS WITH (NOLOCK)
            """)).fetchone()
            print(f"  total_ops    = {r[0]}")
            print(f"  reverted     = {r[1]}")
            print(f"  manual / do / bdc = {r[2]} / {r[3]} / {r[4]}")
            print(f"  first={r[5]}  last={r[6]}")

            print()
            print("  Last 5 ops (regardless of type):")
            ex = conn.execute(text("""
                SELECT TOP 5 OP_ID, OP_TYPE, OP_KEY, CREATED_AT, REVERTED_AT,
                             ROWS_AFFECTED, QTY_TOTAL
                FROM ARS_PEND_ALC_OPERATIONS WITH (NOLOCK)
                ORDER BY OP_ID DESC
            """)).fetchall()
            for row in ex:
                print(f"    op={row[0]} type={row[1]:6s} key={row[2][:30] if row[2] else None} "
                      f"created={row[3]} reverted={row[4]} rows={row[5]} qty={row[6]}")

            # Reverted MANUAL ops — verify pend_alc rows were deleted (session_id approach)
            print()
            print("  Reverted MANUAL ops — verifying rows were actually deleted:")
            ex = conn.execute(text("""
                SELECT TOP 5 OP_ID, OP_KEY, CREATED_AT, REVERTED_AT, ROWS_AFFECTED, QTY_TOTAL
                FROM ARS_PEND_ALC_OPERATIONS WITH (NOLOCK)
                WHERE OP_TYPE = 'MANUAL' AND REVERTED_AT IS NOT NULL
                ORDER BY REVERTED_AT DESC
            """)).fetchall()
            for op_id, op_key, created, reverted, rows_aff, qty in ex:
                still = conn.execute(text(
                    "SELECT COUNT(*), SUM(PEND_QTY) FROM ARS_PEND_ALC "
                    "WHERE SESSION_ID = :sid"
                ), {"sid": op_key}).fetchone()
                print(f"    op={op_id} sid={op_key[:35]} expected_deleted={rows_aff} "
                      f"still_present={still[0]} still_qty={still[1] or 0}")

            # Reverted DO ops — verify HOLD restoration
            print()
            print("  Reverted DO ops — note: _revert_do does NOT restore HOLD_REM "
                  "(see pend_alc_service.py:616-655). Confirming open ops have HOLD touched:")
            cnt = conn.execute(text("""
                SELECT COUNT(*)
                FROM ARS_PEND_ALC_OPERATIONS
                WHERE OP_TYPE='DO' AND REVERTED_AT IS NOT NULL
            """)).scalar() or 0
            print(f"    DO ops reverted to date = {cnt}")

        # ------------------------------------------------------------------
        section("S6  Hold tracking summary")
        # ------------------------------------------------------------------
        if _table_exists(conn, "ARS_NL_TBL_HOLD_TRACKING"):
            r = conn.execute(text("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN IS_CLOSED=0 THEN 1 ELSE 0 END) AS open_,
                    SUM(CASE WHEN IS_CLOSED=0 THEN HOLD_REM ELSE 0 END) AS open_qty
                FROM ARS_NL_TBL_HOLD_TRACKING WITH (NOLOCK)
            """)).fetchone()
            print(f"  total_rows={r[0]} open_rows={r[1]} open_qty={(r[2] or 0):,.2f}")

    print()
    print("Diagnostic complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
