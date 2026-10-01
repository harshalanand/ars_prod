"""
GRT ALC — Store × Article Extract: every row of a GRT ALC table for a store
list and an article list.

Ported from the Streamlit tool's extract.py, with the same rules:

  * Only vetted objects: the ten ARS_B2B_* tables and the two sources the
    module reads (the stock grid and Master_CONT_SZ). The name is interpolated
    into SQL, so it is always checked against this catalogue first.
  * The store and article key are found per table (STORE_CODE / ST_CD / WERKS,
    ART / ARTICLE_NUMBER); a table without one ignores that list, and says so.
  * The lists are staged into indexed temp tables and joined — no 2,100-
    parameter limit, faster than a long IN list.
  * An empty list means no filter on that column.
  * Count before fetch: the page shows the row count and which codes were not
    found before anything is downloaded.

Added: codes are cleaned the way they arrive from Excel ("1111090026.0" →
"1111090026", stray spaces), and the codes that matched nothing are listed,
not just counted.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import text

from app.database.session import data_engine
from app.services import b2b_schema as S

STORE_COLS = ("STORE_CODE", "ST_CD", "WERKS")
ART_COLS = ("ART", "ARTICLE_NUMBER")
SESSION_COL = "SESSION_ID"
STORE_HINTS = ("STORE_CODE", "STORECODE", "STORE", "ST_CD", "ST_CODE", "WERKS", "SITE", "CODE")
ART_HINTS = ("ART", "ARTICLE", "ARTICLE_NUMBER", "ARTICLENUMBER", "ART_NO", "ARTICLE_NO", "MATERIAL", "CODE")
PREVIEW = 200
XLSX_MAX = 1_048_575                       # Excel's row limit, less the header
SOURCES = (S.SRC_GRID, S.SRC_CONT)
TITLES = {S.BIN_MASTER: "Bin Master", S.STORE_MASTER: "Store Master", S.REQ: "REQ",
          S.ART_MBQ: "Demand (MBQ)", S.ALLOC: "Allocation lines", S.BIN_PLAN: "Pick list + leftovers",
          S.SESSION: "Sessions", S.UPLOAD: "Uploads", S.SETTING: "Settings", S.MBQ_BUILD: "Demand builds",
          S.SRC_GRID: "Store stock grid (source)", S.SRC_CONT: "Size contribution (source)"}


def _v(x):
    if isinstance(x, Decimal):
        return float(x)
    if isinstance(x, datetime):
        return x.isoformat()
    return x


def catalog() -> Dict[str, Dict[str, Any]]:
    """Every queryable object with its resolved key columns."""
    names = list(S.ALL_TABLES) + list(SOURCES)
    with data_engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT c.TABLE_NAME, c.COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS c
             WHERE c.TABLE_SCHEMA = 'dbo' AND c.TABLE_NAME IN ({', '.join(f"'{n}'" for n in names)})
             ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION""")).fetchall()
    cols: Dict[str, List[str]] = {}
    for t, c in rows:
        cols.setdefault(t, []).append(c)
    out = {}
    for name in names:
        if name not in cols:
            continue
        upper = {c.upper(): c for c in cols[name]}
        out[name] = {"object": name, "title": TITLES.get(name, name), "owned": name not in SOURCES,
                     "columns": cols[name],
                     "store_col": next((upper[c] for c in STORE_COLS if c in upper), None),
                     "art_col": next((upper[c] for c in ART_COLS if c in upper), None),
                     "session_col": upper.get(SESSION_COL)}
    return out


def objects() -> List[Dict[str, Any]]:
    cat = catalog()
    counts = S.table_counts()
    return [{**{k: m[k] for k in ("object", "title", "owned", "store_col", "art_col", "session_col")},
             "columns": len(m["columns"]), "rows": counts.get(m["object"])} for m in cat.values()]


def describe(obj: str) -> Dict[str, Any]:
    m = catalog().get(obj)
    if m is None:
        raise ValueError(f"{obj!r} is not a table this page can read")
    return m


# ── reading the lists ───────────────────────────────────────────────────────
def clean(codes: List[str]) -> List[str]:
    """Trim, drop blanks, undo Excel's float rendering of numeric codes, dedupe.
    Codes are compared without case in the database (hb76 = HB76), so they are
    de-duplicated the same way and kept upper-case."""
    out: Dict[str, str] = {}
    for c in codes:
        s = str(c).strip()
        if not s or s.lower() in ("nan", "none", "nat", "null"):
            continue
        s = re.sub(r"^(\d+)\.0+$", r"\1", s).upper()
        out.setdefault(s, s)
    return list(out)


def from_text(blob: Optional[str]) -> List[str]:
    """Codes typed or pasted: one a line, comma separated, a column out of Excel, or a mixture."""
    if not blob:
        return []
    return clean(re.split(r"[\s,;|]+", blob.strip()))


def from_file(data: bytes, name: str, kind: str, column: Optional[str] = None) -> Dict[str, Any]:
    """Codes from an uploaded csv / txt / xlsx. A one-column file needs no
    choice; otherwise the column is guessed from its name and can be overridden."""
    if re.search(r"\.(xlsx|xlsm|xls)$", name or "", re.I):
        df = pd.read_excel(io.BytesIO(data), dtype=str)
    else:
        raw = data.decode("utf-8-sig", errors="replace")
        first = next((ln for ln in raw.splitlines() if ln.strip()), "")
        # a fixed set, not pandas' sniffer: it split "STORE_CODE" on the letter O in the tool
        sep = next((d for d in ("\t", ",", ";", "|") if d in first), ",")
        df = pd.read_csv(io.StringIO(raw), dtype=str, sep=sep)
    cols = [str(c) for c in df.columns]
    if df.empty or not cols:
        return {"codes": [], "columns": cols, "used": None}
    if column and column in cols:
        use = column
    else:
        hints = STORE_HINTS if kind == "store" else ART_HINTS
        upper = {c.strip().upper().replace(" ", "_"): c for c in cols}
        use = next((upper[h] for h in hints if h in upper), cols[0])
    return {"codes": clean(df[use].astype(str).tolist()), "columns": cols, "used": use}


# ── querying ────────────────────────────────────────────────────────────────
def _stage(cur, name: str, values: List[str]) -> None:
    cur.execute(f"IF OBJECT_ID('tempdb..{name}') IS NOT NULL DROP TABLE {name};")
    # tempdb's collation is not Rep_Data's; without this the join fails outright.
    cur.execute(f"CREATE TABLE {name} (V NVARCHAR(100) COLLATE DATABASE_DEFAULT NOT NULL PRIMARY KEY);")
    if values:
        cur.fast_executemany = True
        cur.executemany(f"INSERT INTO {name} (V) VALUES (?)", [(v,) for v in values])
        cur.fast_executemany = False


def _where(m: Dict[str, Any], stores, arts, session_id) -> Tuple[str, list]:
    bits, params = [], []
    if stores and m["store_col"]:
        bits.append(f"EXISTS (SELECT 1 FROM #f_store s WHERE s.V = t.[{m['store_col']}])")
    if arts and m["art_col"]:
        bits.append(f"EXISTS (SELECT 1 FROM #f_art a WHERE {_art_eq(m['art_col'], 'a.V')})")
    if session_id is not None and m["session_col"]:
        bits.append(f"t.[{m['session_col']}] = ?")
        params.append(int(session_id))
    return (" WHERE " + " AND ".join(bits)) if bits else "", params


def _art_eq(col: str, v: str) -> str:
    """The article comparison. ARTICLE_NUMBER is BIGINT in the grid: convert the
    code, not the column, so the grid can still seek on its key."""
    if col.upper() == "ARTICLE_NUMBER":
        return f"t.[{col}] = TRY_CONVERT(BIGINT, {v})"
    return f"t.[{col}] = {v}"


def _prepare(cur, m, stores, arts):
    _stage(cur, "#f_store", stores if m["store_col"] else [])
    _stage(cur, "#f_art", arts if m["art_col"] else [])


def _matched(cur, m, obj, stores, arts, session_id) -> List[Dict[str, Any]]:
    sess = f" AND t.[{m['session_col']}] = ?" if (session_id is not None and m["session_col"]) else ""
    params = [int(session_id)] if sess else []
    out = []
    for label, uploaded, col, tmp in (("Stores", stores, m["store_col"], "#f_store"),
                                      ("Articles", arts, m["art_col"], "#f_art")):
        if not uploaded:
            out.append({"list": label, "given": 0, "found": 0, "missing": [], "note": "no list — all included"})
            continue
        if col is None:
            out.append({"list": label, "given": len(uploaded), "found": 0, "missing": [],
                        "note": f"this table has no {'store' if label == 'Stores' else 'article'} column — list ignored"})
            continue
        cond = _art_eq(col, "f.V") if label == "Articles" else f"t.[{col}] = f.V"
        cur.execute(f"""SELECT f.V FROM {tmp} f
                         WHERE NOT EXISTS (SELECT 1 FROM dbo.[{obj}] t WHERE {cond}{sess})
                         ORDER BY f.V""", *params)
        missing = [r[0] for r in cur.fetchall()]
        out.append({"list": label, "given": len(uploaded), "found": len(uploaded) - len(missing),
                    "missing": missing[:200], "missing_count": len(missing),
                    "note": "" if not missing else "not in this table — a typo, a stale code, or the wrong column"})
    return out


def preview(obj: str, stores: List[str], arts: List[str], session_id: Optional[int] = None) -> Dict[str, Any]:
    """Row count, which codes matched, and the first rows — one connection, staged once."""
    m = describe(obj)
    stores, arts = clean(stores or []), clean(arts or [])
    raw = data_engine.raw_connection()
    try:
        cur = raw.cursor()
        _prepare(cur, m, stores, arts)
        where, params = _where(m, stores, arts, session_id)
        cur.execute(f"SELECT COUNT_BIG(*) FROM dbo.[{obj}] t{where}", *params)
        total = int(cur.fetchone()[0])
        cur.execute(f"SELECT TOP {PREVIEW} t.* FROM dbo.[{obj}] t{where}", *params)
        cols = [d[0] for d in cur.description]
        rows = [[_v(v) for v in r] for r in cur.fetchall()]
        matched = _matched(cur, m, obj, stores, arts, session_id)
        raw.commit()
    finally:
        raw.close()
    return {"object": obj, "title": m["title"], "total": total, "columns": cols, "rows": rows,
            "matched": matched, "xlsx_ok": total <= XLSX_MAX,
            "filters": {"store": m["store_col"], "art": m["art_col"], "session": m["session_col"]}}


def download(obj: str, stores: List[str], arts: List[str], session_id: Optional[int], fmt: str) -> Tuple[str, bytes, str]:
    """(file name, bytes, media type) for every matching row."""
    m = describe(obj)
    stores, arts = clean(stores or []), clean(arts or [])
    raw = data_engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.arraysize = 50_000
        _prepare(cur, m, stores, arts)
        where, params = _where(m, stores, arts, session_id)
        if fmt == "xlsx":
            cur.execute(f"SELECT COUNT_BIG(*) FROM dbo.[{obj}] t{where}", *params)
            n = int(cur.fetchone()[0])
            if n > XLSX_MAX:
                raise ValueError(f"{n:,} rows is more than Excel holds; download CSV instead")
        cur.execute(f"SELECT t.* FROM dbo.[{obj}] t{where}", *params)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        raw.commit()
    finally:
        raw.close()
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    base = f"GRT_ALC_extract_{obj}{'_session' + str(session_id) if session_id and m['session_col'] else ''}_{stamp}"
    if fmt == "xlsx":
        import xlsxwriter
        buf = io.BytesIO()
        wb = xlsxwriter.Workbook(buf, {"in_memory": True})
        bold = wb.add_format({"bold": True, "bg_color": "#EEF2F7", "border": 1})
        ws = wb.add_worksheet(obj[:31])
        ws.write_row(0, 0, cols, bold)
        ws.freeze_panes(1, 0)
        for i, r in enumerate(rows, start=1):
            ws.write_row(i, 0, [_v(v) for v in r])
        wb.close()
        return f"{base}.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    sbuf = io.StringIO()
    w = csv.writer(sbuf)
    w.writerow(cols)
    w.writerows(rows)
    return f"{base}.csv", sbuf.getvalue().encode("utf-8-sig"), "text/csv"
