"""
FA & CONS — Allocation engine (Phase B.1: ref-art level).

Runs SEPARATELY from core ARS — its own tables, session-wise. For a chosen stream
(FA/CONS/ALL) and store scope (UPC/OLD/ALL), for every CENTRAL reference article that
has an MBQ for a listed store:

    required   = max(MBQ − current store stock, 0)
    alloc_qty  = min(required, remaining central pool), rounded to whole packs (PAK_SZ)

Stores are processed OLDER→NEWER (OP_DT priority) so older stores draw from a shared
per-RDC×ref central pool first. Engine considers CENTRAL source_type only.

Persisted: ARS_FACONS_ALLOC_SESSION / _REF / _ART (article split filled in B.2).
Reads MBQ + the persisted FA/CONS stock (store) & MSA (DC pool) + VW_MASTER_PRODUCT
(pack size) — all read-only. App-owned writes only.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import data_engine
from app.services import facons_store_list_service as store_svc

SESS = "ARS_FACONS_ALLOC_SESSION"
REF = "ARS_FACONS_ALLOC_REF"
ART = "ARS_FACONS_ALLOC_ART"
MBQ = "ARS_FACONS_MBQ"
STOCK = "ARS_FACONS_STOCK"
MSA = "ARS_FACONS_MSA"
MPROD = "VW_MASTER_PRODUCT"
VALID_STREAM = ("FA", "CONS")


def ensure_tables() -> None:
    ddl = [
        f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{SESS}')
        CREATE TABLE {SESS} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            stream NVARCHAR(10) NOT NULL,          -- FA | CONS | ALL
            store_scope NVARCHAR(10) NOT NULL,     -- UPC | OLD | ALL
            run_date DATE NULL,
            n_stores INT NOT NULL DEFAULT 0,
            n_ref INT NOT NULL DEFAULT 0,
            n_units FLOAT NOT NULL DEFAULT 0,
            status NVARCHAR(20) NOT NULL DEFAULT 'COMPLETE',
            created_by NVARCHAR(100) NULL,
            created_at DATETIME NOT NULL DEFAULT GETDATE()
        );""",
        f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{REF}')
        CREATE TABLE {REF} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            session_id INT NOT NULL,
            stream NVARCHAR(10) NOT NULL,
            st_cd NVARCHAR(20) NOT NULL,
            rdc NVARCHAR(20) NULL,
            ref_art NVARCHAR(30) NOT NULL,
            ref_art_desc NVARCHAR(200) NULL,
            maj_cat NVARCHAR(80) NULL,
            source_type NVARCHAR(10) NULL,
            priority INT NULL,
            mbq FLOAT NULL,
            store_stock FLOAT NULL,
            required FLOAT NULL,
            pack_sz INT NULL,
            alloc_qty FLOAT NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT GETDATE()
        );""",
        f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{ART}')
        CREATE TABLE {ART} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            session_id INT NOT NULL,
            ref_id INT NOT NULL,
            st_cd NVARCHAR(20) NOT NULL,
            ref_art NVARCHAR(30) NOT NULL,
            article_number NVARCHAR(30) NOT NULL,
            sz NVARCHAR(20) NULL,
            clr NVARCHAR(20) NULL,
            qty FLOAT NOT NULL DEFAULT 0,
            split_rule NVARCHAR(20) NULL,
            manual_override BIT NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT GETDATE()
        );""",
    ]
    idx = [
        f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_FACONS_ALLOC_REF_sess') CREATE INDEX IX_FACONS_ALLOC_REF_sess ON {REF}(session_id);",
        f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_FACONS_ALLOC_ART_sess') CREATE INDEX IX_FACONS_ALLOC_ART_sess ON {ART}(session_id, ref_id);",
    ]
    with data_engine.begin() as c:
        for s in ddl:
            c.execute(text(s))
        for s in idx:
            c.execute(text(s))


def _stream_list(stream: Optional[str]) -> List[str]:
    s = (stream or "ALL").strip().upper()
    return list(VALID_STREAM) if s in ("", "ALL") else ([s] if s in VALID_STREAM else [])


def _latest_map(conn, table: str, loc_col: str, qty_col: str, streams: List[str]) -> Dict[tuple, float]:
    """Sum the LATEST-sequence rows of a persisted stock table into
    {(stream, loc, ref_art): qty}."""
    sp = ", ".join(f"'{s}'" for s in streams)
    rows = conn.execute(text(f"""
        SELECT stream, {loc_col} AS loc, ref_art, SUM({qty_col}) AS q
        FROM {table}
        WHERE sequence_id IN (SELECT MAX(sequence_id) FROM {table} GROUP BY stream)
          AND stream IN ({sp})
        GROUP BY stream, {loc_col}, ref_art""")).fetchall()
    return {(str(a), str(b).strip(), str(c).strip()): float(d or 0) for a, b, c, d in rows}


def _pack_map(conn, ref_arts: List[str]) -> Dict[str, int]:
    keys = sorted({str(r).strip() for r in ref_arts if str(r).strip()})
    if not keys:
        return {}
    out: Dict[str, int] = {}
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        inlist = ", ".join(f":k{j}" for j in range(len(chunk)))
        params = {f"k{j}": k for j, k in enumerate(chunk)}
        for ref, pak in conn.execute(text(
                f"SELECT LTRIM(RTRIM(CAST(REF_ART AS NVARCHAR(50)))) ref, MAX(PAK_SZ) pak "
                f"FROM {MPROD} WHERE REF_ART IN ({inlist}) GROUP BY LTRIM(RTRIM(CAST(REF_ART AS NVARCHAR(50))))"),
                params).fetchall():
            try:
                p = int(float(pak or 0))
            except Exception:
                p = 0
            out[str(ref).strip()] = p if p > 0 else 1
    return out


def _round_packs(required: float, remaining: float, pack: int) -> float:
    """Whole-pack quantity: round required to the nearest pack (min 1 pack when any is
    required), capped by whole packs available in the pool."""
    pack = pack if pack and pack > 0 else 1
    if required <= 0 or remaining < pack:
        return 0.0
    need_packs = int(round(required / pack)) or 1
    avail_packs = int(remaining // pack)
    return float(min(need_packs, avail_packs) * pack)


STREAM_DIV = {"FA": ("FA",), "CONS": ("CO",)}


def _active_msa_slocs(conn, streams: List[str]) -> List[str]:
    """SLOCs switched on for FA/CONS on the MSA (warehouse) SLOC settings."""
    cols = []
    if "FA" in streams:
        cols.append("fa_active=1")
    if "CONS" in streams:
        cols.append("co_active=1")
    if not cols:
        return []
    try:
        rows = conn.execute(text(
            f"SELECT sloc FROM ARS_MSA_SLOC_SETTINGS WHERE {' OR '.join(cols)}")).fetchall()
        return [str(r[0]).strip() for r in rows if r[0]]
    except Exception as e:
        logger.warning(f"[facons-alloc] active MSA slocs read skipped: {e}")
        return []


def _member_articles(conn, refs: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """{ref_art: [{article, sz, clr, pack}]} from VW_MASTER_PRODUCT."""
    keys = sorted({str(r).strip() for r in refs if str(r).strip()})
    out: Dict[str, List[Dict[str, Any]]] = {}
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        inlist = ", ".join(f":k{j}" for j in range(len(chunk)))
        params = {f"k{j}": k for j, k in enumerate(chunk)}
        rows = conn.execute(text(f"""
            SELECT LTRIM(RTRIM(CAST(REF_ART AS NVARCHAR(50)))) ref, ARTICLE_NUMBER art,
                   MAX(SZ) sz, MAX(CLR) clr, MAX(PAK_SZ) pak
            FROM {MPROD} WHERE REF_ART IN ({inlist}) AND ARTICLE_NUMBER IS NOT NULL
            GROUP BY LTRIM(RTRIM(CAST(REF_ART AS NVARCHAR(50)))), ARTICLE_NUMBER"""), params).mappings().all()
        for r in rows:
            try:
                pak = int(float(r["pak"] or 0)) or 1
            except Exception:
                pak = 1
            out.setdefault(str(r["ref"]).strip(), []).append(
                {"article": str(r["art"]).strip(), "sz": r["sz"], "clr": r["clr"], "pack": pak})
    return out


def _article_stock(conn, articles: List[str], streams: List[str], slocs: List[str]) -> Dict[tuple, float]:
    """{(rdc, article): warehouse stock} from the MSA view, active SLOCs, stream DIVs."""
    arts = sorted({str(a).strip() for a in articles if str(a).strip()})
    if not arts or not slocs:
        return {}
    divs = tuple(d for s in streams for d in STREAM_DIV.get(s, ()))
    if not divs:
        return {}
    out: Dict[tuple, float] = {}
    for i in range(0, len(arts), 400):
        ac = arts[i:i + 400]
        ain = ", ".join(f":a{j}" for j in range(len(ac)))
        sin = ", ".join(f":s{j}" for j in range(len(slocs)))
        din = ", ".join(f":d{j}" for j in range(len(divs)))
        params = {f"a{j}": a for j, a in enumerate(ac)}
        params.update({f"s{j}": s for j, s in enumerate(slocs)})
        params.update({f"d{j}": d for j, d in enumerate(divs)})
        rows = conn.execute(text(f"""
            SELECT ST_CD rdc, ARTICLE_NUMBER art, SUM(STK_Q) q
            FROM VW_ET_MSA_STK_WITH_MASTER
            WHERE ARTICLE_NUMBER IN ({ain}) AND SLOC IN ({sin}) AND UPPER(DIV) IN ({din})
            GROUP BY ST_CD, ARTICLE_NUMBER"""), params).fetchall()
        for rdc, art, q in rows:
            out[(str(rdc).strip(), str(art).strip())] = float(q or 0)
    return out


def allocate(stream: Optional[str] = "ALL", store_scope: str = "UPC",
             run_date: Optional[str] = None, user: Optional[str] = None) -> Dict[str, Any]:
    ensure_tables()
    store_svc.ensure_tables()
    streams = _stream_list(stream)
    if not streams:
        raise ValueError(f"stream must be FA / CONS / ALL, got {stream!r}")
    scope = (store_scope or "UPC").strip().upper()
    if scope not in ("UPC", "OLD", "ALL"):
        raise ValueError("store_scope must be UPC / OLD / ALL")

    # stores in scope, older→newer, with an RDC (needed for the central pool)
    stores = [s for s in store_svc.list_stores()
              if s.get("rdc") and (scope == "ALL" and s["status"] in ("UPC", "OLD") or s["status"] == scope)]
    store_rdc = {s["st_cd"]: str(s["rdc"]).strip() for s in stores}
    store_prio = {s["st_cd"]: s["priority"] for s in stores}
    scoped = set(store_rdc)
    if not scoped:
        raise ValueError(f"No stores in scope '{scope}'. Add stores to the UPC Store List first.")

    with data_engine.connect() as c:
        mbq_rows = c.execute(text(f"""
            SELECT stream, st_cd, ref_art, mbq_q, maj_cat, ref_art_desc
            FROM {MBQ} WHERE UPPER(ISNULL(source_type,'CENTRAL'))='CENTRAL' AND stream IN ({", ".join("'" + s + "'" for s in streams)})""")).mappings().all()
        store_stock = _latest_map(c, STOCK, "st_cd", "store_stk_ttl", streams)
        pool = _latest_map(c, MSA, "rdc", "dc_stk_ttl", streams)
        packs = _pack_map(c, [r["ref_art"] for r in mbq_rows])

    # only stores in scope; order by priority (older first)
    work = [r for r in mbq_rows if r["st_cd"] in scoped]
    work.sort(key=lambda r: (store_prio.get(r["st_cd"], 1e9), str(r["ref_art"])))

    out_rows: List[Dict[str, Any]] = []
    for r in work:
        st, ref, strm = r["st_cd"], str(r["ref_art"]).strip(), r["stream"]
        rdc = store_rdc.get(st, "")
        mbq = float(r["mbq_q"] or 0)
        stk = store_stock.get((strm, st, ref), 0.0)
        required = max(mbq - stk, 0.0)
        pack = packs.get(ref, 1)
        pkey = (strm, rdc, ref)
        remaining = pool.get(pkey, 0.0)
        qty = _round_packs(required, remaining, pack)
        if qty > 0:
            pool[pkey] = remaining - qty
        out_rows.append({"stream": strm, "st_cd": st, "rdc": rdc, "ref_art": ref,
                         "ref_art_desc": r["ref_art_desc"], "maj_cat": r["maj_cat"],
                         "priority": store_prio.get(st), "mbq": mbq, "store_stock": stk,
                         "required": required, "pack_sz": pack, "alloc_qty": qty})

    shipped = [r for r in out_rows if r["alloc_qty"] > 0]       # already in priority order
    n_units = sum(r["alloc_qty"] for r in shipped)

    # B.2 — ref → actual article split (max-qty-first on warehouse stock), drawing down a
    # shared per-(rdc, article) pool across stores in the same priority order.
    with data_engine.connect() as c:
        members = _member_articles(c, [r["ref_art"] for r in shipped])
        art_slocs = _active_msa_slocs(c, streams)
        all_arts = [m["article"] for lst in members.values() for m in lst]
        artstk = _article_stock(c, all_arts, streams, art_slocs)
    used: Dict[tuple, float] = {}
    n_art_units = 0.0

    with data_engine.begin() as c:
        sid = c.execute(text(f"""
            INSERT INTO {SESS} (stream, store_scope, run_date, n_stores, n_ref, n_units, created_by)
            OUTPUT INSERTED.id
            VALUES (:s,:sc,:d,:ns,:nr,:nu,:u)"""),
            {"s": (stream or "ALL").upper(), "sc": scope, "d": run_date or date.today().isoformat(),
             "ns": len({r["st_cd"] for r in shipped}), "nr": len(shipped), "nu": n_units, "u": user}).scalar()
        for r in shipped:
            ref_id = c.execute(text(f"""
                INSERT INTO {REF} (session_id, stream, st_cd, rdc, ref_art, ref_art_desc, maj_cat,
                                   source_type, priority, mbq, store_stock, required, pack_sz, alloc_qty)
                OUTPUT INSERTED.id
                VALUES (:sid,:st,:sc,:rdc,:ra,:desc,:mc,'CENTRAL',:pr,:mbq,:stk,:req,:pk,:q)"""),
                {"sid": sid, "st": r["stream"], "sc": r["st_cd"], "rdc": r["rdc"], "ra": r["ref_art"],
                 "desc": r["ref_art_desc"], "mc": r["maj_cat"], "pr": r["priority"], "mbq": r["mbq"],
                 "stk": r["store_stock"], "req": r["required"], "pk": r["pack_sz"], "q": r["alloc_qty"]}).scalar()
            # split alloc_qty across member articles, most-warehouse-stock first
            rdc, rem = r["rdc"], r["alloc_qty"]
            avail = sorted(
                ({"art": m["article"], "sz": m["sz"], "clr": m["clr"],
                  "av": max(artstk.get((rdc, m["article"]), 0.0) - used.get((rdc, m["article"]), 0.0), 0.0)}
                 for m in members.get(r["ref_art"], [])),
                key=lambda x: -x["av"])
            for a in avail:
                if rem <= 0:
                    break
                take = min(rem, a["av"])
                if take <= 0:
                    continue
                c.execute(text(f"""
                    INSERT INTO {ART} (session_id, ref_id, st_cd, ref_art, article_number, sz, clr, qty, split_rule)
                    VALUES (:sid,:rid,:st,:ra,:art,:sz,:clr,:q,'MAX_QTY')"""),
                    {"sid": sid, "rid": ref_id, "st": r["st_cd"], "ra": r["ref_art"], "art": a["art"],
                     "sz": a["sz"], "clr": a["clr"], "q": take})
                used[(rdc, a["art"])] = used.get((rdc, a["art"]), 0.0) + take
                n_art_units += take
                rem -= take
    logger.info(f"[facons-alloc] session {sid}: {len(shipped)} ref rows, {n_units} units, {n_art_units} article units")
    return {"session_id": int(sid), "stream": (stream or "ALL").upper(), "store_scope": scope,
            "stores": len({r["st_cd"] for r in shipped}), "ref_rows": len(shipped),
            "considered": len(out_rows), "units": n_units, "article_units": n_art_units,
            "unsplit": round(n_units - n_art_units, 3)}


def list_sessions(limit: int = 50) -> List[Dict[str, Any]]:
    ensure_tables()
    with data_engine.connect() as c:
        rows = c.execute(text(
            f"SELECT TOP {int(limit)} id, stream, store_scope, run_date, n_stores, n_ref, n_units, "
            f"status, created_by, created_at FROM {SESS} ORDER BY created_at DESC, id DESC")).mappings().all()
    return [{"id": r["id"], "stream": r["stream"], "store_scope": r["store_scope"],
             "run_date": r["run_date"].isoformat() if r["run_date"] else None,
             "n_stores": r["n_stores"], "n_ref": r["n_ref"], "n_units": r["n_units"],
             "status": r["status"], "created_by": r["created_by"],
             "created_at": r["created_at"].isoformat() if r["created_at"] else None} for r in rows]


def get_ref_rows(session_id: int, limit: int = 5000) -> List[Dict[str, Any]]:
    ensure_tables()
    with data_engine.connect() as c:
        rows = c.execute(text(
            f"SELECT r.id, r.stream, r.st_cd, r.rdc, r.ref_art, r.ref_art_desc, r.maj_cat, r.priority, "
            f"r.mbq, r.store_stock, r.required, r.pack_sz, r.alloc_qty, "
            f"(SELECT COUNT(*) FROM {ART} a WHERE a.ref_id=r.id) AS n_articles, "
            f"(SELECT MAX(CAST(manual_override AS INT)) FROM {ART} a WHERE a.ref_id=r.id) AS manual "
            f"FROM {REF} r WHERE r.session_id=:s ORDER BY r.priority, r.st_cd, r.ref_art"),
            {"s": session_id}).mappings().all()
    return [{**dict(r), "manual": bool(r["manual"])} for r in rows]


def article_suggestions(session_id: int, ref_id: int) -> Dict[str, Any]:
    """For the manual-override modal: the ref line + its member articles with warehouse
    stock and the currently-allocated qty (auto split or a prior override)."""
    ensure_tables()
    with data_engine.connect() as c:
        ref = c.execute(text(
            f"SELECT id, stream, st_cd, rdc, ref_art, ref_art_desc, alloc_qty FROM {REF} "
            f"WHERE id=:i AND session_id=:s"), {"i": ref_id, "s": session_id}).mappings().first()
        if not ref:
            raise ValueError("ref line not found")
        members = _member_articles(c, [ref["ref_art"]]).get(str(ref["ref_art"]).strip(), [])
        slocs = _active_msa_slocs(c, [ref["stream"]])
        artstk = _article_stock(c, [m["article"] for m in members], [ref["stream"]], slocs)
        cur = {str(a).strip(): float(q or 0) for a, q in c.execute(text(
            f"SELECT article_number, qty FROM {ART} WHERE session_id=:s AND ref_id=:i"),
            {"s": session_id, "i": ref_id}).fetchall()}
    rdc = str(ref["rdc"] or "").strip()
    items = [{"article_number": m["article"], "sz": m["sz"], "clr": m["clr"], "pack": m["pack"],
              "avail_stock": artstk.get((rdc, m["article"]), 0.0), "qty": cur.get(m["article"], 0.0)}
             for m in members]
    items.sort(key=lambda x: -x["avail_stock"])
    return {"ref": {"id": ref["id"], "st_cd": ref["st_cd"], "rdc": rdc, "ref_art": ref["ref_art"],
                    "ref_art_desc": ref["ref_art_desc"], "alloc_qty": float(ref["alloc_qty"] or 0)},
            "items": items}


def save_articles(session_id: int, ref_id: int, items: List[Dict[str, Any]], user: Optional[str] = None) -> Dict[str, Any]:
    """Manual override: replace the article split for one ref line."""
    ensure_tables()
    with data_engine.begin() as c:
        ref = c.execute(text(f"SELECT st_cd, ref_art FROM {REF} WHERE id=:i AND session_id=:s"),
                        {"i": ref_id, "s": session_id}).mappings().first()
        if not ref:
            raise ValueError("ref line not found")
        c.execute(text(f"DELETE FROM {ART} WHERE session_id=:s AND ref_id=:i"), {"s": session_id, "i": ref_id})
        total = 0.0
        for it in items:
            q = float(it.get("qty") or 0)
            if q <= 0:
                continue
            c.execute(text(f"""
                INSERT INTO {ART} (session_id, ref_id, st_cd, ref_art, article_number, sz, clr, qty, split_rule, manual_override)
                VALUES (:sid,:rid,:st,:ra,:art,:sz,:clr,:q,'MANUAL',1)"""),
                {"sid": session_id, "rid": ref_id, "st": ref["st_cd"], "ra": ref["ref_art"],
                 "art": str(it.get("article_number") or "").strip(), "sz": it.get("sz"), "clr": it.get("clr"), "q": q})
            total += q
    return {"ref_id": ref_id, "articles": sum(1 for it in items if float(it.get("qty") or 0) > 0), "total": total}
