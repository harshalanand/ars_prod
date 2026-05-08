"""
Engine 4: Size Allocator
Breaks article-level (generic-color) allocations to variant/size SKU level.
Uses budget size contribution % to proportionally split quantities.
Constrained by DC size-level stock availability.

This replaces the VAR-ALLOC sheet in the old Excel system.
Merged with listing — no Excel row limit, variant-aware from start.
"""
import logging
from typing import Dict
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class SizeAllocator:
    """Break generic-color allocations to size-level variants."""

    def __init__(self, settings: Dict[str, str]):
        self.settings = settings

    def allocate(
        self,
        option_assignments: pd.DataFrame,
        dc_variant_stock: pd.DataFrame,
        bgt_size: pd.DataFrame,
        store_stock: pd.DataFrame,
        majcat: str,
    ) -> pd.DataFrame:
        """
        Break article-level allocations to size level.

        Args:
            option_assignments: From Engine 3 [st_cd, gen_art_color, disp_q, mbq, ...]
            dc_variant_stock: DC stock at variant level [gen_art_color, var_art, sz, stock_qty]
            bgt_size: Size contribution % [majcat, sz, bgt_cont_pct]
            store_stock: Store stock at variant level [st_cd, var_art, stock_qty]
            majcat: MAJCAT being processed

        Returns:
            DataFrame [st_cd, gen_art_color, var_art, sz, alloc_qty, hold_qty,
                       bgt_sz_cont_pct, dc_sz_stock, st_sz_stock, fill_rate_pct, short_qty, excess_qty]
        """
        if option_assignments.empty:
            return pd.DataFrame()

        logger.info(f"[{majcat}] Size allocation for {len(option_assignments)} option assignments")

        # Build size contribution % lookup
        size_pcts = {}
        if not bgt_size.empty:
            sz_data = bgt_size[bgt_size['majcat'] == majcat] if 'majcat' in bgt_size.columns else bgt_size
            for _, row in sz_data.iterrows():
                sz = row.get('sz', '')
                pct = float(row.get('bgt_cont_pct', 0) or 0)
                size_pcts[sz] = pct

        # Build DC variant stock lookup: {gen_art_color: [{var_art, sz, stock_qty}]}
        dc_var_map = {}
        if not dc_variant_stock.empty:
            for _, row in dc_variant_stock.iterrows():
                gac = row.get('gen_art_color', '')
                if gac not in dc_var_map:
                    dc_var_map[gac] = []
                dc_var_map[gac].append({
                    'var_art': row.get('var_art', ''),
                    'sz': row.get('sz', ''),
                    'stock_qty': int(row.get('stock_qty', 0) or 0),
                })

        # Build store stock lookup: {(st_cd, var_art): stock_qty}
        st_stock_map = {}
        if not store_stock.empty:
            for _, row in store_stock.iterrows():
                key = (row.get('st_cd', ''), row.get('var_art', ''))
                st_stock_map[key] = int(row.get('stock_qty', 0) or 0)

        # Track DC variant stock deductions globally
        dc_var_remaining = {}
        for gac, variants in dc_var_map.items():
            for v in variants:
                dc_var_remaining[(gac, v['var_art'])] = v['stock_qty']

        results = []

        for _, opt in option_assignments.iterrows():
            st_cd = opt['st_cd']
            gac = opt['gen_art_color']
            disp_q = int(opt.get('disp_q', 0) or 0)
            mbq = int(opt.get('mbq', 0) or 0)

            # Target quantity = max(disp_q, mbq)
            target_qty = max(disp_q, mbq, 1)

            # Get available variants for this article
            variants = dc_var_map.get(gac, [])
            if not variants:
                # No variant data — use sizes from Supabase size contribution (not generic defaults)
                # size_pcts already has the MAJCAT-specific sizes from Supabase
                if size_pcts:
                    use_sizes = [sz for sz, pct in size_pcts.items() if pct > 0.01]
                else:
                    use_sizes = ['M', 'L', 'XL', 'XXL']  # generic apparel fallback
                gen_art = str(opt.get('gen_art', ''))
                dc_per_size = max(1, int(opt.get('dc_stock_before', target_qty) / max(len(use_sizes), 1)))
                variants = [{'var_art': f'{gen_art}_{sz}', 'sz': sz, 'stock_qty': dc_per_size} for sz in use_sizes]
                for v in variants:
                    dc_var_remaining[(gac, v['var_art'])] = v['stock_qty']

            # Get MRP from option data
            article_mrp = float(opt.get('mrp', 0) or 0)

            # Calculate per-size allocation using contribution %
            total_pct = 0
            var_allocs = []
            for v in variants:
                sz = v['sz']
                pct = size_pcts.get(sz, 1.0 / max(len(variants), 1))
                dc_avail = dc_var_remaining.get((gac, v['var_art']), 0)
                st_stock = st_stock_map.get((st_cd, v['var_art']), 0)

                # Proportional allocation
                raw_alloc = max(0, int(round(target_qty * pct)))

                # Constrain by DC stock
                final_alloc = min(raw_alloc, dc_avail)

                # Adjust for existing store stock
                need = max(0, raw_alloc - st_stock)
                final_alloc = min(need, dc_avail)

                if final_alloc > 0:
                    dc_var_remaining[(gac, v['var_art'])] = dc_avail - final_alloc

                var_allocs.append({
                    'st_cd': st_cd,
                    'majcat': opt.get('majcat', ''),
                    'gen_art_color': gac,
                    'var_art': v['var_art'],
                    'sz': sz,
                    'alloc_qty': final_alloc,
                    'hold_qty': 0,
                    'mrp': article_mrp,
                    'bgt_sz_cont_pct': round(pct, 4),
                    'dc_sz_stock': dc_avail,
                    'st_sz_stock': st_stock,
                    'fill_rate_pct': round(final_alloc / max(raw_alloc, 1) * 100, 2),
                    'short_qty': max(0, raw_alloc - final_alloc),
                    'excess_qty': max(0, st_stock - raw_alloc) if st_stock > raw_alloc else 0,
                })

            # Only keep variants with actual allocation
            results.extend([v for v in var_allocs if v['alloc_qty'] > 0])

        df = pd.DataFrame(results) if results else pd.DataFrame()
        if not df.empty:
            total_alloc = df['alloc_qty'].sum()
            total_short = df['short_qty'].sum()
            logger.info(f"[{majcat}] Size allocation: {len(df)} variant rows, "
                         f"total alloc={total_alloc}, short={total_short}")
        return df
