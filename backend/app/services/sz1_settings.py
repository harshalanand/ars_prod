"""
1SZ ALC — Step 1 rule settings (ARS_SZ1_SETTING). Spec: manual/sz1_alloc.md §B.

Every Step 1 rule has an on/off switch and a value, saved here and stamped on
each build's summary, so a build always says which values made it.

    R1  SZ not A / NA and, by R1_BASIS:
          cont   OPT_CONT_PCT ≤ R1_OPT_CONT_MAX   (company size curve, default)
          var    VAR_COUNT    ≤ R1_VAR_MAX        (the tool's live-size rule)
    R2  single A / NA size, and OPT_FNL ≤ OPT_FNL_MAX when R2_CAP_ON
    R3  CAT_FNL ≤ R3_CAT_FNL_MAX
    R4  OPT_FNL ≤ OPT_FNL_MAX            — the SAME option-qty variable as R2

A category must also be size-applicable (SZ_APPLICABLE = Y) — not a setting.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy import text

from app.database.session import data_engine
from app.services import sz1_schema as S

# key → (default, kind, label, min, max)
SPEC: Dict[str, tuple] = {
    "R1_ON":           (1, "bool", "R1 on", None, None),
    "R1_BASIS":        ("cont", "choice", "R1 basis", None, None),
    "R1_OPT_CONT_MAX": (40.0, "num", "R1 · option CONT % at most", 0, 100),
    "R1_VAR_MAX":      (2, "int", "R1 · live sizes at most", 1, 50),
    "R2_ON":           (1, "bool", "R2 on", None, None),
    "R2_CAP_ON":       (1, "bool", "R2 · apply the option qty cap", None, None),
    "R3_ON":           (1, "bool", "R3 on", None, None),
    "R3_CAT_FNL_MAX":  (200.0, "num", "R3 · category pcs at most", 0, 10_000_000),
    "R4_ON":           (1, "bool", "R4 on", None, None),
    "OPT_FNL_MAX":     (20.0, "num", "R2 + R4 · option pcs at most", 0, 10_000_000),
    # Zero-CONT gap: a size (or a whole option) with 0 company CONT is recorded in
    # ARS_SZ1_GAP and kept out of SZ Base. A / NA options carry 0 in the CO size
    # curve by design (no size), so they are exempt unless GAP_INCL_FULL_OPT.
    "GAP_EXCLUDE":       (1, "bool", "Keep zero-CONT sizes and options out of SZ Base", None, None),
    "GAP_INCL_FULL_OPT": (0, "bool", "Apply the zero-CONT gap to A / NA options too", None, None),
}
CHOICES = {"R1_BASIS": ("cont", "var")}

# The Streamlit tool's fixed rules — used by scripts/sz1_parity.py.
TOOL_PRESET: Dict[str, Any] = {"R1_ON": 1, "R1_BASIS": "var", "R1_VAR_MAX": 2, "R2_ON": 1, "R2_CAP_ON": 0,
                               "R3_ON": 1, "R3_CAT_FNL_MAX": 200.0, "R4_ON": 1, "OPT_FNL_MAX": 20.0,
                               "R1_OPT_CONT_MAX": 40.0, "GAP_EXCLUDE": 0, "GAP_INCL_FULL_OPT": 0}


def defaults() -> Dict[str, Any]:
    return {k: v[0] for k, v in SPEC.items()}


def clean(values: Dict[str, Any], base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`values` merged over `base` (default: defaults), every value checked.
    Raises ValueError naming the setting."""
    out = dict(base or defaults())
    for k, v in (values or {}).items():
        if k not in SPEC:
            raise ValueError(f"Unknown setting {k}")
        _, kind, label, lo, hi = SPEC[k]
        if kind == "bool":
            out[k] = 1 if str(v).strip().lower() in ("1", "true", "yes", "on") else 0
        elif kind == "choice":
            v = str(v).strip().lower()
            if v not in CHOICES[k]:
                raise ValueError(f"{label} must be one of {', '.join(CHOICES[k])}")
            out[k] = v
        else:
            try:
                num = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{label} must be a number") from None
            if num != num or num < lo or num > hi:
                raise ValueError(f"{label} must be between {lo:g} and {hi:g}")
            out[k] = int(num) if kind == "int" else round(num, 4)
    return out


def get_all() -> Dict[str, Any]:
    """Effective settings: saved values over the defaults."""
    S.ensure_tables()
    with data_engine.connect() as conn:
        saved = {r[0]: r[1] for r in conn.execute(text(f"SELECT SETTING_KEY, SETTING_VALUE FROM dbo.{S.SETTING}"))}
    return clean({k: v for k, v in saved.items() if k in SPEC})


def meta() -> Dict[str, Any]:
    """Who saved what, when — for the page."""
    S.ensure_tables()
    with data_engine.connect() as conn:
        r = conn.execute(text(f"SELECT TOP 1 UPDATED_BY, UPDATED_AT FROM dbo.{S.SETTING} ORDER BY UPDATED_AT DESC")).fetchone()
    return {"updated_by": r[0], "updated_at": r[1].isoformat() if r and r[1] else None} if r else {}


def save(values: Dict[str, Any], user: Optional[str]) -> Dict[str, Any]:
    new = clean(values, get_all())
    with data_engine.begin() as conn:
        for k, v in new.items():
            conn.execute(text(f"""
                MERGE dbo.{S.SETTING} AS t
                USING (SELECT :k AS SETTING_KEY) AS s ON t.SETTING_KEY = s.SETTING_KEY
                WHEN MATCHED AND ISNULL(t.SETTING_VALUE, '') <> :v
                    THEN UPDATE SET SETTING_VALUE = :v, UPDATED_BY = :by, UPDATED_AT = SYSDATETIME()
                WHEN NOT MATCHED THEN INSERT (SETTING_KEY, SETTING_VALUE, UPDATED_BY) VALUES (:k, :v, :by);"""),
                {"k": k, "v": str(v), "by": user})
    return new


def same(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> bool:
    """True when two settings snapshots select the same rows."""
    if not a or not b:
        return False
    if any(k not in a or k not in b for k in SPEC):     # a build from before a setting existed
        return False
    try:
        return clean(a) == clean(b)
    except ValueError:
        return False
