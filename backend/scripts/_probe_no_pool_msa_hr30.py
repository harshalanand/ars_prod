"""Diagnose NO_POOL_MSA stamping for GEN_ART=1240055949, CLR='A', WERKS='HR30'.

Run from D:\\ARS_PROD\\ars_prod\\backend:
    python -m scripts._probe_no_pool_msa_hr30
"""
from sqlalchemy import text
from app.database.session import data_engine

GEN_ART = "1240055949"
CLR = "A"
WERKS = "HR30"

with data_engine.connect() as conn:
    # 1. Find alloc tables (most recent)
    print("=" * 80)
    print("Step 1: discover alloc tables that have these rows")
    print("=" * 80)
    tbls = conn.execute(text("""
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_TYPE='BASE TABLE'
          AND (TABLE_NAME LIKE 'ARS_ALLOC_%' OR TABLE_NAME LIKE 'alloc_detail%' OR TABLE_NAME LIKE '%ALLOC_WORKING%')
        ORDER BY TABLE_NAME DESC
    """)).fetchall()
    for r in tbls:
        print(" ", r[0])

    # Probe each candidate for the failing rows
    candidates = [r[0] for r in tbls]
    hit_tables = []
    for t in candidates:
        try:
            n = conn.execute(text(f"""
                SELECT COUNT(*) FROM [{t}]
                WHERE GEN_ART_NUMBER=:g AND ISNULL(CLR,'')=:c AND WERKS=:w
            """), {"g": GEN_ART, "c": CLR, "w": WERKS}).scalar()
            if n and n > 0:
                hit_tables.append((t, n))
        except Exception as e:
            pass
    print("\nTables containing our keys:")
    for t, n in hit_tables:
        print(f"  {t}: {n} rows")

    if not hit_tables:
        print("\nNo alloc table contains these keys. Trying GEN_ART column variant...")
        for t in candidates[:5]:
            try:
                cols = conn.execute(text(f"""
                    SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_NAME = '{t}'
                """)).fetchall()
                col_names = [c[0] for c in cols]
                print(f"\n{t} cols (first 30): {col_names[:30]}")
            except Exception as e:
                print(f"  {t}: {e}")
        raise SystemExit(0)

    # Pick the most recently-written alloc table
    alloc_table = hit_tables[0][0]
    print(f"\nUsing alloc_table = {alloc_table}")

    # 2. Pull failing rows
    print("\n" + "=" * 80)
    print(f"Step 2: rows for GEN_ART={GEN_ART}, CLR={CLR}, WERKS={WERKS}")
    print("=" * 80)
    rows = conn.execute(text(f"""
        SELECT VAR_ART, SZ, OPT_TYPE, ALLOC_STATUS, SKIP_REASON,
               OPT_MBQ, SZ_MBQ, SZ_REQ, SZ_STK, FNL_Q, FNL_Q_REM,
               SHIP_QTY, HOLD_QTY,
               LEFT(ISNULL(ALLOC_REMARKS,''), 400) AS ALLOC_REMARKS
        FROM [{alloc_table}]
        WHERE GEN_ART_NUMBER=:g AND ISNULL(CLR,'')=:c AND WERKS=:w
        ORDER BY VAR_ART, SZ
    """), {"g": GEN_ART, "c": CLR, "w": WERKS}).fetchall()

    print(f"\n{len(rows)} rows total\n")
    # Header
    hdr = ("VAR_ART", "SZ", "OPT_TYPE", "ALLOC_STATUS", "SKIP_REASON",
           "OPT_MBQ", "SZ_MBQ", "SZ_REQ", "SZ_STK", "FNL_Q", "FNL_Q_REM",
           "SHIP", "HOLD")
    print(("{:<14} {:<6} {:<6} {:<10} {:<35} "
           "{:>7} {:>6} {:>6} {:>6} {:>6} {:>9} {:>5} {:>5}").format(*hdr))
    for r in rows:
        print(("{:<14} {:<6} {:<6} {:<10} {:<35} "
               "{:>7} {:>6} {:>6} {:>6} {:>6} {:>9} {:>5} {:>5}").format(
            str(r[0])[:14], str(r[1])[:6], str(r[2])[:6],
            str(r[3])[:10], str(r[4] or '')[:35],
            r[5] or 0, r[6] or 0, r[7] or 0, r[8] or 0, r[9] or 0, r[10] or 0,
            r[11] or 0, r[12] or 0))

    # Print ALLOC_REMARKS for NO_POOL_MSA rows
    print("\n" + "=" * 80)
    print("Step 3: ALLOC_REMARKS for NO_POOL_MSA rows")
    print("=" * 80)
    no_pool_rows = [r for r in rows if (r[4] or '') == 'NO_POOL_MSA']
    print(f"\n{len(no_pool_rows)} NO_POOL_MSA rows")
    for r in no_pool_rows[:25]:
        print(f"\n  VAR_ART={r[0]} SZ={r[1]} OPT_TYPE={r[2]}")
        print(f"    REMARKS: {r[13]}")

    # 4. Distinct SKIP_REASON breakdown for this OPT (across all stores)
    print("\n" + "=" * 80)
    print("Step 4: SKIP_REASON breakdown across this OPT (all stores)")
    print("=" * 80)
    breakdown = conn.execute(text(f"""
        SELECT ISNULL(SKIP_REASON,'(null)') AS reason, COUNT(*) AS n,
               SUM(CASE WHEN ISNULL(SHIP_QTY,0)=0 AND ISNULL(HOLD_QTY,0)=0 THEN 1 ELSE 0 END) AS zero_rows
        FROM [{alloc_table}]
        WHERE GEN_ART_NUMBER=:g AND ISNULL(CLR,'')=:c
        GROUP BY ISNULL(SKIP_REASON,'(null)')
        ORDER BY COUNT(*) DESC
    """), {"g": GEN_ART, "c": CLR}).fetchall()
    for r in breakdown:
        print(f"  {r[0]:<40} count={r[1]:<6} zero_rows={r[2]}")

    # 5. MSA pool source — pull what the engine would have started with
    print("\n" + "=" * 80)
    print("Step 5: MSA pool source for these keys (post-run remaining)")
    print("=" * 80)
    msa_candidates = [
        ("ARS_MSA_VAR_ART", "GEN_ART_NUMBER", "VAR_ART", "SZ"),
        ("ARS_MSA_GEN_ART", "GEN_ART_NUMBER", None, None),
        ("ARS_MSA_TOTAL", None, None, None),
    ]
    for tbl, *_ in msa_candidates:
        try:
            cols = conn.execute(text(f"""
                SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME='{tbl}'
            """)).fetchall()
            if not cols:
                print(f"\n  {tbl}: table not present")
                continue
            colset = {c[0].upper() for c in cols}
            print(f"\n  {tbl}: cols include FNL_Q={'FNL_Q' in colset}, "
                  f"FNL_Q_REM={'FNL_Q_REM' in colset}, GEN_ART_NUMBER={'GEN_ART_NUMBER' in colset}, "
                  f"VAR_ART={'VAR_ART' in colset}, SZ={'SZ' in colset}, "
                  f"CLR={'CLR' in colset}, RDC={'RDC' in colset}, ST_CD={'ST_CD' in colset}")
            # If the table has FNL_Q + GEN_ART + VAR_ART + SZ, pull the pool state
            if {'FNL_Q', 'GEN_ART_NUMBER'}.issubset(colset):
                sz_col = "SZ" if "SZ" in colset else None
                var_col = "VAR_ART" if "VAR_ART" in colset else None
                clr_col = "CLR" if "CLR" in colset else None
                fnlrem = "FNL_Q_REM" if "FNL_Q_REM" in colset else "FNL_Q"
                rdc_col = "RDC" if "RDC" in colset else ("ST_CD" if "ST_CD" in colset else None)
                sel = "FNL_Q"
                if fnlrem != "FNL_Q":
                    sel += f", {fnlrem}"
                grp = ["GEN_ART_NUMBER"]
                if clr_col: grp.append(clr_col)
                if var_col: grp.append(var_col)
                if sz_col: grp.append(sz_col)
                clr_where = f"AND ISNULL({clr_col},'')=:c" if clr_col else ""
                q = f"""
                    SELECT {', '.join(grp)}, SUM(ISNULL(FNL_Q,0)) AS pool_init,
                           {'SUM(ISNULL(' + fnlrem + ',0))' if fnlrem != 'FNL_Q' else 'SUM(ISNULL(FNL_Q,0))'} AS pool_now
                    FROM [{tbl}]
                    WHERE GEN_ART_NUMBER=:g {clr_where}
                    GROUP BY {', '.join(grp)}
                    ORDER BY {', '.join(grp)}
                """
                pool_rows = conn.execute(text(q), {"g": GEN_ART, "c": CLR}).fetchall()
                print(f"    {len(pool_rows)} pool rows for our keys (sample first 15):")
                for pr in pool_rows[:15]:
                    print(f"      {tuple(pr)}")
        except Exception as e:
            print(f"  {tbl}: error {e}")
