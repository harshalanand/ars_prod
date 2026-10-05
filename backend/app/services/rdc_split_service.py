"""
Central RDC Pool — the RDC split child table.

Spec: docs/superpowers/specs/2026-09-21-central-rdc-pool-allocation-brd-fsd-v1.5.md
      §B7.1 (table), §B7.2 (lifecycle), §B6 (the pass that fills it).

WHAT THIS TABLE IS
------------------
Under `All RDCs`, allocation runs against ONE clubbed pool and only afterwards
decides which warehouse physically ships each line (Part 8.37, Step 3). That
decision cannot live on ARS_ALLOC_WORKING: one store-size may be sourced from
two warehouses, and ARS_ALLOC_WORKING's one-row-per-(WERKS, option, VAR_ART, SZ)
grain is relied on by parking, Approve, alloc review and reporting (risk R6).

So the split lives here, as a child: one row per allocation line PER SOURCE RDC.
Rows are written for every line, including single-source ones, so the picking
requirement is a single clean GROUP BY.

MODE SEPARATION (BR-RDC-11)
---------------------------
An `Own` or `Cross` run produces ZERO rows here. Only `All RDCs` with the
ALC_RDC_CENTRAL_POOL switch active writes to this table. Invariant I-11 /
check V14 fail the build if that is ever violated.

SCOPE
-----
Step 1 created the table. Step 3 (this file) adds the split pass itself.
The Approve-time read that feeds the reservation ledgers is Step 4.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import text

SPLIT_TABLE = "ARS_ALLOC_RDC_SPLIT"

# Sourcing policies (D1 / §B6.3). SHIP lines only — every HOLD line runs
# SINGLE_STRICT with a cap of 1 regardless, per BR-RDC-12.
SPLIT_ALWAYS = "SPLIT_ALWAYS"
SINGLE_PREFERRED = "SINGLE_PREFERRED"
SINGLE_STRICT = "SINGLE_STRICT"
VALID_POLICIES = (SPLIT_ALWAYS, SINGLE_PREFERRED, SINGLE_STRICT)

# Quantities below this are treated as zero. The engine works in whole pieces
# after pak rounding, but FLOAT arithmetic on the ledger can leave dust.
EPS = 1e-6

# ── Lifecycle (2026-10-03) ───────────────────────────────────────────────────
# This table is the WORKING copy — it holds the CURRENT run only, exactly like
# ARS_ALLOC_WORKING and ARS_LISTING. Parking copies it to
# ARS_ALLOC_RDC_SPLIT_PARKED and Approve promotes that to
# ARS_ALLOC_RDC_SPLIT_HISTORY, both driven by the generic machinery in
# parked_history._SNAPSHOT_TARGETS.
#
# It deliberately carries NO SESSION_ID: the snapshot machinery copies the
# whole source table (`SELECT … FROM <source>`, no WHERE) and stamps SESSION_ID
# itself as a control column, so a SESSION_ID here would collide with it and a
# multi-session source would be parked wholesale under one id.
#
# Before 2026-10-03 this was a single durable session-keyed table with
# delete-on-reject / delete-on-revert. That could not survive a revert: the
# other targets demote HISTORY→PARKED so a run can be re-approved, but split
# rows were destroyed outright, and a 'MULTI' line's per-warehouse breakdown
# exists nowhere else — so re-approval wrote the wrong per-source pending.
# It also orphaned rows: session 20260926_085716_106 held 19,519 split rows
# after its allocation data had gone, still reading as a live pick instruction.
_DDL = f"""
IF OBJECT_ID('dbo.{SPLIT_TABLE}','U') IS NULL
CREATE TABLE dbo.{SPLIT_TABLE} (
    WERKS           NVARCHAR(50)   NOT NULL,   -- DESTINATION store
    MAJ_CAT         NVARCHAR(200)  NULL,
    GEN_ART_NUMBER  BIGINT         NULL,
    CLR             NVARCHAR(200)  NULL,
    VAR_ART         BIGINT         NOT NULL,
    SZ              NVARCHAR(50)   NOT NULL,
    SRC_RDC         NVARCHAR(20)   NOT NULL,   -- SOURCING warehouse (ships the box)
    SHIP_QTY        FLOAT          NOT NULL DEFAULT 0,
    HOLD_QTY        FLOAT          NOT NULL DEFAULT 0,
    ALLOC_TYPE      NVARCHAR(10)   NOT NULL DEFAULT '',   -- FRESH / GRT
    STORE_RDC       NVARCHAR(20)   NULL,       -- store's own tag, '' if untagged (audit)
    PREF_TIER       NVARCHAR(2)    NULL,       -- 1 | 1F | 2 | 3  (§B6.4)
    IS_CROSS        BIT            NOT NULL DEFAULT 0,    -- SRC_RDC <> STORE_RDC
    CREATED_AT      DATETIME       NOT NULL DEFAULT GETDATE(),
    CONSTRAINT PK_{SPLIT_TABLE} PRIMARY KEY CLUSTERED
        (WERKS, VAR_ART, SZ, SRC_RDC, ALLOC_TYPE)
)
"""

# ALLOC_TYPE sits in the PK, mirroring the Fresh/GRT widening already applied to
# ARS_NL_TBL_HOLD_TRACKING: one store-size can carry a FRESH and a GRT line in
# the same run.

_INDEXES = [
    # The picking requirement: GROUP BY SRC_RDC over the run.
    (f"IX_{SPLIT_TABLE}_PICK",
     f"ON dbo.{SPLIT_TABLE} (SRC_RDC) INCLUDE (SHIP_QTY, HOLD_QTY)"),
    # The Approve-time join in pend_alc_service.write_pend_alc (Step 4, §B7.3 ii)
    # now runs against the HISTORY copy, but the working table keeps the same
    # covering index so the post-run summary and reports stay seek-based.
    (f"IX_{SPLIT_TABLE}_PEND",
     f"ON dbo.{SPLIT_TABLE} (WERKS, VAR_ART, SZ)"),
]


def _is_legacy_shape(conn) -> bool:
    """True when the live table still carries SESSION_ID (pre-2026-10-03)."""
    return bool(conn.execute(text(
        f"SELECT CASE WHEN COL_LENGTH('dbo.{SPLIT_TABLE}','SESSION_ID') "
        f"IS NULL THEN 0 ELSE 1 END"
    )).scalar())


def ensure_split_table(conn) -> None:
    """Create the working split table and its indexes. Idempotent — safe on
    every run, matching pend_alc_service.ensure_pend_alc_table.

    Refuses to run against the pre-2026-10-03 session-keyed shape rather than
    silently writing into it: the INSERT would fail on the NOT NULL SESSION_ID
    and any rows still there belong to other sessions. Run
    `scripts/migrate_rdc_split_to_snapshot.py` once to convert.
    """
    if _is_legacy_shape(conn):
        raise RuntimeError(
            f"{SPLIT_TABLE} still has the legacy SESSION_ID column. Run "
            f"scripts/migrate_rdc_split_to_snapshot.py to move existing rows "
            f"into {SPLIT_TABLE}_PARKED and rebuild the working table."
        )
    conn.execute(text(_DDL))
    for name, body in _INDEXES:
        conn.execute(text(
            f"IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = '{name}') "
            f"CREATE INDEX {name} {body}"
        ))


def clear_working(conn) -> int:
    """Empty the working table before Part 8.37 writes this run's rows.

    The working table holds one run at a time, so a re-run must start clean or
    it would collide on the PK and leave the previous run's lines behind.
    Reject / revert no longer call in here — the parked and history copies are
    managed by the generic snapshot machinery like every other target.
    """
    res = conn.execute(text(f"DELETE FROM dbo.{SPLIT_TABLE}"))
    n = res.rowcount or 0
    if n:
        logger.info(f"[rdc_split] cleared {n} row(s) from the working split table")
    return n


# ---------------------------------------------------------------------------
# §B6.4 — sourcing preference resolution
# ---------------------------------------------------------------------------
def parse_priority(raw: Optional[str], live_rdcs: List[str]) -> List[str]:
    """Turn the ALC_RDC_PRIORITY rule value into an ordered list of live RDCs.

    The stored separator is '>' ("DW01>DH24") because the rules registry
    validates a `choice` by splitting its allowed-values list on commas, so a
    comma inside the value can never be validated. Commas are still accepted
    here so a hand-edited legacy value keeps working.

    An RDC named in the rule but not live is dropped; a live RDC missing from
    the rule is APPENDED alphabetically. The list is a preference, never a
    filter — no warehouse can be excluded from sourcing by omission (§B6.4.1b).
    """
    live = list(live_rdcs)
    if not raw:
        return []
    parts = [p.strip() for p in str(raw).replace(",", ">").split(">")]
    order = [p for p in parts if p and p in live]
    order += sorted(r for r in live if r not in order)
    return order


def preference_order(store_rdc: Optional[str],
                     priority: List[str],
                     live_rdcs: List[str]) -> Tuple[Optional[List[str]], str]:
    """Return (ordered RDC list, PREF_TIER) for one store.

    Tiers stop at the first that yields a preference (§B6.4):

      1  tag is a live RDC     -> own first, then the priority order
      2  tag blank/'ALL'/junk  -> the priority order alone
      3  no priority set       -> None, meaning "largest available first",
                                  which the caller evaluates per line

    A tag naming an RDC that happens to hold no stock needs no special case:
    tier 1 finds nothing there and the walk moves on.
    """
    tag = (store_rdc or "").strip()
    live = list(live_rdcs)
    if tag and tag in live:
        rest = [r for r in priority if r != tag] if priority else []
        rest += sorted(r for r in live if r != tag and r not in rest)
        return ([tag] + rest), "1"
    if priority:
        order = list(priority)
        order += sorted(r for r in live if r not in order)
        return order, "2"
    return None, "3"


# ---------------------------------------------------------------------------
# §B6.3.2 — the ledger walk for ONE draw
# ---------------------------------------------------------------------------
def _draw(avail: Dict[Tuple[str, Any, str], float],
          key_var: Any, key_sz: str,
          qty: float,
          order: Optional[List[str]],
          live_rdcs: List[str],
          policy: str,
          max_split: int) -> Tuple[List[Tuple[str, float]], float]:
    """Source `qty` pieces of one (VAR_ART, SZ) and return
    ([(rdc, taken), ...], shortfall).

    `avail` is mutated: this IS the shared ledger that SHIP and HOLD both
    consume (D2 / BR-RDC-06). Two independent passes would each see the full
    balance and together over-book the warehouse.

    A non-zero shortfall means the caller must REDUCE the line — either
    SINGLE_STRICT found no single warehouse able to cover it, or the
    max_split cap bound first.
    """
    if qty <= EPS:
        return [], 0.0

    def bal(rdc: str) -> float:
        return avail.get((rdc, key_var, key_sz), 0.0)

    # Tier 3 (no priority configured) is evaluated per line: largest first.
    walk = list(order) if order else sorted(live_rdcs, key=bal, reverse=True)

    taken: List[Tuple[str, float]] = []
    remaining = float(qty)

    def take(rdc: str, n: float) -> None:
        nonlocal remaining
        if n <= EPS:
            return
        avail[(rdc, key_var, key_sz)] = bal(rdc) - n
        taken.append((rdc, n))
        remaining -= n

    # pass 1 — one warehouse covers the WHOLE line
    if policy in (SINGLE_PREFERRED, SINGLE_STRICT):
        for rdc in walk:
            if bal(rdc) >= remaining - EPS:
                take(rdc, remaining)
                return taken, 0.0

    # pass 2 — strict: biggest single source, reduce the rest
    if policy == SINGLE_STRICT:
        best = max(walk, key=bal) if walk else None
        if best is not None:
            take(best, min(remaining, bal(best)))
        return taken, max(remaining, 0.0)

    # pass 3 — capped split walk. Own warehouse stays first (it is already
    # first in `walk`); the remaining candidates are ordered by availability
    # descending so the cap buys the most fill it can (§B6.3.1).
    head, tail = walk[:1], walk[1:]
    cands = (head + sorted(tail, key=bal, reverse=True))[:max(1, int(max_split))]
    for rdc in cands:
        if remaining <= EPS:
            break
        take(rdc, min(remaining, bal(rdc)))

    return taken, max(remaining, 0.0)


# ---------------------------------------------------------------------------
# Part 8.37 — the split pass
# ---------------------------------------------------------------------------
def run_split_pass(conn,
                   alloc_table: str,
                   listing_table: str,
                   msa_var_table: str = "ARS_MSA_VAR_ART",
                   alloc_type: str = "FRESH",
                   policy: str = SINGLE_PREFERRED,
                   max_split: int = 2,
                   priority_raw: Optional[str] = None) -> Dict[str, Any]:
    """Tag every shipped and held piece with the warehouse that will ship it.

    Runs ONCE, single-threaded, after every MAJ_CAT worker has finished and
    BEFORE parking, so ARS_ALLOC_PARKED / _HISTORY inherit SRC_RDC through the
    existing column reconcile with no extra code.

    Single-threaded is required. The MAJ_CAT workers can run in parallel
    because POOL_KEYS still contains MAJ_CAT, but THIS ledger is global.

    Returns the cockpit summary (§B8.4).
    """
    policy = str(policy or SINGLE_PREFERRED).strip().upper()
    if policy not in VALID_POLICIES:
        raise ValueError(f"run_split_pass: bad policy {policy!r}")
    max_split = max(1, int(max_split or 2))

    ensure_split_table(conn)
    # V16 — re-run idempotency. The working table holds one run at a time, so
    # a second attempt must start clean or it collides on the PK and leaves
    # the previous run's lines behind.
    clear_working(conn)

    has_remarks = bool(conn.execute(text(
        f"SELECT CASE WHEN COL_LENGTH('{alloc_table}','ALLOC_REMARKS') "
        f"IS NULL THEN 0 ELSE 1 END"
    )).scalar())

    # ── 1. per-warehouse availability, at option-size grain ───────────────
    rows = conn.execute(text(f"""
        SELECT LTRIM(RTRIM(CAST([RDC] AS NVARCHAR(50))))                AS RDC,
               TRY_CAST(TRY_CAST([ARTICLE_NUMBER] AS FLOAT) AS BIGINT)  AS VAR_ART,
               LTRIM(RTRIM(CAST([SZ] AS NVARCHAR(50))))                 AS SZ,
               SUM(TRY_CAST([FNL_Q] AS FLOAT))                          AS FNL_Q
        FROM   [{msa_var_table}] WITH (NOLOCK)
        WHERE  ISNULL([ALLOC_TYPE],'FRESH') = :at
        GROUP  BY LTRIM(RTRIM(CAST([RDC] AS NVARCHAR(50)))),
                  TRY_CAST(TRY_CAST([ARTICLE_NUMBER] AS FLOAT) AS BIGINT),
                  LTRIM(RTRIM(CAST([SZ] AS NVARCHAR(50))))
    """), {"at": alloc_type}).fetchall()

    avail: Dict[Tuple[str, Any, str], float] = {}
    live_set = set()
    for rdc, var_art, sz, q in rows:
        if not rdc or var_art is None:
            continue
        avail[(rdc, var_art, sz or "")] = float(q or 0.0)
        live_set.add(rdc)
    live_rdcs = sorted(live_set)
    opening_total = sum(avail.values())
    priority = parse_priority(priority_raw, live_rdcs)

    # ── 2. the allocation lines, in waterfall order ───────────────────────
    # ST_RANK then ALLOC_SEQ reproduces the order the engine allocated in, so
    # the tagging is deterministic and reproducible (I-6 / V8). WERKS, VAR_ART
    # and SZ break any remaining tie.
    lines = conn.execute(text(f"""
        SELECT [WERKS],
               LTRIM(RTRIM(CAST(ISNULL([RDC],'') AS NVARCHAR(50)))) AS STORE_RDC,
               [MAJ_CAT], [GEN_ART_NUMBER], [CLR],
               TRY_CAST([VAR_ART] AS BIGINT)                        AS VAR_ART,
               LTRIM(RTRIM(CAST([SZ] AS NVARCHAR(50))))             AS SZ,
               ISNULL(TRY_CAST([SHIP_QTY] AS FLOAT),0)              AS SHIP_QTY,
               ISNULL(TRY_CAST([HOLD_QTY] AS FLOAT),0)              AS HOLD_QTY
        FROM   [{alloc_table}]
        WHERE  ISNULL(TRY_CAST([SHIP_QTY] AS FLOAT),0) > 0
           OR  ISNULL(TRY_CAST([HOLD_QTY] AS FLOAT),0) > 0
        ORDER  BY ISNULL(TRY_CAST([ST_RANK]   AS FLOAT), 999999),
                  ISNULL(TRY_CAST([ALLOC_SEQ] AS INT),   999999),
                  [WERKS], TRY_CAST([VAR_ART] AS BIGINT),
                  LTRIM(RTRIM(CAST([SZ] AS NVARCHAR(50))))
    """)).fetchall()

    stats = _walk_lines(lines, avail, live_rdcs, priority, policy, max_split)
    emit = stats.pop("_emit")
    reductions = stats.pop("_reductions")
    src_by_line = stats.pop("_src_by_line")

    # ── 4. invariants, asserted BEFORE anything is written ────────────────
    neg = [(k, v) for k, v in avail.items() if v < -EPS]
    if neg:
        raise RuntimeError(
            f"[rdc_split] BR-RDC-04 violated — {len(neg)} warehouse balance(s) "
            f"went negative, e.g. {neg[0]}"
        )
    for k, srcs in src_by_line.items():
        if len(srcs) > max_split:
            raise RuntimeError(
                f"[rdc_split] I-7/I-12 violated — line {k} sourced from "
                f"{len(srcs)} warehouses, cap is {max_split}"
            )

    # ── 5. write the split rows ───────────────────────────────────────────
    if emit:
        payload = [dict(v, ALLOC_TYPE=alloc_type) for v in emit.values()]
        conn.execute(text(f"""
            INSERT INTO dbo.{SPLIT_TABLE}
                (WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ,
                 SRC_RDC, SHIP_QTY, HOLD_QTY, ALLOC_TYPE, STORE_RDC,
                 PREF_TIER, IS_CROSS)
            VALUES
                (:WERKS, :MAJ_CAT, :GEN_ART_NUMBER, :CLR, :VAR_ART, :SZ,
                 :SRC_RDC, :SHIP_QTY, :HOLD_QTY, :ALLOC_TYPE, :STORE_RDC,
                 :PREF_TIER, :IS_CROSS)
        """), payload)

    # ── 6. stamp SRC_RDC / SRC_SPLIT_CNT back onto the alloc rows ─────────
    _stamp_alloc(conn, alloc_table, src_by_line)

    # ── 7. apply reductions (never silent — §B6.3.3) ──────────────────────
    if reductions:
        _apply_reductions(conn, alloc_table, listing_table, reductions,
                          has_remarks)

    stats.update({
        "opening_stock": opening_total,
        "closing_stock": sum(avail.values()),
        "live_rdcs": live_rdcs,
        "priority": priority,
        "policy": policy,
        "max_split": max_split,
        "split_rows": len(emit),
    })
    stats["consumed"] = stats["opening_stock"] - stats["closing_stock"]

    logger.info(
        f"[rdc_split] Part 8.37: {stats['lines']:,} lines -> "
        f"{len(emit):,} split rows | ship {stats['ship_tagged']:,.0f} "
        f"hold {stats['hold_tagged']:,.0f} | split {stats['split_lines']:,} "
        f"cross {stats['cross_lines']:,} reduced {stats['reduced_lines']:,} "
        f"({stats['reduced_qty']:,.0f} pcs) | policy={policy} cap={max_split} "
        f"priority={'>'.join(priority) or '(none, largest-first)'}"
    )
    for r in live_rdcs:
        b = stats["by_rdc"].get(r, {"ship": 0.0, "hold": 0.0})
        logger.info(
            f"[rdc_split]   PICK {r}: ship {b['ship']:,.0f} hold {b['hold']:,.0f}"
        )
    return stats


def _walk_lines(lines, avail, live_rdcs, priority, policy, max_split) -> Dict[str, Any]:
    """The ledger walk. Split out so the unit tests can drive it directly with
    a synthetic fixture and no database."""
    # emit[(werks, var_art, sz, src_rdc)] — SHIP and HOLD for the same line and
    # warehouse MERGE into one row, because the split PK is
    # (WERKS, VAR_ART, SZ, SRC_RDC, ALLOC_TYPE).
    emit: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    reductions: List[Dict[str, Any]] = []
    src_by_line: Dict[Tuple[Any, ...], List[str]] = {}
    stats: Dict[str, Any] = {
        "lines": 0, "split_lines": 0, "cross_lines": 0,
        "ship_tagged": 0.0, "hold_tagged": 0.0,
        "reduced_lines": 0, "reduced_qty": 0.0,
        "fallback_lines": 0,
        "by_rdc": {r: {"ship": 0.0, "hold": 0.0} for r in live_rdcs},
        "by_tier": {},
    }

    for (werks, store_rdc, maj_cat, gen_art, clr, var_art, sz,
         ship_qty, hold_qty) in lines:
        if var_art is None:
            continue
        stats["lines"] += 1
        order, tier = preference_order(store_rdc, priority, live_rdcs)
        stats["by_tier"][tier] = stats["by_tier"].get(tier, 0) + 1
        if tier != "1":
            stats["fallback_lines"] += 1

        srcs: List[str] = []

        # SHIP first, then HOLD — ONE shared ledger (D2 / BR-RDC-06). Two
        # independent passes would each see the full balance and together
        # over-book the warehouse.
        for draw_name, qty in (("SHIP", float(ship_qty or 0)),
                               ("HOLD", float(hold_qty or 0))):
            if qty <= EPS:
                continue
            if draw_name == "HOLD":
                # BR-RDC-12 — a hold is a physical reservation at ONE
                # warehouse. The hold tracker is keyed on
                # (WERKS, VAR_ART, SZ, ALLOC_TYPE), so two source rows would
                # collide on the primary key. A short hold reduces instead,
                # which is safe because a hold is advisory.
                p_here, cap_here = SINGLE_STRICT, 1
            else:
                p_here, cap_here = policy, max_split

            taken, short = _draw(avail, var_art, sz, qty, order,
                                 live_rdcs, p_here, cap_here)

            for rdc, n in taken:
                k = (werks, var_art, sz, rdc)
                e = emit.setdefault(k, {
                    "WERKS": werks, "MAJ_CAT": maj_cat,
                    "GEN_ART_NUMBER": gen_art, "CLR": clr,
                    "VAR_ART": var_art, "SZ": sz, "SRC_RDC": rdc,
                    "SHIP_QTY": 0.0, "HOLD_QTY": 0.0,
                    "STORE_RDC": store_rdc or "", "PREF_TIER": tier,
                    "IS_CROSS": 1 if rdc != (store_rdc or "") else 0,
                })
                e[draw_name + "_QTY"] += n
                stats["by_rdc"].setdefault(rdc, {"ship": 0.0, "hold": 0.0})
                bucket = "ship" if draw_name == "SHIP" else "hold"
                stats["by_rdc"][rdc][bucket] += n
                stats[bucket + "_tagged"] += n
                if rdc not in srcs:
                    srcs.append(rdc)

            if short > EPS:
                if draw_name == "HOLD":
                    tag = "RDC_HOLD_SHORT"
                    word = "held"
                elif p_here == SINGLE_STRICT:
                    tag = "RDC_SINGLE_SHORT"
                    word = "shipped"
                else:
                    tag = "RDC_SPLIT_CAPPED"
                    word = "shipped"
                reductions.append({
                    "WERKS": werks, "VAR_ART": var_art, "SZ": sz,
                    "draw": draw_name, "alloc": qty, "tagged": qty - short,
                    "short": short,
                    "remark": "%s(alloc=%g,%s=%g)" % (tag, qty, word, qty - short),
                    "MAJ_CAT": maj_cat, "GEN_ART_NUMBER": gen_art, "CLR": clr,
                })
                stats["reduced_lines"] += 1
                stats["reduced_qty"] += short

        if srcs:
            src_by_line[(werks, var_art, sz)] = srcs
            if len(srcs) > 1:
                stats["split_lines"] += 1
            if any(s != (store_rdc or "") for s in srcs):
                stats["cross_lines"] += 1

    stats["_emit"] = emit
    stats["_reductions"] = reductions
    stats["_src_by_line"] = src_by_line
    return stats


def _stamp_alloc(conn, alloc_table: str,
                 src_by_line: Dict[Tuple[Any, ...], List[str]]) -> None:
    """Write SRC_RDC and SRC_SPLIT_CNT onto ARS_ALLOC_WORKING.

    SRC_RDC is the single sourcing warehouse, or 'MULTI' when the line was
    split. 'MULTI' is deliberately NOT a warehouse code: any consumer that
    needs the real per-warehouse breakdown must read the split table, which is
    exactly what Approve does in Step 4.

    The columns are emitted by the SELECT..INTO that rebuilds the alloc table
    (correction M3), so they always exist by the time this runs; the guard is
    there for a partially-migrated environment.
    """
    if not src_by_line:
        return
    if not conn.execute(text(
        f"SELECT CASE WHEN COL_LENGTH('{alloc_table}','SRC_RDC') "
        f"IS NULL THEN 0 ELSE 1 END"
    )).scalar():
        logger.warning(
            f"[rdc_split] {alloc_table} has no SRC_RDC column — split rows "
            f"written but the alloc table could not be stamped. Check that "
            f"rule_engine_new._stage_b_explode emits it (correction M3)."
        )
        return

    # Set-based via a staging table. An executemany of one UPDATE per line
    # would be ~124k statements, each unable to seek because of the TRY_CAST
    # in the predicate — that measured beyond 8 minutes on a live run (V21).
    # Bulk-load the decisions, index them, then apply in ONE pass.
    payload = [
        {"w": w, "v": v, "s": sz,
         "src": srcs[0] if len(srcs) == 1 else "MULTI",
         "cnt": len(srcs)}
        for (w, v, sz), srcs in src_by_line.items()
    ]
    conn.execute(text("""
        IF OBJECT_ID('tempdb..#rdc_stamp') IS NOT NULL DROP TABLE #rdc_stamp;
        -- COLLATE DATABASE_DEFAULT is required: a temp table inherits the
        -- SERVER collation (SQL_Latin1_General_CP1_CI_AS here) while Rep_Data
        -- uses Latin1_General_CI_AS, and joining the two raises
        -- "Cannot resolve the collation conflict".
        CREATE TABLE #rdc_stamp (
            w   NVARCHAR(50) COLLATE DATABASE_DEFAULT NOT NULL,
            v   BIGINT                                NOT NULL,
            s   NVARCHAR(50) COLLATE DATABASE_DEFAULT NOT NULL,
            src NVARCHAR(20) COLLATE DATABASE_DEFAULT NOT NULL,
            cnt INT                                   NOT NULL
        );
    """))
    conn.execute(text(
        "INSERT INTO #rdc_stamp (w, v, s, src, cnt) VALUES (:w, :v, :s, :src, :cnt)"
    ), payload)
    conn.execute(text(
        "CREATE CLUSTERED INDEX IX_rdc_stamp ON #rdc_stamp (w, v, s)"
    ))
    conn.execute(text(f"""
        UPDATE A
           SET A.[SRC_RDC]       = T.src,
               A.[SRC_SPLIT_CNT] = T.cnt
        FROM [{alloc_table}] A
        INNER JOIN #rdc_stamp T
            ON  T.w = A.[WERKS]
            AND T.v = TRY_CAST(A.[VAR_ART] AS BIGINT)
            AND T.s = LTRIM(RTRIM(CAST(A.[SZ] AS NVARCHAR(50))))
    """))
    conn.execute(text("DROP TABLE #rdc_stamp"))
    logger.info(f"[rdc_split] stamped SRC_RDC on {len(payload):,} alloc line(s)")


def _apply_reductions(conn, alloc_table: str, listing_table: str,
                      reductions: List[Dict[str, Any]],
                      has_remarks: bool) -> None:
    """§B6.3.3 — a reduced line must leave the data internally consistent.

    1. SHIP_QTY / HOLD_QTY cut to the tagged quantity.
    2. ALLOC_REMARKS stamped so the reason is on the row itself.
    3. The option-grain rollup on ARS_LISTING_WORKING refreshed, so Part 8.5
       OPT_STATUS (which runs AFTER parking) judges the real shipped quantity
       rather than the pre-reduction one.
    4. The untagged remainder stays unallocated and shows in the residual report.

    Never a silent truncation: the caller also reports the count and quantity
    in the run log and the cockpit.
    """
    # Set-based, for the same reason as _stamp_alloc: a per-line UPDATE loop
    # is unusable at scale (V21).
    conn.execute(text("""
        IF OBJECT_ID('tempdb..#rdc_red') IS NOT NULL DROP TABLE #rdc_red;
        -- See the note on #rdc_stamp: COLLATE DATABASE_DEFAULT on every
        -- string column, or the joins below fail on a collation conflict.
        CREATE TABLE #rdc_red (
            w      NVARCHAR(50)  COLLATE DATABASE_DEFAULT NOT NULL,
            v      BIGINT                                 NOT NULL,
            s      NVARCHAR(50)  COLLATE DATABASE_DEFAULT NOT NULL,
            draw   NVARCHAR(4)   COLLATE DATABASE_DEFAULT NOT NULL,
            tagged FLOAT                                  NOT NULL,
            rk     NVARCHAR(200) COLLATE DATABASE_DEFAULT NOT NULL,
            mc     NVARCHAR(200) COLLATE DATABASE_DEFAULT NULL,
            ga     BIGINT                                 NULL,
            cl     NVARCHAR(200) COLLATE DATABASE_DEFAULT NULL
        );
    """))
    conn.execute(text(
        "INSERT INTO #rdc_red (w, v, s, draw, tagged, rk, mc, ga, cl) "
        "VALUES (:w, :v, :s, :draw, :tagged, :rk, :mc, :ga, :cl)"
    ), [{"w": r["WERKS"], "v": r["VAR_ART"], "s": r["SZ"],
         "draw": r["draw"], "tagged": r["tagged"],
         "rk": " " + r["remark"] + ";",
         "mc": r["MAJ_CAT"], "ga": r["GEN_ART_NUMBER"],
         "cl": r["CLR"] or ""} for r in reductions])
    conn.execute(text(
        "CREATE CLUSTERED INDEX IX_rdc_red ON #rdc_red (w, v, s, draw)"
    ))

    remark_set = (
        ", A.[ALLOC_REMARKS] = LEFT(ISNULL(A.[ALLOC_REMARKS],'') + T.rk, 4000)"
        if has_remarks else ""
    )
    # SHIP and HOLD are separate statements: ALLOC_QTY mirrors SHIP_QTY on the
    # alloc row and must stay in step with it, but a reduced HOLD must not
    # touch it.
    conn.execute(text(f"""
        UPDATE A
           SET A.[SHIP_QTY]  = T.tagged,
               A.[ALLOC_QTY] = T.tagged{remark_set}
        FROM [{alloc_table}] A
        INNER JOIN #rdc_red T
            ON  T.w = A.[WERKS]
            AND T.v = TRY_CAST(A.[VAR_ART] AS BIGINT)
            AND T.s = LTRIM(RTRIM(CAST(A.[SZ] AS NVARCHAR(50))))
        WHERE T.draw = 'SHIP'
    """))
    conn.execute(text(f"""
        UPDATE A
           SET A.[HOLD_QTY] = T.tagged{remark_set}
        FROM [{alloc_table}] A
        INNER JOIN #rdc_red T
            ON  T.w = A.[WERKS]
            AND T.v = TRY_CAST(A.[VAR_ART] AS BIGINT)
            AND T.s = LTRIM(RTRIM(CAST(A.[SZ] AS NVARCHAR(50))))
        WHERE T.draw = 'HOLD'
    """))

    # Refresh the option-grain rollup, but ONLY for the options actually
    # touched — Part 8.5 OPT_STATUS runs after parking and must judge the real
    # shipped quantity, not the pre-reduction one.
    cols = {c.upper() for c in (
        conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = :t"
        ), {"t": listing_table}).scalars().all() or []
    )}
    if not {"ALLOC_QTY", "HOLD_QTY"} <= cols:
        logger.warning(
            f"[rdc_split] {listing_table} lacks ALLOC_QTY/HOLD_QTY — rollup "
            f"not refreshed; Part 8.5 OPT_STATUS may judge pre-reduction totals"
        )
        conn.execute(text("DROP TABLE #rdc_red"))
        return
    conn.execute(text(f"""
        WITH touched AS (
            SELECT DISTINCT w, mc, ga, cl FROM #rdc_red
        ), rolled AS (
            SELECT [WERKS], [MAJ_CAT], [GEN_ART_NUMBER], ISNULL([CLR],'') AS CLR,
                   SUM(ISNULL(TRY_CAST([SHIP_QTY] AS FLOAT),0)) AS ship,
                   SUM(ISNULL(TRY_CAST([HOLD_QTY] AS FLOAT),0)) AS hold
            FROM [{alloc_table}]
            GROUP BY [WERKS], [MAJ_CAT], [GEN_ART_NUMBER], ISNULL([CLR],'')
        )
        UPDATE L
           SET L.[ALLOC_QTY] = ISNULL(R.ship, 0),
               L.[HOLD_QTY]  = ISNULL(R.hold, 0)
        FROM [{listing_table}] L
        INNER JOIN touched T
            ON  T.w  = L.[WERKS]
            AND T.mc = L.[MAJ_CAT]
            AND T.ga = L.[GEN_ART_NUMBER]
            AND T.cl = ISNULL(L.[CLR],'')
        LEFT JOIN rolled R
            ON  R.[WERKS]          = L.[WERKS]
            AND R.[MAJ_CAT]        = L.[MAJ_CAT]
            AND R.[GEN_ART_NUMBER] = L.[GEN_ART_NUMBER]
            AND R.CLR              = ISNULL(L.[CLR],'')
    """))
    n_opts = conn.execute(text(
        "SELECT COUNT(*) FROM (SELECT DISTINCT w, mc, ga, cl FROM #rdc_red) X"
    )).scalar() or 0
    conn.execute(text("DROP TABLE #rdc_red"))
    logger.info(
        f"[rdc_split] reductions applied to {len(reductions):,} line(s); "
        f"rollup refreshed for {n_opts:,} option(s)"
    )


def release_holds_for_mix_options(conn, session_id: str,
                                  listing_table: str) -> int:
    """Zero HOLD_QTY on the split rows of every not-covered (MIX) option.

    §B6.6. Part 8.55 releases warehouse holds on options that end the run
    without display cover, and it runs AFTER parking — so it already has to
    zero three copies (the option row, the alloc size rows, and this
    session's parked rows). The split rows are a fourth copy: without this
    cascade the RDC-wise picking requirement would keep reserving stock the
    run has already handed back to the pool.

    Since 2026-10-03 the split rows live in TWO places by the time this runs —
    the working table (this run) and ARS_ALLOC_RDC_SPLIT_PARKED (this
    session's snapshot, written moments earlier at Part 8.4). Both must be
    zeroed, exactly as the alloc rows are, or Approve would promote a parked
    hold the run has already released.

    Set-based and joined exactly like the other three updates, so it stays in
    step with them if the MIX criterion ever changes.
    """
    def _zero(table: str, sid_filter: str, params: dict) -> int:
        res = conn.execute(text(f"""
            UPDATE S
               SET S.[HOLD_QTY] = 0
            FROM [{table}] S
            INNER JOIN [{listing_table}] L
                ON  L.[WERKS]          = S.[WERKS]
                AND L.[MAJ_CAT]        = S.[MAJ_CAT]
                AND L.[GEN_ART_NUMBER] = S.[GEN_ART_NUMBER]
                AND ISNULL(L.[CLR],'') = ISNULL(S.[CLR],'')
            WHERE L.[OPT_STATUS] = 'MIX'
              AND ISNULL(L.[HOLD_RELEASED_QTY], 0) > 0
              AND ISNULL(S.[HOLD_QTY], 0) > 0
              {sid_filter}
        """), params)
        return int(res.rowcount or 0)

    n = _zero(SPLIT_TABLE, "", {})
    parked_tbl = f"{SPLIT_TABLE}_PARKED"
    if conn.execute(text(
        f"SELECT CASE WHEN OBJECT_ID('dbo.{parked_tbl}','U') "
        f"IS NULL THEN 0 ELSE 1 END"
    )).scalar():
        n += _zero(parked_tbl, "AND S.[SESSION_ID] = :sid", {"sid": session_id})
    if n:
        logger.info(
            f"[rdc_split] Part 8.55 cascade: cleared the hold on {n} split "
            f"row(s) for not-covered options"
        )
    return n
