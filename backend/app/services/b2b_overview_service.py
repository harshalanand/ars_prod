"""
GRT ALC — Overview: where the pipeline stands and what needs attention.

Every check here runs against the LOADED ARS_B2B_* tables, so the page
reflects the data the next build and allocation will actually use. The upload
check (b2b_upload_service) runs the same ideas against a workbook before it
is loaded.

While a load is running its transaction holds TRUNCATE locks on the data
tables, so any query on them would wait until it commits. The overview then
answers from ARS_B2B_UPLOAD alone and says a load is in progress.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from app.database.session import data_engine
from app.services import b2b_schema as S
from app.services import b2b_alloc_service as A
from app.services import b2b_mbq_service as M
from app.services import b2b_settings, b2b_upload_service as U


def _v(x):
    if isinstance(x, Decimal):
        return float(x)
    if isinstance(x, datetime):
        return x.isoformat()
    return x


def _check(code, level, title, detail="", *, count=None, units=None, sample=None, link=None):
    return {"code": code, "level": level, "title": title, "detail": detail,
            "count": count, "units": units, "sample": [str(s) for s in (sample or [])][:25],
            "link": link}


def overview() -> Dict[str, Any]:
    S.ensure_tables()
    busy = U.running_job()
    busy_row = U.get(busy) if busy else None
    if busy_row and busy_row["STATUS"] == "LOADING":
        return {"loading": busy_row, "uploads": U.recent(5)}

    # A build holds the demand table in its transaction until it commits;
    # everything else stays readable, so only that table is skipped.
    building = M.running_build()
    skip = (S.ART_MBQ,) if M.writing() else ()
    build = M.latest_done()
    fresh = M.freshness(build)
    with data_engine.connect() as conn:
        tiles = _tiles(conn, build, fresh)
        checks = _checks(conn, fresh)
        legacy = _legacy(conn)
    settings = b2b_settings.get_all()
    last = U.last_loaded()
    src = {}
    for s in settings:
        src[s["source"] or "MISSING"] = src.get(s["source"] or "MISSING", 0) + 1

    if building:
        b = M.get(building) or {}
        mbq_state, mbq_note = "running", f"build {building} · {b.get('PROGRESS_PCT') or 0}%"
    elif build:
        mbq_state = {"fresh": "done", "aging": "done", "stale": "warn"}.get(fresh["state"], "todo")
        mbq_note = (f"build {build['BUILD_ID']} · {(build.get('FINISHED_AT') or '')[:16].replace('T', ' ')}"
                    + (" · out of date" if fresh["state"] == "stale" else ""))
    else:
        mbq_state, mbq_note = ("todo" if last else "later"), "not built yet"

    running_sid = A.running_run()
    real = next((s for s in A.recent(20) if s["STATUS"] == "DONE" and not s["DRY_RUN"]), None)
    if running_sid:
        run_state, run_note = "running", f"session {running_sid} running"
    elif real:
        run_state = "done" if real["CHECKS_PASSED"] else "warn"
        run_note = f"session {real['SESSION_ID']} · {int(real['UNITS_ALLOCATED'] or 0):,} units"
    else:
        run_state, run_note = ("todo" if build else "later"), "no session yet"
    pipeline = [
        {"step": 1, "key": "upload", "name": "Upload",
         "state": "done" if last else "todo",
         "note": (f"upload {last['UPLOAD_ID']} · {(last['LOADED_AT'] or '')[:16].replace('T', ' ')}"
                  if last else "nothing loaded yet")},
        {"step": 2, "key": "settings", "name": "Settings",
         "state": "warn" if any(c["code"] == "G6" and c["level"] != "ok" for c in checks) else "done",
         "note": f"{sum(1 for s in settings if s['value'] is not None)} of {len(settings)} saved"},
        {"step": 3, "key": "mbq", "name": "Build MBQ", "state": mbq_state, "note": mbq_note},
        {"step": 4, "key": "run", "name": "Allocate", "state": run_state, "note": run_note},
        {"step": 5, "key": "sessions", "name": "Review",
         "state": ("done" if real and real["CHECKS_PASSED"] else "warn" if real else "later"),
         "note": (f"session {real['SESSION_ID']} · pick list {'ready' if real['CHECKS_PASSED'] else 'locked'}"
                  if real else "no stored session")},
    ]
    return {"pipeline": pipeline, "tiles": tiles, "checks": checks,
            "settings_sources": src, "legacy": legacy,
            "uploads": U.recent(5), "tables": S.table_counts(skip),
            "running": busy_row, "building": M.get(building) if building else None,
            "session": A.get(running_sid) if running_sid else None}


def _tiles(conn, build: Optional[Dict[str, Any]], fresh: Dict[str, Any]) -> Dict[str, Any]:
    b = conn.execute(text(f"""
        SELECT COUNT(*), ISNULL(SUM(CAST(QTY AS BIGINT)), 0),
               COUNT(DISTINCT ART), COUNT(DISTINCT BIN), COUNT(DISTINCT MAJ_CAT)
          FROM dbo.{S.BIN_MASTER}""")).fetchone()
    by_rdc = {r[0] or "(none)": {"rows": int(r[1]), "qty": int(r[2] or 0)}
              for r in conn.execute(text(f"""
                  SELECT BIN_RDC, COUNT(*), SUM(CAST(QTY AS BIGINT))
                    FROM dbo.{S.BIN_MASTER} GROUP BY BIN_RDC ORDER BY BIN_RDC"""))}
    st = conn.execute(text(f"SELECT COUNT(*) FROM dbo.{S.STORE_MASTER}")).scalar()
    st_rdc = {r[0] or "(none)": int(r[1]) for r in conn.execute(text(
        f"SELECT RDC, COUNT(*) FROM dbo.{S.STORE_MASTER} GROUP BY RDC ORDER BY RDC"))}
    q = conn.execute(text(f"""
        SELECT COUNT(*), COUNT(DISTINCT STORE_CODE), ISNULL(SUM(REQ), 0), COUNT(DISTINCT MAJ_CAT)
          FROM dbo.{S.REQ}""")).fetchone()
    return {
        "bin": {"rows": int(b[0]), "qty": int(b[1]), "articles": int(b[2]), "bins": int(b[3]),
                "categories": int(b[4]), "by_rdc": by_rdc},
        "store": {"rows": int(st or 0), "by_rdc": st_rdc},
        "req": {"rows": int(q[0]), "stores": int(q[1]), "units": float(q[2]), "categories": int(q[3])},
        # From the build record, not the table: the table may be mid-rewrite.
        "mbq": ({"rows": build.get("ROWS_BUILT") or 0, "build_id": build["BUILD_ID"],
                 "rows_short": build.get("ROWS_SHORT") or 0,
                 "shortfall": build.get("SHORTFALL_TOTAL") or 0, "state": fresh["state"]}
                if build else {"rows": 0, "state": "none"}),
    }


def _checks(conn, fresh: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    have_req = conn.execute(text(f"SELECT TOP 1 1 FROM dbo.{S.REQ}")).scalar()
    have_bin = conn.execute(text(f"SELECT TOP 1 1 FROM dbo.{S.BIN_MASTER}")).scalar()
    have_st = conn.execute(text(f"SELECT TOP 1 1 FROM dbo.{S.STORE_MASTER}")).scalar()
    if not (have_req or have_bin or have_st):
        out.append(_check("EMPTY", "info", "Nothing has been loaded yet",
                          "Start with Upload Data.", link="upload"))

    # G1 — REQ stores missing from Store Master
    if have_req:
        rows = conn.execute(text(f"""
            SELECT r.STORE_CODE, SUM(r.REQ)
              FROM dbo.{S.REQ} r
             WHERE NOT EXISTS (SELECT 1 FROM dbo.{S.STORE_MASTER} m WHERE m.STORE_CODE = r.STORE_CODE)
             GROUP BY r.STORE_CODE ORDER BY SUM(r.REQ) DESC""")).fetchall()
        if rows:
            units = float(sum(r[1] or 0 for r in rows))
            out.append(_check("G1", "warn", f"{len(rows):,} REQ store(s) are missing from Store Master",
                              f"{units:,.0f} units they asked for can never be sent. Nothing errors.",
                              count=len(rows), units=units,
                              sample=[f"{r[0]} ({float(r[1] or 0):,.0f})" for r in rows], link="upload"))
        else:
            out.append(_check("G1", "ok", "Every REQ store is in Store Master"))

    # G2 — categories in one table only, and likely spelling drift
    if have_req and have_bin:
        only_bin = [r[0] for r in conn.execute(text(f"""
            SELECT DISTINCT MAJ_CAT FROM dbo.{S.BIN_MASTER}
            EXCEPT SELECT DISTINCT MAJ_CAT FROM dbo.{S.REQ} ORDER BY 1"""))]
        only_req = [r[0] for r in conn.execute(text(f"""
            SELECT DISTINCT MAJ_CAT FROM dbo.{S.REQ}
            EXCEPT SELECT DISTINCT MAJ_CAT FROM dbo.{S.BIN_MASTER} ORDER BY 1"""))]
        key = lambda c: re.sub(r"[^A-Z0-9]", "", str(c).upper())
        rk = {key(c): c for c in only_req}
        drift = [(b, rk[key(b)]) for b in only_bin if key(b) in rk]
        if drift:
            out.append(_check("G2_DRIFT", "warn",
                              f"{len(drift):,} category name(s) differ only in spelling",
                              "Stock and requirement for these never meet.",
                              count=len(drift), sample=[f"{a}  ≠  {b}" for a, b in drift]))
        if only_bin or only_req:
            out.append(_check("G2", "warn",
                              f"{len(only_bin) + len(only_req):,} categories appear in only one sheet",
                              f"{len(only_bin):,} have stock and no REQ · {len(only_req):,} have REQ and no stock",
                              count=len(only_bin) + len(only_req),
                              sample=[f"stock only: {c}" for c in only_bin][:12]
                                     + [f"REQ only: {c}" for c in only_req][:13]))
        else:
            out.append(_check("G2", "ok", "Every category appears in both sheets"))

    # G3 — every bin names a warehouse Store Master knows
    if have_bin:
        # SQL Server allows no subquery inside an aggregate, hence the join.
        r = conn.execute(text(f"""
            SELECT SUM(CASE WHEN b.BIN_RDC IS NULL THEN 1 ELSE 0 END),
                   SUM(CASE WHEN b.BIN_RDC IS NOT NULL AND m.RDC IS NULL THEN 1 ELSE 0 END)
              FROM dbo.{S.BIN_MASTER} b
              LEFT JOIN (SELECT DISTINCT RDC FROM dbo.{S.STORE_MASTER}) m ON m.RDC = b.BIN_RDC
        """)).fetchone()
        nodash, unknown = int(r[0] or 0), int(r[1] or 0)
        if nodash or unknown:
            out.append(_check("G3", "warn", f"{nodash + unknown:,} bin row(s) have no usable warehouse",
                              f"{nodash:,} have none · {unknown:,} are in a warehouse Store Master has "
                              f"no stores for. Home only can never ship these.", count=nodash + unknown))
        else:
            rows = conn.execute(text(f"""
                SELECT BIN_RDC, COUNT(*), SUM(CAST(QTY AS BIGINT)) FROM dbo.{S.BIN_MASTER}
                 GROUP BY BIN_RDC ORDER BY 1""")).fetchall()
            out.append(_check("G3", "ok", "Every bin's warehouse has stores in Store Master",
                              " · ".join(f"{x[0]} {int(x[1]):,} rows, {int(x[2] or 0):,} pcs" for x in rows)))

    # G4 — one category and one size per article
    if have_bin:
        n = conn.execute(text(f"""
            SELECT COUNT(*) FROM (
                SELECT ART FROM dbo.{S.BIN_MASTER} GROUP BY ART
                HAVING COUNT(DISTINCT MAJ_CAT) > 1 OR COUNT(DISTINCT ISNULL([SIZE], N'')) > 1) x""")).scalar()
        out.append(_check("G4", "warn" if n else "ok",
                          f"{n:,} article(s) have more than one category or size" if n
                          else "Every article has one category and one size",
                          "The demand build would keep only one." if n else "", count=int(n or 0)))

    # G8 — duplicate article-bin rows double the stock
    if have_bin:
        r = conn.execute(text(f"""
            SELECT COUNT(*), ISNULL(SUM(q), 0) FROM (
                SELECT SUM(CAST(QTY AS BIGINT)) q FROM dbo.{S.BIN_MASTER}
                 GROUP BY ART, BIN HAVING COUNT(*) > 1) x""")).fetchone()
        if r[0]:
            out.append(_check("G8", "warn", f"{int(r[0]):,} article-bin pair(s) are loaded more than once",
                              "Usually a repeated Append. Their stock is counted twice.",
                              count=int(r[0]), units=int(r[1]), link="upload"))

    # G6 + G11 — settings
    settings = {s["key"]: s for s in b2b_settings.get_all()}
    missing = [k for k, s in settings.items() if s["value"] is None]
    out.append(_check("G6", "warn" if missing else "ok",
                      f"{len(missing)} setting(s) have no saved value" if missing
                      else f"All {len(settings)} settings are saved",
                      ", ".join(missing), link="settings"))
    # G11 — the warehouse rule is now chosen on every run, never defaulted. What
    # remains worth flagging is a stored session that did send stock across.
    real = next((s for s in A.recent(20) if s["STATUS"] == "DONE" and not s["DRY_RUN"]), None)
    words = {"SAME": "Own RDC only", "HOME_FIRST": "Own RDC first", "ANY": "All RDCs"}
    if real and (real.get("CROSS_RDC_UNITS") or 0) > 0:
        share = real["CROSS_RDC_UNITS"] / max(real.get("UNITS_ALLOCATED") or 1, 1) * 100
        out.append(_check("G11", "warn",
                          f"Session {real['SESSION_ID']} sends {share:.1f}% of its units across warehouses",
                          f"It ran with {words.get(real['ALLOC_CROSS_RDC'], real['ALLOC_CROSS_RDC'])}: "
                          f"{int(real['CROSS_RDC_UNITS']):,} units go from one RDC to another RDC's stores. "
                          f"Confirm DH24 and DW01 ship to each other before the pick list is worked.",
                          units=float(real["CROSS_RDC_UNITS"]), link="run"))
    else:
        out.append(_check("G11", "ok", "The warehouse rule is chosen on every run",
                          (f"Latest session {real['SESSION_ID']}: {words.get(real['ALLOC_CROSS_RDC'])}, "
                           f"nothing crossed warehouses." if real else
                           "Run Allocation asks for it each time; there is no default.")))

    # G7 — the demand table is what the current data and settings would give
    if U.last_loaded():
        if fresh["state"] == "none":
            out.append(_check("G7", "warn", "The demand table has not been built from this data",
                              "Build MBQ turns the loaded sheets into a target and a shortfall per "
                              "store and article. Allocation needs it.", link="mbq"))
        elif fresh["state"] == "stale":
            out.append(_check("G7", "warn", "The demand table is out of date",
                              " ".join(fresh["reasons"]) + " Rebuild before allocating.", link="mbq"))
        elif fresh["state"] == "aging":
            out.append(_check("G7", "info", "Store stock has changed since the last build",
                              " ".join(fresh["notes"]), link="mbq"))
        else:
            out.append(_check("G7", "ok", "The demand table matches the loaded data and settings"))

    order = {"block": 0, "warn": 1, "info": 2, "ok": 3}
    out.sort(key=lambda c: order.get(c["level"], 9))
    return out


def _legacy(conn) -> Optional[Dict[str, Any]]:
    """The Streamlit tool's own tables, side by side, for the parallel run."""
    if not S.table_exists(conn, "B2B_BIN_MASTER"):
        return None
    out: Dict[str, Any] = {}
    for key, tbl, expr in (("bin", "B2B_BIN_MASTER", "COUNT(*), SUM(CAST(QTY AS BIGINT))"),
                           ("store", "B2B_STORE_MASTER", "COUNT(*), NULL"),
                           ("req", "B2B_REQ", "COUNT(*), SUM(REQ)")):
        if S.table_exists(conn, tbl):
            r = conn.execute(text(f"SELECT {expr} FROM dbo.{tbl}")).fetchone()
            out[key] = {"rows": int(r[0]), "total": _v(r[1])}
    if S.table_exists(conn, "B2B_ALLOC_SESSION"):
        r = conn.execute(text("""
            SELECT TOP 1 SESSION_ID, CREATED_AT, UNITS_ALLOCATED, ALLOC_FILL_MODE, ALLOC_CROSS_RDC
              FROM dbo.B2B_ALLOC_SESSION ORDER BY SESSION_ID DESC""")).fetchone()
        if r:
            out["session"] = {"id": r[0], "at": _v(r[1]), "units": _v(r[2]),
                              "fill": r[3], "cross": r[4]}
    return out
