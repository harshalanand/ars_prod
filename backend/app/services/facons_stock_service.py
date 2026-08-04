"""
FA & CONS stock service — SLOC on/off on the two LISTING SLOC tables + a dedicated
FA/CONS store-stock + MSA/DC calculation, persisted to SQL.

Model (kept deliberately simple):
  • ARS_STORE_SLOC_SETTINGS  = STORE bucket  → stock from ET_STORE_STOCK
  • ARS_MSA_SLOC_SETTINGS    = MSA/DC bucket → stock from ET_MSA_STK (enriched view)
The *table* decides the bucket. Per stream a SLOC is simply ACTIVE or not
(fa_active / co_active); if active, its stock (from that table's fact) is summed
into the bucket. Only DIV ∈ {FA, CO} counts. Listing's own columns
(status/kpi/sloc_type/is_active) are read-only here and never written by FA/CONS.

Results: ARS_FACONS_STOCK (store) / ARS_FACONS_MSA (dc) / ARS_FACONS_STOCK_SEQUENCE.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import data_engine

STORE_SETTINGS = "ARS_STORE_SLOC_SETTINGS"
MSA_SETTINGS = "ARS_MSA_SLOC_SETTINGS"
LEGACY_SLOC_TBL = "ARS_FACONS_SLOC_SETTINGS"
MPROD = "VW_MASTER_PRODUCT"
MASTER = "Master_ALC_INPUT_ST_MASTER"
STOCK_TBL = "ARS_FACONS_STOCK"
MSA_TBL = "ARS_FACONS_MSA"
SEQ_TBL = "ARS_FACONS_STOCK_SEQUENCE"
SLOC_QTY_CACHE = "ARS_FACONS_SLOC_STOCK_CACHE"
# The SLOC-qty probe (per-SLOC live stock for the settings page) is heavy (~6s: a
# cast master-join GROUP BY on ET_STORE_STOCK). It changes at most daily, so cache it
# and serve instantly within this TTL; the manual "Sync from stock" button forces a rebuild.
SLOC_QTY_TTL_MIN = 15

# Each SLOC settings table = one bucket, mapped to its own stock fact.
SLOC_SOURCES = [
    {"key": "STORE", "label": "Store", "settings": STORE_SETTINGS,
     "stock": "ET_STORE_STOCK", "discover": "ET_STORE_STOCK", "qty": "PARTICULARS_VALUE",
     "master_join": True, "loc": "s.WERKS", "target": STOCK_TBL, "loc_col": "st_cd",
     "ttl_col": "store_stk_ttl", "status_expr": "status", "status_kind": "str"},
    {"key": "MSA", "label": "MSA / DC", "settings": MSA_SETTINGS,
     "stock": "VW_ET_MSA_STK_WITH_MASTER", "discover": "ET_MSA_STK", "qty": "STK_Q",
     "master_join": False, "loc": "s.ST_CD", "target": MSA_TBL, "loc_col": "rdc",
     "ttl_col": "dc_stk_ttl", "status_expr": "is_active", "status_kind": "bit"},
]
_SRC_BY_KEY = {s["key"]: s for s in SLOC_SOURCES}

# REF_ART values in VW_MASTER_PRODUCT that mean "no reference article" — for these the
# module auto-generates a per-article key so distinct products never club together.
REF_MISSING = ("", "0", "NA", "N/A")
_REF_MISS_IN = ",".join(f"'{t}'" for t in REF_MISSING)


def _eff_ref_sql(ref_col: str, gen_col: str) -> str:
    """SQL for the EFFECTIVE ref_art: the real REF_ART, or 'G~'+gen_art when REF_ART is
    null/blank/0/NA (auto-generated so standalone items stay on their own line)."""
    return (f"CASE WHEN {ref_col} IS NULL "
            f"OR LTRIM(RTRIM(CAST({ref_col} AS NVARCHAR(50)))) IN ({_REF_MISS_IN}) "
            f"OR TRY_CAST({ref_col} AS FLOAT)=0 "
            f"THEN 'G~'+CAST({gen_col} AS NVARCHAR(30)) "
            f"ELSE CAST({ref_col} AS NVARCHAR(60)) END")


def _is_auto_sql(ref_col: str) -> str:
    return (f"CASE WHEN {ref_col} IS NULL "
            f"OR LTRIM(RTRIM(CAST({ref_col} AS NVARCHAR(50)))) IN ({_REF_MISS_IN}) "
            f"OR TRY_CAST({ref_col} AS FLOAT)=0 THEN 1 ELSE 0 END")

VALID_STREAM = ("FA", "CONS")
# Each stream maps STRICTLY to its own product division: FA→DIV 'FA', CONS→DIV 'CO'
# (matches how MBQ auto-segregates). A stream never captures the other's division —
# otherwise "Calculate ALL" would store each row under BOTH streams (duplicate rows).
STREAM_DIV = {"FA": ("FA",), "CONS": ("CO",)}


def _clean_divs(divs, stream: str) -> tuple:
    own = STREAM_DIV[_norm_stream(stream)]
    # requested divs are intersected with the stream's own division (invalid ones dropped)
    ds = tuple(d for d in (str(x).strip().upper() for x in (divs or ()) if str(x).strip()) if d in own)
    return ds or own


def _norm_stream(stream: str) -> str:
    s = (stream or "").strip().upper()
    if s not in VALID_STREAM:
        raise ValueError(f"stream must be one of {VALID_STREAM}, got {stream!r}")
    return s


def _active_col(stream: str) -> str:
    return "fa_active" if _norm_stream(stream) == "FA" else "co_active"


def _in_clause(vals: List[str], prefix: str, params: Dict[str, Any]) -> str:
    keys = []
    for i, v in enumerate(vals):
        k = f"{prefix}{i}"
        params[k] = v
        keys.append(f":{k}")
    return ", ".join(keys)


# ══════════════════════════════════════════════════════════════════════════════
# Provisioning — result tables + additive fa_active/co_active on both listing tables
# ══════════════════════════════════════════════════════════════════════════════
def ensure_tables() -> None:
    result_ddl = [
        f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{SEQ_TBL}')
        CREATE TABLE {SEQ_TBL} (
            sequence_id INT IDENTITY(1,1) PRIMARY KEY, stream NVARCHAR(10) NOT NULL, calc_date DATE NULL,
            slocs_store NVARCHAR(MAX) NULL, slocs_msa NVARCHAR(MAX) NULL,
            store_rows INT NOT NULL DEFAULT 0, msa_rows INT NOT NULL DEFAULT 0,
            status NVARCHAR(20) NOT NULL DEFAULT 'COMPLETED',
            created_by NVARCHAR(100) NULL, created_at DATETIME NOT NULL DEFAULT GETDATE());""",
        f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{STOCK_TBL}')
        CREATE TABLE {STOCK_TBL} (
            id INT IDENTITY(1,1) PRIMARY KEY, sequence_id INT NOT NULL, stream NVARCHAR(10) NOT NULL,
            st_cd NVARCHAR(20) NULL, maj_cat NVARCHAR(50) NULL, ref_art NVARCHAR(30) NULL,
            clr NVARCHAR(20) NULL, store_stk_ttl FLOAT NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT GETDATE());""",
        f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{MSA_TBL}')
        CREATE TABLE {MSA_TBL} (
            id INT IDENTITY(1,1) PRIMARY KEY, sequence_id INT NOT NULL, stream NVARCHAR(10) NOT NULL,
            rdc NVARCHAR(20) NULL, maj_cat NVARCHAR(50) NULL, ref_art NVARCHAR(30) NULL,
            clr NVARCHAR(20) NULL, dc_stk_ttl FLOAT NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT GETDATE());""",
        f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{SLOC_QTY_CACHE}')
        CREATE TABLE {SLOC_QTY_CACHE} (
            stream NVARCHAR(10) NOT NULL, source NVARCHAR(10) NOT NULL, sloc NVARCHAR(50) NOT NULL,
            qty FLOAT NOT NULL DEFAULT 0, snap_date DATE NULL,
            refreshed_at DATETIME NOT NULL DEFAULT GETDATE(),
            CONSTRAINT PK_FACONS_SLOC_QTY_CACHE PRIMARY KEY (stream, source, sloc));""",
    ]
    with data_engine.begin() as c:
        for stmt in result_ddl:
            c.execute(text(stmt))
        c.execute(text(f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_FACONS_STOCK_seq') CREATE INDEX IX_FACONS_STOCK_seq ON {STOCK_TBL}(stream, sequence_id);"))
        c.execute(text(f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_FACONS_MSA_seq') CREATE INDEX IX_FACONS_MSA_seq ON {MSA_TBL}(stream, sequence_id);"))
        # snapshot dates of each source, recorded per run
        c.execute(text(f"IF COL_LENGTH('{SEQ_TBL}','store_date') IS NULL ALTER TABLE {SEQ_TBL} ADD store_date DATE NULL;"))
        c.execute(text(f"IF COL_LENGTH('{SEQ_TBL}','msa_date') IS NULL ALTER TABLE {SEQ_TBL} ADD msa_date DATE NULL;"))
        for t in (STOCK_TBL, MSA_TBL):
            c.execute(text(f"IF COL_LENGTH('{t}','ref_art_desc') IS NULL ALTER TABLE {t} ADD ref_art_desc NVARCHAR(200) NULL;"))
            c.execute(text(f"IF COL_LENGTH('{t}','sloc') IS NULL ALTER TABLE {t} ADD sloc NVARCHAR(50) NULL;"))
            c.execute(text(f"IF COL_LENGTH('{t}','sz') IS NULL ALTER TABLE {t} ADD sz NVARCHAR(20) NULL;"))
            c.execute(text(f"IF COL_LENGTH('{t}','seg') IS NULL ALTER TABLE {t} ADD seg NVARCHAR(20) NULL;"))
            c.execute(text(f"IF COL_LENGTH('{t}','div') IS NULL ALTER TABLE {t} ADD div NVARCHAR(20) NULL;"))
            c.execute(text(f"IF COL_LENGTH('{t}','sub_div') IS NULL ALTER TABLE {t} ADD sub_div NVARCHAR(50) NULL;"))
            # Phase 1 ref_art grain: keep the ACTUAL article (for drill-down) + its gen_art;
            # `ref_art` now holds the EFFECTIVE ref (REF_ART, or 'G~'+gen when REF_ART is NA/blank/0).
            c.execute(text(f"IF COL_LENGTH('{t}','article_number') IS NULL ALTER TABLE {t} ADD article_number NVARCHAR(30) NULL;"))
            c.execute(text(f"IF COL_LENGTH('{t}','gen_art') IS NULL ALTER TABLE {t} ADD gen_art NVARCHAR(30) NULL;"))
            c.execute(text(f"IF COL_LENGTH('{t}','ref_auto') IS NULL ALTER TABLE {t} ADD ref_auto BIT NOT NULL DEFAULT 0;"))
        for t in (STORE_SETTINGS, MSA_SETTINGS):
            # Note whether the active flags exist BEFORE we add them — the legacy
            # role→active migration below must run ONLY on first creation.
            newly = {}
            for ac in ("fa_active", "co_active"):
                newly[ac] = c.execute(text(f"SELECT COL_LENGTH('{t}','{ac}')")).scalar() is None
            for col, typ in (("fa_active", "BIT NOT NULL DEFAULT 0"), ("co_active", "BIT NOT NULL DEFAULT 0"),
                             ("facons_updated_by", "NVARCHAR(100) NULL"), ("facons_updated_at", "DATETIME NULL")):
                c.execute(text(f"IF COL_LENGTH('{t}','{col}') IS NULL ALTER TABLE {t} ADD {col} {typ};"))
            # Migrate the earlier role columns (fa_role/co_role) → active EXACTLY ONCE, on
            # the run that first creates the active flag. Running it on every ensure_tables()
            # would re-activate any SLOC the user later turned OFF, so a Save that deactivates
            # a role-having SLOC would silently revert on the next page load ("save doesn't
            # stick"). Gated on `newly` so it never undoes a user edit again.
            for rc, ac in (("fa_role", "fa_active"), ("co_role", "co_active")):
                if newly.get(ac):
                    c.execute(text(
                        f"IF COL_LENGTH('{t}','{rc}') IS NOT NULL "
                        f"UPDATE {t} SET {ac}=1 WHERE {ac}=0 AND {rc} IS NOT NULL;"))
        _migrate_legacy(c)


def _migrate_legacy(conn) -> None:
    """Fold the old standalone ARS_FACONS_SLOC_SETTINGS active rows into the listing
    tables' active flags (scope STORE→store table, scope MSA→msa table), then drop it."""
    if not conn.execute(text("SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME=:t"),
                        {"t": LEGACY_SLOC_TBL}).first():
        return
    rows = conn.execute(text(
        f"SELECT stream, scope, sloc FROM {LEGACY_SLOC_TBL} WHERE is_active=1")).fetchall()
    for stream, scope, sloc in rows:
        try:
            ac = _active_col(stream)
        except ValueError:
            continue
        tbl = STORE_SETTINGS if str(scope).upper() == "STORE" else MSA_SETTINGS
        conn.execute(text(
            f"UPDATE {tbl} SET {ac}=1, facons_updated_by='migrate', facons_updated_at=GETDATE() WHERE sloc=:s"),
            {"s": str(sloc).strip()})
    conn.execute(text(f"DROP TABLE {LEGACY_SLOC_TBL}"))
    logger.info(f"[facons] migrated {len(rows)} legacy SLOC(s) to active flags; dropped {LEGACY_SLOC_TBL}")


# ══════════════════════════════════════════════════════════════════════════════
# SLOC on/off (fa_active / co_active on the listing tables)
# ══════════════════════════════════════════════════════════════════════════════
def list_sloc_settings(stream: str, scope: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every SLOC from both listing tables with its FA/CONS active flag for this
    stream + the (read-only) listing status/kpi. `scope` optionally limits to one
    source ('STORE' | 'MSA')."""
    ensure_tables()
    stream = _norm_stream(stream)
    ac = _active_col(stream)
    want = (scope or "").strip().upper() or None
    out: List[Dict[str, Any]] = []
    with data_engine.connect() as c:
        for src in SLOC_SOURCES:
            if want and src["key"] != want:
                continue
            rows = c.execute(text(
                f"SELECT sloc, kpi, {src['status_expr']} AS st, {ac} AS active, "
                f"facons_updated_by, facons_updated_at FROM {src['settings']} ORDER BY sloc")).mappings().all()
            for r in rows:
                listing = (("Active" if r["st"] else "Inactive") if src["status_kind"] == "bit"
                           else (r["st"] or "—"))
                out.append({
                    "source": src["key"], "settings_table": src["settings"], "sloc": r["sloc"],
                    "kpi": r["kpi"], "listing_status": listing, "active": bool(r["active"]),
                    "updated_by": r["facons_updated_by"],
                    "updated_at": r["facons_updated_at"].isoformat() if r["facons_updated_at"] else None,
                })
    return out


def bulk_update_sloc_settings(items: List[Dict[str, Any]], user: Optional[str] = None) -> int:
    """Set FA/CONS active on/off for SLOCs. Each item: {source, sloc, stream, active}."""
    ensure_tables()
    n = 0
    with data_engine.begin() as c:
        for it in items:
            src = _SRC_BY_KEY.get(str(it.get("source", "")).strip().upper())
            if not src:
                raise ValueError(f"source must be one of {list(_SRC_BY_KEY)}, got {it.get('source')!r}")
            ac = _active_col(it.get("stream"))
            sloc = (it.get("sloc") or "").strip()
            if not sloc:
                continue
            c.execute(text(
                f"UPDATE {src['settings']} SET {ac}=:a, facons_updated_by=:u, facons_updated_at=GETDATE() WHERE sloc=:s"),
                {"a": 1 if it.get("active") else 0, "u": user, "s": sloc})
            n += 1
    return n


def sync_slocs(stream: str, user: Optional[str] = None) -> Dict[str, Any]:
    """Additively insert any SLOC present in each table's stock fact but not yet in
    the listing table (as Inactive) — so the FA/CONS page shows the full list."""
    ensure_tables()
    _norm_stream(stream)
    added = 0
    with data_engine.begin() as c:
        for src in SLOC_SOURCES:
            slocs = [str(r[0]).strip() for r in c.execute(text(
                f"SELECT DISTINCT SLOC FROM {src['discover']} WHERE SLOC IS NOT NULL")).fetchall()
                if r[0] and str(r[0]).strip()]
            for sl in slocs:
                if src["status_kind"] == "bit":
                    stmt = (f"IF NOT EXISTS (SELECT 1 FROM {src['settings']} WHERE sloc=:s) "
                            f"INSERT INTO {src['settings']} (sloc, sloc_type, is_active) VALUES (:s,'FRESH',0)")
                else:
                    stmt = (f"IF NOT EXISTS (SELECT 1 FROM {src['settings']} WHERE sloc=:s) "
                            f"INSERT INTO {src['settings']} (sloc, status) VALUES (:s,'Inactive')")
                res = c.execute(text(stmt), {"s": sl})
                added += res.rowcount if res.rowcount and res.rowcount > 0 else 0
    return {"stream": _norm_stream(stream), "added": added}


def _read_qty_cache(conn, stream: str):
    """Return (totals, dates, refreshed_at, fresh) from the cache table, or (…, None, False)
    when empty. `fresh` = youngest cache row is within the TTL."""
    rows = conn.execute(text(
        f"SELECT source, sloc, qty, snap_date, refreshed_at, "
        f"       DATEDIFF(MINUTE, refreshed_at, GETDATE()) AS age_min "
        f"FROM {SLOC_QTY_CACHE} WHERE stream=:st"), {"st": stream}).mappings().all()
    if not rows:
        return {}, {}, None, False
    totals: Dict[str, Dict[str, float]] = {}
    dates: Dict[str, Any] = {}
    refreshed = None
    fresh = True
    for r in rows:
        totals.setdefault(r["source"], {})[str(r["sloc"])] = float(r["qty"] or 0)
        if r["snap_date"] is not None:
            dates[r["source"]] = r["snap_date"].isoformat()
        refreshed = r["refreshed_at"].isoformat() if r["refreshed_at"] else refreshed
        if (r["age_min"] or 0) > SLOC_QTY_TTL_MIN:
            fresh = False
    return totals, dates, refreshed, fresh


def _compute_qty(conn, stream: str, divlist, date: Optional[str]):
    """Live per-SLOC stock probe for both sources at their latest snapshot. Returns
    (totals{src:{sloc:qty}}, dates{src:date})."""
    totals: Dict[str, Dict[str, float]] = {}
    dates: Dict[str, Any] = {}
    for src in SLOC_SOURCES:
        d = date or _latest_date(conn, src["stock"])
        dates[src["key"]] = d
        p: Dict[str, Any] = {}
        din = _in_clause(list(divlist), "dv", p)
        dpred = ""
        if d:
            p["d"] = d
            dpred = " AND CAST(s.[DATE] AS DATE)=:d"
        qty = f"SUM(ISNULL(TRY_CAST(s.{src['qty']} AS FLOAT),0))"
        if src["master_join"]:
            sql = f"""
                SELECT s.SLOC AS sloc, {qty} AS q
                FROM {src['stock']} s
                INNER JOIN {MPROD} mp ON CAST(mp.ARTICLE_NUMBER AS NVARCHAR(50)) = CAST(s.MATNR AS NVARCHAR(50))
                WHERE s.SLOC IS NOT NULL AND UPPER(mp.DIV) IN ({din}){dpred}
                GROUP BY s.SLOC"""
        else:
            sql = f"""
                SELECT s.SLOC AS sloc, {qty} AS q
                FROM {src['stock']} s
                WHERE s.SLOC IS NOT NULL AND UPPER(s.DIV) IN ({din}){dpred}
                GROUP BY s.SLOC"""
        m: Dict[str, float] = {}
        for r in conn.execute(text(sql), p).fetchall():
            sl = str(r[0] or "").strip()
            if sl:
                # Clamp negative net stock to 0 — a SLOC whose issues exceed receipts has
                # no dispatchable stock (same max(…,0) rule MSA FNL_Q uses). Without this a
                # large negative SLOC (e.g. CONS MSA 0044) shrinks the denominator below a
                # single positive SLOC, producing nonsense shares like 201%.
                m[sl] = max(float(r[1] or 0), 0.0)
        totals[src["key"]] = m
    return totals, dates


def _discover_from_totals(conn, totals) -> int:
    """Fold SLOC discovery into the totals scan — any SLOC the probe surfaced but the
    listing table lacks is inserted as Inactive. Avoids a separate DISTINCT-scan sync."""
    added = 0
    for src in SLOC_SOURCES:
        for sl in totals.get(src["key"], {}):
            if src["status_kind"] == "bit":
                stmt = (f"IF NOT EXISTS (SELECT 1 FROM {src['settings']} WHERE sloc=:s) "
                        f"INSERT INTO {src['settings']} (sloc, sloc_type, is_active) VALUES (:s,'FRESH',0)")
            else:
                stmt = (f"IF NOT EXISTS (SELECT 1 FROM {src['settings']} WHERE sloc=:s) "
                        f"INSERT INTO {src['settings']} (sloc, status) VALUES (:s,'Inactive')")
            res = conn.execute(text(stmt), {"s": sl})
            added += res.rowcount if res.rowcount and res.rowcount > 0 else 0
    return added


def _write_qty_cache(conn, stream: str, totals, dates) -> None:
    conn.execute(text(f"DELETE FROM {SLOC_QTY_CACHE} WHERE stream=:st"), {"st": stream})
    for src_key, m in totals.items():
        d = dates.get(src_key)
        for sl, q in m.items():
            conn.execute(text(
                f"INSERT INTO {SLOC_QTY_CACHE} (stream, source, sloc, qty, snap_date) "
                f"VALUES (:st,:src,:sl,:q,:d)"),
                {"st": stream, "src": src_key, "sl": sl, "q": float(q or 0), "d": d})


def sloc_stock_totals(stream: str, date: Optional[str] = None,
                      force: bool = False) -> Dict[str, Any]:
    """Live per-SLOC stock qty for the stream's OWN division (FA→FA, CONS→CO), across
    ALL SLOCs (active or not) at each source's latest snapshot — powers the SLOC Settings
    page so you can see which SLOCs carry stock before turning them on.

    Cached (TTL {SLOC_QTY_TTL_MIN} min) because the probe is heavy (~6s: a cast master-join
    GROUP BY on ET_STORE_STOCK) and the snapshot changes at most daily. On a cache miss it
    also folds in SLOC discovery (new SLOCs → inserted Inactive) from the same scan, so the
    page needs no separate DISTINCT-scan sync. `force=True` (or an explicit `date`) rebuilds.
    NOT the persisted calc — a direct probe of the fact tables."""
    ensure_tables()
    stream = _norm_stream(stream)
    divlist = STREAM_DIV[stream]
    explicit = bool(date)

    if not force and not explicit:
        with data_engine.connect() as c:
            totals, dates, refreshed, fresh = _read_qty_cache(c, stream)
        if totals and fresh:
            return {"stream": stream, "totals": totals, "dates": dates,
                    "cached": True, "refreshed_at": refreshed, "added": 0}

    with data_engine.begin() as c:
        totals, dates = _compute_qty(c, stream, divlist, date)
        added = _discover_from_totals(c, totals)
        if not explicit:
            _write_qty_cache(c, stream, totals, dates)
    return {"stream": stream, "totals": totals, "dates": dates,
            "cached": False, "added": added}


# ══════════════════════════════════════════════════════════════════════════════
# The calc — sum each table's ACTIVE SLOCs from its own fact, DIV ∈ FA/CO
# ══════════════════════════════════════════════════════════════════════════════
def _latest_date(conn, src: str) -> Optional[str]:
    row = conn.execute(text(f"SELECT MAX(CAST([DATE] AS DATE)) FROM {src}")).first()
    return row[0].isoformat() if row and row[0] else None


def _active_slocs(conn, src, ac: str) -> List[str]:
    rows = conn.execute(text(
        f"SELECT sloc FROM {src['settings']} WHERE {ac}=1")).fetchall()
    return [str(r[0]).strip() for r in rows if r[0] and str(r[0]).strip()]


def _sum_source(conn, seq, stream, src, slocs, divlist, date) -> int:
    if not slocs:
        return 0
    p: Dict[str, Any] = {"seq": seq, "st": stream}
    dcpred = ""
    if date:
        p["d"] = date
        dcpred = " AND CAST(s.[DATE] AS DATE)=:d"
    sin = _in_clause(slocs, "sl", p)
    din = _in_clause(list(divlist), "dv", p)
    qty = f"SUM(ISNULL(TRY_CAST(s.{src['qty']} AS FLOAT),0))"
    cols = (f"(sequence_id, stream, {src['loc_col']}, sloc, seg, div, sub_div, maj_cat, "
            f"article_number, gen_art, ref_art, ref_auto, ref_art_desc, clr, sz, {src['ttl_col']})")
    if src["master_join"]:
        # STORE: fact ET_STORE_STOCK (article = MATNR); INNER JOIN master for REF_ART + identity.
        eff = _eff_ref_sql("mp.REF_ART", "mp.GEN_ART_NUMBER")
        auto = _is_auto_sql("mp.REF_ART")
        sql = f"""
            INSERT INTO {src['target']} {cols}
            SELECT :seq, :st, {src['loc']}, s.SLOC, MAX(mp.SEG), MAX(mp.DIV), MAX(mp.SUB_DIV), MAX(mp.MAJ_CAT),
                   CAST(s.MATNR AS NVARCHAR(30)), MAX(CAST(mp.GEN_ART_NUMBER AS NVARCHAR(30))),
                   MAX({eff}), MAX({auto}), MAX(CAST(mp.GEN_ART_DESC AS NVARCHAR(200))),
                   MAX(CAST(mp.CLR AS NVARCHAR(20))), MAX(ISNULL(mp.SZ,'')), {qty}
            FROM {src['stock']} s
            INNER JOIN {MPROD} mp ON CAST(mp.ARTICLE_NUMBER AS NVARCHAR(50)) = CAST(s.MATNR AS NVARCHAR(50))
            WHERE s.SLOC IN ({sin}) AND UPPER(mp.DIV) IN ({din}){dcpred}
            GROUP BY {src['loc']}, s.SLOC, CAST(s.MATNR AS NVARCHAR(30))
            HAVING {qty} <> 0"""
    else:
        # MSA/DC: fact view already has identity; LEFT JOIN master only to fetch REF_ART
        # (the view has no REF_ART col). Fall back to the view's gen_art if not in master.
        eff = _eff_ref_sql("mp.REF_ART", "COALESCE(mp.GEN_ART_NUMBER, s.GEN_ART_NUMBER)")
        auto = _is_auto_sql("mp.REF_ART")
        sql = f"""
            INSERT INTO {src['target']} {cols}
            SELECT :seq, :st, {src['loc']}, s.SLOC, MAX(s.SEG), MAX(s.DIV), MAX(s.SUB_DIV), MAX(s.MAJ_CAT),
                   CAST(s.ARTICLE_NUMBER AS NVARCHAR(30)), MAX(CAST(COALESCE(mp.GEN_ART_NUMBER, s.GEN_ART_NUMBER) AS NVARCHAR(30))),
                   MAX({eff}), MAX({auto}), MAX(CAST(s.GEN_ART_DESC AS NVARCHAR(200))),
                   MAX(CAST(s.CLR AS NVARCHAR(20))), MAX(ISNULL(s.SZ,'')), {qty}
            FROM {src['stock']} s
            LEFT JOIN {MPROD} mp ON CAST(mp.ARTICLE_NUMBER AS NVARCHAR(50)) = CAST(s.ARTICLE_NUMBER AS NVARCHAR(50))
            WHERE s.SLOC IN ({sin}) AND UPPER(s.DIV) IN ({din}){dcpred}
            GROUP BY {src['loc']}, s.SLOC, CAST(s.ARTICLE_NUMBER AS NVARCHAR(30))
            HAVING {qty} <> 0"""
    return conn.execute(text(sql), p).rowcount or 0


def calculate(stream: str, date: Optional[str] = None, divs=None,
              user: Optional[str] = None) -> Dict[str, Any]:
    """Store stock = active SLOCs of ARS_STORE_SLOC_SETTINGS (ET_STORE_STOCK);
    MSA/DC = active SLOCs of ARS_MSA_SLOC_SETTINGS (ET_MSA_STK). DIV ∈ divs (FA+CO).
    Keeps only the latest run per stream."""
    ensure_tables()
    stream = _norm_stream(stream)
    ac = _active_col(stream)
    divlist = _clean_divs(divs, stream)
    with data_engine.begin() as c:
        seq = c.execute(text(
            f"INSERT INTO {SEQ_TBL} (stream, calc_date, created_by, status) "
            f"OUTPUT INSERTED.sequence_id VALUES (:st,:d,:usr,'RUNNING')"),
            {"st": stream, "d": date, "usr": user}).scalar()
        c.execute(text(f"DELETE FROM {STOCK_TBL} WHERE stream=:st"), {"st": stream})
        c.execute(text(f"DELETE FROM {MSA_TBL} WHERE stream=:st"), {"st": stream})

        store_src, msa_src = SLOC_SOURCES[0], SLOC_SOURCES[1]
        store_slocs = _active_slocs(c, store_src, ac)
        msa_slocs = _active_slocs(c, msa_src, ac)
        store_date = date or _latest_date(c, store_src["stock"])
        msa_date = date or _latest_date(c, msa_src["stock"])
        store_rows = _sum_source(c, seq, stream, store_src, store_slocs, divlist, store_date)
        msa_rows = _sum_source(c, seq, stream, msa_src, msa_slocs, divlist, msa_date)

        # representative snapshot date for the run = whichever source actually had data
        calc_date = store_date if store_rows else (msa_date or store_date)
        c.execute(text(
            f"UPDATE {SEQ_TBL} SET calc_date=:d, store_date=:sd, msa_date=:md, slocs_store=:ss, slocs_msa=:sm, "
            f"store_rows=:sr, msa_rows=:mr, status='COMPLETED' WHERE sequence_id=:seq"),
            {"d": calc_date, "sd": store_date, "md": msa_date, "ss": json.dumps(sorted(store_slocs)),
             "sm": json.dumps(sorted(msa_slocs)), "sr": store_rows, "mr": msa_rows, "seq": seq})

    logger.info(f"[facons] stock calc stream={stream} seq={seq} store_rows={store_rows} "
                f"msa_rows={msa_rows} store_slocs={sorted(store_slocs)} msa_slocs={sorted(msa_slocs)}")
    warn = []
    if not store_slocs:
        warn.append("Store stock empty — no active SLOC in ARS_STORE_SLOC_SETTINGS.")
    if not msa_slocs:
        warn.append("MSA/DC empty — no active SLOC in ARS_MSA_SLOC_SETTINGS.")
    return {
        "sequence_id": seq, "stream": stream, "calc_date": calc_date, "divs": list(divlist),
        "store_slocs": sorted(store_slocs), "msa_slocs": sorted(msa_slocs),
        "store_rows": store_rows, "msa_rows": msa_rows, "warnings": warn,
    }


def latest_stock_maps(conn, stream: str):
    """For MBQ enrichment: {(st_cd, ref_art): store_stk} and {(rdc, ref_art): dc_stk}
    from the latest persisted run (the result tables hold only the latest run)."""
    stream = _norm_stream(stream)
    store = {}
    for r in conn.execute(text(
        f"SELECT st_cd, ref_art, SUM(store_stk_ttl) q FROM {STOCK_TBL} WHERE stream=:st "
        f"GROUP BY st_cd, ref_art"), {"st": stream}).fetchall():
        store[(str(r[0]), str(r[1]))] = float(r[2] or 0)
    dc = {}
    for r in conn.execute(text(
        f"SELECT rdc, ref_art, SUM(dc_stk_ttl) q FROM {MSA_TBL} WHERE stream=:st "
        f"GROUP BY rdc, ref_art"), {"st": stream}).fetchall():
        dc[(str(r[0]), str(r[1]))] = float(r[2] or 0)
    return store, dc


def get_results(stream: str, scope: str, sequence_id: Optional[int] = None,
                limit: int = 500, grain: str = "ref") -> Dict[str, Any]:
    """grain='ref' (default) → one row per effective ref_art, member articles clubbed
    (stock summed). grain='article' → one row per actual ARTICLE_NUMBER (drill-down)."""
    ensure_tables()
    st_in = (stream or "").strip().upper()
    is_all = st_in in ("", "ALL")           # ALL → both FA + CONS together
    st = None if is_all else _norm_stream(stream)
    scope = (scope or "").strip().upper()
    if scope not in ("STORE", "MSA"):
        raise ValueError("scope must be STORE or MSA")
    grain = (grain or "ref").strip().lower()
    if grain not in ("ref", "article"):
        raise ValueError("grain must be 'ref' or 'article'")
    tbl = STOCK_TBL if scope == "STORE" else MSA_TBL
    loc_col = "st_cd" if scope == "STORE" else "rdc"
    ttl_col = "store_stk_ttl" if scope == "STORE" else "dc_stk_ttl"
    wheres, params = [], {}
    if st:
        wheres.append("t.stream=:st"); params["st"] = st
        # keep FA strictly FA-division, CONS strictly CO — even if a run mixed DIVs
        divs = STREAM_DIV[st]
        din = _in_clause(list(divs), "gdv", params)
        wheres.append(f"(t.div IS NULL OR UPPER(t.div) IN ({din}))")
    if sequence_id:
        wheres.append("t.sequence_id=:seq"); params["seq"] = sequence_id
    wc = ("WHERE " + " AND ".join(wheres)) if wheres else ""
    # SLOC-wise: long rows pivoted in Python to per-row {sloc: qty} + STK_TTL.
    with data_engine.connect() as c:
        rows = c.execute(text(
            f"SELECT t.stream, t.{loc_col} AS loc, t.sloc, t.seg, t.div, t.sub_div, t.maj_cat, "
            f"       t.ref_art, t.ref_auto, t.ref_art_desc, t.article_number, t.gen_art, t.clr, t.sz, "
            f"       t.{ttl_col} AS q, m.ST_NM AS site_name, m.RDC AS rdc, m.HUB AS hub, "
            f"       m.ST_STATUS AS st_status, m.OP_DT AS op_dt "
            f"FROM {tbl} t LEFT JOIN {MASTER} m ON m.ST_CD = t.{loc_col} {wc}"), params).mappings().all()
        # which snapshot the shown rows came from (the result tables hold only the latest
        # run per stream) — the stock date + when it was calculated
        s_and = " AND stream=:st" if st else ""
        date_col = "store_date" if scope == "STORE" else "msa_date"
        seq_rows = c.execute(text(
            f"SELECT stream, {date_col} AS snap_date, created_at FROM {SEQ_TBL} WHERE status='COMPLETED'{s_and} "
            f"AND sequence_id IN (SELECT MAX(sequence_id) FROM {SEQ_TBL} WHERE status='COMPLETED'{s_and} GROUP BY stream)"),
            {"st": st} if st else {}).mappings().all()
    slocs: set = set()
    items: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        if grain == "article":
            key = (r["stream"], r["loc"], r["maj_cat"], r["ref_art"], r["article_number"], r["sz"])
        else:
            key = (r["stream"], r["loc"], r["maj_cat"], r["ref_art"])
        it = items.get(key)
        if it is None:
            it = items[key] = {
                "stream": r["stream"], "loc": r["loc"], "maj_cat": r["maj_cat"], "ref_art": r["ref_art"],
                "ref_auto": bool(r.get("ref_auto")), "ref_art_desc": r.get("ref_art_desc"),
                "article_number": r.get("article_number") if grain == "article" else None,
                "gen_art": r.get("gen_art"), "clr": r.get("clr"), "sz": r["sz"] if grain == "article" else None,
                "seg": r.get("seg"), "div": r.get("div"), "sub_div": r.get("sub_div"),
                "site_name": r.get("site_name"), "rdc": r.get("rdc"), "hub": r.get("hub"),
                "st_status": r.get("st_status"),
                "op_dt": r["op_dt"].isoformat() if r.get("op_dt") else None,
                "slocs": {}, "stk_ttl": 0.0, "_am": {},
            }
        sl = str(r["sloc"] or "").strip()
        q = float(r["q"]) if r["q"] is not None else 0.0
        if sl:
            slocs.add(sl)
            it["slocs"][sl] = it["slocs"].get(sl, 0.0) + q
        it["stk_ttl"] += q
        art = r.get("article_number")
        if art:
            it["_am"][str(art)] = it["_am"].get(str(art), 0.0) + q   # per-article stock (for #Art hover)
    for it in items.values():
        am = it.pop("_am")
        it["articles"] = len(am)                     # how many actual articles clubbed
        # article breakdown for the #Art hover (ref grain) — article + its stock, biggest first
        it["arts"] = [{"article_number": a, "qty": round(v, 2)}
                      for a, v in sorted(am.items(), key=lambda x: -x[1])] if grain == "ref" else []
    ordered = sorted(items.values(), key=lambda x: x["stk_ttl"], reverse=True)
    total_qty = sum(x["stk_ttl"] for x in ordered)
    seqs = [{"stream": r["stream"],
             "calc_date": r["snap_date"].isoformat() if r["snap_date"] else None,
             "calculated_at": r["created_at"].isoformat() if r["created_at"] else None}
            for r in seq_rows]
    latest = max(seqs, key=lambda x: (x["calculated_at"] or ""), default=None) if seqs else None
    return {
        "stream": st_in or "ALL", "scope": scope, "grain": grain,
        "columns": sorted(slocs),                       # SLOC columns (hideable in UI)
        "row_count": len(ordered),
        "total_qty": total_qty,
        "calc_date": latest["calc_date"] if latest else None,
        "calculated_at": latest["calculated_at"] if latest else None,
        "sequences": seqs,
        "items": ordered[:int(limit)],
    }


def get_sequences(stream: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    ensure_tables()
    where = ""
    params: Dict[str, Any] = {}
    if stream:
        where = "WHERE stream=:st"
        params["st"] = _norm_stream(stream)
    with data_engine.connect() as c:
        rows = c.execute(text(
            f"SELECT TOP {int(limit)} sequence_id, stream, calc_date, slocs_store, slocs_msa, "
            f"store_rows, msa_rows, status, created_by, created_at "
            f"FROM {SEQ_TBL} {where} ORDER BY sequence_id DESC"), params).mappings().all()
    out = []
    for r in rows:
        out.append({
            "sequence_id": r["sequence_id"], "stream": r["stream"],
            "calc_date": r["calc_date"].isoformat() if r["calc_date"] else None,
            "store_slocs": json.loads(r["slocs_store"]) if r["slocs_store"] else [],
            "msa_slocs": json.loads(r["slocs_msa"]) if r["slocs_msa"] else [],
            "store_rows": r["store_rows"], "msa_rows": r["msa_rows"],
            "status": r["status"], "created_by": r["created_by"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })
    return out
