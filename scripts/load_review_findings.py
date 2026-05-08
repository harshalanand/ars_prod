"""
One-time loader: imports the May-2026 ARS code review findings into PT_PROJECT
as a 3-level hierarchy (root project → 5 module sub-projects → ~72 task rows).

Safe to re-run: deletes any prior rows whose PROJECT_CODE starts with 'PT-2026-CR-'
before re-inserting. Activity log entries are added for each CREATED row.

Run:
    e:/ARS/backend/venv/Scripts/python.exe e:/ARS/scripts/load_review_findings.py
"""
import pyodbc
from datetime import date, datetime

CONN_STR = (
    "DRIVER={ODBC Driver 18 for SQL Server};SERVER=hopc560;DATABASE=Rep_data;"
    "UID=sa;PWD=vrl@55555;TrustServerCertificate=yes;Encrypt=no"
)
ACTOR = "santosh"
TODAY = date(2026, 5, 7)
DUE = {"NOW": date(2026, 5, 14), "SOON": date(2026, 6, 4), "LATER": date(2026, 7, 6)}
PRIORITY = {"NOW": "CRITICAL", "SOON": "HIGH", "LATER": "MEDIUM"}
PHASE    = {"NOW": "PHASE_1",  "SOON": "PHASE_2", "LATER": "PHASE_3"}
CODE_PREFIX = "PT-2026-CR-"

# ── Module sub-projects (level 2) ───────────────────────────────────────────
MODULES = [
    ("MSA",   "MSA Stock Calculation",
     "Bugs/perf/safety findings in msa_service.py and msa_stock.py — affects FNL_Q correctness, the source of truth for allocation."),
    ("GRID",  "Grid Builder",
     "Bugs/perf/safety findings in grid_builder.py and grid_calculations.py — affects MAJ_CAT grid integrity and overlay correctness."),
    ("LIST",  "Listing",
     "Bugs/perf/concurrency findings in listing.py, listing_allocator.py, listing_sessions.py, listing_job_manager.py."),
    ("PEND",  "Pending Allocation",
     "Bugs/concurrency/perf findings in pend_alc.py and pend_alc_service.py — recently shipped (cf048fa), highest density of issues."),
    ("XCUT",  "Cross-cutting Fixes",
     "Patterns repeated across modules — fix once, benefit everywhere (singletons, DDL guards, parameter binding, dtype normalization, OUTPUT INSERTED.ID, generic exception handler)."),
]

# ── Findings (level 3 tasks) ────────────────────────────────────────────────
# Format: (module_key, finding_id, severity, category, name, description)
FINDINGS = [
    # ===== MSA =====
    ("MSA","1.1","NOW","BUG",
     "msa_service.py:509 — SEG.nunique() KeyError in else-branch",
     "Code logs msa['SEG'].nunique() inside the else branch of `if 'SEG' in msa.columns:`, i.e. exactly when SEG is missing.\n\n"
     "EXAMPLE: A MAJ_CAT filter excludes APP/GM rows → SEG column never created → log line throws KeyError: 'SEG' → MSA job dies mid-pipeline.\n\n"
     "FIX: Replace with a static log message, or move the .nunique() call into the `if` branch."),
    ("MSA","1.2","NOW","BUG",
     "msa_service.py:563-598 — Pend/Hold merge dtype mismatch silently zeros deduction",
     "MSA view returns ST_CD as int64; ARS_PEND_ALC.RDC and the holds query return str. Pandas merges silently produce all-NaN matches when types differ.\n\n"
     "EXAMPLE: Store 1101 has 50 units of pending. After merge, ARS_PEND for 1101 is NaN → fillna(0) → 0 deducted → FNL_Q = STK − 0 − HOLD → MSA reports stock as fully available → next allocation over-orders 50 units.\n\n"
     "FIX: Force both join keys with .astype(str) (or both int) immediately before merge."),
    ("MSA","1.3","NOW","BUG",
     "msa_service.py:524 — DATE in pivot_keys causes row explosion",
     "pivot_keys = [c for c in msa.columns if c not in ['SLOC','STK_Q']] includes every other column, including raw DATE (which may be a timestamp, not a date).\n\n"
     "EXAMPLE: A SLOC has two stock snapshots same day at 09:00 and 23:00. Pivot keys treat them as separate rows. (MAJ_CAT, GEN_ART, ..., '2026-05-06 09:00:00') and (..., '2026-05-06 23:00:00') produce two rows where there should be one. SEG/CLR aggregate is wrong.\n\n"
     "FIX: msa['DATE'] = pd.to_datetime(msa['DATE']).dt.normalize() (or drop DATE from pivot_keys entirely)."),
    ("MSA","1.4","NOW","BUG",
     "msa_stock.py:416, :428 — LIMIT n is invalid on SQL Server",
     "SQL Server uses TOP n, not MySQL/PostgreSQL LIMIT n.\n\n"
     "EXAMPLE: Hit /debug endpoint → server returns 500 with 'Incorrect syntax near LIMIT'. Endpoint has been broken since it was written.\n\n"
     "FIX: SELECT TOP 10 * FROM ... instead of SELECT * FROM ... LIMIT 10."),
    ("MSA","1.5","NOW","BUG",
     "msa_stock.py:747 — /pivot loads full view with empty filters",
     "service.apply_filters('', {}) with no filter pulls the entire vw_master_stock view into pandas.\n\n"
     "EXAMPLE: User opens MSA pivot page with the default no-filter state. View is ~20M rows. API request times out at 60s, server memory spikes to 4GB.\n\n"
     "FIX: Require at least one filter (e.g. DATE) at the schema level; reject empty filter sets with HTTP 400."),
    ("MSA","1.6","NOW","BUG",
     "msa_service.py:255-269, :250-253 — Fake DH24/DH25 fallback masks DB outage",
     "get_distinct_values and _get_test_distinct_values catch all exceptions and return hard-coded fake values.\n\n"
     "EXAMPLE: SQL Server credentials rotate without updating .env. Frontend dropdown shows 'DH24, DH25' as if normal. User runs MSA on DH24 → 500. Operations team has no signal that the DB is unreachable for 3 hours.\n\n"
     "FIX: Remove the fake fallback or gate behind `if settings.DEBUG:`. Let the exception propagate as HTTP 500."),
    ("MSA","1.7","SOON","BUG",
     "msa_service.py:617, :809-851 — Dead MASTER_ALC_PEND deduction path",
     "Docstring says FNL_Q deducts both ARS_PEND and legacy MASTER_ALC_PEND. Only ARS_PEND is wired in. _get_pending_allocation references self.pending_table which is never assigned in __init__ → calling it raises AttributeError.\n\n"
     "EXAMPLE: Migration plan was probably 'deduct both during transition, drop legacy later.' Right now we deduct only the new table. If any legacy pending rows still exist, they're double-counted in stock.\n\n"
     "FIX: Decide intent. Either delete the dead method + update docstring, or wire it in (and set self.pending_table = 'MASTER_ALC_PEND' in __init__)."),
    ("MSA","1.8","SOON","BUG",
     "msa_service.py:631-634 — Color threshold strictly > excludes equal values",
     "transform('sum') > threshold excludes rows whose color total exactly equals the threshold.\n\n"
     "EXAMPLE: UI says 'Minimum 25%'. A color contributing exactly 25% gets dropped. UI lies (says ≥, code says >).\n\n"
     "FIX: Change to >=."),
    ("MSA","1.9","SOON","BUG",
     "msa_service.py:497-501 — String '0' replaced with NaN destroys legitimate values",
     "replace(['', ' ', '0', ...], np.nan) applies globally to all columns.\n\n"
     "EXAMPLE: M_VND_CD stored as text contains '0' for unassigned vendor. Replace makes it NaN → fillna('NA') → vendor lookups join on 'NA' instead of '0' → wrong vendor mapping.\n\n"
     "FIX: Per-column replace dict; restrict the '0' replacement to columns where 0 is genuinely a missing-value sentinel."),
    ("MSA","1.10","SOON","ENHANCEMENT",
     "msa_service.py:526-535 — pivot_table is the hot path",
     "pivot_table over a wide pivot_keys list is much slower than groupby + unstack for the same shape.\n\n"
     "EXAMPLE: 320 SLOCs × 5M rows: pivot_table ~90s; df.groupby(pivot_keys + ['SLOC'])['STK_Q'].sum().unstack('SLOC', fill_value=0) ~8s. ~10x for the largest step.\n\n"
     "FIX: Switch to groupby+unstack."),
    ("MSA","1.11","SOON","BUG",
     "msa_stock.py:282-374 — save_filter_config race produces duplicate rows",
     "SELECT-then-INSERT/UPDATE without unique constraint or MERGE.\n\n"
     "EXAMPLE: User double-clicks Save. Both calls SELECT → both find no row → both INSERT → two configs with same name.\n\n"
     "FIX: MERGE on config_name, or add unique index + use INSERT ... WHERE NOT EXISTS."),
    ("MSA","1.12","SOON","BUG",
     "msa_stock.py:111-119 — /columns endpoint leaks str(e) and hides errors",
     "Returns 200 with {'data':[], 'message': str(e)}. Schema/path leaks to client; monitoring can't tell success from failure.\n\n"
     "FIX: logger.exception(e) + raise HTTPException(500, 'internal error') with optional opaque error id."),
    ("MSA","1.13","LATER","ENHANCEMENT",
     "msa_service.py:354-364 — Column-discovery roundtrip per filter request",
     "Each apply_filters issues SELECT TOP 1 * to learn columns. ~50ms × every request.\n\n"
     "FIX: Cache column list as class attribute on MSAService (or fetch once at app startup)."),
    ("MSA","1.14","LATER","ENHANCEMENT",
     "msa_service.py:90-96 — INFORMATION_SCHEMA per holds query",
     "Cache rdc_col in __init__."),
    ("MSA","1.15","LATER","BUG",
     "msa_service.py:148 — str(d.date()) assumes Timestamp",
     "If pyodbc returns datetime.date (driver-dependent), .date() raises AttributeError.\n\n"
     "FIX: pd.to_datetime(df['date_val']).dt.date.astype(str).tolist()."),

    # ===== GRID BUILDER =====
    ("GRID","2.1","NOW","BUG",
     "grid_builder.py:1145 — Dedupe ROW_NUMBER deletes real rows when MP returns 'NA'",
     "ROW_NUMBER() OVER (PARTITION BY pk_cols ORDER BY [STK_TTL] DESC) keeps only rn=1. When PK includes a lookup column that resolves to 'NA' for unmatched articles, distinct articles collapse to the same key.\n\n"
     "EXAMPLE: Grid PK = (MAJ_CAT, M_VND_CD). Articles 1001 and 1002 both belong to MAJ_CAT='APP', both have unmapped vendor → M_VND_CD='NA'. Both get key ('APP','NA'). Only the one with higher STK_TTL survives. The other is silently dropped from the grid.\n\n"
     "FIX: Either include MATNR/GEN_ART_NUMBER in the PK for article-level grids, or skip dedupe entirely when articles are present."),
    ("GRID","2.2","NOW","BUG",
     "grid_calculations.py:318, :766 — Overlay treats '0' as no-override",
     "LTRIM(RTRIM(CAST(S.[col] AS NVARCHAR(MAX)))) NOT IN ('', '0') filters out '0' values as if they were null.\n\n"
     "EXAMPLE: CO has LISTING=1 for store X, MAJ_CAT Y. User explicitly sets store override LISTING=0 to delist that store from MAJ_CAT Y. Overlay treats the 0 as 'no override' → CO's 1 wins → store stays listed → store keeps receiving allocations they shouldn't.\n\n"
     "FIX: Filter only on IS NOT NULL; treat 0 as a valid override value."),
    ("GRID","2.3","NOW","BUG",
     "grid_calculations.py:392-432 — INT_DAYS / PRD_DAYS priority broken",
     "Priority comment says ST_MAJ > CO_MAJ > ST_MASTER, but code reuses ST_MASTER.INT_DAYS/PRD_DAYS for both override branches → CO's override values never applied.\n\n"
     "EXAMPLE: ST_MASTER says INT_DAYS=30. CO_MAJ override says INT_DAYS=15 (faster cycle for that category). Code still uses 30 → orders too rarely → understocking.\n\n"
     "FIX: Read INT_DAYS/PRD_DAYS from the highest-priority source actually present, not always ST_MASTER."),
    ("GRID","2.4","NOW","BUG",
     "grid_builder.py:803 — kpi_filter f-string injection / semantic break",
     "kpi_filter.upper().replace(chr(39), '') interpolated as f-string. Strip-the-quote isn't bind-equivalent.\n\n"
     "EXAMPLE: kpi_filter = 'APPAREL OR 1=1 --' slips through (no quotes to strip) → query semantics altered. Same value flows into the staged-pivot CTE at line 1026.\n\n"
     "FIX: Bind as parameter (SQLAlchemy :kpi) or whitelist against known KPI values."),
    ("GRID","2.5","SOON","BUG",
     "grid_builder.py:561 — Lookup endswith auto-fix matches unrelated columns",
     "Auto-resolve column name uses endswith symmetrically. Any column ending in a 3+ letter suffix can match unrelated columns.\n\n"
     "EXAMPLE: Lookup expects MAJ_CAT. Source has CAT (a different field) and MAJ_CAT. Both end in CAT → wrong join, wrong values.\n\n"
     "FIX: Restrict to exact match or strict prefix patterns; require length difference < 3."),
    ("GRID","2.6","SOON","BUG",
     "grid_calculations.py:530 — MIN(MAJ_CAT) is non-deterministic",
     "When an article maps to multiple MAJ_CATs, MIN([MAJ_CAT]) picks alphabetically.\n\n"
     "EXAMPLE: Article 9999 mapped to both APPAREL and GENERAL. MIN = APPAREL. Tomorrow user re-categorises → mapped to BAGS and GENERAL → MIN = BAGS. Grids change without warning.\n\n"
     "FIX: Define a business rule (latest mapping date? primary category flag?) and use that."),
    ("GRID","2.7","SOON","BUG",
     "grid_builder.py:1235, :1297 — Two-step UPDATE+INSERT without transaction",
     "'Set old grid use_for_opt_sale=0, then insert new grid' — two conn.execute calls without BEGIN TRAN.\n\n"
     "EXAMPLE: UPDATE succeeds (zero grids flagged); process killed (deploy, OOM); INSERT never runs → no grid is use_for_opt_sale=1 → next allocation finds no grid and either errors or silently skips.\n\n"
     "FIX: Wrap in `with conn.begin():` or a single MERGE."),
    ("GRID","2.8","SOON","ENHANCEMENT",
     "grid_calculations.py:307-322, :756-770 — Per-column UPDATE bombardment",
     "One UPDATE per ST column (10-20 columns), each scanning the table.\n\n"
     "EXAMPLE: Grid table has 5M rows. 15 ST columns × 5M-row scan = 75M row-touches when one UPDATE with 15 SET clauses would do 5M.\n\n"
     "FIX: Coalesce UPDATE ... SET col1=..., col2=..., ... FROM JOIN."),
    ("GRID","2.9","SOON","BUG",
     "grid_builder.py:519, :614 / grid_calculations.py:600, :1104, :1109 — ALTER TABLE bare-except",
     "try: ALTER TABLE ADD COLUMN; except: pass — masks every failure mode.\n\n"
     "EXAMPLE: ADD COLUMN times out due to lock from a long-running SELECT. Exception swallowed. Subsequent UPDATE fails with 'Invalid column name' minutes later, far from the cause. Or rename column fails because target name already exists from a prior partial run → next step uses the stale column.\n\n"
     "FIX: Replace with `IF COL_LENGTH('table','col') IS NULL ALTER TABLE ...` (idempotent SQL guard); or check exception class and re-raise unless it's 'column exists'."),
    ("GRID","2.10","LATER","ENHANCEMENT",
     "grid_builder.py:1414-1417 — 30 round-trips for grid manifest UPDATE",
     "30 grids × 30 round-trips. ~150ms wasted at typical SQL latency.\n\n"
     "FIX: UPDATE ... FROM (VALUES ...) v(id, status) WHERE ..."),
    ("GRID","2.11","LATER","ENHANCEMENT",
     "grid_builder.py:478-674 — INFORMATION_SCHEMA per lookup per grid",
     "_apply_post_lookups runs 4-5 INFORMATION_SCHEMA queries per lookup per grid.\n\n"
     "FIX: Cache the lookup table's column list once per call."),
    ("GRID","2.12","LATER","BUG",
     "grid_builder.py:107-119 — DBCC SHRINKFILE inside engine.connect()",
     "SHRINKFILE inside an implicit user transaction is unreliable.\n\n"
     "FIX: Use engine.begin() with autocommit mode, or run in a dedicated AUTOCOMMIT connection."),

    # ===== LISTING =====
    ("LIST","3.1","NOW","BUG",
     "listing.py:823 — Cartesian explosion when rdc_mode == 'all'",
     "INNER JOIN ({stores_sql}) S ON 1=1 when msa_rdc_join is empty (the 'all' branch). Stores are crossed against MSA options before NOT EXISTS filters.\n\n"
     "EXAMPLE: 320 stores × 5,000 MSA options = 1.6M intermediate rows for what should be a few thousand. tempdb spikes; for larger MSAs (50K options) → 16M-row temp → minutes added to listing run.\n\n"
     "FIX: Always carry an RDC equality into the JOIN ON, even when rdc_mode='all' (use store→RDC mapping unconditionally)."),
    ("LIST","3.2","NOW","BUG",
     "listing.py:1737-1741 — REQ does not subtract ART_EXCESS",
     "Comment at line 1693 promises REQ formula deducts excess; SQL doesn't.\n\n"
     "EXAMPLE: Article has STK_TTL=80, MBQ=100, ART_EXCESS=30 (calculated earlier as excess across stores). REQ should be MBQ − STK_TTL − ART_EXCESS = -10 (don't order; have surplus). Code computes 100 − 80 = 20 → orders 20 unnecessarily → working capital tied up.\n\n"
     "FIX: Add `− ISNULL(ART_EXCESS,0)` to the REQ formula and clamp to 0."),
    ("LIST","3.3","NOW","BUG",
     "listing.py:1675-1682 — ART_EXCESS for IS_NEW=1 articles wrongly flags seed stock",
     "ART_EXCESS = STK − excess_multiplier × OPT_MBQ. New articles have OPT_MBQ=0.\n\n"
     "EXAMPLE: New SKU launched with seed stock of 50 in stores. OPT_MBQ=0 (no MBQ defined yet). ART_EXCESS = 50 − 1.5×0 = 50 → entire seed flagged excess → next allocation pulls it back to RDC → store has nothing to sell.\n\n"
     "FIX: WHERE OPT_MBQ > 0 guard on the ART_EXCESS calc, or CASE WHEN OPT_MBQ=0 THEN 0 ELSE STK − k*OPT_MBQ END."),
    ("LIST","3.4","NOW","BUG",
     "listing.py:776, :786 — MAJ_CAT/RDC f-string injection",
     "_effective_majcats interpolated as '{v}' with no escape; SSN block does chr(39)*2 escape but MAJ_CAT/store/RDC paths don't.\n\n"
     "EXAMPLE: A category named 'Men's' (legitimate apostrophe) breaks the SQL — WHERE MAJ_CAT IN ('Men's') syntax error. Worse: a malicious user with API access can inject `'); DROP TABLE ...; --`.\n\n"
     "FIX: Bind as parameters via SQLAlchemy expanding=True (or apply the same chr(39)*2 escape used in the SSN path)."),
    ("LIST","3.5","NOW","BUG",
     "listing.py:2105-2114 — alloc_result NameError on exception",
     "If pandas rule engine raises, alloc_result is never assigned. Subsequent alloc_result.get(...) raises NameError, masking the original error.\n\n"
     "EXAMPLE: Rule engine crashes on a bad merge; user sees 'NameError: name alloc_result is not defined' instead of the real cause. Debugging takes hours.\n\n"
     "FIX: alloc_result = {} immediately before the try:."),
    ("LIST","3.6","SOON","BUG",
     "listing.py:364-369 — has_running_session race allows duplicate runs",
     "Check + start_session is two separate steps; concurrent requests both pass the check.\n\n"
     "EXAMPLE: User clicks Generate twice quickly (or hits Enter twice). Both /generate calls pass has_running_session() → two sessions running → both writing to ARS_LISTING → cross-contamination.\n\n"
     "FIX: Atomic INSERT ... WHERE NOT EXISTS (SELECT 1 FROM SESSIONS WHERE STATUS='RUNNING') and check rowcount; or sp_getapplock."),
    ("LIST","3.7","SOON","BUG",
     "listing.py:1623-1652 — RL_HOLD potentially double-counted as available",
     "OPT_REQ uses STK_TTL directly; if STK_TTL already includes held stock, the 'available' math is inflated.\n\n"
     "EXAMPLE: Store has 100 units of which 20 are RL_HOLD (reserved for return-to-RDC). STK_TTL = 100. OPT_REQ logic treats all 100 as available → over-allocates 20 to other stores that won't actually be served.\n\n"
     "FIX: Audit whether STK_TTL is gross or net of holds; if gross, subtract RL_HOLD_QTY."),
    ("LIST","3.8","SOON","BUG",
     "listing.py:1413 — Pre-resolve UPDATE non-deterministic across MP variants",
     "UPDATE joins MP without de-dup. SQL Server picks any row when multiple match.\n\n"
     "EXAMPLE: Article 1234 has 5 variants in vw_master_product. Each variant has a different MAJ_CAT due to data quality. UPDATE picks any → MAJ_CAT='APP' on Monday's run, 'GM' on Tuesday's run. Listing flips.\n\n"
     "FIX: Build a temp from MP with ROW_NUMBER() OVER (PARTITION BY ARTICLE_NUMBER ORDER BY <tiebreaker>)=1 filter and JOIN to that."),
    ("LIST","3.9","SOON","BUG",
     "listing_allocator.py:185-188 — TRY_CAST chain silently drops non-numeric GEN_ART",
     "TRY_CAST(TRY_CAST(GEN_ART_NUMBER AS FLOAT) AS BIGINT) returns NULL for any non-numeric → row silently dropped.\n\n"
     "EXAMPLE: A test/staging article like TEST123 exists in MSA → silently disappears from allocator. No error, no log. Two days later someone notices the article never gets stock.\n\n"
     "FIX: Add WHERE V.[GEN_ART_NUMBER] IS NOT NULL and surface non-numeric rows in a separate diagnostic counter."),
    ("LIST","3.10","SOON","ENHANCEMENT",
     "listing.py:1399-1414 — Pre-resolve hot path rewrites every row × every column",
     "Rewrites every row in ARS_LISTING for every MP column on every run.\n\n"
     "EXAMPLE: 5M rows × 8 columns = 40M cell updates per run. With temp+ROW_NUMBER staging + WHERE L.[<col>] IS NULL skip, drops to a single pass per column on changed rows only — 5x speedup or more.\n\n"
     "FIX: Build MP staging temp once + add WHERE L.[<mc>] IS NULL to skip already-set rows."),
    ("LIST","3.11","SOON","ENHANCEMENT",
     "listing.py:1423-1488 — Six grid UPDATEs run sequentially against same table",
     "Six grids → six full-table UPDATEs against same listing table.\n\n"
     "EXAMPLE: Each takes ~30s → 3 minutes serial. Run in parallel via thread pool (each grid is independent on join keys) or coalesce into one UPDATE per grid type.\n\n"
     "FIX: Threaded run, or merge SET clauses where the source tables align."),
    ("LIST","3.12","LATER","ENHANCEMENT",
     "listing_allocator.py:1027-1092 — Greedy waterfall CTE rebuilt every iteration",
     "CTE re-evaluated every loop iteration: 50 rounds × 3 OPT_TYPEs × max_irod = ~1500 query executions, each scanning final_table.\n\n"
     "FIX: Pre-build per-round window into a temp table, reuse for all iterations of that round.\n"
     "NOTE: dormant code path (gated by `if False:` in listing.py) — fix when re-enabling."),
    ("LIST","3.13","LATER","ENHANCEMENT",
     "listing_allocator.py:233-292 — LTRIM(RTRIM(CAST(...))) on join columns kills index seeks",
     "Non-sargable predicates kill index seeks → table scans.\n\n"
     "FIX: Pre-trim source data once into a staging table with proper indexes."),
    ("LIST","3.14","LATER","BUG",
     "listing_sessions.py:50-51 — _ACTIVE_SINKS dict is per-process",
     "EXAMPLE: gunicorn -w 4. User starts session via worker A, kill request hits worker B → not found → silent no-op. Logs split across worker stdouts inconsistently.\n\n"
     "FIX: Move session/sink registry to DB; current behaviour is OK for single-worker dev only."),
    ("LIST","3.15","LATER","BUG",
     "listing_job_manager.py:179-180 — In-memory daemon-thread job manager loses state on restart",
     "EXAMPLE: App restart while listing job runs. Job vanishes from /jobs API. Daemon thread continues writing to DB until engine close fails. From user's POV the run 'just stopped'.\n\n"
     "FIX: Persist job state in DB; on startup, mark orphaned RUNNING sessions as ABORTED."),
    ("LIST","3.16","LATER","BUG",
     "listing_sessions.py:46 — os.makedirs at import-time",
     "EXAMPLE: LOG_DIR points to a path that's read-only or doesn't exist (container without volume mount). Module import crashes → API won't start.\n\n"
     "FIX: Wrap in try/except; fall back to tempfile.gettempdir()."),

    # ===== PENDING ALLOCATION =====
    ("PEND","4.1","NOW","BUG",
     "pend_alc_service.py:1399 — Lost update on MASTER_ALC_PEND.DO_QTY (concurrency)",
     "Read DO_QTY → compute new value → UPDATE, with no row lock.\n\n"
     "EXAMPLE: T0: Row has DO_QTY=0. T1: User A's upload reads DO_QTY=0, intends to add 10. T2: User B's upload reads DO_QTY=0, intends to add 10. T3: A writes 10. T4: B writes 10. (Should be 20.) Result: 10 units of receipt vanished. MSA next day deducts 10 too few from pending → FNL_Q is 10 too low → next allocation under-allocates by 10.\n\n"
     "FIX: Wrap each row's read-modify-write in a serialized transaction with WITH (UPDLOCK, HOLDLOCK) on the SELECT, or rewrite as a single UPDATE ... SET DO_QTY = DO_QTY + :delta WHERE ... (with OUTPUT for FIFO if needed)."),
    ("PEND","4.2","NOW","BUG",
     "pend_alc_service.py:1368 — Empty st_cd poisons stamp_bdc_qty across whole batch",
     "JOIN condition (u.st_cd = '' OR ISNULL(P.ST_CD,'') = u.st_cd). Mixing per-store and global pairs in the same temp table makes the OR fire across the entire batch.\n\n"
     "EXAMPLE: Payload [(rdc=1, art=A, st_cd='S1', qty=10), (rdc=1, art=B, st_cd='', qty=5)]. The temp table is shared. When stamping art=B (st_cd=''), the OR clause u.st_cd='' matches every row in PEND_ALC for (rdc=1, art=B). Worse: when stamping art=A's row, the OR is evaluated against every row in the temp — including art=B's empty st_cd — so art=A also gets globally stamped across all stores.\n\n"
     "FIX: Split into two queries: one with scoped st_cd, one with global; never mix in one JOIN. Or use a sentinel value (e.g. '__GLOBAL__') and explicit branches."),
    ("PEND","4.3","NOW","BUG",
     "pend_alc_service.py:1462 — DO_NUMBER concat truncation in NVARCHAR(100)",
     "'DO1, DO2, DO3, ...' concatenated into NVARCHAR(100).\n\n"
     "EXAMPLE: Article receives DOs DO00001 through DO00015. Concat exceeds 100 chars → silent truncation: stored value is 'DO00001, DO00002, ..., DO000'. Last DOs lost from the audit trail; revert by DO# breaks for the truncated ones.\n\n"
     "FIX: Either widen column to NVARCHAR(MAX) and dedupe + cap-to-N entries, or move DO numbers to a child table (PEND_ALC_DO_REF with FK + DO_NUMBER)."),
    ("PEND","4.4","NOW","BUG",
     "pend_alc.py:594 — bdc-generate two-phase race (concurrency)",
     "Read PEND_QTY > 0 rows → return Excel → stamp BDC_QTY = PEND_QTY in a second step.\n\n"
     "EXAMPLE: T0: PEND_QTY=20 for (rdc=1, art=A, st_cd=S1). T1: bdc-generate reads, returns Excel saying 'send 20'. T2: Concurrent DO upload increments DO_QTY by 5 → PEND_QTY drops to 15. T3: stamp_bdc_qty writes BDC_QTY=15. Result: Excel out the door says '20', ledger says '15 sent' → reconciliation fails when DOs come back.\n\n"
     "FIX: Wrap both phases in a single transaction with WITH (UPDLOCK, HOLDLOCK) on the initial SELECT; or pass the SELECT's data into stamp directly without re-querying."),
    ("PEND","4.5","NOW","BUG",
     "pend_alc_service.py:1289 — Cross-store DO credit when st_cd not supplied",
     "When DO row has no st_cd, the FIFO history-credit picks any matching (rdc, art) row, not the one whose alloc the DO was for.\n\n"
     "EXAMPLE: RDC 1, article A. Store S1 had PEND=5, store S2 had PEND=5. DO arrives for article A with no st_cd, qty=5. FIFO picks first by date → S1's history credited even though S2 was the actual recipient. Now S1's BDC_QTY says '5 received' when nothing was; S2 still shows '5 pending' forever.\n\n"
     "FIX: Require st_cd match between DO and history when caller supplies st_cd; when not supplied, fail loudly rather than guessing."),
    ("PEND","4.6","NOW","BUG",
     "pend_alc.py:661 / pend_alc_service.py:1226 — allocation_number race + ID re-read",
     "Sequence reservation is outside the insert transaction. After insert, IDs are read back by WHERE ALLOCATION_NUMBER = :a — if another generate raced to the same number, the SELECT returns mixed IDs.\n\n"
     "EXAMPLE: Two concurrent bdc-generate calls both reserve A100 (race in _get_next_allocation_no). Both insert rows with ALLOCATION_NUMBER=A100. Caller A reads back IDs WHERE ALLOC_NUM=A100 → gets B's rows too. User A clicks 'revert' → deletes user B's allocation.\n\n"
     "FIX: Use INSERT ... OUTPUT INSERTED.ID to get the IDs of *this insert only*; reserve allocation number via DB-level sequence/identity inside the same transaction."),
    ("PEND","4.7","NOW","BUG",
     "pend_alc_service.py:1369 — Re-stamp lies about historical ask",
     "Re-stamping overwrites BDC_QTY with current PEND_QTY, which may be smaller than the original ask after partial DO.\n\n"
     "EXAMPLE: Day 1: PEND=20, BDC stamped=20. Day 2: DO arrives 15 → PEND=5, DO_RECEIVED=15. Day 3: User runs bdc-generate again → BDC_QTY overwritten to 5. Ledger now says 'we asked for 5, received 15' → looks like over-receipt by 10.\n\n"
     "FIX: Block re-stamp if BDC_QTY > 0 (raise to caller); or accumulate (BDC_QTY = BDC_QTY + new_pend); or move to history-only model where BDC_QTY in PEND_ALC is never overwritten."),
    ("PEND","4.8","NOW","BUG",
     "pend_alc_service.py:1458 — DO silently dropped on already-closed alloc",
     "Filter IS_CLOSED=0 excludes already-closed rows. DOs arriving for closed allocations vanish.\n\n"
     "EXAMPLE: Allocation A50 fully received last week, IS_CLOSED=1. Today a late DO arrives with alloc_num=A50, qty=10 (operational delay). Filter excludes A50 row → 10 units silently disappear from the pipeline. Excel says 'received', PEND_ALC says nothing happened.\n\n"
     "FIX: Either log/return 'could not consume X units, alloc closed' so caller can investigate, or reopen IS_CLOSED=0 and let the standard FIFO handle it."),
    ("PEND","4.9","NOW","BUG",
     "pend_alc.py:486-507 — do-update has separate commits between PEND and history",
     "apply_do_deductions commits PEND_ALC; update_bdc_history_with_do then commits history. Failure between leaves them inconsistent.\n\n"
     "EXAMPLE: Apply succeeds (DO_QTY incremented in PEND_ALC). DB connection drops before history update. PEND says 'received', history says 'didn't'. MSA next day uses PEND values → correct. But revert flow uses history → inconsistent. Reconciliation report will flag the drift weeks later.\n\n"
     "FIX: Single transaction across both calls. Move commits out of the service functions and commit once at the endpoint."),
    ("PEND","4.10","NOW","BUG",
     "pend_alc.py:1109 — manual-upload partial commit leaves orphan rows",
     "Three sequential commits (write_manual_pend → adjust_msa → log_operation). MSA failure leaves rows with no operation log.\n\n"
     "EXAMPLE: User uploads 1000 manual pending rows. Write succeeds. MSA adjust fails (lock timeout). Operation log never written. User has no revert handle; rows are now stuck in PEND_ALC permanently.\n\n"
     "FIX: Single transaction; write op log first or last with same txn; fail-fast strategy."),
    ("PEND","4.11","NOW","BUG",
     "pend_alc_service.py:1659, :1684, :1700 — adjust_msa silent failure",
     "Broad except Exception returns partial result; manual-upload reports success=true regardless.\n\n"
     "EXAMPLE: Manual upload of 5000 rows. MSA adjust query times out — exception caught, result has 'error' field but caller doesn't read it. UI shows green check. Three days later, allocation is wrong because MSA was never actually adjusted.\n\n"
     "FIX: Surface result['error'] as HTTP 500 (or as a warning the UI must acknowledge). Don't silently degrade."),
    ("PEND","4.12","SOON","BUG",
     "pend_alc_service.py:1620 — NULL STK_QTY → FNL_Q stored as NULL",
     "STK − PEND − HOLD is NULL if STK is NULL → CASE WHEN NULL < 0 is unknown → FNL_Q stored as NULL.\n\n"
     "EXAMPLE: New article never stocked → STK_QTY=NULL. FNL_Q ends up NULL. Downstream WHERE FNL_Q > 0 excludes it (correct), but SUM(FNL_Q) aggregates ignore NULLs → reporting numbers don't match.\n\n"
     "FIX: ISNULL(T.STK_QTY,0) − PEND − HOLD."),
    ("PEND","4.13","SOON","BUG",
     "pend_alc_service.py:548, :580 — ISO-string-to-DATETIME binding is dialect-fragile",
     "Revert reads ISO timestamp from JSON payload and binds as a string to a DATETIME column.\n\n"
     "EXAMPLE: pyodbc + ODBC Driver 17 accepts the conversion; pyodbc + ODBC Driver 18 with newer SQL Server may reject '2026-05-06T14:30:25' (the T separator). Revert flow that worked in dev suddenly 500s in prod after driver update.\n\n"
     "FIX: datetime.fromisoformat(s) before binding; use a datetime object."),
    ("PEND","4.14","SOON","BUG",
     "pend_alc_service.py:267 — _load_operation JSON parse 500s entire endpoint",
     "Bare _json.loads(row[8]) with no try/except.\n\n"
     "EXAMPLE: One historical operation row has a corrupt PAYLOAD (deploy mid-write, char-set issue, manual SQL fix). Calling /operations endpoint iterates → first malformed row throws → endpoint 500 → the entire log is unreachable until that row is fixed.\n\n"
     "FIX: Per-row try/except; mark malformed rows as _error: true in the response and continue."),
    ("PEND","4.15","SOON","BUG",
     "pend_alc.py:728 — Excel MATERIAL NO lstrip('0') destroys all-zero articles",
     "EXAMPLE: Article number '00000' → empty string in Excel → upload back of that file fails with 'ARTICLE_NUMBER required'. Edge case but real with placeholder articles.\n\n"
     "FIX: s.lstrip('0') or '0'."),
    ("PEND","4.16","SOON","ENHANCEMENT",
     "pend_alc.py:235, :1182 — OUTER APPLY runs per row, twice per request",
     "OUTER APPLY (SELECT TOP 1 ... ORDER BY BDC_DATE DESC) runs per row, twice per request (count + select).\n\n"
     "EXAMPLE: Detail page for 50K pending rows. 50K × 2 sub-queries × 5ms = 8 minutes for the page load.\n\n"
     "FIX: Replace with LEFT JOIN to a CTE: latest_bdc AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY RDC, ST_CD, ARTICLE ORDER BY BDC_DATE DESC, ID DESC) rn FROM ...) filtered to rn=1."),
    ("PEND","4.17","SOON","ENHANCEMENT",
     "pend_alc_service.py:1424 — Per-row DO processing loop",
     "EXAMPLE: 5000-row DO upload → 10000 round-trips (SELECT + UPDATE per row) × 5ms = 50s. Batching via temp table + FIFO CTE drops to ~2s.\n\n"
     "FIX: INSERT to temp, UPDATE ... FROM cte with ROW_NUMBER + SUM OVER to do FIFO matching in one pass."),
    ("PEND","4.18","SOON","ENHANCEMENT",
     "pend_alc_service.py:341, :1351, :1492 — Bulk inserts not using fast_executemany",
     "SQLAlchemy text(...) with list-of-dicts doesn't enable fast_executemany; the pyodbc-direct path at line 1092 does.\n\n"
     "EXAMPLE: 100K-row backfill via SQLAlchemy path = ~5 minutes; same data via fast_executemany pyodbc = ~30 seconds.\n\n"
     "FIX: Drop to raw pyodbc (with cursor.fast_executemany = True) for bulk inserts, mirroring the existing manual-upload path."),
    ("PEND","4.19","SOON","BUG",
     "pend_alc_service.py:937 — Idempotency check without unique index",
     "NOT EXISTS check at app level only.\n\n"
     "EXAMPLE: Two concurrent calls with same (SESSION, RDC, ST_CD, ARTICLE, MODE) → both pass NOT EXISTS check → both insert → duplicate rows.\n\n"
     "FIX: Add unique constraint on the 5-tuple; rely on UNIQUE violation to enforce idempotency, with try/except IntegrityError: pass."),
    ("PEND","4.20","SOON","BUG",
     "pend_alc.py many lines — HTTPException(500, str(e)) leaks internals",
     "Every endpoint does except Exception as e: raise HTTPException(500, str(e)).\n\n"
     "EXAMPLE: Exception 'Cannot insert null into rep_data.dbo.MASTER_ALC_PEND.RDC' → browser shows table + column to anyone with API access.\n\n"
     "FIX: logger.exception(e) + raise HTTPException(500, 'internal error') (with optional opaque error id)."),
    ("PEND","4.21","LATER","ENHANCEMENT",
     "pend_alc_service.py:316 — Backfill loop creates temp table per iteration",
     "EXAMPLE: 10K historical allocations → 10K CREATE+DROP TABLE → ~30 minutes. Single global temp keyed by allocation_number = ~1 minute.\n\n"
     "FIX: Build one temp table once."),
    ("PEND","4.22","LATER","ENHANCEMENT",
     "pend_alc.py:75 — Summary endpoint runs 4 separate round-trips",
     "FIX: Single CTE-driven query."),
    ("PEND","4.23","LATER","MAINTENANCE",
     "pend_alc_service.py:1250 — Return type annotation says int, returns dict",
     "FIX: Update annotation to Dict."),

    # ===== CROSS-CUTTING =====
    ("XCUT","5.1","SOON","BUG",
     "Module-level mutable singletons break multi-worker WSGI",
     "_ACTIVE_SINKS, job_manager, _ACTIVE_JOBS appear in listing_sessions/listing_job_manager.\n\n"
     "WHY: Per-process state breaks under gunicorn -w N; orphan threads on restart.\n\n"
     "FIX: Persist registry to DB; on startup, mark stale RUNNING sessions ABORTED."),
    ("XCUT","5.2","SOON","BUG",
     "Bare `except: pass` around DDL operations across grid_builder/grid_calculations/listing",
     "Silent column-rename / add-column failures cause downstream 'invalid column name' errors far from the cause.\n\n"
     "FIX: Replace with `IF COL_LENGTH(...) IS NULL ALTER TABLE ...` (idempotent SQL guard); if you must catch, check error class."),
    ("XCUT","5.3","NOW","BUG",
     "f-string SQL with user-supplied list across listing/grid_builder/msa_stock",
     "Listing (MAJ_CAT/store/RDC), grid_builder (KPI), msa_stock — one SSN block escapes; rest don't.\n\n"
     "WHY: Apostrophe in data breaks SQL; malicious input enables injection.\n\n"
     "FIX: SQLAlchemy expanding=True parameter binding; whitelist known values."),
    ("XCUT","5.4","NOW","BUG",
     "Dtype mismatch on join keys across msa_service/listing_allocator/pend_alc",
     "RDC int↔str, ST_CD int↔str, GEN_ART_NUMBER int↔str.\n\n"
     "WHY: Silent all-NaN merges in pandas; silent zero-row joins in SQL Server. Single cause of multiple 'phantom missing' bugs.\n\n"
     "FIX: Central normalization layer at every DB read boundary; document each table's true dtype."),
    ("XCUT","5.5","SOON","ENHANCEMENT",
     "No OUTPUT INSERTED.ID on bulk insert across pend_alc_service",
     "Re-SELECT to recover IDs is slower AND racy under concurrency.\n\n"
     "FIX: Adopt OUTPUT INSERTED.ID INTO @ids everywhere bulk insert is followed by ID retrieval."),
    ("XCUT","5.6","SOON","BUG",
     "HTTPException(500, str(e)) across pend_alc.py / listing.py / msa_stock.py",
     "Leaks SQL/schema/path information to clients.\n\n"
     "FIX: One project-wide exception handler that logs full stack and returns generic message."),
]


# ── Loader ──────────────────────────────────────────────────────────────────
def main():
    conn = pyodbc.connect(CONN_STR, autocommit=False)
    cur = conn.cursor()

    # 1. Clean prior load (re-runnable)
    cur.execute(f"DELETE FROM PT_ACTIVITY_LOG WHERE PROJECT_ID IN (SELECT PROJECT_ID FROM PT_PROJECT WHERE PROJECT_CODE LIKE '{CODE_PREFIX}%')")
    cur.execute(f"DELETE FROM PT_PROJECT WHERE PROJECT_CODE LIKE '{CODE_PREFIX}%'")
    deleted = cur.rowcount
    print(f"Cleaned {deleted} prior review rows.")

    # 2. Insert root project
    seq = [0]
    def code():
        seq[0] += 1
        return f"{CODE_PREFIX}{seq[0]:04d}"

    root_code = code()
    cur.execute("""
        INSERT INTO PT_PROJECT (PARENT_ID, PROJECT_CODE, NAME, DESCRIPTION, PROJECT_TYPE,
                                STATUS, PRIORITY, PHASE, CATEGORY, TAGS,
                                OWNER_USERNAME, ASSIGNEES, PROGRESS_PCT, AUTO_PROGRESS,
                                START_DATE, DUE_DATE, CREATED_BY, UPDATED_BY)
        OUTPUT INSERTED.PROJECT_ID
        VALUES (NULL, ?, ?, ?, 'PROJECT',
                'NOT_STARTED', 'CRITICAL', 'PHASE_1', 'MAINTENANCE', 'review,code-review,may-2026',
                ?, ?, 0, 1,
                ?, ?, ?, ?)
    """,
    root_code,
    "ARS Code Review Action Items - May 2026",
    "Findings from the May-2026 code review of MSA stock calculation, grid builder, listing, and pending allocation modules. Each task lists the file:line, a concrete example of the bug or perf issue, and a suggested fix. Priority maps: CRITICAL=fix this sprint (data corruption / endpoint broken), HIGH=fix in 2-4 weeks, MEDIUM=cleanup later. Use Status=IN_PROGRESS when picking up a task; STATUS=COMPLETED auto-rolls progress up to module + root.",
    ACTOR, ACTOR,
    TODAY, date(2026, 7, 31), ACTOR, ACTOR)
    root_id = int(cur.fetchone()[0])
    cur.execute("INSERT INTO PT_ACTIVITY_LOG (PROJECT_ID, ACTIVITY_TYPE, ACTOR, DETAILS) VALUES (?, 'CREATED', ?, ?)",
                root_id, ACTOR, f"Created root '{root_code}' from review-loader script")
    print(f"  Root: id={root_id} code={root_code}")

    # 3. Insert module sub-projects
    module_ids = {}
    for key, name, desc in MODULES:
        c = code()
        cur.execute("""
            INSERT INTO PT_PROJECT (PARENT_ID, PROJECT_CODE, NAME, DESCRIPTION, PROJECT_TYPE,
                                    STATUS, PRIORITY, PHASE, CATEGORY, TAGS,
                                    OWNER_USERNAME, ASSIGNEES, PROGRESS_PCT, AUTO_PROGRESS,
                                    START_DATE, DUE_DATE, CREATED_BY, UPDATED_BY)
            OUTPUT INSERTED.PROJECT_ID
            VALUES (?, ?, ?, ?, 'SUB_PROJECT',
                    'NOT_STARTED', 'HIGH', 'PHASE_1', 'MAINTENANCE', ?,
                    ?, ?, 0, 1,
                    ?, ?, ?, ?)
        """,
        root_id, c, name, desc,
        f"review,module-{key.lower()}",
        ACTOR, ACTOR,
        TODAY, date(2026, 7, 31), ACTOR, ACTOR)
        sub_id = int(cur.fetchone()[0])
        module_ids[key] = sub_id
        cur.execute("INSERT INTO PT_ACTIVITY_LOG (PROJECT_ID, ACTIVITY_TYPE, ACTOR, DETAILS) VALUES (?, 'CREATED', ?, ?)",
                    sub_id, ACTOR, f"Created sub-project '{c}' ({key})")
        print(f"  Sub: id={sub_id} code={c} module={key}")

    # 4. Insert findings
    inserted = 0
    for mod_key, fid, severity, category, name, desc in FINDINGS:
        parent_id = module_ids[mod_key]
        c = code()
        full_desc = f"[Finding {fid}]\n\n{desc}\n\n---\nSource: May-2026 code review. Severity bucket: {severity}."
        tags = f"review,module-{mod_key.lower()},sev-{severity.lower()},finding-{fid}"
        cur.execute("""
            INSERT INTO PT_PROJECT (PARENT_ID, PROJECT_CODE, NAME, DESCRIPTION, PROJECT_TYPE,
                                    STATUS, PRIORITY, PHASE, CATEGORY, TAGS,
                                    OWNER_USERNAME, ASSIGNEES, PROGRESS_PCT, AUTO_PROGRESS,
                                    START_DATE, DUE_DATE, CREATED_BY, UPDATED_BY)
            OUTPUT INSERTED.PROJECT_ID
            VALUES (?, ?, ?, ?, 'TASK',
                    'NOT_STARTED', ?, ?, ?, ?,
                    ?, ?, 0, 1,
                    ?, ?, ?, ?)
        """,
        parent_id, c, name[:255], full_desc, PRIORITY[severity], PHASE[severity], category, tags,
        ACTOR, ACTOR,
        TODAY, DUE[severity], ACTOR, ACTOR)
        task_id = int(cur.fetchone()[0])
        cur.execute("INSERT INTO PT_ACTIVITY_LOG (PROJECT_ID, ACTIVITY_TYPE, ACTOR, DETAILS) VALUES (?, 'CREATED', ?, ?)",
                    task_id, ACTOR, f"Imported review finding {fid}")
        inserted += 1

    print(f"  Tasks: {inserted}")
    conn.commit()
    print(f"\nCommitted. Total rows: 1 root + {len(MODULES)} subs + {inserted} tasks = {1 + len(MODULES) + inserted}.")

    # 5. Verify counts
    cur.execute(f"""
        SELECT PROJECT_TYPE, PRIORITY, COUNT(*)
        FROM PT_PROJECT
        WHERE PROJECT_CODE LIKE '{CODE_PREFIX}%'
        GROUP BY PROJECT_TYPE, PRIORITY
        ORDER BY PROJECT_TYPE, PRIORITY
    """)
    print("\nBreakdown:")
    for r in cur.fetchall():
        print(f"  {r[0]:<12} {r[1]:<10} {r[2]}")

    cur.execute(f"""
        SELECT TAGS, COUNT(*)
        FROM PT_PROJECT
        WHERE PROJECT_CODE LIKE '{CODE_PREFIX}%' AND PROJECT_TYPE = 'TASK'
        GROUP BY TAGS
    """)
    by_module = {}
    for r in cur.fetchall():
        for tag in r[0].split(','):
            if tag.startswith('module-'):
                by_module[tag] = by_module.get(tag, 0) + r[1]
    print("\nTasks per module:")
    for k, v in sorted(by_module.items()):
        print(f"  {k:<20} {v}")

    conn.close()


if __name__ == "__main__":
    main()
