"""
sec_cap_growth_matrix.py

Cont%-driven ceiling override for the per-grid secondary cap.

Business rule: small contributors deserve a bigger stretch. A grain that owns
2% of a MAJ_CAT's total MBQ should be allowed to grow past 120% (300% is
fine — absolute quantity is small). A grain that owns 60% must stay capped
at 120% because a 300% breach there floods the store.

Toggle is off by default. When on, the flat per-grid `sec_cap_pct` is replaced
per grain by a growth% resolved from a cont%-banded matrix.

Spec: docs/superpowers/specs/2026-07-08-sec-cap-growth-matrix-design.md
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import text


Band = Tuple[float, Optional[float], float]   # (lo, hi, growth_pct)

BANDS_TABLE = "ARS_SEC_CAP_GROWTH_MATRIX"
CFG_TABLE   = "ARS_SEC_CAP_GROWTH_MATRIX_CFG"

DEFAULT_BANDS: List[Band] = [
    (0.0,  5.0,   300.0),
    (5.0,  10.0,  250.0),
    (10.0, 15.0,  200.0),
    (15.0, 30.0,  150.0),
    (30.0, None,  120.0),
]


def _ensure_growth_matrix_tables(engine) -> None:
    """Auto-create both tables in Rep_data and seed defaults if empty.

    Idempotent — runs at every load. Follows the grid_builder.py convention
    of IF NOT EXISTS DDL blocks.
    """
    with engine.begin() as c:
        c.execute(text(f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
                           WHERE TABLE_NAME='{BANDS_TABLE}')
            BEGIN
                CREATE TABLE {BANDS_TABLE} (
                    id            INT IDENTITY(1,1) PRIMARY KEY,
                    cont_pct_lo   FLOAT NOT NULL,
                    cont_pct_hi   FLOAT NULL,
                    growth_pct    FLOAT NOT NULL,
                    seq           INT   NOT NULL,
                    updated_at    DATETIME NOT NULL DEFAULT GETDATE(),
                    updated_by    NVARCHAR(100) NULL
                )
            END
        """))
        c.execute(text(f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES
                           WHERE TABLE_NAME='{CFG_TABLE}')
            BEGIN
                CREATE TABLE {CFG_TABLE} (
                    id          INT NOT NULL PRIMARY KEY,
                    is_enabled  BIT NOT NULL DEFAULT 0,
                    updated_at  DATETIME NOT NULL DEFAULT GETDATE(),
                    updated_by  NVARCHAR(100) NULL
                )
            END
        """))
        # Seed default bands only when the bands table is empty. Never
        # touches operator-edited rows.
        row = c.execute(text(f"SELECT COUNT(*) FROM {BANDS_TABLE}")).scalar()
        if not row:
            for seq, (lo, hi, g) in enumerate(DEFAULT_BANDS, start=1):
                c.execute(
                    text(f"""
                        INSERT INTO {BANDS_TABLE}
                            (cont_pct_lo, cont_pct_hi, growth_pct, seq, updated_by)
                        VALUES (:lo, :hi, :g, :seq, :by)
                    """),
                    {"lo": lo, "hi": hi, "g": g, "seq": seq, "by": "seed"},
                )
        # Seed single config row (id=1, disabled) when missing.
        cfg = c.execute(text(f"SELECT COUNT(*) FROM {CFG_TABLE} WHERE id = 1")).scalar()
        if not cfg:
            c.execute(text(f"""
                INSERT INTO {CFG_TABLE} (id, is_enabled, updated_by)
                VALUES (1, 0, 'seed')
            """))


def load_matrix(engine) -> Tuple[bool, List[Band]]:
    """Return (enabled_flag, bands_sorted_by_lo).

    Reads once per allocation run. Ensures the underlying tables exist and
    are seeded on first call. When the bands table is empty for any reason,
    or the config row is missing, returns (False, []) so the caller falls
    back to today's per-grid `sec_cap_pct` behaviour.

    Never raises — a matrix-load failure must never abort a run.
    """
    try:
        _ensure_growth_matrix_tables(engine)
        with engine.connect() as c:
            enabled_row = c.execute(
                text(f"SELECT is_enabled FROM {CFG_TABLE} WHERE id = 1")
            ).scalar()
            enabled = bool(enabled_row) if enabled_row is not None else False
            rows = c.execute(text(f"""
                SELECT cont_pct_lo, cont_pct_hi, growth_pct
                FROM {BANDS_TABLE}
                ORDER BY seq ASC, cont_pct_lo ASC
            """)).fetchall()
        bands: List[Band] = []
        for lo, hi, g in rows:
            lo_f = float(lo)
            hi_f = float(hi) if hi is not None else None
            g_f  = float(g)
            # Defence: never let a stored growth<100 tighten the ceiling
            # below MBQ (spec §7). Clamp and log.
            if g_f < 100.0:
                logger.warning(
                    f"[sec_cap_matrix] band ({lo_f}, {hi_f}) has growth_pct={g_f} "
                    f"< 100; clamping to 100"
                )
                g_f = 100.0
            bands.append((lo_f, hi_f, g_f))
        if not bands:
            logger.warning("[sec_cap_matrix] table empty, matrix disabled")
            return False, []
        return enabled, bands
    except Exception as e:
        logger.warning(f"[sec_cap_matrix] load failed ({e}); matrix disabled")
        return False, []


def resolve_growth(
    cont_pct: float,
    bands: List[Band],
    fallback_pct: float,
) -> Tuple[float, bool]:
    """Return (growth_pct, matched_band_flag).

    Bands are half-open [lo, hi) per spec §2. The final band with hi=None is
    open-ended and matches everything from its lo upwards. When cont_pct
    falls in NO band (gap in a hand-edited table), returns (fallback_pct,
    False) so the caller can stamp SEC_CAP_MATRIX_GAP and continue.
    """
    if not bands:
        return fallback_pct, False
    for lo, hi, g in bands:
        if hi is None:
            if cont_pct >= lo:
                return g, True
        else:
            if lo <= cont_pct < hi:
                return g, True
    return fallback_pct, False


def snapshot_matrix(enabled: bool, bands: List[Band]) -> Dict[str, Any]:
    """Return the JSON-safe dict stamped into ARS_RUN_PARAMS_AUDIT.

    Historic runs can be traced back to the matrix active at the time.
    """
    return {
        "enabled": bool(enabled),
        "bands": [
            {"lo": float(lo),
             "hi": None if hi is None else float(hi),
             "growth": float(g)}
            for lo, hi, g in bands
        ],
    }


def validate_bands(bands: List[Dict[str, Any]]) -> Tuple[List[str], List[str]]:
    """Structural validation for a PUT payload.

    Returns (errors, warnings). errors → 400; warnings → 200 with a `warnings`
    field in the response.

    Enforced rules (per spec §4.5):
      1. bands non-empty and each row has numeric lo, growth (hi optional).
      2. bands[i].lo >= 0.
      3. bands sorted ascending by lo.
      4. Contiguous: bands[i].hi == bands[i+1].lo.
      5. Exactly one row has hi=None; must be the last row.
      6. Every growth >= 100 (matrix relaxes, never tightens).

    Warning-only:
      * Growth% doesn't strictly decrease as cont% increases (business
        intent is monotonic, but some operators may deliberately break it).
    """
    errors: List[str] = []
    warnings: List[str] = []
    if not bands:
        errors.append("bands must contain at least one row")
        return errors, warnings

    los = []
    his = []
    growths = []
    for i, b in enumerate(bands):
        if "lo" not in b or "growth" not in b:
            errors.append(f"row {i}: 'lo' and 'growth' are required")
            continue
        try:
            lo = float(b["lo"])
            g  = float(b["growth"])
            hi = None if b.get("hi") in (None, "") else float(b["hi"])
        except (TypeError, ValueError):
            errors.append(f"row {i}: numeric parse failed")
            continue
        if lo < 0:
            errors.append(f"row {i}: lo must be >= 0")
        if g < 100:
            errors.append(f"row {i}: growth must be >= 100 (matrix relaxes, never tightens)")
        los.append(lo)
        his.append(hi)
        growths.append(g)

    if errors:
        return errors, warnings

    # Sorted ascending by lo
    if los != sorted(los):
        errors.append("bands must be sorted ascending by lo")
    # Exactly one open-ended row and it must be last
    none_count = sum(1 for h in his if h is None)
    if none_count != 1:
        errors.append(f"exactly one row must have hi=null (found {none_count})")
    elif his[-1] is not None:
        errors.append("the open-ended row (hi=null) must be the last row")
    # Contiguous
    for i in range(len(bands) - 1):
        if his[i] is None:
            errors.append(f"row {i}: only the last row may have hi=null")
            break
        if abs(his[i] - los[i + 1]) > 1e-9:
            errors.append(f"row {i}: hi ({his[i]}) must equal next row's lo ({los[i + 1]})")

    # Monotonic warning (not an error)
    if not errors:
        strictly_decreasing = all(
            growths[i] >= growths[i + 1] for i in range(len(growths) - 1)
        )
        if not strictly_decreasing:
            warnings.append(
                "growth% is not monotonically non-increasing across bands; "
                "small contributors will not always get a larger stretch"
            )

    return errors, warnings
