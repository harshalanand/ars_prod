"""
1SZ ALC — readiness: validate first, then create.

Before a Step 1 build, a Step 2 build or a Step 3 run, `check(scope)` lists
every reason it should not start (block) and every reason its result may be
off (warn), as one checklist the page can show in full:

  sources    the tables the step reads exist and hold rows
  order      no other 1SZ ALC job is running; the steps before it are built,
             and (run) Step 2 is newer than Step 1
  freshness  a source table changed after the build that read it
  rules      the Step 1 rule settings changed after the Step 1 build
  coverage   listed stores × categories that will get a size budget of 0
             (no ARS_GRID_MJ row, no size curve), and the zero-CONT gap
  sessions   (run) the PARKED sessions and what they hold (warn when parked
             longer than STALE_PARKED_DAYS), sessions approved into Pend ALC since
             the latest build, and whether the stored balances are live

Read only and quick: metadata, EXISTS / TOP 1, NOLOCK, and a 3 s lock
timeout so a table being rebuilt elsewhere cannot hang the page. A check
that cannot run says so instead of failing the whole list.

Review: an item with `has_rows` has a list behind its count —
`item_rows(scope, key)` returns it (the same rule the check counts) and
`export_csv(scope, key)` streams all of it. Read only as well.
Spec: frontend/public/docs/manual/sz1_alloc.md §A.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from loguru import logger
from sqlalchemy import text

from app.database.session import data_engine
from app.services import sz1_base_service as B
from app.services import sz1_schema as S
from app.services.sz1_base_service import q

SCOPES = ("build1", "build2", "run")
SCOPE_WORDS = {"build1": "Step 1 build", "build2": "Step 2 build", "run": "Step 3 run"}

LOCK_TIMEOUT_MS = 3000
STALE_PARKED_DAYS = 2           # a session parked longer than this should be approved or discarded

# What each step reads. Step 1 also reads Master_CONT_SZ (company size CONT,
# the zero-CONT gap), so a change there makes Step 1 stale as well.
STEP1_SOURCES = [S.MSA_TOTAL, S.HIERARCHY, S.CONT_SZ]
STEP2_SOURCES = [S.STORE_MASTER, S.GRID_MJ, S.GRID_VAR_ART, S.CONT_SZ, S.MSA_TOTAL, S.HIERARCHY]
IROD_SOURCES = [S.CALC_ST_MAJ_CAT, S.CALC_ST_ART]          # Step 3 reads them live; optional

_SOURCES_BY_SCOPE = {
    "build1": [S.MSA_TOTAL, S.HIERARCHY, S.CONT_SZ],
    "build2": [S.MSA_TOTAL, S.HIERARCHY, S.CONT_SZ, S.STORE_MASTER, S.GRID_MJ, S.GRID_VAR_ART],
    "run": [S.MSA_TOTAL, S.HIERARCHY, S.CONT_SZ, S.STORE_MASTER, S.GRID_MJ, S.GRID_VAR_ART] + IROD_SOURCES,
}

_WHY = {                         # what an empty / missing source breaks, in business words
    S.MSA_TOTAL: "Step 1 takes the warehouse free stock (FNL_Q) from it",
    S.HIERARCHY: "Step 1 keeps only size-applicable categories (SZ_APPLICABLE) from it",
    S.CONT_SZ: "the size contribution (CONT %) of every category comes from it",
    S.STORE_MASTER: "Step 2 takes the listed stores (LISTING = 1) from it",
    S.GRID_MJ: "Step 2 takes each store's MAJ_CAT stock, display and sales from it",
    S.GRID_VAR_ART: "Step 2 takes each store's stock per size from it",
    S.CALC_ST_MAJ_CAT: "I_ROD per store + MAJ_CAT comes from it; without it I_ROD defaults to 1",
    S.CALC_ST_ART: "I_ROD per store + option comes from it; without it the MAJ_CAT value (else 1) is used",
}


# ═══════════════════════════════════════════════════════════════════════════
#  helpers
# ═══════════════════════════════════════════════════════════════════════════
@contextmanager
def _reader():
    """A connection that gives up after a few seconds on a lock instead of
    hanging. LOCK_TIMEOUT is a session setting: reset before the pooled
    connection goes back."""
    with data_engine.connect() as conn:
        conn.execute(text(f"SET LOCK_TIMEOUT {LOCK_TIMEOUT_MS}"))
        try:
            yield conn
        finally:
            try:
                conn.rollback()
                conn.execute(text("SET LOCK_TIMEOUT -1"))
            except Exception:  # noqa: BLE001 — a dead connection is discarded by the pool
                pass


def _ts(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except ValueError:
        return None


def _fmt(v: Any) -> str:
    """12-10 08:30 — day-month and time, the database's clock."""
    d = _ts(v)
    return d.strftime("%d-%m %H:%M") if d else "?"


def _n(v: Any) -> str:
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:,.0f}" if abs(f - round(f)) < 1e-9 else f"{f:,.2f}"


def _busy(e: Exception) -> bool:
    s = str(e)
    return "1222" in s or "Lock request time out" in s


class _List:
    """The checklist being built."""

    def __init__(self) -> None:
        self.items: List[Dict[str, Any]] = []

    def add(self, key: str, group: str, name: str, level: str, detail: str, value: Optional[str] = None) -> None:
        self.items.append({"key": key, "group": group, "name": name, "level": level,
                           "detail": detail, "value": value})

    def skip(self, key: str, group: str, name: str, why: str) -> None:
        self.add(key, group, name, "ok", f"Skipped — {why}.")

    def guard(self, conn, key: str, group: str, name: str, fn) -> None:
        """Run one check; a lock timeout or error becomes a WARN item instead of
        failing the whole list."""
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            if _busy(e):
                self.add(key, group, name, "warn",
                         f"{name}: could not be checked — a table it reads is locked by another job. Check again shortly.")
            else:
                logger.warning(f"[sz1 readiness] {key} failed: {e}")
                self.add(key, group, name, "warn", f"{name}: could not be checked ({str(e)[:160]}).")


def _table_info(conn, names: List[str]) -> Dict[str, Dict[str, Any]]:
    """Per existing dbo table: modify_date and the last INSERT/UPDATE/DELETE
    seen since the server started (sys.dm_db_index_usage_stats: NULL after a
    restart, or when the login may not read the DMV). `changed` = the later."""
    wanted = ", ".join(f"'{t}'" for t in names)
    base = f"""
        SELECT t.name, t.modify_date, {{upd}} AS LAST_WRITE
          FROM sys.tables t
         WHERE t.name IN ({wanted}) AND SCHEMA_NAME(t.schema_id) = 'dbo'"""
    dmv = """(SELECT MAX(u.last_user_update) FROM sys.dm_db_index_usage_stats u
               WHERE u.database_id = DB_ID() AND u.object_id = t.object_id)"""
    try:
        res = conn.execute(text(base.format(upd=dmv))).fetchall()
    except Exception as e:  # noqa: BLE001 — no VIEW SERVER STATE: modify_date only
        if _busy(e):
            raise
        conn.rollback()
        res = conn.execute(text(base.format(upd="CAST(NULL AS DATETIME)"))).fetchall()
    out = {}
    for name, mod, upd in res:
        changed = max([d for d in (mod, upd) if d is not None], default=None)
        out[name] = {"modify_date": mod, "last_write": upd, "changed": changed}
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  the check
# ═══════════════════════════════════════════════════════════════════════════
def check(scope: str) -> Dict[str, Any]:
    """{scope, ok, block, warn, items, checked_at} — see the module docstring."""
    return check_with_context(scope)[0]


def check_with_context(scope: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """check(), plus what it read: the latest DONE builds (step1 / step2, as
    latest_build returns them), the running job and the balance sessions —
    so sz1_run_service.gates() need not read them again."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    S.ensure_tables()
    L = _List()
    b1 = B.latest_build(1)
    b2 = B.latest_build(2) if scope != "build1" else None
    live = B.running()
    ctx: Dict[str, Any] = {"step1": b1, "step2": b2 if scope != "build1" else B.latest_build(2),
                           "running": live, "balance_sessions": []}

    with _reader() as conn:
        checked_at = conn.execute(text("SELECT SYSDATETIME()")).scalar()
        info: Optional[Dict[str, Dict[str, Any]]] = None
        try:
            info = _table_info(conn, list(dict.fromkeys(_SOURCES_BY_SCOPE["run"])))
        except Exception as e:  # noqa: BLE001 — each table is still probed below
            conn.rollback()
            logger.warning(f"[sz1 readiness] table metadata unreadable: {e}")
        unusable = _sources(conn, L, scope, info)
        _order(conn, L, scope, b1, b2, live)
        if scope in ("build2", "run"):
            _freshness(L, scope, info, unusable, b1, b2)
            _rules(L, b1)
            _coverage(conn, L, scope, b1, b2, live, unusable)
        if scope == "run":
            ctx["balance_sessions"] = _sessions(conn, L, live, b1, b2)

    for i in L.items:                      # the review can open the list behind it
        i["has_rows"] = row_kind(scope, i["key"]) is not None
    block = [i["detail"] for i in L.items if i["level"] == "block"]
    warn = [i["detail"] for i in L.items if i["level"] == "warn"]
    result = {"scope": scope, "ok": not block, "block": block, "warn": warn, "items": L.items,
              "checked_at": checked_at.isoformat() if checked_at else datetime.now().isoformat()}
    return result, ctx


# ── 1. sources ──────────────────────────────────────────────────────────────
def _sources(conn, L: _List, scope: str, info: Optional[Dict[str, Dict[str, Any]]]) -> set:
    """One item per table the scope reads. Returns the tables missing or empty.
    `info` None = the metadata could not be read: existence is probed per table."""
    unusable = set()
    for t in _SOURCES_BY_SCOPE[scope]:
        optional = t in IROD_SOURCES
        bad = "warn" if optional else "block"
        key = f"src_{t}"
        exists = (t in info) if info is not None else             bool(conn.execute(text("SELECT OBJECT_ID(:t, 'U')"), {"t": f"dbo.{t}"}).scalar())
        if not exists:
            unusable.add(t)
            L.add(key, "sources", t, bad, f"{t} does not exist — {_WHY[t]}.", "missing")
            continue

        def one(t=t, key=key, bad=bad):
            if t == S.STORE_MASTER:
                n = conn.execute(text(f"SELECT COUNT(DISTINCT ST_CD) FROM {q(t)} WITH (NOLOCK) "
                                      "WHERE LISTING = 1")).scalar() or 0
                if not n:
                    unusable.add(t)
                    L.add(key, "sources", t, bad, f"{t} has no listed store (LISTING = 1) — {_WHY[t]}.", "0 listed")
                else:
                    L.add(key, "sources", t, "ok", f"{t}: {_n(n)} listed store(s).", f"{_n(n)} listed stores")
                return
            has = conn.execute(text(f"SELECT TOP 1 1 FROM {q(t)} WITH (NOLOCK)")).fetchone()
            if not has:
                unusable.add(t)
                L.add(key, "sources", t, bad, f"{t} is empty — {_WHY[t]}.", "empty")
            else:
                n = conn.execute(text("""SELECT SUM(rows) FROM sys.partitions
                                          WHERE object_id = OBJECT_ID(:t) AND index_id IN (0, 1)"""),
                                 {"t": f"dbo.{t}"}).scalar()
                L.add(key, "sources", t, "ok", f"{t}: {_n(n)} rows.", f"{_n(n)} rows")

        L.guard(conn, key, "sources", t, one)
    return unusable


# ── 2. order ────────────────────────────────────────────────────────────────
def _order(conn, L: _List, scope: str, b1, b2, live) -> None:
    if live:
        word = B.JOB_WORDS.get(live["kind"], "A job")
        L.add("job", "order", "One 1SZ ALC job at a time", "block",
              f"{word} {live['id']} is still running. Wait for it to finish.", f"{word} {live['id']}")
    else:
        L.add("job", "order", "One 1SZ ALC job at a time", "ok", "No other 1SZ ALC job is running.")
    if scope == "build1":
        return

    def built(step: int, b, table: str, name: str) -> bool:
        key = f"step{step}_built"
        if not b:
            L.add(key, "order", f"Step {step} built", "block",
                  f"{name} has not been built. Generate Step {step} first.")
            return False
        label = f"Step {step} build #{b['BUILD_ID']} ({_fmt(b.get('STARTED_AT'))})"
        if live:                            # the job may hold the table: do not wait on it
            L.add(key, "order", f"Step {step} built", "ok", f"{label} is in place.", f"#{b['BUILD_ID']}")
            return True

        def one():
            if not conn.execute(text(f"SELECT TOP 1 1 FROM {q(table)} WITH (NOLOCK)")).fetchone():
                L.add(key, "order", f"Step {step} built", "block",
                      f"{label} left the {name} table empty. Generate Step {step} again.", f"#{b['BUILD_ID']}")
            else:
                L.add(key, "order", f"Step {step} built", "ok",
                      f"{label} is in place ({_n(b.get('ROWS_BUILT'))} rows).", f"#{b['BUILD_ID']}")
        L.guard(conn, key, "order", f"Step {step} built", one)
        return True

    ok1 = built(1, b1, S.SZ_BASE, "Step 1 (SZ Base)")
    if scope == "run":
        ok2 = built(2, b2, S.REQ_BASE, "Step 2 (ST/MJ/SZ req)")
        if ok1 and ok2:
            if b2["BUILD_ID"] < b1["BUILD_ID"]:
                L.add("step2_after_step1", "order", "Step 2 built after Step 1", "block",
                      f"Step 2 build #{b2['BUILD_ID']} ({_fmt(b2.get('STARTED_AT'))}) was built before the latest "
                      f"Step 1 build #{b1['BUILD_ID']} ({_fmt(b1.get('STARTED_AT'))}). Rebuild Step 2 so its "
                      "MAJ_CAT + size pairs match the candidates.")
            else:
                L.add("step2_after_step1", "order", "Step 2 built after Step 1", "ok",
                      f"Step 2 build #{b2['BUILD_ID']} is newer than Step 1 build #{b1['BUILD_ID']}.")
        else:
            L.skip("step2_after_step1", "order", "Step 2 built after Step 1", "a build is missing")


# ── 3. freshness ────────────────────────────────────────────────────────────
def _freshness(L: _List, scope: str, info: Optional[Dict[str, Dict[str, Any]]], unusable: set, b1, b2) -> None:
    """A source changed after the build that read it. Step 1's sources are
    compared with the Step 1 build; under a run, Step 2's with the Step 2 build."""
    for t in _fresh_tables(scope):
        key, name = f"fresh_{t}", f"{t}: last change vs build"
        meta = (info or {}).get(t)
        if t in unusable:
            L.skip(key, "freshness", name, f"{t} is missing or empty")
            continue
        if meta is None:
            L.skip(key, "freshness", name, "the table metadata could not be read")
            continue
        changed = meta["changed"]
        if changed is None:
            L.skip(key, "freshness", name, f"no change time is known for {t}")
            continue
        when = _fmt(changed)
        level, step, against, at = _fresh_verdict(t, scope, changed, b1, b2)
        if level == "warn" and step == 1:
            todo = "Generate Step 1 again before Step 2." if scope == "build2" else "Rebuild Step 1, then Step 2."
            L.add(key, "freshness", name, "warn", f"{t} changed at {when}, after {against} ({_fmt(at)}). {todo}", when)
        elif level == "warn":
            L.add(key, "freshness", name, "warn", f"{t} changed at {when}, after {against} ({_fmt(at)}). Rebuild Step 2.", when)
        elif level == "skip":
            L.skip(key, "freshness", name, "the build that reads it has not run")
        else:
            L.add(key, "freshness", name, "ok", f"{t} last changed at {when}, before {against}.", when)


def _fresh_tables(scope: str) -> List[str]:
    return list(STEP1_SOURCES) if scope == "build2" else list(dict.fromkeys(STEP1_SOURCES + STEP2_SOURCES))


def _fresh_verdict(t: str, scope: str, changed: datetime, b1, b2) -> Tuple[str, Optional[int], Optional[str], Optional[datetime]]:
    """(level warn | skip | ok, the step compared with, "Step n build #id", its start).
    Step 1's sources are compared with the Step 1 build; under a run, Step 2's
    with the Step 2 build. One rule for the check and its review list."""
    s1 = _ts(b1.get("STARTED_AT")) if b1 else None
    s2 = _ts(b2.get("STARTED_AT")) if b2 else None
    w1 = f"Step 1 build #{b1['BUILD_ID']}" if b1 else None
    w2 = f"Step 2 build #{b2['BUILD_ID']}" if b2 else None
    if t in STEP1_SOURCES and s1 and changed > s1:
        return "warn", 1, w1, s1
    if scope == "run" and t in STEP2_SOURCES and s2 and changed > s2:
        return "warn", 2, w2, s2
    if (t in STEP1_SOURCES and not s1) or (scope == "run" and t not in STEP1_SOURCES and not s2):
        return "skip", None, None, None
    return ("ok", 1, w1, s1) if t in STEP1_SOURCES else ("ok", 2, w2, s2)


# ── rules ───────────────────────────────────────────────────────────────────
def _rules(L: _List, b1) -> None:
    from app.services import sz1_settings
    name = "Step 1 used the current rule settings"
    if not b1:
        L.skip("rules", "rules", name, "Step 1 has not been built")
        return
    if not sz1_settings.same((b1.get("summary") or {}).get("rules"), sz1_settings.get_all()):
        L.add("rules", "rules", name, "warn",
              f"The rule settings changed after Step 1 build #{b1['BUILD_ID']} (or it predates them). "
              "Generate Step 1 again, then Step 2, to apply them.")
    else:
        L.add("rules", "rules", name, "ok", f"Step 1 build #{b1['BUILD_ID']} used the current rule settings.")


# ── 4. coverage ─────────────────────────────────────────────────────────────
def _coverage(conn, L: _List, scope: str, b1, b2, live, unusable: set) -> None:
    n_grid, n_curve, n_gap = ("cov_grid", "Grid row per store × category"), \
                             ("cov_curve", "Size curve per store × category"), \
                             ("cov_gap", "Zero-CONT gap (information)")
    if live:
        for k, n in (n_grid, n_curve, n_gap):
            L.skip(k, "coverage", n, f"{B.JOB_WORDS.get(live['kind'], 'a job')} {live['id']} is running")
        return
    if not b1:
        for k, n in (n_grid, n_curve, n_gap):
            L.skip(k, "coverage", n, "Step 1 has not been built")
        return

    # (a) listed stores × SZ Base categories with no ARS_GRID_MJ row → budget 0
    def grid():
        k, n = n_grid
        if scope == "run":
            if not b2:
                L.skip(k, "coverage", n, "Step 2 has not been built")
                return
            # what the run will read: Step 2 rows whose grid join found nothing
            r = conn.execute(text(f"""
                SELECT COUNT(*) AS ROWS_, COUNT(DISTINCT ST_CD + '|' + MAJ_CAT) AS PAIRS, COUNT(DISTINCT ST_CD) AS STORES
                  FROM {q(S.REQ_BASE)} WITH (NOLOCK)
                 WHERE MJ_STK IS NULL AND MJ_DISP IS NULL AND MJ_SAL_PD IS NULL""")).fetchone()
            rows_, pairs, stores = int(r[0] or 0), int(r[1] or 0), int(r[2] or 0)
            src = f"in Step 2 build #{b2['BUILD_ID']}"
        else:
            if {S.STORE_MASTER, S.GRID_MJ} & unusable:
                L.skip(k, "coverage", n, "the store master or ARS_GRID_MJ is missing or empty")
                return
            r = conn.execute(text(f"""
                WITH st AS (SELECT DISTINCT ST_CD FROM {q(S.STORE_MASTER)} WITH (NOLOCK) WHERE LISTING = 1),
                     mc AS (SELECT DISTINCT MAJ_CAT FROM {q(S.SZ_BASE)} WITH (NOLOCK))
                SELECT COUNT(*) AS PAIRS, COUNT(DISTINCT st.ST_CD) AS STORES
                  FROM st CROSS JOIN mc
                 WHERE NOT EXISTS (SELECT 1 FROM {q(S.GRID_MJ)} g WITH (NOLOCK)
                                    WHERE g.WERKS = st.ST_CD AND g.MAJ_CAT = mc.MAJ_CAT)""")).fetchone()
            rows_, pairs, stores = None, int(r[0] or 0), int(r[1] or 0)
            src = "today"
        if pairs:
            extra = f" ({_n(rows_)} store × size rows)" if rows_ else ""
            L.add(k, "coverage", n, "warn",
                  f"{_n(pairs)} listed store × MAJ_CAT pair(s) at {_n(stores)} store(s) have no ARS_GRID_MJ row "
                  f"{src}{extra}, so their size budget is 0 and they get nothing.",
                  f"{_n(pairs)} pairs · {_n(stores)} stores")
        else:
            L.add(k, "coverage", n, "ok", f"Every listed store has an ARS_GRID_MJ row for every Step 1 category ({src}).",
                  "0 pairs")

    # (b) no real size with CONT > 0 in the store's own rows nor the CO rows → budget 0
    def curve():
        k, n = n_curve
        if {S.STORE_MASTER, S.CONT_SZ} & unusable:
            L.skip(k, "coverage", n, "the store master or Master_CONT_SZ is missing or empty")
            return
        r = conn.execute(text(f"""
            WITH st AS (SELECT DISTINCT ST_CD FROM {q(S.STORE_MASTER)} WITH (NOLOCK) WHERE LISTING = 1),
                 mc AS (SELECT DISTINCT MAJ_CAT FROM {q(S.SZ_BASE)} WITH (NOLOCK) WHERE SZ NOT IN ({B.FULL_OPT_SQL})),
                 cv AS (SELECT DISTINCT ST_CD, MAJ_CAT FROM {q(S.CONT_SZ)} WITH (NOLOCK)
                         WHERE SZ NOT IN ({B.FULL_OPT_SQL}) AND CONT > 0)
            SELECT COUNT(*) AS PAIRS, COUNT(DISTINCT st.ST_CD) AS STORES, COUNT(DISTINCT mc.MAJ_CAT) AS CATS
              FROM st CROSS JOIN mc
             WHERE NOT EXISTS (SELECT 1 FROM cv WHERE cv.ST_CD = st.ST_CD AND cv.MAJ_CAT = mc.MAJ_CAT)
               AND NOT EXISTS (SELECT 1 FROM cv WHERE cv.ST_CD = 'CO' AND cv.MAJ_CAT = mc.MAJ_CAT)""")).fetchone()
        pairs, stores, cats = int(r[0] or 0), int(r[1] or 0), int(r[2] or 0)
        if pairs:
            L.add(k, "coverage", n, "warn",
                  f"{_n(pairs)} listed store × MAJ_CAT pair(s) at {_n(stores)} store(s) ({_n(cats)} categories) have "
                  "no size curve in Master_CONT_SZ — neither the store's rows nor the CO rows give a real size a "
                  "CONT % — so their size budget is 0 (CONT_SRC NO_CURVE).",
                  f"{_n(pairs)} pairs · {_n(stores)} stores")
        else:
            L.add(k, "coverage", n, "ok",
                  "Every listed store has a size curve (its own or CO) for every Step 1 category with real sizes.",
                  "0 pairs")

    # (c) the zero-CONT gap of the latest Step 1 build — information
    def gap():
        k, n = n_gap
        r = conn.execute(text(f"""
            SELECT COUNT(*), COUNT(DISTINCT ARTICLE_NUMBER), SUM(CAST(EXCLUDED AS INT)),
                   SUM(FNL_Q), SUM(CASE WHEN EXCLUDED = 1 THEN FNL_Q END)
              FROM {q(S.GAP)} WITH (NOLOCK) WHERE BUILD_ID = :bid"""), {"bid": b1["BUILD_ID"]}).fetchone()
        rows_, arts, excl, pcs, excl_pcs = int(r[0] or 0), int(r[1] or 0), int(r[2] or 0), r[3], r[4]
        if rows_:
            L.add(k, "coverage", n, "ok",
                  f"{_n(rows_)} RDC × article row(s) ({_n(arts)} articles, {_n(pcs)} pcs free) have no CONT % anywhere "
                  f"in Master_CONT_SZ in Step 1 build #{b1['BUILD_ID']}; {_n(excl)} of them ({_n(excl_pcs)} pcs) are "
                  "kept out of SZ Base. See the CONT Gap Report.",
                  f"{_n(rows_)} articles")
        else:
            L.add(k, "coverage", n, "ok", f"No zero-CONT gap in Step 1 build #{b1['BUILD_ID']}.", "0 articles")

    L.guard(conn, n_grid[0], "coverage", n_grid[1], grid)
    L.guard(conn, n_curve[0], "coverage", n_curve[1], curve)
    L.guard(conn, n_gap[0], "coverage", n_gap[1], gap)


# ── 5. sessions ─────────────────────────────────────────────────────────────
def _sessions(conn, L: _List, live, b1=None, b2=None) -> List[str]:
    """Phase 3: every PARKED session (legacy DONE too) holds its allocation
    inside 1SZ until it is approved into Pend ALC or discarded — Listing can't
    see it. List them with the units held; warn for each parked longer than
    STALE_PARKED_DAYS. Count the sessions approved since the latest build.
    Returns the parked session ids (newest first)."""
    ids: List[str] = []
    name_held, name_old = "Parked sessions holding stock", "Parked sessions waiting too long"
    name_appr, name_drift = "Approved since the latest build", "Stored balances are live"
    if live:
        why = f"{B.JOB_WORDS.get(live['kind'], 'a job')} {live['id']} is running"
        for k, n in (("sessions_held", name_held), ("sessions_stale", name_old),
                     ("sessions_approved", name_appr), ("balance_drift", name_drift)):
            L.skip(k, "sessions", n, why)
        return ids

    def held():
        res = conn.execute(text(f"""
            SELECT SESSION_ID, SESSION_AT, ISNULL(ALLOC_QTY, 0), DATEDIFF(MINUTE, SESSION_AT, SYSDATETIME()),
                   STORES_SERVED, CREATED_BY
              FROM {q(S.SESSION)} WITH (NOLOCK)
             WHERE STATUS IN {B.PARKED_SQL}
             ORDER BY SESSION_AT DESC, SESSION_ID DESC""")).fetchall()
        ids.extend(str(r[0]) for r in res if str(r[0]).isdigit())
        if not res:
            L.add("sessions_held", "sessions", name_held, "ok",
                  "No session is parked: every run starts from the live stock and pending.", "none")
            L.add("sessions_stale", "sessions", name_old, "ok", "No parked session.", "none")
            return
        units = sum(float(r[2] or 0) for r in res)
        listing = "; ".join(f"{r[0]} ({_fmt(r[1])}, {_n(r[2])} units)" for r in res[:12])
        more = f"; and {len(res) - 12} more" if len(res) > 12 else ""
        L.add("sessions_held", "sessions", name_held, "ok",
              f"{len(res)} parked session(s) hold {_n(units)} units inside 1SZ: {listing}{more}. "
              "A run nets them off the stock and the size budgets.",
              f"{len(res)} · {_n(units)} units")
        stale = [r for r in res if int(r[3] or 0) > STALE_PARKED_DAYS * 24 * 60]
        for r in stale:
            days = int(r[3] or 0) // 1440
            L.add(f"sessions_stale_{r[0]}", "sessions", name_old, "warn",
                  f"Session {r[0]} parked {days} day{'s' if days != 1 else ''} ago still holds {_n(r[2])} units "
                  "inside 1SZ only — approve it into Pend ALC or discard it. Listing can't see parked stock.",
                  f"{r[0]} · {days} d")
        if not stale:
            L.add("sessions_stale", "sessions", name_old, "ok",
                  f"No session has been parked longer than {STALE_PARKED_DAYS} days.", "none")

    def approved():
        since = max([d for d in (_ts((b1 or {}).get("STARTED_AT")), _ts((b2 or {}).get("STARTED_AT"))) if d],
                    default=None)
        if since is None:
            L.skip("sessions_approved", "sessions", name_appr, "no build yet")
            return
        r = conn.execute(text(f"""
            SELECT COUNT(*), ISNULL(SUM(ALLOC_QTY), 0) FROM {q(S.SESSION)} WITH (NOLOCK)
             WHERE STATUS = 'APPROVED' AND APPROVED_AT > :since"""), {"since": since}).fetchone()
        n, qty = int(r[0] or 0), r[1]
        L.add("sessions_approved", "sessions", name_appr, "ok",
              (f"{n} session(s) ({_n(qty)} units) approved into Pend ALC since the latest build ({_fmt(since)}); "
               "a run nets them as live open pending.") if n else
              f"No session approved into Pend ALC since the latest build ({_fmt(since)}).",
              f"{n} · {_n(qty)} units")

    def drift():
        d = B.balance_drift(conn)
        if d:
            L.add("balance_drift", "sessions", name_drift, "ok",
                  "The stored balance columns lag the live stock / pending / parked sessions: " + "; ".join(d)
                  + ". A run re-applies them before it reads; “Re-apply balances” updates the Step 1 / 2 tables now.",
                  "refresh on run")
        else:
            L.add("balance_drift", "sessions", name_drift, "ok",
                  "BAL_FNL_Q and REM_MJ_SZ_SRT match the live stock, open pending and parked sessions.")

    L.guard(conn, "sessions_held", "sessions", name_held, held)
    L.guard(conn, "sessions_approved", "sessions", name_appr, approved)
    L.guard(conn, "balance_drift", "sessions", name_drift, drift)
    return ids


# ═══════════════════════════════════════════════════════════════════════════
#  review: the list behind an item
# ═══════════════════════════════════════════════════════════════════════════
ROW_TOP = 5000                      # rows a review list returns; export streams all
_STALE_KEY = re.compile(r"^sessions_stale_\d+$")
_PRIORITY_WORDS = {"weighted": "Balanced", "mj_srt": "Biggest total shortage", "sequence": "Own sequence"}
_CROSS_WORDS = {"all": "All RDC pool", "same": "Own RDC", "home_first": "Home first", "pooled": "Pooled (legacy)",
                "fallback": "Fallback"}
_BASIS_WORDS = {"live": "Live stock", "initial": "Full stock (older run)", "balance": "Remaining stock (older run)"}


def row_kind(scope: str, key: str) -> Optional[str]:
    """The list an item of this scope has, or None (no list / not an item of it)."""
    key = str(key or "")
    if scope not in SCOPES:
        return None
    if key.startswith("src_"):
        return "sources" if key[4:] in _SOURCES_BY_SCOPE[scope] else None
    if scope == "build1":
        return None
    if key.startswith("fresh_"):
        return "freshness" if key[6:] in _fresh_tables(scope) else None
    if key in ("rules", "cov_grid", "cov_curve", "cov_gap"):
        return key
    if scope != "run":
        return None
    if key in ("sessions_held", "sessions_stale") or _STALE_KEY.match(key):
        return "sessions_parked"
    if key in ("sessions_approved", "balance_drift"):
        return key
    return None


def _latest(conn, step: int) -> Optional[Dict[str, Any]]:
    r = conn.execute(text(f"""SELECT TOP 1 * FROM {q(S.BUILD)} WITH (NOLOCK)
                               WHERE STEP = :s AND STATUS = 'DONE' ORDER BY BUILD_ID DESC"""), {"s": step}).fetchone()
    return B._build_row(r) if r is not None else None


def _cut(rows_: List[Dict[str, Any]], top: Optional[int]) -> List[Dict[str, Any]]:
    return rows_ if top is None else rows_[:top]


def _yn(v: Any) -> str:
    return "Y" if v else "N"


def _num(v: Any) -> Any:
    v = B.jsonable(v)
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _empty(note: str) -> Dict[str, Any]:
    return {"columns": [], "rows": [], "total": 0, "note": note}


# ── sources ─────────────────────────────────────────────────────────────────
def _rows_sources(conn, scope: str, b1, b2, top) -> Dict[str, Any]:
    tables = _SOURCES_BY_SCOPE[scope]
    info = _table_info(conn, tables)
    names = ", ".join(f"OBJECT_ID('dbo.{t}')" for t in tables)
    counts = {r[0]: int(r[1] or 0) for r in conn.execute(text(f"""
        SELECT OBJECT_NAME(object_id), SUM(rows) FROM sys.partitions
         WHERE object_id IN ({names}) AND index_id IN (0, 1) GROUP BY object_id"""))}
    listed = None
    if S.STORE_MASTER in info:
        listed = conn.execute(text(f"SELECT COUNT(DISTINCT ST_CD) FROM {q(S.STORE_MASTER)} WITH (NOLOCK) "
                                   "WHERE LISTING = 1")).scalar() or 0
    out = []
    for t in tables:
        have = t in info
        n = counts.get(t, 0)
        if not have:
            st = "Missing"
        elif t == S.STORE_MASTER and not listed:
            st = "No listed store"
        elif not n and not conn.execute(text(f"SELECT TOP 1 1 FROM {q(t)} WITH (NOLOCK)")).fetchone():
            st = "Empty"
        else:
            st = "OK"
        out.append({"TABLE": t, "STATUS": st, "REQUIRED": "N (I_ROD, optional)" if t in IROD_SOURCES else "Y",
                    "ROWS": n if have else None, "LISTED_STORES": listed if t == S.STORE_MASTER else None,
                    "LAST_CHANGE": info[t]["changed"] if have else None, "USED_FOR": _WHY[t]})
    return {"columns": ["TABLE", "STATUS", "REQUIRED", "ROWS", "LISTED_STORES", "LAST_CHANGE", "USED_FOR"],
            "rows": out, "total": len(out),
            "note": f"Every table the {SCOPE_WORDS[scope]} reads. ROWS from the table metadata; LAST_CHANGE = the later "
                    "of the last schema change and the last write seen since the SQL Server started."}


# ── freshness ───────────────────────────────────────────────────────────────
def _rows_freshness(conn, scope: str, b1, b2, top) -> Dict[str, Any]:
    tables = _fresh_tables(scope)
    info = _table_info(conn, tables)
    out = []
    for t in tables:
        meta = info.get(t)
        read_by = " + ".join(w for w, s in (("Step 1", STEP1_SOURCES), ("Step 2", STEP2_SOURCES))
                             if t in s and (w == "Step 1" or scope == "run"))
        row = {"TABLE": t, "READ_BY": read_by, "LAST_CHANGE": None, "SCHEMA_CHANGED": None, "LAST_WRITE": None,
               "COMPARED_WITH": None, "BUILD_STARTED": None, "VERDICT": None}
        if meta is None:
            row["VERDICT"] = "Table missing"
        elif meta["changed"] is None:
            row["VERDICT"] = "No change time known"
        else:
            row.update(LAST_CHANGE=meta["changed"], SCHEMA_CHANGED=meta["modify_date"], LAST_WRITE=meta["last_write"])
            level, step, against, at = _fresh_verdict(t, scope, meta["changed"], b1, b2)
            row.update(COMPARED_WITH=against, BUILD_STARTED=at)
            row["VERDICT"] = ("Changed after the build — " + ("rebuild Step 1, then Step 2" if step == 1 else "rebuild Step 2")
                              if level == "warn" else "The build that reads it has not run" if level == "skip"
                              else "Unchanged since the build")
        out.append(row)
    return {"columns": ["TABLE", "READ_BY", "LAST_CHANGE", "SCHEMA_CHANGED", "LAST_WRITE", "COMPARED_WITH",
                        "BUILD_STARTED", "VERDICT"],
            "rows": out, "total": len(out),
            "note": "Every source compared with the build that read it. Change time = the later of sys.tables.modify_date "
                    "(SCHEMA_CHANGED) and the last INSERT / UPDATE / DELETE seen since the SQL Server started (LAST_WRITE)."}


# ── rules ───────────────────────────────────────────────────────────────────
def _rows_rules(conn, scope: str, b1, b2, top) -> Dict[str, Any]:
    from app.services import sz1_settings
    if not b1:
        return _empty("Step 1 has not been built.")
    now = sz1_settings.get_all()
    then = (b1.get("summary") or {}).get("rules")
    out = []
    for k, spec in sz1_settings.SPEC.items():
        was = (then or {}).get(k)
        changed = "—" if then is None else "Y" if k not in then or _num(was) != _num(now.get(k)) else "N"
        out.append({"SETTING": k, "LABEL": spec[2], "AT_STEP1_BUILD": was, "NOW": now.get(k), "CHANGED": changed})
    out.sort(key=lambda r: r["CHANGED"] != "Y")
    note = f"Step 1 build #{b1['BUILD_ID']} ({_fmt(b1.get('STARTED_AT'))}) against the saved settings now."
    if then is None:
        note += " The build predates the rule settings, so it records none."
    return {"columns": ["SETTING", "LABEL", "AT_STEP1_BUILD", "NOW", "CHANGED"], "rows": out, "total": len(out),
            "note": note}


# ── coverage: grid ──────────────────────────────────────────────────────────
def _stock_in(conn) -> Dict[str, Dict[str, Any]]:
    """Per Step 1 category: where 1SZ holds its free stock — 'DH24 1,234 · DW01 56' and the total."""
    by: Dict[str, Dict[str, Any]] = {}
    for mc, rdc, fq in conn.execute(text(f"""SELECT MAJ_CAT, RDC, SUM(FNL_Q) FROM {q(S.SZ_BASE)} WITH (NOLOCK)
                                             GROUP BY MAJ_CAT, RDC ORDER BY MAJ_CAT, RDC""")):
        e = by.setdefault(mc, {"rdcs": [], "parts": [], "fnl": 0.0})
        e["rdcs"].append(rdc)
        e["parts"].append(f"{rdc} {_n(fq)}")
        e["fnl"] += float(fq or 0)
    return by


def _grid_facts(conn, keys: List[List[str]]) -> Dict[Tuple[str, str], Dict[str, int]]:
    """Per [ST_CD, MAJ_CAT] (OPENJSON, so each test is an index seek): does the
    store hold stock rows of the category, and does ARS_GRID_MJ have the row now."""
    if not keys:
        return {}
    res = conn.execute(text(f"""
        WITH k AS (SELECT ST_CD, MAJ_CAT FROM OPENJSON(:j) WITH (ST_CD NVARCHAR(50) '$[0]', MAJ_CAT NVARCHAR(255) '$[1]'))
        SELECT k.ST_CD, k.MAJ_CAT,
               CASE WHEN EXISTS (SELECT 1 FROM {q(S.GRID_VAR_ART)} v WITH (NOLOCK)
                                  WHERE v.WERKS = k.ST_CD AND v.MAJ_CAT = k.MAJ_CAT) THEN 1 ELSE 0 END,
               CASE WHEN EXISTS (SELECT 1 FROM {q(S.GRID_MJ)} g WITH (NOLOCK)
                                  WHERE g.WERKS = k.ST_CD AND g.MAJ_CAT = k.MAJ_CAT) THEN 1 ELSE 0 END
          FROM k"""), {"j": json.dumps(keys)})
    return {(r[0], r[1]): {"stk": int(r[2]), "grid": int(r[3])} for r in res}


def _grid_why(rdc: str, own_msa: bool, has_stk: bool, stocked: List[str], grid_now: Optional[bool]) -> str:
    if grid_now:
        why = "ARS_GRID_MJ has the row now — Step 2 was built before it; rebuild Step 2 to pick it up."
    elif not own_msa and not has_stk:
        why = (f"The store's RDC ({rdc}) does not carry the category in ARS_MSA_TOTAL and the store holds none of it, "
               "so the grid builds no row (outside the grid's scope).")
    elif own_msa:
        why = f"The store's RDC ({rdc}) carries the category, yet the grid has no row — the grid is older than the MSA."
    else:
        why = "The store holds stock of the category, yet the grid has no row — the grid is older than the store stock."
    if stocked and rdc not in stocked:
        why += f" 1SZ holds it only in {', '.join(stocked)} (offered through the All RDC pool)."
    return why


def _rows_cov_grid(conn, scope: str, b1, b2, top) -> Dict[str, Any]:
    if not b1:
        return _empty("Step 1 has not been built — nothing to compare.")
    if scope == "run":
        if not b2:
            return _empty("Step 2 has not been built — nothing to compare.")
        # what the run will read: Step 2 rows whose grid join found nothing (the check's rule)
        pairs = B.rows(conn, f"""
            SELECT RDC, ST_CD, MAX(ST_NM) AS ST_NM, MAJ_CAT, COUNT(*) AS SIZE_ROWS
              FROM {q(S.REQ_BASE)} WITH (NOLOCK)
             WHERE MJ_STK IS NULL AND MJ_DISP IS NULL AND MJ_SAL_PD IS NULL
             GROUP BY RDC, ST_CD, MAJ_CAT""")
        note = (f"Rows of Step 2 build #{b2['BUILD_ID']} ({_fmt(b2.get('STARTED_AT'))}) whose ARS_GRID_MJ join found "
                "nothing, one line per store × MAJ_CAT (SIZE_ROWS = its store × size rows). GRID_NOW = Y: the grid has "
                "the row today, so a Step 2 rebuild gives it a budget.")
    else:
        if not all(conn.execute(text("SELECT OBJECT_ID(:t, 'U')"), {"t": f"dbo.{t}"}).scalar()
                   for t in (S.STORE_MASTER, S.GRID_MJ)):
            return _empty("The store master or ARS_GRID_MJ is missing.")
        pairs = B.rows(conn, f"""
            WITH st AS (SELECT ST_CD, MAX(ST_NM) AS ST_NM, MAX(RDC) AS RDC
                          FROM {q(S.STORE_MASTER)} WITH (NOLOCK) WHERE LISTING = 1 GROUP BY ST_CD),
                 mc AS (SELECT DISTINCT MAJ_CAT FROM {q(S.SZ_BASE)} WITH (NOLOCK))
            SELECT st.RDC, st.ST_CD, st.ST_NM, mc.MAJ_CAT
              FROM st CROSS JOIN mc
             WHERE NOT EXISTS (SELECT 1 FROM {q(S.GRID_MJ)} g WITH (NOLOCK)
                                WHERE g.WERKS = st.ST_CD AND g.MAJ_CAT = mc.MAJ_CAT)""")
        note = (f"Listed stores (LISTING = 1) × the categories of Step 1 build #{b1['BUILD_ID']}, checked against "
                "today's ARS_GRID_MJ — what the next Step 2 build will read.")
    pairs.sort(key=lambda p: (str(p["RDC"]), str(p["ST_CD"]), str(p["MAJ_CAT"])))
    total = len(pairs)
    shown = _cut(pairs, top)
    stock = _stock_in(conn)
    msa = {(r[0], r[1]) for r in conn.execute(text(
        f"SELECT DISTINCT RDC, MAJ_CAT FROM {q(S.MSA_TOTAL)} WITH (NOLOCK)"))}
    facts = _grid_facts(conn, [[p["ST_CD"], p["MAJ_CAT"]] for p in shown])
    out = []
    for p in shown:
        fx = facts.get((p["ST_CD"], p["MAJ_CAT"]), {})
        sk = stock.get(p["MAJ_CAT"], {"rdcs": [], "parts": [], "fnl": 0.0})
        own = (p["RDC"], p["MAJ_CAT"]) in msa
        grid_now = bool(fx.get("grid")) if scope == "run" else None
        row = {"ST_CD": p["ST_CD"], "ST_NM": p["ST_NM"], "RDC": p["RDC"], "MAJ_CAT": p["MAJ_CAT"],
               "STOCKED_IN": " · ".join(sk["parts"]) or None, "SZ1_FNL_Q": _num(round(sk["fnl"], 2)),
               "OWN_RDC_CARRIES": _yn(own), "STORE_HAS_STOCK": _yn(fx.get("stk"))}
        if scope == "run":
            row["SIZE_ROWS"] = p["SIZE_ROWS"]
            row["GRID_NOW"] = _yn(grid_now)
        row["WHY"] = _grid_why(p["RDC"], own, bool(fx.get("stk")), sk["rdcs"], grid_now)
        out.append(row)
    cols = ["ST_CD", "ST_NM", "RDC", "MAJ_CAT", "STOCKED_IN", "SZ1_FNL_Q", "OWN_RDC_CARRIES", "STORE_HAS_STOCK"] \
        + (["SIZE_ROWS", "GRID_NOW"] if scope == "run" else []) + ["WHY"]
    return {"columns": cols, "rows": out, "total": total, "note": note}


# ── coverage: size curve ────────────────────────────────────────────────────
def _rows_cov_curve(conn, scope: str, b1, b2, top) -> Dict[str, Any]:
    if not b1:
        return _empty("Step 1 has not been built — nothing to compare.")
    pairs = B.rows(conn, f"""
        WITH st AS (SELECT ST_CD, MAX(ST_NM) AS ST_NM, MAX(RDC) AS RDC
                      FROM {q(S.STORE_MASTER)} WITH (NOLOCK) WHERE LISTING = 1 GROUP BY ST_CD),
             mc AS (SELECT DISTINCT MAJ_CAT FROM {q(S.SZ_BASE)} WITH (NOLOCK) WHERE SZ NOT IN ({B.FULL_OPT_SQL})),
             cv AS (SELECT DISTINCT ST_CD, MAJ_CAT FROM {q(S.CONT_SZ)} WITH (NOLOCK)
                     WHERE SZ NOT IN ({B.FULL_OPT_SQL}) AND CONT > 0)
        SELECT st.ST_CD, st.ST_NM, st.RDC, mc.MAJ_CAT
          FROM st CROSS JOIN mc
         WHERE NOT EXISTS (SELECT 1 FROM cv WHERE cv.ST_CD = st.ST_CD AND cv.MAJ_CAT = mc.MAJ_CAT)
           AND NOT EXISTS (SELECT 1 FROM cv WHERE cv.ST_CD = 'CO' AND cv.MAJ_CAT = mc.MAJ_CAT)""")
    pairs.sort(key=lambda p: (str(p["MAJ_CAT"]), str(p["RDC"]), str(p["ST_CD"])))
    total = len(pairs)
    shown = _cut(pairs, top)
    out = []
    if shown:
        cats = sorted({p["MAJ_CAT"] for p in shown})
        names = ", ".join(f":c{i}" for i in range(len(cats)))
        prm = {f"c{i}": c for i, c in enumerate(cats)}
        sizes: Dict[str, List[str]] = {}
        for mc, sz in conn.execute(text(f"""SELECT DISTINCT MAJ_CAT, SZ FROM {q(S.SZ_BASE)} WITH (NOLOCK)
                                            WHERE MAJ_CAT IN ({names}) AND SZ NOT IN ({B.FULL_OPT_SQL})"""), prm):
            sizes.setdefault(mc, []).append(str(sz))
        cnt = {(r[0], r[1]): int(r[2]) for r in conn.execute(text(f"""
            SELECT ST_CD, MAJ_CAT, COUNT(*) FROM {q(S.CONT_SZ)} WITH (NOLOCK)
             WHERE MAJ_CAT IN ({names}) GROUP BY ST_CD, MAJ_CAT"""), prm)}
        stock = _stock_in(conn)
        for p in shown:
            own, co = cnt.get((p["ST_CD"], p["MAJ_CAT"]), 0), cnt.get(("CO", p["MAJ_CAT"]), 0)
            why = ("Neither the store nor CO has any Master_CONT_SZ row for the category" if not own and not co else
                   "The store's and CO's rows give no real size a CONT > 0 (only A / NA or zeros)")
            sk = stock.get(p["MAJ_CAT"], {"parts": [], "fnl": 0.0})
            out.append({"ST_CD": p["ST_CD"], "ST_NM": p["ST_NM"], "RDC": p["RDC"], "MAJ_CAT": p["MAJ_CAT"],
                        "SZ1_SIZES": ", ".join(sorted(sizes.get(p["MAJ_CAT"], []))),
                        "STOCKED_IN": " · ".join(sk["parts"]) or None, "SZ1_FNL_Q": _num(round(sk["fnl"], 2)),
                        "STORE_MASTER_ROWS": own, "CO_MASTER_ROWS": co,
                        "WHY": why + " — budget 0 (CONT_SRC NO_CURVE)."})
    return {"columns": ["ST_CD", "ST_NM", "RDC", "MAJ_CAT", "SZ1_SIZES", "STOCKED_IN", "SZ1_FNL_Q",
                        "STORE_MASTER_ROWS", "CO_MASTER_ROWS", "WHY"],
            "rows": out, "total": total,
            "note": f"Listed stores × the real-size categories of Step 1 build #{b1['BUILD_ID']} with no size curve in "
                    "today's Master_CONT_SZ — neither the store's rows nor the CO rows give a real size a CONT %."}


# ── coverage: zero-CONT gap ─────────────────────────────────────────────────
def _rows_cov_gap(conn, scope: str, b1, b2, top) -> Dict[str, Any]:
    if not b1:
        return _empty("Step 1 has not been built — no gap recorded.")
    cols = ["RDC", "MAJ_CAT", "GEN_ART_NUMBER", "CLR", "ARTICLE_NUMBER", "SZ", "FNL_Q", "GAP_TYPE", "CONT_SRC",
            "ART_CONT_PCT", "OPT_CONT_PCT", "RULES_IF_KEPT", "EXCLUDED"]
    lim = f"TOP ({int(top)})" if top is not None else ""
    data = B.rows(conn, f"""
        SELECT {lim} {', '.join(cols[:-1])}, CASE WHEN EXCLUDED = 1 THEN 'Y' ELSE 'N' END AS EXCLUDED,
               COUNT(*) OVER () AS TOTAL_
          FROM {q(S.GAP)} WITH (NOLOCK) WHERE BUILD_ID = :bid
         ORDER BY FNL_Q DESC, RDC, MAJ_CAT, ARTICLE_NUMBER""", {"bid": b1["BUILD_ID"]})
    total = int(data[0]["TOTAL_"]) if data else 0
    for r in data:
        r.pop("TOTAL_", None)
        r["FNL_Q"] = _num(r["FNL_Q"])
    return {"columns": cols, "rows": data, "total": total,
            "note": f"RDC × article rows of Step 1 build #{b1['BUILD_ID']} with no CONT % anywhere in Master_CONT_SZ; "
                    "EXCLUDED = Y: kept out of SZ Base by the gap rule. Largest free stock first."}


# ── sessions ────────────────────────────────────────────────────────────────
def _rows_sessions_parked(conn, scope: str, b1, b2, top) -> Dict[str, Any]:
    data = B.rows(conn, f"""
        SELECT SESSION_ID, STATUS, SESSION_AT, DATEDIFF(MINUTE, SESSION_AT, SYSDATETIME()) AS AGE_MIN,
               ISNULL(ALLOC_QTY, 0) AS ALLOC_QTY, STORES_SERVED, LINES_SERVED, PRIORITY_MODE, CROSS_RDC, STOCK_BASIS,
               BASE_BUILD_ID, REQ_BUILD_ID, CREATED_BY, NOTE
          FROM {q(S.SESSION)} WITH (NOLOCK)
         WHERE STATUS IN {B.PARKED_SQL}
         ORDER BY SESSION_AT DESC, SESSION_ID DESC""")
    out = []
    for r in data:
        mins = int(r["AGE_MIN"] or 0)
        out.append({"SESSION_ID": r["SESSION_ID"], "STATUS": "PARKED" if r["STATUS"] == "DONE" else r["STATUS"],
                    "PARKED_AT": r["SESSION_AT"], "AGE_DAYS": round(mins / 1440, 1),
                    "TOO_OLD": _yn(mins > STALE_PARKED_DAYS * 24 * 60),
                    "UNITS": _num(r["ALLOC_QTY"]), "STORES": r["STORES_SERVED"], "LINES": r["LINES_SERVED"],
                    "PRIORITY": _PRIORITY_WORDS.get(r["PRIORITY_MODE"], r["PRIORITY_MODE"]),
                    "WAREHOUSE": _CROSS_WORDS.get(r["CROSS_RDC"], r["CROSS_RDC"]),
                    "STOCK_BASIS": _BASIS_WORDS.get(r["STOCK_BASIS"], r["STOCK_BASIS"]),
                    "BUILDS": f"#{r['BASE_BUILD_ID'] or '?'} / #{r['REQ_BUILD_ID'] or '?'}",
                    "CREATED_BY": r["CREATED_BY"], "NOTE": r["NOTE"]})
    return {"columns": ["SESSION_ID", "STATUS", "PARKED_AT", "AGE_DAYS", "TOO_OLD", "UNITS", "STORES", "LINES",
                        "PRIORITY", "WAREHOUSE", "STOCK_BASIS", "BUILDS", "CREATED_BY", "NOTE"],
            "rows": _cut(out, top), "total": len(out),
            "note": "Every parked session (legacy DONE included) — its units are held inside 1SZ until approved into "
                    f"Pend ALC or discarded. TOO_OLD = parked longer than {STALE_PARKED_DAYS} days. "
                    "BUILDS = the Step 1 / Step 2 builds it read."}


def _rows_sessions_approved(conn, scope: str, b1, b2, top) -> Dict[str, Any]:
    since = max([d for d in (_ts((b1 or {}).get("STARTED_AT")), _ts((b2 or {}).get("STARTED_AT"))) if d], default=None)
    if since is None:
        return _empty("No build yet.")
    data = B.rows(conn, f"""
        SELECT SESSION_ID, SESSION_AT, APPROVED_AT, APPROVED_BY, ISNULL(ALLOC_QTY, 0) AS UNITS,
               STORES_SERVED AS STORES, LINES_SERVED AS LINES, PEND_SESSION_ID, CREATED_BY
          FROM {q(S.SESSION)} WITH (NOLOCK)
         WHERE STATUS = 'APPROVED' AND APPROVED_AT > :since
         ORDER BY APPROVED_AT DESC""", {"since": since})
    for r in data:
        r["UNITS"] = _num(r["UNITS"])
    return {"columns": ["SESSION_ID", "SESSION_AT", "APPROVED_AT", "APPROVED_BY", "UNITS", "STORES", "LINES",
                        "PEND_SESSION_ID", "CREATED_BY"],
            "rows": _cut(data, top), "total": len(data),
            "note": f"Sessions approved into Pend ALC since the latest build ({_fmt(since)}); a run nets them as live "
                    "open pending."}


def _rows_balance_drift(conn, scope: str, b1, b2, top) -> Dict[str, Any]:
    """The rows whose stored balance disagrees with the live figure — the rule
    B.balance_drift counts — largest difference first."""
    ids = B.parked_sessions(conn)
    lim = f"TOP ({int(top)})" if top is not None else ""
    one = B.rows(conn, f"""
        SELECT {lim} N'Step 1 · BAL_FNL_Q' AS BALANCE, b.RDC, CAST(NULL AS NVARCHAR(50)) AS ST_CD, b.MAJ_CAT,
               b.ARTICLE_NUMBER, b.SZ, b.BAL_FNL_Q AS STORED, {B.LIVE_FREE_EXPR} AS LIVE, COUNT(*) OVER () AS TOTAL_
          FROM {q(S.SZ_BASE)} b WITH (NOLOCK) {B.live_joins_base(ids)}
         WHERE ABS(ISNULL(b.BAL_FNL_Q, -1) - {B.LIVE_FREE_EXPR}) > 0.005
         ORDER BY ABS(ISNULL(b.BAL_FNL_Q, 0) - {B.LIVE_FREE_EXPR}) DESC""")
    two = B.rows(conn, f"""
        SELECT {lim} N'Step 2 · REM_MJ_SZ_SRT' AS BALANCE, r.RDC, r.ST_CD, r.MAJ_CAT,
               CAST(NULL AS NVARCHAR(200)) AS ARTICLE_NUMBER, r.SZ, r.REM_MJ_SZ_SRT AS STORED,
               {B.LIVE_ROOM_EXPR} AS LIVE, COUNT(*) OVER () AS TOTAL_
          FROM {q(S.REQ_BASE)} r WITH (NOLOCK) {B.live_joins_req(ids, cat=False)}
         WHERE ABS(ISNULL(r.REM_MJ_SZ_SRT, -1) - {B.LIVE_ROOM_EXPR}) > 0.005
         ORDER BY ABS(ISNULL(r.REM_MJ_SZ_SRT, 0) - {B.LIVE_ROOM_EXPR}) DESC""")
    total = (int(one[0]["TOTAL_"]) if one else 0) + (int(two[0]["TOTAL_"]) if two else 0)
    for r in one + two:
        r.pop("TOTAL_", None)
        stored, live = r["STORED"], r["LIVE"]
        r["STORED"], r["LIVE"] = _num(stored), _num(live)
        r["DIFF"] = _num(round(float(stored or 0) - float(live or 0), 2))
    out = sorted(one + two, key=lambda r: -abs(float(r["DIFF"] or 0)))
    return {"columns": ["BALANCE", "RDC", "ST_CD", "MAJ_CAT", "ARTICLE_NUMBER", "SZ", "STORED", "LIVE", "DIFF"],
            "rows": _cut(out, top), "total": total,
            "note": "Rows whose stored balance differs from the live figure (live MSA free stock / live room, less every "
                    "parked session), largest difference first. A run re-applies them before it reads; "
                    "“Re-apply balances” updates the Step 1 / 2 tables now."}


_ROW_BUILDERS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "sources": _rows_sources, "freshness": _rows_freshness, "rules": _rows_rules,
    "cov_grid": _rows_cov_grid, "cov_curve": _rows_cov_curve, "cov_gap": _rows_cov_gap,
    "sessions_parked": _rows_sessions_parked, "sessions_approved": _rows_sessions_approved,
    "balance_drift": _rows_balance_drift,
}


def item_rows(scope: str, key: str, top: Optional[int] = ROW_TOP) -> Dict[str, Any]:
    """{columns, rows, total, note, truncated, seconds, …} — the list behind one
    readiness item, by the same rule the check counts (scope build2 = today's
    grid, run = the built Step 2). top = None returns every row (export). Read only."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    kind = row_kind(scope, key)
    if kind is None:
        raise ValueError(f"Readiness item '{key}' has no list for the {SCOPE_WORDS[scope]}")
    if top is not None:
        top = min(max(int(top), 1), 100_000)
    t0 = time.perf_counter()
    with _reader() as conn:
        b1 = _latest(conn, 1)
        b2 = _latest(conn, 2) if scope == "run" else None
        try:
            out = _ROW_BUILDERS[kind](conn, scope, b1, b2, top)
        except Exception as e:  # noqa: BLE001
            if _busy(e):
                raise RuntimeError("A table this list reads is locked by another job — try again shortly.") from e
            raise
    out["rows"] = [{k: B.jsonable(v) for k, v in r.items()} for r in out["rows"]]
    out.update(scope=scope, key=key, kind=kind, top=top, truncated=out["total"] > len(out["rows"]),
               seconds=round(time.perf_counter() - t0, 2))
    return out


def export_csv(scope: str, key: str) -> Tuple[str, Iterator[bytes]]:
    """(file name, CSV chunks) of the whole list behind an item. The rows are
    read before the first chunk, so a bad key or a locked table fails early."""
    data = item_rows(scope, key, top=None)
    cols = data["columns"]
    name = f"1SZ_READINESS_{scope}_{re.sub(r'[^A-Za-z0-9]+', '_', key).strip('_').upper()[:60]}.csv"

    def gen() -> Iterator[bytes]:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(cols)
        for i, r in enumerate(data["rows"], 1):
            w.writerow(["" if r.get(c) is None else r.get(c) for c in cols])
            if i % 5000 == 0:
                yield buf.getvalue().encode("utf-8")
                buf.seek(0)
                buf.truncate(0)
        if buf.tell():
            yield buf.getvalue().encode("utf-8")
    return name, gen()
