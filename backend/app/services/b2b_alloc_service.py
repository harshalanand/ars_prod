"""
GRT ALC — Step 4: run an allocation as a session.

Each run is a new row in ARS_B2B_SESSION — nothing is ever overwritten — with
the demand build it read (G10), every choice it was given, live progress, skip
counts, and the balance checks (G9). The engine is b2b_alloc_engine; this
module is the job around it:

    gates      a demand build exists and is not out of date, nothing is loading
               or building, no other run is going
    run        b2b_alloc_engine.allocate_all, categories on a process pool
    check      G9 on the in-memory result
    write      (not on a dry run) lines, picks and leftovers in ONE transaction,
               then G9 again on the stored rows, before commit
    record     totals, skips, checks, leftover reasons, timings on the session

The warehouse rule is required on every run (G11): there is no default, so
nobody ships stock between RDCs because a setting was never saved.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import data_engine
from app.services import b2b_alloc_engine as E
from app.services import b2b_schema as S
from app.services import b2b_settings

ALLOC_WORKERS = 8                   # processes; one category at a time each
BATCH = 20_000
LIVE = {"bin": S.BIN_MASTER, "store": S.STORE_MASTER, "req": S.REQ, "mbq": S.ART_MBQ,
        "bin_rdc": "BIN_RDC"}

CHOICES = {"priority": E.PRIORITIES, "fill_mode": E.FILL_MODES, "fair_basis": E.FAIR_BASES,
           "bin_pick": E.BIN_PICKS, "cross_rdc": E.CROSS_RDCS}
SETTING_OF = {"priority": "ALLOC_PRIORITY", "min_qty": "ALLOC_MIN_QTY", "fill_mode": "ALLOC_FILL_MODE",
              "fair_basis": "ALLOC_FAIR_BASIS", "bin_pick": "ALLOC_BIN_PICK", "cross_rdc": "ALLOC_CROSS_RDC"}

_job: Dict[str, Any] = {"id": None, "thread": None, "cancel": None}
_job_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════
#  what a run may use, and whether it may start
# ═══════════════════════════════════════════════════════════════════════════
def running_run() -> Optional[int]:
    with _job_lock:
        t = _job["thread"]
        return _job["id"] if t is not None and t.is_alive() else None


def defaults() -> Dict[str, Any]:
    """The saved allocation settings, as the run form's starting point. The
    warehouse rule is shown but never pre-selected."""
    eff = b2b_settings.effective()
    return {k: (int(eff[s]) if k == "min_qty" else eff[s]) for k, s in SETTING_OF.items()}


def warehouses() -> Dict[str, Any]:
    """Which warehouses hold bins and which have stores — what the warehouse
    rule decides between."""
    with data_engine.connect() as conn:
        bins = {r[0] or "(none)": {"rows": int(r[1]), "pcs": int(r[2] or 0)} for r in conn.execute(text(f"""
            SELECT BIN_RDC, COUNT(*), SUM(CAST(QTY AS BIGINT)) FROM dbo.{S.BIN_MASTER}
             GROUP BY BIN_RDC ORDER BY BIN_RDC"""))}
        stores = {r[0] or "(none)": int(r[1]) for r in conn.execute(text(f"""
            SELECT RDC, COUNT(*) FROM dbo.{S.STORE_MASTER} GROUP BY RDC ORDER BY RDC"""))}
    return {"bins": bins, "stores": stores,
            "stores_without_bins": {w: n for w, n in stores.items() if w not in bins}}


def gates() -> Dict[str, Any]:
    """Every reason a run may not start now, and the warnings it would carry."""
    from app.services import b2b_mbq_service as M
    from app.services import b2b_upload_service as U
    S.ensure_tables()
    block, warn = [], []
    build = M.latest_done()
    fresh = M.freshness(build)
    if not build:
        block.append("The demand table has not been built. Build MBQ first.")
    elif fresh["state"] == "stale":
        block.append("The demand table is out of date: " + " ".join(fresh["reasons"]) + " Rebuild it first.")
    elif fresh["state"] == "aging":
        warn.extend(fresh["notes"])
    busy = U.running_job()
    if busy and (U.get(busy) or {}).get("STATUS") == "LOADING":
        block.append(f"Upload {busy} is loading.")
    if M.running_build():
        block.append(f"MBQ build {M.running_build()} is running.")
    if running_run():
        block.append(f"Session {running_run()} is still running.")
    return {"ok": not block, "block": block, "warn": warn, "build": build, "freshness": fresh}


def validate(opts: Dict[str, Any]) -> Dict[str, Any]:
    """Every choice checked; the warehouse rule must be given."""
    clean: Dict[str, Any] = {}
    for k, allowed in CHOICES.items():
        v = str(opts.get(k) or "").strip().upper()
        if k == "cross_rdc" and not v:
            raise ValueError("Choose which warehouse may ship: Own RDC only, Own RDC first, or All RDCs. "
                             "There is no default.")
        if v not in allowed:
            raise ValueError(f"{k} must be one of {', '.join(allowed)}")
        clean[k] = v
    try:
        clean["min_qty"] = int(opts.get("min_qty", 1))
    except (TypeError, ValueError):
        raise ValueError("min_qty must be a whole number") from None
    if clean["min_qty"] < 1:
        raise ValueError("min_qty must be at least 1")
    return clean


# ═══════════════════════════════════════════════════════════════════════════
#  start / run
# ═══════════════════════════════════════════════════════════════════════════
def _update(session_id: int, **cols) -> None:
    if not cols:
        return
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    with data_engine.begin() as conn:
        conn.execute(text(f"UPDATE dbo.{S.SESSION} SET {sets} WHERE SESSION_ID = :id"),
                     {**cols, "id": session_id})


def start_run(opts: Dict[str, Any], dry_run: bool, note: Optional[str], user: Optional[str]) -> int:
    from app.services import b2b_upload_service as U
    o = validate(opts)
    with U._admission:                         # shared with loads and builds
        g = gates()
        if not g["ok"]:
            raise RuntimeError(" ".join(g["block"]))
        build = g["build"]
        settings = {**o, "build_id": build["BUILD_ID"], "upload_id": build.get("UPLOAD_ID"),
                    "workers": ALLOC_WORKERS}
        with data_engine.begin() as conn:
            sid = conn.execute(text(f"""
                INSERT INTO dbo.{S.SESSION}
                    (SESSION_TAG, STATUS, DRY_RUN, BUILD_ID, ALLOC_PRIORITY, ALLOC_MIN_QTY,
                     ALLOC_FILL_MODE, ALLOC_FAIR_BASIS, ALLOC_BIN_PICK, ALLOC_CROSS_RDC,
                     SETTINGS_JSON, NOTE, PROGRESS, PROGRESS_PCT, CREATED_BY)
                OUTPUT INSERTED.SESSION_ID
                VALUES (CONVERT(NVARCHAR(40), SYSDATETIME(), 120), 'RUNNING', :dry, :b, :p, :m,
                        :f, :fb, :bp, :x, :s, :note, 'Starting', 0, :by)"""),
                {"dry": 1 if dry_run else 0, "b": build["BUILD_ID"], "p": o["priority"],
                 "m": o["min_qty"], "f": o["fill_mode"],
                 "fb": o["fair_basis"] if o["fill_mode"] == "ROUND_ROBIN" else None,
                 "bp": o["bin_pick"], "x": o["cross_rdc"], "s": json.dumps(settings),
                 "note": (note or "")[:200] or None, "by": user}).scalar()
        ev = threading.Event()
        t = threading.Thread(target=_run, args=(sid, o, dry_run, ev, user, g["warn"]),
                             name=f"b2b-run-{sid}", daemon=True)
        with _job_lock:
            _job.update(id=sid, thread=t, cancel=ev)
        t.start()
    return sid


def cancel(session_id: int) -> bool:
    with _job_lock:
        if _job["id"] != session_id or not (_job["thread"] and _job["thread"].is_alive()):
            return False
        _job["cancel"].set()
    return True


def _run(sid: int, o: Dict[str, Any], dry_run: bool, ev: threading.Event, user: Optional[str],
         warnings: List[str]) -> None:
    t0 = time.time()
    steps: Dict[str, float] = {}

    def progress(p):
        if p.get("phase") == "categories":
            total = max(p["total"], 1)
            pct = 3 + int(p["done"] / total * 80)
            _update(sid, PROGRESS_PCT=pct, PROGRESS=json.dumps({
                "text": f"{p['done']} of {p['total']} categories" + (f" · last {p.get('cat')}" if p.get("cat") else ""),
                **{k: p.get(k) for k in ("done", "total", "lines", "units", "cross", "candidates", "candidates_total")}})[:300])

    try:
        r = E.allocate_all(LIVE, o, workers=ALLOC_WORKERS, progress=progress, cancelled=ev.is_set)
        steps["allocate"] = round(time.time() - t0, 1)
        checks = check_in_memory(r, o)
        summary = _summary(r, warnings)
        if ev.is_set():
            raise E.Cancelled()
        if not dry_run:
            _update(sid, PROGRESS_PCT=85, PROGRESS=json.dumps({"text": "Writing lines, picks and leftovers"}))
            t1 = time.time()
            stored, left = _write(sid, r, o, ev)
            steps["write"] = round(time.time() - t1, 1)
            checks = stored                       # the stored rows are what counts
            summary["leftovers"] = left
        passed = not any(c["level"] == "block" for c in checks)
        took = round(time.time() - t0, 1)
        s = r["stats"]
        _update(sid, STATUS="DONE", SUPPLY_UNITS=r["supply_units"], UNITS_ALLOCATED=r["allocated_units"],
                UNITS_LEFT=r["supply_left"], CROSS_RDC_UNITS=s["cross_rdc_units"],
                ART_LINES=len(r["lines"]), BIN_LINES=len(r["picks"]),
                UNALLOC_LINES=sum(v["rows"] for v in summary.get("leftovers", {}).values()) if not dry_run else None,
                STORES_SERVED=r["stores_served"], ARTS_USED=r["arts_used"],
                SKIPS_JSON=json.dumps(s), CHECKS_JSON=json.dumps(checks), CHECKS_PASSED=1 if passed else 0,
                SUMMARY_JSON=json.dumps(summary), STEPS_JSON=json.dumps(steps), WORKERS=r["workers"],
                FINISHED_AT=datetime.now(), DURATION_SEC=took, PROGRESS_PCT=100,
                PROGRESS=json.dumps({"text": ("Dry run: " if dry_run else "")
                                     + f"{r['allocated_units']:,} units on {len(r['lines']):,} lines in {took:,.0f}s"}))
        logger.info(f"[b2b run {sid}] done ({'dry' if dry_run else 'stored'}) {r['allocated_units']:,} units, "
                    f"{len(r['lines']):,} lines, checks {'pass' if passed else 'FAIL'}, {took}s, by {user}")
    except E.Cancelled:
        _update(sid, STATUS="CANCELLED", FINISHED_AT=datetime.now(), DURATION_SEC=round(time.time() - t0, 1),
                PROGRESS=json.dumps({"text": "Cancelled. Nothing was written."}))
    except Exception as e:
        logger.exception(f"[b2b run {sid}] failed")
        _update(sid, STATUS="FAILED", FINISHED_AT=datetime.now(), DURATION_SEC=round(time.time() - t0, 1),
                PROGRESS=json.dumps({"text": "Failed. Nothing was written."}),
                ERROR=f"{e}\n\n{traceback.format_exc()[-1500:]}")


def _summary(r: Dict[str, Any], warnings: List[str]) -> Dict[str, Any]:
    by_rule = r["stats"]
    lines = r["lines"]
    per_cat = r["per_category"]
    span = len(r["picks"]) - len(lines)
    return {
        "warnings": warnings,
        "multi_bin_lines": span,
        "unfulfilled": r["unfulfilled"],
        "workers": r["workers"], "seconds": r["total_sec"],
        "top_categories": sorted(per_cat, key=lambda c: -c["units"])[:25],
        "slowest_categories": sorted(per_cat, key=lambda c: -(c["read_sec"] + c["run_sec"]))[:5],
        "skips": {k: by_rule[k] for k in ("blocked_art_empty", "blocked_req_full", "blocked_no_req",
                                          "below_min_qty", "blocked_no_rdc")},
    }


# ═══════════════════════════════════════════════════════════════════════════
#  G9 — the balance checks
# ═══════════════════════════════════════════════════════════════════════════
def _c(code, level, title, detail="", *, count=None, units=None, sample=None):
    return {"code": code, "level": level, "title": title, "detail": detail,
            "count": count, "units": units, "sample": [str(s) for s in (sample or [])][:25]}


def check_in_memory(r: Dict[str, Any], o: Dict[str, Any]) -> List[Dict[str, Any]]:
    """G9 on the engine's result, before anything is written (and the only
    checks a dry run gets)."""
    lines, picks = r["lines"], r["picks"]
    out = []
    a, p = sum(x["ALLOC_QTY"] for x in lines), sum(x["QTY"] for x in picks)
    out.append(_c("G9_PICKS", "ok" if a == p and not r["unfulfilled"] else "block",
                  "Pick-list units equal allocated units" if a == p else
                  f"Pick-list units ({p:,}) differ from allocated units ({a:,})",
                  f"{a:,} allocated · {p:,} on the pick list · {r['unfulfilled']:,} untraceable"))
    grp: Dict[tuple, list] = {}
    for x in lines:
        g = grp.setdefault((x["STORE_CODE"], x["MAJ_CAT"], x["BIN_SIZE"]), [0, x["REQ_CAP"]])
        g[0] += x["ALLOC_QTY"]
    over = [k for k, (s, cap) in grp.items() if s > cap]
    out.append(_c("G9_REQ", "block" if over else "ok",
                  f"{len(over):,} store × category × size group(s) over their REQ" if over
                  else "No store gets more than its REQ for a category + size",
                  count=len(over), sample=[" / ".join(str(v) for v in k) for k in over]))
    held: Dict[tuple, int] = {}
    for x in picks:
        held[(x["BIN"], x["ART"])] = x["BIN_QTY"]
    took: Dict[tuple, int] = {}
    for x in picks:
        took[(x["BIN"], x["ART"])] = took.get((x["BIN"], x["ART"]), 0) + x["QTY"]
    overbin = [k for k, v in took.items() if v > held[k]]
    out.append(_c("G9_BINS", "block" if overbin else "ok",
                  f"{len(overbin):,} bin(s) asked for more than they hold" if overbin
                  else "No bin gives more than it holds",
                  count=len(overbin), sample=[f"{b} · {a}" for b, a in overbin]))
    overline = [x for x in lines if x["ALLOC_QTY"] > x["SHORTFALL"]]
    out.append(_c("G9_SHORT", "block" if overline else "ok",
                  f"{len(overline):,} line(s) over the store's shortfall" if overline
                  else "No line is more than the store is short",
                  count=len(overline), sample=[f"{x['STORE_CODE']} · {x['ART']}" for x in overline]))
    if o["cross_rdc"] == "SAME":
        cross = [x for x in picks if x["STORE_RDC"] != x["BIN_RDC"]]
        out.append(_c("G9_RDC", "block" if cross else "ok",
                      f"{len(cross):,} pick(s) cross warehouses under Own RDC only" if cross
                      else "Every pick is from the store's own warehouse", count=len(cross)))
    return out


def _check_stored(cur, sid: int, o: Dict[str, Any]) -> List[Dict[str, Any]]:
    """G9 on the rows just written, inside the write transaction."""
    def one(sql):
        cur.execute(sql, sid) if "?" in sql else cur.execute(sql)
        return cur.fetchone()

    out = []
    r = one(f"""SELECT (SELECT ISNULL(SUM(CAST(ALLOC_QTY AS BIGINT)), 0) FROM dbo.{S.ALLOC} WHERE SESSION_ID = {sid}),
                       (SELECT ISNULL(SUM(CAST(QTY AS BIGINT)), 0) FROM dbo.{S.BIN_PLAN}
                         WHERE SESSION_ID = {sid} AND ROW_TYPE = N'ALLOC')""")
    out.append(_c("G9_PICKS", "ok" if r[0] == r[1] else "block",
                  "Pick-list units equal allocated units" if r[0] == r[1] else
                  f"Pick-list units ({int(r[1]):,}) differ from allocated units ({int(r[0]):,})",
                  f"{int(r[0]):,} units allocated, {int(r[1]):,} on the pick list (stored rows)"))
    n = one(f"""SELECT COUNT(*) FROM (SELECT STORE_CODE, MAJ_CAT, BIN_SIZE, MAX(REQ_CAP) C, SUM(ALLOC_QTY) G
                  FROM dbo.{S.ALLOC} WHERE SESSION_ID = {sid} GROUP BY STORE_CODE, MAJ_CAT, BIN_SIZE) x
                 WHERE x.G > x.C""")[0]
    out.append(_c("G9_REQ", "block" if n else "ok",
                  f"{n:,} store × category × size group(s) over their REQ" if n
                  else "No store gets more than its REQ for a category + size", count=n))
    n = one(f"""SELECT COUNT(*) FROM (SELECT BIN, ART, SUM(QTY) Q FROM dbo.{S.BIN_PLAN}
                  WHERE SESSION_ID = {sid} AND ROW_TYPE = N'ALLOC' GROUP BY BIN, ART) p
                  JOIN (SELECT BIN, ART, SUM(QTY) H FROM dbo.{S.BIN_MASTER} GROUP BY BIN, ART) b
                    ON b.BIN = p.BIN AND b.ART = p.ART WHERE p.Q > b.H""")[0]
    out.append(_c("G9_BINS", "block" if n else "ok",
                  f"{n:,} bin(s) asked for more than they hold" if n else "No bin gives more than it holds",
                  count=n))
    n = one(f"SELECT COUNT(*) FROM dbo.{S.ALLOC} WHERE SESSION_ID = {sid} AND ALLOC_QTY > SHORTFALL")[0]
    out.append(_c("G9_SHORT", "block" if n else "ok",
                  f"{n:,} line(s) over the store's shortfall" if n
                  else "No line is more than the store is short", count=n))
    # Picks + leftovers reconcile to Bin Master exactly, bin by bin.
    r = one(f"""SELECT COUNT(*), ISNULL(SUM(ABS(b.H - ISNULL(p.Q, 0))), 0)
                  FROM (SELECT BIN, ART, SUM(QTY) H FROM dbo.{S.BIN_MASTER} GROUP BY BIN, ART HAVING SUM(QTY) > 0) b
                  FULL JOIN (SELECT BIN, ART, SUM(QTY) Q FROM dbo.{S.BIN_PLAN} WHERE SESSION_ID = {sid}
                              GROUP BY BIN, ART) p ON p.BIN = b.BIN AND p.ART = b.ART
                 WHERE ISNULL(b.H, 0) <> ISNULL(p.Q, 0)""")
    out.append(_c("G9_RECONCILE", "block" if r[0] else "ok",
                  f"{int(r[0]):,} bin(s) where picks + leftovers ≠ Bin Master" if r[0]
                  else "Picks + leftovers add up to Bin Master, bin by bin",
                  units=float(r[1]) if r[0] else None, count=int(r[0])))
    if o["cross_rdc"] == "SAME":
        n = one(f"""SELECT COUNT(*) FROM dbo.{S.BIN_PLAN} WHERE SESSION_ID = {sid} AND ROW_TYPE = N'ALLOC'
                     AND ISNULL(STORE_RDC, N'') <> ISNULL(BIN_RDC, N'')""")[0]
        out.append(_c("G9_RDC", "block" if n else "ok",
                      f"{n:,} pick(s) cross warehouses under Own RDC only" if n
                      else "Every pick is from the store's own warehouse", count=n))
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  writing — one transaction per session
# ═══════════════════════════════════════════════════════════════════════════
ALLOC_COLS = [("STORE_CODE", "s", 10), ("ST_NM", "s", 100), ("ART", "s", 20), ("SEG", "s", 10),
              ("DIV", "s", 20), ("SUB_DIV", "s", 30), ("MAJ_CAT", "s", 60), ("BIN_SIZE", "s", 20),
              ("SZ", "s", 20), ("SEASON", "s", 20), ("CONT", "f", 0), ("MBQ_ROUNDED", "i", 0),
              ("STK_TTL", "f", 0), ("SHORTFALL", "f", 0), ("ALLOC_SEQ", "i", 0), ("ALLOC_SEQ_GRP", "i", 0),
              ("ART_BIN_QTY", "i", 0), ("ART_BIN_LEFT", "i", 0), ("REQ_CAP", "i", 0),
              ("REQ_CAP_LEFT", "i", 0), ("ALLOC_CUM", "i", 0), ("ALLOC_QTY", "i", 0),
              ("RESIDUAL_SHORT", "f", 0), ("RESIDUAL_SHORT_GRP", "f", 0), ("STILL_SENDABLE", "f", 0)]
PLAN_COLS = [("ROW_TYPE", "s", 8), ("BIN", "s", 20), ("ART", "s", 20), ("STORE_CODE", "s", 10),
             ("ST_NM", "s", 100), ("STORE_RDC", "s", 10), ("BIN_RDC", "s", 10), ("SEG", "s", 10),
             ("DIV", "s", 20), ("SUB_DIV", "s", 30), ("MAJ_CAT", "s", 60), ("BIN_SIZE", "s", 20),
             ("SZ", "s", 20), ("SEASON", "s", 20), ("BIN_QTY", "i", 0), ("PICKED_QTY", "i", 0),
             ("QTY", "i", 0), ("ALLOC_SEQ", "i", 0), ("PICK_SEQ", "i", 0), ("BIN_QTY_LEFT", "i", 0),
             ("STORE_ALLOC_QTY", "i", 0)]


def _insert(cur, table: str, cols, rows: List[dict], sid: int, ev: threading.Event) -> None:
    import pyodbc
    names = ["SESSION_ID"] + [c for c, _, _ in cols]
    sizes = [(pyodbc.SQL_INTEGER, 0, 0)] + [
        (pyodbc.SQL_WVARCHAR, w, 0) if k == "s" else (pyodbc.SQL_DOUBLE, 0, 0) if k == "f"
        else (pyodbc.SQL_BIGINT, 0, 0) for _, k, w in cols]
    sql = (f"INSERT INTO dbo.[{table}] ({', '.join(f'[{n}]' for n in names)}) "
           f"VALUES ({', '.join('?' for _ in names)})")
    cur.fast_executemany = True
    for i in range(0, len(rows), BATCH):
        if ev.is_set():
            raise E.Cancelled()
        part = [(sid, *(x.get(c) for c, _, _ in cols)) for x in rows[i:i + BATCH]]
        cur.setinputsizes(sizes)
        cur.executemany(sql, part)


def _write(sid: int, r: Dict[str, Any], o: Dict[str, Any], ev: threading.Event):
    """Lines, picks, leftovers and the stored-row checks in one transaction."""
    raw = data_engine.raw_connection()
    try:
        cur = raw.cursor()
        _insert(cur, S.ALLOC, ALLOC_COLS, r["lines"], sid, ev)
        _insert(cur, S.BIN_PLAN, PLAN_COLS, r["picks"], sid, ev)
        cur.fast_executemany = False
        _unallocated(cur, sid)
        checks = _check_stored(cur, sid, o)
        cur.execute(f"""SELECT LEFT_REASON, COUNT(*), SUM(CAST(QTY AS BIGINT)) FROM dbo.{S.BIN_PLAN}
                         WHERE SESSION_ID = ? AND ROW_TYPE = N'UNALLOC' GROUP BY LEFT_REASON""", sid)
        left = {row[0]: {"rows": int(row[1]), "pcs": int(row[2] or 0)} for row in cur.fetchall()}
        if ev.is_set():
            raise E.Cancelled()
        raw.commit()
        return checks, left
    except BaseException:
        try:
            raw.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            raw.close()
        except Exception:
            pass


def _unallocated(cur, sid: int) -> None:
    """Every bin position still holding stock, and why — the tool's
    build_unallocated, with the bin's warehouse kept."""
    cur.execute(f"""
        WITH held AS (
            SELECT BIN, ART, MAX(BIN_RDC) BIN_RDC, MAX(SEG) SEG, MAX(DIV) DIV, MAX(SUB_DIV) SUB_DIV,
                   MAX(MAJ_CAT) MAJ_CAT, MAX([SIZE]) BIN_SIZE, MAX(SEASON) SEASON, SUM(QTY) BIN_QTY
              FROM dbo.{S.BIN_MASTER} GROUP BY BIN, ART),
        picked AS (
            SELECT BIN, ART, SUM(QTY) PICKED_QTY FROM dbo.{S.BIN_PLAN}
             WHERE SESSION_ID = ? AND ROW_TYPE = N'ALLOC' GROUP BY BIN, ART),
        demand AS (
            SELECT ART, SUM(CASE WHEN SHORTFALL > 0 THEN 1 ELSE 0 END) STORES_WANTING,
                   CONVERT(DECIMAL(18,4), SUM(CASE WHEN SHORTFALL > 0 THEN SHORTFALL ELSE 0 END)) TOTAL_SHORTFALL
              FROM dbo.{S.ART_MBQ} GROUP BY ART),
        allocated AS (SELECT DISTINCT ART FROM dbo.{S.ALLOC} WHERE SESSION_ID = ?),
        req_any AS (
            SELECT MAJ_CAT, ISNULL([SIZE], N'') SZ_KEY, SUM(CASE WHEN REQ > 0 THEN REQ ELSE 0 END) REQ_POS
              FROM dbo.{S.REQ} GROUP BY MAJ_CAT, ISNULL([SIZE], N'')),
        req_master AS (
            SELECT r.MAJ_CAT, ISNULL(r.[SIZE], N'') SZ_KEY, SUM(CASE WHEN r.REQ > 0 THEN r.REQ ELSE 0 END) REQ_POS
              FROM dbo.{S.REQ} r JOIN dbo.{S.STORE_MASTER} s ON s.STORE_CODE = r.STORE_CODE
             GROUP BY r.MAJ_CAT, ISNULL(r.[SIZE], N'')),
        req_cat AS (SELECT MAJ_CAT FROM dbo.{S.REQ} GROUP BY MAJ_CAT)
        INSERT INTO dbo.{S.BIN_PLAN} WITH (TABLOCK)
            (SESSION_ID, ROW_TYPE, BIN, ART, STORE_CODE, BIN_RDC, SEG, DIV, SUB_DIV, MAJ_CAT,
             BIN_SIZE, SZ, SEASON, BIN_QTY, PICKED_QTY, QTY, LEFT_REASON, STORES_WANTING, TOTAL_SHORTFALL)
        SELECT  ?, N'UNALLOC', h.BIN, h.ART, N'', h.BIN_RDC, h.SEG, h.DIV, h.SUB_DIV, h.MAJ_CAT,
                h.BIN_SIZE, h.BIN_SIZE, h.SEASON, h.BIN_QTY, ISNULL(p.PICKED_QTY, 0),
                h.BIN_QTY - ISNULL(p.PICKED_QTY, 0),
                CASE WHEN ISNULL(p.PICKED_QTY, 0) > 0 THEN N'PARTIAL'
                     WHEN d.ART IS NULL THEN
                         CASE WHEN rc.MAJ_CAT IS NULL        THEN N'NOT_IN_REQ'
                              WHEN ISNULL(ra.REQ_POS, 0) = 0 THEN N'NO_SEASON_REQ'
                              WHEN ISNULL(rm.REQ_POS, 0) = 0 THEN N'NO_STORE_MASTER'
                              ELSE N'NO_STORE_NEED' END
                     WHEN ISNULL(d.STORES_WANTING, 0) = 0     THEN N'NO_SHORTFALL'
                     ELSE N'REQ_CAP_FULL' END,
                d.STORES_WANTING, d.TOTAL_SHORTFALL
          FROM held h
          LEFT JOIN picked p      ON p.BIN = h.BIN AND p.ART = h.ART
          LEFT JOIN demand d      ON d.ART = h.ART
          LEFT JOIN allocated a   ON a.ART = h.ART
          LEFT JOIN req_cat rc    ON rc.MAJ_CAT = h.MAJ_CAT
          LEFT JOIN req_any ra    ON ra.MAJ_CAT = h.MAJ_CAT AND ra.SZ_KEY = ISNULL(h.BIN_SIZE, N'')
          LEFT JOIN req_master rm ON rm.MAJ_CAT = h.MAJ_CAT AND rm.SZ_KEY = ISNULL(h.BIN_SIZE, N'')
         WHERE h.BIN_QTY - ISNULL(p.PICKED_QTY, 0) > 0""", sid, sid, sid)
    while cur.nextset():
        pass


# ═══════════════════════════════════════════════════════════════════════════
#  reading and deleting sessions
# ═══════════════════════════════════════════════════════════════════════════
def _row(r) -> Dict[str, Any]:
    d = dict(r._mapping)
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        elif isinstance(v, Decimal):
            d[k] = float(v)
    for k, name in (("SETTINGS_JSON", "settings"), ("SKIPS_JSON", "skips"), ("CHECKS_JSON", "checks"),
                    ("SUMMARY_JSON", "summary"), ("STEPS_JSON", "steps"), ("PROGRESS", "progress")):
        if k in d:
            v = d.pop(k)
            try:
                d[name] = json.loads(v) if v else None
            except ValueError:
                d[name] = {"text": v} if name == "progress" else None
    return d


def get(session_id: int) -> Optional[Dict[str, Any]]:
    S.ensure_tables()
    with data_engine.connect() as conn:
        r = conn.execute(text(f"SELECT * FROM dbo.{S.SESSION} WHERE SESSION_ID = :id"),
                         {"id": session_id}).fetchone()
    if r is None:
        return None
    d = _row(r)
    if d["STATUS"] == "RUNNING" and running_run() != session_id:
        _update(session_id, STATUS="FAILED", PROGRESS=json.dumps({"text": "Interrupted"}),
                ERROR="The server stopped while this session was running. Its rows were never "
                      "committed, so nothing was written.")
        return get(session_id)
    return d


def recent(limit: int = 15) -> List[Dict[str, Any]]:
    S.ensure_tables()
    with data_engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT TOP (:n) SESSION_ID, SESSION_TAG, STATUS, DRY_RUN, BUILD_ID, ALLOC_PRIORITY,
                   ALLOC_MIN_QTY, ALLOC_FILL_MODE, ALLOC_FAIR_BASIS, ALLOC_BIN_PICK, ALLOC_CROSS_RDC,
                   SUPPLY_UNITS, UNITS_ALLOCATED, UNITS_LEFT, CROSS_RDC_UNITS, ART_LINES, BIN_LINES,
                   STORES_SERVED, CHECKS_PASSED, NOTE, CREATED_BY, CREATED_AT, DURATION_SEC
              FROM dbo.{S.SESSION} ORDER BY SESSION_ID DESC"""), {"n": limit}).fetchall()
    return [_row(r) for r in rows]


def delete(session_id: int, user: Optional[str]) -> Dict[str, int]:
    """Remove one session from all three tables, in one transaction."""
    if running_run() == session_id:
        raise RuntimeError("That session is still running. Cancel it first.")
    removed: Dict[str, int] = {}
    with data_engine.begin() as conn:
        for tbl in (S.BIN_PLAN, S.ALLOC, S.SESSION):
            removed[tbl] = conn.execute(text(f"DELETE FROM dbo.{tbl} WHERE SESSION_ID = :id"),
                                        {"id": session_id}).rowcount
    if not removed.get(S.SESSION):
        raise ValueError(f"Session {session_id} not found")
    logger.info(f"[b2b run] session {session_id} deleted by {user}: {removed}")
    return removed


def page() -> Dict[str, Any]:
    """Everything the Run Allocation page shows."""
    g = gates()
    rid = running_run()
    return {"defaults": defaults(), "choices": {k: list(v) for k, v in CHOICES.items()},
            "gates": {"ok": g["ok"], "block": g["block"], "warn": g["warn"]},
            "build": g["build"], "freshness": g["freshness"], "warehouses": warehouses(),
            "running": get(rid) if rid else None, "sessions": recent(12)}
