"""
Snowflake Data Loader — reads pre-cached Snowflake data from JSON files.

Architecture:
  Snowflake (246M scored pairs, 4.97M store stock) → cached as JSON → Azure reads JSON
  
  The JSON cache is refreshed by:
  1. Daily cron job that queries Snowflake and saves results
  2. Manual refresh via /allocation-engine/refresh-snowflake endpoint
  3. Deployment includes latest cached data

Files:
  data/snowflake/scored_pairs.json.gz → {MAJCAT: {scored: [...], stock: [...]}}
"""
import logging
import gzip
import json
import os
import pandas as pd
from typing import List, Optional

logger = logging.getLogger(__name__)

# Cache in memory after first load
_cache = None
_cache_path = None


def _find_cache_file() -> str:
    """Find the Snowflake cache file."""
    candidates = [
        os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'snowflake', 'scored_pairs.json.gz'),
        '/home/site/wwwroot/data/snowflake/scored_pairs.json.gz',  # Azure App Service
        os.path.join(os.getcwd(), 'data', 'snowflake', 'scored_pairs.json.gz'),
    ]
    for p in candidates:
        fp = os.path.abspath(p)
        if os.path.exists(fp):
            return fp
    return ''


def _load_cache():
    """Load the JSON cache into memory."""
    global _cache, _cache_path
    if _cache is not None:
        return _cache

    path = _find_cache_file()
    if not path:
        logger.warning("No Snowflake cache file found")
        _cache = {}
        return _cache

    try:
        with gzip.open(path, 'rt') as f:
            _cache = json.load(f)
        _cache_path = path
        majcats = list(_cache.keys())
        total_scored = sum(len(v.get('scored', [])) for v in _cache.values())
        total_stock = sum(len(v.get('stock', [])) for v in _cache.values())
        logger.info(f"Loaded Snowflake cache: {len(majcats)} MAJCATs, "
                    f"{total_scored:,} scored pairs, {total_stock:,} store stock rows "
                    f"from {path}")
        return _cache
    except Exception as e:
        logger.error(f"Failed to load Snowflake cache: {e}")
        _cache = {}
        return _cache


def get_scored_pairs(majcat: str, top_n: int = 200) -> pd.DataFrame:
    """Get pre-computed scored pairs from Snowflake cache."""
    cache = _load_cache()
    mc_data = cache.get(majcat, {})
    scored_list = mc_data.get('scored', [])

    if not scored_list:
        logger.warning(f"[{majcat}] No scored pairs in Snowflake cache")
        return pd.DataFrame()

    df = pd.DataFrame(scored_list)
    logger.info(f"[{majcat}] Snowflake scores: {len(df):,} pairs, "
                f"{df['st_cd'].nunique()} stores, {df['gen_art_color'].nunique()} articles, "
                f"score range: {df['total_score'].min()}-{df['total_score'].max()}")
    return df


def get_store_stock(gen_art_colors: List[str] = None, majcat: str = None) -> pd.DataFrame:
    """Get store stock from Snowflake cache."""
    cache = _load_cache()
    mc_data = cache.get(majcat, {})
    stock_list = mc_data.get('stock', [])

    if not stock_list:
        logger.warning(f"[{majcat}] No store stock in Snowflake cache")
        return pd.DataFrame()

    df = pd.DataFrame(stock_list)

    # Filter to scored articles if specified
    if gen_art_colors:
        gac_set = set(gen_art_colors)
        df = df[df['gen_art_color'].isin(gac_set)]

    logger.info(f"[{majcat}] Store stock: {len(df):,} rows, "
                f"{df['st_cd'].nunique()} stores, {df['gen_art_color'].nunique()} articles")
    return df


def get_budget_cascade(majcat: str) -> pd.DataFrame:
    """Get budget cascade — not cached, use Supabase/SQL."""
    return pd.DataFrame()


def get_dc_variant_stock(gen_art_colors: List[str], majcat: str) -> pd.DataFrame:
    """Get DC variant stock — not cached, use SQL."""
    return pd.DataFrame()


def get_available_majcats() -> List[str]:
    """Get list of MAJCATs available in cache."""
    cache = _load_cache()
    return list(cache.keys())


def reload_cache():
    """Force reload of cache from disk."""
    global _cache
    _cache = None
    _load_cache()
