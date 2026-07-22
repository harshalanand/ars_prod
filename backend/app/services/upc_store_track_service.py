"""
UPC Store Tracking service — store-opening lifecycle tracker.

Replaces the manual "STORE OPENING DATES *.xlsx" workbook with a dynamic module.

What is PERSISTED (three tables, see scripts/024_upc_store_tracking.sql):
  • ARS_UPC_STORE_TRACK              — one row/store: current tracking state +
                                       derived counters (dates given/changed,
                                       first/latest dates, remarks count).
  • ARS_UPC_STORE_TRACK_DATE_HIST    — immutable event per proposed-date/share.
  • ARS_UPC_STORE_TRACK_REMARK_HIST  — immutable event per remark change.

What is joined LIVE at read time (never copied):
  • Identity   ← Master_ALC_INPUT_ST_MASTER  (ST_NM, RDC, HUB, ST_STATUS, OP_DT,
                                               MANUAL_ST_PRIORITY)
  • Metrics    ← ARS_GRID_MJ aggregated per store (SUM over all MAJ_CAT):
                 SUM(MBQ) = MBQ 100 %, SUM(STK_TTL) = total stock,
                 SUM(DISP_Q) = display, fill-rate = total stock ÷ MBQ, and the
                 SLOC-wise stock columns (0001/0002/0004/0006/0099/0017,
                 HUB_INTRA, HUB_PRD_Q, V06/V07, PTL, STO, PEND_ALC). These grid
                 columns map directly to the manual workbook's SLOC-wise block.

The user uploads/edits only ST_CD + proposed opening date + share date
(+ optional remarks / layout). Everything else is derived or joined.

History is EVENT-BASED: each submission appends a *_HIST row, so
"how many times a date was given / changed" and "last remark / remark change
count" are auditable, not manually dragged.
"""
from __future__ import annotations

import io
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger
from sqlalchemy import text

from app.database.session import data_engine

HEAD   = "ARS_UPC_STORE_TRACK"
DHIST  = "ARS_UPC_STORE_TRACK_DATE_HIST"
RHIST  = "ARS_UPC_STORE_TRACK_REMARK_HIST"
SHIST  = "ARS_UPC_STORE_TRACK_STATUS_HIST"
MASTER = "Master_ALC_INPUT_ST_MASTER"
GRID   = "ARS_GRID_MJ"
MPROD  = "VW_MASTER_PRODUCT"     # MAJ_CAT → SEG (APP/GM/…) authoritative view

# Lifecycle status. ACTIVE = still being tracked (default when unset).
VALID_STATUS = {"ACTIVE", "OPENED", "HOLD", "CANCELLED"}

# Segments (from MASTER_PRODUCT.SEG). MBQ / stock / SLOC are computed for the
# selected segments only — default APP + GM. The B-prefixed grid categories that
# aren't in MASTER_PRODUCT are simply left out of the segment filter (as-is).
VALID_SEG    = ("APP", "GM", "MKT", "ACC", "NT", "FAB", "NA")
DEFAULT_SEG  = ("APP", "GM")


def _clean_segments(segments) -> tuple:
    segs = tuple(s for s in (segments or ()) if s in VALID_SEG)
    return segs or DEFAULT_SEG

# ARS_GRID_MJ columns surfaced as the SLOC-wise / stock breakdown, SUM-aggregated
# per store (over all MAJ_CAT). Left = output key, right = grid column
# (bracket-quoted in SQL because several start with a digit). These map directly
# to the manual workbook's SLOC-wise columns.
SLOC_COLS = {
    "STK_0001":       "0001",
    "STK_0002":       "0002",
    "STK_0004":       "0004",
    "STK_0006":       "0006",
    "INT_0099":       "0099",
    "STK_0017":       "0017",
    "HUB_INTRA":      "HUB_INTRA",
    "HUB_PRD_Q":      "HUB_PRD_Q",
    "ST_STK_V06_QTY": "ST_STK_V06_QTY",
    "ST_STK_V07_QTY": "ST_STK_V07_QTY",
    "DH24_PTL_V07_Q": "DH24_PTL_V07_Q",
    "DH24_PTL_V18_Q": "DH24_PTL_V18_Q",
    "DH24_PTL_V25_Q": "DH24_PTL_V25_Q",
    "DW01_PTL_V07_Q": "DW01_PTL_V07_Q",
    "STO_DH24":       "DH24_STO_QTY_Q",
    "STO_DW01":       "DW01_STO_QTY_Q",
    "PND":            "PEND_ALC",
}


# ══════════════════════════════════════════════════════════════════════════════
# Provisioning
# ══════════════════════════════════════════════════════════════════════════════
def ensure_tables() -> None:
    """Lazily create the three tracking tables (mirrors scripts/024)."""
    ddl = [
        f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{HEAD}')
        CREATE TABLE {HEAD} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            st_cd NVARCHAR(20) NOT NULL,
            first_proposed_dt DATE NULL,
            latest_proposed_dt DATE NULL,
            first_share_dt DATE NULL,
            latest_share_dt DATE NULL,
            date_given_count INT NOT NULL DEFAULT 0,
            date_change_count INT NOT NULL DEFAULT 0,
            layout_generated BIT NOT NULL DEFAULT 0,
            layout_rec_dt DATE NULL,
            display_generated BIT NOT NULL DEFAULT 0,
            first_disp_dt DATE NULL,
            actual_open_dt DATE NULL,
            status NVARCHAR(20) NULL,
            status_priority NVARCHAR(40) NULL,
            last_remarks NVARCHAR(MAX) NULL,
            remarks_change_count INT NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT GETDATE(),
            updated_at DATETIME NOT NULL DEFAULT GETDATE(),
            CONSTRAINT UQ_{HEAD} UNIQUE (st_cd)
        );""",
        f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{DHIST}')
        CREATE TABLE {DHIST} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            st_cd NVARCHAR(20) NOT NULL,
            proposed_opening_dt DATE NULL,
            share_dt DATE NULL,
            changed BIT NOT NULL DEFAULT 0,
            prev_proposed_dt DATE NULL,
            source NVARCHAR(20) NOT NULL DEFAULT 'upload',
            note NVARCHAR(400) NULL,
            changed_by NVARCHAR(100) NULL,
            changed_at DATETIME NOT NULL DEFAULT GETDATE()
        );""",
        f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{RHIST}')
        CREATE TABLE {RHIST} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            st_cd NVARCHAR(20) NOT NULL,
            remarks NVARCHAR(MAX) NULL,
            prev_remarks NVARCHAR(MAX) NULL,
            source NVARCHAR(20) NOT NULL DEFAULT 'edit',
            changed_by NVARCHAR(100) NULL,
            changed_at DATETIME NOT NULL DEFAULT GETDATE()
        );""",
        f"""IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{SHIST}')
        CREATE TABLE {SHIST} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            st_cd NVARCHAR(20) NOT NULL,
            status NVARCHAR(20) NULL,
            prev_status NVARCHAR(20) NULL,
            source NVARCHAR(20) NOT NULL DEFAULT 'edit',
            changed_by NVARCHAR(100) NULL,
            changed_at DATETIME NOT NULL DEFAULT GETDATE()
        );""",
    ]
    with data_engine.begin() as c:
        for stmt in ddl:
            c.execute(text(stmt))
        # add columns to tables that pre-existed without them
        c.execute(text(
            f"IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
            f"WHERE TABLE_NAME='{HEAD}' AND COLUMN_NAME='status') "
            f"ALTER TABLE {HEAD} ADD status NVARCHAR(20) NULL;"))
        c.execute(text(
            f"IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
            f"WHERE TABLE_NAME='{HEAD}' AND COLUMN_NAME='display_generated') "
            f"ALTER TABLE {HEAD} ADD display_generated BIT NOT NULL DEFAULT 0;"))


# ══════════════════════════════════════════════════════════════════════════════
# Small helpers
# ══════════════════════════════════════════════════════════════════════════════
def _to_date(v: Any) -> Optional[date]:
    if v is None or v == "" or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    # ISO strings from the API/date-inputs must NOT be parsed dayfirst (that
    # would flip 2026-07-01 → 7 Jan). Try ISO first, then fall back to the
    # Indian DD-MM-YYYY that hand-typed / Excel-text dates use.
    try:
        return datetime.fromisoformat(s[:10]).date()
    except Exception:
        pass
    ts = pd.to_datetime(s, dayfirst=True, errors="coerce")
    return None if pd.isna(ts) else ts.date()


def _iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


def _clean_text(v: Any) -> Optional[str]:
    """Return trimmed text, or None for blanks and NaN-like junk ('nan', 'none',
    'nat') that pandas/Excel produce for empty cells."""
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
# Event recorders (called inside a transaction)
# ══════════════════════════════════════════════════════════════════════════════
def _get_head(conn, st_cd: str) -> Optional[dict]:
    row = conn.execute(text(f"SELECT * FROM {HEAD} WHERE st_cd=:s"), {"s": st_cd}).mappings().first()
    return dict(row) if row else None


def _record_date_event(conn, st_cd: str, proposed_dt: Optional[date],
                       share_dt: Optional[date], source: str,
                       user: Optional[str], note: Optional[str] = None) -> dict:
    """Append a date event and roll the head counters. Returns
    {'changed': proposed value differs from prev latest,
     'recorded': a history row was written (baseline or change)}."""
    if proposed_dt is None and share_dt is None:
        return {"changed": False, "recorded": False}
    head = _get_head(conn, st_cd)
    prev = head["latest_proposed_dt"] if head else None
    is_first = head is None
    # The first date ever given is the baseline, not a "change". A change is only
    # a later submission whose value differs from the previous latest.
    changed = proposed_dt is not None and prev is not None and proposed_dt != prev

    # Record a history row ONLY for the baseline (first) or a genuine change —
    # re-sharing the same date does not add a history entry (just bumps counts).
    if is_first or changed:
        conn.execute(text(f"""
            INSERT INTO {DHIST} (st_cd, proposed_opening_dt, share_dt, changed,
                                 prev_proposed_dt, source, note, changed_by)
            VALUES (:s,:p,:sh,:ch,:pv,:src,:note,:usr)"""),
            {"s": st_cd, "p": proposed_dt, "sh": share_dt, "ch": 1 if changed else 0,
             "pv": prev, "src": source, "note": note, "usr": user})

    if head is None:
        conn.execute(text(f"""
            INSERT INTO {HEAD} (st_cd, first_proposed_dt, latest_proposed_dt,
                                first_share_dt, latest_share_dt,
                                date_given_count, date_change_count)
            VALUES (:s,:p,:p,:sh,:sh,:gc,0)"""),
            {"s": st_cd, "p": proposed_dt, "sh": share_dt,
             "gc": 1 if proposed_dt is not None else 0})
    else:
        # latest_share_dt tracks the share date of the CURRENT proposed value, so it
        # only advances on the baseline or a genuine change — an unchanged re-share
        # still counts toward date_given_count but must NOT move the share date.
        conn.execute(text(f"""
            UPDATE {HEAD} SET
                first_proposed_dt = COALESCE(first_proposed_dt, :p),
                latest_proposed_dt = CASE WHEN :chinc=1 AND :p IS NOT NULL THEN :p ELSE latest_proposed_dt END,
                first_share_dt = COALESCE(first_share_dt, :sh),
                latest_share_dt = CASE WHEN :chinc=1 AND :sh IS NOT NULL THEN :sh ELSE latest_share_dt END,
                date_given_count = date_given_count + CASE WHEN :p IS NOT NULL THEN 1 ELSE 0 END,
                date_change_count = date_change_count + :chinc,
                updated_at = GETDATE()
            WHERE st_cd = :s"""),
            {"s": st_cd, "p": proposed_dt, "sh": share_dt,
             "chinc": 1 if changed else 0})
    return {"changed": changed, "recorded": (is_first or changed) and proposed_dt is not None}


def _record_remark_event(conn, st_cd: str, remarks: Optional[str],
                         source: str, user: Optional[str]) -> bool:
    """Append a remark event only when the text actually changes. Ensures a head
    row exists. Returns True if recorded."""
    remarks = _clean_text(remarks)
    if remarks is None:
        return False
    head = _get_head(conn, st_cd)
    prev = _clean_text((head or {}).get("last_remarks"))
    if prev is not None and prev == remarks:
        return False

    if head is None:
        conn.execute(text(f"INSERT INTO {HEAD} (st_cd) VALUES (:s)"), {"s": st_cd})

    conn.execute(text(f"""
        INSERT INTO {RHIST} (st_cd, remarks, prev_remarks, source, changed_by)
        VALUES (:s,:r,:pv,:src,:usr)"""),
        {"s": st_cd, "r": remarks, "pv": prev, "src": source, "usr": user})
    conn.execute(text(f"""
        UPDATE {HEAD} SET last_remarks=:r,
               remarks_change_count = remarks_change_count + 1,
               updated_at = GETDATE()
        WHERE st_cd=:s"""), {"s": st_cd, "r": remarks})
    return True


def _record_status_event(conn, st_cd: str, new_status: Any, user: Optional[str],
                         source: str = "edit") -> bool:
    """Change lifecycle status and append a STATUS_HIST row (who/when, from→to)
    only when it actually changes. Returns True if recorded."""
    if new_status is None:
        return False
    new_status = str(new_status).strip().upper()
    if new_status not in VALID_STATUS:
        return False
    head = _get_head(conn, st_cd)
    prev = (head or {}).get("status")
    prev_eff = prev if prev in VALID_STATUS else ("OPENED" if (head or {}).get("actual_open_dt") else "ACTIVE")
    if head is None:
        conn.execute(text(f"INSERT INTO {HEAD} (st_cd) VALUES (:s)"), {"s": st_cd})
    if new_status == prev_eff:
        # effective status unchanged — just persist the value, no history noise
        conn.execute(text(f"UPDATE {HEAD} SET status=:st WHERE st_cd=:s"), {"st": new_status, "s": st_cd})
        return False
    conn.execute(text(f"""
        INSERT INTO {SHIST} (st_cd, status, prev_status, source, changed_by)
        VALUES (:s,:st,:pv,:src,:usr)"""),
        {"s": st_cd, "st": new_status, "pv": prev_eff, "src": source, "usr": user})
    conn.execute(text(f"UPDATE {HEAD} SET status=:st, updated_at=GETDATE() WHERE st_cd=:s"),
                 {"st": new_status, "s": st_cd})
    return True


def _apply_layout_outcome(conn, st_cd: str, fields: Dict[str, Any]) -> None:
    """Set layout / display / actual-open / priority fields (no history).
    (Status is handled separately by _record_status_event.)

    Convention: a field value of None = "not provided, leave as-is"; an empty
    string = "clear to NULL". This lets the Edit form clear a date/priority while
    inline edits (which omit the field → None) never touch it."""
    updates: Dict[str, Any] = {}
    for col in ("layout_rec_dt", "first_disp_dt", "actual_open_dt"):
        raw = fields.get(col)
        if raw is None:
            continue                                   # untouched
        updates[col] = None if str(raw).strip() == "" else _to_date(raw)  # '' clears
    if fields.get("layout_generated") is not None:
        updates["layout_generated"] = 1 if fields["layout_generated"] else 0
    if fields.get("display_generated") is not None:
        updates["display_generated"] = 1 if fields["display_generated"] else 0
    # status_priority handled separately by _apply_priority (writes the store master)
    if not updates:
        return
    if _get_head(conn, st_cd) is None:
        conn.execute(text(f"INSERT INTO {HEAD} (st_cd) VALUES (:s)"), {"s": st_cd})
    assigns = ", ".join(f"{k}=:{k}" for k in updates)
    updates["s"] = st_cd
    conn.execute(text(f"UPDATE {HEAD} SET {assigns}, updated_at=GETDATE() WHERE st_cd=:s"), updates)


def _apply_priority(conn, st_cd: str, raw: Any) -> None:
    """Store priority — the store master (Master_ALC_INPUT_ST_MASTER.
    MANUAL_ST_PRIORITY, INT) is the single source of truth. Editing priority in
    UPC writes it there (so allocation/ranking see it) AND mirrors a copy into the
    tracker head. Reading is done live from the master. '' clears to NULL; a
    non-integer is ignored."""
    s = str(raw).strip()
    if s == "":
        prio = None
    else:
        try:
            prio = int(float(s))
        except (TypeError, ValueError):
            return
    # 1) source of truth: the store master (no-op if the store isn't in it)
    conn.execute(text(f"UPDATE {MASTER} SET MANUAL_ST_PRIORITY=:p WHERE ST_CD=:s"),
                 {"p": prio, "s": st_cd})
    # 2) mirror into the tracker for the module's own record / export
    if _get_head(conn, st_cd) is None:
        conn.execute(text(f"INSERT INTO {HEAD} (st_cd) VALUES (:s)"), {"s": st_cd})
    conn.execute(text(f"UPDATE {HEAD} SET status_priority=:sp, updated_at=GETDATE() WHERE st_cd=:s"),
                 {"sp": (str(prio) if prio is not None else None), "s": st_cd})


# ══════════════════════════════════════════════════════════════════════════════
# Write ops
# ══════════════════════════════════════════════════════════════════════════════
def save_store(st_cd: str, proposed_opening_dt: Any = None, share_dt: Any = None,
               remarks: Optional[str] = None, source: str = "edit",
               user: Optional[str] = None, note: Optional[str] = None,
               **outcome) -> Dict[str, Any]:
    """Create/update one store's tracking. Appends history where values change."""
    st_cd = (st_cd or "").strip().upper()
    if not st_cd:
        raise ValueError("st_cd is required")
    ensure_tables()
    p = _to_date(proposed_opening_dt)
    sh = _to_date(share_dt)
    reactivated = False
    with data_engine.begin() as c:
        dres = _record_date_event(c, st_cd, p, sh, source, user, note)
        remark_changed = _record_remark_event(c, st_cd, remarks, source, user)
        status_changed = _record_status_event(c, st_cd, outcome.get("status"), user, source)
        _apply_layout_outcome(c, st_cd, outcome)
        if outcome.get("status_priority") is not None:
            _apply_priority(c, st_cd, outcome["status_priority"])
        # Auto-reactivate: a freshly given/changed proposed date revives a dormant
        # store (CANCELLED / HOLD / unset) back to ACTIVE — unless this same call
        # explicitly set a status, or the store is already OPENED.
        if dres["recorded"] and outcome.get("status") is None:
            cur = (_get_head(c, st_cd) or {}).get("status")
            if cur not in ("ACTIVE", "OPENED"):
                reactivated = _record_status_event(c, st_cd, "ACTIVE", user, "auto-date")
    return {"st_cd": st_cd, "date_changed": dres["changed"],
            "remark_changed": remark_changed, "status_changed": status_changed,
            "reactivated": reactivated}


def ingest_upload(file_bytes: bytes, user: Optional[str] = None) -> Dict[str, Any]:
    """Ingest an uploaded workbook. ALL three columns are mandatory
    (case/space-insensitive): ST_CD, PROPOSED_OPENING_DATE (/ OP_DATE / BGT_OP_DT),
    SHARE_DATE (/ DATE_OF_SHARING). Any row missing one is rejected. Remarks are
    NOT ingested — they are added in the UI after review."""
    ensure_tables()
    df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
    # normalise headers → snake keys
    norm = {c: str(c).strip().lower().replace(" ", "_").replace("-", "_").replace("\n", "_")
            for c in df.columns}
    df = df.rename(columns=norm)

    def pick(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    c_st  = pick("st_cd", "stcd", "store", "store_code", "site_code")
    c_op  = pick("proposed_opening_date", "proposed_opening_dt", "op_date",
                 "opening_date", "bgt_op_dt", "latest_bgt_op_dt", "proposed_date")
    c_sh  = pick("share_date", "date_of_sharing", "share_dt", "shared_on",
                 "date_sharing", "1st_date_share")
    missing_cols = [name for name, col in
                    (("ST_CD", c_st), ("PROPOSED_OPENING_DATE", c_op), ("SHARE_DATE", c_sh))
                    if not col]
    if missing_cols:
        raise ValueError("Upload is missing mandatory column(s): " + ", ".join(missing_cols)
                         + ". Download the template.")

    processed = changed = created = skipped = 0
    errors: List[str] = []
    for i, r in df.iterrows():
        st_cd = str(r.get(c_st) or "").strip().upper()
        if not st_cd or st_cd in ("NAN", "NONE"):
            skipped += 1
            continue
        # all three columns are mandatory per row
        p, sh = _to_date(r.get(c_op)), _to_date(r.get(c_sh))
        row_missing = [n for n, v in (("PROPOSED_OPENING_DATE", p), ("SHARE_DATE", sh)) if v is None]
        if row_missing:
            errors.append(f"row {int(i) + 2} ({st_cd}): missing {', '.join(row_missing)}")
            continue
        try:
            existed = get_head_exists(st_cd)
            res = save_store(st_cd, proposed_opening_dt=p, share_dt=sh,
                             source="upload", user=user)
            processed += 1
            if not existed:
                created += 1
            if res["date_changed"]:
                changed += 1
        except Exception as e:
            errors.append(f"{st_cd}: {e}")
    return {"rows": int(len(df)), "processed": processed, "created": created,
            "changed": changed, "skipped": skipped,
            "errors": errors[:50], "error_count": len(errors)}


def get_head_exists(st_cd: str) -> bool:
    with data_engine.connect() as c:
        return c.execute(text(f"SELECT 1 FROM {HEAD} WHERE st_cd=:s"),
                         {"s": st_cd.strip().upper()}).first() is not None


def delete_store(st_cd: str) -> None:
    st_cd = st_cd.strip().upper()
    with data_engine.begin() as c:
        for t in (RHIST, DHIST, SHIST, HEAD):
            c.execute(text(f"DELETE FROM {t} WHERE st_cd=:s"), {"s": st_cd})


def compact_history() -> Dict[str, Any]:
    """Clean up date history built under the old rule: for each store keep only
    the baseline (first) row + rows where the proposed date genuinely changed,
    deleting redundant same-date re-shares. Re-flags `changed`/`prev_proposed_dt`
    and recomputes `date_change_count` on the head. `date_given_count` (how many
    times a date was shared) is preserved. Idempotent."""
    ensure_tables()
    removed = stores = 0
    with data_engine.begin() as c:
        st_cds = [r[0] for r in c.execute(text(f"SELECT DISTINCT st_cd FROM {DHIST}")).fetchall()]
        for st in st_cds:
            rows = c.execute(text(
                f"SELECT id, proposed_opening_dt FROM {DHIST} WHERE st_cd=:s "
                f"ORDER BY changed_at, id"), {"s": st}).fetchall()
            prev = None
            first_seen = False
            changes = 0
            for rid, pod in rows:
                if not first_seen:
                    first_seen = True
                    c.execute(text(f"UPDATE {DHIST} SET changed=0, prev_proposed_dt=NULL WHERE id=:i"), {"i": rid})
                    prev = pod
                    continue
                is_change = pod is not None and prev is not None and pod != prev
                if is_change:
                    c.execute(text(f"UPDATE {DHIST} SET changed=1, prev_proposed_dt=:p WHERE id=:i"),
                              {"p": prev, "i": rid})
                    prev = pod
                    changes += 1
                else:
                    c.execute(text(f"DELETE FROM {DHIST} WHERE id=:i"), {"i": rid})
                    removed += 1
            # recompute first/latest proposed + share dates from the surviving rows,
            # so a re-share that wrongly advanced latest_share_dt is corrected.
            surv = c.execute(text(
                f"SELECT proposed_opening_dt, share_dt FROM {DHIST} WHERE st_cd=:s "
                f"ORDER BY changed_at, id"), {"s": st}).fetchall()
            if surv:
                fp, fsh = surv[0]
                lp = next((p for p, _ in reversed(surv) if p is not None), fp)
                lsh = next((sh for _, sh in reversed(surv) if sh is not None), fsh)
                c.execute(text(f"""UPDATE {HEAD} SET date_change_count=:n,
                        first_proposed_dt=:fp, latest_proposed_dt=:lp,
                        first_share_dt=:fsh, latest_share_dt=:lsh, updated_at=GETDATE()
                        WHERE st_cd=:s"""),
                        {"n": changes, "fp": fp, "lp": lp, "fsh": fsh, "lsh": lsh, "s": st})
            else:
                c.execute(text(f"UPDATE {HEAD} SET date_change_count=:n, updated_at=GETDATE() WHERE st_cd=:s"),
                          {"n": changes, "s": st})
            stores += 1

        # scrub NaN-like junk remarks ("nan"/"none"/"") left by earlier uploads
        bad_remarks = 0
        rrows = c.execute(text(f"SELECT id, remarks FROM {RHIST}")).fetchall()
        for rid, rm in rrows:
            if _clean_text(rm) is None:
                c.execute(text(f"DELETE FROM {RHIST} WHERE id=:i"), {"i": rid})
                bad_remarks += 1
        # recompute last_remarks + remarks_change_count from the surviving rows
        for st in [r[0] for r in c.execute(text(f"SELECT DISTINCT st_cd FROM {HEAD}")).fetchall()]:
            rr = c.execute(text(f"SELECT remarks FROM {RHIST} WHERE st_cd=:s ORDER BY changed_at, id"),
                           {"s": st}).fetchall()
            last = rr[-1][0] if rr else None
            c.execute(text(f"UPDATE {HEAD} SET last_remarks=:r, remarks_change_count=:n WHERE st_cd=:s"),
                      {"r": last, "n": len(rr), "s": st})
    return {"stores": stores, "removed_rows": removed, "removed_remarks": bad_remarks}


# ══════════════════════════════════════════════════════════════════════════════
# Read ops (live joins + derived)
# ══════════════════════════════════════════════════════════════════════════════
def _grid_agg_cte(segments=DEFAULT_SEG) -> str:
    """Per-store metrics = SUM over grid rows whose MAJ_CAT falls in the selected
    segments (APP/GM by default), via the MASTER_PRODUCT MAJ_CAT→SEG map."""
    segs = _clean_segments(segments)
    inlist = ", ".join(f"'{s}'" for s in segs)   # segs validated → safe to inline
    sloc = ", ".join(f"SUM(g.[{src}]) AS trend_{key}" for key, src in SLOC_COLS.items())
    return f"""
    WITH seg_cats AS (
        SELECT DISTINCT MAJ_CAT FROM {MPROD} WHERE SEG IN ({inlist})
    ),
    grid_agg AS (
        SELECT g.WERKS AS ST_CD,
               SUM(g.MBQ)      AS mbq_100,
               SUM(g.STK_TTL)  AS total_stock,
               SUM(g.DISP_Q)   AS disp_q,
               {sloc}
        FROM {GRID} g
        JOIN seg_cats sc ON sc.MAJ_CAT = g.MAJ_CAT
        GROUP BY g.WERKS
    )"""


def _derive(row: dict) -> dict:
    """Compute delay / balance-days / delayed flag for one joined row."""
    fp = row.get("first_proposed_dt")
    lp = row.get("latest_proposed_dt")
    ao = row.get("actual_open_dt")
    # effective status: explicit status wins; else OPENED if an actual-open date
    # exists; else ACTIVE.
    status = (row.get("status") or "").strip().upper()
    if status not in VALID_STATUS:
        status = "OPENED" if ao else "ACTIVE"
    today = date.today()
    total_delay = (lp - fp).days if (fp and lp) else None
    if status in ("OPENED", "CANCELLED"):
        bal_days = 0 if status == "OPENED" else None
        delayed = False
    elif lp:
        bal_days = (lp - today).days
        # HOLD stores are paused — not counted as delayed
        delayed = bal_days < 0 and status == "ACTIVE"
    else:
        bal_days, delayed = None, False
    mbq = _num(row.get("mbq_100"))
    stk = _num(row.get("total_stock"))
    fr = _num(row.get("fill_rate"))
    if fr is None and mbq and stk is not None:
        fr = round(stk / mbq, 4) if mbq else None
    return {
        "status": status,
        "total_delay_days": total_delay,
        "bal_days": bal_days,
        "delayed": delayed,
        "mbq_100": mbq,
        "mbq_110": round(mbq * 1.10, 3) if mbq is not None else None,
        "total_stock": stk,
        "fill_rate": fr,
        "fill_rate_pct": round(fr * 100, 2) if fr is not None else None,
    }


def list_stores(segments=DEFAULT_SEG) -> List[Dict[str, Any]]:
    ensure_tables()
    sloc_sel = ", ".join(f"g.trend_{key}" for key in SLOC_COLS)
    sql = _grid_agg_cte(segments) + f"""
        SELECT h.*,
               m.ST_NM AS site_name, m.RDC AS rdc, m.HUB AS hub,
               m.ST_STATUS AS st_status, m.OP_DT AS master_op_dt,
               m.MANUAL_ST_PRIORITY AS master_priority,
               g.mbq_100, g.disp_q, g.total_stock,
               CAST(NULL AS DATE) AS metric_dt, {sloc_sel},
               (SELECT TOP 1 d.prev_proposed_dt FROM {DHIST} d
                WHERE d.st_cd = h.st_cd AND d.changed = 1
                ORDER BY d.changed_at DESC, d.id DESC) AS prev_proposed_dt
        FROM {HEAD} h
        LEFT JOIN {MASTER} m ON m.ST_CD = h.st_cd
        LEFT JOIN grid_agg g ON g.ST_CD = h.st_cd
        ORDER BY h.updated_at DESC"""
    out = []
    with data_engine.connect() as c:
        rows = c.execute(text(sql)).mappings().all()
        # lightweight date history per store — lets the UI draw the per-store
        # date-change chart on hover without a second round-trip.
        dh = c.execute(text(f"""SELECT st_cd, proposed_opening_dt, share_dt, changed_at
                    FROM {DHIST} ORDER BY st_cd, changed_at, id""")).fetchall()
    hist: Dict[str, list] = {}
    for st, pod, sh, cat in dh:
        hist.setdefault(st, []).append({
            "proposed_opening_dt": _iso(pod), "share_dt": _iso(sh),
            "changed_at": cat.isoformat() if cat else None})
    for r in rows:
        r = dict(r)
        d = _derive(r)
        sloc = {key: _num(r.get(f"trend_{key}")) for key in SLOC_COLS}
        out.append({
            "st_cd": r["st_cd"],
            "site_name": r.get("site_name"),
            "rdc": r.get("rdc"), "hub": r.get("hub"),
            "st_status": r.get("st_status"),
            # master (MANUAL_ST_PRIORITY) is the source of truth; tracker mirror is
            # only a fallback for a store not present in the master.
            "status_priority": (r.get("master_priority") if r.get("master_priority") is not None
                                else r.get("status_priority")),
            "first_proposed_dt": _iso(r.get("first_proposed_dt")),
            "latest_proposed_dt": _iso(r.get("latest_proposed_dt")),
            "prev_proposed_dt": _iso(r.get("prev_proposed_dt")),   # date before the latest change
            "first_share_dt": _iso(r.get("first_share_dt")),
            "latest_share_dt": _iso(r.get("latest_share_dt")),
            "date_given_count": r.get("date_given_count"),
            "date_change_count": r.get("date_change_count"),
            "layout_generated": bool(r.get("layout_generated")),
            "layout_rec_dt": _iso(r.get("layout_rec_dt")),
            "display_generated": bool(r.get("display_generated")),
            "first_disp_dt": _iso(r.get("first_disp_dt")),
            "actual_open_dt": _iso(r.get("actual_open_dt")),
            "last_remarks": r.get("last_remarks"),
            "remarks_change_count": r.get("remarks_change_count"),
            "disp_q": _num(r.get("disp_q")),
            "metric_dt": _iso(r.get("metric_dt")),
            "sloc": sloc,
            "date_history": hist.get(r["st_cd"], []),
            **d,
        })
    return out


def get_store(st_cd: str) -> Dict[str, Any]:
    st_cd = st_cd.strip().upper()
    stores = [s for s in list_stores() if s["st_cd"] == st_cd]
    if not stores:
        raise KeyError(st_cd)
    head = stores[0]
    with data_engine.connect() as c:
        dh = c.execute(text(f"""SELECT proposed_opening_dt, share_dt, changed,
                    prev_proposed_dt, source, note, changed_by, changed_at
                    FROM {DHIST} WHERE st_cd=:s ORDER BY changed_at"""),
                    {"s": st_cd}).mappings().all()
        rh = c.execute(text(f"""SELECT remarks, prev_remarks, source, changed_by, changed_at
                    FROM {RHIST} WHERE st_cd=:s ORDER BY changed_at"""),
                    {"s": st_cd}).mappings().all()
        sh_rows = c.execute(text(f"""SELECT status, prev_status, source, changed_by, changed_at
                    FROM {SHIST} WHERE st_cd=:s ORDER BY changed_at"""),
                    {"s": st_cd}).mappings().all()
    head["date_history"] = [{
        "proposed_opening_dt": _iso(x["proposed_opening_dt"]),
        "share_dt": _iso(x["share_dt"]),
        "changed": bool(x["changed"]),
        "prev_proposed_dt": _iso(x["prev_proposed_dt"]),
        "source": x["source"], "note": x["note"], "changed_by": x["changed_by"],
        "changed_at": x["changed_at"].isoformat() if x["changed_at"] else None,
    } for x in dh]
    head["remark_history"] = [{
        "remarks": x["remarks"], "prev_remarks": x["prev_remarks"],
        "source": x["source"], "changed_by": x["changed_by"],
        "changed_at": x["changed_at"].isoformat() if x["changed_at"] else None,
    } for x in rh]
    head["status_history"] = [{
        "status": x["status"], "prev_status": x["prev_status"],
        "source": x["source"], "changed_by": x["changed_by"],
        "changed_at": x["changed_at"].isoformat() if x["changed_at"] else None,
    } for x in sh_rows]
    return head


# ══════════════════════════════════════════════════════════════════════════════
# Charts + export
# ══════════════════════════════════════════════════════════════════════════════
def charts(segments=DEFAULT_SEG) -> Dict[str, Any]:
    stores = list_stores(segments)

    # 1) date changes over time — each date carries the stores that changed then
    with data_engine.connect() as c:
        rows = c.execute(text(f"""
            SELECT CAST(COALESCE(share_dt, CAST(changed_at AS DATE)) AS DATE) AS d, st_cd
            FROM {DHIST} WHERE changed=1
            ORDER BY d""")).all()
    from collections import OrderedDict
    bydate: "OrderedDict[str, list]" = OrderedDict()
    for d, st in rows:
        bydate.setdefault(_iso(d), []).append(st)
    date_changes = [{"date": k, "changes": len(v), "stores": sorted(set(v))} for k, v in bydate.items()]

    # 2) on-track vs delayed buckets (balance days)
    buckets = {"delayed": 0, "0-7": 0, "8-30": 0, "30+": 0, "opened": 0, "no_date": 0}
    for s in stores:
        if s["actual_open_dt"]:
            buckets["opened"] += 1
        elif s["bal_days"] is None:
            buckets["no_date"] += 1
        elif s["bal_days"] < 0:
            buckets["delayed"] += 1
        elif s["bal_days"] <= 7:
            buckets["0-7"] += 1
        elif s["bal_days"] <= 30:
            buckets["8-30"] += 1
        else:
            buckets["30+"] += 1
    bal_buckets = [{"bucket": k, "count": v} for k, v in buckets.items()]

    # 3) first-given vs latest-budgeted per store (slippage)
    slippage = sorted(
        [{"st_cd": s["st_cd"], "site_name": s["site_name"],
          "first_proposed_dt": s["first_proposed_dt"],
          "latest_proposed_dt": s["latest_proposed_dt"],
          "total_delay_days": s["total_delay_days"]}
         for s in stores if s["total_delay_days"] is not None],
        key=lambda x: (x["total_delay_days"] or 0), reverse=True)[:25]

    # 4) given-days-count distribution
    dist: Dict[int, int] = {}
    for s in stores:
        k = s["date_given_count"] or 0
        dist[k] = dist.get(k, 0) + 1
    given_dist = [{"given_count": k, "stores": dist[k]} for k in sorted(dist)]

    # 5) status distribution
    sc: Dict[str, int] = {}
    for s in stores:
        st = s.get("status") or "ACTIVE"
        sc[st] = sc.get(st, 0) + 1
    status_dist = [{"status": k, "count": sc[k]} for k in ("ACTIVE", "OPENED", "HOLD", "CANCELLED") if k in sc]

    # 6) stores by opening month (from latest budgeted date)
    mc: Dict[str, int] = {}
    for s in stores:
        mk = (s["latest_proposed_dt"] or "")[:7]     # YYYY-MM
        if mk:
            mc[mk] = mc.get(mk, 0) + 1
    month_dist = [{"month": k, "count": mc[k]} for k in sorted(mc)]

    return {
        "total_stores": len(stores),
        "delayed_stores": buckets["delayed"],
        "opened_stores": buckets["opened"],
        "date_changes": date_changes,
        "bal_buckets": bal_buckets,
        "slippage": slippage,
        "given_dist": given_dist,
        "status_dist": status_dist,
        "month_dist": month_dist,
    }


def template_bytes() -> bytes:
    """Blank upload template: the three mandatory headers + a sample row + an
    instructions sheet. All columns are mandatory. Dates as YYYY-MM-DD or
    DD-MM-YYYY. Remarks are NOT uploaded — they are added in the UI after review."""
    cols = ["ST_CD", "PROPOSED_OPENING_DATE", "SHARE_DATE"]
    # Illustrative placeholder codes (not real stores) — replace with your rows.
    sample = pd.DataFrame([
        {"ST_CD": "ZZ01", "PROPOSED_OPENING_DATE": "2026-07-22", "SHARE_DATE": "2026-07-20"},
        {"ST_CD": "ZZ02", "PROPOSED_OPENING_DATE": "2026-08-01", "SHARE_DATE": "2026-07-21"},
    ], columns=cols)

    notes = pd.DataFrame([
        ["ST_CD", "Yes", "Store code. Must exist in the store master; identity (name/RDC/hub) is filled automatically."],
        ["PROPOSED_OPENING_DATE", "Yes", "Currently budgeted opening date. Each upload with a NEW value is recorded as a date change."],
        ["SHARE_DATE", "Yes", "Date this opening date was shared/given. Drives the 'date given' timeline."],
        ["", "", ""],
        ["All columns mandatory", "", "Every row must have ST_CD + PROPOSED_OPENING_DATE + SHARE_DATE. Rows missing any are rejected."],
        ["Date format", "", "YYYY-MM-DD (e.g. 2026-07-22) or DD-MM-YYYY (e.g. 22-07-2026)."],
        ["Header aliases", "", "PROPOSED_OPENING_DATE = OP_DATE / BGT_OP_DT; SHARE_DATE = DATE_OF_SHARING. Case/spaces ignored."],
        ["Remarks", "", "NOT uploaded here — add remarks per store in the UI after review."],
        ["Metrics", "", "MBQ, stock, SLOC-wise and fill-rate are NOT uploaded — they are pulled live from the grid."],
    ], columns=["Column", "Required", "Description"])

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        sample.to_excel(w, index=False, sheet_name="Upload")
        notes.to_excel(w, index=False, sheet_name="Instructions")
        # widen columns for readability
        for sh, widths in (("Upload", [12, 24, 16]), ("Instructions", [26, 12, 90])):
            ws = w.sheets[sh]
            for i, width in enumerate(widths):
                ws.column_dimensions[chr(65 + i)].width = width
    return buf.getvalue()


def export_bytes(segments=DEFAULT_SEG) -> bytes:
    stores = list_stores(segments)
    flat = []
    for s in stores:
        row = {k: v for k, v in s.items() if k not in ("sloc",)}
        for key, val in (s.get("sloc") or {}).items():
            row[f"SLOC_{key}"] = val
        flat.append(row)
    df = pd.DataFrame(flat)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="UPC Store Tracking")
    return buf.getvalue()
