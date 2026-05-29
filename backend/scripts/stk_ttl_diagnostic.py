"""
STK_TTL vs PEND_ALC drift diagnostic.

Runs V1-V4 from the plan and prints a single consolidated report.

Usage (from backend/):
    venv/Scripts/python.exe scripts/stk_ttl_diagnostic.py
"""
from __future__ import annotations

import sys
from typing import List, Tuple

from sqlalchemy import text

from app.database.session import get_data_engine


GRIDS_OF_INTEREST: List[str] = [
    "ARS_GRID_MJ",
    "ARS_GRID_MJ_MERGE_RNG_SEG",
    "ARS_GRID_MJ_RNG_SEG",
    "ARS_GRID_MJ_MACRO_MVGR",
    "ARS_GRID_MJ_MICRO_MVGR",
    "ARS_GRID_MJ_CLR",
    "ARS_GRID_MJ_FAB",
    "ARS_GRID_MJ_GEN_ART",
    "ARS_GRID_MJ_M_VND_CD",
    "ARS_GRID_MJ_VAR_ART",
]


def _table_exists(conn, name: str) -> bool:
    return (conn.execute(text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :t"
    ), {"t": name}).scalar() or 0) > 0


def _col_exists(conn, table: str, col: str) -> bool:
    return (conn.execute(text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME = :t AND COLUMN_NAME = :c"
    ), {"t": table, "c": col}).scalar() or 0) > 0


def section(label: str) -> None:
    print()
    print("=" * 78)
    print(label)
    print("=" * 78)


def main() -> int:
    engine = get_data_engine()
    with engine.connect() as conn:
        section("Pre-flight")
        for nm in ("ET_STORE_STOCK", "vw_master_product",
                   "ARS_STORE_SLOC_SETTINGS", "ARS_PEND_ALC",
                   "ARS_GRID_BUILDER"):
            print(f"  {nm:30s}  exists={_table_exists(conn, nm)}")

        # ── V4 — User's exact sum query, per grid ──
        section("V4 — SUM(PEND_ALC), SUM(STK_TTL), COUNT(*) per grid table")
        print(f"  {'grid':32s} {'rows':>12s} {'sum_pend_alc':>16s} {'sum_stk_ttl':>18s}")
        v4_rows: List[Tuple[str, int, float, float]] = []
        for g in GRIDS_OF_INTEREST:
            if not _table_exists(conn, g):
                print(f"  {g:32s} {'(missing)':>12s}")
                continue
            has_pend = _col_exists(conn, g, "PEND_ALC")
            has_stk  = _col_exists(conn, g, "STK_TTL")
            rc       = conn.execute(text(f"SELECT COUNT(*) FROM [{g}] WITH (NOLOCK)")).scalar() or 0
            p_sum    = (conn.execute(text(
                f"SELECT SUM(TRY_CAST(PEND_ALC AS FLOAT)) FROM [{g}] WITH (NOLOCK)"
            )).scalar() or 0.0) if has_pend else None
            s_sum    = (conn.execute(text(
                f"SELECT SUM(TRY_CAST(STK_TTL AS FLOAT)) FROM [{g}] WITH (NOLOCK)"
            )).scalar() or 0.0) if has_stk else None
            print(f"  {g:32s} {rc:>12,} "
                  f"{(f'{p_sum:>16,.2f}' if p_sum is not None else 'no col'):>16s} "
                  f"{(f'{s_sum:>18,.2f}' if s_sum is not None else 'no col'):>18s}")
            v4_rows.append((g, rc, p_sum or 0.0, s_sum or 0.0))

        # ── V3 — grid_builder rows for these tables ──
        section("V3 — ARS_GRID_BUILDER metadata (kpi_filter, hierarchy_columns)")
        if _table_exists(conn, "ARS_GRID_BUILDER"):
            # Build a dynamic IN list safely
            named = {f"t{i}": g for i, g in enumerate(GRIDS_OF_INTEREST)}
            in_clause = ", ".join(f":{k}" for k in named)
            rows = conn.execute(text(f"""
                SELECT seq, grid_name, output_table, kpi_filter,
                       pivot_only, grid_group, hierarchy_columns
                FROM ARS_GRID_BUILDER
                WHERE output_table IN ({in_clause})
                ORDER BY seq, id
            """), named).fetchall()
            for r in rows:
                seq, gn, ot, kf, po, gg, hc = r
                print(f"  seq={seq:>3} {ot:32s} kpi={str(kf):>6s} pivot_only={bool(po)} "
                      f"group={str(gg):>10s} hier={hc}")
        else:
            print("  ARS_GRID_BUILDER missing")

        # ── V2 — Unmapped MATNRs in vw_master_product ──
        section("V2 — Stock with MATNR missing from vw_master_product")
        if _table_exists(conn, "ET_STORE_STOCK") and _table_exists(conn, "vw_master_product"):
            res = conn.execute(text("""
                SELECT
                    COUNT(*)                                                          AS rows_total,
                    SUM(CASE WHEN MP.ARTICLE_NUMBER IS NULL THEN 1 ELSE 0 END)        AS rows_unmapped,
                    COUNT(DISTINCT STK.MATNR)                                         AS distinct_matnr,
                    COUNT(DISTINCT CASE WHEN MP.ARTICLE_NUMBER IS NULL
                                        THEN STK.MATNR END)                           AS distinct_unmapped,
                    SUM(TRY_CAST(STK.PARTICULARS_VALUE AS FLOAT))                     AS total_value,
                    SUM(CASE WHEN MP.ARTICLE_NUMBER IS NULL
                             THEN TRY_CAST(STK.PARTICULARS_VALUE AS FLOAT) END)       AS unmapped_value
                FROM ET_STORE_STOCK STK WITH (NOLOCK)
                LEFT JOIN vw_master_product MP WITH (NOLOCK) ON STK.MATNR = MP.ARTICLE_NUMBER
                INNER JOIN ARS_STORE_SLOC_SETTINGS S WITH (NOLOCK) ON STK.SLOC = S.SLOC
                WHERE UPPER(S.STATUS)='ACTIVE' AND UPPER(S.KPI)='STK'
                  AND STK.WERKS IS NOT NULL AND STK.WERKS <> ''
            """)).fetchone()
            rt, ru, dm, du, tv, uv = res
            print(f"  rows_total           = {rt:,}")
            print(f"  rows_unmapped        = {ru:,}  ({(100.0*ru/rt) if rt else 0:.2f}%)")
            print(f"  distinct_matnr       = {dm:,}")
            print(f"  distinct_unmapped    = {du:,}  ({(100.0*du/dm) if dm else 0:.2f}%)")
            print(f"  total_stk_value      = {tv or 0:,.2f}")
            print(f"  unmapped_stk_value   = {uv or 0:,.2f}  ({(100.0*(uv or 0)/(tv or 1)):.2f}%)")
        else:
            print("  ET_STORE_STOCK or vw_master_product missing — skipping")

        # ── V1 — Raw stk value from un-deduped pivot CTE ──
        section("V1 — Raw STK value (no dedup, no LISTING filter)")
        if _table_exists(conn, "ET_STORE_STOCK") and _table_exists(conn, "ARS_STORE_SLOC_SETTINGS"):
            res = conn.execute(text("""
                SELECT
                    SUM(TRY_CAST(STK.PARTICULARS_VALUE AS FLOAT))      AS raw_total,
                    COUNT(*)                                          AS raw_rows
                FROM ET_STORE_STOCK STK WITH (NOLOCK)
                INNER JOIN ARS_STORE_SLOC_SETTINGS S WITH (NOLOCK) ON STK.SLOC = S.SLOC
                WHERE UPPER(S.STATUS)='ACTIVE' AND UPPER(S.KPI)='STK'
                  AND STK.WERKS IS NOT NULL AND STK.WERKS <> ''
            """)).fetchone()
            raw_total, raw_rows = res
            print(f"  raw_total (all KPI=STK stock, no filter) = {(raw_total or 0):,.2f}")
            print(f"  raw_rows                                 = {raw_rows:,}")

            # PEND raw input
            if _table_exists(conn, "ARS_PEND_ALC"):
                p = conn.execute(text("""
                    SELECT SUM(TRY_CAST(PEND_QTY AS FLOAT)), COUNT(*)
                    FROM ARS_PEND_ALC WITH (NOLOCK)
                    WHERE IS_CLOSED = 0 AND PEND_QTY > 0
                """)).fetchone()
                print(f"  raw PEND_QTY (IS_CLOSED=0, >0)           = {(p[0] or 0):,.2f}  in {p[1]:,} rows")

            # Show drift per grid vs raw_total
            section("V1 — Per-grid STK_TTL vs raw_total (drift from un-deduped baseline)")
            print(f"  {'grid':32s} {'sum_stk_ttl':>18s} {'drift_vs_raw':>16s} {'pct':>8s}")
            for g, rc, p_sum, s_sum in v4_rows:
                drift = (raw_total or 0) - s_sum
                pct = (100.0 * drift / (raw_total or 1)) if raw_total else 0
                print(f"  {g:32s} {s_sum:>18,.2f} {drift:>16,.2f} {pct:>7.2f}%")
        else:
            print("  source tables missing — skipping")

        # Effect of LISTING filter alone — what's the stock value tied to listed stores
        section("Listing-filter expected loss (uniform across grids)")
        if _table_exists(conn, "Master_ALC_INPUT_ST_MASTER"):
            res = conn.execute(text("""
                SELECT
                    SUM(TRY_CAST(STK.PARTICULARS_VALUE AS FLOAT)) AS listed_total,
                    SUM(CASE WHEN ISNULL(CAST(M.LISTING AS NVARCHAR(50)),'') <> '1'
                             THEN TRY_CAST(STK.PARTICULARS_VALUE AS FLOAT) END) AS unlisted_value
                FROM ET_STORE_STOCK STK WITH (NOLOCK)
                INNER JOIN ARS_STORE_SLOC_SETTINGS S WITH (NOLOCK) ON STK.SLOC = S.SLOC
                LEFT JOIN Master_ALC_INPUT_ST_MASTER M WITH (NOLOCK) ON STK.WERKS = M.ST_CD
                WHERE UPPER(S.STATUS)='ACTIVE' AND UPPER(S.KPI)='STK'
                  AND STK.WERKS IS NOT NULL AND STK.WERKS <> ''
            """)).fetchone()
            print(f"  raw total              = {(res[0] or 0):,.2f}")
            print(f"  unlisted stock value   = {(res[1] or 0):,.2f}")
            print(f"  listed-only baseline   = {((res[0] or 0) - (res[1] or 0)):,.2f}")
        else:
            print("  Master_ALC_INPUT_ST_MASTER missing — skipping")

    print()
    print("Diagnostic complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
