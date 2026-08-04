"""
FA & CONS — UPC Store List service.

A planner-maintained list of the stores FA/CONS allocation runs against. Each store's
UPC vs OLD status is DERIVED live from the store master (Master_ALC_INPUT_ST_MASTER):
ST_STATUS='UPC' → still a UPC (project) store, 'OLD' → opened / running store. So when
the master flips a store to OLD, it auto-shows as OLD here (no manual flip).

Validation (mirrors the BDC Schedule store check):
  • missing_in_master — uploaded stores not found in the store master
  • missing_in_list   — stores that have FA/CONS MBQ but are not in this list

Priority = OP_DT older → newer (older store ranks first).

Persisted: ARS_FACONS_STORE_LIST (+ _HIST). App-owned; safe to write.
"""
from __future__ import annotations

import io
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger
from sqlalchemy import text

from app.database.session import data_engine

LIST = "ARS_FACONS_STORE_LIST"
HIST = "ARS_FACONS_STORE_LIST_HIST"
MASTER = "Master_ALC_INPUT_ST_MASTER"
MBQ = "ARS_FACONS_MBQ"


def _clean(v: Any) -> Optional[str]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return None if s == "" or s.lower() in ("nan", "none", "nat") else s


def ensure_tables() -> None:
    ddl = [
        f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{LIST}')
        CREATE TABLE {LIST} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            st_cd NVARCHAR(20) NOT NULL,
            remarks NVARCHAR(500) NULL,
            created_by NVARCHAR(100) NULL,
            created_at DATETIME NOT NULL DEFAULT GETDATE(),
            updated_at DATETIME NOT NULL DEFAULT GETDATE(),
            CONSTRAINT UQ_FACONS_STORE_LIST UNIQUE (st_cd)
        );""",
        f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{HIST}')
        CREATE TABLE {HIST} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            st_cd NVARCHAR(20) NOT NULL,
            action NVARCHAR(12) NOT NULL,       -- ADD | REMOVE
            status_at NVARCHAR(10) NULL,        -- master status at the time
            in_master BIT NULL,
            remarks NVARCHAR(500) NULL,
            changed_by NVARCHAR(100) NULL,
            changed_at DATETIME NOT NULL DEFAULT GETDATE()
        );""",
    ]
    idx = (f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_FACONS_STORE_LIST_HIST') "
           f"CREATE INDEX IX_FACONS_STORE_LIST_HIST ON {HIST}(st_cd, changed_at DESC);")
    with data_engine.begin() as c:
        for s in ddl:
            c.execute(text(s))
        c.execute(text(idx))


def _norm_status(v: Any) -> str:
    return "OLD" if str(v or "").strip().upper() == "OLD" else "UPC"


def _log(conn, st_cd, action, status_at=None, in_master=None, remarks=None, user=None) -> None:
    conn.execute(text(f"""
        INSERT INTO {HIST} (st_cd, action, status_at, in_master, remarks, changed_by)
        VALUES (:s,:a,:st,:im,:r,:u)"""),
        {"s": st_cd, "a": action, "st": status_at, "im": (1 if in_master else 0) if in_master is not None else None,
         "r": remarks, "u": user})


def list_stores() -> List[Dict[str, Any]]:
    """The store list enriched LIVE from the master + a derived UPC/OLD status and an
    OP_DT (older→newer) priority rank."""
    ensure_tables()
    with data_engine.connect() as c:
        rows = c.execute(text(f"""
            SELECT l.id, l.st_cd, l.remarks, l.created_by, l.created_at,
                   m.ST_NM AS st_nm, m.RDC AS rdc, m.HUB AS hub, m.OP_DT AS op_dt,
                   m.ST_STATUS AS master_status, m.MANUAL_ST_PRIORITY AS manual_priority,
                   CASE WHEN m.ST_CD IS NULL THEN 0 ELSE 1 END AS in_master
            FROM {LIST} l
            LEFT JOIN {MASTER} m ON m.ST_CD = l.st_cd
            ORDER BY CASE WHEN m.OP_DT IS NULL THEN 1 ELSE 0 END, m.OP_DT ASC, l.st_cd ASC""")).mappings().all()
    out = []
    for r in rows:
        in_master = bool(r["in_master"])
        out.append({
            "id": r["id"], "st_cd": r["st_cd"], "remarks": r["remarks"],
            "st_nm": r["st_nm"], "rdc": r["rdc"], "hub": r["hub"],
            "op_dt": r["op_dt"].isoformat() if r["op_dt"] else None,
            "in_master": in_master,
            "master_status": r["master_status"],
            # derived: OLD (opened) / UPC (still project) from the master; unknown if absent
            "status": _norm_status(r["master_status"]) if in_master else "NOT IN MASTER",
            "manual_priority": r["manual_priority"],
            "created_by": r["created_by"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })
    # older-store-first priority rank (stores already ordered by OP_DT asc)
    for i, r in enumerate(out):
        r["priority"] = i + 1
    return out


def summary() -> Dict[str, Any]:
    items = list_stores()
    upc = sum(1 for r in items if r["status"] == "UPC")
    old = sum(1 for r in items if r["status"] == "OLD")
    nim = sum(1 for r in items if not r["in_master"])
    return {"total": len(items), "upc": upc, "old": old, "not_in_master": nim}


def validation() -> Dict[str, Any]:
    """Two cross-checks (like the BDC Schedule validation):
       missing_in_master — list stores absent from the store master
       missing_in_list   — stores with FA/CONS MBQ that are not in this list."""
    ensure_tables()
    with data_engine.connect() as c:
        mim = c.execute(text(f"""
            SELECT l.st_cd, l.created_by, l.created_at
            FROM {LIST} l
            WHERE NOT EXISTS (SELECT 1 FROM {MASTER} m WHERE m.ST_CD = l.st_cd)
            ORDER BY l.st_cd""")).mappings().all()
        mil = c.execute(text(f"""
            SELECT b.st_cd,
                   MAX(m.ST_NM) AS st_nm, MAX(m.ST_STATUS) AS master_status,
                   MAX(CASE WHEN m.ST_CD IS NULL THEN 0 ELSE 1 END) AS in_master,
                   COUNT(*) AS mbq_rows
            FROM {MBQ} b
            LEFT JOIN {MASTER} m ON m.ST_CD = b.st_cd
            WHERE NOT EXISTS (SELECT 1 FROM {LIST} l WHERE l.st_cd = b.st_cd)
            GROUP BY b.st_cd
            ORDER BY b.st_cd""")).mappings().all()
        mun = c.execute(text(f"""
            SELECT m.ST_CD AS st_cd, m.ST_NM AS st_nm, m.OP_DT AS op_dt
            FROM {MASTER} m
            WHERE m.ST_STATUS = 'UPC' AND NOT EXISTS (SELECT 1 FROM {LIST} l WHERE l.st_cd = m.ST_CD)
            ORDER BY CASE WHEN m.OP_DT IS NULL THEN 1 ELSE 0 END, m.OP_DT ASC, m.ST_CD""")).mappings().all()
    return {
        "missing_in_master": [{"st_cd": r["st_cd"], "created_by": r["created_by"],
                               "created_at": r["created_at"].isoformat() if r["created_at"] else None} for r in mim],
        "missing_in_list": [{"st_cd": r["st_cd"], "st_nm": r["st_nm"],
                             "master_status": r["master_status"], "in_master": bool(r["in_master"]),
                             "mbq_rows": r["mbq_rows"]} for r in mil],
        "master_upc_not_listed": [{"st_cd": r["st_cd"], "st_nm": r["st_nm"],
                                   "op_dt": r["op_dt"].isoformat() if r["op_dt"] else None} for r in mun],
    }


def add_missing(source: str = "master_upc", status: str = "UPC", user: Optional[str] = None) -> Dict[str, Any]:
    """Bulk-add stores that aren't yet in the list:
       source='master_upc' → UPC stores in the master (default), 'master' → all master
       stores (optionally one status), 'mbq' → stores that have FA/CONS MBQ."""
    ensure_tables()
    with data_engine.begin() as c:
        if source == "mbq":
            rows = [str(r[0]) for r in c.execute(text(
                f"SELECT DISTINCT b.st_cd FROM {MBQ} b WHERE NOT EXISTS (SELECT 1 FROM {LIST} l WHERE l.st_cd=b.st_cd)")).fetchall()]
        else:
            stt = "UPC" if source == "master_upc" else (status if status and status.upper() != "ALL" else None)
            sfilt = "AND m.ST_STATUS=:stt" if stt else ""
            params = {"stt": stt} if stt else {}
            rows = [str(r[0]) for r in c.execute(text(
                f"SELECT m.ST_CD FROM {MASTER} m WHERE 1=1 {sfilt} "
                f"AND NOT EXISTS (SELECT 1 FROM {LIST} l WHERE l.st_cd=m.ST_CD)"), params).fetchall()]
        msmap = _master_status_map(c, rows)
        for sc in rows:
            c.execute(text(f"INSERT INTO {LIST} (st_cd, created_by) VALUES (:s,:u)"), {"s": sc, "u": user})
            _log(c, sc, "ADD", status_at=msmap.get(sc), in_master=(sc in msmap), remarks=f"bulk:{source}", user=user)
    return {"added": len(rows), "source": source}


def _master_status_map(conn, st_cds: List[str]) -> Dict[str, Optional[str]]:
    keys = sorted({s for s in st_cds if s})
    if not keys:
        return {}
    inlist = ", ".join(f":k{i}" for i in range(len(keys)))
    params = {f"k{i}": k for i, k in enumerate(keys)}
    rows = conn.execute(text(f"SELECT ST_CD, ST_STATUS FROM {MASTER} WHERE ST_CD IN ({inlist})"), params).fetchall()
    return {str(a): b for a, b in rows}


def add_store(st_cd: str, remarks: Optional[str] = None, user: Optional[str] = None) -> Dict[str, Any]:
    ensure_tables()
    sc = (st_cd or "").strip().upper()
    if not sc:
        raise ValueError("Store code is required")
    with data_engine.begin() as c:
        exists = c.execute(text(f"SELECT 1 FROM {LIST} WHERE st_cd=:s"), {"s": sc}).first()
        msmap = _master_status_map(c, [sc])
        c.execute(text(f"""
            MERGE {LIST} AS t USING (SELECT :s AS st_cd) AS s ON t.st_cd = s.st_cd
            WHEN MATCHED THEN UPDATE SET remarks=:r, updated_at=GETDATE()
            WHEN NOT MATCHED THEN INSERT (st_cd, remarks, created_by) VALUES (:s,:r,:u);"""),
            {"s": sc, "r": _clean(remarks), "u": user})
        if not exists:
            _log(c, sc, "ADD", status_at=msmap.get(sc), in_master=(sc in msmap), remarks=_clean(remarks), user=user)
    return {"st_cd": sc, "added": not exists}


def delete_store(row_id: int, user: Optional[str] = None) -> None:
    ensure_tables()
    with data_engine.begin() as c:
        row = c.execute(text(f"SELECT st_cd FROM {LIST} WHERE id=:i"), {"i": row_id}).mappings().first()
        c.execute(text(f"DELETE FROM {LIST} WHERE id=:i"), {"i": row_id})
        if row:
            msmap = _master_status_map(c, [row["st_cd"]])
            _log(c, row["st_cd"], "REMOVE", status_at=msmap.get(row["st_cd"]), in_master=(row["st_cd"] in msmap), user=user)


def ingest_upload(file_bytes: bytes, user: Optional[str] = None) -> Dict[str, Any]:
    """Upload a store list (single column ST_CD; optional REMARKS). Adds new stores,
    logs each ADD, and returns counts + the uploaded stores that are missing in master."""
    ensure_tables()
    df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
    df = df.rename(columns={c: str(c).strip().lower().replace(" ", "_") for c in df.columns})

    def pick(*names):
        for n in names:
            if n in df.columns:
                return n
        return None
    c_st = pick("st_cd", "stcd", "store", "store_code", "site_code", "werks")
    c_rmk = pick("remarks", "remark", "note", "notes")
    if not c_st:
        raise ValueError("Upload is missing the ST_CD column. Download the template.")

    codes = []
    for _, r in df.iterrows():
        sc = str(r.get(c_st) or "").strip().upper()
        if sc and sc not in ("NAN", "NONE"):
            codes.append((sc, _clean(r.get(c_rmk)) if c_rmk else None))
    added = existing = 0
    with data_engine.begin() as c:
        have = {str(a) for (a,) in c.execute(text(f"SELECT st_cd FROM {LIST}")).fetchall()}
        msmap = _master_status_map(c, [sc for sc, _ in codes])
        for sc, rmk in codes:
            if sc in have:
                existing += 1
                c.execute(text(f"UPDATE {LIST} SET remarks=COALESCE(:r, remarks), updated_at=GETDATE() WHERE st_cd=:s"),
                          {"r": rmk, "s": sc})
                continue
            c.execute(text(f"INSERT INTO {LIST} (st_cd, remarks, created_by) VALUES (:s,:r,:u)"),
                      {"s": sc, "r": rmk, "u": user})
            _log(c, sc, "ADD", status_at=msmap.get(sc), in_master=(sc in msmap), remarks=rmk, user=user)
            have.add(sc); added += 1
    missing = [sc for sc, _ in codes if sc not in msmap] if codes else []
    # de-dup preserve order
    seen = set(); missing = [x for x in missing if not (x in seen or seen.add(x))]
    return {"rows": len(codes), "added": added, "existing": existing, "missing_in_master": missing}


def list_history(st_cd: Optional[str] = None, limit: int = 300) -> List[Dict[str, Any]]:
    ensure_tables()
    where, params = "", {}
    if st_cd:
        where = "WHERE st_cd=:s"; params["s"] = st_cd.strip().upper()
    with data_engine.connect() as c:
        rows = c.execute(text(
            f"SELECT TOP {int(limit)} id, st_cd, action, status_at, in_master, remarks, changed_by, changed_at "
            f"FROM {HIST} {where} ORDER BY changed_at DESC, id DESC"), params).mappings().all()
    return [{"id": r["id"], "st_cd": r["st_cd"], "action": r["action"], "status_at": r["status_at"],
             "in_master": bool(r["in_master"]) if r["in_master"] is not None else None,
             "remarks": r["remarks"], "changed_by": r["changed_by"],
             "changed_at": r["changed_at"].isoformat() if r["changed_at"] else None} for r in rows]


def template_bytes() -> bytes:
    sample = pd.DataFrame([{"ST_CD": "ZZ01", "REMARKS": "new UPC store"},
                           {"ST_CD": "ZZ02", "REMARKS": ""}], columns=["ST_CD", "REMARKS"])
    notes = pd.DataFrame([
        ["ST_CD", "Yes", "Store code. Validated against the store master; UPC/OLD status is read from the master."],
        ["REMARKS", "No", "Optional note."],
        ["", "", ""],
        ["Status", "", "UPC = still a project store, OLD = opened/running. Derived from the master — opens auto-convert UPC→OLD."],
        ["Priority", "", "Allocation priority = opening date, older store first."],
    ], columns=["Column", "Required", "Description"])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        sample.to_excel(w, index=False, sheet_name="Upload")
        notes.to_excel(w, index=False, sheet_name="Instructions")
        for sh, widths in (("Upload", [12, 26]), ("Instructions", [16, 12, 96])):
            ws = w.sheets[sh]
            for i, width in enumerate(widths):
                ws.column_dimensions[chr(65 + i)].width = width
    return buf.getvalue()
