"""
Business Rules registry — module-wise behavior switches with Active/Inactive.

One row per rule in ARS_BUSINESS_RULES. Code reads rules through
`rule_value()` / `rule_flag()`; **Inactive (or missing row) always falls
back to the caller-supplied default**, i.e. the hardcoded behavior — so
switching every rule off returns the system to plain code behavior and a
missing/broken table can never take a run down.

Seed metadata (name, description, consequence, bounds, wiring) self-heals
on rows still owned by 'seed'; the user's state (IS_ACTIVE, RULE_VALUE)
is NEVER overwritten by the seed.

Every change is appended to ARS_BUSINESS_RULES_LOG (who / when / old→new).
"""
import time
import threading
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine

TABLE = "ARS_BUSINESS_RULES"
LOG_TABLE = "ARS_BUSINESS_RULES_LOG"

# ── Seed — (module, key, name, description, consequence_when_off,
#            value_type, default_value, value_min, value_max, choices,
#            is_wired, default_active, algorithm)
# value_type: 'flag' (on/off only), 'number', 'choice'
# is_wired=0 → registered but no code reads it yet; UI shows "wiring
# pending" and disables the toggle so the page never lies.
# algorithm: plain-English step-by-step + a small worked example — rendered
# in the page's Algorithm tab.
SEED: List[tuple] = [
    ("Listing & Alloc", "LST_IROD_AMIX_FLOOR", "I_ROD floor for assorted colours",
     "Options with colour 'A' or 'A_MIX' represent multiple real colours on one rod, so they are stocked at deeper density. This rule lifts their I_ROD to at least the floor value. MAX semantics: a maintained higher value from the calc masters is always kept, never overridden down.",
     "A/A_MIX options keep their maintained I_ROD (default 1) — display depth halves from the next run.",
     "number", "2", 1, 4, None, 1, 1),
    ("Listing & Alloc", "LST_OPT_STATUS_STAMP", "OPT_STATUS stamping (Part 8.5)",
     "After allocation, stamp every option with its post-alloc status (NL / RL / WRL / TBL / L / MIX) plus TBL_LISTED_DATE on first allocation.",
     "OPT_STATUS stays NULL after runs; reports lose the post-alloc verdict. The Part 8.55 hold release also stops (it keys on OPT_STATUS = MIX).",
     "flag", None, None, None, None, 1, 1),
    ("Listing & Alloc", "LST_MIX_AGGREGATE", "MIX aggregation mode",
     "Collapse MIX-tagged rows to one line per store × MAJ_CAT during listing (Part 3.7), or keep every MIX row as its own line.",
     "Handled by the Run Setup mix_mode for now — this registry entry is not wired to code yet.",
     "choice", "st_maj", None, None, "st_maj,each", 0, 1),
    ("Listing & Alloc", "LST_RL_REQUIRES_MSA", "RL needs fresh MSA (not hold-only)",
     "An adequately-stocked option needs fresh MSA supply to classify RL; an open prior-run hold alone is not enough (2026-07-30 rule).",
     "CAUTION: off restores the pre-2026-07-30 classification where a hold alone could make RL. Not wired yet.",
     "flag", None, None, None, None, 0, 1),
    ("Listing & Alloc", "ALC_HOLD_RELEASE_855", "Post-run hold release (Part 8.55)",
     "An option that ends the run NOT covered (OPT_STATUS = MIX) releases its warehouse hold back to the free pool — on the working, alloc and parked copies — instead of committing it at Approve.",
     "Not-covered options keep their holds; the pieces are reserved at Approve for displays that are not viable (pre-2026-07-30 behavior).",
     "flag", None, None, None, None, 1, 1),
    ("Listing & Alloc", "ALC_TBL_HOLD_RETRY", "In-run hold retry (Option B)",
     "After the TBL waterfall, holds of not-covered options are released into the live pool and ONE extra TBL pass lets still-hungry options consume the freed pieces in the same run. All caps and gates re-apply.",
     "Freed pieces wait for the next run instead of shipping the same run (Part 8.55 still releases them post-run).",
     "flag", None, None, None, None, 1, 1),
    ("Listing & Alloc", "ALC_MJREQ_SKIP_FACTOR", "MJ_REQ skip factor",
     "In the sequential per-OPT walk, an option is skipped when the remaining MAJ_CAT requirement is below this factor × OPT_MBQ.",
     "Engine constant 0.5 is used. Not wired yet.",
     "number", "0.5", 0.1, 1.0, None, 0, 1),
    ("Listing & Alloc", "GRD_SEC_CAP_DEFAULT", "Default sec-cap % when grid blank",
     "Secondary-grid cap percentage used when a participating grid has no sec_cap_pct maintained.",
     "Hardcoded 130% is used. Not wired yet.",
     "number", "130", 100, 300, None, 0, 1),
    ("Listing & Alloc", "GRD_R09_FACTOR", "R09 headroom factor",
     "R09 skips upcoming options when the remaining MJ headroom is below this factor × ACS_D.",
     "Engine constant 0.5 is used. Not wired yet.",
     "number", "0.5", 0.1, 1.0, None, 0, 1),
    ("Pending Allocation", "DSP_BDC_SUPPRESS", "BDC suppression master",
     "Apply the hold-article and division-delete control tables when generating BDC, so controlled stock stays pending.",
     "Suppression tables are ignored — BDC generates for all pending rows. Not wired yet (tables are always applied today).",
     "flag", None, None, None, None, 0, 1),
    ("Listing & Alloc", "MSA_NET_ARS_PEND", "Net ARS pending in FNL_Q",
     "Free-to-allocate stock nets off pendings created by ARS runs themselves (FNL_Q = STK − PEND).",
     "CAUTION: off means free stock ignores ARS-created pendings — double-allocation risk. Not wired yet.",
     "flag", None, None, None, None, 0, 1),
    ("Listing & Alloc", "ALC_MULTI_PARKED", "Multi-parked runs (parking mode)",
     "Allow new listing runs to stack alongside parked sessions that are still awaiting approve/reject. MIGRATED 2026-07-31 from Settings → Application (Parking Mode toggle) — this rule is now the source of truth; the old toggle reads/writes it too.",
     "Single-parked mode: a new run is BLOCKED (409) while any parked session awaits review, so the parked snapshot can't be overwritten. Admins can still bypass with a logged warning.",
     "flag", None, None, None, None, 1, 1),
    ("Listing & Alloc", "LST_AUDIT_ALL_TO_WORKING", "Audit mode — all rows to working",
     "Listing Part 7 copies EVERY ARS_LISTING row into ARS_LISTING_WORKING, including ineligible ones, so reviewers can see every excluded option and why. Allocation still processes only ELIG_FLAG=1 rows — run output is byte-identical either way. MIGRATED 2026-07-31 from Settings → Application (shift_all_to_working).",
     "Working table carries only eligible (ELIG_FLAG=1) rows — leaner and faster, but excluded options are not visible for audit.",
     "flag", None, None, None, None, 1, 1),
    ("Reports", "UPC_TRACKING_ENABLED", "UPC Store Tracking module",
     "Master switch for the UPC Store Tracking module (/reports/upc-tracking): uploads, event history and live MBQ/stock joins.",
     "Module continues to work — this switch is not read by any code yet (wiring pending).",
     "flag", None, None, None, None, 0, 1),
    ("Settings", "SEC_SINGLE_LOGIN", "Single login per user",
     "When ACTIVE, each user may hold only ONE live session: a new login automatically signs out that user's other sessions (their tokens die on the next request). When INACTIVE (default), the same user may stay logged in from any number of devices/browsers at once.",
     "Default behavior — unlimited concurrent logins per user; sessions are still recorded and individually revocable from the sessions API.",
     "flag", None, None, None, None, 1, 0),
    ("Data Management", "DAT_DICT_SELF_HEAL", "Dictionary seed self-heal",
     "Refresh system-owned data-dictionary rows from the in-code SEED when their text drifts; user-edited rows are never touched.",
     "Dictionary text freezes at its current state; only brand-new columns are inserted. Not wired yet.",
     "flag", None, None, None, None, 0, 1),
]

# Plain-English algorithm + worked example per rule — rendered in the
# page's Algorithm tab. Kept separate from SEED so tuples stay readable.
ALGO: Dict[str, str] = {
    "LST_IROD_AMIX_FLOOR": (
        "I_ROD is filled in 3 layers, in order:\n"
        "  1. MAJ_CAT cascade — I_ROD from ARS_CALC_ST_MAJ_CAT\n"
        "  2. Article override — ARS_CALC_ST_ART value wins where maintained\n"
        "  3. THIS RULE — for CLR 'A'/'A_MIX' only:\n"
        "       if I_ROD (or blank/0) < floor F  →  set I_ROD = F\n"
        "       if I_ROD >= F                    →  KEEP maintained (MAX semantics)\n"
        "Result: I_ROD = MAX(maintained, F). The rule can only lift, never lower.\n"
        "Downstream: option gets rounds 1..I_ROD, per-size target/cap = I_ROD × SZ_MBQ,\n"
        "excess ceiling = MAX(I_ROD, excess multiplier).\n\n"
        "Example (F=2): maintained NULL→2, 1→2, 2→2, 3→3 (kept), 6→6 (kept);\n"
        "CLR 'RED' rows never touched. With SZ_MBQ=3: floored option targets 6 pcs\n"
        "over 2 rounds; a maintained-3 option targets 9 pcs over 3 rounds."
    ),
    "LST_OPT_STATUS_STAMP": (
        "After allocation (Part 8.5), per option compute post = STK_TTL + ALLOC_QTY\n"
        "and shelf-need g = Stock% × ACS_D (18 if blank). First match wins:\n"
        "  1. OPT_TYPE L   → L        2. OPT_TYPE MIX → MIX\n"
        "  3. RL,  alloc=0 → WRL      4. TBL, alloc=0 → UNQ   (unqualified this run)\n"
        "  5. post < g     → MIX      (shipped but display under threshold)\n"
        "  6. TBL → NL     7. TBC → RL     8. RL → RL\n"
        "  9. leftovers: alloc>0 → RL, else L.   No 'NA' — vocabulary is exhaustive.\n"
        "Also stamps TBL_LISTED_DATE on a TBL's first allocation.\n\n"
        "Example (ACS_D=20 → g=12): TBL shipped 15 → NL; TBL shipped 5 → MIX;\n"
        "TBL shipped 0 → UNQ; RL shipped 0 → WRL; TBC shipped to 14 → RL."
    ),
    "LST_MIX_AGGREGATE": (
        "Listing Part 3.7: collect all OPT_TYPE='MIX' rows per (store, MAJ_CAT),\n"
        "sum the numeric columns, fetch ACS_D/ALC_D fresh (not summed), write ONE\n"
        "row with GEN_ART=0, CLR='MIX'; delete the originals.\n"
        "'each' keeps every MIX row as its own line instead.\n\n"
        "Example: store 101 × M_TEES_HS has 9 MIX options → one MIX line whose\n"
        "STK_TTL is the 9-row sum. Last run: 836 MIX rows → ~90 lines."
    ),
    "LST_RL_REQUIRES_MSA": (
        "OPT_TYPE classification (Part 3.6) — the RL branch requires fresh supply:\n"
        "  RL = (stock >= g AND (MSA_FNL_Q > 0 OR hold > 0))\n"
        "       OR (sold-out AND hold-only)\n"
        "With this rule OFF the pre-2026-07-30 branch returns: adequate stock +\n"
        "an open hold alone (MSA=0) would classify RL.\n\n"
        "Example: stock 15 (g=12), MSA=0, hold=5 → ON: RL only via the hold-release\n"
        "path at alloc; OFF: classified RL purely because a hold exists."
    ),
    "ALC_HOLD_RELEASE_855": (
        "Part 8.55, after OPT_STATUS stamping: for every option with\n"
        "OPT_STATUS='MIX' AND HOLD_QTY>0:\n"
        "  1. remember qty in HOLD_RELEASED_QTY (option row)\n"
        "  2. zero HOLD_QTY on ARS_LISTING_WORKING, ARS_ALLOC_WORKING and this\n"
        "     session's ARS_ALLOC_PARKED (parked feeds hold-tracking at Approve —\n"
        "     zeroing only working copies would still commit the hold)\n"
        "  3. stamp ';HOLD_RELEASED_NOT_COVERED(n)' in ALLOC_REMARKS\n"
        "Freed pieces simply stay in the free pool for the next run.\n\n"
        "Example: option shipped 5 of need 12 (under cover) holding 3 at RDC →\n"
        "3 pcs released to pool. Covered NL options keep their holds untouched."
    ),
    "ALC_TBL_HOLD_RETRY": (
        "Engine, end of each MAJ_CAT's TBL waterfall (Option B):\n"
        "  1. find TBL options that shipped but stayed under cover\n"
        "     (STK_TTL + Σ SHIP < Stock% × ACS_D)\n"
        "  2. release their HOLD_QTY into the LIVE pool_dict\n"
        "  3. run ONE extra TBL band pass — still-hungry options (PARTIAL /\n"
        "     POOL_EMPTY sizes) consume the freed pieces the SAME run; all gates\n"
        "     re-apply; gate-vetoed (SKIPPED) options stay skipped\n"
        "  4. a released option cannot re-take its own pieces (POOL_CONSUMED\n"
        "     stays spent). Part 8.55 remains the post-run safety net.\n\n"
        "Example: store S2 under cover holding 4 pcs of size M → released; store\n"
        "S3 (same article, POOL_EMPTY on M) receives them in the retry pass.\n"
        "Measured potential on live data: 133/133 freed pcs had same-run takers."
    ),
    "ALC_MJREQ_SKIP_FACTOR": (
        "Sequential per-OPT walk within (store, MAJ_CAT), priority RL→TBC→TBL:\n"
        "req_rem starts at MJ_REQ and each shipping OPT consumes its ship qty.\n"
        "Before each OPT: if req_rem < factor × OPT_MBQ → SKIP the OPT\n"
        "(remaining budget too small to make a meaningful display).\n\n"
        "Example (factor 0.5): req_rem 4, next OPT_MBQ 10 → 4 < 5 → skipped;\n"
        "req_rem 6 → 6 >= 5 → OPT is allowed to ship."
    ),
    "GRD_SEC_CAP_DEFAULT": (
        "Sec-cap pre-gate per participating grid:\n"
        "  budget = MAX(0, grid MBQ_ORIG × cap% − grid STK_TTL); breach = block.\n"
        "cap% comes from ARS_GRID_BUILDER.sec_cap_pct; when NULL/blank, THIS\n"
        "value is used as the default.\n\n"
        "Example (default 130): grid MBQ_ORIG 100, stock 110 → budget =\n"
        "130 − 110 = 20 pcs headroom; at 140 stock the grid blocks new ships."
    ),
    "GRD_R09_FACTOR": (
        "R09 headroom check at each OPT_TYPE boundary (after RL, before TBC…):\n"
        "per (store, MAJ_CAT), headroom = cap% × MJ_MBQ − MJ_STK_TTL − Σ allocated.\n"
        "If headroom < factor × ACS_D → all PENDING rows of the UPCOMING types\n"
        "are skipped (not enough room left for even a fraction of one option).\n\n"
        "Example (factor 0.5, ACS_D 18): headroom 7 < 9 → upcoming TBC/TBL\n"
        "options at that store×MAJ_CAT are skipped this run."
    ),
    "DSP_BDC_SUPPRESS": (
        "BDC generation (/pend-alc/bdc-generate) applies NOT EXISTS against three\n"
        "control tables: ARS_HOLD_ARTICLE_BDC, ARS_DIVISION_DELETE_BDC and\n"
        "ARS_DIVISION_DELETE_ON_MAJ_CAT_BDC — matching pending rows are excluded\n"
        "from the BDC so the stock stays pending (controlled).\n\n"
        "Example: article 12345 in ARS_HOLD_ARTICLE_BDC → its pending rows are\n"
        "left out of the generated BDC; the Gap report shows them as held."
    ),
    "MSA_NET_ARS_PEND": (
        "MSA free stock per (article, RDC):\n"
        "  FNL_Q = MAX(STK_QTY − PEND_QTY, 0)\n"
        "where PEND_QTY sums the open (undelivered) pendings, including those\n"
        "created by earlier ARS runs.\n\n"
        "Example: warehouse 100 pcs, open pendings 30 → FNL_Q = 70. Without\n"
        "netting the run would see 100 and could promise the same 30 twice."
    ),
    "DAT_DICT_SELF_HEAL": (
        "On every dictionary load (GET /data-dictionary → _ensure_table):\n"
        "  1. INSERT any SEED column not present\n"
        "  2. for rows still system-owned (updated_by in seed/code-sweep) whose\n"
        "     text differs from the in-code SEED → UPDATE to the SEED text\n"
        "  3. user-edited rows (updated_by = a username) are NEVER touched.\n\n"
        "Example: OPT_TYPE description changed in code → next page load refreshes\n"
        "the row; a column you edited by hand keeps your wording forever."
    ),
    "ALC_MULTI_PARKED": (
        "Guard at the start of /listing/generate:\n"
        "  if THIS RULE is OFF and any parked session is awaiting approve/reject\n"
        "  → block the run (HTTP 409) so the parked snapshot can't be overwritten\n"
        "    by a new run recreating ARS_ALLOC_WORKING / ARS_LISTING_WORKING.\n"
        "  ADMIN / SUPER_ADMIN bypass the block (logged as a warning).\n"
        "  ON → runs stack alongside pending parked sessions (multi-parked).\n\n"
        "Example: session A parked awaiting review; user starts run B —\n"
        "OFF: 409 'approve/reject first'; ON: run B proceeds and parks alongside."
    ),
    "LST_AUDIT_ALL_TO_WORKING": (
        "Listing Part 7 builds ARS_LISTING_WORKING from ARS_LISTING:\n"
        "  rule OFF → SELECT ... WHERE ELIG_FLAG = 1   (eligible rows only)\n"
        "  rule ON  → SELECT ... (no WHERE — every row, incl. ineligible)\n"
        "Allocation always processes only ELIG_FLAG=1 rows either way, so run\n"
        "output is byte-identical — ON only adds audit visibility (query\n"
        "ELIG_REASON to see why a row was excluded). Larger working/parked tables.\n\n"
        "Example: 2.1M listing rows, 18k eligible → OFF: working has 18k rows;\n"
        "ON: working has all rows with ELIG_REASON = NOT_LISTED / NO_STOCK / …"
    ),
    "UPC_TRACKING_ENABLED": (
        "Intended wiring (pending): the /upc-store-track API checks this rule\n"
        "on every call — OFF → endpoints return 'module disabled by admin' and\n"
        "the page shows a disabled banner. Currently NO code reads the rule, so\n"
        "the toggle has no effect yet.\n\n"
        "Module recap: upload ST_CD + proposed/share dates, identity from the\n"
        "store master, live MBQ/stock/SLOC/FR from the latest TREND_ST row,\n"
        "event-based date/remark history.\n\n"
        "Example (once wired): admin turns the rule OFF at 10:00 — a user opening\n"
        "/reports/upc-tracking sees 'Module disabled by admin' and an upload\n"
        "attempt is rejected; existing data stays untouched. Turning it back ON\n"
        "restores the module instantly (max 30s cache delay). Today, because the\n"
        "wiring is pending, both states behave identically — module always works."
    ),
    "SEC_SINGLE_LOGIN": (
        "Every login creates a session row (rbac_user_sessions) keyed by a\n"
        "unique id (jti) carried inside the user's tokens. On every API call\n"
        "the token is honoured only while its session row is active.\n\n"
        "This rule decides what happens at LOGIN time:\n"
        "  ACTIVE   → single-login: the new session deactivates the user's\n"
        "             other sessions; those devices get 'Session terminated'\n"
        "             on their next request and must log in again.\n"
        "  INACTIVE → multi-login (default): all sessions stay live.\n\n"
        "Example: user 'amits' is logged in on PC-1. SEC_SINGLE_LOGIN is ON.\n"
        "amits logs in on PC-2 → PC-1's session is revoked instantly; the next\n"
        "click on PC-1 shows 'signed in on another device'.\n\n"
        "Regardless of this rule, a superadmin can view and revoke any single\n"
        "session via GET /auth/sessions and DELETE /auth/sessions/{id}.\n"
        "Older tokens issued before 2026-07-31 carry no session id and stay\n"
        "valid until they expire (max 8h) — enforcement covers all logins\n"
        "made after the upgrade."
    ),
}

_cache: Dict[str, Dict[str, Any]] = {}
_cache_ts: float = 0.0
_CACHE_TTL = 30.0
_lock = threading.Lock()
_tables_ready = False


def ensure_tables(conn) -> None:
    """Create + seed the rules tables (idempotent, self-healing metadata)."""
    conn.execute(text(f"""
        IF OBJECT_ID('{TABLE}') IS NULL
        CREATE TABLE [{TABLE}] (
            id            INT IDENTITY PRIMARY KEY,
            module        NVARCHAR(50)   NOT NULL,
            rule_key      NVARCHAR(64)   NOT NULL UNIQUE,
            rule_name     NVARCHAR(200)  NOT NULL,
            description   NVARCHAR(MAX)  NULL,
            consequence   NVARCHAR(MAX)  NULL,
            value_type    NVARCHAR(20)   NOT NULL DEFAULT 'flag',
            rule_value    NVARCHAR(100)  NULL,
            default_value NVARCHAR(100)  NULL,
            value_min     FLOAT          NULL,
            value_max     FLOAT          NULL,
            choices       NVARCHAR(400)  NULL,
            is_wired      BIT            NOT NULL DEFAULT 0,
            is_active     BIT            NOT NULL DEFAULT 1,
            updated_by    NVARCHAR(128)  NULL,
            updated_at    DATETIME2      NOT NULL DEFAULT SYSDATETIME()
        )
    """))
    conn.execute(text(f"""
        IF OBJECT_ID('{LOG_TABLE}') IS NULL
        CREATE TABLE [{LOG_TABLE}] (
            id         INT IDENTITY PRIMARY KEY,
            rule_key   NVARCHAR(64)  NOT NULL,
            old_value  NVARCHAR(100) NULL,
            new_value  NVARCHAR(100) NULL,
            old_active BIT           NULL,
            new_active BIT           NULL,
            note       NVARCHAR(400) NULL,
            changed_by NVARCHAR(128) NULL,
            changed_at DATETIME2     NOT NULL DEFAULT SYSDATETIME(),
            INDEX IX_BRL_KEY (rule_key, changed_at DESC)
        )
    """))
    # Column migration — algorithm tab text (added 2026-07-31).
    conn.execute(text(f"""
        IF COL_LENGTH('{TABLE}', 'algorithm') IS NULL
            ALTER TABLE [{TABLE}] ADD [algorithm] NVARCHAR(MAX) NULL
    """))
    existing = {
        (r[0] or "").strip().upper()
        for r in conn.execute(text(f"SELECT rule_key FROM [{TABLE}]")).fetchall()
    }
    added = healed = 0
    for (module, key, name, desc, conseq, vtype, dval,
         vmin, vmax, choices, wired, active) in SEED:
        algo = ALGO.get(key)
        if key.upper() in existing:
            # [algorithm] is pure documentation (never user state) — heal it
            # on EVERY row regardless of ownership, so doc updates always land.
            conn.execute(text(f"""
                UPDATE [{TABLE}] SET [algorithm] = :alg
                WHERE rule_key = :k
                  AND ISNULL(CAST([algorithm] AS NVARCHAR(MAX)),'') <> ISNULL(:alg,'')
            """), {"k": key, "alg": algo})
            # Self-heal remaining METADATA on seed-owned rows only. IS_ACTIVE
            # and RULE_VALUE are user state — never touched here.
            res = conn.execute(text(f"""
                UPDATE [{TABLE}]
                SET module = :m, rule_name = :n, description = :d,
                    consequence = :c, value_type = :t, default_value = :dv,
                    value_min = :mn, value_max = :mx, choices = :ch,
                    is_wired = :w
                WHERE rule_key = :k
                  AND ISNULL(updated_by, 'seed') = 'seed'
                  AND (ISNULL(rule_name,'') <> :n OR ISNULL(description,'') <> ISNULL(:d,'')
                    OR ISNULL(consequence,'') <> ISNULL(:c,'') OR is_wired <> :w
                    OR ISNULL(default_value,'') <> ISNULL(:dv,'')
                    OR ISNULL(module,'') <> :m)
            """), {"m": module, "n": name, "d": desc, "c": conseq, "t": vtype,
                   "dv": dval, "mn": vmin, "mx": vmax, "ch": choices,
                   "w": wired, "k": key})
            healed += res.rowcount or 0
            continue
        conn.execute(text(f"""
            INSERT INTO [{TABLE}] (module, rule_key, rule_name, description,
                consequence, value_type, rule_value, default_value,
                value_min, value_max, choices, is_wired, is_active, updated_by,
                [algorithm])
            VALUES (:m, :k, :n, :d, :c, :t, :v, :dv, :mn, :mx, :ch, :w, :a, 'seed',
                    :alg)
        """), {"m": module, "k": key, "n": name, "d": desc, "c": conseq,
               "t": vtype, "v": dval, "dv": dval, "mn": vmin, "mx": vmax,
               "ch": choices, "w": wired, "a": active, "alg": algo})
        added += 1
    if added or healed:
        logger.info(f"[business-rules] seeded {added} new, healed {healed} entries")
    conn.commit()


def _load_all(force: bool = False) -> Dict[str, Dict[str, Any]]:
    """Load all rules into the module-level cache (TTL 30s)."""
    global _cache, _cache_ts, _tables_ready
    now = time.time()
    if not force and _cache and (now - _cache_ts) < _CACHE_TTL:
        return _cache
    with _lock:
        if not force and _cache and (time.time() - _cache_ts) < _CACHE_TTL:
            return _cache
        try:
            de = get_data_engine()
            with de.connect() as conn:
                if not _tables_ready:
                    ensure_tables(conn)
                    _tables_ready = True
                rows = conn.execute(text(f"""
                    SELECT rule_key, module, rule_name, description, consequence,
                           value_type, rule_value, default_value, value_min,
                           value_max, choices, is_wired, is_active,
                           updated_by, CONVERT(NVARCHAR(19), updated_at, 120),
                           [algorithm]
                    FROM [{TABLE}]
                """)).fetchall()
            _cache = {
                str(r[0]).upper(): {
                    "rule_key": r[0], "module": r[1], "rule_name": r[2],
                    "description": r[3], "consequence": r[4],
                    "value_type": r[5], "rule_value": r[6],
                    "default_value": r[7], "value_min": r[8],
                    "value_max": r[9], "choices": r[10],
                    "is_wired": bool(r[11]), "is_active": bool(r[12]),
                    "updated_by": r[13], "updated_at": r[14],
                    "algorithm": r[15],
                } for r in rows
            }
            _cache_ts = time.time()
        except Exception as e:
            # Never let the rules table take a run down — stale cache or
            # empty dict simply means every caller gets its default.
            logger.warning(f"[business-rules] load failed (defaults apply): {e}")
    return _cache


def invalidate_cache() -> None:
    global _cache_ts
    _cache_ts = 0.0


def rule_flag(key: str, default: bool = True) -> bool:
    """Flag-type rule: returns IS_ACTIVE; missing row → default."""
    r = _load_all().get(str(key).upper())
    if r is None:
        return default
    return bool(r["is_active"])


def rule_value(key: str, default: Any = None) -> Any:
    """Value-type rule: returns RULE_VALUE (bounds-clamped float when
    numeric) if the rule is ACTIVE; inactive/missing/blank → default."""
    r = _load_all().get(str(key).upper())
    if r is None or not r["is_active"]:
        return default
    v = r.get("rule_value")
    if v is None or str(v).strip() == "":
        return default
    if r.get("value_type") == "number":
        try:
            f = float(v)
            if r.get("value_min") is not None:
                f = max(f, float(r["value_min"]))
            if r.get("value_max") is not None:
                f = min(f, float(r["value_max"]))
            return f
        except Exception:
            return default
    return v


def list_rules() -> List[Dict[str, Any]]:
    rules = list(_load_all(force=True).values())
    rules.sort(key=lambda r: (r["module"], r["rule_key"]))
    return rules


def update_rule(key: str, *, value: Optional[str] = None,
                is_active: Optional[bool] = None, user: str = "system") -> Dict:
    de = get_data_engine()
    with de.connect() as conn:
        ensure_tables(conn)
        row = conn.execute(text(
            f"SELECT rule_value, is_active, value_type, value_min, value_max, choices "
            f"FROM [{TABLE}] WHERE rule_key = :k"), {"k": key}).fetchone()
        if not row:
            raise ValueError(f"unknown rule '{key}'")
        old_value, old_active = row[0], bool(row[1])
        new_value = old_value if value is None else str(value).strip()
        new_active = old_active if is_active is None else bool(is_active)
        if row[2] == "number" and value is not None:
            f = float(new_value)  # raises on junk → 400 upstream
            if row[3] is not None and f < float(row[3]):
                raise ValueError(f"value below minimum {row[3]}")
            if row[4] is not None and f > float(row[4]):
                raise ValueError(f"value above maximum {row[4]}")
        if row[2] == "choice" and value is not None:
            allowed = [c.strip() for c in (row[5] or "").split(",") if c.strip()]
            if allowed and new_value not in allowed:
                raise ValueError(f"value must be one of {allowed}")
        conn.execute(text(f"""
            UPDATE [{TABLE}]
            SET rule_value = :v, is_active = :a, updated_by = :u,
                updated_at = SYSDATETIME()
            WHERE rule_key = :k
        """), {"v": new_value, "a": new_active, "u": user, "k": key})
        conn.execute(text(f"""
            INSERT INTO [{LOG_TABLE}] (rule_key, old_value, new_value,
                old_active, new_active, changed_by)
            VALUES (:k, :ov, :nv, :oa, :na, :u)
        """), {"k": key, "ov": old_value, "nv": new_value,
               "oa": old_active, "na": new_active, "u": user})
        conn.commit()
    invalidate_cache()
    return {"rule_key": key, "rule_value": new_value, "is_active": new_active}


def rule_history(key: str, limit: int = 50) -> List[Dict]:
    de = get_data_engine()
    with de.connect() as conn:
        ensure_tables(conn)
        rows = conn.execute(text(f"""
            SELECT TOP (:lim) old_value, new_value, old_active, new_active,
                   note, changed_by, CONVERT(NVARCHAR(19), changed_at, 120)
            FROM [{LOG_TABLE}] WHERE rule_key = :k ORDER BY changed_at DESC
        """), {"k": key, "lim": limit}).fetchall()
    return [{"old_value": r[0], "new_value": r[1],
             "old_active": bool(r[2]) if r[2] is not None else None,
             "new_active": bool(r[3]) if r[3] is not None else None,
             "note": r[4], "changed_by": r[5], "changed_at": r[6]} for r in rows]
