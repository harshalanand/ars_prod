"""
FA & CONS — Pending Allocation (Phase C).

A SELF-CONTAINED pending pipeline for FA/CONS — its OWN tables, session-wise. The core
ARS Pending Allocation menu / tables / logic are NOT touched.

Lifecycle (plain terms): an allocation is APPROVED → it becomes pending lines (article
grain) → deliveries (DO) reduce the balance → the line CLOSES when fully delivered (or is
closed by hand). Manual entry adds a line directly. Every action is written to an
operations log stamped with stream + session + user + time.

Persisted: ARS_FACONS_PEND / ARS_FACONS_PEND_OPS. App-owned writes only.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import data_engine

PEND = "ARS_FACONS_PEND"
OPS = "ARS_FACONS_PEND_OPS"
ART = "ARS_FACONS_ALLOC_ART"
REF = "ARS_FACONS_ALLOC_REF"
VALID_STREAM = ("FA", "CONS")


def _clean(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def ensure_tables() -> None:
    ddl = [
        f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{PEND}')
        CREATE TABLE {PEND} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            source NVARCHAR(10) NOT NULL DEFAULT 'alloc',   -- alloc | manual
            alloc_session_id INT NULL,
            stream NVARCHAR(10) NOT NULL,
            st_cd NVARCHAR(20) NOT NULL,
            rdc NVARCHAR(20) NULL,
            ref_art NVARCHAR(30) NOT NULL,
            article_number NVARCHAR(30) NULL,
            sz NVARCHAR(20) NULL,
            clr NVARCHAR(20) NULL,
            qty FLOAT NOT NULL DEFAULT 0,
            delivered FLOAT NOT NULL DEFAULT 0,
            status NVARCHAR(12) NOT NULL DEFAULT 'OPEN',     -- OPEN | CLOSED
            reason NVARCHAR(500) NULL,
            created_by NVARCHAR(100) NULL,
            created_at DATETIME NOT NULL DEFAULT GETDATE(),
            updated_at DATETIME NOT NULL DEFAULT GETDATE(),
            CONSTRAINT CK_FACONS_PEND_stream CHECK (stream IN ('FA','CONS'))
        );""",
        f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{OPS}')
        CREATE TABLE {OPS} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            pend_id INT NULL,
            alloc_session_id INT NULL,
            stream NVARCHAR(10) NULL,
            st_cd NVARCHAR(20) NULL,
            ref_art NVARCHAR(30) NULL,
            article_number NVARCHAR(30) NULL,
            action NVARCHAR(16) NOT NULL,    -- APPROVE | MANUAL_ADD | DELIVER | CLOSE | REOPEN
            qty FLOAT NULL,
            remarks NVARCHAR(500) NULL,
            changed_by NVARCHAR(100) NULL,
            changed_at DATETIME NOT NULL DEFAULT GETDATE()
        );""",
    ]
    idx = [
        f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_FACONS_PEND_key') CREATE INDEX IX_FACONS_PEND_key ON {PEND}(stream, status, st_cd);",
        f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_FACONS_PEND_OPS_key') CREATE INDEX IX_FACONS_PEND_OPS_key ON {OPS}(changed_at DESC);",
    ]
    with data_engine.begin() as c:
        for s in ddl:
            c.execute(text(s))
        for s in idx:
            c.execute(text(s))
        # DO (Delivery Order) lifecycle — mirrors core pend-alc's DO_NUMBER + DO entry.
        for col, typ in (("do_number", "NVARCHAR(100) NULL"), ("do_generated", "BIT NOT NULL DEFAULT 0"),
                         ("do_generated_at", "DATETIME NULL"), ("do_generated_by", "NVARCHAR(100) NULL")):
            c.execute(text(f"IF COL_LENGTH('{PEND}','{col}') IS NULL ALTER TABLE {PEND} ADD {col} {typ};"))


def _norm_stream(s: str) -> str:
    v = (s or "").strip().upper()
    if v not in VALID_STREAM:
        raise ValueError(f"stream must be FA / CONS, got {s!r}")
    return v


def _log(conn, action, *, pend_id=None, session_id=None, stream=None, st_cd=None,
         ref_art=None, article=None, qty=None, remarks=None, user=None) -> None:
    conn.execute(text(f"""
        INSERT INTO {OPS} (pend_id, alloc_session_id, stream, st_cd, ref_art, article_number, action, qty, remarks, changed_by)
        VALUES (:p,:s,:st,:sc,:ra,:art,:ac,:q,:r,:u)"""),
        {"p": pend_id, "s": session_id, "st": stream, "sc": st_cd, "ra": ref_art,
         "art": article, "ac": action, "q": qty, "r": remarks, "u": user})


def approve_session(alloc_session_id: int, user: Optional[str] = None) -> Dict[str, Any]:
    """Turn an allocation session's ARTICLE lines into pending lines (idempotent per
    session). Logs one APPROVE op."""
    ensure_tables()
    with data_engine.begin() as c:
        done = c.execute(text(f"SELECT COUNT(*) FROM {PEND} WHERE source='alloc' AND alloc_session_id=:s"),
                         {"s": alloc_session_id}).scalar()
        if done and int(done) > 0:
            raise ValueError(f"Allocation session {alloc_session_id} is already approved into pending.")
        rows = c.execute(text(f"""
            SELECT a.st_cd, r.rdc, a.ref_art, a.article_number, a.sz, a.clr, a.qty, a.session_id, r.stream
            FROM {ART} a JOIN {REF} r ON r.id = a.ref_id
            WHERE a.session_id = :s AND a.qty > 0"""), {"s": alloc_session_id}).mappings().all()
        if not rows:
            raise ValueError(f"Allocation session {alloc_session_id} has no article lines to approve.")
        total = 0.0
        for r in rows:
            c.execute(text(f"""
                INSERT INTO {PEND} (source, alloc_session_id, stream, st_cd, rdc, ref_art, article_number, sz, clr, qty, created_by)
                VALUES ('alloc',:s,:st,:sc,:rdc,:ra,:art,:sz,:clr,:q,:u)"""),
                {"s": alloc_session_id, "st": r["stream"], "sc": r["st_cd"], "rdc": r["rdc"],
                 "ra": r["ref_art"], "art": r["article_number"], "sz": r["sz"], "clr": r["clr"],
                 "q": float(r["qty"] or 0), "u": user})
            total += float(r["qty"] or 0)
        _log(c, "APPROVE", session_id=alloc_session_id, qty=total, user=user,
             remarks=f"approved {len(rows)} line(s) from alloc session {alloc_session_id}")
    return {"alloc_session_id": alloc_session_id, "lines": len(rows), "units": total}


def add_manual(stream: str, st_cd: str, ref_art: str, qty: Any, article_number: Optional[str] = None,
               sz: Optional[str] = None, clr: Optional[str] = None, rdc: Optional[str] = None,
               reason: Optional[str] = None, user: Optional[str] = None) -> Dict[str, Any]:
    """Add a pending line by hand (like the old manual entry). Reason is mandatory."""
    ensure_tables()
    strm = _norm_stream(stream)
    st = (st_cd or "").strip().upper()
    ra = (ref_art or "").strip()
    rsn = _clean(reason)
    try:
        q = float(qty)
    except Exception:
        raise ValueError("qty must be numeric")
    if not st or not ra:
        raise ValueError("st_cd and ref_art are required")
    if q <= 0:
        raise ValueError("qty must be greater than 0")
    if not rsn:
        raise ValueError("Reason is mandatory for a manual pending entry")
    art = _clean(article_number) or ra
    with data_engine.begin() as c:
        pid = c.execute(text(f"""
            INSERT INTO {PEND} (source, stream, st_cd, rdc, ref_art, article_number, sz, clr, qty, reason, created_by)
            OUTPUT INSERTED.id
            VALUES ('manual',:st,:sc,:rdc,:ra,:art,:sz,:clr,:q,:rsn,:u)"""),
            {"st": strm, "sc": st, "rdc": _clean(rdc), "ra": ra, "art": art,
             "sz": _clean(sz), "clr": _clean(clr), "q": q, "rsn": rsn, "u": user}).scalar()
        _log(c, "MANUAL_ADD", pend_id=pid, stream=strm, st_cd=st, ref_art=ra, article=art,
             qty=q, remarks=rsn, user=user)
    return {"id": int(pid), "st_cd": st, "ref_art": ra, "qty": q}


def deliver(pend_id: int, qty: Any, remarks: Optional[str] = None, user: Optional[str] = None) -> Dict[str, Any]:
    """Record a delivery (DO) against a pending line; auto-CLOSE when fully delivered."""
    ensure_tables()
    try:
        q = float(qty)
    except Exception:
        raise ValueError("qty must be numeric")
    if q <= 0:
        raise ValueError("delivery qty must be greater than 0")
    with data_engine.begin() as c:
        row = c.execute(text(f"SELECT stream, st_cd, ref_art, article_number, qty, delivered, status FROM {PEND} WHERE id=:i"),
                        {"i": pend_id}).mappings().first()
        if not row:
            raise ValueError("pending line not found")
        if row["status"] == "CLOSED":
            raise ValueError("line is already closed")
        new_del = float(row["delivered"] or 0) + q
        status = "CLOSED" if new_del >= float(row["qty"] or 0) - 1e-9 else "OPEN"
        c.execute(text(f"UPDATE {PEND} SET delivered=:d, status=:s, updated_at=GETDATE() WHERE id=:i"),
                  {"d": new_del, "s": status, "i": pend_id})
        _log(c, "DELIVER", pend_id=pend_id, stream=row["stream"], st_cd=row["st_cd"], ref_art=row["ref_art"],
             article=row["article_number"], qty=q, remarks=_clean(remarks), user=user)
        if status == "CLOSED":
            _log(c, "CLOSE", pend_id=pend_id, stream=row["stream"], st_cd=row["st_cd"], ref_art=row["ref_art"],
                 article=row["article_number"], remarks="fully delivered", user=user)
    return {"id": pend_id, "delivered": new_del, "status": status}


def close(pend_id: int, reason: Optional[str] = None, user: Optional[str] = None) -> Dict[str, Any]:
    """Adhoc-close a pending line (balance won't be fulfilled). Reason mandatory."""
    ensure_tables()
    rsn = _clean(reason)
    if not rsn:
        raise ValueError("Reason is mandatory to close a pending line")
    with data_engine.begin() as c:
        row = c.execute(text(f"SELECT stream, st_cd, ref_art, article_number, status FROM {PEND} WHERE id=:i"),
                        {"i": pend_id}).mappings().first()
        if not row:
            raise ValueError("pending line not found")
        c.execute(text(f"UPDATE {PEND} SET status='CLOSED', reason=:r, updated_at=GETDATE() WHERE id=:i"),
                  {"r": rsn, "i": pend_id})
        _log(c, "CLOSE", pend_id=pend_id, stream=row["stream"], st_cd=row["st_cd"], ref_art=row["ref_art"],
             article=row["article_number"], remarks=rsn, user=user)
    return {"id": pend_id, "status": "CLOSED"}


def reopen(pend_id: int, user: Optional[str] = None) -> Dict[str, Any]:
    ensure_tables()
    with data_engine.begin() as c:
        row = c.execute(text(f"SELECT stream, st_cd, ref_art, article_number, qty, delivered FROM {PEND} WHERE id=:i"),
                        {"i": pend_id}).mappings().first()
        if not row:
            raise ValueError("pending line not found")
        st = "CLOSED" if float(row["delivered"] or 0) >= float(row["qty"] or 0) - 1e-9 else "OPEN"
        c.execute(text(f"UPDATE {PEND} SET status=:s, updated_at=GETDATE() WHERE id=:i"), {"s": st, "i": pend_id})
        _log(c, "REOPEN", pend_id=pend_id, stream=row["stream"], st_cd=row["st_cd"], ref_art=row["ref_art"],
             article=row["article_number"], user=user)
    return {"id": pend_id, "status": st}


def generate_do(pend_ids: List[int], do_number: Optional[str] = None,
                user: Optional[str] = None) -> Dict[str, Any]:
    """DO Entry: stamp a Delivery Order number on one or more OPEN pending lines and flag
    them DO-generated. `do_number` is used verbatim if given; if blank, a running
    'FACONS-DO-000n' placeholder is auto-assigned to the whole batch. One DO can cover
    many lines. Logs a GENERATE_DO op per line."""
    ensure_tables()
    ids = [int(i) for i in (pend_ids or []) if str(i).strip()]
    if not ids:
        raise ValueError("Select at least one pending line for the DO")
    dn = _clean(do_number)
    id_list = ",".join(str(i) for i in ids)            # ints only → injection-safe
    with data_engine.begin() as c:
        rows = c.execute(text(
            f"SELECT id, stream, st_cd, ref_art, article_number, status FROM {PEND} WHERE id IN ({id_list})")).mappings().all()
        openrows = [r for r in rows if r["status"] == "OPEN"]
        if not openrows:
            raise ValueError("None of the selected lines are OPEN — nothing to generate a DO for")
        if not dn:                                     # auto: next FACONS-DO-000n
            mx = c.execute(text(
                f"SELECT MAX(TRY_CAST(REPLACE(do_number,'FACONS-DO-','') AS INT)) "
                f"FROM {PEND} WHERE do_number LIKE 'FACONS-DO-%'")).scalar()
            dn = f"FACONS-DO-{(int(mx or 0) + 1):04d}"
        for r in openrows:
            c.execute(text(
                f"UPDATE {PEND} SET do_number=:dn, do_generated=1, do_generated_at=GETDATE(), "
                f"do_generated_by=:u, updated_at=GETDATE() WHERE id=:i"),
                {"dn": dn, "u": user, "i": r["id"]})
            _log(c, "GENERATE_DO", pend_id=r["id"], stream=r["stream"], st_cd=r["st_cd"],
                 ref_art=r["ref_art"], article=r["article_number"], remarks=f"DO {dn}", user=user)
    return {"do_number": dn, "lines": len(openrows)}


def update_line(pend_id: int, article_number: Optional[str] = None, sz: Optional[str] = None,
                clr: Optional[str] = None, qty: Any = None, reason: Optional[str] = None,
                user: Optional[str] = None) -> Dict[str, Any]:
    """Edit an OPEN pending line at the ACTUAL-article grain — set/correct the actual
    article, size, colour, or promised qty. qty may not drop below what's already
    delivered. Reason mandatory; logged as UPDATE."""
    ensure_tables()
    rsn = _clean(reason)
    if not rsn:
        raise ValueError("Reason is mandatory to update a pending line")
    with data_engine.begin() as c:
        row = c.execute(text(
            f"SELECT stream, st_cd, ref_art, article_number, sz, clr, qty, delivered, status "
            f"FROM {PEND} WHERE id=:i"), {"i": pend_id}).mappings().first()
        if not row:
            raise ValueError("pending line not found")
        if row["status"] == "CLOSED":
            raise ValueError("cannot edit a closed line — reopen it first")
        if qty is None or str(qty).strip() == "":
            new_q = float(row["qty"] or 0)
        else:
            try:
                new_q = float(qty)
            except Exception:
                raise ValueError("qty must be numeric")
        if new_q < float(row["delivered"] or 0) - 1e-9:
            raise ValueError(f"qty ({new_q:g}) cannot be less than already delivered ({row['delivered']:g})")
        if new_q <= 0:
            raise ValueError("qty must be greater than 0")
        art = _clean(article_number) or row["article_number"]
        new_sz = _clean(sz) if sz is not None else row["sz"]
        new_clr = _clean(clr) if clr is not None else row["clr"]
        c.execute(text(
            f"UPDATE {PEND} SET article_number=:art, sz=:sz, clr=:clr, qty=:q, updated_at=GETDATE() WHERE id=:i"),
            {"art": art, "sz": new_sz, "clr": new_clr, "q": new_q, "i": pend_id})
        _log(c, "UPDATE", pend_id=pend_id, stream=row["stream"], st_cd=row["st_cd"], ref_art=row["ref_art"],
             article=art, qty=new_q, remarks=rsn, user=user)
    return {"id": pend_id, "article_number": art, "qty": new_q}


def list_pend(stream: Optional[str] = None, status: Optional[str] = None,
              alloc_session_id: Optional[int] = None, limit: int = 5000) -> List[Dict[str, Any]]:
    ensure_tables()
    where, params = [], {}
    if stream:
        where.append("stream=:st"); params["st"] = _norm_stream(stream)
    if status:
        where.append("status=:s"); params["s"] = status.strip().upper()
    if alloc_session_id:
        where.append("alloc_session_id=:a"); params["a"] = alloc_session_id
    wc = ("WHERE " + " AND ".join(where)) if where else ""
    with data_engine.connect() as c:
        rows = c.execute(text(
            f"SELECT TOP {int(limit)} id, source, alloc_session_id, stream, st_cd, rdc, ref_art, article_number, "
            f"sz, clr, qty, delivered, status, do_number, do_generated, reason, created_by, created_at FROM {PEND} {wc} "
            f"ORDER BY status ASC, created_at DESC, id DESC"), params).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["balance"] = max(float(r["qty"] or 0) - float(r["delivered"] or 0), 0.0)
        d["do_generated"] = bool(r["do_generated"])
        d["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
        out.append(d)
    return out


def summary(stream: Optional[str] = None) -> Dict[str, Any]:
    ensure_tables()
    where, params = ("WHERE stream=:st", {"st": _norm_stream(stream)}) if stream else ("", {})
    with data_engine.connect() as c:
        r = c.execute(text(f"""
            SELECT COUNT(*) lines,
                   SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END) open_lines,
                   SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END) closed_lines,
                   ISNULL(SUM(qty),0) promised, ISNULL(SUM(delivered),0) delivered,
                   ISNULL(SUM(CASE WHEN status='OPEN' THEN qty-delivered ELSE 0 END),0) balance
            FROM {PEND} {where}"""), params).mappings().first()
    return {"lines": r["lines"] or 0, "open": r["open_lines"] or 0, "closed": r["closed_lines"] or 0,
            "promised": float(r["promised"] or 0), "delivered": float(r["delivered"] or 0),
            "balance": float(r["balance"] or 0)}


def list_ops(stream: Optional[str] = None, action: Optional[str] = None,
             st_cd: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
    ensure_tables()
    where, params = [], {}
    if stream:
        where.append("stream=:st"); params["st"] = _norm_stream(stream)
    if action:
        where.append("action=:a"); params["a"] = action.strip().upper()
    if st_cd:
        where.append("st_cd=:sc"); params["sc"] = st_cd.strip().upper()
    wc = ("WHERE " + " AND ".join(where)) if where else ""
    with data_engine.connect() as c:
        rows = c.execute(text(
            f"SELECT TOP {int(limit)} id, pend_id, alloc_session_id, stream, st_cd, ref_art, article_number, "
            f"action, qty, remarks, changed_by, changed_at FROM {OPS} {wc} "
            f"ORDER BY changed_at DESC, id DESC"), params).mappings().all()
    return [{**dict(r), "changed_at": r["changed_at"].isoformat() if r["changed_at"] else None} for r in rows]
