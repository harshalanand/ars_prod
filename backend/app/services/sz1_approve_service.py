"""
1SZ ALC — phase 3 (P5–P7): a parked session goes into the ARS Pending
Allocation ledger, or is discarded, re-fitted or trimmed.

    recheck(sid)            READ ONLY. Is the session still valid against live
                            stock and live store room? blocks / warns / totals.
    approve(sid, user)      ONE transaction: re-check → ARS_PEND_ALC rows
                            (SESSION_ID '1SZ-<sid>', SOURCE / ALLOC_MODE '1SZ',
                            SRC_RDC = ship-from, RDC = store's own warehouse)
                            → +1 MSA / grid delta →
                            operation APPROVE_1SZ in the Pend ALC log → header
                            APPROVED → balances re-applied. Any error: nothing.
    revert(sid, user)       through Pend ALC's revert dispatcher (op APPROVE_1SZ),
                            so it works from this module and from the Pend ALC
                            Operations Log alike. Refused once BDC / DO touched it.
    discard(sid, user)      PARKED → DISCARDED (lines kept for audit).
    refit(sid, user)        discard + a new run with the same stores and options.
    remove_stores(...)      drop some stores' lines from a parked session.
    pend_progress(ids)      READ ONLY. How far the approved sessions have shipped.

Spec: frontend/public/docs/manual/sz1_alloc.md (§J P3) and pendalc.md.

Why a no-commit wrapper: Pend ALC's apply_pend_alc_delta and log_operation
commit on the connection they are handed. Approve and revert must be ONE
transaction (pending rows, MSA / grid delta, operation row, 1SZ header and the
1SZ balances together), so those helpers get `NoCommit(conn)`; the caller's
transaction commits once at the end, or rolls everything back.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from loguru import logger
from sqlalchemy import text

from app.database.session import data_engine
from app.services import sz1_base_service as B
from app.services import sz1_schema as S
from app.services.sz1_base_service import NOW, q, rows

PEND_PREFIX = "1SZ-"                 # ARS_PEND_ALC.SESSION_ID = '1SZ-' + 1SZ session id
PEND_SOURCE = "1SZ"                  # ARS_PEND_ALC.SOURCE      (NVARCHAR(20))
PEND_MODE = "1SZ"                    # ARS_PEND_ALC.ALLOC_MODE  (NVARCHAR(10))
OP_TYPE = "APPROVE_1SZ"              # ARS_PEND_ALC_OPERATIONS.OP_TYPE (NVARCHAR(20))
PEND_TABLE = "ARS_PEND_ALC"
OPS_TABLE = "ARS_PEND_ALC_OPERATIONS"
PARKED = ("PARKED", "DONE")          # 'DONE' = a finished run from before phase 3
OLD_DAYS = 2                         # a parked session older than this is warned about
EXAMPLES = 50                        # short_stock / short_budget rows returned
NEW_COLS = ("APPROVED_BY", "APPROVED_AT", "PEND_SESSION_ID", "CLOSED_BY", "CLOSED_AT", "REFIT_OF")
# ARS_PEND_ALC column widths a 1SZ line must fit (PROD 2026-10-12)
PEND_WIDTHS = {"ARTICLE_NUMBER": 30, "MAJ_CAT": 50, "GEN_ART_NUMBER": 30, "CLR": 20, "ST_CD": 20, "SRC_RDC": 20}


# ═══════════════════════════════════════════════════════════════════════════
#  plumbing
# ═══════════════════════════════════════════════════════════════════════════
class NoCommit:
    """Pass a connection to a helper that calls conn.commit() itself, without
    letting it end the caller's transaction. Everything else is delegated."""

    def __init__(self, conn):
        self._conn = conn

    def commit(self):          # the caller commits once, at the end
        return None

    def __getattr__(self, name):
        return getattr(self._conn, name)


def pend_session_id(sid: str) -> str:
    return PEND_PREFIX + sid


def _check_sid(sid: str) -> str:
    sid = str(sid or "").strip()
    if not sid.isdigit() or len(sid) > 12:       # generated DDMMYYHHMM[SS]
        raise ValueError(f"'{sid}' is not a 1SZ session id")
    return sid


def assert_txn_alive(conn) -> None:
    """A deadlock (or any error that dooms the transaction) inside a helper that
    swallows errors would leave later statements running in a NEW transaction.
    Refuse to go on unless our transaction is still open and committable."""
    st = conn.execute(text("SELECT XACT_STATE()")).scalar()
    if st != 1:
        raise RuntimeError(f"The database transaction was lost (XACT_STATE = {st}); nothing was saved.")


def lock_session(conn, sid: str) -> Optional[Dict[str, Any]]:
    """Serialise approve / revert / discard / refit / remove-stores of one session:
    U-lock its header row (also starts the implicit transaction), then a
    transaction-owned applock — released by commit or rollback, so a failed
    call never leaves a lock behind on a pooled connection."""
    r = conn.execute(text(f"SELECT * FROM {q(S.SESSION)} WITH (UPDLOCK, HOLDLOCK) WHERE SESSION_ID = :s"),
                     {"s": sid}).fetchone()
    res = conn.execute(text("""
        DECLARE @r INT;
        EXEC @r = sp_getapplock @Resource = :res, @LockMode = 'Exclusive',
                                @LockOwner = 'Transaction', @LockTimeout = 60000;
        SELECT @r;"""), {"res": f"sz1_session:{sid}"}).scalar()
    if res is None or int(res) < 0:
        raise RuntimeError(f"Session {sid} is being changed by someone else right now (lock {res}). Try again.")
    return dict(r._mapping) if r is not None else None


def _json(v) -> Dict[str, Any]:
    try:
        d = json.loads(v) if v else {}
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _db_now(conn) -> str:
    return conn.execute(text("SELECT CONVERT(NVARCHAR(19), SYSDATETIME(), 120)")).scalar()


def _session_cols(conn) -> set:
    return {r[0].upper() for r in conn.execute(text(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = :t"), {"t": S.SESSION})}


def _msa_has_type(conn) -> bool:
    return bool(conn.execute(text(
        "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME = 'ARS_MSA_TOTAL' AND COLUMN_NAME = 'ALLOC_TYPE'")).scalar())


def _update_header(conn, sid: str, where_status: Tuple[str, ...], **cols) -> int:
    sets, params = B.set_clause(cols)
    st = ", ".join(f"'{s}'" for s in where_status)
    return int(conn.execute(text(f"UPDATE {q(S.SESSION)} SET {sets} WHERE SESSION_ID = :sid AND STATUS IN ({st})"),
                            {**params, "sid": sid}).rowcount or 0)


# ═══════════════════════════════════════════════════════════════════════════
#  the pending rows a session becomes (also the approve preview)
# ═══════════════════════════════════════════════════════════════════════════
def pend_src_cte(msa_has_type: bool) -> str:
    """CTE chain ending in `src`: one row per (SRC_RDC as RDC, ST_CD, ARTICLE,
    ALLOC_TYPE) with ALLOC_QTY > 0. Bind :sid (the 1SZ session id).

    ALLOC_TYPE = the article's type in Step 1 (ARS_SZ1_MSA_SZ_BASE). A mixed
    article ('FRESH+GRT') is split by the warehouse's live FRESH free stock,
    FRESH first: stores take FRESH in ST_CD order until it is used up, the rest
    is GRT. Without an ALLOC_TYPE column on ARS_MSA_TOTAL everything is FRESH
    (the typed MSA delta folds untyped rows into FRESH anyway)."""
    if msa_has_type:
        kind = """CASE WHEN bt.BASE_TYPE LIKE '%+%'                        THEN 'MIX'
                       WHEN UPPER(ISNULL(bt.BASE_TYPE, '')) IN ('', 'FRESH') THEN 'FRESH'
                       ELSE UPPER(bt.BASE_TYPE) END"""
        fresh = f"""LEFT JOIN (SELECT RDC, ARTICLE_NUMBER,
                                     SUM(CASE WHEN FNL_Q > 0 THEN FNL_Q ELSE 0 END) AS FRESH_FREE
                                FROM {q(S.MSA_TOTAL)} WHERE ALLOC_TYPE = 'FRESH'
                               GROUP BY RDC, ARTICLE_NUMBER) fr
                       ON fr.RDC = ln.RDC AND fr.ARTICLE_NUMBER = ln.ARTICLE_NUMBER"""
        fresh_col = "ISNULL(fr.FRESH_FREE, 0)"
    else:
        kind, fresh, fresh_col = "'FRESH'", "", "0"
    return f"""
WITH ln AS (                      -- the session's served lines at the pending grain
    SELECT SRC_RDC AS RDC, ST_CD, ARTICLE_NUMBER,
           MAX(MAJ_CAT) AS MAJ_CAT, MAX(GEN_ART_NUMBER) AS GEN_ART_NUMBER, MAX(CLR) AS CLR,
           CAST(SUM(ALLOC_QTY) AS FLOAT) AS QTY
      FROM {q(S.LINES)}
     WHERE SESSION_ID = :sid AND ALLOC_QTY > 0
     GROUP BY SRC_RDC, ST_CD, ARTICLE_NUMBER
),
typed AS (
    SELECT ln.*, {kind} AS KIND, {fresh_col} AS FRESH_FREE,
           SUM(ln.QTY) OVER (PARTITION BY ln.RDC, ln.ARTICLE_NUMBER ORDER BY ln.ST_CD
                             ROWS UNBOUNDED PRECEDING) - ln.QTY AS BEFORE_Q
      FROM ln
      LEFT JOIN (SELECT RDC, ARTICLE_NUMBER, MAX(ALLOC_TYPE) AS BASE_TYPE
                   FROM {q(S.SZ_BASE)} GROUP BY RDC, ARTICLE_NUMBER) bt
             ON bt.RDC = ln.RDC AND bt.ARTICLE_NUMBER = ln.ARTICLE_NUMBER
      {fresh}
),
split AS (
    SELECT typed.*,
           CASE WHEN KIND = 'FRESH' THEN QTY
                WHEN KIND = 'MIX' THEN
                     CASE WHEN FRESH_FREE - BEFORE_Q <= 0  THEN 0
                          WHEN FRESH_FREE - BEFORE_Q >= QTY THEN QTY
                          ELSE FRESH_FREE - BEFORE_Q END
                ELSE 0 END AS FRESH_Q
      FROM typed
),
src AS (
    SELECT RDC, ST_CD, ARTICLE_NUMBER, MAJ_CAT, GEN_ART_NUMBER, CLR,
           CAST('FRESH' AS NVARCHAR(10)) AS ALLOC_TYPE, FRESH_Q AS ALLOC_QTY
      FROM split WHERE FRESH_Q > 0
    UNION ALL
    SELECT RDC, ST_CD, ARTICLE_NUMBER, MAJ_CAT, GEN_ART_NUMBER, CLR,
           CAST(CASE WHEN KIND = 'MIX' THEN 'GRT' ELSE KIND END AS NVARCHAR(10)), QTY - FRESH_Q
      FROM split WHERE QTY - FRESH_Q > 0
)"""


def insert_sql(msa_has_type: bool) -> str:
    """The approve INSERT. Bind :sid (1SZ id), :psid (pending id), :remark.
    Idempotent: NOT EXISTS on the pending grain, as write_pend_alc does, with
    ALLOC_TYPE added because a mixed article yields a FRESH and a GRT row.

    In the src CTE `RDC` is the 1SZ line's ship-from warehouse. It is written
    to ARS_PEND_ALC.SRC_RDC (what MSA, BDC and DO key on); the ledger's RDC
    is the store's own warehouse from the store master (2026-10-14)."""
    return pend_src_cte(msa_has_type) + f"""
INSERT INTO {PEND_TABLE}
    (SESSION_ID, RDC, SRC_RDC, ST_CD, ARTICLE_NUMBER, MAJ_CAT, GEN_ART_NUMBER, CLR,
     ALLOC_QTY, DO_QTY, ALLOC_MODE, SOURCE, ALLOC_TYPE, REMARKS)
SELECT :psid,
       COALESCE((SELECT TOP 1 SM.[RDC] FROM [Master_ALC_INPUT_ST_MASTER] SM
                  WHERE SM.[ST_CD] = src.ST_CD), src.RDC),
       src.RDC, src.ST_CD, src.ARTICLE_NUMBER, src.MAJ_CAT, src.GEN_ART_NUMBER, src.CLR,
       src.ALLOC_QTY, 0, '{PEND_MODE}', '{PEND_SOURCE}', src.ALLOC_TYPE, :remark
  FROM src
 WHERE NOT EXISTS (
       SELECT 1 FROM {PEND_TABLE} P
        WHERE P.SESSION_ID         = :psid
          AND COALESCE(NULLIF(P.SRC_RDC, ''), P.RDC) = src.RDC
          AND ISNULL(P.ST_CD, '')  = ISNULL(src.ST_CD, '')
          AND P.ARTICLE_NUMBER     = src.ARTICLE_NUMBER
          AND P.ALLOC_MODE         = '{PEND_MODE}'
          AND ISNULL(P.ALLOC_TYPE, '') = src.ALLOC_TYPE)"""


def pend_rows_preview(session_id: str, top: int = 100) -> Dict[str, Any]:
    """The ARS_PEND_ALC rows approve would write, without writing them."""
    sid = _check_sid(session_id)
    with data_engine.connect() as conn:
        cte = pend_src_cte(_msa_has_type(conn))
        by_type = rows(conn, cte + " SELECT ALLOC_TYPE, COUNT(*) AS ROWS_, SUM(ALLOC_QTY) AS UNITS "
                                   "FROM src GROUP BY ALLOC_TYPE ORDER BY ALLOC_TYPE", {"sid": sid})
        sample = rows(conn, cte + f" SELECT TOP ({int(top)}) * FROM src ORDER BY RDC, ST_CD, ARTICLE_NUMBER, ALLOC_TYPE",
                      {"sid": sid})
    return {"pend_session_id": pend_session_id(sid), "by_type": by_type, "sample": sample}


# ═══════════════════════════════════════════════════════════════════════════
#  recheck — READ ONLY
# ═══════════════════════════════════════════════════════════════════════════
def recheck(session_id: str, conn=None) -> Dict[str, Any]:
    """Is the session still valid NOW? Blocks refuse the approve; warnings do not.

    Stock: per (SRC_RDC, article) the session's units ≤ live free stock
    (sz1_base_service.live_free_sql — ARS_MSA_TOTAL FNL_Q now, which already nets
    every approved pending), else another approval or a listing took it.
    Store room: per store + MAJ_CAT + size the session's units ≤
    max(MJ_SZ_MBQ − MJ_SZ_STK − live open pending − Σ the OTHER parked sessions, 0)
    from the current Step 2 row."""
    sid = _check_sid(session_id)
    if conn is None:
        S.ensure_tables()
        with data_engine.connect() as c:
            return recheck(sid, c)
    t0 = time.time()
    block: List[str] = []
    warn: List[str] = []
    out: Dict[str, Any] = {"session_id": sid, "pend_session_id": pend_session_id(sid), "ok": False,
                           "block": block, "warn": warn, "short_stock": [], "short_budget": [],
                           "short_stock_count": 0, "short_budget_count": 0,
                           "totals": {"lines": 0, "units": 0.0, "pend_rows": 0, "stores": 0}}

    h = conn.execute(text(f"""
        SELECT SESSION_ID, STATUS, CHECKS_PASSED, BASE_BUILD_ID, REQ_BUILD_ID, SESSION_AT,
               DATEDIFF(HOUR, SESSION_AT, SYSDATETIME()) AS AGE_H
          FROM {q(S.SESSION)} WHERE SESSION_ID = :s"""), {"s": sid}).fetchone()
    if h is None:
        raise ValueError(f"Session {sid} not found")
    h = dict(h._mapping)
    out["status"] = h["STATUS"]
    if h["STATUS"] not in PARKED:
        block.append(f"Session {sid} is {h['STATUS']}; only a parked session can be approved.")
    if not h["CHECKS_PASSED"]:
        block.append(f"Session {sid} did not pass its allocation-limit checks.")
    missing = [c for c in NEW_COLS if c not in _session_cols(conn)]
    if missing:
        block.append("ARS_SZ1_SESSION lacks " + ", ".join(missing) +
                     " — run 1SZ ALC › Repair tables (039_sz1_module.sql) first.")

    psid = pend_session_id(sid)
    have = conn.execute(text(f"SELECT COUNT(*) FROM {PEND_TABLE} WITH (NOLOCK) WHERE SESSION_ID = :p"),
                        {"p": psid}).scalar() or 0
    if have:
        block.append(f"Pend ALC already holds {have:,} row(s) for {psid}.")
    op = conn.execute(text(f"""
        SELECT TOP 1 OP_ID, CREATED_BY, OP_DATE FROM {OPS_TABLE}
         WHERE OP_TYPE = '{OP_TYPE}' AND OP_KEY = :p AND REVERTED_AT IS NULL ORDER BY OP_ID DESC"""),
        {"p": psid}).fetchone()
    if op:
        block.append(f"Already approved (operation #{op[0]} by {op[1] or '?'} at {op[2]}).")

    # ── totals + values that would not fit ARS_PEND_ALC
    t = conn.execute(text(f"""
        SELECT COUNT(*), ISNULL(SUM(ALLOC_QTY), 0), COUNT(DISTINCT ST_CD),
               SUM(CASE WHEN LEN(ARTICLE_NUMBER) > {PEND_WIDTHS['ARTICLE_NUMBER']}
                          OR LEN(MAJ_CAT) > {PEND_WIDTHS['MAJ_CAT']}
                          OR LEN(ISNULL(GEN_ART_NUMBER, '')) > {PEND_WIDTHS['GEN_ART_NUMBER']}
                          OR LEN(ISNULL(CLR, '')) > {PEND_WIDTHS['CLR']}
                          OR LEN(ST_CD) > {PEND_WIDTHS['ST_CD']}
                          OR LEN(SRC_RDC) > {PEND_WIDTHS['SRC_RDC']} THEN 1 ELSE 0 END)
          FROM {q(S.LINES)} WHERE SESSION_ID = :s AND ALLOC_QTY > 0"""), {"s": sid}).fetchone()
    out["totals"].update(lines=int(t[0] or 0), units=float(t[1] or 0), stores=int(t[2] or 0))
    if not t[0]:
        block.append(f"Session {sid} allocated nothing; there is nothing to approve.")
    if t[3]:
        block.append(f"{int(t[3]):,} line(s) carry a value too long for ARS_PEND_ALC "
                     f"(widths {PEND_WIDTHS}).")
    out["totals"]["pend_rows"] = int(conn.execute(text(pend_src_cte(_msa_has_type(conn)) +
                                                       " SELECT COUNT(*) FROM src"), {"sid": sid}).scalar() or 0)

    # ── live stock
    stock = rows(conn, f"""
        SELECT n.SRC_RDC, n.ARTICLE_NUMBER, n.NEED, ISNULL(f.FREE_Q, 0) AS FREE
          FROM (SELECT SRC_RDC, ARTICLE_NUMBER, SUM(ALLOC_QTY) AS NEED
                  FROM {q(S.LINES)} WHERE SESSION_ID = :s AND ALLOC_QTY > 0
                 GROUP BY SRC_RDC, ARTICLE_NUMBER) n
          LEFT JOIN (SELECT RDC, ARTICLE_NUMBER, SUM(FREE_Q) AS FREE_Q
                       FROM ({B.live_free_sql()}) lf GROUP BY RDC, ARTICLE_NUMBER) f
                 ON f.RDC = n.SRC_RDC AND f.ARTICLE_NUMBER = n.ARTICLE_NUMBER
         WHERE n.NEED > ISNULL(f.FREE_Q, 0) + 0.005
         ORDER BY n.NEED - ISNULL(f.FREE_Q, 0) DESC, n.SRC_RDC, n.ARTICLE_NUMBER""", {"s": sid})
    out["short_stock_count"] = len(stock)
    out["short_stock"] = stock[:EXAMPLES]
    if stock:
        units = sum(float(r["NEED"]) - float(r["FREE"]) for r in stock)
        block.append(f"{len(stock):,} warehouse × article(s) no longer have the free stock this session "
                     f"needs ({units:,.0f} units short) — another approval or a listing took it. Re-fit the session.")

    # ── live store room per store + MAJ_CAT + size
    others = [s for s in B.parked_sessions(conn) if str(s) != sid]
    # the same room formula the runs and balances use (sz1_base_service.LIVE_ROOM_EXPR):
    # A / NA rows count the whole category's pending, like Step 2
    budget = rows(conn, f"""
        SELECT x.ST_CD, x.MAJ_CAT, x.SZ, x.NEED, x.ROOM, x.MJ_SZ_MBQ, x.LIVE_STK, x.OTHER_PARKED
          FROM (
            SELECT n.ST_CD, n.MAJ_CAT, n.SZ, n.NEED, r.MJ_SZ_MBQ,
                   {B.LIVE_STK_SZ_EXPR} AS LIVE_STK, ISNULL(pts.TAKEN, 0) AS OTHER_PARKED,
                   CASE WHEN r.ST_CD IS NULL THEN 0 ELSE {B.LIVE_ROOM_EXPR} END AS ROOM
              FROM (SELECT ST_CD, MAJ_CAT, SZ, SUM(ALLOC_QTY) AS NEED
                      FROM {q(S.LINES)} WHERE SESSION_ID = :s AND ALLOC_QTY > 0
                     GROUP BY ST_CD, MAJ_CAT, SZ) n
              LEFT JOIN {q(S.REQ_BASE)} r ON r.ST_CD = n.ST_CD AND r.MAJ_CAT = n.MAJ_CAT AND r.SZ = n.SZ
              {B.live_joins_req(others, cat=False)}
          ) x
         WHERE x.NEED > x.ROOM + 0.005
         ORDER BY x.NEED - x.ROOM DESC, x.ST_CD, x.MAJ_CAT, x.SZ""", {"s": sid})
    out["short_budget_count"] = len(budget)
    out["short_budget"] = budget[:EXAMPLES]
    if budget:
        units = sum(float(r["NEED"]) - float(r["ROOM"]) for r in budget)
        block.append(f"{len(budget):,} store × MAJ_CAT × size(s) no longer have room for what this session "
                     f"sends ({units:,.0f} units over) — store stock now (pending included) and the other parked sessions. "
                     "Remove those stores or re-fit the session.")

    # ── warnings
    for step, col in ((1, "BASE_BUILD_ID"), (2, "REQ_BUILD_ID")):
        latest = conn.execute(text(f"SELECT MAX(BUILD_ID) FROM {q(S.BUILD)} WHERE STEP = :st AND STATUS = 'DONE'"),
                              {"st": step}).scalar()
        if latest is not None and h[col] is not None and int(h[col]) < int(latest):
            warn.append(f"Built on Step {step} build {h[col]}; the current Step {step} build is {latest}.")
    if h["AGE_H"] is not None and int(h["AGE_H"]) > OLD_DAYS * 24:
        warn.append(f"The session is {int(h['AGE_H']) // 24} day(s) old; store stock and need may have moved.")
    if others:
        warn.append(f"{len(others)} other parked session(s) are netted from the store room: {', '.join(map(str, others[:10]))}"
                    + (" …" if len(others) > 10 else ""))

    out["ok"] = not block
    out["seconds"] = round(time.time() - t0, 2)
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  approve
# ═══════════════════════════════════════════════════════════════════════════
def approve(session_id: str, user: Optional[str]) -> Dict[str, Any]:
    sid = _check_sid(session_id)
    psid = pend_session_id(sid)
    S.ensure_tables()
    t0 = time.time()
    with B.job_slot("approve"):
        with data_engine.begin() as conn:
            head = lock_session(conn, sid)
            if head is None:
                raise ValueError(f"Session {sid} not found")
            chk = recheck(sid, conn)                         # the same transaction, after the lock
            if not chk["ok"]:
                raise ValueError("Cannot approve: " + " ".join(chk["block"]))

            n = int(conn.execute(text(insert_sql(_msa_has_type(conn))),
                                 {"sid": sid, "psid": psid, "remark": f"1SZ ALC session {sid}"}).rowcount or 0)
            got = conn.execute(text(f"SELECT COUNT(*), ISNULL(SUM(ALLOC_QTY), 0) FROM {PEND_TABLE} "
                                    "WHERE SESSION_ID = :p"), {"p": psid}).fetchone()
            units = float(got[1] or 0)
            if n != chk["totals"]["pend_rows"] or int(got[0]) != n or abs(units - chk["totals"]["units"]) > 0.01:
                raise RuntimeError(f"Pending rows do not reconcile: wrote {n} / {got[0]} rows, {units} units; "
                                   f"expected {chk['totals']['pend_rows']} rows, {chk['totals']['units']} units.")

            from app.services import pend_alc_service as P
            delta = P.apply_pend_alc_delta_by_session(NoCommit(conn), psid, sign=+1)
            if delta.get("error"):
                raise RuntimeError(f"MSA / grid delta failed: {delta['error']}")
            if n and not delta.get("msa_total"):
                raise RuntimeError("MSA delta matched no ARS_MSA_TOTAL row; nothing was saved.")
            assert_txn_alive(conn)

            op_id = P.log_operation(
                NoCommit(conn), op_type=OP_TYPE, op_key=psid,
                payload={"session_id": psid, "sz1_session_id": sid, "rows": n, "units": units,
                         "warn": chk["warn"], "msa_delta": {k: v for k, v in delta.items() if k != "error"}},
                summary=f"Approve 1SZ session {sid}: {n} pend rows, {int(units)} units, by {user}",
                rows_affected=n, qty_total=units, created_by=user)

            if _update_header(conn, sid, PARKED, STATUS="APPROVED", APPROVED_BY=user, APPROVED_AT=NOW,
                              PEND_SESSION_ID=psid) != 1:
                raise RuntimeError(f"Session {sid} changed while approving; nothing was saved.")
            balances = B.push_balances(conn)
            assert_txn_alive(conn)
    logger.info(f"[sz1] session {sid} approved by {user}: {n} pend rows, {units:,.0f} units, op {op_id}, "
                f"{time.time() - t0:.1f}s")
    return {"session_id": sid, "pend_session_id": psid, "rows": n, "units": units, "op_id": op_id,
            "warn": chk["warn"], "balance_sessions": balances, "seconds": round(time.time() - t0, 1)}


# ═══════════════════════════════════════════════════════════════════════════
#  revert — through Pend ALC (op APPROVE_1SZ), so either screen can do it
# ═══════════════════════════════════════════════════════════════════════════
def active_op(conn, sid: str) -> Optional[int]:
    r = conn.execute(text(f"""
        SELECT TOP 1 OP_ID FROM {OPS_TABLE}
         WHERE OP_TYPE = '{OP_TYPE}' AND OP_KEY = :p AND REVERTED_AT IS NULL ORDER BY OP_ID DESC"""),
        {"p": pend_session_id(sid)}).fetchone()
    return int(r[0]) if r else None


def revert(session_id: str, user: Optional[str]) -> Dict[str, Any]:
    """Preview first (BDC / DO gate); then Pend ALC's revert_operation, which
    calls _revert_approve_1sz → reopen_after_revert below, and commits once."""
    sid = _check_sid(session_id)
    from app.services import pend_alc_service as P
    with B.job_slot("revert"):
        with data_engine.connect() as conn:
            op_id = active_op(conn, sid)
            if op_id is None:
                raise ValueError(f"Session {sid} has no active 1SZ approval in the Pend ALC operations log.")
            pv = P.preview_revert(conn, op_id)
            if pv.get("error") or not pv.get("can_revert"):
                conn.rollback()
                raise ValueError("Cannot revert: " + (pv.get("error") or " ".join(pv.get("blockers") or [])))
            res = P.revert_operation(conn, op_id, reverted_by=user, note=f"1SZ ALC: revert of session {sid}")
            if not res.get("success"):
                conn.rollback()
                raise ValueError("Cannot revert: " + str(res.get("error")))
    logger.info(f"[sz1] session {sid} approval reverted by {user} (op {op_id})")
    return {"session_id": sid, "op_id": op_id, **{k: v for k, v in res.items() if k != "success"}}


def reopen_after_revert(conn, sz1_sid: Optional[str], pend_sid: str, reverted_by: Optional[str],
                        op_id: Optional[int]) -> Dict[str, Any]:
    """Called by pend_alc_service._revert_approve_1sz INSIDE its transaction,
    after the −1 delta and the DELETE: the 1SZ session goes back to PARKED (its
    approval is kept in the summary under `approvals`) and the balances are
    re-applied. Raises on any inconsistency so the whole revert rolls back."""
    if not sz1_sid:
        sz1_sid = pend_sid[len(PEND_PREFIX):] if pend_sid.startswith(PEND_PREFIX) else None
    if not sz1_sid:
        return {"reopened": 0}
    head = conn.execute(text(f"SELECT STATUS, APPROVED_BY, APPROVED_AT, SUMMARY_JSON FROM {q(S.SESSION)} "
                             "WHERE SESSION_ID = :s"), {"s": sz1_sid}).fetchone()
    if head is None:                     # header gone: the pending side is still reverted
        return {"reopened": 0, "note": f"1SZ session {sz1_sid} not found"}
    if head[0] != "APPROVED":
        raise RuntimeError(f"1SZ session {sz1_sid} is {head[0]}, not APPROVED; revert refused.")
    summary = _json(head[3])
    summary.setdefault("approvals", []).append({
        "pend_session_id": pend_sid, "approved_by": head[1], "approved_at": B.jsonable(head[2]),
        "reverted_by": reverted_by, "reverted_at": _db_now(conn), "op_id": op_id})
    n = _update_header(conn, sz1_sid, ("APPROVED",), STATUS="PARKED", APPROVED_BY=None, APPROVED_AT=None,
                       PEND_SESSION_ID=None, SUMMARY_JSON=json.dumps(summary, default=str))
    if n != 1:
        raise RuntimeError(f"1SZ session {sz1_sid} changed while reverting; revert refused.")
    balances = B.push_balances(conn)
    return {"reopened": n, "sz1_session_id": sz1_sid, "balance_sessions": balances}


# ═══════════════════════════════════════════════════════════════════════════
#  discard · refit · remove stores
# ═══════════════════════════════════════════════════════════════════════════
def _close(conn, sid: str, user: Optional[str], action: str, note: Optional[str] = None) -> None:
    head = lock_session(conn, sid)
    if head is None:
        raise ValueError(f"Session {sid} not found")
    if head["STATUS"] not in PARKED:
        raise ValueError(f"Session {sid} is {head['STATUS']}; only a parked session can be {action}ed.")
    summary = _json(head.get("SUMMARY_JSON"))
    summary["closed"] = {"action": action, "by": user, "at": _db_now(conn), "note": note}
    if _update_header(conn, sid, PARKED, STATUS="DISCARDED", CLOSED_BY=user, CLOSED_AT=NOW,
                      SUMMARY_JSON=json.dumps(summary, default=str)) != 1:
        raise RuntimeError(f"Session {sid} changed meanwhile; nothing was saved.")


def discard(session_id: str, user: Optional[str], note: Optional[str] = None) -> Dict[str, Any]:
    """PARKED → DISCARDED. The lines stay for audit; its stock and store room
    go back to the pool (balances re-applied)."""
    sid = _check_sid(session_id)
    S.ensure_tables()
    with B.job_slot("discard"):
        with data_engine.begin() as conn:
            _close(conn, sid, user, "discard", note)
            balances = B.push_balances(conn)
    logger.info(f"[sz1] session {sid} discarded by {user}")
    return {"session_id": sid, "status": "DISCARDED", "balance_sessions": balances}


def _reopen(sid: str, why: str) -> None:
    """Undo a refit's discard when its new run could not start."""
    with data_engine.begin() as conn:
        head = lock_session(conn, sid)
        summary = _json((head or {}).get("SUMMARY_JSON"))
        summary.pop("closed", None)
        summary.setdefault("refit_failed", []).append({"at": _db_now(conn), "error": why[:500]})
        _update_header(conn, sid, ("DISCARDED",), STATUS="PARKED", CLOSED_BY=None, CLOSED_AT=None,
                       SUMMARY_JSON=json.dumps(summary, default=str))
        B.push_balances(conn)


def _run_body(head: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """The start_run body that repeats a session: its ok stores, sequence and options."""
    lst = _json(head.get("STORE_LIST"))
    opts = lst.get("options") or _json(head.get("SUMMARY_JSON")).get("options") or {}
    notes = [] if opts else ["The session predates recorded run options; the header's mode / stock basis / "
                             "warehouse mode were used and the current defaults for the All RDC options."]
    stores = [str(s) for s in (lst.get("stores") or [])]
    seq = lst.get("sequence")
    body = {"codes": stores,
            "mode": opts.get("mode") or head.get("PRIORITY_MODE") or "weighted",
            "weight_mj": opts.get("weight_mj") if opts.get("weight_mj") is not None
            else (float(head["WEIGHT_MJ"]) if head.get("WEIGHT_MJ") is not None else 0.5),
            "sequence": " ".join(map(str, seq)) if isinstance(seq, list) else seq,
            "stock_basis": opts.get("stock_basis") or head.get("STOCK_BASIS") or "initial",
            "cross_rdc": opts.get("cross_rdc") or head.get("CROSS_RDC") or "all",
            "protect_other": opts.get("protect_other"), "one_source": opts.get("one_source"),
            "note": f"Re-fit of session {head['SESSION_ID']}"}
    return body, notes


def refit(session_id: str, user: Optional[str]) -> Dict[str, Any]:
    """Discard the session, then run its stores again with its options on the
    stock and room as they are now.

    Order: discard FIRST (committed), then start_run, because the new run must
    not see the old session's lines netted off the balances — it is meant to
    re-use that stock. start_run validates synchronously (codes, readiness,
    stores with need); if it raises, the discard is undone (session back to
    PARKED, balances re-applied) before the error is returned. start_run takes
    the module's job slot itself, so it is called outside our own slot (the
    slot's lock is not re-entrant). If the background run fails later, the new
    session shows FAILED and the old one stays DISCARDED (with refit_to set):
    the old lines are still on record for audit, and a fresh run can be planned."""
    sid = _check_sid(session_id)
    S.ensure_tables()
    from app.services import sz1_run_service as R
    with data_engine.connect() as conn:
        head = conn.execute(text(f"SELECT * FROM {q(S.SESSION)} WHERE SESSION_ID = :s"), {"s": sid}).fetchone()
    if head is None:
        raise ValueError(f"Session {sid} not found")
    head = dict(head._mapping)
    if head["STATUS"] not in PARKED:
        raise ValueError(f"Session {sid} is {head['STATUS']}; only a parked session can be re-fitted.")
    body, notes = _run_body(head)
    if not body["codes"]:
        raise ValueError(f"Session {sid} has no store list on record; plan a new run instead.")

    with B.job_slot("refit"):
        with data_engine.begin() as conn:
            _close(conn, sid, user, "refit", "re-fitted into a new run")
            B.push_balances(conn)
    try:
        new_sid = R.start_run(body, user)
    except Exception as e:  # noqa: BLE001
        try:
            _reopen(sid, str(e))
        except Exception:  # noqa: BLE001
            logger.exception(f"[sz1] refit of {sid}: could not re-open it after the new run failed to start")
            raise RuntimeError(f"The new run did not start ({e}) and session {sid} could not be re-opened; "
                               "it is DISCARDED. Re-apply balances and plan a new run.") from e
        raise
    with data_engine.begin() as conn:
        conn.execute(text(f"UPDATE {q(S.SESSION)} SET REFIT_OF = :old WHERE SESSION_ID = :new"),
                     {"old": sid, "new": new_sid})
        r = conn.execute(text(f"SELECT SUMMARY_JSON FROM {q(S.SESSION)} WHERE SESSION_ID = :s"), {"s": sid}).scalar()
        summary = _json(r)
        summary.setdefault("closed", {})["refit_to"] = new_sid
        conn.execute(text(f"UPDATE {q(S.SESSION)} SET SUMMARY_JSON = :j WHERE SESSION_ID = :s"),
                     {"j": json.dumps(summary, default=str), "s": sid})
    logger.info(f"[sz1] session {sid} re-fitted by {user} into {new_sid}")
    return {"session_id": new_sid, "refit_of": sid, "notes": notes}


def remove_stores(session_id: str, stores: List[str], user: Optional[str]) -> Dict[str, Any]:
    """Drop some stores' lines from a parked session and recompute its header,
    checks and summary from what is left — one transaction."""
    from app.services import sz1_run_service as R
    sid = _check_sid(session_id)
    codes = sorted({str(s).strip().upper() for s in (stores or []) if str(s).strip()})
    if not codes:
        raise ValueError("Give at least one store code to remove.")
    S.ensure_tables()
    names = [c for c, _, _ in R.LINE_COLS]
    with B.job_slot("remove_stores"):
        with data_engine.begin() as conn:
            head = lock_session(conn, sid)
            if head is None:
                raise ValueError(f"Session {sid} not found")
            if head["STATUS"] not in PARKED:
                raise ValueError(f"Session {sid} is {head['STATUS']}; stores can be removed only from a parked session.")
            params = {f"c{i}": c for i, c in enumerate(codes)}
            inn = ", ".join(f":{k}" for k in params)
            gone = conn.execute(text(f"""
                SELECT COUNT(*), ISNULL(SUM(ALLOC_QTY), 0), COUNT(DISTINCT ST_CD)
                  FROM {q(S.LINES)} WHERE SESSION_ID = :sid AND ST_CD IN ({inn})"""), {"sid": sid, **params}).fetchone()
            if not gone[0]:
                raise ValueError(f"None of {', '.join(codes)} have lines in session {sid}.")
            left = conn.execute(text(f"SELECT COUNT(*) FROM {q(S.LINES)} WHERE SESSION_ID = :sid AND ST_CD NOT IN ({inn})"),
                                {"sid": sid, **params}).scalar()
            if not left:
                raise ValueError("That would remove every store of the session — discard it instead.")
            conn.execute(text(f"DELETE FROM {q(S.LINES)} WHERE SESSION_ID = :sid AND ST_CD IN ({inn})"),
                         {"sid": sid, **params})

            res = conn.execute(text(f"SELECT {', '.join(names)} FROM {q(S.LINES)} WHERE SESSION_ID = :sid"), {"sid": sid})
            df = pd.DataFrame(res.fetchall(), columns=list(res.keys()))
            for c, k, _ in R.LINE_COLS:
                if k == "f":
                    df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
                elif k == "i":
                    df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype(int)
            tot = R._summary_totals(df)
            checks = R.check_stored(conn, sid, expect_rows=len(df), expect_qty=float(df["ALLOC_QTY"].sum()))
            if not all(c["ok"] for c in checks):
                raise RuntimeError("The remaining lines fail an allocation limit: " +
                                   "; ".join(c["name"] for c in checks if not c["ok"]))

            old = _json(head.get("SUMMARY_JSON"))
            summary = {**old, **R._summary(df)}               # keep steps / readiness / options …
            summary.setdefault("removed_stores", []).append({
                "stores": codes, "lines": int(gone[0]), "units": float(gone[1]), "by": user, "at": _db_now(conn)})
            lst = _json(head.get("STORE_LIST"))
            if lst.get("stores"):
                lst["stores"] = [s for s in lst["stores"] if str(s).upper() not in codes]
            if isinstance(lst.get("sequence"), list):
                lst["sequence"] = [s for s in lst["sequence"] if str(s).upper() not in codes]
            n = _update_header(conn, sid, PARKED, ROWS_WRITTEN=int(tot["ROWS"]), STORES_SERVED=tot["STORES_SERVED"],
                               LINES_SERVED=tot["LINES_SERVED"], ASK_QTY=tot["ASK_QTY"], ASK_QTY_ROD=tot["ASK_QTY_ROD"],
                               ALLOC_QTY=tot["ALLOC_QTY"], PACKS=tot["PACKS"], CROSS_QTY=tot["CROSS_QTY"],
                               STORES_OK=len(lst.get("stores") or []) or int(df["ST_CD"].nunique()),
                               STORE_LIST=json.dumps(lst, default=str),
                               CHECKS_JSON=json.dumps(checks, default=str),
                               SUMMARY_JSON=json.dumps(summary, default=str))
            if n != 1:
                raise RuntimeError(f"Session {sid} changed meanwhile; nothing was saved.")
            balances = B.push_balances(conn)
    logger.info(f"[sz1] session {sid}: stores {codes} removed by {user} ({int(gone[0])} lines, {float(gone[1]):,.0f} units)")
    return {"session_id": sid, "removed": codes, "lines_removed": int(gone[0]), "units_removed": float(gone[1]),
            "totals": tot, "balance_sessions": balances}


# ═══════════════════════════════════════════════════════════════════════════
#  pending progress — READ ONLY
# ═══════════════════════════════════════════════════════════════════════════
def pend_progress(session_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """{sid: {rows, alloc_qty, bdc_qty, do_qty, pend_qty, closed_rows, open_rows, shipped}}
    from ARS_PEND_ALC; shipped = every row closed."""
    ids = [s for s in {str(x).strip() for x in session_ids or []} if s.isdigit() and len(s) <= 12]
    if not ids:
        return {}
    params = {f"p{i}": pend_session_id(s) for i, s in enumerate(ids)}
    with data_engine.connect() as conn:
        res = conn.execute(text(f"""
            SELECT SESSION_ID, COUNT(*), ISNULL(SUM(ALLOC_QTY), 0), ISNULL(SUM(BDC_QTY), 0),
                   ISNULL(SUM(DO_QTY), 0), ISNULL(SUM(PEND_QTY), 0),
                   SUM(CASE WHEN IS_CLOSED = 1 THEN 1 ELSE 0 END), SUM(CASE WHEN IS_CLOSED = 1 THEN 0 ELSE 1 END)
              FROM {PEND_TABLE} WITH (NOLOCK)
             WHERE SESSION_ID IN ({', '.join(':' + k for k in params)})
             GROUP BY SESSION_ID"""), params).fetchall()
    out = {}
    for r in res:
        n, opn = int(r[1] or 0), int(r[7] or 0)
        out[str(r[0])[len(PEND_PREFIX):]] = {
            "rows": n, "alloc_qty": float(r[2]), "bdc_qty": float(r[3]), "do_qty": float(r[4]),
            "pend_qty": float(r[5]), "closed_rows": int(r[6] or 0), "open_rows": opn, "shipped": n > 0 and opn == 0}
    return out
