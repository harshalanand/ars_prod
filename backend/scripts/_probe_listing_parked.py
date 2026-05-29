"""Probe ARS_LISTING_WORKING_PARKED / ARS_LISTING_PARKED / ARS_LISTING_HISTORY in Rep_Data."""
from sqlalchemy import create_engine, text
import urllib

ODBC = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    "SERVER=tcp:hopc560;DATABASE=Rep_Data;UID=sa;PWD=vrl@55555;"
    "TrustServerCertificate=yes;Encrypt=no;Connection Timeout=60;"
)
url = "mssql+pyodbc:///?odbc_connect=" + urllib.parse.quote_plus(ODBC)
eng = create_engine(url)

SESSION = "20260528_110819_156"

with eng.connect() as cx:
    for tbl in ("ARS_LISTING_WORKING_PARKED", "ARS_LISTING_PARKED", "ARS_LISTING_HISTORY"):
        print("=" * 72)
        print(f"TABLE: {tbl}")
        try:
            cols = cx.execute(text(f"""
                SELECT COLUMN_NAME, DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = :t
                ORDER BY ORDINAL_POSITION
            """), {"t": tbl}).fetchall()
            if not cols:
                print("  (does not exist)")
                continue
            print(f"  columns ({len(cols)}):")
            for c, dt in cols:
                print(f"    {c}  [{dt}]")
        except Exception as e:
            print(f"  ERROR listing cols: {e}")
            continue

        # Look for candidate column names
        col_names = [c[0] for c in cols]
        candidates = {
            "OPT_TYPE": [c for c in col_names if c.upper() == "OPT_TYPE"],
            "PRIORITY/RANK": [c for c in col_names if any(k in c.upper() for k in ("RANK", "PRIORITY"))],
            "ALLOC_SEQ": [c for c in col_names if "ALLOC_SEQ" in c.upper() or "SEQ" in c.upper()],
            "STATUS": [c for c in col_names if "STATUS" in c.upper() or "STATE" in c.upper() or "FLAG" in c.upper()],
            "REMARKS/NOTES": [c for c in col_names if any(k in c.upper() for k in ("REMARK", "NOTE", "COMMENT", "REASON"))],
            "EXCESS_STK": [c for c in col_names if "EXCESS" in c.upper()],
        }
        print("  candidate matches:")
        for k, v in candidates.items():
            print(f"    {k}: {v}")

        # EXCESS_STK probe
        if "EXCESS_STK" in [c.upper() for c in col_names]:
            try:
                rows = cx.execute(text(f"""
                    SELECT TOP 5 SESSION_ID, GEN_ART_NUMBER, CLR, RDC, EXCESS_STK
                    FROM {tbl}
                    WHERE SESSION_ID = :s AND EXCESS_STK > 0
                """), {"s": SESSION}).fetchall()
                print(f"  EXCESS_STK>0 rows for session {SESSION}: {len(rows)}")
                for r in rows:
                    print(f"    {tuple(r)}")
                cnt_total = cx.execute(text(f"SELECT COUNT(*) FROM {tbl} WHERE SESSION_ID = :s"), {"s": SESSION}).scalar()
                cnt_pos = cx.execute(text(f"SELECT COUNT(*) FROM {tbl} WHERE SESSION_ID = :s AND EXCESS_STK > 0"), {"s": SESSION}).scalar()
                cnt_zero = cx.execute(text(f"SELECT COUNT(*) FROM {tbl} WHERE SESSION_ID = :s AND (EXCESS_STK = 0 OR EXCESS_STK IS NULL)"), {"s": SESSION}).scalar()
                print(f"  session row counts: total={cnt_total}, EXCESS_STK>0={cnt_pos}, zero/null={cnt_zero}")
            except Exception as e:
                print(f"  EXCESS_STK probe error: {e}")

        # also: sample one row
        try:
            sample = cx.execute(text(f"SELECT TOP 1 * FROM {tbl} WHERE SESSION_ID = :s"), {"s": SESSION}).fetchone()
            if sample:
                print(f"  sample row keys present (truncated): {list(sample._mapping.keys())[:25]}...")
        except Exception as e:
            print(f"  sample row error: {e}")
