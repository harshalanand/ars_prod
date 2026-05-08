"""Test allocation determinism by running the pandas waterfall N times on
identical input data and diffing the outputs.

If results differ across runs, the diff prints the first divergent rows.
"""
import sys
import os
import hashlib
import pyodbc
import pandas as pd
from sqlalchemy import create_engine, text

# Make backend imports resolvable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Force the engine to use HOPC560 BEFORE app imports run
os.environ['DB_SERVER']   = 'HOPC560'
os.environ['DB_USERNAME'] = 'sa'
os.environ['DB_PASSWORD'] = 'vrl@55555'
os.environ['DB_NAME']     = 'rep_data'
os.environ['DATA_DB_NAME'] = 'rep_data'
os.environ['DB_DRIVER']   = 'ODBC Driver 18 for SQL Server'
os.environ['DB_TRUST_CERT'] = 'yes'
os.environ['JWT_SECRET_KEY'] = 'test'
os.environ['SUPER_ADMIN_USERNAME'] = 'test'
os.environ['SUPER_ADMIN_EMAIL'] = 'test@test.com'
os.environ['SUPER_ADMIN_PASSWORD'] = 'test'

ENGINE_URL = (
    "mssql+pyodbc://sa:vrl%4055555@HOPC560/rep_data"
    "?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes"
)

MAJ_CAT_TO_TEST = 'M_JEANS'
N_RUNS = 3


def main():
    engine = create_engine(ENGINE_URL)

    print(f"Loading ARS_LISTING_WORKING + ARS_ALLOC_WORKING for {MAJ_CAT_TO_TEST}...")
    with engine.connect() as conn:
        # Read deterministic snapshot
        listing = pd.read_sql(
            text(
                "SELECT * FROM ARS_LISTING_WORKING WITH (NOLOCK) "
                "WHERE MAJ_CAT = :mc "
                "ORDER BY MAJ_CAT, WERKS, GEN_ART_NUMBER, ISNULL(CLR,'')"
            ),
            conn, params={"mc": MAJ_CAT_TO_TEST},
        )
        alloc = pd.read_sql(
            text(
                "SELECT * FROM ARS_ALLOC_WORKING WITH (NOLOCK) "
                "WHERE MAJ_CAT = :mc "
                "ORDER BY MAJ_CAT, RDC, GEN_ART_NUMBER, ISNULL(CLR,''), "
                "         VAR_ART, SZ, OPT_PRIORITY_RANK, "
                "         ISNULL(ST_RANK, 999999), WERKS"
            ),
            conn, params={"mc": MAJ_CAT_TO_TEST},
        )
    print(f"  listing rows: {len(listing)}")
    print(f"  alloc   rows: {len(alloc)}")

    # Hash the inputs to confirm we're running on identical data
    in_listing_hash = hashlib.sha1(
        pd.util.hash_pandas_object(listing, index=False).values.tobytes()
    ).hexdigest()[:12]
    in_alloc_hash = hashlib.sha1(
        pd.util.hash_pandas_object(alloc, index=False).values.tobytes()
    ).hexdigest()[:12]
    print(f"  listing input hash: {in_listing_hash}")
    print(f"  alloc   input hash: {in_alloc_hash}")
    print()

    # Lazy-import after env is configured
    from app.services import rule_engine_pandas as rne_pd
    from app.services.rule_engine_new import _discover_primary_grids

    with engine.connect() as conn:
        grids = _discover_primary_grids(conn)
    print(f"  primary grids: {list(grids.keys())}")
    print()

    # Run the per-MAJ_CAT waterfall N times, with FRESH copies each time so
    # mutation in one run can't leak into the next.
    results = []
    for i in range(N_RUNS):
        a_in = alloc.copy()
        w_in = listing.copy()

        # The function mutates in place — we want pure repeatability.
        a_out, w_out = rne_pd._run_majcat_waterfall(
            a_in, w_in, grids,
            pri_ct_check_rl=False,
            pri_ct_check_tbc=False,
            rl_mbq_cap_pct=110.0,
            tbc_mbq_cap_pct=110.0,
            size_threshold=0.6,
            min_size_count=3,
        )

        ship_total = float(a_out['SHIP_QTY'].fillna(0).sum())
        hold_total = float(a_out['HOLD_QTY'].fillna(0).sum())
        ship_rl  = float(a_out.loc[a_out['OPT_TYPE']=='RL',  'SHIP_QTY'].fillna(0).sum())
        ship_tbc = float(a_out.loc[a_out['OPT_TYPE']=='TBC', 'SHIP_QTY'].fillna(0).sum())
        ship_tbl = float(a_out.loc[a_out['OPT_TYPE']=='TBL', 'SHIP_QTY'].fillna(0).sum())

        # Hash key alloc columns for cross-run comparison
        sig_cols = ['WERKS','RDC','MAJ_CAT','GEN_ART_NUMBER','CLR','VAR_ART','SZ',
                    'SHIP_QTY','HOLD_QTY','POOL_CONSUMED','ALLOC_STATUS']
        sig_cols = [c for c in sig_cols if c in a_out.columns]
        out_sorted = a_out[sig_cols].sort_values(
            ['WERKS','RDC','MAJ_CAT','GEN_ART_NUMBER','CLR','VAR_ART','SZ'],
            kind='mergesort',
        ).reset_index(drop=True)
        out_hash = hashlib.sha1(
            pd.util.hash_pandas_object(out_sorted, index=False).values.tobytes()
        ).hexdigest()[:12]

        results.append({
            'run':   i + 1,
            'total': ship_total + hold_total,
            'ship':  ship_total, 'hold': hold_total,
            'rl':    ship_rl, 'tbc': ship_tbc, 'tbl': ship_tbl,
            'hash':  out_hash, 'frame': out_sorted,
        })
        print(f"Run {i+1}: total={ship_total+hold_total:.0f}  ship={ship_total:.0f}  "
              f"hold={hold_total:.0f}  RL={ship_rl:.0f}  TBC={ship_tbc:.0f}  "
              f"TBL={ship_tbl:.0f}  hash={out_hash}")

    print()
    hashes = {r['hash'] for r in results}
    if len(hashes) == 1:
        print("*** DETERMINISTIC: all runs produced identical output. ***")
        return 0
    else:
        print("*** NON-DETERMINISTIC: outputs differ across runs. ***")
        # Show first diff
        a = results[0]['frame']
        b = results[1]['frame']
        if a.shape != b.shape:
            print(f"  shape mismatch: {a.shape} vs {b.shape}")
        else:
            ne_mask = (a != b).any(axis=1)
            ne = a[ne_mask].head(15)
            print(f"  first {len(ne)} divergent rows (run 1 vs run 2):")
            print(ne.to_string())
            print()
            print("  same rows in run 2:")
            print(b.loc[ne.index].to_string())
        return 1


if __name__ == '__main__':
    sys.exit(main())
