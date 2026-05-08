"""
Engine 1: Budget Cascade
Cascades company budget → store → MAJCAT → segment → option slots.

Data sources:
  - Supabase (v2srm): BGT sales and BGT display per store × MAJCAT
  - SQL Server: Budget contribution % tables (SEG, MVGR, etc.)
  - Store priority list

Key formulas from Excel (Appendix C):
  - C.3: OPT at SEG = MROUND(MAJCAT_options × SEG_cont%, 1)
  - C.6: MBQ has 7 calculation modes (DISP, B_MTH, SSN, combinations)
  - C.8: Conservative rounding — only round up if fraction > 0.7
"""
import logging
import math
from typing import Dict, Optional
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


def conservative_round(x: float) -> int:
    """Round up only if fractional part > 0.7 (from Excel formula C.8)."""
    base = int(x)
    frac = x - base
    return base + 1 if frac > 0.7 else base


class BudgetCascade:
    """Cascade budget from company level to per-store option slots."""

    MBQ_TYPES = ['DISP', 'B_MTH', 'SSN', 'DISP+B_MTH', 'DISP+SSN', 'DISP/B_MTH', 'DISP/SSN']

    def __init__(self, settings: Dict[str, str]):
        self.settings = settings
        self.mbq_density = int(settings.get('mbq_accessory_density', 3))
        self.mbq_sales_days = int(settings.get('mbq_sales_cover_days', 14))
        self.mbq_transit_days = int(settings.get('mbq_intransit_days', 3))
        self.mbq_scan_days = int(settings.get('mbq_scan_days', 2))
        self.default_mbq_type = settings.get('mbq_type', 'DISP')

    def cascade(self, bgt_majcat, bgt_seg, bgt_mvgr, store_priority, majcat,
                current_month=1, month_days=30, remaining_days=30, mbq_type_override=None):
        logger.info(f"[{majcat}] Budget cascade for month {current_month}")
        mbq_type = mbq_type_override or self.default_mbq_type

        # Resolve column names (handles Supabase v2srm + SQL Server formats)
        sales_col = next((c for c in [f'bgt_sales_m{current_month}', 'bgt_sales', 'BGT_SALES',
                         f'BGT_SALES_M{current_month}', 'bgt_sales_qty'] if c in bgt_majcat.columns), None)
        disp_col = next((c for c in [f'bgt_disp_m{current_month}', 'bgt_display', 'bgt_disp',
                         'BGT_DISPLAY', 'BGT_DISP'] if c in bgt_majcat.columns), None)
        majcat_col = next((c for c in ['majcat', 'MAJ_CAT', 'major_category'] if c in bgt_majcat.columns), None)
        st_col = next((c for c in ['st_cd', 'ST_CD', 'store_code', 'STORE_CODE'] if c in bgt_majcat.columns), None)

        if not sales_col or not st_col:
            logger.warning(f"[{majcat}] Missing budget columns. Sales: {sales_col}, Store: {st_col}")
            return pd.DataFrame()

        bgt = bgt_majcat[bgt_majcat[majcat_col] == majcat].copy() if majcat_col else bgt_majcat.copy()
        if bgt.empty:
            return pd.DataFrame()

        # Filter to listed stores
        listing_col = f'listing_m{current_month}'
        if not store_priority.empty:
            if listing_col in store_priority.columns:
                listed = store_priority[store_priority[listing_col] == 1]['st_cd'].unique()
            else:
                listed = store_priority[store_priority.get('is_active', pd.Series([1]*len(store_priority))) == 1]['st_cd'].unique()
            bgt = bgt[bgt[st_col].isin(listed)]

        logger.info(f"[{majcat}] {len(bgt)} stores with budget")

        # Segment contribution data
        seg_data = pd.DataFrame()
        if not bgt_seg.empty:
            seg_mc_col = next((c for c in ['majcat','MAJ_CAT'] if c in bgt_seg.columns), None)
            if seg_mc_col:
                seg_data = bgt_seg[bgt_seg[seg_mc_col] == majcat]

        results = []
        for _, row in bgt.iterrows():
            st_cd = row[st_col]
            total_sales = float(row.get(sales_col, 0) or 0)
            total_disp = float(row.get(disp_col, total_sales) or total_sales) if disp_col else total_sales
            if total_sales <= 0 and total_disp <= 0:
                continue

            base_density = 16
            if not store_priority.empty:
                sp = store_priority[store_priority['st_cd'] == st_cd]
                if not sp.empty:
                    base_density = int(sp.iloc[0].get('opt_density', 16) or 16)

            # Split by segment
            if not seg_data.empty:
                seg_st_col = next((c for c in ['st_cd','ST_CD'] if c in seg_data.columns), None)
                store_segs = seg_data[seg_data[seg_st_col] == st_cd] if seg_st_col else pd.DataFrame()
            else:
                store_segs = pd.DataFrame()

            if store_segs.empty:
                segments = [{'seg': 'ALL', 'cont_pct': 1.0, 'sales': total_sales, 'disp': total_disp}]
            else:
                segments = []
                for _, sr in store_segs.iterrows():
                    seg = sr.get('seg', sr.get('SEG', sr.get('dimension_value', 'ALL')))
                    pct = float(sr.get('bgt_cont_pct', 0) or 0)
                    s_sales = total_sales * pct if pct > 0 else 0
                    if s_sales > 0 or pct > 0:
                        segments.append({'seg': seg, 'cont_pct': pct, 'sales': s_sales,
                                        'disp': total_disp * pct})
                if not segments:
                    segments = [{'seg': 'ALL', 'cont_pct': 1.0, 'sales': total_sales, 'disp': total_disp}]

            for si in segments:
                seg, cont_pct = si['seg'], si['cont_pct']
                seg_sales, seg_disp = si['sales'], si['disp']

                # Conservative rounding (C.8)
                opt_count = max(1, conservative_round(base_density * cont_pct))
                sales_per_day = seg_sales / max(month_days, 1)
                disp_per_opt = seg_disp / max(opt_count, 1)

                # MBQ 7 types (C.6)
                disp_mbq = self.mbq_density + int(disp_per_opt)
                bgt_mth_mbq = int(sales_per_day * self.mbq_sales_days)
                ssn_mbq = int(sales_per_day * (self.mbq_sales_days + self.mbq_transit_days + self.mbq_scan_days))

                mbq_calc = {
                    'DISP': disp_mbq,
                    'B_MTH': bgt_mth_mbq,
                    'SSN': ssn_mbq,
                    'DISP+B_MTH': disp_mbq + bgt_mth_mbq,
                    'DISP+SSN': disp_mbq + ssn_mbq,
                    'DISP/B_MTH': max(disp_mbq, bgt_mth_mbq),
                    'DISP/SSN': max(disp_mbq, ssn_mbq),
                }
                mbq = mbq_calc.get(mbq_type, disp_mbq)

                results.append({
                    'st_cd': st_cd, 'majcat': majcat, 'seg': seg, 'macro_mvgr': '',
                    'bgt_disp_q': round(seg_disp, 2), 'opt_density': base_density,
                    'opt_count': opt_count, 'bgt_sales_per_day': round(sales_per_day, 4), 'mbq': mbq,
                })

        df = pd.DataFrame(results)
        if not df.empty:
            logger.info(f"[{majcat}] Budget cascade: {len(df)} rows, "
                        f"slots: {df['opt_count'].sum()}, MBQ type: {mbq_type}")
        return df

    @staticmethod
    def load_from_supabase(supabase_url, supabase_key, table_name='v2srm', majcat=None):
        """Load budget data from Supabase v2srm table."""
        import requests
        headers = {
            'apikey': supabase_key,
            'Authorization': f'Bearer {supabase_key}',
            'Content-Type': 'application/json',
        }
        url = f'{supabase_url}/rest/v1/{table_name}?select=*'
        if majcat:
            url += f'&majcat=eq.{majcat}'
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            df = pd.DataFrame(resp.json())
            logger.info(f"Loaded {len(df)} budget rows from Supabase {table_name}")
            return df
        except Exception as e:
            logger.error(f"Failed to load from Supabase: {e}")
            return pd.DataFrame()
