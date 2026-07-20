"""
rule_engine_per_opt.py

Per-OPT sequential allocation engine. Replaces the cumulative-window race in
rule_engine_pandas._run_band with deterministic one-OPT-at-a-time allocation
with PRE-VALIDATED gates.

Activated by env var: ARS_PER_OPT_MODE=1. Defaults OFF — production behavior
unchanged unless the operator explicitly switches engines.

Design (locked through prior planning discussion):
  - One OPT at a time, all sizes together
  - OPT = (WERKS, GEN_ART_NUMBER, CLR) within an OPT_TYPE
  - Sort OPTs by OPT_PRIORITY_RANK ASC -> ST_RANK ASC -> WERKS ASC
  - Pre-validate every gate before consuming pool:
      * R07 live size-ratio (TBL only)
      * MJ_REQ_CAP per-WERKS budget
      * (SEC_CAP_PRE, PAK_SZ rounding deferred to follow-up)
  - RL/TBC: ship min(need, live_pool) per size; partial qty OK; draw from hold_dict first
  - TBL: if R07 passes, ship min(need, live_pool) per size with WH-buffer HOLD split
  - SKIP_REASON taxonomy: honest, no NO_POOL_MSA catch-all, no "race" language
      * R07_SIZE_RATIO_LIVE  (TBL OPT skipped at its turn)
      * MBQ_CAP_<RL/TBC/TBL> (pre-check exceeded budget)
      * POOL_EMPTY           (size had no live pool, with remarks for FNL_Q vs consumed_prior)
  - No convergence loop needed: all gates pre-validated, no post-loop refunds

Coexists with rule_engine_pandas. All Stage A/B logic, pool_dict build,
write-back, and ARS_LISTING_SESSIONS timing reuses the existing module.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from loguru import logger


def _safe_int(x: Any) -> int:
    """Coerce numeric-ish input to int, treating None/NaN as 0.

    Sec-cap remark text uses many int() casts for human-readable numbers.
    A single NaN leak would crash the whole MAJ_CAT worker. This helper
    is defence-in-depth so a future state-builder regression cannot take
    down a full /listing/generate run.
    """
    try:
        if x is None:
            return 0
        if isinstance(x, float) and math.isnan(x):
            return 0
    except Exception:
        return 0
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


# Same constants as rule_engine_pandas to keep the pool/opt key contract identical.
POOL_KEYS = ["RDC", "MAJ_CAT", "GEN_ART_NUMBER", "CLR", "VAR_ART", "SZ"]
OPT_KEYS = ["WERKS", "GEN_ART_NUMBER", "CLR"]


def _hold_suppress_arr(
    alloc_df: pd.DataFrame,
    idx,
    skip_hold_upc: bool = False,
    apply_hold_seg_app: bool = True,
    apply_hold_seg_gm: bool = True,
) -> Optional[np.ndarray]:
    """FS-06 hold-suppression predicate (FSD_FRESH_GRT_HOLD_CONTROL §7).

    Duplicated from rule_engine_pandas (deliberately — pandas lazily imports
    this module, so importing back at module level would risk a cycle).

    suppress(row) =  (skip_hold_upc        AND ST_STATUS == 'UPC')
                  OR (NOT apply_hold_seg_app AND SEG == 'APP')
                  OR (NOT apply_hold_seg_gm  AND SEG == 'GM')

    Returns None when every flag is at its default (no suppression active)
    so callers keep the pre-change fast path byte-identical. NULL/NaN
    ST_STATUS or SEG never suppresses (fillna('') before compare, E-04);
    a missing column behaves the same as all-NULL.
    """
    if not skip_hold_upc and apply_hold_seg_app and apply_hold_seg_gm:
        return None
    suppress = np.zeros(len(idx), dtype=bool)
    if skip_hold_upc and 'ST_STATUS' in alloc_df.columns:
        st = alloc_df.loc[idx, 'ST_STATUS'].fillna('').astype(str).to_numpy()
        suppress |= (st == 'UPC')
    if (not apply_hold_seg_app or not apply_hold_seg_gm) and 'SEG' in alloc_df.columns:
        seg = alloc_df.loc[idx, 'SEG'].fillna('').astype(str).to_numpy()
        if not apply_hold_seg_app:
            suppress |= (seg == 'APP')
        if not apply_hold_seg_gm:
            suppress |= (seg == 'GM')
    return suppress


def _mark_opt_skip(
    alloc_df: pd.DataFrame,
    idx: np.ndarray,
    reason: str,
    remark: str,
    live_pool: Optional[np.ndarray] = None,
) -> None:
    """Stamp every row of an OPT with SKIPPED status, SKIP_REASON, and a remark.

    When ``live_pool`` is supplied, also write the per-size live pool snapshot
    to FNL_Q_REM so the audit row reflects the pool that was actually visible
    at this OPT's turn (instead of the stale pre-allocation FNL_Q seed) — the
    pre-pool-take skip sites bypass the post-draw FNL_Q_REM write at 5f.1.

    SKIP_REASON is written only when currently empty, preserving whatever a
    more specific upstream gate already stamped on the row.
    """
    if len(idx) == 0:
        return
    alloc_df.loc[idx, 'ALLOC_STATUS'] = 'SKIPPED'
    prev = alloc_df.loc[idx, 'ALLOC_REMARKS'].fillna('').astype(str)
    alloc_df.loc[idx, 'ALLOC_REMARKS'] = prev + f' {reason}({remark});'
    if 'SKIP_REASON' in alloc_df.columns:
        cur = alloc_df.loc[idx, 'SKIP_REASON'].fillna('').astype(str)
        alloc_df.loc[idx, 'SKIP_REASON'] = cur.where(cur != '', reason)
    if live_pool is not None:
        alloc_df.loc[idx, 'FNL_Q_REM'] = live_pool.astype('float64')


def _mark_opt_skip_sec_cap(
    alloc_df: pd.DataFrame,
    idx: np.ndarray,
    grid_name: str,
    cap_pct: float,
    stk_val: float,
    ceiling_val: float,
    mbq_orig_val: float,
    run_before: float,
    intended_ship: float,
    budget: float,
    configured: bool = True,
    is_primary: bool = False,
    hard_block_reasons: Optional[List[str]] = None,
    grain: Optional[tuple] = None,
    cont_pct: Optional[float] = None,
    band_matched: bool = True,
    matrix_enabled: bool = False,
) -> None:
    """Sec-cap / primary-cap block (per-OPT, pre-pool-take). REPLACES
    ALLOC_REMARKS (the waterfall never stamped a 'B[…] ship=N' prefix for
    this OPT) and sets SKIP_REASON to the canonical
        - PRIMARY_CAP_PRE_<grid>(cap=N%)  when the offending grid is Primary
        - SEC_CAP_PRE_<grid>(cap=N%)      when it is Secondary
        - <reason1>,<reason2>             when hard_block_reasons is set
    so dashboards can tell the two apart at a glance.

    Hard-block branch (new, 2026-06-30): when `hard_block_reasons` is set,
    SKIP_REASON is the comma-joined tag list (e.g.
    "SEC_CAP_GRID_NULL[MJ_M_YARN_02],SEC_CAP_MBQ_ZERO[MJ_M_YARN_02]") and
    ALLOC_REMARKS narrates which of the three empty-data conditions tripped.
    See docs/superpowers/specs/2026-06-30-sec-cap-empty-grid-hard-block-design.md.

    Three remark branches (when not hard-block):
      configured=False  → grain has no MBQ_ORIG configured (NULL); strict
                          mode blocks any dispatch into this grain.
      budget <= 0       → grain already at/over the cap ceiling; no room.
      else              → grain has room but this OPT pushes past it.

    All numeric formatting goes through _safe_int so any stray NaN/None
    that slips into the breach dict prints as 0 instead of crashing the
    worker subprocess.
    """
    if len(idx) == 0:
        return
    if hard_block_reasons:
        skip_reason = ",".join(hard_block_reasons)
        grain_str = f"({','.join(str(g) for g in grain)})" if grain else "(?)"
        remark = (
            f"SKIPPED by sec-cap hard-block | grid={grid_name}"
            f" | reasons={skip_reason}"
            f" | grain={grain_str}"
            f" | intended_ship={_safe_int(intended_ship)} -> final_ship=0"
            f" | pool_untouched=true"
        )
        alloc_df.loc[idx, 'ALLOC_STATUS'] = 'SKIPPED'
        if 'SKIP_REASON' in alloc_df.columns:
            cur = alloc_df.loc[idx, 'SKIP_REASON'].fillna('').astype(str)
            alloc_df.loc[idx, 'SKIP_REASON'] = cur.where(cur != '', skip_reason)
        alloc_df.loc[idx, 'ALLOC_REMARKS'] = remark
        return
    cap_str = f"{_safe_int(cap_pct)}%"
    prefix = "PRIMARY_CAP_PRE_" if is_primary else "SEC_CAP_PRE_"
    label  = "primary-cap" if is_primary else "sec-cap"
    # Include cont% in the skip-reason label when the matrix path resolved
    # a band for this grain. Format matches spec 2026-07-08 §4.3:
    #   SEC_CAP_PRE_<grid>(cap=200%,cont=13%)
    if matrix_enabled and cont_pct is not None and band_matched:
        cont_str = f"{_safe_int(cont_pct)}%"
        skip_reason = f"{prefix}{grid_name}(cap={cap_str},cont={cont_str})"
    else:
        skip_reason = f"{prefix}{grid_name}(cap={cap_str})"
    # Matrix was enabled but no band matched this grain's cont% → operator
    # sees SEC_CAP_MATRIX_GAP[<grid>] appended so the fallback is visible.
    if matrix_enabled and not band_matched:
        skip_reason = f"{skip_reason},SEC_CAP_MATRIX_GAP[{grid_name}]"
    if not configured:
        remark = (
            f"SKIPPED by {label} | grid={grid_name}"
            f" | MBQ_ORIG IS NULL at this grain (no cap configured)"
            f" | strict mode = block any dispatch"
            f" | intended_ship={_safe_int(intended_ship)} -> final_ship=0"
            f" | pool_untouched=true"
        )
    elif budget <= 0:
        remark = (
            f"SKIPPED by {label} | grid={grid_name}"
            f" | grain already at stock {_safe_int(stk_val)} which meets/exceeds ceiling "
            f"{_safe_int(ceiling_val)}"
            f" (= MBQ_ORIG {_safe_int(mbq_orig_val)} x {cap_str})"
            f" | no room for new dispatch"
            f" | intended_ship={_safe_int(intended_ship)} -> final_ship=0"
            f" | pool_untouched=true"
        )
    else:
        total_after = _safe_int(stk_val + run_before + intended_ship)
        remark = (
            f"SKIPPED by {label} | grid={grid_name}"
            f" | stock {_safe_int(stk_val)} + already_shipped_this_run {_safe_int(run_before)}"
            f" + this_OPT {_safe_int(intended_ship)} = {total_after}"
            f" would exceed ceiling {_safe_int(ceiling_val)}"
            f" (= MBQ_ORIG {_safe_int(mbq_orig_val)} x {cap_str})"
            f" by {total_after - _safe_int(ceiling_val)}"
            f" | intended_ship={_safe_int(intended_ship)} -> final_ship=0"
            f" | pool_untouched=true"
        )
    alloc_df.loc[idx, 'ALLOC_STATUS'] = 'SKIPPED'
    if 'SKIP_REASON' in alloc_df.columns:
        # Only stamp SKIP_REASON if it is currently empty — preserves whatever
        # an earlier gate (R07, MBQ_CAP, TBL_MJ_REQ_GATE) already wrote.
        cur = alloc_df.loc[idx, 'SKIP_REASON'].fillna('').astype(str)
        alloc_df.loc[idx, 'SKIP_REASON'] = cur.where(cur != '', skip_reason)
    # REPLACE the remark — pool was never touched, so there is no waterfall
    # narrative worth preserving for these rows.
    alloc_df.loc[idx, 'ALLOC_REMARKS'] = remark


def _stamp_sec_cap_override(
    alloc_df: pd.DataFrame,
    idx: np.ndarray,
    info: Dict[str, Any],
) -> None:
    """APPEND a SEC_CAP_OVERSHOOT narrative to ALLOC_REMARKS — the
    natural waterfall trace stays intact, the admit info is added so
    reviewers can see this OPT shipped INTO a capped grain because the
    breach was <= 0.5 × intended_ship (unified overshoot rule,
    2026-07-04). Applies to both Primary and Secondary grids.

    Status / SKIP_REASON are NOT touched — the OPT remains ALLOCATED /
    PARTIAL based on what actually shipped."""
    if len(idx) == 0:
        return
    grid            = str(info.get("grid", ""))
    threshold_i     = _safe_int(info.get("threshold", 0))
    ceiling_i       = _safe_int(info.get("ceiling", 0))
    stk_i           = _safe_int(info.get("stk", 0))
    runb_i          = _safe_int(info.get("run_before", 0))
    intended_i      = _safe_int(info.get("intended_ship", 0))
    overshoot_i     = _safe_int(info.get("overshoot", 0))
    matrix_enabled  = bool(info.get("matrix_enabled", False))
    band_matched    = bool(info.get("band_matched", True))
    cont_pct        = info.get("cont_pct")
    # Effective cap for the label: matrix path uses the resolved growth%;
    # legacy path uses the grid's flat cap_pct.
    cap_pct_i = _safe_int(
        info.get("growth_pct") if (matrix_enabled and band_matched) else info.get("cap_pct", 0)
    )
    cap_clause = f"cap={cap_pct_i}%"
    if matrix_enabled and band_matched and cont_pct is not None:
        cap_clause += f",cont={_safe_int(cont_pct)}%"
    note = (
        f" SEC_CAP_OVERSHOOT(grid={grid}, {cap_clause}"
        f", reason=overshoot({overshoot_i}) <= 0.5xintended({intended_i})={threshold_i}"
        f", grain_stk={stk_i}, grain_ceiling={ceiling_i}"
        f", running_before={runb_i}, this_OPT_intended={intended_i}"
        f", would_have_blocked=true);"
    )
    if matrix_enabled and not band_matched:
        note += f" SEC_CAP_MATRIX_GAP[{grid}];"
    prev = alloc_df.loc[idx, 'ALLOC_REMARKS'].fillna('').astype(str)
    alloc_df.loc[idx, 'ALLOC_REMARKS'] = prev + note


def build_sec_cap_state(
    working_df: pd.DataFrame,
    grid_specs: List[Tuple[str, Dict[str, Any]]],
    matrix_enabled: bool = False,
    matrix_bands: Optional[List[Tuple[float, Optional[float], float]]] = None,
) -> Dict[str, Any]:
    """Build per-OPT sec-cap state from the in-memory working_df slice.

    grid_specs is a list of (grid_name, meta) tuples where meta carries:
        prefix       — column-name prefix on working_df (e.g. "MJ", "M_YARN_02")
        extras       — list of extra grain columns (e.g. ["M_YARN_02"], [] for MJ)
        cap_factor   — sec_cap_pct / 100.0
        cap_pct      — sec_cap_pct (for remarks)
        gh_col       — GH_<HC> column name (or "" if none)

    matrix_enabled / matrix_bands (spec 2026-07-08): when both are supplied and
    enabled=True, each grain's ceiling is derived from a cont%-band lookup
    (grain's MBQ share of MAJ_CAT total → growth%) INSTEAD of the flat
    per-grid `cap_factor`. Small contributors get a bigger stretch; dominant
    grains stay at their cap. When enabled=False (default) behavior is
    byte-identical to the pre-matrix implementation.

    Returns:
        {
            "grids":            grid_specs (passed through),
            "budgets":          {grid_name: {grain_tuple: max(0, ceiling − stk)}},
            "ceilings":         {grid_name: {grain_tuple: MBQ_ORIG × cap_factor}},
            "stks":             {grid_name: {grain_tuple: STK_TTL}},
            "mbqs":             {grid_name: {grain_tuple: MBQ_ORIG}},
            "configured":       {grid_name: {grain_tuple: bool}},   # False iff MBQ_ORIG was NULL
            "running":          {grid_name: defaultdict(float)},    # mutated as OPTs ship
            "gh_applies":       {grid_name: {maj_cat: bool}},
            "cont_pcts":        {grid_name: {grain_tuple: cont%}},    # matrix path only
            "resolved_growths": {grid_name: {grain_tuple: growth%}},  # matrix or grid-flat
            "band_matched":     {grid_name: {grain_tuple: bool}},     # False → fell back
            "matrix_enabled":   bool,
        }

    Distinction between NULL and explicit-zero MBQ matters: a NULL MBQ_ORIG
    is a data gap and is treated by the gate as a strict block (the
    `configured` flag goes False); an explicit MBQ_ORIG=0 was invariant 3
    ("no constraint at this grain"). As of 2026-06-30 (per design spec
    docs/superpowers/specs/2026-06-30-sec-cap-empty-grid-hard-block-design.md)
    both NULL and explicit-zero MBQ — plus a NULL/'NA' grid-extra value — are
    hard-blocked through the `hard_block` dict.
    """
    state: Dict[str, Any] = {
        "grids":            grid_specs,
        "budgets":          {},
        "ceilings":         {},
        "stks":             {},
        "mbqs":             {},
        "configured":       {},
        "hard_block":       {},
        "running":          {g_name: defaultdict(float) for g_name, _ in grid_specs},
        "gh_applies":       {},
        "cont_pcts":        {},
        "resolved_growths": {},
        "band_matched":     {},
        "matrix_enabled":   bool(matrix_enabled and matrix_bands),
    }
    if working_df is None or working_df.empty:
        return state

    work_cols_upper = {c.upper(): c for c in working_df.columns}

    def _col(name: str) -> Optional[str]:
        return work_cols_upper.get(name.upper())

    # Import here to avoid a hard import cycle on module load; the matrix
    # module has no reverse dependency on rule_engine_per_opt but we keep
    # the surface minimal.
    if state["matrix_enabled"]:
        from app.services.sec_cap_growth_matrix import resolve_growth
    else:
        resolve_growth = None  # type: ignore[assignment]

    for g_name, g_meta in grid_specs:
        prefix = g_meta.get("prefix") or g_name
        extras = list(g_meta.get("extras") or [])
        cap_factor = float(g_meta.get("cap_factor") or 1.30)
        grid_default_pct = cap_factor * 100.0
        mbq_orig_col = _col(f"{prefix}_MBQ_ORIG")
        mbq_live_col = _col(f"{prefix}_MBQ")
        anchor_col = mbq_orig_col or mbq_live_col
        if not anchor_col:
            continue  # grid not materialised on working_df
        stk_col = _col(f"{prefix}_STK_TTL")
        gh_col = g_meta.get("gh_col") or ""
        gh_col = _col(gh_col) if gh_col else None
        # grain keys must exist on working_df
        grain_cols = ["WERKS", "MAJ_CAT"] + extras
        if not all(_col(k) for k in grain_cols):
            continue
        grain_cols_resolved = [_col(k) for k in grain_cols]

        # Aggregate by grain (one MBQ/STK per grain). max() is fine — every row
        # in a grain carries the same value by construction.
        agg_cols = [anchor_col]
        if stk_col:
            agg_cols.append(stk_col)
        try:
            agg = (working_df[grain_cols_resolved + agg_cols]
                   .groupby(grain_cols_resolved, sort=False, observed=True)
                   .max())
        except Exception as e:
            logger.warning(f"[sec_cap_per_opt] state build failed for {g_name}: {e}")
            continue

        # MAJ_CAT totals for cont% (matrix path only). Sum grain MBQs per
        # (WERKS, MAJ_CAT). Cheap groupby on the already-aggregated frame.
        # Handled once per grid — the total is grid-specific because each
        # grid's grain granularity differs (MJ_FAB rolls into fewer grains
        # per MAJ_CAT than MJ_M_YARN_02).
        maj_total: Dict[Tuple[str, str], float] = {}
        if state["matrix_enabled"]:
            try:
                mt = (agg[anchor_col]
                      .groupby(level=[0, 1] if agg.index.nlevels >= 2 else [0])
                      .sum())
                # 1-level (WERKS-only) is unreachable in practice since grain
                # always includes MAJ_CAT, but guard the branch anyway.
                if agg.index.nlevels >= 2:
                    for (w, mc), v in mt.items():
                        maj_total[(str(w), str(mc))] = float(v)
                else:
                    for w, v in mt.items():
                        maj_total[(str(w), "")] = float(v)
            except Exception as _e:
                logger.warning(
                    f"[sec_cap_matrix] MAJ_CAT total roll-up failed for {g_name}: {_e}; "
                    f"falling back to grid-flat cap for this grid"
                )
                maj_total = {}

        bmap: Dict[tuple, float] = {}
        cmap: Dict[tuple, float] = {}
        smap: Dict[tuple, float] = {}
        mmap: Dict[tuple, float] = {}
        fmap: Dict[tuple, bool]  = {}
        hbmap: Dict[tuple, List[str]] = {}
        cpmap: Dict[tuple, float] = {}   # cont%
        gpmap: Dict[tuple, float] = {}   # resolved growth%
        gbmap: Dict[tuple, bool]  = {}   # band matched?
        # Positions 0/1 in grain are WERKS/MAJ_CAT (always real). Positions
        # 2+ are extras (M_YARN_02, M_DESIGN_02, …) — those are the ones we
        # check for empty/'NA'/null.
        extras_start = 2
        for grain_idx, row in agg.iterrows():
            grain = grain_idx if isinstance(grain_idx, tuple) else (grain_idx,)
            grain = tuple(str(g) if g is not None else '' for g in grain)
            # `or 0` does NOT trap NaN (NaN is truthy in Python). Use pd.isna
            # so NULL MBQ_ORIG / NULL STK_TTL become a clean 0 and never leak
            # NaN into the state dicts where they'd later blow up int() casts.
            _mbq_raw = row[anchor_col]
            mbq_null = pd.isna(_mbq_raw)
            mbq_val = 0.0 if mbq_null else float(_mbq_raw)

            # Resolve growth% — matrix (per-grain cont%) or grid-flat cap.
            # Matrix path only fires when the toggle is on AND we have a
            # valid MAJ_CAT total for this grain; otherwise we degrade to
            # the pre-matrix flat cap so behaviour stays byte-identical
            # with the toggle off.
            cont_pct = 0.0
            band_matched = True
            if state["matrix_enabled"] and maj_total and not mbq_null:
                total = maj_total.get((grain[0], grain[1]), 0.0)
                cont_pct = (mbq_val / total * 100.0) if total > 0 else 0.0
                growth_pct, band_matched = resolve_growth(
                    cont_pct, matrix_bands or [], grid_default_pct
                )
                cap_factor_grain = growth_pct / 100.0
            else:
                growth_pct = grid_default_pct
                cap_factor_grain = cap_factor

            ceiling = mbq_val * cap_factor_grain
            if stk_col:
                _stk_raw = row[stk_col]
                stk_val = 0.0 if pd.isna(_stk_raw) else float(_stk_raw)
            else:
                stk_val = 0.0
            bmap[grain] = max(0.0, ceiling - stk_val)
            cmap[grain] = ceiling
            smap[grain] = stk_val
            mmap[grain] = mbq_val
            fmap[grain] = not mbq_null
            cpmap[grain] = cont_pct
            gpmap[grain] = growth_pct
            gbmap[grain] = band_matched
            # Hard-block reasons (per design spec 2026-06-30):
            #   1) grid extra value is empty / 'NA' / 'NONE'
            #   2) explicit MBQ_ORIG = 0
            #   3) MBQ_ORIG IS NULL
            # All three are independent and may co-occur on the same grain.
            reasons: List[str] = []
            if extras:
                for ev in grain[extras_start:]:
                    if ev is None or str(ev).upper().strip() in ('', 'NA', 'NONE', 'NAN'):
                        reasons.append(f"SEC_CAP_GRID_NULL[{g_name}]")
                        break  # one tag per grid is enough
            if not mbq_null and mbq_val == 0.0:
                reasons.append(f"SEC_CAP_MBQ_ZERO[{g_name}]")
            elif mbq_null:
                reasons.append(f"SEC_CAP_NULL[{g_name}]")
            if reasons:
                hbmap[grain] = reasons
        state["budgets"][g_name]          = bmap
        state["ceilings"][g_name]         = cmap
        state["stks"][g_name]             = smap
        state["mbqs"][g_name]             = mmap
        state["configured"][g_name]       = fmap
        state["hard_block"][g_name]       = hbmap
        state["cont_pcts"][g_name]        = cpmap
        state["resolved_growths"][g_name] = gpmap
        state["band_matched"][g_name]     = gbmap

        # GH_<HC> applicability map per MAJ_CAT
        if gh_col:
            try:
                gh_agg = (working_df[["MAJ_CAT", gh_col]]
                          .groupby("MAJ_CAT", sort=False, observed=True)
                          .max())
                state["gh_applies"][g_name] = {
                    str(mc): bool(int(v or 0))
                    for mc, v in gh_agg[gh_col].items()
                }
            except Exception:
                state["gh_applies"][g_name] = {}
        else:
            state["gh_applies"][g_name] = {}

    return state


def _evaluate_sec_cap_per_opt(
    sec_cap_state: Dict[str, Any],
    opt_rows: pd.DataFrame,
    werks_v: Any,
    intended_ship: float,
    mj_req_rem_dict: Optional[Dict[str, float]] = None,
    mbq_gate_factor: float = 0.5,
) -> Tuple[Optional[Dict[str, Any]], List[Tuple[str, tuple]]]:
    """Walk every active grid for this OPT.

    Returns:
        (breach_info_or_None, participating)
            breach_info: dict with `action` ∈ {"block", "override"} plus
                grid/grain/budget/ceiling/stk/mbq/run_before/etc.
                None when no grid breaches.
            participating: list of (grid_name, grain_tuple) for grids the OPT
                touches — used to advance `running` after admission.

    Two-pass evaluation (2026-07-13):

    Pass 1 — veto scan (order-independent). Iterate every applicable grid
        (same GH_<HC> applicability guard as Pass 2) and collect hard_block
        reasons (2026-06-30 spec: empty grid extra, explicit MBQ_ORIG=0,
        NULL MBQ_ORIG). If any grid vetoes, return action="block" with the
        combined hard_block_reasons list; `participating` stays empty so
        `running` is NOT advanced for any grid the OPT touched.

    Pass 2 — breach scan (Primary-first, unchanged from 2026-07-04). Only
        reached when Pass 1 finds no veto. On breach
        (`run_before + intended_ship > budget`) compute
            overshoot = run_before + intended_ship − budget
        and admit when
            configured  AND  overshoot > 0  AND  overshoot <= 0.5 × intended_ship
        i.e. one boundary OPT per grain per band may breach by up to
        ½ × its own intended ship. After admission the 5g.1 `running`
        advance pushes the grain past budget, so the next OPT's overshoot
        inevitably exceeds ½ × its intended and hard-blocks. NULL-MBQ grains
        (`configured=False`) stay strict-block for data-gap detection.
        Primary grids (group='Primary' — today MJ and MJ_MERGE_RNG_SEG) use
        the same rule as Secondary; the only difference is the SKIP_REASON
        label carries `primary_block=True` when the block path is taken.

    Invariant: veto > override. A hard-block on ANY applicable grid vetoes
        the OPT regardless of what any other grid would admit via overshoot.
    """
    participating: List[Tuple[str, tuple]] = []
    if intended_ship <= 0 or not sec_cap_state.get("grids"):
        return None, participating
    row0 = opt_rows.iloc[0]
    maj_cat = str(row0.get("MAJ_CAT", ""))

    # =========================================================================
    # Pass 1 — veto scan (order-independent).
    # Any applicable grid carrying a hard-block reason (2026-06-30 spec: empty
    # grid extra, explicit MBQ_ORIG=0, NULL MBQ_ORIG) vetoes the OPT regardless
    # of what an earlier grid would admit via the Primary overshoot allowance.
    # Runs BEFORE Pass 2 so an overshoot admit can never shadow a downstream
    # veto. The GH_<HC> applicability guard is re-applied here so a grid that
    # does not apply to this MAJ_CAT (GH=0) cannot veto.
    # Ref: docs/superpowers/specs/2026-07-13-sec-cap-hard-block-precedence-design.md
    # =========================================================================
    vetoing: List[Tuple[str, tuple, Dict[str, Any], List[str]]] = []
    for g_name, g_meta in sec_cap_state["grids"]:
        gh_map = sec_cap_state["gh_applies"].get(g_name, {})
        if gh_map and not gh_map.get(maj_cat, True):
            continue
        extras = list(g_meta.get("extras") or [])
        grain = tuple([str(werks_v), maj_cat] + [str(row0.get(e, "")) for e in extras])
        hb_reasons = sec_cap_state.get("hard_block", {}).get(g_name, {}).get(grain)
        if hb_reasons:
            vetoing.append((g_name, grain, g_meta, list(hb_reasons)))

    if vetoing:
        v_grid, v_grain, v_meta, _ = vetoing[0]
        combined_reasons: List[str] = []
        for _, _, _, rs in vetoing:
            for rsn in rs:
                if rsn not in combined_reasons:
                    combined_reasons.append(rsn)
        info = {
            "action":             "block",
            "grid":               v_grid,
            "grain":              v_grain,
            "budget":             sec_cap_state["budgets"].get(v_grid, {}).get(v_grain, 0.0),
            "ceiling":            sec_cap_state["ceilings"].get(v_grid, {}).get(v_grain, 0.0),
            "stk":                sec_cap_state["stks"].get(v_grid, {}).get(v_grain, 0.0),
            "mbq":                sec_cap_state["mbqs"].get(v_grid, {}).get(v_grain, 0.0),
            "cap_pct":            float(v_meta.get("cap_pct") or 100.0),
            "run_before":         sec_cap_state["running"][v_grid].get(v_grain, 0.0),
            "configured":         sec_cap_state.get("configured", {}).get(v_grid, {}).get(v_grain, True),
            "mj_req_rem":         float((mj_req_rem_dict or {}).get(str(werks_v), 0.0)),
            "opt_mbq":            0.0,
            "threshold":          0.0,
            "intended_ship":      intended_ship,
            "hard_block_reasons": combined_reasons,
            # matrix keys preserved (added post-FSD; read by _stamp_* narrative)
            "cont_pct":           sec_cap_state.get("cont_pcts", {}).get(v_grid, {}).get(v_grain, 0.0),
            "growth_pct":         sec_cap_state.get("resolved_growths", {}).get(v_grid, {}).get(v_grain, float(v_meta.get("cap_pct") or 100.0)),
            "band_matched":       sec_cap_state.get("band_matched", {}).get(v_grid, {}).get(v_grain, True),
            "matrix_enabled":     bool(sec_cap_state.get("matrix_enabled", False)),
        }
        # participating stays empty — vetoed OPTs contribute no ship, so no
        # `running` advance should happen for any grid they touched.
        return info, participating

    # =========================================================================
    # Pass 2 — breach scan (Primary-first). Only reached when Pass 1 found no
    # veto. Hard-block branch removed (handled above in Pass 1).
    # =========================================================================
    for g_name, g_meta in sec_cap_state["grids"]:
        gh_map = sec_cap_state["gh_applies"].get(g_name, {})
        if gh_map and not gh_map.get(maj_cat, True):
            continue
        extras = list(g_meta.get("extras") or [])
        grain = tuple([str(werks_v), maj_cat] + [str(row0.get(e, "")) for e in extras])
        # `configured` is False ONLY when MBQ_ORIG was NULL at this grain.
        # Default True so legacy callers that don't populate the map still
        # behave as before. The flag lets us distinguish NULL (strict block)
        # from explicit MBQ=0 (invariant 3: skip grid for this OPT).
        configured = sec_cap_state.get("configured", {}).get(g_name, {}).get(grain, True)
        ceiling = sec_cap_state["ceilings"].get(g_name, {}).get(grain, 0.0)
        if configured and ceiling <= 0:
            continue  # explicit MBQ=0 → invariant 3: no constraint at this grain
        budget = sec_cap_state["budgets"].get(g_name, {}).get(grain, 0.0)
        run_before = sec_cap_state["running"][g_name].get(grain, 0.0)
        participating.append((g_name, grain))
        breach_now = (not configured) or (run_before + intended_ship > budget)
        if breach_now:
            # Primary grids (ARS_GRID_BUILDER.grid_group='Primary' — today
            # MJ and MJ_MERGE_RNG_SEG) hard-block on breach EXCEPT for the
            # 2026-07-04 overshoot allowance: when the excess is small
            # relative to the boundary OPT (overshoot <= 0.5 × intended),
            # admit the OPT via action="override". The 5g.1 running
            # advance then pushes the grain past budget, so the next OPT
            # in this grain naturally fails the same 0.5 × intended check
            # and hard-blocks. Bounded to one OPT per grain per band.
            # NULL-MBQ grains (configured=False) stay strict-block for
            # data-gap detection. Secondary grids continue to use the
            # headroom-based override path below.
            if bool(g_meta.get("is_primary", False)):
                overshoot = float(run_before + intended_ship - budget)
                primary_threshold = 0.5 * intended_ship
                overshoot_admit_ok = (
                    configured
                    and overshoot > 0
                    and overshoot <= primary_threshold
                )
                action = "override" if overshoot_admit_ok else "block"
                info = {
                    "action":        action,
                    "grid":          g_name,
                    "grain":         grain,
                    "budget":        budget,
                    "ceiling":       ceiling if configured else 0.0,
                    "stk":           sec_cap_state["stks"].get(g_name, {}).get(grain, 0.0),
                    "mbq":           sec_cap_state["mbqs"].get(g_name, {}).get(grain, 0.0),
                    "cap_pct":       float(g_meta.get("cap_pct") or 100.0),
                    "run_before":    run_before,
                    "configured":    configured,
                    "mj_req_rem":    float((mj_req_rem_dict or {}).get(str(werks_v), 0.0)),
                    "opt_mbq":       0.0,
                    "threshold":     primary_threshold,
                    "intended_ship": intended_ship,
                    "overshoot":     overshoot,
                    "cont_pct":      sec_cap_state.get("cont_pcts", {}).get(g_name, {}).get(grain, 0.0),
                    "growth_pct":    sec_cap_state.get("resolved_growths", {}).get(g_name, {}).get(grain, float(g_meta.get("cap_pct") or 100.0)),
                    "band_matched":  sec_cap_state.get("band_matched", {}).get(g_name, {}).get(grain, True),
                    "matrix_enabled": bool(sec_cap_state.get("matrix_enabled", False)),
                }
                if action == "block":
                    info["primary_block"] = True
                else:
                    info["overshoot_admit"] = True
                return info, participating
            # Override decision (Secondary) — unified overshoot rule
            # (2026-07-04): admit when breach <= 0.5 × intended_ship.
            # Same shape as sec-cap Primary and primary MBQ_CAP so cap
            # semantics are consistent across all gates and OPT_TYPEs.
            # NULL-MBQ grains stay strict-block for data-gap detection.
            overshoot = float(run_before + intended_ship - budget)
            threshold = 0.5 * intended_ship
            override_eligible = (
                configured                       # never override a NULL-MBQ block
                and overshoot > 0
                and overshoot <= threshold
            )
            action = "override" if override_eligible else "block"
            info = {
                "action":        action,
                "grid":          g_name,
                "grain":         grain,
                "budget":        budget,
                "ceiling":       ceiling if configured else 0.0,
                "stk":           sec_cap_state["stks"].get(g_name, {}).get(grain, 0.0),
                "mbq":           sec_cap_state["mbqs"].get(g_name, {}).get(grain, 0.0),
                "cap_pct":       float(g_meta.get("cap_pct") or 130.0),
                "run_before":    run_before,
                "configured":    configured,
                "mj_req_rem":    float((mj_req_rem_dict or {}).get(str(werks_v), 0.0)),
                "opt_mbq":       0.0,
                "threshold":     threshold,
                "intended_ship": intended_ship,
                "overshoot":     overshoot,
                "cont_pct":      sec_cap_state.get("cont_pcts", {}).get(g_name, {}).get(grain, 0.0),
                "growth_pct":    sec_cap_state.get("resolved_growths", {}).get(g_name, {}).get(grain, float(g_meta.get("cap_pct") or 130.0)),
                "band_matched":  sec_cap_state.get("band_matched", {}).get(g_name, {}).get(grain, True),
                "matrix_enabled": bool(sec_cap_state.get("matrix_enabled", False)),
            }
            if action == "override":
                info["overshoot_admit"] = True
            return info, participating
    return None, participating


def _run_band_per_opt(
    alloc_df: pd.DataFrame,
    pool_dict: Dict[Tuple, float],
    ot: str,
    r: int,
    mbq_budget: Optional[Dict[str, float]] = None,
    hold_dict: Optional[Dict[Tuple, float]] = None,
    size_threshold: float = 0.6,
    min_size_count: int = 3,
    mj_req_rem_dict: Optional[Dict[str, float]] = None,
    tbl_mj_req_cap_pct: float = 100.0,
    mbq_gate_factor: float = 0.5,
    sec_cap_state: Optional[Dict[str, Any]] = None,
    rl_dispatch_mode: str = 'COMPLETE',
    tbc_dispatch_mode: str = 'COMPLETE',
    # ── Fresh/GRT hold control (FSD_FRESH_GRT_HOLD_CONTROL) ──
    # alloc_type is accepted for contract parity with the pandas engine;
    # it is not read here — hold_dict already arrives filtered to
    # (ALLOC_TYPE = run type OR legacy '') by the loader in
    # rule_engine_pandas._pandas_run_one_majcat (FS-09). The three flags
    # drive the FS-06 hold-suppression mask at the TBL SHIP/HOLD split
    # (5c.5). Defaults keep behaviour byte-identical.
    alloc_type: str = "FRESH",
    skip_hold_upc: bool = False,
    apply_hold_seg_app: bool = True,
    apply_hold_seg_gm: bool = True,
) -> None:
    """
    Per-OPT replacement for rule_engine_pandas._run_band.

    Signature and side-effects are identical to _run_band plus three extra
    knobs that drive the TBL MJ_REQ_GATE pre-check (Fix B):

      mj_req_rem_dict       — {WERKS: MJ_REQ_REM} for the current MAJ_CAT slice.
                              Decremented in-place as TBL OPTs ship so the next
                              OPT in the same WERKS sees an updated remainder.
      tbl_mj_req_cap_pct    — same value as run_listing_and_allocation_pandas
                              `tbl_mj_req_cap_pct` (default 100). Used to
                              compute `cap_pct/100 * MJ_REQ_REM`.
      mbq_gate_factor       — the user's "0.5" factor. An OPT passes the gate
                              iff `cap_pct/100 * MJ_REQ_REM >= factor * OPT_MBQ`.

    Each OPT decides everything at its turn:
      1. R07 live (TBL): skip if too few sizes have pool
      2. TBL MJ_REQ_GATE: skip if cap_pct*MJ_REQ_REM < factor*OPT_MBQ
      3. MJ_REQ_CAP (per-WERKS budget): skip TBL strictly / scale RL+TBC
      4. Allocate per size: take min(need, live_pool); RL+TBC draw from hold
      5. Update live pool, mbq_budget, mj_req_rem_dict
      6. Stamp honest SKIP_REASON for any size that ended at SHIP=0
    """
    # 1) Eligible row mask — identical to _run_band line 1588.
    mask = (
        (alloc_df['OPT_TYPE'] == ot)
        & (alloc_df['I_ROD'] >= r)
        & (~alloc_df['ALLOC_STATUS'].isin(['SKIPPED', 'INELIGIBLE']))
    )
    if not mask.any():
        return

    # 2) Snapshot the eligible slice into a working DataFrame keyed by the
    #    original alloc_df index, so write-back at the end can use .loc[idx].
    cols = [
        'WERKS', 'RDC', 'MAJ_CAT', 'GEN_ART_NUMBER', 'CLR', 'VAR_ART', 'SZ',
        'OPT_PRIORITY_RANK', 'ST_RANK', 'IS_NEW',
        'SZ_MBQ', 'SZ_MBQ_WH', 'SZ_STK', 'I_ROD',
        'POOL_CONSUMED', 'SHIP_QTY',
        'OPT_MBQ',   # for Fix B (TBL MJ_REQ_GATE pre-check)
    ]
    # PAK_SZ is optional in some upstream pulls; include only if present so
    # the in-loop pak rounding step downstream has access to it (no-op when absent).
    if 'PAK_SZ' in alloc_df.columns:
        cols.append('PAK_SZ')
    # When sec-cap state is supplied, pull every grid's extras column too
    # so per-OPT grain lookup ({WERKS}, {MAJ_CAT}, {extras…}) sees real
    # values instead of '' (would otherwise miss the budget map → ceiling=0
    # → silent no-block).
    if sec_cap_state is not None:
        for _g_name, _g_meta in (sec_cap_state.get("grids") or []):
            for _ex in (_g_meta.get("extras") or []):
                if _ex in alloc_df.columns and _ex not in cols:
                    cols.append(_ex)
    sub = alloc_df.loc[mask, cols].copy()
    sub['_orig_idx'] = sub.index  # preserve for write-back

    # 3) Compute need_pool and need_ship per row — same formulas as _run_band.
    sz_mbq = sub['SZ_MBQ'].to_numpy()
    sz_mbq_wh = sub['SZ_MBQ_WH'].to_numpy()
    sz_stk = sub['SZ_STK'].to_numpy()
    pool_cons = sub['POOL_CONSUMED'].to_numpy()
    ship_qty = sub['SHIP_QTY'].to_numpy()

    need_ship = np.maximum(r * sz_mbq - sz_stk - ship_qty, 0.0)
    if ot == 'TBL':
        tbl_cum = sz_mbq_wh + (r - 1) * sz_mbq
        need_pool = np.maximum(tbl_cum - sz_stk - pool_cons, 0.0)
        need_pool = np.where(need_ship == 0, 0.0, need_pool)
    else:
        need_pool = np.maximum(r * sz_mbq - sz_stk - pool_cons, 0.0)

    sub['need_pool'] = need_pool
    sub['need_ship'] = need_ship

    # 4) Sort OPTs by priority. The per-OPT loop iterates groupby groups in
    #    sort order — pandas preserves group order when sort=False AND the
    #    input is pre-sorted by the group key.
    sub['_st_rank_fill'] = sub['ST_RANK'].fillna(999999).astype('float64')
    sub['_opt_rank_fill'] = sub['OPT_PRIORITY_RANK'].fillna(999999).astype('float64')
    sub.sort_values(
        ['_opt_rank_fill', '_st_rank_fill', 'WERKS', 'GEN_ART_NUMBER', 'CLR', 'SZ'],
        kind='mergesort',
        inplace=True,
    )

    skipped_r07 = 0
    skipped_mj_req_gate = 0
    skipped_cap = 0
    skipped_cap_override = 0   # OPTs admitted past sec-cap by the new Primary-condition override
    skipped_empty_pool = 0
    allocated_opts = 0
    n_partial_sizes = 0

    # 5) Per-OPT loop. Each group = one OPT = (WERKS, GEN_ART, CLR) within this OPT_TYPE.
    for (werks_v, gen_art_v, clr_v), opt_rows in sub.groupby(
        ['WERKS', 'GEN_ART_NUMBER', 'CLR'], sort=False, observed=True
    ):
        if opt_rows.empty:
            continue

        # Skip OPTs whose demand is already zero (already allocated to target).
        if (opt_rows['need_pool'] <= 0).all() and (opt_rows['need_ship'] <= 0).all():
            continue

        opt_idx = opt_rows['_orig_idx'].to_numpy()

        # 5a) Read LIVE pool for every size of this OPT at this moment.
        opt_pool_keys: List[Tuple] = list(zip(
            opt_rows['RDC'].astype(str).tolist(),
            opt_rows['MAJ_CAT'].astype(str).tolist(),
            opt_rows['GEN_ART_NUMBER'].astype(str).tolist(),
            opt_rows['CLR'].astype(str).tolist(),
            opt_rows['VAR_ART'].astype(str).tolist(),
            opt_rows['SZ'].astype(str).tolist(),
        ))
        live_pool = np.array(
            [float(pool_dict.get(k, 0.0)) for k in opt_pool_keys],
            dtype='float64',
        )

        # FNL_Q_REM is written AFTER the pool draw at 5f.1 — using the live
        # post-decrement value so the audit row reflects what was actually
        # left for subsequent OPTs. The pre-draw value is held in the local
        # `live_pool` array for the rest of this function.

        # 5b) Pre-check 1: TBL R07 live size-ratio gate.
        #     Skip whole OPT if EITHER:
        #       (sizes_with_pool < min_size_count)
        #       OR (ratio < size_threshold)
        #
        #     Strict OR semantics (changed from AND on 2026-06-06 at user
        #     request). Rationale: when an operator sets min_size_count=0,
        #     the count clause is disabled — but the ratio clause should
        #     still gate the OPT on its own. With AND, min_size_count=0
        #     effectively disabled R07 entirely, letting OPTs with very
        #     low size_ratio (e.g. 2/6 = 33%) into allocation. The user's
        #     intent ("if size ratio < threshold, don't allocate as TBL")
        #     is naturally expressed as OR. NOTE: this diverges from
        #     Stage-A `R07_VAR_RATIO_TBL` (still AND on master MSA counts);
        #     deliberate — Stage A is structural (master data), this gate
        #     is real-time (live pool) and should be stricter.
        if ot == 'TBL' and (size_threshold > 0 or min_size_count > 0):
            sizes_with_pool = int(np.count_nonzero(live_pool > 0))
            total_sizes = len(opt_rows)
            ratio = sizes_with_pool / total_sizes if total_sizes > 0 else 0.0
            if (sizes_with_pool < min_size_count) or (ratio < size_threshold):
                _mark_opt_skip(
                    alloc_df, opt_idx,
                    'R07_SIZE_RATIO_LIVE',
                    f'sizes_with_pool={sizes_with_pool}/{total_sizes},'
                    f'ratio={ratio:.2f},thr={size_threshold},min={min_size_count}',
                    live_pool=live_pool,
                )
                skipped_r07 += 1
                continue

        # 5b2) Pre-check 1.5: TBL MJ_REQ_GATE.
        #     Mirrors rule_engine_new._stage_c_apply_opt_mj_req_gate but applied
        #     at this OPT's turn so the pool isn't drained by OPTs the post-loop
        #     gate would zero anyway. An OPT passes iff:
        #         (cap_pct/100) * MJ_REQ_REM[WERKS]  >=  mbq_gate_factor * OPT_MBQ
        #     If the gate fails, the OPT is skipped with TBL_MJ_REQ_GATE_FAIL
        #     and pool_dict is left untouched — those units stay available for
        #     later, lower-priority OPTs (this is the fix that lets HP11 see
        #     the 3 units that today get stranded after retroactive zeroing).
        if ot == 'TBL' and mj_req_rem_dict is not None:
            opt_mbq_val = float(opt_rows['OPT_MBQ'].iloc[0] or 0.0)
            cap_rem = float(mj_req_rem_dict.get(str(werks_v), 0.0))
            gate_threshold = mbq_gate_factor * opt_mbq_val
            cap_allowance = (tbl_mj_req_cap_pct / 100.0) * cap_rem
            if opt_mbq_val > 0 and cap_allowance < gate_threshold:
                _mark_opt_skip(
                    alloc_df, opt_idx,
                    'TBL_MJ_REQ_GATE_FAIL',
                    f'opt_mbq={int(opt_mbq_val)},cap_rem={int(cap_rem)},'
                    f'cap_pct={tbl_mj_req_cap_pct:g},'
                    f'factor={mbq_gate_factor:g},'
                    f'threshold={int(gate_threshold)},'
                    f'allowance={int(cap_allowance)}',
                    live_pool=live_pool,
                )
                skipped_mj_req_gate += 1
                continue

        # 5c) Pre-check 2: MJ_REQ_CAP per-WERKS budget.
        opt_need = opt_rows['need_pool'].to_numpy().astype('float64')

        # 5c.5) PAK_SZ rounding. Split-basis for TBL (2026-06-26):
        #       SHIP and HOLD are rounded INDEPENDENTLY to pak multiples,
        #       each with its own 0.5*pak half-up gate. Ship basis =
        #       need_ship (OPT_MBQ-derived); Hold basis = need_pool -
        #       want_ship_pak (= the WH-buffer overhead). If SHIP gates
        #       to 0, HOLD is forced to 0 (no orphan hold). The downstream
        #       SHIP-first/HOLD-from-rem logic in 5e enforces:
        #         - SHIP may be a non-pak last-balance sliver when live
        #           pool < pak (intentional drain),
        #         - HOLD is always a full pak EXCEPT when pool remaining
        #           after SHIP is below one pak (last-sliver loose drain).
        #       RL/TBC retain the legacy single-pass rounding on opt_need
        #       (they never produce HOLD and the existing ship-ceiling /
        #       refund logic at 5g already keeps SHIP pak-aligned).
        if 'PAK_SZ' in opt_rows.columns:
            pak = (
                opt_rows['PAK_SZ']
                .fillna(0).replace(0, 1)
                .to_numpy().astype('float64')
            )
        else:
            pak = np.ones(len(opt_rows), dtype='float64')
        raw_need_for_audit = opt_need.copy()
        if ot == 'TBL':
            ship_basis = opt_rows['need_ship'].to_numpy().astype('float64')
            want_ship_pak = np.where(
                ship_basis < 0.5 * pak,
                0.0,
                np.floor((ship_basis + 0.5 * pak) / pak) * pak,
            )
            hold_basis = np.maximum(opt_need - want_ship_pak, 0.0)
            # FS-06 hold suppression (FSD_FRESH_GRT_HOLD_CONTROL §7 / IM-1):
            # zero the HOLD basis for suppressed rows (UPC store / SEG
            # toggle) BEFORE the half-up pak-alignment below, so rounding
            # rounds the already-zeroed hold (0 stays 0) and can never
            # re-inflate a suppressed hold. want_hold_pak → 0 also removes
            # the hold from opt_need, so 5e's hold_target is 0 and the
            # un-held qty stays in the live pool (BR-11). Ship-side pak
            # alignment (want_ship_pak above) is untouched.
            _sup = _hold_suppress_arr(
                alloc_df, opt_idx,
                skip_hold_upc=skip_hold_upc,
                apply_hold_seg_app=apply_hold_seg_app,
                apply_hold_seg_gm=apply_hold_seg_gm,
            )
            if _sup is not None:
                hold_basis = np.where(_sup, 0.0, hold_basis)
            want_hold_pak = np.where(
                (want_ship_pak == 0) | (hold_basis < 0.5 * pak),
                0.0,
                np.floor((hold_basis + 0.5 * pak) / pak) * pak,
            )
            opt_need = want_ship_pak + want_hold_pak
            gated_mask = (want_ship_pak == 0) & (raw_need_for_audit > 0)
        else:
            want_ship_pak = None
            want_hold_pak = None
            gated_mask = raw_need_for_audit < 0.5 * pak
            opt_need = np.where(
                gated_mask,
                0.0,
                np.floor((raw_need_for_audit + 0.5 * pak) / pak) * pak,
            )

        total_need = float(opt_need.sum())

        scale = 1.0
        if total_need > 0:
            if ot == 'TBL':
                # TBL admission — unified overshoot rule (2026-07-04):
                # intended (MBQ-only, = want_ship_pak.sum()) vs remaining
                # MJ_REQ headroom. Breach = intended − mj_rem. Admit when
                # overshoot <= 0.5 × intended (same shape as primary
                # MBQ_CAP COMPLETE and sec-cap Primary/Secondary). One
                # TBL OPT per WERKS can overshoot: 5h2 decrements
                # mj_req_rem_dict WITHOUT a max(0,…) floor, so the next
                # TBL OPT sees negative/zero remainder and its overshoot
                # exceeds 0.5 × intended → skip.
                # intended is MBQ-only per project_cap_validation_mbq_only
                # — want_hold_pak (RDC-hold buffer) is invisible.
                # Legacy deployments with empty mj_req_rem_dict fall
                # through permissively.
                werks_key = str(werks_v)
                if mj_req_rem_dict and werks_key in mj_req_rem_dict:
                    mj_rem = float(mj_req_rem_dict[werks_key])
                    intended_tbl = (
                        float(want_ship_pak.sum())
                        if want_ship_pak is not None
                        else float(opt_need.sum())
                    )
                    if intended_tbl > 0 and mj_rem < intended_tbl:
                        overshoot = intended_tbl - mj_rem
                        threshold = 0.5 * intended_tbl
                        if overshoot > threshold:
                            _mark_opt_skip(
                                alloc_df, opt_idx,
                                'MBQ_CAP_TBL',
                                f'pre-check:intended={int(intended_tbl)},'
                                f'mj_req_rem={int(mj_rem)},'
                                f'overshoot={int(overshoot)}>'
                                f'0.5*intended={int(threshold)}',
                                live_pool=live_pool,
                            )
                            skipped_cap += 1
                            continue
                        # ADMIT with overshoot: audit stamp.
                        note = (
                            f' MBQ_CAP_OVERSHOOT(TBL,'
                            f'intended={int(intended_tbl)},'
                            f'mj_req_rem={int(mj_rem)},'
                            f'overshoot={int(overshoot)});'
                        )
                        prev_r = (
                            alloc_df.loc[opt_idx, 'ALLOC_REMARKS']
                            .fillna('').astype(str)
                        )
                        alloc_df.loc[opt_idx, 'ALLOC_REMARKS'] = prev_r + note
                # PASS or overshoot-admit: full need ships.
            elif mbq_budget is not None:
                # RL / TBC: cap against mbq_budget, which still carries the
                # rl/tbc_mbq_cap_pct headroom from _live_mbq_budget.
                # Dispatch mode (per OPT_TYPE, from ARS run config):
                #   SCALED   — proportional round-then-shave; OPT ships
                #              partial, up to werks_cap. Recovers the units
                #              lost to the legacy floor-per-size truncation.
                #   COMPLETE — all-or-skip. Ship full need if budget covers,
                #              else skip whole OPT and leave the budget for
                #              the next OPT in the band.
                werks_cap = float(mbq_budget.get(str(werks_v), 0.0))
                if total_need > werks_cap:
                    if werks_cap <= 0:
                        _mark_opt_skip(
                            alloc_df, opt_idx,
                            f'MBQ_CAP_{ot}',
                            f'pre-check:cap_rem=0,need={int(total_need)}',
                            live_pool=live_pool,
                        )
                        skipped_cap += 1
                        continue
                    mode = (rl_dispatch_mode if ot == 'RL'
                            else tbc_dispatch_mode).upper()
                    if mode == 'COMPLETE':
                        # COMPLETE with overshoot allowance: if the remaining
                        # cap covers >= half of total_need, ship the full need
                        # even though it exceeds werks_cap. The 5h decrement
                        # then drives mbq_budget[werks] negative, and every
                        # subsequent OPT in the same WERKS hits the
                        # `werks_cap <= 0` hard-skip branch above.
                        # Overshoot per OPT is bounded to <= 0.5 x total_need.
                        if werks_cap >= 0.5 * total_need:
                            note = (
                                f' MBQ_CAP_OVERSHOOT({ot},'
                                f'need={int(total_need)},'
                                f'cap={int(werks_cap)},'
                                f'overshoot={int(total_need - werks_cap)});'
                            )
                            prev_r = (
                                alloc_df.loc[opt_idx, 'ALLOC_REMARKS']
                                .fillna('').astype(str)
                            )
                            alloc_df.loc[opt_idx, 'ALLOC_REMARKS'] = prev_r + note
                            # opt_need untouched — full ship. Fall through.
                        else:
                            _mark_opt_skip(
                                alloc_df, opt_idx,
                                f'MBQ_CAP_{ot}',
                                f'complete-mode:need={int(total_need)},'
                                f'cap_rem={int(werks_cap)},'
                                f'overshoot={int(total_need - werks_cap)}>'
                                f'0.5*need={int(0.5 * total_need)}',
                                live_pool=live_pool,
                            )
                            skipped_cap += 1
                            continue
                    elif mode == 'SCALED':
                        # SCALED: round-then-shave. np.round (nearest int)
                        # instead of floor recovers the per-size
                        # "1-less-than-SZ_MBQ" loss that floor produces when
                        # scale is just under 1.0; the shave pass below
                        # guarantees sum(opt_need) <= werks_cap so the MBQ
                        # budget invariant still holds.
                        #
                        # GUARD (2026-07-17): this block is reached ONLY when
                        # mode == 'SCALED'. Previously it was an unguarded
                        # fall-through after the COMPLETE block, so a
                        # COMPLETE-mode overshoot-admit (which stamps
                        # MBQ_CAP_OVERSHOOT and intends a FULL ship) fell
                        # straight into here and got silently rescaled down to
                        # the cap — producing a contradictory double stamp
                        # (OVERSHOOT + SCALE) and shipping the scaled qty, not
                        # the full need. The `elif` makes COMPLETE and SCALED
                        # mutually exclusive.
                        scale = werks_cap / total_need
                        ideal = opt_need * scale
                        opt_need = np.round(ideal / pak) * pak
                        over_units = float(opt_need.sum()) - werks_cap
                        if over_units > 1e-9:
                            # Shave whole paks from rows that were rounded UP
                            # (frac >= 0.5); pick the row with frac closest to
                            # 0.5 first — that round-up was the weakest signal,
                            # so reverting it costs the least vs. ideal.
                            frac = (ideal / np.where(pak > 0, pak, 1.0))
                            frac = frac - np.floor(frac)
                            iter_safety = int(len(opt_need)) * 2 + 4
                            while over_units > 1e-9 and iter_safety > 0:
                                iter_safety -= 1
                                score = np.where(
                                    (frac >= 0.5) & (opt_need > 0),
                                    frac, np.inf,
                                )
                                idx_s = int(np.argmin(score))
                                if not np.isfinite(score[idx_s]):
                                    # No rounded-up rows left — fall back to
                                    # shaving the largest positive row.
                                    positive = np.where(opt_need > 0)[0]
                                    if len(positive) == 0:
                                        break
                                    idx_s = int(positive[np.argmax(opt_need[positive])])
                                opt_need[idx_s] = max(0.0, opt_need[idx_s] - pak[idx_s])
                                frac[idx_s] = -np.inf  # don't reselect as "rounded up"
                                over_units = float(opt_need.sum()) - werks_cap
                        # Audit stamp on every row of this OPT so reviewers see
                        # why each size landed where it did (was previously
                        # silent for PAK_SZ=1 rows). Reads alongside the
                        # existing B[ot.rN.rkK] trace.
                        note = (
                            f' MBQ_CAP_SCALE({ot},need={int(total_need)},'
                            f'cap={int(werks_cap)},scale={scale:.3f});'
                        )
                        prev_r = alloc_df.loc[opt_idx, 'ALLOC_REMARKS'].fillna('').astype(str)
                        alloc_df.loc[opt_idx, 'ALLOC_REMARKS'] = prev_r + note

        # NOTE: by design, TBL is NOT clamped to remaining MJ_REQ_REM after
        # admission. If the admission test at 5b2 lets the OPT in, it ships
        # the full OPT_MBQ (TBL = "complete set or skip"). The per-OPT
        # decrement at 5h2 then drives mj_req_rem_dict to 0 and the NEXT
        # TBL OPT in the same WERKS fails admission, so the worst-case
        # overshoot is one OPT's MBQ — not a cumulative blow-out.

        # 5c.6) Per-OPT SEC-CAP pre-gate. Replaces the post-pass gate that
        #       used to run after the whole waterfall. Evaluating HERE — at
        #       the OPT's turn, BEFORE pool draw — means a blocked OPT does
        #       NOT consume any pool, leaving units available for later
        #       lower-priority OPTs that would otherwise be starved.
        #
        #       Block when running + intended_ship > budget for any
        #       participating grid. Override (rule #3) admits the OPT
        #       anyway when MJ_REQ_REM(werks) >= 0.5 × OPT_MBQ — same
        #       formula as the TBL Primary check.
        sec_cap_override_info: Optional[Dict[str, Any]] = None
        if sec_cap_state is not None and sec_cap_state.get("grids"):
            # Cap validations are MBQ-only. For TBL, opt_need = want_ship_pak
            # + want_hold_pak; the want_hold_pak portion is the SZ_MBQ_WH
            # excess (RDC-hold buffer on first dispatch) and must NOT enter
            # cap arithmetic. RL/TBC opt_need is already MBQ-only.
            if ot == 'TBL' and want_ship_pak is not None:
                intended_ship_sc = float(want_ship_pak.sum())
            else:
                intended_ship_sc = float(opt_need.sum())
            breach, participating_grids = _evaluate_sec_cap_per_opt(
                sec_cap_state, opt_rows, werks_v, intended_ship_sc,
                mj_req_rem_dict=mj_req_rem_dict,
                mbq_gate_factor=mbq_gate_factor,
            )
            if breach is not None and breach.get("action") == "block":
                # When the matrix path resolved a band, the effective cap
                # on this grain is the resolved growth% (not the grid's
                # flat cap). Fall back to the flat cap in every other case
                # (matrix off, or matrix on with band gap).
                _matrix_on   = bool(breach.get("matrix_enabled", False))
                _band_ok     = bool(breach.get("band_matched", True))
                _effective_cap = (
                    float(breach.get("growth_pct", breach["cap_pct"]))
                    if _matrix_on and _band_ok
                    else float(breach["cap_pct"])
                )
                _mark_opt_skip_sec_cap(
                    alloc_df, opt_idx,
                    grid_name=breach["grid"],
                    cap_pct=_effective_cap,
                    stk_val=breach["stk"],
                    ceiling_val=breach["ceiling"],
                    mbq_orig_val=breach["mbq"],
                    run_before=breach["run_before"],
                    intended_ship=intended_ship_sc,
                    budget=breach["budget"],
                    configured=breach.get("configured", True),
                    is_primary=bool(breach.get("primary_block", False)),
                    hard_block_reasons=breach.get("hard_block_reasons"),
                    grain=breach.get("grain"),
                    cont_pct=breach.get("cont_pct"),
                    band_matched=_band_ok,
                    matrix_enabled=_matrix_on,
                )
                skipped_cap += 1
                continue  # do NOT touch pool_dict / hold_dict — units stay live
            elif breach is not None and breach.get("action") == "override":
                # ADMIT despite the grain breach — Primary condition holds.
                # Stash info so step 5j can stamp the override remark on the
                # shipped OPT rows alongside the natural waterfall trace.
                sec_cap_override_info = breach
                skipped_cap_override += 1
            # else: no breach — normal admit.
            # In all admit paths, running totals are advanced AFTER pool draw
            # by the section 5g.1 block below.
        else:
            participating_grids = []

        # 5d) Compute hold draw FIRST (RL/TBC only). Match _run_band step 1b
        #     semantics: hold consumed before pool, by (WERKS, VAR_ART, SZ) key.
        #     PAK-aware (2026-06-26): when hold_rem >= pak, draw whole paks
        #     only (floor). When hold_rem < pak, allow loose drain so the
        #     buffer doesn't strand sub-pak residue forever.
        from_hold = np.zeros(len(opt_rows), dtype='float64')
        if ot in ('RL', 'TBC') and hold_dict:
            for i in range(len(opt_rows)):
                if opt_need[i] <= 0:
                    continue
                hk = (str(werks_v), str(opt_rows.iloc[i]['VAR_ART']),
                      str(opt_rows.iloc[i]['SZ']))
                hold_rem = float(hold_dict.get(hk, 0.0))
                if hold_rem <= 0:
                    continue
                pak_i = float(pak[i])
                if hold_rem >= pak_i:
                    avail = np.floor(hold_rem / pak_i) * pak_i
                    take_h = min(opt_need[i], avail)
                else:
                    take_h = min(opt_need[i], hold_rem)
                if take_h > 0:
                    from_hold[i] = take_h
                    hold_dict[hk] = hold_rem - take_h
            opt_need = np.maximum(opt_need - from_hold, 0.0)

        # 5e) Pool draw.
        #   - TBL (2026-06-26): SHIP-first / HOLD-from-remaining with pak rules.
        #     SHIP takes from live_pool. If live_pool < want_ship_pak the
        #     remainder ships loose (last-balance drain to store). HOLD takes
        #     from pool_after_ship: full want_hold_pak when it fits, else 0,
        #     except when pool_after_ship is below one pak in which case
        #     the sliver drains loose to HOLD. take_pool here = round_ship +
        #     round_hold so the pool_dict decrement at 5f reflects the
        #     actual draw (not the inflated pak target).
        #   - RL/TBC: legacy single-pass clamp; ship-ceiling refund is in 5g.
        if ot == 'TBL':
            ship_target = want_ship_pak.astype('float64') if want_ship_pak is not None else np.zeros_like(opt_need)
            hold_target = want_hold_pak.astype('float64') if want_hold_pak is not None else np.zeros_like(opt_need)
            round_ship = np.minimum(ship_target, live_pool)
            pool_after_ship = np.maximum(live_pool - round_ship, 0.0)
            full_pak_ok = (pool_after_ship >= hold_target) & (hold_target > 0)
            sliver_loose = (pool_after_ship > 0) & (pool_after_ship < pak) & (hold_target > 0)
            ship_done_mask = round_ship > 0
            round_hold = np.where(
                ~ship_done_mask, 0.0,
                np.where(full_pak_ok, hold_target,
                         np.where(sliver_loose, pool_after_ship, 0.0)),
            )
            take_pool = round_ship + round_hold
        else:
            take_pool = np.minimum(opt_need, live_pool)
            # round_ship / round_hold computed in 5g (RL/TBC branch).

        # 5f) Update live pool_dict by mutating in place (so next OPT sees decrement).
        for i, k in enumerate(opt_pool_keys):
            if take_pool[i] > 0:
                pool_dict[k] = max(0.0, pool_dict.get(k, 0.0) - float(take_pool[i]))

        # 5f.1) Persist the post-draw pool value into FNL_Q_REM for every row
        #       of this OPT. Operator-visible in ARS_ALLOC_HISTORY — lets
        #       audits tell pool-exhausted (FNL_Q_REM == 0) apart from
        #       pak-gated (FNL_Q_REM > 0 with PAK_SZ_GATE remark) without
        #       mentally replaying ST_RANK order.
        post_draw_pool = np.array(
            [float(pool_dict.get(k, 0.0)) for k in opt_pool_keys],
            dtype='float64',
        )
        alloc_df.loc[opt_idx, 'FNL_Q_REM'] = post_draw_pool

        # 5g) SHIP / HOLD split.
        #     TBL: round_ship and round_hold already computed in 5e (new
        #     SHIP-first/HOLD-from-rem logic). Just expose pool_take_total.
        #     RL/TBC: legacy ship-ceiling + pak-floor + refund path below.
        need_ship_arr = opt_rows['need_ship'].to_numpy().astype('float64')
        if ot == 'TBL':
            pool_take_total = round_ship + round_hold
        else:
            # Two constraints on the RL/TBC ship:
            #  1. PAK ceiling, not bare need_ship: ship may overshoot
            #     need_ship by up to (pak-1) so a half-up pak round at R1
            #     still ships a full pak. A bare need_ship clamp would
            #     wipe out the pak round-up.
            #  2. PAK alignment on the ship itself: snap down to a pak
            #     multiple UNLESS combined supply (live_pool + from_hold)
            #     is below one pak — that's genuine exhaustion and a
            #     non-pak partial is acceptable (the last inventory ships).
            # The ceiling also caps the cross-round from_hold drift: in
            # later rounds POOL_CONSUMED is behind SHIP_QTY by prior
            # from_hold, which inflates need_pool — the ceiling stops the
            # extra units from shipping, and the refund below puts the
            # over-take back into pool_dict.
            ship_ceiling = np.ceil(need_ship_arr / pak) * pak
            raw_ship = np.minimum(take_pool + from_hold, ship_ceiling)
            combined_supply = live_pool + from_hold
            supply_below_pak = combined_supply < pak
            effective_ship = np.where(
                supply_below_pak,
                raw_ship,
                np.floor(raw_ship / pak) * pak,
            )
            pool_used = np.maximum(effective_ship - from_hold, 0.0)
            excess_pool = take_pool - pool_used
            if excess_pool.any():
                for _i, _k in enumerate(opt_pool_keys):
                    if excess_pool[_i] > 0:
                        pool_dict[_k] = pool_dict.get(_k, 0.0) + float(excess_pool[_i])
                post_draw_pool = np.array(
                    [float(pool_dict.get(_k, 0.0)) for _k in opt_pool_keys],
                    dtype='float64',
                )
                alloc_df.loc[opt_idx, 'FNL_Q_REM'] = post_draw_pool
                take_pool = pool_used
            round_ship = effective_ship
            round_hold = np.zeros_like(take_pool)
            pool_take_total = take_pool  # FROM_HOLD_QTY accounted separately below

        # 5g.1) Advance sec-cap `running` totals by what ACTUALLY shipped to
        #       MBQ (display). Cap arithmetic is MBQ-only — the SZ_MBQ_WH
        #       excess (TBL round_hold = RDC-hold buffer on first dispatch)
        #       must NOT enter the running counter, otherwise later OPTs at
        #       the same grain see an inflated `already_shipped_this_run`
        #       and get skipped against a ceiling they shouldn't be measured
        #       against. RL/TBC round_ship already includes the from_hold
        #       contribution (raw_ship = min(take_pool + from_hold,
        #       ship_ceiling) in 5g), so it stands on its own; TBL round_ship
        #       is the want_ship_pak draw, also MBQ-only. round_hold (TBL
        #       only) is intentionally dropped.
        if sec_cap_state is not None and participating_grids:
            actual_moved = float(round_ship.sum())
            if actual_moved > 0:
                for g_name, grain in participating_grids:
                    sec_cap_state["running"][g_name][grain] = (
                        sec_cap_state["running"][g_name].get(grain, 0.0) + actual_moved
                    )

        # 5h) Update mbq_budget so the next OPT in the same WERKS sees the
        #     post-allocation budget. Match _run_band step 5a's per-WERKS
        #     cumulative deduction semantics.
        #     No max(0.0, …) floor: when a COMPLETE-mode OPT overshoots
        #     (ships need > cap because cap >= 0.5 × need), the cap must be
        #     allowed to go negative so every subsequent RL/TBC OPT in the
        #     same WERKS hits the `werks_cap <= 0` hard-skip branch at 5b2.
        if mbq_budget is not None:
            consumed = float(pool_take_total.sum())
            if ot in ('RL', 'TBC'):
                consumed += float(from_hold.sum())
            mbq_budget[str(werks_v)] = (
                float(mbq_budget.get(str(werks_v), 0.0)) - consumed
            )

        # 5h2) For TBL, also decrement the MJ_REQ_REM running total so the
        #      next TBL OPT in the same WERKS evaluates the gate against the
        #      updated cap. Counts SHIP_QTY only (hold doesn't consume req).
        #      No max(0,…) floor: after a TBL overshoot admission the
        #      remainder can go negative so the next TBL OPT's overshoot
        #      exceeds 0.5 × intended and hard-skips (2026-07-04 unified rule).
        if ot == 'TBL' and mj_req_rem_dict is not None:
            shipped = float(round_ship.sum())
            if shipped > 0:
                mj_req_rem_dict[str(werks_v)] = (
                    float(mj_req_rem_dict.get(str(werks_v), 0.0)) - shipped
                )

        # 5i) Write back to alloc_df. Same column updates as _run_band step 7.
        prev_pc = alloc_df.loc[opt_idx, 'POOL_CONSUMED'].to_numpy()
        prev_ship = alloc_df.loc[opt_idx, 'SHIP_QTY'].to_numpy()
        prev_hold = alloc_df.loc[opt_idx, 'HOLD_QTY'].to_numpy()

        # POOL_CONSUMED tracks ONLY pool-take (consumption from FNL_Q). The hold
        # draw is recorded separately in FROM_HOLD_QTY. The post-loop SQL recompute
        # at rule_engine_pandas.py:887 computes FNL_Q_REM = FNL_Q - SUM(POOL_CONSUMED)
        # per pool key — including from_hold here would double-subtract the hold
        # draw from the pool (hold comes from a separate hold_dict, not from FNL_Q),
        # producing negative FNL_Q_REM. See diagnosis: HP11 2XL FNL_Q=44,
        # SUM(POOL_CONSUMED)=45 → FNL_Q_REM=-1.
        new_pc = prev_pc + take_pool

        alloc_df.loc[opt_idx, 'POOL_CONSUMED'] = new_pc
        alloc_df.loc[opt_idx, 'ROUND_SHIP'] = round_ship
        alloc_df.loc[opt_idx, 'ROUND_HOLD'] = round_hold
        alloc_df.loc[opt_idx, 'SHIP_QTY'] = prev_ship + round_ship
        alloc_df.loc[opt_idx, 'HOLD_QTY'] = prev_hold + round_hold
        alloc_df.loc[opt_idx, 'ALLOC_WAVE'] = f"{ot}_R{r}"
        alloc_df.loc[opt_idx, 'ALLOC_ROUND'] = float(r)
        if ot in ('RL', 'TBC') and from_hold.any():
            prev_fh = alloc_df.loc[opt_idx, 'FROM_HOLD_QTY'].fillna(0.0).to_numpy()
            alloc_df.loc[opt_idx, 'FROM_HOLD_QTY'] = prev_fh + from_hold

        # ALLOC_STATUS — compare cumulative SHIP_QTY against ship target.
        i_rod_arr = opt_rows['I_ROD'].to_numpy().astype('float64')
        sz_mbq_arr = opt_rows['SZ_MBQ'].to_numpy().astype('float64')
        sz_stk_arr = opt_rows['SZ_STK'].to_numpy().astype('float64')
        target = np.maximum(i_rod_arr * sz_mbq_arr - sz_stk_arr, 0.0)
        new_ship_arr = alloc_df.loc[opt_idx, 'SHIP_QTY'].to_numpy()
        alloc_df.loc[opt_idx, 'ALLOC_STATUS'] = np.where(
            new_ship_arr >= target, 'ALLOCATED', 'PARTIAL'
        )

        # 5j) Audit remarks per size — preserves the existing ALLOC_REMARKS
        #     breadcrumb convention so reviewers can read both formats.
        moved = (round_ship + round_hold + from_hold) > 0
        rank_str = str(int(opt_rows['_opt_rank_fill'].iloc[0])) if len(opt_rows) else '?'
        # Capture pak-rounded target before MBQ scaling / hold draw so the
        # audit marker reflects what pak rounding asked for, independent of
        # downstream cap or hold reductions.
        pak_rounded_target = np.where(
            gated_mask, 0.0,
            np.floor((raw_need_for_audit + 0.5 * pak) / pak) * pak,
        )
        for i in range(len(opt_rows)):
            row_idx = opt_idx[i]
            prev = str(alloc_df.loc[row_idx, 'ALLOC_REMARKS'] or '')
            new_remarks = prev
            if moved[i]:
                trace = (
                    f' B[{ot}.r{r}.rk{rank_str}] '
                    f'ship={int(round_ship[i])} hold={int(round_hold[i])}'
                )
                if from_hold[i] > 0:
                    trace += f' from_hold={int(from_hold[i])}'
                if take_pool[i] < opt_need[i] and opt_need[i] > 0:
                    trace += f' partial(need={int(opt_need[i])},pool={int(live_pool[i])})'
                trace += ';'
                new_remarks = new_remarks + trace
            else:
                # SHIP=0 for this size — stamp honest SKIP_REASON in remarks.
                # _stage_c_finalise_skip_reasons in rule_engine_new will read these
                # and promote the most specific to ARS_ALLOC_HISTORY.SKIP_REASON.
                fnl_q = float(alloc_df.loc[row_idx, 'FNL_Q'] or 0.0)
                lp = float(live_pool[i])
                consumed_prior = max(0.0, fnl_q - lp)
                if lp == 0.0:
                    reason = 'POOL_EMPTY'
                    remark = (
                        f'FNL_Q={int(fnl_q)},live_pool=0,'
                        f'consumed_prior={int(consumed_prior)}'
                    )
                    new_remarks = new_remarks + f' {reason}({remark});'
                    skipped_empty_pool += 1

            # PAK_SZ audit marker. Only emit when pak > 1 AND pak rounding
            # actually changed the row (gated to 0, or rounded to a different
            # target). The Stage D safety-net at rule_engine_pandas.py:400-464
            # is guarded to skip rows already carrying a PAK_SZ_ marker, so
            # this row will not be re-rounded at finalize.
            #
            # TBL (2026-06-26) emits PAK_SZ_SHIP and PAK_SZ_HOLD as separate
            # markers — SHIP and HOLD are now pak-rounded INDEPENDENTLY (see
            # 5c.5 / 5e). RL/TBC keep the legacy PAK_SZ_GATE / PAK_SZ_ROUND.
            if pak[i] > 1:
                raw_i = int(raw_need_for_audit[i])
                if ot == 'TBL' and want_ship_pak is not None:
                    ship_basis_i = int(opt_rows.iloc[i]['need_ship'])
                    ship_target_i = int(want_ship_pak[i])
                    hold_target_i = int(want_hold_pak[i])
                    ship_act_i = int(round_ship[i])
                    hold_act_i = int(round_hold[i])
                    if ship_basis_i > 0 and ship_target_i == 0:
                        new_remarks += (
                            f' PAK_SZ_GATE(ship={ship_basis_i},pak={int(pak[i])});'
                        )
                    elif ship_target_i > 0:
                        if ship_act_i == ship_target_i:
                            new_remarks += (
                                f' PAK_SZ_SHIP(from={ship_basis_i},'
                                f'to={ship_target_i},pak={int(pak[i])});'
                            )
                        elif 0 < ship_act_i < ship_target_i:
                            new_remarks += (
                                f' PAK_SZ_SHIP(want={ship_target_i},'
                                f'act={ship_act_i},pak={int(pak[i])},loose);'
                            )
                        else:
                            new_remarks += (
                                f' PAK_SZ_SHIP(want={ship_target_i},'
                                f'act={ship_act_i},pak={int(pak[i])},short);'
                            )
                    if hold_target_i > 0 or hold_act_i > 0:
                        if hold_act_i == hold_target_i and hold_target_i > 0:
                            new_remarks += (
                                f' PAK_SZ_HOLD(want={hold_target_i},'
                                f'pak={int(pak[i])});'
                            )
                        elif hold_act_i == 0:
                            new_remarks += (
                                f' PAK_SZ_HOLD(want={hold_target_i},act=0,'
                                f'pak={int(pak[i])},gated);'
                            )
                        else:
                            new_remarks += (
                                f' PAK_SZ_HOLD(want={hold_target_i},'
                                f'act={hold_act_i},pak={int(pak[i])},loose);'
                            )
                elif gated_mask[i] and raw_i > 0:
                    new_remarks += (
                        f' PAK_SZ_GATE(req={raw_i},pak={int(pak[i])});'
                    )
                elif int(pak_rounded_target[i]) != raw_i and raw_i > 0:
                    rounded_i = int(pak_rounded_target[i])
                    # Total units that left this row (pool + hold draws).
                    actual_out = float(take_pool[i]) + float(from_hold[i])
                    if actual_out + 1e-9 < rounded_i:
                        # Disambiguate the short reason — the prior
                        # `short=stock` label read as "pool ran out" even
                        # when the bind was the MBQ_CAP scaling or the
                        # cross-round ship ceiling. Order matters: cap is
                        # detected at opt_need (post-budget, pre-pool); pool
                        # at take_pool+from_hold vs opt_need; ceiling is
                        # what's left.
                        if opt_need[i] + 1e-9 < pak_rounded_target[i]:
                            short_label = 'cap'
                        elif (take_pool[i] + from_hold[i]) + 1e-9 < opt_need[i]:
                            short_label = 'pool'
                        else:
                            short_label = 'ceiling'
                        new_remarks += (
                            f' PAK_SZ_ROUND(from={raw_i},to={rounded_i},'
                            f'pak={int(pak[i])},short={short_label}={int(actual_out)});'
                        )
                    else:
                        new_remarks += (
                            f' PAK_SZ_ROUND(from={raw_i},to={rounded_i},'
                            f'pak={int(pak[i])});'
                        )

            if new_remarks != prev:
                alloc_df.loc[row_idx, 'ALLOC_REMARKS'] = new_remarks

        # 5j.1) If this OPT was admitted via the sec-cap override path,
        #       stamp the override narrative on every row of the OPT so
        #       reviewers see "this OPT shipped INTO a capped grain because
        #       MAJ_CAT-level demand was still meaningful" alongside the
        #       natural waterfall trace stamped just above.
        if sec_cap_override_info is not None:
            _stamp_sec_cap_override(alloc_df, opt_idx, sec_cap_override_info)

        allocated_opts += 1
        n_partial_sizes += int(np.sum((take_pool > 0) & (take_pool < opt_need)))

    logger.info(
        f"[C-per-opt] {ot} round={r}: "
        f"allocated={allocated_opts} skipped_r07={skipped_r07} "
        f"skipped_mj_req_gate={skipped_mj_req_gate} "
        f"skipped_cap={skipped_cap} sec_cap_override={skipped_cap_override} "
        f"sizes_pool_empty={skipped_empty_pool} "
        f"sizes_partial={n_partial_sizes}"
    )
