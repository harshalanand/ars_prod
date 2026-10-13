"""
1SZ ALC — Excel-style column filters, applied on the SERVER so a filter always
searches the whole table (not the rows on screen), plus the distinct values
each column offers in its filter list.

A filter set (`cf`, sent as JSON) maps a column to one spec:
    {"values": ["DH24", "DW01", null]}     the value is one of these (null = blank)
    {"expr": ">100"}                        numbers: >10 <=5 =0 10-20 ; text: contains
Column names are checked against the table's own columns, so nothing else
reaches the SQL.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from app.database.session import data_engine
from app.services import sz1_schema as S
from app.services.sz1_base_service import jsonable, q

TABLES = {"base": S.SZ_BASE, "req": S.REQ_BASE, "gap": S.GAP}
MAX_VALUES = 1000           # values one filter may select
DISTINCT_TOP = 300          # values a filter list shows (search narrows the rest)
_NUM_TYPES = {"int", "bigint", "smallint", "tinyint", "decimal", "numeric", "float", "real", "money", "bit"}


def columns(table: str) -> Dict[str, str]:
    """{COLUMN_NAME: data_type} of a module table."""
    with data_engine.connect() as conn:
        return {r[0]: r[1] for r in conn.execute(text(
            "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"), {"t": table})}


def parse(cf: Optional[str]) -> Dict[str, Any]:
    if not cf:
        return {}
    try:
        d = json.loads(cf)
    except ValueError:
        raise ValueError("Column filters are not valid JSON") from None
    return d if isinstance(d, dict) else {}


def where(table: str, cf: Dict[str, Any], prefix: str = "cf") -> Tuple[List[str], Dict[str, Any]]:
    """SQL conditions + params for a filter set on `table`."""
    cols = columns(table)
    out, p = [], {}
    for i, (col, spec) in enumerate((cf or {}).items()):
        if col not in cols or not isinstance(spec, dict):
            continue
        is_num = cols[col] in _NUM_TYPES
        if "values" in spec and isinstance(spec["values"], list):
            vals = spec["values"][:MAX_VALUES]
            if not vals:
                out.append("1 = 0")          # nothing ticked = nothing shown
                continue
            keep = [v for v in vals if v is not None and v != ""]
            names = []
            for j, v in enumerate(keep):
                k = f"{prefix}{i}_{j}"
                p[k] = v
                names.append(f":{k}")
            parts = [f"[{col}] IN ({', '.join(names)})"] if names else []
            if len(keep) < len(vals):
                parts.append(f"[{col}] IS NULL")
            out.append("(" + " OR ".join(parts) + ")")
        elif spec.get("expr") not in (None, ""):
            t = str(spec["expr"]).strip()
            k = f"{prefix}{i}"
            m = re.match(r"^(>=|<=|<>|>|<|=)\s*(-?\d+(\.\d+)?)$", t) if is_num else None
            r = re.match(r"^(-?\d+(\.\d+)?)\s*-\s*(-?\d+(\.\d+)?)$", t) if is_num else None
            if m:
                out.append(f"[{col}] {m.group(1)} :{k}"); p[k] = float(m.group(2))
            elif r:
                out.append(f"[{col}] BETWEEN :{k}a AND :{k}b"); p[f"{k}a"] = float(r.group(1)); p[f"{k}b"] = float(r.group(3))
            elif is_num and re.match(r"^-?\d+(\.\d+)?$", t):
                out.append(f"[{col}] = :{k}"); p[k] = float(t)
            else:
                out.append(f"CAST([{col}] AS NVARCHAR(400)) LIKE :{k}"); p[k] = f"%{t}%"
    return out, p


def order_by(table: str, col: Optional[str], direction: Optional[str], default: str) -> str:
    """ORDER BY for a clicked column header (checked against the table's own
    columns), the default order breaking ties; the default alone otherwise."""
    if not col or col not in columns(table):
        return default
    return f"[{col}] {'DESC' if str(direction).lower() == 'desc' else 'ASC'}, {default}"


def distinct(kind: str, col: str, cf: Dict[str, Any], search: Optional[str] = None,
             base_where: Optional[List[str]] = None, base_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The distinct values of `col` (with row counts) under every OTHER filter —
    what that column's filter list offers: text most frequent first, numbers in order."""
    table = TABLES.get(kind)
    if not table:
        raise ValueError("Unknown table")
    S.ensure_tables()
    cols = columns(table)
    if col not in cols:
        raise ValueError(f"Unknown column {col}")
    others = {k: v for k, v in (cf or {}).items() if k != col}
    conds, p = where(table, others)
    conds = list(base_where or []) + conds
    p.update(base_params or {})
    if search:
        conds.append(f"CAST([{col}] AS NVARCHAR(400)) LIKE :dq"); p["dq"] = f"%{search.strip()}%"
    w = ("WHERE " + " AND ".join(conds)) if conds else ""
    order = f"[{col}]" if cols[col] in _NUM_TYPES else f"COUNT(*) DESC, [{col}]"   # numbers by value, text by frequency
    with data_engine.connect() as conn:
        res = conn.execute(text(f"""
            SELECT TOP ({DISTINCT_TOP + 1}) [{col}] AS V, COUNT(*) AS N
              FROM {q(table)} WITH (NOLOCK) {w}
             GROUP BY [{col}] ORDER BY {order}"""), p).fetchall()
        total = conn.execute(text(f"SELECT COUNT(DISTINCT [{col}]) FROM {q(table)} WITH (NOLOCK) {w}"), p).scalar()
    vals = [{"v": jsonable(r[0]), "n": int(r[1])} for r in res[:DISTINCT_TOP]]
    return {"values": vals, "distinct": int(total or 0), "more": len(res) > DISTINCT_TOP,
            "numeric": cols[col] in _NUM_TYPES}
