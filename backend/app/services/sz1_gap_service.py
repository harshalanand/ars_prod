"""
1SZ ALC — CONT gap report (ARS_SZ1_GAP). Spec: manual/sz1_alloc.md §B.

Every Step 1 build records the size-applicable articles with no company size
contribution — the whole option (OPT_NO_CONT: OPT_CONT_PCT = 0) or one size
(SZ_NO_CONT: ART_CONT_PCT = 0 in an option that has some) — and, with
GAP_EXCLUDE on, keeps them out of SZ Base. This module only reads that table.
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any, Dict, Iterator, Optional, Tuple

from sqlalchemy import text

from app.database.session import data_engine
from app.services import sz1_schema as S
from app.services.sz1_base_service import q, rows

GAP_WORDS = {"OPT_NO_CONT": "Whole option has no CONT", "SZ_NO_CONT": "Size has no CONT"}
SORTS = {"pcs": "FNL_Q DESC, RDC, MAJ_CAT", "cat": "RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, SZ",
         "opt": "RDC, OPT_KEY, SZ"}


def summary() -> Dict[str, Any]:
    S.ensure_tables()
    G = q(S.GAP)
    from app.services.sz1_base_service import latest_build
    b1 = latest_build(1) or {}
    with data_engine.connect() as conn:
        build = conn.execute(text(f"SELECT MAX(BUILD_ID), MAX(BUILT_AT) FROM {G}")).fetchone()
        # the complete MSA stock, bucketed (validates the gap): the one stored with
        # the same build, else worked out now from today's MSA and saved rules
        recon = (b1.get("summary") or {}).get("msa_recon") if b1.get("BUILD_ID") == build[0] else None
        if recon:
            recon = {**recon, "source": "build"}
        else:
            from app.services import sz1_base_service, sz1_settings
            recon = {**sz1_base_service.msa_recon(conn, sz1_settings.get_all()), "source": "live"}
        return {
            "build_id": build[0], "built_at": build[1].isoformat() if build[1] else None,
            "msa_recon": recon,
            "by_type": rows(conn, f"""
                SELECT GAP_TYPE, COUNT(DISTINCT RDC + '|' + OPT_KEY) AS OPTIONS, COUNT(*) AS ARTICLES,
                       SUM(FNL_Q) AS FNL_Q, SUM(CAST(EXCLUDED AS INT)) AS EXCLUDED,
                       ISNULL(SUM(CASE WHEN EXCLUDED = 1 THEN FNL_Q END), 0) AS EXCLUDED_FNL_Q
                  FROM {G} GROUP BY GAP_TYPE ORDER BY GAP_TYPE"""),
            "by_rdc": rows(conn, f"""
                SELECT RDC,
                       COUNT(DISTINCT CASE WHEN GAP_TYPE = 'OPT_NO_CONT' THEN OPT_KEY END) AS OPT_NO_CONT_OPTIONS,
                       SUM(CASE WHEN GAP_TYPE = 'OPT_NO_CONT' THEN 1 ELSE 0 END) AS OPT_NO_CONT_ARTICLES,
                       SUM(CASE WHEN GAP_TYPE = 'SZ_NO_CONT' THEN 1 ELSE 0 END) AS SZ_NO_CONT_ARTICLES,
                       COUNT(*) AS ARTICLES, SUM(FNL_Q) AS FNL_Q, SUM(CAST(EXCLUDED AS INT)) AS EXCLUDED
                  FROM {G} GROUP BY RDC ORDER BY RDC"""),
            "by_src": rows(conn, f"""
                SELECT GAP_TYPE, CONT_SRC, COUNT(*) AS ARTICLES, SUM(FNL_Q) AS FNL_Q
                  FROM {G} GROUP BY GAP_TYPE, CONT_SRC ORDER BY GAP_TYPE, COUNT(*) DESC"""),
            # One row per category, every list DISTINCT. SIZES_WITHOUT_CONT covers both
            # gap types; MASTER_SIZES = the sizes Master_CONT_SZ does carry (> 0, any
            # store or CO) — side by side they show why a size has no CONT.
            "by_cat": rows(conn, f"""
                WITH g AS (SELECT * FROM {G}),
                top_cat AS (SELECT TOP 100 MAJ_CAT, SUM(FNL_Q) AS FNL_Q FROM g GROUP BY MAJ_CAT ORDER BY SUM(FNL_Q) DESC),
                sz  AS (SELECT DISTINCT MAJ_CAT, SZ FROM g),
                src AS (SELECT DISTINCT MAJ_CAT, CONT_SRC FROM g),
                rdc AS (SELECT DISTINCT MAJ_CAT, RDC FROM g),
                ms  AS (SELECT DISTINCT m.MAJ_CAT, m.SZ FROM {q(S.CONT_SZ)} m
                         JOIN top_cat t ON t.MAJ_CAT = m.MAJ_CAT WHERE TRY_CAST(m.CONT AS FLOAT) > 0)
                SELECT  t.MAJ_CAT,
                        (SELECT STRING_AGG(RDC, ', ') WITHIN GROUP (ORDER BY RDC) FROM rdc WHERE rdc.MAJ_CAT = t.MAJ_CAT) AS RDCS,
                        (SELECT COUNT(DISTINCT RDC + '|' + OPT_KEY) FROM g WHERE g.MAJ_CAT = t.MAJ_CAT AND GAP_TYPE = 'OPT_NO_CONT') AS OPT_NO_CONT_OPTIONS,
                        (SELECT COUNT(*) FROM g WHERE g.MAJ_CAT = t.MAJ_CAT AND GAP_TYPE = 'SZ_NO_CONT') AS SZ_NO_CONT_ARTICLES,
                        (SELECT STRING_AGG(SZ, ', ') WITHIN GROUP (ORDER BY SZ) FROM sz WHERE sz.MAJ_CAT = t.MAJ_CAT) AS SIZES_WITHOUT_CONT,
                        ISNULL((SELECT STRING_AGG(SZ, ', ') WITHIN GROUP (ORDER BY SZ) FROM ms WHERE ms.MAJ_CAT = t.MAJ_CAT), '(none)') AS MASTER_SIZES,
                        (SELECT STRING_AGG(CONT_SRC, ', ') WITHIN GROUP (ORDER BY CONT_SRC) FROM src WHERE src.MAJ_CAT = t.MAJ_CAT) AS CONT_SRC,
                        (SELECT COUNT(*) FROM g WHERE g.MAJ_CAT = t.MAJ_CAT) AS ARTICLES,
                        t.FNL_Q,
                        (SELECT SUM(CAST(EXCLUDED AS INT)) FROM g WHERE g.MAJ_CAT = t.MAJ_CAT) AS EXCLUDED
                  FROM  top_cat t ORDER BY t.FNL_Q DESC"""),
        }


def _filter(rdc, maj_cat, gap_type, only_excluded, art, cf=None) -> Tuple[str, Dict[str, Any]]:
    from app.services import sz1_filter
    where, p = sz1_filter.where(S.GAP, cf or {})
    if rdc:
        where.append("RDC = :rdc"); p["rdc"] = rdc.strip().upper()
    if maj_cat:
        where.append("MAJ_CAT LIKE :mc"); p["mc"] = f"%{maj_cat.strip()}%"
    if gap_type in GAP_WORDS:
        where.append("GAP_TYPE = :gt"); p["gt"] = gap_type
    if only_excluded:
        where.append("EXCLUDED = 1")
    if art:
        where.append("(ARTICLE_NUMBER = :art OR GEN_ART_NUMBER = :art)"); p["art"] = art.strip()
    return ("WHERE " + " AND ".join(where)) if where else "", p


def lines(rdc=None, maj_cat=None, gap_type=None, only_excluded=False, art=None, sort="pcs",
          page: int = 1, size: int = 100, cf=None, ob=None, od=None) -> Dict[str, Any]:
    S.ensure_tables()
    w, p = _filter(rdc, maj_cat, gap_type, only_excluded, art, cf)
    size = min(max(int(size), 1), 500)
    off = (max(int(page), 1) - 1) * size
    with data_engine.connect() as conn:
        tot = conn.execute(text(f"SELECT COUNT(*), ISNULL(SUM(FNL_Q), 0) FROM {q(S.GAP)} {w}"), p).fetchone()
        data = rows(conn, f"""
            SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, ARTICLE_NUMBER, SZ, GAP_TYPE, CONT_SRC, ART_CONT_PCT,
                   OPT_CONT_PCT, FNL_Q, OPT_FNL, VAR_COUNT, RULES_IF_KEPT, EXCLUDED
              FROM {q(S.GAP)} {w} ORDER BY {_order(sort, ob, od)}
             OFFSET {off} ROWS FETCH NEXT {size} ROWS ONLY""", p)
    return {"rows": data, "total": int(tot[0]), "fnl_q": float(tot[1]), "page": page, "size": size}


def _order(sort, ob=None, od=None) -> str:
    from app.services import sz1_filter
    return sz1_filter.order_by(S.GAP, ob, od, SORTS.get(sort, SORTS["pcs"]))


def export_csv(rdc=None, maj_cat=None, gap_type=None, only_excluded=False, art=None, cf=None,
               sort="cat", ob=None, od=None) -> Iterator[bytes]:
    S.ensure_tables()
    w, p = _filter(rdc, maj_cat, gap_type, only_excluded, art, cf)
    args = [p[m] for m in re.findall(r":(\w+)", w)]
    w = re.sub(r":\w+", "?", w)
    raw = data_engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(f"SELECT * FROM {q(S.GAP)} WITH (NOLOCK) {w} ORDER BY {_order(sort, ob, od)}", *args)
        buf = io.StringIO()
        wr = csv.writer(buf)
        wr.writerow([d[0] for d in cur.description])
        while True:
            chunk = cur.fetchmany(10_000)
            if not chunk:
                break
            wr.writerows(chunk)
            yield buf.getvalue().encode("utf-8")
            buf.seek(0); buf.truncate(0)
        if buf.tell():
            yield buf.getvalue().encode("utf-8")
    finally:
        try:
            raw.close()
        except Exception:
            pass
