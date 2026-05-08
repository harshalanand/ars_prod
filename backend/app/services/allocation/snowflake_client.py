"""
Snowflake Client using REST API (pure HTTP, no snowflake-connector-python needed).
Queries V2_ALLOCATION.RESULTS.ARTICLE_SCORES and V2RETAIL.GOLD.FACT_STOCK_GENCOLOR.
"""
import logging
import time
import json
import requests
import pandas as pd
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

SF_ACCOUNT = 'iafphkw-hh80816'
SF_USER = 'akashv2kart'
SF_PASSWORD = 'SVXqEe5pDdamMb9'
SF_WAREHOUSE = 'ALLOC_WH'

_session_token = None
_token_expires = 0


def _get_token() -> str:
    """Login to Snowflake and get session token (cached)."""
    global _session_token, _token_expires
    if _session_token and time.time() < _token_expires:
        return _session_token

    resp = requests.post(
        f'https://{SF_ACCOUNT}.snowflakecomputing.com/session/v1/login-request',
        json={
            'data': {
                'ACCOUNT_NAME': SF_ACCOUNT,
                'LOGIN_NAME': SF_USER,
                'PASSWORD': SF_PASSWORD,
                'CLIENT_APP_ID': 'ARS',
                'CLIENT_APP_VERSION': '2.0',
            }
        },
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    _session_token = data.get('data', {}).get('token', '')
    _token_expires = time.time() + 3500  # ~1 hour
    if not _session_token:
        raise Exception(f"Snowflake login failed: {json.dumps(data)[:200]}")
    logger.info("Snowflake session token acquired")
    return _session_token


def _execute_query(sql: str, database: str = 'V2_ALLOCATION') -> pd.DataFrame:
    """Execute SQL via Snowflake REST API and return DataFrame."""
    token = _get_token()

    resp = requests.post(
        f'https://{SF_ACCOUNT}.snowflakecomputing.com/queries/v1/query-request',
        json={
            'sqlText': sql,
            'asyncExec': False,
            'sequenceId': int(time.time()),
            'querySubmissionTime': int(time.time() * 1000),
        },
        headers={
            'Content-Type': 'application/json',
            'Accept': 'application/snowflake',
            'Authorization': f'Snowflake Token="{token}"',
        },
        params={
            'requestId': f'ars-{int(time.time())}',
        },
        timeout=120
    )

    if resp.status_code != 200:
        raise Exception(f"Snowflake query failed HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()

    if not data.get('success', False):
        msg = data.get('message', 'Unknown error')
        raise Exception(f"Snowflake query error: {msg}")

    result_data = data.get('data', {})
    row_type = result_data.get('rowtype', [])
    rows = result_data.get('rowset', [])

    if not rows:
        return pd.DataFrame()

    col_names = [col['name'].lower() for col in row_type]
    df = pd.DataFrame(rows, columns=col_names)
    return df


def get_scored_pairs(majcat: str, top_n_per_store: int = 200) -> pd.DataFrame:
    """Fetch pre-computed scores from Snowflake ARTICLE_SCORES."""
    t0 = time.time()
    try:
        sql = f"""
            SELECT ST_CD, GEN_ART_COLOR, GEN_ART, COLOR, SEG, TOTAL_SCORE,
                   DC_STOCK_QTY, MRP, VENDOR_CODE, FABRIC, SEASON,
                   IS_ST_SPECIFIC, PRIORITY_TYPE
            FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY ST_CD ORDER BY TOTAL_SCORE DESC) as rn
                FROM V2_ALLOCATION.RESULTS.ARTICLE_SCORES WHERE MAJCAT = '{majcat}'
            ) WHERE rn <= {top_n_per_store}
        """
        df = _execute_query(sql)

        # Convert types
        for col in ['total_score', 'dc_stock_qty']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0).astype(int)
        if 'mrp' in df.columns:
            df['mrp'] = pd.to_numeric(df['mrp'], errors='coerce').fillna(0)

        logger.info(f"[{majcat}] Snowflake scores: {len(df):,} rows, "
                    f"{df['st_cd'].nunique() if not df.empty else 0} stores, "
                    f"score {df['total_score'].min()}-{df['total_score'].max() if not df.empty else 0}, "
                    f"{time.time()-t0:.1f}s")
        return df
    except Exception as e:
        logger.error(f"[{majcat}] Snowflake scored pairs failed: {e}")
        return pd.DataFrame()


def get_store_stock(gencolor_keys: List[str], batch_size: int = 500) -> pd.DataFrame:
    """Fetch store stock from Snowflake FACT_STOCK_GENCOLOR."""
    t0 = time.time()
    if not gencolor_keys:
        return pd.DataFrame(columns=['st_cd', 'gen_art_color', 'stock_qty'])

    try:
        all_dfs = []
        for i in range(0, len(gencolor_keys), batch_size):
            batch = gencolor_keys[i:i+batch_size]
            gac_list = "','".join(str(g).replace("'", "''") for g in batch)
            sql = f"""
                SELECT STORE_CODE as ST_CD, GENCOLOR_KEY as GEN_ART_COLOR, STK_QTY as STOCK_QTY
                FROM V2RETAIL.GOLD.FACT_STOCK_GENCOLOR
                WHERE STK_QTY > 0 AND GENCOLOR_KEY IN ('{gac_list}')
            """
            df = _execute_query(sql)
            if not df.empty:
                all_dfs.append(df)

        if not all_dfs:
            return pd.DataFrame(columns=['st_cd', 'gen_art_color', 'stock_qty'])

        result = pd.concat(all_dfs, ignore_index=True)
        result['stock_qty'] = pd.to_numeric(result['stock_qty'], errors='coerce').fillna(0)

        logger.info(f"Snowflake store stock: {len(result):,} rows, "
                    f"{result['st_cd'].nunique()} stores, "
                    f"{result['gen_art_color'].nunique()} articles, "
                    f"{time.time()-t0:.1f}s")
        return result
    except Exception as e:
        logger.error(f"Snowflake store stock failed: {e}")
        return pd.DataFrame(columns=['st_cd', 'gen_art_color', 'stock_qty'])
