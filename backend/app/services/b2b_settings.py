"""
GRT ALC — the 19 settings that drive the demand build and the allocation.

Ported from the Streamlit tool's config.MBQ_DEFAULTS / SETTING_HELP. The tool
has a 20th, ALLOC_CHUNK_BY, which it documents as memory-only ("the allocated
quantities are identical either way"). ARS always chunks by category, so that
knob is not carried over.

Seeding. The tool stores only the settings someone saved — 14 of 19 on
HOPC866 today — and silently uses code defaults for the rest. Here every
setting always has a row. On first use each missing row is seeded:

    LEGACY   copied from the tool's B2B_MBQ_SETTING, when it has a valid value
    DEFAULT  the tool's code default, when it does not

LEGACY first so that, while both systems run on the same workbook, they also
run on the same settings — otherwise the two could not be compared.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import data_engine
from app.services.b2b_schema import LEGACY_SETTING, SETTING, table_exists

MBQ, ALLOC = "MBQ", "ALLOC"


def _spec(key, group, kind, default, label, help_, *, choices=None, lo=None,
          applies=None) -> Dict[str, Any]:
    return {"key": key, "group": group, "kind": kind, "default": default,
            "label": label, "help": help_, "choices": choices or [],
            "min": lo, "applies_when": applies or {}}


SPECS: List[Dict[str, Any]] = [
    # ── Demand (MBQ) — changing any of these means the demand table is stale ──
    _spec("SHORT_DAYS", MBQ, "int", "60", "Norm days, fast sizes",
          "Norm period for the sizes in SHORT_SZ_LIST. Short sizes turn faster, "
          "so they get a shorter period.", lo=1),
    _spec("LONG_DAYS", MBQ, "int", "90", "Norm days, other sizes",
          "Norm period for every size not in SHORT_SZ_LIST.", lo=1),
    _spec("SHORT_SZ_LIST", MBQ, "list", "A,A_MIX,NA", "Fast sizes",
          "Comma-separated SZ values that use SHORT_DAYS."),
    _spec("TREAT_MAJCAT_MIX_AS_SHORT", MBQ, "bool", "1", "MIX categories use fast days",
          "A category whose name ends in MIX (LADIES_MIX, KIDS_MIX, MENS_MIX) also uses "
          "SHORT_DAYS. No SZ value A_MIX exists in the data, so this is how the A_MIX "
          "rule is actually applied."),
    _spec("DEFAULT_SALE_COVER_DAYS", MBQ, "int", "30", "Default sale cover days",
          "Used wherever ARS_B2B_REQ.SALE_COVER_DAYS is blank. The workbook has no "
          "such column, so after a fresh load this applies to every row.", lo=0),
    _spec("TREAT_MISSING_STK_AS_ZERO", MBQ, "bool", "0", "No grid row means zero stock",
          "Off: a store × article with no row in the stock grid is not a candidate. "
          "On: it counts as zero stock, so its whole target becomes shortfall. Only "
          "about 7.5% of combinations exist in the grid, so On makes the demand table "
          "far larger — it is why the tool's table holds 19M rows."),
    _spec("CONT_SOURCE_MODE", MBQ, "choice", "HYBRID", "Size share source",
          "Where the size contribution % comes from. It is the only size-aware term in "
          "the target, because ACC_D is a category figure. HYBRID keeps "
          "Master_CONT_SZ wherever it states a positive share, fills its gaps from the "
          "REQ sheet, and rescales the mix to 1.00. REQ uses the REQ sheet only. "
          "MASTER is the original behaviour; it scored 20,241 REQ lines (309,710 units) "
          "at 0% because a missing store row fell through to a company row of 0.00.",
          choices=["HYBRID", "REQ", "MASTER"]),
    _spec("CONT_APPLY", MBQ, "bool", "1", "Scale ACC_D by size share",
          "On: each article gets its own share of the category requirement. Off: use "
          "the raw ACC_D for every size; the share is still stored for reference."),
    _spec("CONT_FULL_SZ_LIST", MBQ, "list", "A,NA", "Sizes forced to 100%",
          "MASTER mode only. Sizes that always take a share of 1, because one all-size "
          "article carries the whole category.",
          applies={"CONT_SOURCE_MODE": "MASTER"}),
    _spec("CONT_FALLBACK", MBQ, "num", "1", "Share when no row exists",
          "MASTER mode only. The share used when the store, category and size have no "
          "contribution row at all. 1 leaves ACC_D unscaled; 0 treats it as no "
          "requirement.", lo=0, applies={"CONT_SOURCE_MODE": "MASTER"}),
    _spec("MBQ_MIN_WHEN_CONT", MBQ, "num", "1", "Smallest target when share > 0",
          "A small share can compute to a fraction of a unit. This lifts it to a "
          "stockable quantity. A share of exactly 0 stays at a target of 0.", lo=0),
    _spec("MBQ_PRUNE_TO_REQ", MBQ, "bool", "1", "Build only rows a store can take",
          "On: build a store × article row only where the store needs at least "
          "MBQ_MIN_REQ_UNITS of that category and size. The allocator's REQ cap rejects "
          "everything else, so building it is wasted work (measured 44.7M → 12.0M rows)."),
    _spec("MBQ_MIN_REQ_UNITS", MBQ, "num", "1", "Minimum REQ to build a row",
          "The threshold MBQ_PRUNE_TO_REQ uses. 1 means the store must need at least one "
          "whole unit of that category and size.", lo=0),

    # ── Allocation — changing these only needs a new run ──
    _spec("ALLOC_PRIORITY", ALLOC, "choice", "SHORTFALL_DESC", "Who is served first",
          "Biggest shortfall, highest size share, or biggest target. Measured within "
          "0.004% of each other on total units; they change which store gets which "
          "article, not how much goes out.",
          choices=["SHORTFALL_DESC", "CONT_DESC", "MBQ_DESC"]),
    _spec("ALLOC_MIN_QTY", ALLOC, "int", "1", "Smallest line",
          "Never send fewer than this on one line. Under Round robin it is also the step "
          "each store takes per pass. It removes small lines, it does not round them up.",
          lo=1),
    _spec("ALLOC_FILL_MODE", ALLOC, "choice", "GREEDY", "How stock is handed out",
          "GREEDY fills the first store in the order before looking at the next; when "
          "stores tie, alphabetical store code decides, and early stores can sweep a "
          "scarce article (7 of 161 stores took all 890 units of LS_W_WNTR_SOCK / A). "
          "ROUND_ROBIN gives every competing store a step at a time.",
          choices=["GREEDY", "ROUND_ROBIN"]),
    _spec("ALLOC_FAIR_BASIS", ALLOC, "choice", "PROPORTIONAL", "Re-order between articles",
          "Round robin only. PROPORTIONAL serves the store furthest from its own "
          "requirement, so big stores still get more. FLAT serves whoever has received "
          "least. NONE keeps the priority order.",
          choices=["PROPORTIONAL", "FLAT", "NONE"],
          applies={"ALLOC_FILL_MODE": "ROUND_ROBIN"}),
    _spec("ALLOC_BIN_PICK", ALLOC, "choice", "MAX_CONSUMPTION", "Which bin a unit comes from",
          "Never changes what a store receives, only the pick list. MAX_CONSUMPTION uses "
          "the smallest bin that covers a whole line, else the largest first. BIN_ORDER "
          "walks bins alphabetically.",
          choices=["MAX_CONSUMPTION", "BIN_ORDER"]),
    _spec("ALLOC_CROSS_RDC", ALLOC, "choice", "ANY", "Which warehouse may ship",
          "ANY pools both warehouses; on HOPC866 on 29 Sep it sent 51.1% of units across. "
          "HOME_FIRST drains the store's own warehouse first, for the same total. SAME "
          "ships from the store's own warehouse only, and allocates fewer units. If "
          "DH24 and DW01 do not ship to each other's stores, ANY produces pick lists "
          "that cannot be worked.",
          choices=["ANY", "HOME_FIRST", "SAME"]),
]
BY_KEY: Dict[str, Dict[str, Any]] = {s["key"]: s for s in SPECS}


# ── validation ──────────────────────────────────────────────────────────────
def normalise(key: str, raw: Any) -> str:
    """Return the canonical string for `raw`, or raise ValueError."""
    s = BY_KEY.get(key)
    if s is None:
        raise ValueError(f"unknown setting {key!r}")
    v = "" if raw is None else str(raw).strip()
    kind = s["kind"]
    if kind == "bool":
        if v.lower() in ("1", "true", "on", "yes", "1.0"):
            return "1"
        if v.lower() in ("0", "false", "off", "no", "0.0"):
            return "0"
        raise ValueError(f"{key}: expected on/off, got {v!r}")
    if kind in ("int", "num"):
        try:
            x = float(v)
        except ValueError:
            raise ValueError(f"{key}: expected a number, got {v!r}") from None
        if kind == "int" and x != int(x):
            raise ValueError(f"{key}: expected a whole number, got {v!r}")
        if s["min"] is not None and x < s["min"]:
            raise ValueError(f"{key}: must be at least {s['min']:g}, got {x:g}")
        return str(int(x)) if kind == "int" else f"{x:g}"
    if kind == "choice":
        u = v.upper()
        if u not in s["choices"]:
            raise ValueError(f"{key}: must be one of {', '.join(s['choices'])}, got {v!r}")
        return u
    if kind == "list":
        parts = [p.strip() for p in v.split(",") if p.strip()]
        return ",".join(parts)
    raise ValueError(f"{key}: unsupported kind {kind}")


# ── seed / read / save ──────────────────────────────────────────────────────
def seed() -> Dict[str, str]:
    """Insert a row for every setting that has none. Never touches an existing
    row. Returns {key: seed_source} for the rows it inserted."""
    inserted: Dict[str, str] = {}
    with data_engine.begin() as conn:
        have = {r[0] for r in conn.execute(text(f"SELECT SETTING_KEY FROM dbo.{SETTING}"))}
        missing = [s for s in SPECS if s["key"] not in have]
        if not missing:
            return inserted
        legacy: Dict[str, str] = {}
        if table_exists(conn, LEGACY_SETTING):
            legacy = {r[0]: r[1] for r in conn.execute(text(
                f"SELECT SETTING_KEY, SETTING_VALUE FROM dbo.{LEGACY_SETTING}"))}
        for s in missing:
            src, val = "DEFAULT", s["default"]
            if s["key"] in legacy:
                try:
                    val, src = normalise(s["key"], legacy[s["key"]]), "LEGACY"
                except ValueError as e:
                    logger.warning(f"[b2b] legacy value ignored, using default: {e}")
            conn.execute(text(f"""
                INSERT INTO dbo.{SETTING}
                    (SETTING_KEY, SETTING_VALUE, SETTING_GROUP, NEEDS_REBUILD, SEED_SOURCE, UPDATED_BY)
                VALUES (:k, :v, :g, :r, :src, 'seed')"""),
                {"k": s["key"], "v": val, "g": s["group"],
                 "r": 1 if s["group"] == MBQ else 0, "src": src})
            inserted[s["key"]] = src
    if inserted:
        logger.info(f"[b2b] seeded {len(inserted)} settings: {inserted}")
    return inserted


def get_all() -> List[Dict[str, Any]]:
    """Every setting with its value, metadata and where the value came from."""
    from app.services.b2b_schema import ensure_tables
    ensure_tables()
    with data_engine.connect() as conn:
        rows = {r[0]: r for r in conn.execute(text(f"""
            SELECT SETTING_KEY, SETTING_VALUE, SEED_SOURCE, UPDATED_BY, UPDATED_AT
              FROM dbo.{SETTING}"""))}
    out = []
    for s in SPECS:
        r = rows.get(s["key"])
        out.append({**s,
                    "value": r[1] if r else None,
                    "source": r[2] if r else None,
                    "updated_by": r[3] if r else None,
                    "updated_at": r[4].isoformat() if r and r[4] else None,
                    "is_default": (r[1] if r else None) == s["default"]})
    return out


def effective() -> Dict[str, str]:
    """{key: value} as the build and the engine should use them."""
    return {s["key"]: (s["value"] if s["value"] is not None else s["default"])
            for s in get_all()}


def save(values: Dict[str, Any], user: Optional[str]) -> Dict[str, Any]:
    """Validate every value first; write nothing unless all are valid."""
    from app.services.b2b_schema import ensure_tables
    ensure_tables()
    clean = {k: normalise(k, v) for k, v in values.items()}   # raises on the first bad one
    current = effective()
    changed = {k: v for k, v in clean.items() if current.get(k) != v}
    if not changed:
        return {"changed": [], "needs_rebuild": False}
    with data_engine.begin() as conn:
        for k, v in changed.items():
            conn.execute(text(f"""
                UPDATE dbo.{SETTING}
                   SET SETTING_VALUE = :v, SEED_SOURCE = 'USER',
                       UPDATED_BY = :u, UPDATED_AT = SYSDATETIME()
                 WHERE SETTING_KEY = :k"""), {"k": k, "v": v, "u": user})
    needs = any(BY_KEY[k]["group"] == MBQ for k in changed)
    logger.info(f"[b2b] settings saved by {user}: {changed} (rebuild={needs})")
    return {"changed": sorted(changed), "needs_rebuild": needs}


def reset_to_defaults(keys: Optional[List[str]], user: Optional[str]) -> Dict[str, Any]:
    targets = keys or [s["key"] for s in SPECS]
    return save({k: BY_KEY[k]["default"] for k in targets if k in BY_KEY}, user)
