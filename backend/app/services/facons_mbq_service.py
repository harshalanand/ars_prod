"""
FA & CONS MBQ Master service.

Upload is intentionally minimal — only THREE columns: ST_CD, REF_ART, MBQ_Q.
Everything else (store name / RDC / hub / opening date / status) is joined LIVE
at read time from Master_ALC_INPUT_ST_MASTER — never copied — exactly like the
UPC Store Tracking module (services/upc_store_track_service.py).

Persisted: ARS_FACONS_MBQ, grain (stream, st_cd, ref_art, clr). Only st_cd /
ref_art / mbq_q come from the upload; maj_cat / opt_type / clr are resolved
later at allocation time.
"""
from __future__ import annotations

import io
from collections import defaultdict
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger
from sqlalchemy import text

from app.database.session import data_engine

MBQ = "ARS_FACONS_MBQ"
HIST = "ARS_FACONS_MBQ_HIST"
SESS = "ARS_FACONS_MBQ_SESSION"          # one row per upload batch (session), for session-wise change review
MASTER = "Master_ALC_INPUT_ST_MASTER"
MPROD = "VW_MASTER_PRODUCT"
STOCK_TBL = "ARS_FACONS_STOCK"          # persisted store-stock calc result (read fast)
MSA_TBL = "ARS_FACONS_MSA"              # persisted MSA/DC-pool calc result (read fast)
VALID_STREAM = ("FA", "CONS")


def _norm_stream(stream: str) -> str:
    s = (stream or "").strip().upper()
    if s not in VALID_STREAM:
        raise ValueError(f"stream must be one of {VALID_STREAM}, got {stream!r}")
    return s


# ── Stream auto-segregation by DIV (single MBQ upload, no manual FA/CONS choice) ─
#   DIV='CO'  → CONS  (consumables)
#   DIV='FA'  → FA    (fixed assets / project store)
#   anything else / unknown → FA (fixed-asset default)
def _stream_from_div(div: Any) -> str:
    return "CONS" if str(div or "").strip().upper() == "CO" else "FA"


def _art_attrs_map(conn, ref_arts: List[str]) -> Dict[str, Dict[str, Any]]:
    """{ref_art: {div, sub_div, maj_cat, sz, desc}} from VW_MASTER_PRODUCT, keyed on
    REF_ART (UNION GEN_ART fallback), restricted to the given ref-arts. ONE scan —
    used at upload/save time to denormalise the (static) product attributes onto the
    MBQ row so the listing never has to scan the 3.6M-row master again."""
    keys = sorted({str(r).strip() for r in ref_arts if str(r).strip()})
    if not keys:
        return {}
    params: Dict[str, Any] = {}
    inlist = ", ".join(f":k{i}" for i in range(len(keys)))
    for i, k in enumerate(keys):
        params[f"k{i}"] = k
    sql = f"""
        SELECT keyval, MAX(DIV) AS div, MAX(SUB_DIV) AS sub_div, MAX(MAJ_CAT) AS maj_cat,
               MAX(SZ) AS sz, MAX(GEN_ART_DESC) AS [desc]
        FROM (
            SELECT LTRIM(RTRIM(CAST(REF_ART AS NVARCHAR(50)))) AS keyval,
                   DIV, SUB_DIV, MAJ_CAT, SZ, GEN_ART_DESC FROM {MPROD}
              WHERE REF_ART IS NOT NULL AND REF_ART NOT IN ('NA','0','')
            UNION ALL
            SELECT LTRIM(RTRIM(CAST(GEN_ART_NUMBER AS NVARCHAR(50)))) AS keyval,
                   DIV, SUB_DIV, MAJ_CAT, SZ, GEN_ART_DESC FROM {MPROD}
        ) u
        WHERE u.keyval IN ({inlist})
        GROUP BY keyval"""
    out: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute(text(sql), params).mappings().all():
        out[str(r["keyval"]).strip()] = {
            "div": (r["div"] or ""), "sub_div": (r["sub_div"] or ""),
            "maj_cat": (r["maj_cat"] or ""), "sz": (r["sz"] or ""), "desc": (r["desc"] or ""),
        }
    return out


def _norm_source_type(v: Any) -> Optional[str]:
    """Normalise a free-text sourcing value to 'CENTRAL' | 'LOCAL' (None = unspecified)."""
    s = str(v or "").strip().upper()
    if not s or s in ("NAN", "NONE"):
        return None
    if s in ("L", "LOCAL", "LOC", "STORE"):
        return "LOCAL"
    if s in ("C", "CENTRAL", "CEN", "RDC", "DC"):
        return "CENTRAL"
    return "LOCAL" if s.startswith("L") else "CENTRAL"


def resolve_stream(ref_art: str) -> str:
    """Derive FA/CONS for a single ref-art from its DIV (default FA)."""
    with data_engine.connect() as c:
        m = _art_attrs_map(c, [ref_art])
    return _stream_from_div((m.get(str(ref_art).strip()) or {}).get("div"))


def _clean_text(v: Any) -> Optional[str]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return None if s == "" or s.lower() in ("nan", "none", "nat") else s


def _num(v: Any) -> Optional[float]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return round(float(v), 3)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Provisioning
# ══════════════════════════════════════════════════════════════════════════════
def ensure_tables() -> None:
    ddl = [f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{MBQ}')
        CREATE TABLE {MBQ} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            stream NVARCHAR(10) NOT NULL,
            st_cd NVARCHAR(20) NOT NULL,
            ref_art NVARCHAR(30) NOT NULL,
            clr NVARCHAR(20) NOT NULL DEFAULT '',
            maj_cat NVARCHAR(50) NULL,
            opt_type NVARCHAR(10) NULL,
            mbq_q FLOAT NOT NULL DEFAULT 0,
            priority INT NULL,
            cover_days INT NULL,
            reason NVARCHAR(500) NULL,
            source NVARCHAR(20) NOT NULL DEFAULT 'manual',
            eff_from DATE NULL,
            eff_to DATE NULL,
            created_at DATETIME NOT NULL DEFAULT GETDATE(),
            updated_at DATETIME NOT NULL DEFAULT GETDATE(),
            updated_by NVARCHAR(100) NULL,
            CONSTRAINT CK_FACONS_MBQ_stream CHECK (stream IN ('FA','CONS')),
            CONSTRAINT UQ_FACONS_MBQ UNIQUE (stream, st_cd, ref_art, clr)
        );""",
        # immutable event log — one row per MBQ create / modify / delete, so every
        # change to an MBQ value is auditable end-to-end.
        f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{HIST}')
        CREATE TABLE {HIST} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            stream NVARCHAR(10) NULL,
            st_cd NVARCHAR(20) NOT NULL,
            ref_art NVARCHAR(30) NOT NULL,
            clr NVARCHAR(20) NOT NULL DEFAULT '',
            action NVARCHAR(10) NOT NULL,      -- CREATE | UPDATE | DELETE
            old_mbq FLOAT NULL,
            new_mbq FLOAT NULL,
            reason NVARCHAR(500) NULL,
            source NVARCHAR(20) NULL,
            changed_by NVARCHAR(100) NULL,
            changed_at DATETIME NOT NULL DEFAULT GETDATE()
        );""",
        # one row per upload batch — the "session" that a set of change events belongs
        # to, so Change Review can slice changes session-wise (which upload made them).
        f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{SESS}')
        CREATE TABLE {SESS} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            source NVARCHAR(20) NOT NULL DEFAULT 'upload',   -- upload | edit
            filename NVARCHAR(260) NULL,
            reason NVARCHAR(500) NULL,
            n_created INT NOT NULL DEFAULT 0,
            n_updated INT NOT NULL DEFAULT 0,
            n_unchanged INT NOT NULL DEFAULT 0,
            n_skipped INT NOT NULL DEFAULT 0,
            n_fa INT NOT NULL DEFAULT 0,
            n_cons INT NOT NULL DEFAULT 0,
            n_errors INT NOT NULL DEFAULT 0,
            created_by NVARCHAR(100) NULL,
            created_at DATETIME NOT NULL DEFAULT GETDATE()
        );"""]
    idx = f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_FACONS_MBQ_HIST_key') CREATE INDEX IX_FACONS_MBQ_HIST_key ON {HIST}(st_cd, ref_art, changed_at DESC);"
    # additive columns (safe on tables created by an earlier version)
    alters = [
        f"IF COL_LENGTH('{MBQ}','remarks') IS NULL ALTER TABLE {MBQ} ADD remarks NVARCHAR(500) NULL;",
        f"IF COL_LENGTH('{MBQ}','approved_by') IS NULL ALTER TABLE {MBQ} ADD approved_by NVARCHAR(100) NULL;",
        f"IF COL_LENGTH('{MBQ}','approved_at') IS NULL ALTER TABLE {MBQ} ADD approved_at DATETIME NULL;",
        # denormalised (static) product attributes — populated at save/upload so the
        # listing never scans VW_MASTER_PRODUCT (maj_cat already exists on the table)
        f"IF COL_LENGTH('{MBQ}','div') IS NULL ALTER TABLE {MBQ} ADD div NVARCHAR(20) NULL;",
        f"IF COL_LENGTH('{MBQ}','sub_div') IS NULL ALTER TABLE {MBQ} ADD sub_div NVARCHAR(50) NULL;",
        f"IF COL_LENGTH('{MBQ}','sz') IS NULL ALTER TABLE {MBQ} ADD sz NVARCHAR(20) NULL;",
        f"IF COL_LENGTH('{MBQ}','ref_art_desc') IS NULL ALTER TABLE {MBQ} ADD ref_art_desc NVARCHAR(200) NULL;",
        # CENTRAL/LOCAL sourcing — a ref-art property (Phase 2). Default CENTRAL.
        f"IF COL_LENGTH('{MBQ}','source_type') IS NULL ALTER TABLE {MBQ} ADD source_type NVARCHAR(10) NULL;",
        f"UPDATE {MBQ} SET source_type='CENTRAL' WHERE source_type IS NULL;",
        f"IF COL_LENGTH('{HIST}','old_remarks') IS NULL ALTER TABLE {HIST} ADD old_remarks NVARCHAR(500) NULL;",
        f"IF COL_LENGTH('{HIST}','new_remarks') IS NULL ALTER TABLE {HIST} ADD new_remarks NVARCHAR(500) NULL;",
        # link each change event to the upload batch (session) that produced it
        f"IF COL_LENGTH('{HIST}','session_id') IS NULL ALTER TABLE {HIST} ADD session_id INT NULL;",
    ]
    idx2 = f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_FACONS_MBQ_HIST_session') CREATE INDEX IX_FACONS_MBQ_HIST_session ON {HIST}(session_id);"
    with data_engine.begin() as c:
        for stmt in ddl:
            c.execute(text(stmt))
        c.execute(text(idx))
        for stmt in alters:
            c.execute(text(stmt))
        c.execute(text(idx2))


def _log_event(conn, stream, st_cd, ref_art, clr, action, old_mbq, new_mbq,
               reason=None, source=None, user=None,
               old_remarks=None, new_remarks=None, session_id=None) -> None:
    conn.execute(text(f"""
        INSERT INTO {HIST} (stream, st_cd, ref_art, clr, action, old_mbq, new_mbq, old_remarks, new_remarks, reason, source, changed_by, session_id)
        VALUES (:s,:sc,:ra,:cl,:ac,:om,:nm,:orm,:nrm,:rsn,:src,:usr,:sid)"""),
        {"s": stream, "sc": st_cd, "ra": ref_art, "cl": clr or "", "ac": action,
         "om": old_mbq, "nm": new_mbq, "orm": old_remarks, "nrm": new_remarks,
         "rsn": reason, "src": source, "usr": user, "sid": session_id})


def create_session(source: str = "upload", filename: Optional[str] = None,
                   reason: Optional[str] = None, counts: Optional[Dict[str, int]] = None,
                   user: Optional[str] = None) -> int:
    """Open an upload batch (session) row and return its id; change events logged with
    this id are reviewable session-wise in Change Review."""
    ensure_tables()
    c = counts or {}
    with data_engine.begin() as conn:
        row = conn.execute(text(f"""
            INSERT INTO {SESS} (source, filename, reason, n_created, n_updated, n_unchanged,
                                n_skipped, n_fa, n_cons, n_errors, created_by)
            OUTPUT INSERTED.id
            VALUES (:src,:fn,:rsn,:cr,:up,:un,:sk,:fa,:co,:er,:usr)"""),
            {"src": source, "fn": filename, "rsn": _clean_text(reason),
             "cr": c.get("created", 0), "up": c.get("updated", 0), "un": c.get("unchanged", 0),
             "sk": c.get("skipped", 0), "fa": c.get("fa", 0), "co": c.get("cons", 0),
             "er": c.get("error_count", 0), "usr": user}).scalar()
    return int(row)


def update_session_errors(session_id: int, n_errors: int) -> None:
    with data_engine.begin() as conn:
        conn.execute(text(f"UPDATE {SESS} SET n_errors=:e WHERE id=:i"), {"e": int(n_errors), "i": session_id})


# ══════════════════════════════════════════════════════════════════════════════
# Write ops
# ══════════════════════════════════════════════════════════════════════════════
def save_mbq(stream: str, st_cd: str, ref_art: str, mbq_q: Any,
             clr: str = "", priority: Any = None, cover_days: Any = None,
             reason: Optional[str] = None, remarks: Optional[str] = None,
             source: str = "manual", user: Optional[str] = None,
             attrs: Optional[Dict[str, Any]] = None, session_id: Optional[int] = None,
             source_type: Optional[str] = None) -> Dict[str, Any]:
    """Upsert one MBQ row on the (stream, st_cd, ref_art, clr) grain. Logs an event
    when the qty OR the remarks change (CREATE on first insert), capturing the user.
    Denormalises the product attributes (div/sub_div/maj_cat/sz/desc) onto the row so
    the listing never scans the master — `attrs` may be supplied (bulk upload) else
    resolved here."""
    ensure_tables()
    stream = _norm_stream(stream)
    st_cd = (st_cd or "").strip().upper()
    ref_art = (ref_art or "").strip()
    clr = (clr or "").strip()
    if not st_cd or not ref_art:
        raise ValueError("st_cd and ref_art are required")
    q = _num(mbq_q)
    if q is None:
        raise ValueError(f"MBQ_Q must be numeric (got {mbq_q!r})")
    prio = None if priority in (None, "") else int(float(priority))
    cov = None if cover_days in (None, "") else int(float(cover_days))
    rmk = _clean_text(remarks)
    with data_engine.begin() as c:
        if attrs is None:
            attrs = _art_attrs_map(c, [ref_art]).get(ref_art, {})
        a = {k: (attrs.get(k) or None) for k in ("div", "sub_div", "maj_cat", "sz", "desc")}
        prev = c.execute(text(
            f"SELECT mbq_q, remarks FROM {MBQ} WHERE stream=:st AND st_cd=:sc AND ref_art=:ra AND clr=:cl"),
            {"st": stream, "sc": st_cd, "ra": ref_art, "cl": clr}).mappings().first()
        prev_q = float(prev["mbq_q"]) if prev and prev["mbq_q"] is not None else None
        prev_rmk = _clean_text(prev["remarks"]) if prev else None
        # keep the existing remark if the caller didn't supply one (blank upload cell)
        new_rmk = rmk if rmk is not None else prev_rmk
        c.execute(text(f"""
            MERGE {MBQ} AS tgt
            USING (SELECT :st AS stream, :sc AS st_cd, :ra AS ref_art, :cl AS clr) AS src
            ON tgt.stream=src.stream AND tgt.st_cd=src.st_cd
               AND tgt.ref_art=src.ref_art AND tgt.clr=src.clr
            WHEN MATCHED THEN UPDATE SET mbq_q=:q, priority=:pr, cover_days=:cov,
                 reason=:rsn, remarks=:rmk, source=:src, updated_at=GETDATE(), updated_by=:usr,
                 div=:div, sub_div=:sub_div, maj_cat=:maj_cat, sz=:sz, ref_art_desc=:desc,
                 source_type=ISNULL(:styp, source_type)
            WHEN NOT MATCHED THEN
                INSERT (stream, st_cd, ref_art, clr, mbq_q, priority, cover_days, reason, remarks, source, updated_by,
                        div, sub_div, maj_cat, sz, ref_art_desc, source_type)
                VALUES (:st,:sc,:ra,:cl,:q,:pr,:cov,:rsn,:rmk,:src,:usr,
                        :div,:sub_div,:maj_cat,:sz,:desc, ISNULL(:styp,'CENTRAL'));"""),
            {"st": stream, "sc": st_cd, "ra": ref_art, "cl": clr, "q": q,
             "pr": prio, "cov": cov, "rsn": _clean_text(reason), "rmk": new_rmk, "src": source, "usr": user,
             "div": a["div"], "sub_div": a["sub_div"], "maj_cat": a["maj_cat"], "sz": a["sz"], "desc": a["desc"],
             "styp": _norm_source_type(source_type)})
        # event: CREATE if new; UPDATE when the qty OR the remark actually changed
        qty_chg = prev_q is not None and prev_q != q
        rmk_chg = prev is not None and (prev_rmk or "") != (new_rmk or "")
        action = "CREATE" if prev is None else ("UPDATE" if (qty_chg or rmk_chg) else None)
        if action:
            _log_event(c, stream, st_cd, ref_art, clr, action, prev_q, q,
                       reason=_clean_text(reason), source=source, user=user,
                       old_remarks=prev_rmk, new_remarks=new_rmk, session_id=session_id)
    return {"stream": stream, "st_cd": st_cd, "ref_art": ref_art, "clr": clr, "mbq_q": q}


def delete_mbq(row_id: int, user: Optional[str] = None, reason: Optional[str] = None) -> None:
    """Delete one MBQ row and log a DELETE event. `reason` (why it was removed) is
    MANDATORY — it is recorded on the event for Change Review."""
    ensure_tables()
    rsn = _clean_text(reason)
    if not rsn:
        raise ValueError("Reason for this deletion is mandatory")
    with data_engine.begin() as c:
        row = c.execute(text(
            f"SELECT stream, st_cd, ref_art, clr, mbq_q FROM {MBQ} WHERE id=:i"),
            {"i": row_id}).mappings().first()
        c.execute(text(f"DELETE FROM {MBQ} WHERE id=:i"), {"i": row_id})
        if row:
            _log_event(c, row["stream"], row["st_cd"], row["ref_art"], row["clr"],
                       "DELETE", float(row["mbq_q"]) if row["mbq_q"] is not None else None,
                       None, reason=rsn, source="edit", user=user)


def list_history(st_cd: Optional[str] = None, ref_art: Optional[str] = None,
                 limit: int = 300) -> List[Dict[str, Any]]:
    """MBQ change events, newest first — all, or scoped to one store/ref-art."""
    ensure_tables()
    where, params = [], {}
    if st_cd:
        where.append("st_cd=:sc"); params["sc"] = st_cd.strip().upper()
    if ref_art:
        where.append("ref_art=:ra"); params["ra"] = ref_art.strip()
    wc = ("WHERE " + " AND ".join(where)) if where else ""
    with data_engine.connect() as c:
        rows = c.execute(text(
            f"SELECT TOP {int(limit)} id, stream, st_cd, ref_art, clr, action, old_mbq, new_mbq, "
            f"old_remarks, new_remarks, reason, source, changed_by, changed_at FROM {HIST} {wc} "
            f"ORDER BY changed_at DESC, id DESC"),
            params).mappings().all()
    return [{
        "id": r["id"], "stream": r["stream"], "st_cd": r["st_cd"], "ref_art": r["ref_art"],
        "clr": r["clr"], "action": r["action"],
        "old_mbq": _num(r["old_mbq"]), "new_mbq": _num(r["new_mbq"]),
        "old_remarks": r["old_remarks"], "new_remarks": r["new_remarks"],
        "reason": r["reason"], "source": r["source"], "changed_by": r["changed_by"],
        "changed_at": r["changed_at"].isoformat() if r["changed_at"] else None,
    } for r in rows]


def _review_where(date_from, date_to, stream, action, st_cd, ref_art, changed_by, session_id=None):
    where, params = [], {}
    # a specific session overrides the date window (all of that upload's events, whenever)
    if session_id not in (None, "", 0):
        where.append("session_id=:sid"); params["sid"] = int(session_id)
    else:
        if date_from:
            where.append("CAST(changed_at AS DATE) >= :df"); params["df"] = date_from
        if date_to:
            where.append("CAST(changed_at AS DATE) <= :dt"); params["dt"] = date_to
    if stream and str(stream).strip().upper() in VALID_STREAM:
        where.append("stream=:st"); params["st"] = str(stream).strip().upper()
    if action:
        where.append("action=:ac"); params["ac"] = str(action).strip().upper()
    if st_cd:
        where.append("st_cd=:sc"); params["sc"] = str(st_cd).strip().upper()
    if ref_art:
        where.append("ref_art=:ra"); params["ra"] = str(ref_art).strip()
    if changed_by:
        where.append("changed_by=:cb"); params["cb"] = str(changed_by).strip()
    return (("WHERE " + " AND ".join(where)) if where else ""), params


def change_review(date_from=None, date_to=None, stream=None, action=None,
                  st_cd=None, ref_art=None, changed_by=None, session_id=None,
                  limit: int = 2000) -> Dict[str, Any]:
    """On-demand audit of MBQ change events for a date or date range (+ optional
    filters, incl. a specific upload `session_id`), with a summary (created/changed/
    deleted, distinct stores/ref-arts, net MBQ delta). Reads ARS_FACONS_MBQ_HIST only."""
    ensure_tables()
    wc, params = _review_where(date_from, date_to, stream, action, st_cd, ref_art, changed_by, session_id)
    with data_engine.connect() as c:
        rows = c.execute(text(
            f"SELECT TOP {int(limit)} id, stream, st_cd, ref_art, action, old_mbq, new_mbq, "
            f"old_remarks, new_remarks, reason, source, changed_by, changed_at, session_id FROM {HIST} {wc} "
            f"ORDER BY changed_at DESC, id DESC"), params).mappings().all()
        s = c.execute(text(
            f"SELECT COUNT(*) total, "
            f"SUM(CASE WHEN action='CREATE' THEN 1 ELSE 0 END) created, "
            f"SUM(CASE WHEN action='UPDATE' THEN 1 ELSE 0 END) updated, "
            f"SUM(CASE WHEN action='DELETE' THEN 1 ELSE 0 END) deleted, "
            f"SUM(CASE WHEN action='APPROVE' THEN 1 ELSE 0 END) approved, "
            f"COUNT(DISTINCT st_cd) stores, COUNT(DISTINCT ref_art) ref_arts, "
            f"SUM(ISNULL(new_mbq,0)-ISNULL(old_mbq,0)) net FROM {HIST} {wc}"), params).mappings().first()
    items = [{
        "id": r["id"], "stream": r["stream"], "st_cd": r["st_cd"], "ref_art": r["ref_art"],
        "action": r["action"], "old_mbq": _num(r["old_mbq"]), "new_mbq": _num(r["new_mbq"]),
        "old_remarks": r["old_remarks"], "new_remarks": r["new_remarks"],
        "reason": r["reason"], "source": r["source"], "changed_by": r["changed_by"],
        "changed_at": r["changed_at"].isoformat() if r["changed_at"] else None,
        "session_id": r["session_id"],
    } for r in rows]
    summary = {
        "total": s["total"] or 0, "created": s["created"] or 0, "updated": s["updated"] or 0,
        "deleted": s["deleted"] or 0, "approved": s["approved"] or 0,
        "stores": s["stores"] or 0, "ref_arts": s["ref_arts"] or 0,
        "net_delta": float(s["net"]) if s["net"] is not None else 0.0,
        "shown": len(items), "capped": len(items) >= int(limit),
    }
    return {"items": items, "summary": summary}


def change_review_export(**kw) -> bytes:
    data = change_review(limit=100000, **kw)
    df = pd.DataFrame(data["items"])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="MBQ Change Review")
    return buf.getvalue()


def list_sessions(date_from=None, date_to=None, limit: int = 50) -> List[Dict[str, Any]]:
    """Recent upload batches (sessions), newest first — for session-wise change review.
    Each row carries its reason, user, counts and actual logged-event count."""
    ensure_tables()
    where, params = [], {}
    if date_from:
        where.append("CAST(s.created_at AS DATE) >= :df"); params["df"] = date_from
    if date_to:
        where.append("CAST(s.created_at AS DATE) <= :dt"); params["dt"] = date_to
    wc = ("WHERE " + " AND ".join(where)) if where else ""
    with data_engine.connect() as c:
        rows = c.execute(text(
            f"SELECT TOP {int(limit)} s.id, s.source, s.filename, s.reason, s.n_created, s.n_updated, "
            f"s.n_unchanged, s.n_skipped, s.n_fa, s.n_cons, s.n_errors, s.created_by, s.created_at, "
            f"(SELECT COUNT(*) FROM {HIST} h WHERE h.session_id = s.id) AS n_events "
            f"FROM {SESS} s {wc} ORDER BY s.created_at DESC, s.id DESC"), params).mappings().all()
    return [{
        "id": r["id"], "source": r["source"], "filename": r["filename"], "reason": r["reason"],
        "n_created": r["n_created"], "n_updated": r["n_updated"], "n_unchanged": r["n_unchanged"],
        "n_skipped": r["n_skipped"], "n_fa": r["n_fa"], "n_cons": r["n_cons"],
        "n_errors": r["n_errors"], "n_events": r["n_events"],
        "created_by": r["created_by"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    } for r in rows]


def ingest_upload(file_bytes: bytes, user: Optional[str] = None, dry_run: bool = False,
                  reason: Optional[str] = None, filename: Optional[str] = None) -> Dict[str, Any]:
    """Ingest the MBQ workbook (ST_CD, REF_ART, MBQ_Q, REMARKS — all mandatory).
    Each row is AUTO-SEGREGATED into FA vs CONS by the article's DIV (CO→CONS, FA→FA).
    `reason` is a batch-level change justification stamped on every CREATE/UPDATE
    event logged by this upload (the "why" shown in MBQ Change Review).

    dry_run=True → parse + classify each row (CREATE / UPDATE / UNCHANGED / skipped /
    error) and return counts + per-row details WITHOUT writing anything — the caller
    shows a confirmation, then re-uploads with dry_run=False to commit."""
    ensure_tables()
    df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
    norm = {c: str(c).strip().lower().replace(" ", "_").replace("-", "_").replace("\n", "_")
            for c in df.columns}
    df = df.rename(columns=norm)

    def pick(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    c_st = pick("st_cd", "stcd", "store", "store_code", "site_code", "werks")
    c_ra = pick("ref_art", "refart", "ref_article", "reference_article",
                "gen_art", "gen_art_number", "article", "matnr")
    c_q = pick("mbq_q", "mbq", "mbq_qty", "mbqq", "qty", "quantity")
    c_rmk = pick("remarks", "remark", "note", "notes", "comment", "comments")
    c_type = pick("source_type", "type", "ref_type", "ref_art_type", "central_local", "sourcing")
    missing_cols = [name for name, col in
                    (("ST_CD", c_st), ("REF_ART", c_ra), ("MBQ_Q", c_q), ("REMARKS", c_rmk)) if not col]
    if missing_cols:
        raise ValueError("Upload is missing mandatory column(s): " + ", ".join(missing_cols)
                         + ". Download the template.")

    # bulk-resolve product attrs (DIV→stream + …) AND existing MBQ values (for
    # CREATE/UPDATE/UNCHANGED detection) in one pass each — no per-row queries.
    all_refs = [str(r.get(c_ra) or "").strip() for _, r in df.iterrows()]
    with data_engine.connect() as c:
        attrs_map = _art_attrs_map(c, all_refs)
        existing = {(str(a), str(b).strip().upper(), str(cc).strip()): float(d or 0)
                    for a, b, cc, d in c.execute(text(
                        f"SELECT stream, st_cd, ref_art, mbq_q FROM {MBQ}")).fetchall()}

    # in-sheet duplicate detection: same (ST_CD, REF_ART) appearing more than once.
    # The upload is an upsert (last row wins) — so a duplicate silently overwrites; flag it.
    key_counts: Dict[tuple, int] = {}
    for _, r in df.iterrows():
        sc = str(r.get(c_st) or "").strip().upper()
        ra = str(r.get(c_ra) or "").strip()
        if sc and sc not in ("NAN", "NONE") and ra and ra.upper() not in ("NAN", "NONE"):
            key_counts[(sc, ra)] = key_counts.get((sc, ra), 0) + 1
    dup_keys = sum(1 for v in key_counts.values() if v > 1)
    dup_rows = sum(v - 1 for v in key_counts.values() if v > 1)

    created = updated = unchanged = skipped = 0
    seg = {"FA": 0, "CONS": 0}
    errors: List[str] = []
    details: List[Dict[str, Any]] = []
    to_save: List[tuple] = []
    for i, r in df.iterrows():
        st_cd = str(r.get(c_st) or "").strip().upper()
        ref_art = str(r.get(c_ra) or "").strip()
        if not st_cd or st_cd in ("NAN", "NONE") or not ref_art or ref_art.upper() in ("NAN", "NONE"):
            skipped += 1
            continue
        q = _num(r.get(c_q))
        if q is None:
            errors.append(f"row {int(i) + 2} ({st_cd}/{ref_art}): MBQ_Q not numeric")
            continue
        rmk = _clean_text(r.get(c_rmk))
        if rmk is None:
            errors.append(f"row {int(i) + 2} ({st_cd}/{ref_art}): REMARKS is mandatory")
            continue
        attrs = attrs_map.get(ref_art, {})
        stream = _stream_from_div(attrs.get("div"))   # auto-segregate by DIV
        prev = existing.get((stream, st_cd, ref_art))
        action = "CREATE" if prev is None else ("UPDATE" if float(prev) != q else "UNCHANGED")
        if action == "CREATE":
            created += 1
        elif action == "UPDATE":
            updated += 1
        else:
            unchanged += 1
        seg[stream] += 1
        if len(details) < 2000:
            details.append({"st_cd": st_cd, "ref_art": ref_art, "stream": stream, "action": action,
                            "old_mbq": _num(prev) if prev is not None else None, "new_mbq": q,
                            "dup": key_counts.get((st_cd, ref_art), 0) > 1})
        styp = _norm_source_type(r.get(c_type)) if c_type else None
        to_save.append((stream, st_cd, ref_art, q, rmk, attrs, styp))

    session_id = None
    if not dry_run:
        rsn = _clean_text(reason)
        if not rsn:
            raise ValueError("Reason for this change is mandatory")
        # open the upload batch (session) so its change events are reviewable together
        session_id = create_session(
            source="upload", filename=filename, reason=rsn, user=user,
            counts={"created": created, "updated": updated, "unchanged": unchanged,
                    "skipped": skipped, "fa": seg["FA"], "cons": seg["CONS"], "error_count": 0})
        for stream, st_cd, ref_art, q, rmk, attrs, styp in to_save:
            try:
                save_mbq(stream, st_cd, ref_art, q, remarks=rmk, reason=rsn,
                         source="upload", user=user, attrs=attrs, session_id=session_id,
                         source_type=styp)
            except Exception as e:
                errors.append(f"{st_cd}/{ref_art}: {e}")
        if errors:
            update_session_errors(session_id, len(errors))
    return {"rows": int(len(df)), "dry_run": dry_run, "session_id": session_id,
            "processed": created + updated + unchanged,
            "created": created, "updated": updated, "unchanged": unchanged, "skipped": skipped,
            "fa": seg["FA"], "cons": seg["CONS"],
            "duplicates": dup_keys, "dup_rows": dup_rows,
            "errors": errors[:50], "error_count": len(errors), "details": details}


def reclassify_streams(user: Optional[str] = None) -> Dict[str, Any]:
    """Recompute the FA/CONS stream of every existing MBQ row from its DIV
    (CO→CONS, FA→FA). Fixes rows loaded before auto-segregation. Only touches
    ARS_FACONS_MBQ; skips a change that would collide with an existing row on the
    (stream, st_cd, ref_art, clr) key."""
    ensure_tables()
    changed = 0
    with data_engine.begin() as c:
        rows = c.execute(text(f"SELECT id, st_cd, ref_art, clr, stream FROM {MBQ}")).mappings().all()
        attrs_map = _art_attrs_map(c, [r["ref_art"] for r in rows])
        for r in rows:
            target = _stream_from_div((attrs_map.get(str(r["ref_art"]).strip()) or {}).get("div"))
            if target == r["stream"]:
                continue
            clash = c.execute(text(
                f"SELECT 1 FROM {MBQ} WHERE stream=:s AND st_cd=:sc AND ref_art=:ra AND clr=:cl AND id<>:id"),
                {"s": target, "sc": r["st_cd"], "ra": r["ref_art"], "cl": r["clr"] or "", "id": r["id"]}).first()
            if clash:
                c.execute(text(f"DELETE FROM {MBQ} WHERE id=:id"), {"id": r["id"]})  # dup — drop the mis-tagged one
            else:
                c.execute(text(f"UPDATE {MBQ} SET stream=:s, updated_by=:u, updated_at=GETDATE() WHERE id=:id"),
                          {"s": target, "u": user or "reclassify", "id": r["id"]})
            changed += 1
    return {"changed": changed}


def backfill_attrs(user: Optional[str] = None) -> Dict[str, Any]:
    """One-time: populate the denormalised product attrs (div/sub_div/maj_cat/sz/
    ref_art_desc) on existing MBQ rows from the master, so the fast listing has them
    without re-upload. Resolves all distinct ref-arts in ONE master scan."""
    ensure_tables()
    with data_engine.begin() as c:
        refs = [str(r[0]).strip() for r in c.execute(text(f"SELECT DISTINCT ref_art FROM {MBQ}")).fetchall()]
        amap = _art_attrs_map(c, refs)
        n = 0
        for ref, a in amap.items():
            res = c.execute(text(
                f"UPDATE {MBQ} SET div=:d, sub_div=:sd, maj_cat=:mc, sz=:sz, ref_art_desc=:ds "
                f"WHERE LTRIM(RTRIM(ref_art))=:r"),
                {"d": a["div"] or None, "sd": a["sub_div"] or None, "mc": a["maj_cat"] or None,
                 "sz": a["sz"] or None, "ds": a["desc"] or None, "r": ref})
            n += res.rowcount or 0
    return {"ref_arts": len(amap), "rows_updated": n}


def _exists(stream: str, st_cd: str, ref_art: str) -> bool:
    with data_engine.connect() as c:
        return c.execute(text(
            f"SELECT 1 FROM {MBQ} WHERE stream=:st AND st_cd=:sc AND ref_art=:ra"),
            {"st": stream, "sc": st_cd.strip().upper(), "ra": ref_art.strip()}).first() is not None


# ══════════════════════════════════════════════════════════════════════════════
# Read ops (live store-master enrichment)
# ══════════════════════════════════════════════════════════════════════════════
def list_mbq(stream: Optional[str] = None) -> List[Dict[str, Any]]:
    """MBQ rows (all streams, or one), enriched LIVE from:
      • the store master (name/RDC/hub/OP-date/status), and
      • VW_MASTER_PRODUCT keyed on REF_ART (DIV / SUB_DIV / MAJ_CAT / SZ) — with a
        GEN_ART_NUMBER fallback so multi-variant FA reference articles also resolve.
    stream=None → all rows (each carries its auto-segregated FA/CONS stream)."""
    ensure_tables()
    st = _norm_stream(stream) if stream else None
    where = "b.stream=:st" if st else "1=1"
    mbq_filter = "WHERE stream=:st" if st else ""
    params: Dict[str, Any] = {"st": st} if st else {}
    # FAST: product attributes are denormalised on the row (div/sub_div/maj_cat/sz/
    # ref_art_desc) — no VW_MASTER_PRODUCT scan. Only a tiny join to the store master.
    sql = f"""
        SELECT b.id, b.stream, b.st_cd, b.ref_art, b.clr, b.opt_type,
               b.mbq_q, b.priority, b.cover_days, b.reason, b.remarks,
               b.source, b.source_type, b.updated_at, b.div, b.sub_div, b.maj_cat, b.sz, b.ref_art_desc,
               m.ST_NM AS site_name, m.RDC AS rdc, m.HUB AS hub,
               m.OP_DT AS op_dt, m.ST_STATUS AS st_status
        FROM {MBQ} b
        LEFT JOIN {MASTER} m ON m.ST_CD = b.st_cd
        WHERE {where}
        ORDER BY b.stream, b.st_cd, b.ref_art, b.clr"""
    with data_engine.connect() as c:
        rows = c.execute(text(sql), params).mappings().all()
        out = []
        for r in rows:
            out.append({
                "id": r["id"], "stream": r["stream"], "st_cd": r["st_cd"],
                "site_name": r.get("site_name"), "rdc": r.get("rdc"), "hub": r.get("hub"),
                "op_dt": r["op_dt"].isoformat() if r.get("op_dt") else None,
                "st_status": r.get("st_status"),
                "ref_art": r["ref_art"], "ref_art_desc": r.get("ref_art_desc") or None,
                "clr": r.get("clr"),
                "div": r.get("div") or None, "sub_div": r.get("sub_div") or None,
                "maj_cat": r.get("maj_cat") or None, "sz": r.get("sz") or None,
                "opt_type": r.get("opt_type"), "source_type": r.get("source_type") or "CENTRAL",
                "mbq_q": _num(r.get("mbq_q")), "priority": r.get("priority"),
                "cover_days": r.get("cover_days"), "reason": r.get("reason"),
                "remarks": r.get("remarks"),
                "source": r.get("source"),
                "updated_at": r["updated_at"].isoformat() if r.get("updated_at") else None,
            })
        _attach_persisted_stock(c, out)   # fast indexed read of the calc result
        _attach_hist_count(c, out)        # change-event count per row (for the history badge)
    return out


def _attach_hist_count(conn, out: List[Dict[str, Any]]) -> None:
    """Set `hist_count` = number of change events per (st_cd, ref_art) — matches the
    scope of the per-row history view. ONE grouped read over ARS_FACONS_MBQ_HIST."""
    for r in out:
        r["hist_count"] = 0
    if not out:
        return
    try:
        cnt: Dict[tuple, int] = {}
        for st_cd, ref_art, n in conn.execute(text(
                f"SELECT st_cd, ref_art, COUNT(*) AS n FROM {HIST} GROUP BY st_cd, ref_art")).fetchall():
            cnt[(str(st_cd).strip(), str(ref_art).strip())] = int(n or 0)
        for r in out:
            r["hist_count"] = cnt.get((str(r["st_cd"]).strip(), str(r["ref_art"]).strip()), 0)
    except Exception as e:
        logger.warning(f"[facons] hist count read skipped: {e}")


def _attach_persisted_stock(conn, out: List[Dict[str, Any]]) -> None:
    """FAST: read the LAST-RUN store stock (ARS_FACONS_STOCK, per stream×st_cd×ref_art)
    AND the DC/MSA pool (ARS_FACONS_MSA, per stream×rdc×ref_art) from the persisted calc
    results — no live ET_* scan. Sets `stock` (store), `dc_stock` (MSA/DC), `balance`
    (max(mbq−store,0)) and `fill_rate_pct`. Requires a Stock & MSA run; degrades to 0."""
    for r in out:
        r["stock"], r["dc_stock"], r["balance"], r["fill_rate_pct"], r["sloc"] = 0.0, 0.0, None, None, {}
    if not out:
        return
    stk: Dict[tuple, float] = {}
    dc: Dict[tuple, float] = {}
    try:
        for stream, st_cd, ref_art, q in conn.execute(text(
                f"SELECT stream, st_cd, ref_art, SUM(store_stk_ttl) AS q "
                f"FROM {STOCK_TBL} GROUP BY stream, st_cd, ref_art")).fetchall():
            stk[(str(stream), str(st_cd).strip(), str(ref_art).strip())] = float(q or 0)
        for stream, rdc, ref_art, q in conn.execute(text(
                f"SELECT stream, rdc, ref_art, SUM(dc_stk_ttl) AS q "
                f"FROM {MSA_TBL} GROUP BY stream, rdc, ref_art")).fetchall():
            dc[(str(stream), str(rdc).strip(), str(ref_art).strip())] = float(q or 0)
    except Exception as e:
        logger.warning(f"[facons] persisted stock read skipped: {e}")
        return
    for r in out:
        ref = str(r["ref_art"]).strip()
        total = stk.get((r["stream"], str(r["st_cd"]).strip(), ref), 0.0)
        dpool = dc.get((r["stream"], str(r.get("rdc") or "").strip(), ref), 0.0)
        mbq = r.get("mbq_q") or 0
        r["stock"] = round(total, 3)
        r["dc_stock"] = round(dpool, 3)
        r["balance"] = round(max(mbq - total, 0), 3) if mbq else None
        r["fill_rate_pct"] = round(total / mbq * 100, 2) if mbq else None


def summary(stream: Optional[str] = None) -> Dict[str, Any]:
    ensure_tables()
    st = _norm_stream(stream) if stream else None
    where = "WHERE stream=:st" if st else ""
    params: Dict[str, Any] = {"st": st} if st else {}
    with data_engine.connect() as c:
        row = c.execute(text(
            f"SELECT COUNT(*) AS rows_n, COUNT(DISTINCT st_cd) AS stores, "
            f"COUNT(DISTINCT ref_art) AS ref_arts, SUM(mbq_q) AS total_mbq "
            f"FROM {MBQ} {where}"), params).mappings().first()
        # per-stream breakdown (the auto-segregation result)
        seg = c.execute(text(
            f"SELECT stream, COUNT(*) AS n FROM {MBQ} {where} GROUP BY stream"),
            params).fetchall()
    return {"stream": st or "ALL", "rows": row["rows_n"] or 0, "stores": row["stores"] or 0,
            "ref_arts": row["ref_arts"] or 0,
            "total_mbq": float(row["total_mbq"]) if row["total_mbq"] is not None else 0.0,
            "by_stream": {str(r[0]): r[1] for r in seg}}


# ══════════════════════════════════════════════════════════════════════════════
# Template + export
# ══════════════════════════════════════════════════════════════════════════════
def template_bytes() -> bytes:
    cols = ["ST_CD", "REF_ART", "MBQ_Q", "REMARKS", "SOURCE_TYPE"]
    sample = pd.DataFrame([
        {"ST_CD": "ZZ01", "REF_ART": "100000001", "MBQ_Q": 12, "REMARKS": "layout revised", "SOURCE_TYPE": "CENTRAL"},
        {"ST_CD": "ZZ01", "REF_ART": "100000002", "MBQ_Q": 6, "REMARKS": "initial load", "SOURCE_TYPE": "LOCAL"},
    ], columns=cols)
    notes = pd.DataFrame([
        ["ST_CD", "Yes", "Store code. Must exist in the store master; name/RDC/hub/OP-date are filled automatically."],
        ["REF_ART", "Yes", "Reference (general) article number the MBQ is for."],
        ["MBQ_Q", "Yes", "Minimum-buy quantity for that store x reference article."],
        ["REMARKS", "Yes", "Mandatory note / reason for the MBQ. Tracked in the change history."],
        ["SOURCE_TYPE", "No", "CENTRAL (ship from RDC/DC) or LOCAL (handled at the store). Blank = CENTRAL. It is a property of the reference article, so it applies to all stores of that ref."],
        ["", "", ""],
        ["Mandatory columns", "", "Every row must have ST_CD + REF_ART + MBQ_Q + REMARKS. SOURCE_TYPE is optional (defaults CENTRAL)."],
        ["Upload = upsert", "", "Re-uploading a store x reference article UPDATES its MBQ (and logs the change) — it does NOT create a duplicate."],
        ["Header aliases", "", "ST_CD = STORE / WERKS; REF_ART = GEN_ART / ARTICLE; MBQ_Q = MBQ / QTY; REMARKS = REMARK / NOTE; SOURCE_TYPE = TYPE / REF_TYPE. Case/spaces ignored."],
        ["Audit", "", "Every create / MBQ change / remark change is logged with the user — see a row's history (count badge)."],
        ["Enrichment", "", "Store name, RDC, HUB, opening date, description are NOT uploaded — they are joined live."],
    ], columns=["Column", "Required", "Description"])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        sample.to_excel(w, index=False, sheet_name="Upload")
        notes.to_excel(w, index=False, sheet_name="Instructions")
        for sh, widths in (("Upload", [12, 16, 10, 26, 14]), ("Instructions", [26, 12, 98])):
            ws = w.sheets[sh]
            for i, width in enumerate(widths):
                ws.column_dimensions[chr(65 + i)].width = width
    return buf.getvalue()


def set_source_type(ref_art: str, value: str, stream: Optional[str] = None,
                    user: Optional[str] = None) -> Dict[str, Any]:
    """Set CENTRAL/LOCAL for a reference article across ALL its stores (source_type is a
    ref-art property). Optionally scope to one stream. Returns rows updated."""
    ensure_tables()
    val = _norm_source_type(value) or "CENTRAL"
    ref = (ref_art or "").strip()
    if not ref:
        raise ValueError("ref_art is required")
    params: Dict[str, Any] = {"v": val, "ra": ref, "u": user}
    where = "ref_art=:ra"
    if stream:
        where += " AND stream=:st"; params["st"] = _norm_stream(stream)
    with data_engine.begin() as c:
        res = c.execute(text(
            f"UPDATE {MBQ} SET source_type=:v, updated_by=:u, updated_at=GETDATE() WHERE {where}"), params)
    return {"ref_art": ref, "source_type": val, "rows_updated": res.rowcount or 0}


def export_bytes(stream: Optional[str] = None) -> bytes:
    rows = list_mbq(stream)
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name=(f"{_norm_stream(stream)} MBQ" if stream else "FA CONS MBQ"))
    return buf.getvalue()
