"""
Engine 2: Article Scorer
Computes a composite score for every (article, store) pair within a MAJCAT.
Replaces the 8-level waterfall with a single scoring pass.

Score = sum of attribute match weights (configurable from UI)
- ST_SPECIFIC: ∞ for target stores, 0 for all others
- NATIONAL_HERO / CORE_FOCUS / ASSORTED: bonus points
- SEG, MVGR, VENDOR, MRP, FABRIC, COLOR, SEASON, NECK: match points
- DC stock = 0 → article not scored (hard filter)
"""
import logging
from typing import Dict, List, Optional
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class ArticleScorer:
    """Score every MSA article for every store within a MAJCAT."""

    def __init__(self, weights: Dict[str, int], settings: Dict[str, str]):
        self.weights = weights
        self.settings = settings
        self.min_score = int(settings.get('min_score_threshold', 0))

    def score(
        self,
        msa_articles: pd.DataFrame,
        stores: pd.DataFrame,
        dc_stock: pd.DataFrame,
        priority_list: pd.DataFrame,
        st_specific: pd.DataFrame,
        budget_cascade: pd.DataFrame,
        majcat: str,
    ) -> pd.DataFrame:
        """
        Score all MSA articles against all stores for a given MAJCAT.

        Args:
            msa_articles: MSA articles with columns [gen_art_color, gen_art, color, seg,
                          macro_mvgr, mvgr1, vendor_code, mrp, fabric, season, neck, ...]
            stores: Store list with columns [st_cd, st_nm, ...]
            dc_stock: DC stock with columns [gen_art_color, stock_qty, rdc_code]
            priority_list: DC article priority [gen_art_color, priority_type, priority_rank]
            st_specific: Store-specific listing [st_cd, gen_art_color, is_specific]
            budget_cascade: Budget cascade results [st_cd, majcat, seg, ...]
            majcat: The MAJCAT being processed

        Returns:
            DataFrame with columns [st_cd, gen_art_color, total_score, score_breakdown..., dc_stock_qty]
            Sorted by total_score DESC
        """
        logger.info(f"[{majcat}] Scoring {len(msa_articles)} articles × {len(stores)} stores")

        # Step 1: Filter to articles with DC stock > 0
        dc_avail = dc_stock[dc_stock['stock_qty'] > 0][['gen_art_color', 'stock_qty']].copy()
        dc_avail = dc_avail.groupby('gen_art_color')['stock_qty'].sum().reset_index()

        articles = msa_articles.merge(dc_avail, on='gen_art_color', how='inner')
        if articles.empty:
            logger.warning(f"[{majcat}] No articles with DC stock > 0")
            return pd.DataFrame()

        logger.info(f"[{majcat}] {len(articles)} articles have DC stock")

        # Step 2: Build priority lookup
        hero_set = set()
        focus_set = set()
        assorted_set = set()
        priority_map = {}

        if not priority_list.empty:
            for _, row in priority_list.iterrows():
                gac = row.get('gen_art_color', '')
                ptype = row.get('priority_type', '')
                if ptype == 'NATIONAL_HERO':
                    hero_set.add(gac)
                elif ptype == 'CORE_FOCUS':
                    focus_set.add(gac)
                elif ptype == 'ASSORTED':
                    assorted_set.add(gac)
                priority_map[gac] = ptype

        # Step 3: Build ST_SPECIFIC lookup {gen_art_color: set(st_cd)}
        st_spec_map = {}
        if not st_specific.empty:
            for _, row in st_specific.iterrows():
                gac = row.get('gen_art_color', '')
                st = row.get('st_cd', '')
                if gac not in st_spec_map:
                    st_spec_map[gac] = set()
                st_spec_map[gac].add(st)

        # Step 4: Build budget-based attribute expectations per store
        # What SEG/MVGR does each store expect?
        store_expectations = {}
        if not budget_cascade.empty:
            for _, row in budget_cascade.iterrows():
                st = row.get('st_cd', '')
                seg = row.get('seg', '')
                mvgr = row.get('macro_mvgr', '')
                if st not in store_expectations:
                    store_expectations[st] = {'segs': set(), 'mvgrs': set()}
                if seg:
                    store_expectations[st]['segs'].add(seg)
                if mvgr:
                    store_expectations[st]['mvgrs'].add(mvgr)

        # Step 5: Cross-join articles × stores and compute scores
        # For efficiency, vectorize as much as possible
        store_list = stores['st_cd'].unique().tolist()
        article_records = articles.to_dict('records')

        scored_pairs = []
        w = self.weights

        for art in article_records:
            gac = art.get('gen_art_color', '')
            gen_art = art.get('gen_art', '')
            color = art.get('color', '')
            art_seg = str(art.get('seg', '')).strip()
            art_mvgr = str(art.get('macro_mvgr', '')).strip()
            art_mvgr1 = str(art.get('mvgr1', '')).strip()
            art_vendor = str(art.get('vendor_code', '')).strip()
            art_mrp = art.get('mrp', 0)
            art_fabric = str(art.get('fabric', '')).strip()
            art_color = str(color).strip()
            art_season = str(art.get('season', '')).strip()
            art_neck = str(art.get('neck', '')).strip()
            dc_qty = int(art.get('stock_qty', 0))

            # Check if ST_SPECIFIC — only score for target stores
            is_st_specific = gac in st_spec_map
            target_stores = st_spec_map.get(gac, None)

            # Priority bonuses (same for all stores)
            score_hero = w.get('NATIONAL_HERO', 0) if gac in hero_set else 0
            score_focus = w.get('CORE_FOCUS', 0) if gac in focus_set else 0
            score_assorted = w.get('ASSORTED', 0) if gac in assorted_set else 0
            ptype = priority_map.get(gac)

            for st_cd in store_list:
                # ST_SPECIFIC: article only scores for its target stores
                if is_st_specific:
                    if st_cd not in target_stores:
                        continue  # Score = 0, skip entirely
                    score_st_spec = w.get('ST_SPECIFIC', 9999)
                else:
                    score_st_spec = 0

                # Attribute matching against store expectations
                exp = store_expectations.get(st_cd, {'segs': set(), 'mvgrs': set()})

                score_seg = w.get('SEG', 0) if art_seg and art_seg in exp['segs'] else 0
                score_mvgr = w.get('MACRO_MVGR', 0) if art_mvgr and art_mvgr in exp['mvgrs'] else 0
                score_vendor = w.get('VENDOR', 0) if art_vendor else 0  # TODO: match against store vendor history
                score_mrp = w.get('MRP_RANGE', 0) if art_mrp > 0 else 0  # TODO: match against store MRP profile
                score_fabric = w.get('FABRIC', 0) if art_fabric else 0
                score_color = w.get('COLOR', 0) if art_color else 0
                score_season = w.get('SEASON', 0) if art_season else 0
                score_neck = w.get('NECK', 0) if art_neck else 0
                score_mvgr1 = w.get('MVGR1', 0) if art_mvgr1 else 0

                total = (score_st_spec + score_hero + score_focus + score_assorted +
                         score_seg + score_mvgr + score_mvgr1 + score_vendor +
                         score_mrp + score_fabric + score_color + score_season + score_neck)

                if total < self.min_score and not is_st_specific:
                    continue

                scored_pairs.append({
                    'st_cd': st_cd,
                    'majcat': majcat,
                    'gen_art_color': gac,
                    'gen_art': gen_art,
                    'color': color,
                    'seg': art_seg,
                    'total_score': total,
                    'score_st_specific': score_st_spec,
                    'score_hero': score_hero,
                    'score_focus': score_focus,
                    'score_seg': score_seg,
                    'score_mvgr': score_mvgr,
                    'score_vendor': score_vendor,
                    'score_mrp': score_mrp,
                    'score_fabric': score_fabric,
                    'score_color': score_color,
                    'score_season': score_season,
                    'score_neck': score_neck,
                    'score_gp_psf': 0,
                    'dc_stock_qty': dc_qty,
                    'mrp': art_mrp,
                    'vendor_code': art_vendor,
                    'fabric': art_fabric,
                    'season': art_season,
                    'is_st_specific': 1 if is_st_specific else 0,
                    'priority_type': ptype,
                })

        if not scored_pairs:
            logger.warning(f"[{majcat}] No scored pairs generated")
            return pd.DataFrame()

        df = pd.DataFrame(scored_pairs)
        df = df.sort_values('total_score', ascending=False).reset_index(drop=True)

        logger.info(f"[{majcat}] Generated {len(df)} scored pairs. "
                     f"Max score={df['total_score'].max()}, Min={df['total_score'].min()}, "
                     f"Avg={df['total_score'].mean():.1f}")
        return df
