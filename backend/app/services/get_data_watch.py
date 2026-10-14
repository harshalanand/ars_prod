"""
Get Data — the "When the source changes" trigger (GD_JOB.TRIGGER_TYPE 'on_change').

A watched job is checked every few minutes. A check asks Snowflake when each
watched table last changed (SYSTEM$LAST_CHANGE_COMMIT_TIME — metadata only, it
does not resume a warehouse) and the job runs when ALL of these hold:
  • the newest change differs from what the last successful run loaded
    (GD_JOB.SYNCED_TOKEN, written by the run from a token it read before its
    data query — so a change made during a run triggers another one),
  • the source has been quiet for `quiet_min` (an upstream reload has finished),
  • now is inside the IST window and on an allowed weekday,
  • the last run started at least `min_gap_min` ago,
  • no run is going.
A change outside the window waits for it to open. An optional daily safety run
(SCHEDULE_CONFIG / NEXT_RUN_AT, fired by the scheduler like a schedule) loads
anyway unless the job loaded successfully in the last SAFETY_SKIP_HOURS.

Views are watched through their base tables. Asked about a view, Snowflake
missed changes in tables behind nested views (2026-10-14: V_ARS_STORE_STK_DATA
answered 11:26 while DIM_PRODUCT_V2 under it changed at 11:33), so a job lists
tables, found with GET_OBJECT_REFERENCES when it is edited (that lookup does
use the warehouse, briefly — the checks never do).

The scheduler (get_data_scheduler) claims due checks and starts the runs; this
module holds the settings, the decision and the Snowflake reads, and never
imports the sync service. Spec: frontend/public/docs/manual/get_data.md.
"""
import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine
from app.services import get_data_snowflake as gsf
from app.services.get_data_schema import JOB_TABLE, RUN_TABLE
from app.services.get_data_snowflake import GetDataError

IST = timedelta(hours=5, minutes=30)
DEFAULTS = {"check_every_min": 5, "quiet_min": 10, "min_gap_min": 60}
LIMITS = {"check_every_min": (1, 60), "quiet_min": (0, 240), "min_gap_min": (0, 1440)}
MAX_TABLES = 50
SAFETY_SKIP_HOURS = 23     # a safety run is skipped if the job loaded within this
SYNCED_MARGIN_MIN = 15     # a run that started this long after a change already holds it
LINEAGE_MAX_OBJECTS = 200
_EPOCH = datetime(1970, 1, 1)
WD = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
STARTING_LEAD = "Starting a run"     # the scheduler swaps it for "Run #N started"
RUNNING_LEAD = "A run is in progress"


# ── Settings ─────────────────────────────────────────────────────────────────
def _hhmm(v: Any, label: str) -> Optional[str]:
    s = str(v or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise GetDataError(f"{label} '{s}' is not a valid time — use HH:MM (24-hour, IST).")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def clean_watch_config(raw: Any) -> Dict[str, Any]:
    """Validate the on-change settings. Tables are DATABASE.SCHEMA.NAME."""
    if not isinstance(raw, dict):
        raise GetDataError("'When the source changes' needs its settings.")
    tables: List[str] = []
    for t in raw.get("tables") or []:
        fq = gsf.normalize_object(t)
        if fq not in tables:
            tables.append(fq)
    if not tables:
        raise GetDataError("Pick at least one table to watch.")
    if len(tables) > MAX_TABLES:
        raise GetDataError(f"Watch {MAX_TABLES} tables at most.")
    out: Dict[str, Any] = {"tables": tables}
    labels = {"check_every_min": "Check every", "quiet_min": "Wait until quiet",
              "min_gap_min": "At most once every"}
    for k, (lo, hi) in LIMITS.items():
        v = raw.get(k)
        try:
            n = DEFAULTS[k] if v is None or v == "" else int(v)
        except (TypeError, ValueError):
            raise GetDataError(f"{labels[k]} must be a number of minutes.")
        if not lo <= n <= hi:
            raise GetDataError(f"{labels[k]} must be between {lo} and {hi} minutes.")
        out[k] = n
    ws = _hhmm(raw.get("window_start"), "Window start")
    we = _hhmm(raw.get("window_end"), "Window end")
    if bool(ws) != bool(we):
        raise GetDataError("Give both window times, or leave both empty to allow any time.")
    if ws and ws == we:
        raise GetDataError("The window start and end can't be the same time.")
    out["window_start"], out["window_end"] = ws, we
    wds = raw.get("weekdays")
    wds = sorted({int(w) % 7 for w in (range(7) if wds is None else wds)})
    if not wds:
        raise GetDataError("Pick at least one weekday.")
    out["weekdays"] = wds
    out["safety_time"] = _hhmm(raw.get("safety_time"), "Safety run time")
    return out


def safety_schedule(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The safety run as a schedule config (stored in SCHEDULE_CONFIG)."""
    if not cfg.get("safety_time"):
        return None
    wds = cfg.get("weekdays") or list(range(7))
    if len(wds) == 7:
        return {"freq": "daily", "times": [cfg["safety_time"]]}
    return {"freq": "weekly", "weekdays": wds, "times": [cfg["safety_time"]]}


def describe(cfg: Optional[Dict[str, Any]]) -> str:
    """'06:00–22:00 · Mon–Sat' — for messages."""
    if not cfg:
        return ""
    win = f"{cfg['window_start']}–{cfg['window_end']}" if cfg.get("window_start") else "any time"
    wds = cfg.get("weekdays") or list(range(7))
    if len(wds) == 7:
        return win
    return f"{win} · {', '.join(WD[w] for w in wds)}"


# ── Time helpers ─────────────────────────────────────────────────────────────
def token_time(token: Any) -> Optional[datetime]:
    """SYSTEM$LAST_CHANGE_COMMIT_TIME is nanoseconds since the epoch (UTC)."""
    try:
        return (_EPOCH + timedelta(microseconds=int(token) // 1000)).replace(microsecond=0)
    except (TypeError, ValueError, OverflowError):
        return None


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_window(cfg: Dict[str, Any], now_ist: datetime) -> bool:
    wds = set(cfg.get("weekdays") or range(7))
    if not cfg.get("window_start"):
        return now_ist.weekday() in wds
    s, e = _mins(cfg["window_start"]), _mins(cfg["window_end"])
    m = now_ist.hour * 60 + now_ist.minute
    if s < e:
        return now_ist.weekday() in wds and s <= m < e
    # Overnight (22:00–06:00): the part after midnight belongs to the day it opened.
    if m >= s:
        return now_ist.weekday() in wds
    return m < e and (now_ist - timedelta(days=1)).weekday() in wds


def next_window_open(cfg: Dict[str, Any], now_ist: datetime) -> Optional[datetime]:
    wds = set(cfg.get("weekdays") or range(7))
    start = _mins(cfg["window_start"]) if cfg.get("window_start") else 0
    day0 = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(8):
        day = day0 + timedelta(days=d)
        t = day + timedelta(minutes=start)
        if day.weekday() in wds and t > now_ist:
            return t
    return None


def _when(dt_utc: Optional[datetime], now_utc: datetime) -> str:
    """IST, short: '14:05' today, 'Thu 06:00' within a week, else '16 Oct 06:00'."""
    if not dt_utc:
        return "—"
    t, n = dt_utc + IST, now_utc + IST
    if t.date() == n.date():
        return t.strftime("%H:%M")
    if abs((t.date() - n.date()).days) < 7:
        return t.strftime("%a %H:%M")
    return t.strftime("%d %b %H:%M")


def short_table(fq: Optional[str]) -> str:
    return str(fq or "").split(".")[-1]


def trigger_label(table: Optional[str], change_at: Optional[datetime]) -> str:
    """GD_RUN.TRIGGERED_BY for a change-triggered run."""
    at = (change_at + IST).strftime("%d %b %H:%M") if change_at else "?"
    return f"change · {short_table(table)} {at}"[:128]


# ── The decision ─────────────────────────────────────────────────────────────
def decide(cfg: Dict[str, Any], *, token: str, change_at: datetime, change_table: str,
           synced_token: Optional[str], last_ok_start: Optional[datetime],
           last_run_start: Optional[datetime], running: bool,
           now: datetime, sf_now: datetime) -> Tuple[bool, str, str, Optional[str]]:
    """One check of one job. Times are naive UTC: `now` is SQL Server's clock
    (window, gap — the run times are SQL Server's), `sf_now` is Snowflake's
    (quiet — the change times are Snowflake's). Returns (run?, state,
    message, synced_init). synced_init seeds SYNCED_TOKEN for a job that has
    none yet when its last successful run started well after the change."""
    # The state leads, so it survives when the jobs list clips the line.
    what = f"{_when(change_at, now)} ({short_table(change_table)})"
    synced_init = None
    if (synced_token is None and last_ok_start and change_at
            and last_ok_start >= change_at + timedelta(minutes=SYNCED_MARGIN_MIN)):
        synced_init = synced_token = token
    if token == synced_token:
        return False, "up_to_date", f"Up to date · last change {what} is loaded", synced_init
    if running:
        return False, "running", f"{RUNNING_LEAD} · changed {what}", synced_init
    quiet = int(cfg.get("quiet_min") or 0)
    quiet_until = change_at + timedelta(minutes=quiet)
    if sf_now < quiet_until:
        about = quiet_until + (now - sf_now)          # on SQL Server's clock, for display
        return (False, "waiting_quiet",
                f"Waiting for {quiet} quiet min (about {_when(about, now)}) · changed {what}", synced_init)
    if not in_window(cfg, now + IST):
        opens = next_window_open(cfg, now + IST)
        when = _when(opens - IST, now) if opens else "never — check the weekdays"
        return False, "waiting_window", f"Waits for the window (opens {when}) · changed {what}", synced_init
    gap = int(cfg.get("min_gap_min") or 0)
    if gap and last_run_start and now < last_run_start + timedelta(minutes=gap):
        return (False, "waiting_gap",
                f"Next run allowed {_when(last_run_start + timedelta(minutes=gap), now)} · changed {what}",
                synced_init)
    return True, "started", f"{STARTING_LEAD} · changed {what}", synced_init


def safety_due(last_ok_start: Optional[datetime], now: datetime) -> Tuple[bool, str]:
    if last_ok_start and last_ok_start >= now - timedelta(hours=SAFETY_SKIP_HOURS):
        return False, f"Safety run skipped — loaded at {_when(last_ok_start, now)}"
    return True, "Safety run — no successful load in the last 23 hours"


# ── Snowflake ────────────────────────────────────────────────────────────────
def read_tokens(cur, tables: List[str]) -> Tuple[Dict[str, Optional[int]], Dict[str, str]]:
    """Last-change token per table, in one statement; one by one if that fails
    so a dropped or hidden table is named rather than failing the rest."""
    toks: Dict[str, Optional[int]] = {}
    errs: Dict[str, str] = {}
    if not tables:
        return toks, errs
    try:
        cur.execute("SELECT " + ", ".join("SYSTEM$LAST_CHANGE_COMMIT_TIME(%s)" for _ in tables),
                    tuple(tables))
        for t, v in zip(tables, cur.fetchone()):
            toks[t] = int(v) if v is not None else None
        return toks, errs
    except Exception:
        pass
    for t in tables:
        try:
            cur.execute("SELECT SYSTEM$LAST_CHANGE_COMMIT_TIME(%s)", (t,))
            v = cur.fetchone()[0]
            toks[t] = int(v) if v is not None else None
        except Exception as e:
            toks[t] = None
            errs[t] = gsf.sf_message(e)[:200]
    return toks, errs


def poll(tables: List[str]) -> Tuple[Dict[str, Optional[int]], Dict[str, str], datetime]:
    """Tokens for every table, plus Snowflake's clock (UTC) for the quiet test."""
    conn = gsf.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT SYSDATE()")             # UTC, TIMESTAMP_NTZ
        sf_now = cur.fetchone()[0].replace(microsecond=0)
        toks, errs = read_tokens(cur, tables)
        return toks, errs, sf_now
    finally:
        conn.close()


def job_token(tables: List[str], toks: Dict[str, Optional[int]],
              errs: Dict[str, str]) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """(newest token, its table, error). Any unreadable table is an error: a
    token taken over fewer tables could look like a change and start a run."""
    bad = [t for t in tables if t in errs or toks.get(t) is None]
    if bad:
        why = errs.get(bad[0]) or "no change time returned"
        more = f" (+{len(bad) - 1} more)" if len(bad) > 1 else ""
        return None, None, f"Can't read {short_table(bad[0])}{more}: {why}"
    best = max(tables, key=lambda t: toks[t])
    return toks[best], best, None


def _references(cur, fq: str) -> List[Tuple[str, str]]:
    db, sch, name = fq.split(".")
    cur.execute(f"""
        SELECT REFERENCED_DATABASE_NAME, REFERENCED_SCHEMA_NAME, REFERENCED_OBJECT_NAME,
               REFERENCED_OBJECT_TYPE
        FROM TABLE({db}.INFORMATION_SCHEMA.GET_OBJECT_REFERENCES(
            DATABASE_NAME => %s, SCHEMA_NAME => %s, OBJECT_NAME => %s))
    """, (db, sch, name))
    out = []
    for rdb, rsch, rname, rtype in cur.fetchall():
        try:
            out.append((gsf.normalize_object(f"{rdb}.{rsch}.{rname}"), str(rtype or "").upper()))
        except GetDataError:
            logger.info(f"[get-data] watch: skipped reference {rdb}.{rsch}.{rname} (name not supported)")
    return out


def _kind(cur, fq: str) -> Optional[str]:
    db, sch, name = fq.split(".")
    cur.execute(f"SHOW OBJECTS LIKE '{name}' IN SCHEMA {db}.{sch}")
    cols = [d[0].lower() for d in cur.description]
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        if str(d.get("name", "")).upper() == name:
            return str(d.get("kind") or "TABLE").upper()
    return None


def _is_view(kind: str) -> bool:
    return "VIEW" in kind and "MATERIALIZED" not in kind


def watch_candidates(source_object: Any) -> Dict[str, Any]:
    """The tables behind a source, each with its last change — what the job
    form offers to watch. Walks nested views (GET_OBJECT_REFERENCES)."""
    fq = gsf.normalize_object(source_object)
    notes: List[str] = []
    conn = gsf.connect()
    try:
        cur = conn.cursor()
        kind = _kind(cur, fq)
        if not kind:
            raise GetDataError(f"{fq} was not found, or the Snowflake role can't see it.")
        leaves: Dict[str, List[str]] = {}
        if not _is_view(kind):
            leaves[fq] = []
        else:
            queue, seen = [(fq, [])], {fq}
            while queue:
                obj, path = queue.pop(0)
                try:
                    refs = _references(cur, obj)
                except Exception as e:
                    notes.append(f"Couldn't read what {short_table(obj)} uses: {gsf.sf_message(e)[:150]}")
                    continue
                for ref, rtype in refs:
                    if ref in seen:
                        continue
                    seen.add(ref)
                    if len(seen) > LINEAGE_MAX_OBJECTS:
                        notes.append(f"Stopped after {LINEAGE_MAX_OBJECTS} objects.")
                        queue = []
                        break
                    if _is_view(rtype):
                        queue.append((ref, path + [obj]))
                    else:
                        leaves.setdefault(ref, path + [obj])
            if not leaves and not notes:
                notes.append("No tables found behind this view — add the table to watch by hand.")
        toks, errs = read_tokens(cur, list(leaves))
    finally:
        conn.close()
    items = [{"table": t, "via": [short_table(v) for v in path],
              "last_change": _iso(token_time(toks.get(t))) if toks.get(t) else None,
              "error": errs.get(t)} for t, path in leaves.items()]
    items.sort(key=lambda x: x["last_change"] or "", reverse=True)
    return {"source": fq, "kind": kind, "tables": items, "notes": notes}


def _iso(v: Optional[datetime]) -> Optional[str]:
    return v.isoformat() + "Z" if v else None


# ── Job state (GD_JOB) ───────────────────────────────────────────────────────
def claim_checks() -> List[Dict[str, Any]]:
    """Watched jobs whose check is due, claimed with a guarded UPDATE so one
    worker checks each job per interval (4 uvicorn workers tick every 30 s)."""
    eng = get_data_engine()
    with eng.connect() as c:
        due = c.execute(text(f"""
            SELECT JOB_ID, WATCH_CONFIG FROM {JOB_TABLE}
            WHERE ENABLED = 1 AND TRIGGER_TYPE = 'on_change'
              AND (NEXT_CHECK_AT IS NULL OR NEXT_CHECK_AT <= SYSUTCDATETIME())
        """)).fetchall()
    claimed: List[Tuple[int, Dict[str, Any]]] = []
    for jid, raw in due:
        try:
            cfg = json.loads(raw or "{}")
        except Exception:
            cfg = {}
        every = int(cfg.get("check_every_min") or DEFAULTS["check_every_min"])
        # OUTPUT, not rowcount (rowcount is -1 on a pooled SET NOCOUNT ON connection).
        with eng.begin() as c:
            got = c.execute(text(f"""
                UPDATE {JOB_TABLE} SET NEXT_CHECK_AT = DATEADD(MINUTE, :m, SYSUTCDATETIME())
                OUTPUT INSERTED.JOB_ID
                WHERE JOB_ID = :id AND ENABLED = 1 AND TRIGGER_TYPE = 'on_change'
                  AND (NEXT_CHECK_AT IS NULL OR NEXT_CHECK_AT <= SYSUTCDATETIME())
            """), {"m": every, "id": jid}).fetchone()
        if got:
            claimed.append((int(jid), cfg))
    if not claimed:
        return []
    ids = ", ".join(str(j) for j, _ in claimed)
    with eng.connect() as c:
        rows = c.execute(text(f"""
            SELECT j.JOB_ID, j.JOB_NAME, j.SYNCED_TOKEN, j.LOCK_RUN_ID,
                   (SELECT MAX(r.STARTED_AT) FROM {RUN_TABLE} r
                     WHERE r.JOB_ID = j.JOB_ID AND r.STATUS = 'success') AS LAST_OK_START,
                   (SELECT MAX(r.STARTED_AT) FROM {RUN_TABLE} r
                     WHERE r.JOB_ID = j.JOB_ID AND r.STATUS <> 'skipped') AS LAST_RUN_START
            FROM {JOB_TABLE} j WHERE j.JOB_ID IN ({ids})
        """)).mappings().fetchall()
    cfgs = dict(claimed)
    return [dict(r, cfg=cfgs[int(r["JOB_ID"])]) for r in rows]


def save_check(job_id: int, state: str, message: str, token: Optional[str] = None,
               change_at: Optional[datetime] = None, change_table: Optional[str] = None,
               synced_init: Optional[str] = None) -> None:
    with get_data_engine().begin() as c:
        c.execute(text(f"""
            UPDATE {JOB_TABLE} SET CHECKED_AT = SYSUTCDATETIME(), CHECK_STATE = :st,
                CHECK_MESSAGE = :m,
                CHANGE_TOKEN = COALESCE(:tok, CHANGE_TOKEN),
                CHANGE_AT = COALESCE(:at, CHANGE_AT),
                CHANGE_TABLE = COALESCE(:tbl, CHANGE_TABLE),
                SYNCED_TOKEN = COALESCE(SYNCED_TOKEN, :init)
            WHERE JOB_ID = :id
        """), {"st": state, "m": (message or "")[:400], "tok": token, "at": change_at,
               "tbl": change_table, "init": synced_init, "id": job_id})


def last_ok_start(job_id: int) -> Optional[datetime]:
    with get_data_engine().connect() as c:
        return c.execute(text(f"""
            SELECT MAX(STARTED_AT) FROM {RUN_TABLE} WHERE JOB_ID = :id AND STATUS = 'success'
        """), {"id": job_id}).scalar()
