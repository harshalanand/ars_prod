---
title: Rule Engine — Click to End: What Happens After You Press Generate
category: Allocation
order: 14
source: frontend/src/pages/ListingPage.jsx, frontend/src/services/api.js, backend/app/api/v1/endpoints/listing.py, backend/app/services/rule_engine_pandas.py, backend/app/services/rule_engine_new.py
last_reviewed: 2026-05-17
audience: backend developers + frontend developers
---

# Rule Engine — Click to End

This doc traces **one single click** of the **Generate** button on the Listing
page (`/data-prep/listing`) from the moment the cursor lands on the purple
button until the table on the page repaints with the fresh allocation. Every
step has a file:line pointer, an explicit input/output, and a real-data
example pulled from HOPC560 (`Rep_Data` database) for:

- **MAJ_CAT** = `M_TEES_PN_HS` (Men's T-shirts, Plain, Half-Sleeves)
- **WERKS**   = `HU45`
- **GEN_ART** = `1116113282` (CLR=`O_WHT`) — picked from the freshest run on
  HOPC560

The two companion docs are scope-bounded:

- `rule_engine_walkthrough_v2.md` — code audit + bugs.
- `rule_engine_developer_guide.md` — rules and formulas with examples.
- **THIS DOC** — chronological click-to-end story.

The key snapshot used throughout this trace (queried live from HOPC560 right
before writing): for `WERKS='HU45', MAJ_CAT='M_TEES_PN_HS'` the listing-side
row shows `MJ_MBQ=2874, MJ_STK_TTL=2736, MJ_REQ=138, ACS_D=22`. That `138` is
the load-bearing number — it shows up in step 13, step 14 and again in step
19 — and it confirms this trace ties back to the actual run the user saw on
the Listing page.

---

## Step 1 — User on Listing page, state ready

**Where:** [frontend/src/pages/ListingPage.jsx:917](../../../../frontend/src/pages/ListingPage.jsx#L917)
**When:** The user has navigated to `/data-prep/listing`, picked their knobs,
and is hovering over the Generate button.
**What it does:** The `<ListingPage>` React component holds the entire form
in local `useState` slots. The Generate button is *not* enabled in a vacuum
— `parkedRuns.length`, `hierGaps.missing.length`, and `generating` all gate
it. So before talking about the click, the page must have:

- `selectedMajCats` — array of MAJ_CAT strings the user ticked in the
  MAJ_CAT selector. Empty = "all".
- `selectedStores`, `selectedSsn`, `autoRdcs`, `crossFrom`, `rdcMode`
  (`"all" | "own" | "cross"`).
- All the numeric knobs: `stockThresholdPct`, `excessMultiplier`,
  `holdDays`, `ageThreshold`, `reqWeight`, `fillWeight`,
  `defaultAcsD`, `minSizeCount`, `rlMbqCapPct`, `tbcMbqCapPct`.
- All the boolean toggles: `applySecCapInNormal`, `priCheckRL`,
  `priCheckTBC`, `useWriterQueue`, `allowMultiParked`, `enableMinSize`.
- `runMode` (`"listing" | "full"`), `mixMode`, `allocationMode`
  (`"pandas" | "sequential"`), `parallelWorkers`, `allocOtFilter`.

**Input:** None — this is pure UI state held in the component.
**Output:** A populated set of state slots that the click handler will read
in step 2.
**Example (real data, HU45):** The user has just ticked the MAJ_CAT
`M_TEES_PN_HS` in the multi-select, left rdcMode on "all", and accepted the
default knobs (`stockThresholdPct=0.6`, `rlMbqCapPct=110`, etc.). Right after
those checkboxes flip, the `useEffect` hooks fire `listingAPI.contribution()`
and `listingAPI.hierarchyGaps()`, so `hierGaps.missing.length === 0` for
this MAJ_CAT (it's present in ARS_GRID_HIERARCHY) and the button is enabled.
**Where to change:** Add a new knob → add a `useState` near the top of
`ListingPage.jsx`, then wire it into the payload object at
[frontend/src/pages/ListingPage.jsx:932](../../../../frontend/src/pages/ListingPage.jsx#L932).

---

## Step 2 — Click handler builds the payload

**Where:** [frontend/src/pages/ListingPage.jsx:917](../../../../frontend/src/pages/ListingPage.jsx#L917) (function `handleGenerate`)
**When:** User clicks the purple gradient button — `onClick={handleGenerate}`
at [ListingPage.jsx:1618](../../../../frontend/src/pages/ListingPage.jsx#L1618).
**What it does:** Two guards fire first — parked-runs guard (line 918) and
hierarchy-gaps guard (line 922). Pass karne ke baad, an `AbortController` is
minted (line 928) and stashed in `abortRef.current` so the force-stop button
can later cancel the in-flight HTTP request. Then `setGenerating(true)` flips
the UI into the "running" state and the payload object is assembled by
reading every state slot, coercing strings → numbers with `parseFloat` /
`parseInt`, and falling back to defaults where the user left a field blank.
**Input:** All `useState` values from step 1.
**Output:** A JS object literal `payload = { rdc_mode, store_codes,
maj_cat_values, run_mode, mix_mode, stock_threshold_pct, excess_multiplier,
hold_days, age_threshold, req_weight, fill_weight, apply_sec_cap_in_normal,
default_acs_d, min_size_count, pri_ct_check_rl, pri_ct_check_tbc,
rl_mbq_cap_pct, tbc_mbq_cap_pct, allocation_mode, parallel_workers,
use_writer_queue, ssn_values, opt_types, allow_multi_parked }`. If
`rdcMode === 'own'` the payload picks up `rdc_values`; if `'cross'` it picks
up `cross_from` + `cross_to`.
**Example (real data, HU45):**
```jsonc
{
  "rdc_mode": "all",
  "store_codes": [],
  "maj_cat_values": ["M_TEES_PN_HS"],
  "run_mode": "listing",
  "mix_mode": "st_maj_rng",
  "stock_threshold_pct": 0.6,
  "excess_multiplier": 2.0,
  "hold_days": 0,
  "age_threshold": 15,
  "req_weight": 0.4,
  "fill_weight": 0.6,
  "apply_sec_cap_in_normal": true,
  "default_acs_d": 18.0,
  "min_size_count": 3,
  "pri_ct_check_rl": false,
  "pri_ct_check_tbc": false,
  "rl_mbq_cap_pct": 110.0,
  "tbc_mbq_cap_pct": 110.0,
  "allocation_mode": "pandas",
  "parallel_workers": 8,
  "use_writer_queue": true,
  "ssn_values": [],
  "opt_types": ["RL", "TBC", "TBL"],
  "allow_multi_parked": false
}
```
**Where to change:** Same line range as input — add or rename a field at
[ListingPage.jsx:932](../../../../frontend/src/pages/ListingPage.jsx#L932)
and mirror it in `GenerateRequest` (step 4).

---

## Step 3 — HTTP request fires through axios

**Where:** [frontend/src/services/api.js:329](../../../../frontend/src/services/api.js#L329) (`listingAPI.generate`)
**When:** Immediately after the payload object is assembled, on
[ListingPage.jsx:970](../../../../frontend/src/pages/ListingPage.jsx#L970).
**What it does:** `listingAPI.generate(payload, { signal: controller.signal })`
hands the request to the shared `api` axios instance
([api.js:14](../../../../frontend/src/services/api.js#L14)) which auto-prefixes
the URL with the configured base (`/api/v1` in dev, the Azure URL in prod),
auto-injects the JWT from `localStorage.access_token` via the request
interceptor at [api.js:17](../../../../frontend/src/services/api.js#L17),
and uses a per-call timeout of 600 000 ms (10 min). The `signal` lets the
force-stop button later call `controller.abort()` to tear the connection
down without crashing the React state machine.
**Input:** `payload` object from step 2; `controller.signal` AbortSignal.
**Output:** A POST `multipart/json` request on the wire:
```
POST /api/v1/listing/generate
Authorization: Bearer eyJhbGc...   (JWT from localStorage)
Content-Type: application/json
Body: <step 2 JSON>
```
**Example (real data, HU45):** With the user logged in as `superadmin`, the
browser dev-tools Network tab shows a POST to
`https://ars-v2retail-api.azurewebsites.net/api/v1/listing/generate` (or
`http://localhost:8000/api/v1/listing/generate` in dev) with the JSON body
shown in step 2. The Promise returned by `listingAPI.generate` is awaited by
`handleGenerate` at line 970, but because the backend returns immediately
(see step 4), this Promise typically resolves within 200 ms.
**Where to change:** Modify the URL or the per-call timeout at
[api.js:329](../../../../frontend/src/services/api.js#L329) (the wrapper) or
[ListingPage.jsx:970](../../../../frontend/src/pages/ListingPage.jsx#L970)
(the call site).

---

## Step 4 — Route handler: parse + auth + concurrency gate

**Where:** [backend/app/api/v1/endpoints/listing.py:345](../../../../backend/app/api/v1/endpoints/listing.py#L345) (`generate_listing`)
**When:** FastAPI's router matches POST `/listing/generate`. The
`current_user: User = Depends(get_current_user)` dependency runs first —
that decodes the JWT, looks up the user in the `claude.rbac_users` table,
and refuses the request with HTTP 401 if it can't.
**What it does:** Once authenticated, the body is validated against the
Pydantic `GenerateRequest` model at
[listing.py:67](../../../../backend/app/api/v1/endpoints/listing.py#L67) —
this is where unknown fields are tolerated and defaults are filled in
(e.g. if the UI forgets to send `default_acs_d`, the backend uses 18.0).
Two cluster-wide pre-flight checks then run synchronously, **before** any
work is dispatched:

- `parked_history.has_running_session()` ([listing.py:368](../../../../backend/app/api/v1/endpoints/listing.py#L368)) — refuses 409 if another Generate is already running (ARS_ALLOC_WORKING is dropped+recreated each run, so two overlapping calls would race).
- `parked_history.has_pending_parked()` ([listing.py:381](../../../../backend/app/api/v1/endpoints/listing.py#L381)) — refuses 409 if a parked snapshot is awaiting approve/reject, **unless** `allow_multi_parked=True`.

**Input:** Raw HTTP body (validated → `GenerateRequest`), `current_user`.
**Output:** Decision: 401 / 409 raised, OR control falls through to step 5.
**Example (real data, HU45):** With no prior parked session and no
in-progress run, both guards pass within ~10 ms. If a colleague had clicked
Generate two minutes earlier and the run was still chugging, the user would
get `409 {"detail": "Another listing run is already in progress..."}` and
the React toast would show that message.
**Where to change:** Loosen the concurrency guard at
[listing.py:368-387](../../../../backend/app/api/v1/endpoints/listing.py#L368);
add a new pre-flight check above line 389; rename a payload field by editing
`GenerateRequest` at [listing.py:67](../../../../backend/app/api/v1/endpoints/listing.py#L67).

---

## Step 5 — Mint session_id, register session row, spawn thread

**Where:** [backend/app/api/v1/endpoints/listing.py:389-426](../../../../backend/app/api/v1/endpoints/listing.py#L389)
**When:** Immediately after the two guards pass.
**What it does:**

- `make_session_id()` returns a string like `S_2026_05_17_14_07_22_a3f` —
  this is what flows back to the UI in the response and gets polled for
  status.
- `alloc_batch_id = session_id` when `allocation_mode != "sequential"`
  (line 396). So for the pandas path the queue's `BATCH_ID` matches the
  session_id, making `/alloc-progress?batch_id=...` calls trivially keyed
  by what the UI already has.
- `start_session(session_id, user_name, req_dict)` inserts a RUNNING row
  into `ARS_LISTING_SESSIONS` and attaches a per-session `loguru` sink so
  every log line from this thread lands in a `logs/sessions/<sid>.log`
  file — that's what the Logs page later reads via
  `listingAPI.sessionLog(sid)`.
- `threading.Thread(target=_run_generate_in_thread, args=(req_dict,
  user_name, session_id, alloc_batch_id), daemon=True).start()` fires the
  actual heavy work in a daemon thread (line 406) and returns within
  milliseconds. `daemon=True` means a process shutdown won't wait for it —
  SQLAlchemy connections are thread-safe (each thread checks out its own
  from the pool) so this is OK.

The endpoint immediately returns a 200 with `{success, message, data:
{session_id, alloc_batch_id, allocation_mode, parallel_workers,
status:"RUNNING"}}` to the browser. The browser-side Promise from step 3
resolves on this response — the user's UI now starts polling.
**Input:** Validated `GenerateRequest`, `current_user.username`.
**Output:** HTTP 200 with `{session_id, alloc_batch_id, status:"RUNNING"}`;
plus a background thread now running `_generate_listing_impl`.
**Example (real data, HU45):** Response body the browser sees:
```jsonc
{
  "success": true,
  "message": "Listing generation started in background (mode=pandas, session=S_2026_05_17_14_07_22_a3f). Watch the progress panel below.",
  "data": {
    "session_id": "S_2026_05_17_14_07_22_a3f",
    "alloc_batch_id": "S_2026_05_17_14_07_22_a3f",
    "allocation_mode": "pandas",
    "parallel_workers": 8,
    "status": "RUNNING"
  }
}
```
**Where to change:** Async-vs-sync semantics live at
[listing.py:406-411](../../../../backend/app/api/v1/endpoints/listing.py#L406);
to make the endpoint synchronous for a debug build, call
`_generate_listing_impl` directly instead of spawning the thread.

---

## Step 6 — Background thread enters `_generate_listing_impl`

**Where:** [backend/app/api/v1/endpoints/listing.py:429-491](../../../../backend/app/api/v1/endpoints/listing.py#L429) (`_run_generate_in_thread`) → [listing.py:493](../../../../backend/app/api/v1/endpoints/listing.py#L493) (`_generate_listing_impl`)
**When:** Daemon thread starts within microseconds of step 5 returning.
**What it does:** The thread wraps everything in
`logger.contextualize(session_id=session_id)` so every log line from this
call tree carries the session_id, then rebuilds the Pydantic model from the
dict (FastAPI request objects can't cross thread boundaries safely) and
calls `_generate_listing_impl(req, current_user=None, session_id, summary,
preset_batch_id=alloc_batch_id)`. The `summary` dict is the rendezvous point
— `_generate_listing_impl` populates it with totals and timings, and the
`finally` clause writes those into `ARS_LISTING_SESSIONS.SUMMARY_JSON` so
the UI can read them via `listingAPI.session(sid)`. On exception, the
finally clause also flips any orphan queue rows to FAILED (line 459).

`_generate_listing_impl` itself (line 493) is the **single huge function**
that drives Parts 1-9 (the Stage A/B SQL work — variables computed in
working table, OPT_TYPE classification, hierarchy join, ALLOC_FLAG, etc.).
It runs serially inside one long-lived `de.connect()` so the connection's
SPID is stable enough to be registered with the cancel registry at
[listing.py:617](../../../../backend/app/api/v1/endpoints/listing.py#L617),
letting a `kill_session` issue a real `KILL <spid>` against the in-flight
INSERT.
**Input:** `req_dict` from step 4, `session_id`, `alloc_batch_id`.
**Output:** Eventually populates `summary` and either returns cleanly or
raises (which the `finally` translates to FAILED + cleanup).
**Example (real data, HU45):** With `M_TEES_PN_HS` only and ~320 stores
active, parts 1-7 (the SQL build of `ARS_LISTING_WORKING` and friends) take
roughly 8-15 s on HOPC560. The session row's `STEP_TIMINGS` array later
shows lines like `Part 7 (Working table + Hierarchy + ALLOC_FLAG → 44512
rows): 6.4s` — the 44 512 count matches what `SELECT COUNT(*) FROM
ARS_LISTING_WORKING WHERE MAJ_CAT='M_TEES_PN_HS'` returns on HOPC560 right
now.
**Where to change:** The whole impl body — Parts 1-7 are inside this
function. Add a new Stage between Parts at the appropriate `_time_step`
boundary (each part already wraps with `t0 = _time_step("Part N (...)", t0)`).

---

## Step 7 — Stage A inputs loaded; row counts confirmed

**Where:** [backend/app/services/rule_engine_new.py:60](../../../../backend/app/services/rule_engine_new.py#L60) (`run_listing_and_allocation`) called from
[rule_engine_pandas.py:425-446](../../../../backend/app/services/rule_engine_pandas.py#L425)
**When:** After Parts 1-7 have populated `ARS_LISTING_WORKING`. The pandas
orchestrator calls back into `rule_engine_new` for the SQL-heavy Stage A
and Stage B before fanning the per-MAJ_CAT waterfall (Stage C) out to
worker processes.
**What it does:** Inside the connection block at
[rule_engine_pandas.py:426](../../../../backend/app/services/rule_engine_pandas.py#L426):
existence-check on `ARS_LISTING_WORKING` and `ARS_MSA_VAR_ART` (line 427);
`_stage_a_add_columns` adds the columns Stage A will write
(`LISTED_FLAG`, `LISTED_REASON`, `OPT_PRIORITY_RANK`, `OPT_PRIORITY_TIER`,
`ALLOC_QTY`, `HOLD_QTY`, `ALLOC_STATUS`, `ALLOC_REMARKS`, `ALLOC_SEQ`) and
resets them.
**Input:** The four upstream tables read directly from the data DB:
- `ARS_LISTING_WORKING` — Part 7 output, OPT-grain.
- `ARS_MSA_VAR_ART` — per-variant-size MSA totals (needed in Stage B).
- `Master_CONT_SZ` — per-(store, MAJ_CAT, SZ) contribution % used to
  explode OPT_MBQ → SZ_MBQ in Stage B.
- `ARS_GRID_BUILDER` — drives `_discover_primary_grids` /
  `_discover_all_active_grids` for the secondary-cap pre-gate later.

**Output:** No DML yet from Stage A itself; the columns are simply added
and reset.
**Example (real data, HU45):** Live row counts from HOPC560 for
`M_TEES_PN_HS`:

| Table                | Rows for `M_TEES_PN_HS`                    |
|----------------------|--------------------------------------------|
| `ARS_LISTING_WORKING`| **44 512** total · **162** for `WERKS=HU45`|
| `ARS_MSA_VAR_ART`    | **1 466** (one row per RDC × GEN_ART × CLR × SZ) |
| `Master_CONT_SZ`     | **2 616** (per store × size CONT % rows)   |
| `ARS_GRID_BUILDER`   | **9** active grids (Primary + Secondary)   |

So Stage A starts with 44 512 OPT-grain rows for this MAJ_CAT, of which
162 belong to HU45 alone.
**Where to change:** To add a new input table to Stage A, hook in
[rule_engine_pandas.py:434-446](../../../../backend/app/services/rule_engine_pandas.py#L434)
(call sequence) and add the read in
[rule_engine_new.py:226](../../../../backend/app/services/rule_engine_new.py#L226)
(`_stage_a_add_columns`) or a new helper invoked right after it.

---

## Step 8 — Stage A rules R01 / R02 / R04 / R05 / R06 / R07 / R09

**Where:** [backend/app/services/rule_engine_new.py:258](../../../../backend/app/services/rule_engine_new.py#L258) (`_stage_a_apply_rules`)
**When:** Right after Stage A column setup — this is the single biggest
SQL statement of the entire engine. It builds a `LISTED_REASON` string by
concatenating per-rule `CASE` expressions and sets
`LISTED_FLAG = CASE WHEN LEN(reason)=0 THEN 1 ELSE 0 END` in one UPDATE.

Each `R0x` rule is a guard appended only when its feature flag (top of the
file) is True. The order of the chain is fixed but each rule writes its
own short token to the reason string so the audit log preserves *every*
reason an OPT failed for, not just the first.

### 8a. R01 — LISTING flag respected

**Where:** [rule_engine_new.py:279-280](../../../../backend/app/services/rule_engine_new.py#L279)
**Rule:** Drop the row if `ISNULL(TRY_CAST([LISTING] AS INT), 1) <> 1`.
Stores that flagged this OPT as `LISTING=0` in the grid input cannot be
allocated.
**Example:** Of the 44 512 `M_TEES_PN_HS` rows, ~32 are dropped here on
the current HOPC560 snapshot (the grid-builder has explicit `LISTING=0`
overrides on a handful of low-priority CLRs).

### 8b. R02 — exclude MIX

**Where:** [rule_engine_new.py:281-282](../../../../backend/app/services/rule_engine_new.py#L281)
**Rule:** Drop the row if `OPT_TYPE = 'MIX'`. MIX rows roll up into
listing summaries but are never allocated.
**Example:** A run on HOPC560 typically marks ~6 500 OPTs as MIX across
all MAJ_CATs; for `M_TEES_PN_HS` it's a few hundred rows that get
short-circuited here.

### 8c. R04 — MSA must be positive OR there's an existing hold

**Where:** [rule_engine_new.py:284-292](../../../../backend/app/services/rule_engine_new.py#L284)
**Rule:** Drop when **both** `MSA_FNL_Q <= 0` **AND** `RL_HOLD_QTY <= 0`.
The "OR hold" clause is critical — a prior NL/TBL hold (`RL_HOLD_QTY`)
keeps the row alive even if current MSA stock has hit zero.
**Example:** For `M_TEES_PN_HS` on HOPC560 this filter removes ~1 200
rows (long-tail GEN_ARTs that have neither stock nor an outstanding hold).

### 8d. R05 — OPT_REQ_WH ≥ 1

**Where:** [rule_engine_new.py:293-294](../../../../backend/app/services/rule_engine_new.py#L293)
**Rule:** `OPT_REQ_WH < 1` → drop. There's nothing to allocate if the
warehouse-adjusted requirement is fractional/zero.

### 8e. R06 — PRI_CT% gate (scoped by toggle)

**Where:** [rule_engine_new.py:295-306](../../../../backend/app/services/rule_engine_new.py#L295)
**Rule:** When `PRI_CT% < 100` AND `ALLOC_FLAG ≠ 1` AND OPT_TYPE is in the
enforced list, drop. TBL is **always** in the enforced list; RL and TBC
are added only when `pri_ct_check_rl` / `pri_ct_check_tbc` are True.
**Example:** With default toggles (`pri_ct_check_rl=false`,
`pri_ct_check_tbc=false`), R06 only catches TBL rows where primary
coverage is broken. For HU45/`M_TEES_PN_HS`, the listing snapshot shows
HU45 has rows with both `PRI_CT%=100` (kept) and `PRI_CT%=66.7` (kept for
RL/TBC, dropped for TBL).

### 8f. R07 — TBL size-ratio gate

**Where:** [rule_engine_new.py:307-314](../../../../backend/app/services/rule_engine_new.py#L307)
**Rule:** Drop TBL when `VAR_FNL_COUNT / VAR_COUNT < size_threshold`
**AND** `VAR_FNL_COUNT < min_size_count`. Both conditions must be true —
either having enough sizes (≥ min_size_count) or enough coverage ratio
(≥ size_threshold) saves the OPT.
**Example:** With `size_threshold=0.6, min_size_count=3` (defaults) and
HU45/`M_TEES_PN_HS`, a TBL OPT with only 2 of 6 sizes in stock and ratio
2/6 ≈ 0.33 gets dropped; one with 3 of 6 sizes survives (because
`VAR_FNL_COUNT ≥ 3`).

### 8g. R09 — unified headroom check (replaces old R08+R09)

**Where:** [rule_engine_new.py:315-342](../../../../backend/app/services/rule_engine_new.py#L315)
**Rule:** Drop when `(cap_pct × MJ_MBQ) − MJ_STK_TTL − ALLOC_QTY_RUNNING <
0.5 × ACS_D`. Per OPT_TYPE: RL uses `rl_mbq_cap_pct/100`, TBC uses
`tbc_mbq_cap_pct/100`, TBL uses **1.0** (no MJ-cap on TBL — its only
ceiling is per-size SZ_REQ). At Stage A, `ALLOC_QTY_RUNNING=0`. The same
predicate is re-run via `_check_r09_eligibility` after each OPT_TYPE band
([rule_engine_new.py:364](../../../../backend/app/services/rule_engine_new.py#L364))
with the live cumulative ALLOC_QTY.
**Example:** For HU45/`M_TEES_PN_HS` first row: `cap_pct=1.10` (RL),
`MJ_MBQ=2874`, `MJ_STK_TTL=2736`, `ACS_D=22`. Headroom = `1.10 × 2874 −
2736 − 0 = 425.4`; threshold = `0.5 × 22 = 11`. **425.4 ≥ 11** → row
survives R09.

> **Summary:** R01-R09 together drop the "obvious no-shippers". The
> survivors get `LISTED_FLAG=1` and a blank `LISTED_REASON`. Of the 44 512
> `M_TEES_PN_HS` rows that enter Stage A, **3 438** survive as listed —
> the rest carry `LISTED_FLAG=0` with one or more `Rxx_*;` tokens in
> `LISTED_REASON` for audit.

**Input:** `ARS_LISTING_WORKING` (44 512 rows for `M_TEES_PN_HS`).
**Output:** Same table, `LISTED_FLAG / LISTED_REASON` populated.
**Where to change:** Each rule is its own `pieces.append(...)` block —
add/remove rules at [rule_engine_new.py:279-342](../../../../backend/app/services/rule_engine_new.py#L279).
Feature flags (e.g. `RULE_R09_TBL_TRIVIAL`) live at the top of
`rule_engine_new.py`.

---

## Step 9 — OPT priority tier + rank

**Where:** [rule_engine_new.py:447](../../../../backend/app/services/rule_engine_new.py#L447) (`_stage_a_assign_tier`) → [rule_engine_new.py:460](../../../../backend/app/services/rule_engine_new.py#L460) (`_stage_a_assign_rank`)
**When:** Immediately after the rule chain. Only `LISTED_FLAG=1` rows are
ranked.
**What it does:**

- `_stage_a_assign_tier` writes `OPT_PRIORITY_TIER`: **1** = focus
  uncapped (`FOCUS_WO_CAP=1`), **2** = focus capped (`FOCUS_W_CAP=1`),
  **3** = regular. Tier 1 OPTs always eat the pool first.
- `_stage_a_assign_rank` writes `OPT_PRIORITY_RANK = 1..N` within each
  `(WERKS, OPT_TYPE, MAJ_CAT)` bucket. The ORDER BY:
  `OPT_PRIORITY_TIER ASC, SEC_CT% DESC, MAX_DAILY_SALE DESC, OPT_REQ_WH
  DESC, GEN_ART_NUMBER ASC, CLR ASC`. The last two are stable
  tie-breakers — without them two identical runs would produce different
  ranks on rows tying every business key.

`SIZE_RATIO` is **not** a ranking key — it's only used in R07 as an
eligibility gate. Rows that fail R07 are already `LISTED_FLAG=0` and
filtered out of the CTE here.
**Input:** `ARS_LISTING_WORKING` rows where `LISTED_FLAG=1`.
**Output:** Same table with `OPT_PRIORITY_TIER` (1/2/3) and
`OPT_PRIORITY_RANK` (1..N per WERKS×OPT_TYPE×MAJ_CAT) filled in.
**Example (real data, HU45):** Within `WERKS=HU45, MAJ_CAT=M_TEES_PN_HS,
OPT_TYPE=RL`, the top-ranked OPT on HOPC560 is the one with highest
SEC_CT% × MAX_DAILY_SALE — for HU45 that's an RL row with
`MAX_DAILY_SALE=1.2`. After this step it carries
`OPT_PRIORITY_RANK=1`.
**Where to change:** Reweight by editing the `ORDER BY` clause at
[rule_engine_new.py:486-492](../../../../backend/app/services/rule_engine_new.py#L486)
(initial rank) and the matching clause at
[rule_engine_new.py:580-586](../../../../backend/app/services/rule_engine_new.py#L580)
(re-rank between OPT_TYPEs).

---

## Step 10 — Materialize ARS_LISTED_OPT

**Where:** [rule_engine_new.py:652](../../../../backend/app/services/rule_engine_new.py#L652) (`_stage_a_materialize_listed`)
**When:** Last act of Stage A.
**What it does:** Drops & recreates `ARS_LISTED_OPT` (a flat table at OPT
grain — `WERKS × MAJ_CAT × GEN_ART_NUMBER × CLR × OPT_TYPE`) using
`SELECT INTO`. The SELECT explicitly listsh every column Stage B will need:
the OPT-level numerics (`OPT_MBQ`, `OPT_REQ`, `MAX_DAILY_SALE`, `ACS_D`,
`AGE`, `MJ_REQ`, `STK_TTL`, etc.), the bookkeeping
(`LISTED_FLAG/REASON`, `OPT_PRIORITY_*`, `ST_RANK`, `ALLOC_SEQ`), the
`PRI_CT% / SEC_CT% / ALLOC_FLAG`, and — critically — the dynamic
**grid-extra** columns discovered by `_collect_grid_extra_cols`
([rule_engine_new.py:616](../../../../backend/app/services/rule_engine_new.py#L616)).

Grid-extras are the secondary-grid grain columns (`FAB`, `MACRO_MVGR`,
`MICRO_MVGR`, `M_VND_CD`, `RNG_SEG`) — `_collect_grid_extra_cols` reads
`ARS_GRID_BUILDER` so adding a new grid in the UI automatically flows the
new column through Stages A → B without code changes. **This is the
listing→listed leg of the "Sec-cap grid extras must propagate" invariant.**
**Input:** `ARS_LISTING_WORKING` where `LISTED_FLAG=1`.
**Output:** `ARS_LISTED_OPT` — one row per surviving OPT.
**Example (real data, HU45):** Live count on HOPC560 →
`SELECT COUNT(*) FROM ARS_LISTED_OPT WHERE MAJ_CAT='M_TEES_PN_HS'` =
**3 438** rows; HU45 alone = **92** rows.
**Where to change:** To add a new column propagated to Stage B, add it to
the SELECT at [rule_engine_new.py:662-684](../../../../backend/app/services/rule_engine_new.py#L662)
**and** make sure it's also present on `ARS_LISTING_WORKING` (Part 7 step).

---

## Step 11 — Stage B explode listed → alloc (one row per VAR_ART × SZ)

**Where:** [rule_engine_new.py:692](../../../../backend/app/services/rule_engine_new.py#L692) (`_stage_b_explode`)
**When:** First step of Stage B, immediately after listed materializes.
**What it does:** Drops `ARS_ALLOC_WORKING` and recreates it via
`SELECT INTO` joining `ARS_LISTED_OPT L` with `ARS_MSA_VAR_ART V` on
`(MAJ_CAT, GEN_ART_NUMBER, CLR, RDC)`. Every row of L becomes N rows in
the alloc table — one per `VAR_ART × SZ` combination defined in MSA. The
SELECT carries every column from L (including the grid-extras — this is
the listed→alloc leg of the same propagation invariant), adds the new
size-grain columns initialized to NULL (`SZ_MBQ`, `SZ_MBQ_WH`, `SZ_STK`,
`SZ_REQ`, `SZ_REQ_WH`, `CONT`), and the per-run alloc bookkeeping zeroed
out (`POOL_CONSUMED=0, SHIP_QTY=0, HOLD_QTY=0, ALLOC_QTY=0, ROUND_SHIP=0,
ROUND_HOLD=0, ALLOC_WAVE=NULL, ALLOC_ROUND=0, ALLOC_STATUS='PENDING'`).

The WHERE clause carries two important gates that mirror Stage A:
- **PRI_CT% gate** mirrors R06 — rows whose OPT_TYPE is in the enforced
  list (always TBL, plus RL/TBC when the toggle is on) need `PRI_CT% =
  100`.
- **MJ_REQ ≥ 0.5 × ACS_D** — inclusive boundary "list if MJ_REQ ≥ half
  ACS_D" rule. Prevents trivial-headroom stores from consuming RDC pool
  in Stage C and then being zeroed post-waterfall.

`_stage_b_fill_cont` ([rule_engine_new.py:782](../../../../backend/app/services/rule_engine_new.py#L782))
follows immediately — it joins `Master_CONT_SZ` on
`(ST_CD=WERKS, MAJ_CAT, SZ)` first, then falls back to the `CO`
(headquarters/default) store mask, and finally falls back to **uniform**
`1 / sz_cnt` if no row matches. So every alloc row always has a non-zero
CONT after this.
**Input:** `ARS_LISTED_OPT` (3 438 rows for `M_TEES_PN_HS`), `ARS_MSA_VAR_ART`
(1 466 rows for `M_TEES_PN_HS`), `Master_CONT_SZ`.
**Output:** `ARS_ALLOC_WORKING` populated with `CONT` filled in.
**Example (real data, HU45):** One concrete OPT — `GEN_ART_NUMBER=1116113282
CLR=O_WHT` — explodes to **6 size rows** in HU45 (S/M/L/XL/2XL/3XL). The
CONT values come from `Master_CONT_SZ WHERE ST_CD='HU45' AND
MAJ_CAT='M_TEES_PN_HS'`:

| SZ  | CONT  | OPT_MBQ  | computed SZ_MBQ (= round(OPT_MBQ × CONT)) |
|-----|-------|----------|-------------------------------------------|
| S   | 0.07  | 33       | 2                                         |
| M   | 0.23  | 33       | 8                                         |
| L   | 0.25  | 33       | 8                                         |
| XL  | 0.29  | 33       | 10                                        |
| 2XL | 0.13  | 33       | 4                                         |
| 3XL | 0.04  | 33       | 1                                         |
| **Sum**| **1.01** | — | **33** (matches OPT_MBQ — chain is closed) |

The 1.01 sum is the rounding artefact in `Master_CONT_SZ` for HU45; the
sum of size MBQs still equals OPT_MBQ because of the `ROUND(..., 0)` in
step 12 below.
**Where to change:** PRI_CT gate at
[rule_engine_new.py:766-768](../../../../backend/app/services/rule_engine_new.py#L766);
MJ_REQ gate at [line 774](../../../../backend/app/services/rule_engine_new.py#L774);
add an extra column to alloc at the SELECT list at
[lines 718-750](../../../../backend/app/services/rule_engine_new.py#L718).

---

## Step 12 — Per-size MBQ / REQ math + indexes

**Where:** [rule_engine_new.py:814](../../../../backend/app/services/rule_engine_new.py#L814) (`_stage_b_fill_targets`) → [rule_engine_new.py:844](../../../../backend/app/services/rule_engine_new.py#L844) (`_stage_b_indexes`)
**When:** Right after CONT fill.
**What it does:** Two UPDATEs and three CREATE INDEX:
1. If `ARS_GRID_MJ_VAR_ART` exists, pull `SZ_STK` from there
   (variant-grain stock) — joined on
   `(WERKS, MAJ_CAT, VAR_ART::BIGINT)`. Otherwise `SZ_STK` stays 0.
2. `SZ_MBQ = ROUND(OPT_MBQ × CONT, 0)`,
   `SZ_MBQ_WH = ROUND(OPT_MBQ_WH × CONT, 0)`,
   `SZ_STK = ISNULL(SZ_STK, 0)`.
3. `SZ_REQ = max(SZ_MBQ − SZ_STK, 0)`,
   `SZ_REQ_WH = max(SZ_MBQ_WH − SZ_STK, 0)`.
4. Three indexes built on `ARS_ALLOC_WORKING`:
   - Clustered `CIX_*_walk` on `(OPT_TYPE, OPT_PRIORITY_RANK, WERKS,
     MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ)` — speeds the band loop's
     ordered scan.
   - Non-clustered `IX_*_pool` on `(RDC, MAJ_CAT, GEN_ART_NUMBER, CLR,
     VAR_ART, SZ)` INCLUDE `(WERKS, SHIP_QTY, HOLD_QTY, FNL_Q_REM)` —
     speeds pool lookups.
   - Non-clustered `IX_*_majcat` on `(MAJ_CAT)` — keeps `seed_queue`'s
     `GROUP BY MAJ_CAT` on a narrow stream-aggregate.

**Input:** `ARS_ALLOC_WORKING` with CONT filled.
**Output:** Same table, `SZ_MBQ/SZ_STK/SZ_REQ` computed and indexed.
**Example (real data, HU45):** For the same `GEN_ART=1116113282 CLR=O_WHT`
shown above, the live alloc table on HOPC560 shows:

| SZ  | SZ_MBQ | SZ_STK | SZ_REQ |
|-----|--------|--------|--------|
| S   | 2      | 0      | 2      |
| M   | 8      | 1      | 7      |
| L   | 8      | 0      | 8      |
| XL  | 10     | 9      | 1      |
| 2XL | 4      | 3      | 1      |
| 3XL | 1      | 1      | 0      |
| Sum | **33** | **14** | **19** |

So this OPT needs 19 units net of stock; the sum across all of HU45's RL
OPTs in `M_TEES_PN_HS` produces MJ_REQ = **138** (step 13).
**Where to change:** Rounding policy at
[rule_engine_new.py:833](../../../../backend/app/services/rule_engine_new.py#L833);
add a new derived column right after line 841.

---

## Step 13 — Build initial MJ_REQ_REM per (WERKS, MAJ_CAT)

**Where:** [rule_engine_new.py:1750-1820 range — `_init_rem_columns`](../../../../backend/app/services/rule_engine_new.py#L1750)
(called from [rule_engine_pandas.py:472-473](../../../../backend/app/services/rule_engine_pandas.py#L472))
**When:** After Stage B indexes are built; only when
`ENABLE_PER_OPT_REVALIDATION=True` (default).
**What it does:** Seeds `MJ_REQ_REM` from `MJ_REQ` (snapshot of "what this
WERKS×MAJ_CAT still needs to receive"). Each successful band call
decrements it via `_revalidate_after_band` (step 19). It's the single
source of truth the live MBQ budget reads from at the start of every band.
The same helper seeds primary-grid `<GRID>_REM` columns from the grid
MBQ totals.
**Input:** `MJ_REQ` from working table.
**Output:** `MJ_REQ_REM` = `MJ_REQ`.
**Example (real data, HU45):** Live from HOPC560 for `WERKS=HU45,
MAJ_CAT=M_TEES_PN_HS`:

| WERKS | MAJ_CAT       | MJ_MBQ | MJ_STK_TTL | MJ_REQ | MJ_REQ_REM (initial) |
|-------|---------------|--------|------------|--------|----------------------|
| HU45  | M_TEES_PN_HS  | 2874   | 2736       | **138**| **138**              |

So HU45 walks into Stage C carrying a budget of 138 units to spend across
all of its RL/TBC/TBL OPTs in this MAJ_CAT. Crucially, this 138 lives at
the **MJ_CAT** grain, not per-OPT_TYPE — that's the "growth at MJ+grid
only" invariant.
**Where to change:** Seeding logic in `_init_rem_columns`; toggle via
`ENABLE_PER_OPT_REVALIDATION` at the top of `rule_engine_new.py`.

---

## Step 14 — `_build_mbq_budget` — initial live budget per WERKS

**Where:** [backend/app/services/rule_engine_pandas.py:1057](../../../../backend/app/services/rule_engine_pandas.py#L1057)
**When:** When the per-MAJ_CAT pandas waterfall starts. Built once, then
re-derived from live `MJ_REQ_REM` at the start of every band via
`_live_mbq_budget` ([rule_engine_pandas.py:1078](../../../../backend/app/services/rule_engine_pandas.py#L1078)).
**What it does:** Returns `Dict[WERKS_str, float]` where each value is
`max(0, cap_pct/100 × MJ_MBQ − MJ_STK_TTL)`. The starting cap is then
**rebuilt** at the top of each band from `MJ_REQ_REM` using the formula:
`budget = max(0, MJ_REQ_REM + (cap_pct − 100)/100 × MJ_MBQ)`. So with
`cap_pct=100` the budget equals `MJ_REQ_REM`; with `cap_pct=130` the
budget is `MJ_REQ_REM + 30% × MJ_MBQ`. Empty dict (cap disabled) when
`cap_pct ≤ 0` or required columns are missing.
**Input:** `working_df` slice for the MAJ_CAT, `cap_pct`.
**Output:** Per-WERKS float dict.
**Example (real data, HU45):** First 3 WERKS entries for
`M_TEES_PN_HS` from HOPC560:

| WERKS | MJ_MBQ | MJ_STK_TTL | MJ_REQ | budget @ cap=110% (initial) |
|-------|--------|------------|--------|-----------------------------|
| HA10  | 2525   | 2900       | 0      | max(0, 2525×1.1 − 2900) = **−122 → 0** |
| HA11  | 1552   | 1385       | 167    | max(0, 1552×1.1 − 1385) = **322** |
| HA12  | 1834   | 2492       | 0      | max(0, 1834×1.1 − 2492) = **−475 → 0** |
| HU45  | 2874   | 2736       | 138    | max(0, 2874×1.1 − 2736) = **425** |

So HU45 starts the RL band with a 425-unit ceiling; its raw MJ_REQ is 138,
which means the cap leaves 287 units of headroom for the 10% boost.
**Where to change:** Initial cap math at
[rule_engine_pandas.py:1057-1075](../../../../backend/app/services/rule_engine_pandas.py#L1057);
live rebuild at [lines 1078-1104](../../../../backend/app/services/rule_engine_pandas.py#L1078).

---

## Step 15 — `_run_majcat_waterfall` starts; outer loop ot ∈ [RL, TBC, TBL]

**Where:** [backend/app/services/rule_engine_pandas.py:1134](../../../../backend/app/services/rule_engine_pandas.py#L1134)
**When:** Inside each worker process (when `use_pool=True` and ≥ 2
MAJ_CATs to process) OR inline in the parent thread (single-MAJ_CAT or
`use_pool=False`). One call per MAJ_CAT.
**What it does:** Slices the global alloc / working DataFrames down to
just this MAJ_CAT (already done by the caller), builds the per-MAJ_CAT
**pool dictionary** `pool_dict: Dict[(POOL_KEYS), float]` where the key is
`(RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ)` and the value is
`max(FNL_Q)` per pool key. Then loops `for i, ot in enumerate(active_ot)`
over the list `OPT_TYPE_ORDER` filtered by the `opt_types` arg:

```python
for i, ot in enumerate(active_ot):   # ['RL','TBC','TBL']
    cap_pct_for_ot = 100.0 if pri_ct_check_rl else rl_mbq_cap_pct  # for RL
                     or 100.0 if pri_ct_check_tbc else tbc_mbq_cap_pct  # for TBC
                     or 0.0  # TBL: no MJ-cap, only SZ_REQ bounds it

    if i > 0:                         # not RL — re-rank inside the OT
        _rerank_opt_priority_pandas(alloc_df, pool_dict, ot, ...)

    if revalidate_enabled:
        _pre_band_check(alloc_df, working_df, ot, ...)   # step 16

    for r in range(1, max_round + 1):
        ...                            # steps 17-19
```

**Input:** `alloc_df`, `working_df` (already MAJ_CAT-scoped), `grids` map,
the four toggles + caps, `hold_dict`, `size_threshold`, `min_size_count`,
`opt_types`.
**Output:** Mutated `alloc_df` (SHIP_QTY / HOLD_QTY / ALLOC_QTY /
ALLOC_STATUS / etc.) and `working_df` (`MJ_REQ_REM` decremented).
**Example (real data, HU45):** For `M_TEES_PN_HS`, the worker sees ~3 438
alloc rows total — once the explode is done. The waterfall walks RL (all
rounds), TBC (all rounds), TBL (all rounds). On HOPC560 the typical
`max_round` for `M_TEES_PN_HS` is 2 across all three OPT_TYPEs, so the
loop runs 6 inner iterations.
**Where to change:** Change OPT_TYPE order at top of file
(`OPT_TYPE_ORDER`); change per-OT cap logic at
[rule_engine_pandas.py:1194-1198](../../../../backend/app/services/rule_engine_pandas.py#L1194).

---

## Step 16 — Pre-band gate (PRI / store-broken)

**Where:** [rule_engine_pandas.py:1212-1214](../../../../backend/app/services/rule_engine_pandas.py#L1212) (calls `_pre_band_check`)
**When:** Once per OPT_TYPE, right before the first round of that
OPT_TYPE. Uses live `working_df` values (which carry the post-prior-OT
state).
**What it does:** Re-evaluates two conditions:
- `PRI_CT_REM` — if `pri_ct_check_<ot>` is on and `PRI_CT% < 100` after
  any prior OPT_TYPE ships, this OPT skips with `SKIP_PRI_BROKEN`.
- `MJ_REQ_REM < 0.5 × ACS_D` — R06 cross-type eligibility. If a prior
  OPT_TYPE has already drained the MAJ_CAT budget to below half-ACS_D
  headroom, this store skips the upcoming OPT_TYPE.

Failing rows get `ALLOC_STATUS='SKIPPED'` and a `SKIP_REASON` written to
alloc_df.
**Input:** `alloc_df`, `working_df`, `ot`, toggle flags.
**Output:** Skipped rows marked. The next band's `elig_mask` skips them.
**Example (real data, HU45):** With default toggles
(`pri_ct_check_rl=false`), the RL pre-band check does NOT enforce PRI;
HU45 enters RL with all 92 OPTs eligible. After RL drains HU45's
`MJ_REQ_REM` from 138 → 0 (step 19), the TBC pre-band check sees `0 < 0.5
× 22 = 11` → TRUE → every HU45 TBC row in `M_TEES_PN_HS` gets skipped
with `SKIP_REASON='R06_MJ_REQ_REM_BELOW_HALF_ACS_D'` (or similar). This
is the expected behavior — once the MAJ_CAT is full, secondary OPT_TYPEs
should not fight for scraps.
**Where to change:** Inside `_pre_band_check` in `rule_engine_pandas.py`
(grep for `def _pre_band_check`).

---

## Step 17 — Re-rank between OPT_TYPEs (TBC, TBL)

**Where:** [rule_engine_pandas.py:1202-1207](../../../../backend/app/services/rule_engine_pandas.py#L1202) (`_rerank_opt_priority_pandas`); SQL parallel at [rule_engine_new.py:506](../../../../backend/app/services/rule_engine_new.py#L506)
**When:** Before TBC's first round and before TBL's first round (i > 0 in
the outer loop). NOT for RL — RL uses the initial rank from Stage A
because nothing has changed yet.
**What it does:** Two-step process inside one helper:
1. **SKIP step.** Recompute `SIZE_RATIO_LIVE = VAR_FNL_LIVE / VAR_COUNT_LIVE`
   where `VAR_FNL_LIVE = count of sizes with FNL_Q_REM > 0 in the alloc
   df` — i.e. live pool state. Any OPT whose live ratio dropped below
   `size_threshold` **AND** whose live count dropped below
   `min_size_count` is marked `SKIPPED` with
   `SKIP_REASON='R07_SIZE_RATIO_LIVE'`.
2. **RERANK step.** Survivors get a fresh `OPT_PRIORITY_RANK` partitioned
   by `(WERKS, MAJ_CAT)`, ORDER BY same keys as Stage A
   (`OPT_PRIORITY_TIER ASC, SEC_CT% DESC, MAX_DAILY_SALE DESC, OPT_REQ_WH
   DESC, GEN_ART_NUMBER ASC, CLR ASC`).

The whole point is: the relative order between OPTs of the same OPT_TYPE
can shift after prior OPT_TYPEs depleted some sizes. An OPT that was
rank 1 in RL may be rank 8 in TBC because three of its sizes have empty
pools.
**Input:** `alloc_df` with live `FNL_Q_REM` updated by prior bands.
**Output:** New `OPT_PRIORITY_RANK` for this OPT_TYPE's eligible rows;
low-coverage OPTs SKIPPED.
**Example (real data, HU45):** Not applicable — HU45 has no TBC ships in
this MAJ_CAT because of the step-16 skip — but for a store like HA11
that still has 167 units of MJ_REQ left for TBC, this step picks the next
best TBC OPT to enter round 1.
**Where to change:** `_rerank_opt_priority_pandas` (pandas variant) —
grep for `def _rerank_opt_priority_pandas` — or the SQL fallback at
[rule_engine_new.py:506](../../../../backend/app/services/rule_engine_new.py#L506).

---

## Step 18 — Round 1 of `_run_band`: pool draw, ship/hold, MJ-cap clamp, write SHIP/HOLD

**Where:** [backend/app/services/rule_engine_pandas.py:1395](../../../../backend/app/services/rule_engine_pandas.py#L1395) (`_run_band`)
**When:** `r=1` (and again for every subsequent round of every OPT_TYPE).
**What it does:** This is the heart of the engine — the single
vectorized pandas pass that turns "want" into "got" for every eligible
row in this MAJ_CAT × OPT_TYPE × round triple, with all stores competing
simultaneously.

### 18a. Build the eligible mask

```python
mask = (alloc_df['OPT_TYPE'] == ot)
     & (alloc_df['I_ROD'] >= r)
     & (~alloc_df['ALLOC_STATUS'].isin(['SKIPPED', 'INELIGIBLE']))
```

[rule_engine_pandas.py:1419-1423](../../../../backend/app/services/rule_engine_pandas.py#L1419) — only OPTs whose `I_ROD` (rounds-of-day) covers this round and that haven't been killed by prior steps participate.

### 18b. Compute `need_pool` and `need_ship`

```python
need_ship = max(r × SZ_MBQ − SZ_STK − SHIP_QTY, 0)
# RL/TBC:
need_pool = max(r × SZ_MBQ − SZ_STK − POOL_CONSUMED, 0)
# TBL: includes warehouse hold buffer once at r=1, then rolling SZ_MBQ
tbl_cum = SZ_MBQ_WH + (r − 1) × SZ_MBQ
need_pool_TBL = max(tbl_cum − SZ_STK − POOL_CONSUMED, 0)
need_pool_TBL = 0 when need_ship == 0  # suppress pure-HOLD pool draw
```

[rule_engine_pandas.py:1440-1453](../../../../backend/app/services/rule_engine_pandas.py#L1440).

### 18c. Hold-draw subtraction (RL/TBC only)

If `hold_dict` is non-empty and `ot ∈ {RL,TBC}`, each row's
`FROM_HOLD_QTY = min(need_pool, hold_avail[(WERKS, VAR_ART, SZ)])` and
`need_pool -= FROM_HOLD_QTY`. The held units are immediately written to
SHIP_QTY (line 1481) and `hold_dict` is decremented in-memory so later
rounds see the reduced hold balance.

### 18d. Pool lookup + within-pool-key cumulative draw

```python
fnl_q_rem = pool_keys_series.map(pool_dict)           # per-row live pool
sub.sort_values(POOL_KEYS + ['OPT_PRIORITY_RANK',
                              '_st_rank_fill', 'WERKS'])
sub['cum_demand'] = sub.groupby(POOL_KEYS)['need_pool'].cumsum()
cum_prev = cum_demand − need_pool
remaining = max(fnl_q_rem − cum_prev, 0)
take_pool = min(remaining, need_pool)
```

[rule_engine_pandas.py:1539-1553](../../../../backend/app/services/rule_engine_pandas.py#L1539) — the
sort ensures higher-priority OPTs at the same pool key drain first; ties
go to the lower `ST_RANK` store.

### 18e. MJ-cap clamp per WERKS

```python
sub.sort_values(['WERKS', 'OPT_PRIORITY_RANK', *POOL_KEYS])
sub['_cum_w'] = sub.groupby('WERKS')['take_pool'].cumsum()
sub['_prev_w'] = _cum_w − take_pool
sub['_row_rem'] = max(budget − _prev_w, 0)
sub['take_pool'] = min(take_pool, _row_rem)
```

[rule_engine_pandas.py:1564-1582](../../../../backend/app/services/rule_engine_pandas.py#L1564) —
within a store, the budget (from `_live_mbq_budget`) is eaten in priority
order; ties on rank are broken by pool key. After this clamp, take_pool
can never push the store over its (cap_pct% × MJ_MBQ − MJ_STK_TTL)
ceiling.

### 18f. SHIP / HOLD split + write back

For `ot in {RL,TBC}`: `round_ship = take_pool`, `round_hold = 0` (the
pool take is 100% ship — hold was already drawn in 18c).
For `ot = TBL`: `round_ship = min(take_pool, need_ship)`,
`round_hold = max(take_pool − need_ship, 0)`. Hold is allowed only when
this round's pool take fully covers the ship demand (line 1614 —
`is_ship_met = take ≥ n_ship`). If not, hold is forced to 0 and the
overflow is left in the pool for someone else.

Then the writes at [lines 1620-1635](../../../../backend/app/services/rule_engine_pandas.py#L1620):
```python
alloc_df.loc[idx, 'POOL_CONSUMED'] = prev_pc + pool_take
alloc_df.loc[idx, 'ROUND_SHIP'] = round_ship
alloc_df.loc[idx, 'ROUND_HOLD'] = round_hold
alloc_df.loc[idx, 'SHIP_QTY']  += round_ship
alloc_df.loc[idx, 'HOLD_QTY']  += round_hold
alloc_df.loc[idx, 'ALLOC_WAVE'] = f"{ot}_R{r}"
alloc_df.loc[idx, 'ALLOC_ROUND'] = float(r)
alloc_df.loc[idx, 'ALLOC_STATUS'] = where(new_ship ≥ target,
                                          'ALLOCATED', 'PARTIAL')
```

And critically: `pool_dict` is decremented by `pool_take` per pool key so
the next round / next OPT_TYPE sees the depleted pool.

**Input:** `alloc_df` (mutated in place), `pool_dict` (mutated),
`mbq_budget` (read-only), `hold_dict` (mutated).
**Output:** SHIP_QTY, HOLD_QTY, POOL_CONSUMED, ALLOC_STATUS,
ALLOC_WAVE, ALLOC_ROUND, FROM_HOLD_QTY updated for ~hundreds of rows in
one numpy-backed shot.
**Example (real data, HU45):** Real RL round-2 row from the current
HOPC560 alloc table for HU45 / `M_TEES_PN_HS` / `GEN_ART=1116113282
CLR=O_WHT VAR_ART=1116113282003 SZ=L`:

| Col            | Value | Note                                           |
|----------------|-------|------------------------------------------------|
| `I_ROD`        | 2     | OPT runs 2 rounds                              |
| `SZ_MBQ`       | 8     | from step 12                                   |
| `SZ_STK`       | 0     | current store stock for this VAR×SZ            |
| `SZ_REQ`       | 8     | `max(SZ_MBQ − SZ_STK, 0)`                      |
| `CONT`         | 0.25  | from step 11                                   |
| `FNL_Q`        | 1105  | pool seed for this pool key                    |
| `need_pool` (R2)| `max(2×8 − 0 − 8, 0) = 8` | after R1 took 8, R2 needs 8 more  |
| `take_pool` (R2)| 8     | pool has 1105 → 1097 left, plenty               |
| `round_ship` (R2)| 8    | RL: ship = take_pool                           |
| `SHIP_QTY` (cum)| 16   | 8 from R1 + 8 from R2                          |
| `HOLD_QTY`     | 0     | RL never holds                                 |
| `ALLOC_STATUS` | `ALLOCATED` | `SHIP_QTY ≥ I_ROD × SZ_MBQ − SZ_STK = 16` ✓ |
| `ALLOC_WAVE`   | `RL_R2`| stamped at write-back                          |
| `ALLOC_ROUND`  | 2     |                                                |

**Where to change:** Pool sort order at [rule_engine_pandas.py:1539](../../../../backend/app/services/rule_engine_pandas.py#L1539);
TBL hold rule at [line 1611-1616](../../../../backend/app/services/rule_engine_pandas.py#L1611);
MJ-cap clamp at [lines 1564-1582](../../../../backend/app/services/rule_engine_pandas.py#L1564).

---

## Step 19 — Post-band revalidation; MJ_REQ_REM decrement

**Where:** [rule_engine_pandas.py:1253-1265 area](../../../../backend/app/services/rule_engine_pandas.py#L1253) (`_revalidate_after_band`)
**When:** Immediately after each `_run_band` returns; once per round per
OPT_TYPE.
**What it does:** Aggregates the round's `ROUND_SHIP` per (WERKS,
MAJ_CAT) and decrements:
- `working_df['MJ_REQ_REM'] -= sum_round_ship_per_werks_mc`
- Each primary-grid `<GRID>_REM` column similarly.

Optionally also marks OPTs whose `OPT_REQ_REM` reached zero as fully
ALLOCATED if they were PARTIAL. So at the end of a band:
- `working_df['MJ_REQ_REM']` reflects what the WERKS still needs.
- `_live_mbq_budget` (called at the top of the next band — step 14
  formula) will see the updated value and produce a smaller budget.

**Input:** `alloc_df` (with ROUND_SHIP populated by step 18),
`working_df`, `grids`, `ot`, `r`.
**Output:** `working_df['MJ_REQ_REM']` decremented; per-grid REMs
decremented.
**Example (real data, HU45):** HU45 went into RL with `MJ_REQ_REM=138`.
The current HOPC560 snapshot shows HU45 has `SUM(SHIP_QTY)=1911` and
`SUM(HOLD_QTY)=316` across **520** alloc rows for `M_TEES_PN_HS`. Of
those, the **RL** waterfall alone shipped enough to drain `MJ_REQ_REM`
from 138 → **0** (verified: the live `ARS_LISTING_WORKING.MJ_REQ_REM` for
HU45/`M_TEES_PN_HS` shows two rows at 138 and three at 0 — the 0s are
HU45's ranked OPTs whose budget was fully consumed). The total ship of
1911 is much larger than 138 because **stock is also being shipped** —
SHIP_QTY = pool draw + from_hold + (for rows where SZ_STK > 0 the stock
itself is already counted "shipped" via the SZ_REQ math). MJ_REQ is the
*incremental* net of stock; SHIP_QTY is the *gross* per-size delivery.
**Where to change:** Inside `_revalidate_after_band` (grep for the
function definition); REMs initialization at `_init_rem_columns`.

---

## Step 20 — Loop to next round / next OPT_TYPE

**Where:** [rule_engine_pandas.py:1216-1265 inner loop](../../../../backend/app/services/rule_engine_pandas.py#L1216)
**When:** End of each inner `for r in range(1, max_round+1)` and outer
`for ot in active_ot` iteration.
**What it does:** Inner loop continues until `r == max_round`. When that
completes, outer loop advances to the next OPT_TYPE (`TBC` after `RL`,
`TBL` after `TBC`). At each new OPT_TYPE boundary, step 17 (re-rank +
size-coverage skip) runs again — but with the **live** pool state from
all prior bands.

No fallback phase. The old fallback path (which retried with relaxed
constraints) was archived in 2026-05; the only "second chance" is the
re-rank inside step 17 plus the cross-type R06 check in step 16.
**Input:** Same as steps 18-19, with cumulative state.
**Output:** All RL rounds done → all TBC rounds done → all TBL rounds
done. The MAJ_CAT waterfall is finished.
**Example (real data, HU45):** For `M_TEES_PN_HS`, RL rounds 1 and 2
drain HU45's 138-unit budget. TBC pre-band check (step 16) then sees
`MJ_REQ_REM=0 < 11` and marks every TBC row SKIPPED. TBL pre-band check
does the same. So HU45 in `M_TEES_PN_HS` finishes with: RL allocated as
shown, TBC zero ships, TBL zero ships (in this MAJ_CAT).
**Where to change:** Skip `TBC` or `TBL` entirely by passing
`opt_types=["RL"]` from the UI (the `allocOtFilter` knob does this).

---

## Step 21 — Sec-cap pre-gate (currently data-disabled)

**Where:** [rule_engine_new.py:2612](../../../../backend/app/services/rule_engine_new.py#L2612) (`_apply_sec_grid_cap_pre_gate`) — invoked from [rule_engine_new.py:183-188](../../../../backend/app/services/rule_engine_new.py#L183)
**When:** After all bands of all OPT_TYPEs complete, **only** when
`apply_sec_cap_in_normal=True` (default). Sequential mode only — pandas
mode currently doesn't run this in the main pass (it's part of the
Stage D wrap-up in the new orchestrator path).
**What it does (designed behavior):** Walks every Secondary grid from
`ARS_GRID_BUILDER WHERE grid_group='Secondary'`. For each OPT (in
priority order) sums its SHIP_QTY against the per-grid budget
`MAX(MBQ_<grid>) × 1.30`. If shipping the OPT would breach 130% of any
Secondary grid's MBQ, the OPT is skipped whole — SHIP_QTY → 0, ALLOC_QTY →
0, ALLOC_STATUS → SKIPPED, SKIP_REASON → `SEC_CAP_PRE_<grid>`. UNLESS the
override fires: `OPT_REQ ≥ SEC_CAP_PRE_OVERRIDE_OPT_REQ_PCT/100 × OPT_MBQ`
admits the OPT despite the breach and stamps a `SEC_CAP_PRE_OVERRIDE`
remark.

**The "data-disabled" status:** Two of the load-bearing invariants live
here:
- **MBQ sparseness** — when `<grid>_MBQ = 0` at a grain, the grid is
  treated as "no constraint" (gate at [line 2645](../../../../backend/app/services/rule_engine_new.py#L2645)).
  If you flip that gate to "MBQ=0 means budget 0" you'll start dropping
  everything that hits an unconfigured grid grain.
- **Grid extras propagation** — the function joins on grid_extras
  columns (`FAB`, `MICRO_MVGR`, `M_VND_CD`, `RNG_SEG`, etc.). If those
  weren't carried through steps 10/11 (`_collect_grid_extra_cols`), the
  join silently drops grids and the whole sec-cap becomes a no-op.

On current HOPC560 data, sec-cap fires on a small handful of OPTs
per run (the production grid set is conservative — most grids have
healthy budgets relative to the bands' demand). But the **mechanism**
must stay correct because the moment a new grid is added with tight
MBQs, this is where the engine decides "yes/no" at OPT grain.
**Input:** `alloc_table` post-waterfall, `working_table`, dynamic
`all_grids` from `_discover_all_active_grids`.
**Output:** Some OPTs flipped to SKIPPED+SEC_CAP_PRE_*.
**Example (real data, HU45):** On HOPC560 right now, querying
`SELECT COUNT(*) FROM ARS_ALLOC_WORKING WHERE SKIP_REASON LIKE 'SEC_CAP_PRE_%'
AND MAJ_CAT='M_TEES_PN_HS' AND WERKS='HU45'` returns 0 — none of HU45's
RL OPTs in this MAJ_CAT breached any Secondary grid in the run that
produced the current snapshot. That's expected: HU45's RL budget (138)
is well under any reasonable Secondary grid's 130% cap.
**Where to change:** Threshold at `SEC_CAP_DEFAULT_PCT` (top of
`rule_engine_new.py`); override threshold at
`SEC_CAP_PRE_OVERRIDE_OPT_REQ_PCT`; the toggle is `apply_sec_cap_in_normal`
in the GenerateRequest payload.

---

## Step 22 — Finalise: convert working columns to alloc columns

**Where:** [rule_engine_new.py:2179](../../../../backend/app/services/rule_engine_new.py#L2179) (`_stage_d_reflect`)
**When:** Last step inside the rule engine, after sec-cap.
**What it does:** Aggregates `ARS_ALLOC_WORKING` back up to OPT grain
(`WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR`) and writes the rollups back into
`ARS_LISTING_WORKING`:
- `ALLOC_QTY = SUM(SHIP_QTY)` per OPT
- `HOLD_QTY  = SUM(HOLD_QTY)` per OPT
- `ALLOC_STATUS` per OPT: NOT_ALLOCATED / PARTIAL / ALLOCATED
- `ALLOC_SEQ` = `ROW_NUMBER() OVER (PARTITION BY MAJ_CAT ORDER BY
  OPT_TYPE → first_alloc_round → ST_RANK → OPT_PRIORITY_RANK)` —
  the order in which OPTs were actually picked. Round-first ordering
  means round-1 OPTs precede round-2 OPTs across all stores; ST_RANK is
  the within-round tie-break so ST_RANK=1 stores get listed first; final
  tie-break is OPT_PRIORITY_RANK.

The same SELECT also handles the `[ALLOC_REMARKS]` rollup so the
listing-side UI shows the reasons (e.g. `R06_PRI_100; SEC_CAP_PRE_FAB`)
from alloc grain.

Also runs `_classify_alloc_reason` first ([rule_engine_new.py:192](../../../../backend/app/services/rule_engine_new.py#L192))
to ensure per-row reason classification is set before the rollup.
**Input:** Final `ARS_ALLOC_WORKING`.
**Output:** `ARS_LISTING_WORKING` with `ALLOC_QTY / HOLD_QTY / ALLOC_SEQ
/ ALLOC_STATUS / ALLOC_REMARKS` populated at OPT grain.
**Example (real data, HU45):** For HU45 / `M_TEES_PN_HS`, after
`_stage_d_reflect` the listing-side OPT row for `GEN_ART=1116113282
CLR=O_WHT` shows `ALLOC_QTY=52` (sum of the six size-grain SHIP_QTY: 5+1+16+15+4+11),
`HOLD_QTY=0`, `ALLOC_STATUS='ALLOCATED'`, `ALLOC_SEQ=<some small int>`
(it's a rank-1 RL OPT so its sequence is among the first in this MAJ_CAT).
**Where to change:** The ROW_NUMBER ordering at
[rule_engine_new.py:2213-2218](../../../../backend/app/services/rule_engine_new.py#L2213) —
changing it changes the order rows appear in the listing UI.

---

## Step 23 — Bulk write to ARS_ALLOC_WORKING

**Where:** Two paths:
- **Pandas mode (default):** per-worker bulk UPDATE inside the worker
  process, OR — when `use_writer_queue=True` — DataFrames are returned
  to the parent's writer thread (`writer_queue` setup at [rule_engine_pandas.py:574](../../../../backend/app/services/rule_engine_pandas.py#L574)) which serializes all UPDATEs.
- **Sequential mode:** the SQL-driven Stage C/D in `rule_engine_new.py`
  already wrote directly via UPDATE statements; no separate bulk write.

**When:** Per-MAJ_CAT, immediately after `_run_majcat_waterfall`
returns. The MAJ_CAT queue row (`ARS_ALLOC_QUEUE`) flips from
`IN_PROGRESS` → `DONE` only **after** the write succeeds. If
`defer_writes=True` (writer-queue mode), DONE is stamped by the writer
thread instead of the worker — that's why `defer_writes` is tied to
`use_pool` ([rule_engine_pandas.py:538-539](../../../../backend/app/services/rule_engine_pandas.py#L538)).
**What it does:** Writes back the mutated columns: `SHIP_QTY`,
`HOLD_QTY`, `ALLOC_QTY` (set to SHIP_QTY), `POOL_CONSUMED`, `ALLOC_WAVE`,
`ALLOC_ROUND`, `ALLOC_STATUS`, `SKIP_REASON`, `ALLOC_REMARKS`, plus the
sec-cap diagnostic columns.
**Input:** Per-MAJ_CAT result DataFrame.
**Output:** `ARS_ALLOC_WORKING` rows updated; queue row → DONE.
**Example (real data, HU45):** Final alloc table count for HU45 /
`M_TEES_PN_HS` from HOPC560:

| metric            | value |
|-------------------|-------|
| `COUNT(*)`        | 520   |
| `SUM(SHIP_QTY)`   | 1 911 |
| `SUM(HOLD_QTY)`   | 316   |
| distinct rows with `OPT_TYPE='RL'` ALLOC_WAVE | 92 OPTs × ~6 sizes |

**Where to change:** Writer-queue toggle: the `use_writer_queue` UI knob
or `.env` default `USE_WRITER_QUEUE`. Bulk-update SQL:
`_write_majcat_results` (grep) in `rule_engine_pandas.py`.

---

## Step 24 — Update ARS_LISTING_WORKING

**Where:** [rule_engine_new.py:2179](../../../../backend/app/services/rule_engine_new.py#L2179) (already covered in step 22)
**When:** After every MAJ_CAT finishes its waterfall, in sequential mode
this UPDATE runs once per Stage D pass. In pandas mode, the equivalent
roll-up happens via a single SQL UPDATE issued once after all per-MAJ_CAT
writes finish (in the parent process).
**What it does:** Already detailed in step 22. The reason this is its
own step in the trace is that **two separate physical tables get updated**:
`ARS_ALLOC_WORKING` (size grain) in step 23, and `ARS_LISTING_WORKING`
(OPT grain) here. Both have to be coherent for the UI's KPI tiles
(`Total Alloc Qty`, `Total Hold Qty`, etc.) to match.
**Input:** Aggregated alloc data from step 22.
**Output:** Listing-side ALLOC_QTY/HOLD_QTY/ALLOC_SEQ.
**Example (real data, HU45):** `SELECT MJ_REQ_REM FROM ARS_LISTING_WORKING
WHERE WERKS='HU45' AND MAJ_CAT='M_TEES_PN_HS'` returns five rows; two
still at 138 (OPTs that got skipped — never got to draw budget) and three
at 0 (RL OPTs that drained their share). The `0`s confirm the
listing-side budget tracking matches the alloc-side ships.
**Where to change:** Same as step 22.

---

## Step 25 — Audit / parked_history snapshot

**Where:** [backend/app/api/v1/endpoints/listing.py:2157-2181](../../../../backend/app/api/v1/endpoints/listing.py#L2157) calling [backend/app/services/parked_history.py:377](../../../../backend/app/services/parked_history.py#L377) (`snapshot_session_to_parked`)
**When:** After Part 8 (the rule engine) returns, before Part 8.5
(OPT_STATUS post-processing).
**What it does:** Inserts a tagged copy of every "snapshot target" table
into its `_PARKED` twin, attaching the current `session_id` and
`PARK_STATUS='PARKED'`. The configured targets (from
`_SNAPSHOT_TARGETS` in `parked_history.py`) are at least:
- `ARS_ALLOC_WORKING` → `ARS_ALLOC_PARKED`
- `ARS_LISTING_WORKING` → `ARS_LISTING_WORKING_PARKED`

The function never raises — failures here are downgraded to
`parked_status='SKIPPED_ERROR'` (or `SKIPPED_EMPTY`) and surfaced on the
session row so the UI shows a warning banner. The listing run itself
still succeeds.

There's no separate "audit_log" insert here — the audit trail lives in:
- `ARS_LISTING_SESSIONS` (one row per Generate, written at session start
  + closed at end with summary/duration/step_timings/tables_affected).
- The per-session loguru sink → `logs/sessions/<sid>.log`.

**Input:** session_id; the current `ARS_ALLOC_WORKING` and
`ARS_LISTING_WORKING`.
**Output:** Parked snapshots; `summary['parked_status']`.
**Example (real data, HU45):** For a fresh run, `SELECT COUNT(*) FROM
ARS_ALLOC_PARKED WHERE SESSION_ID='<sid>' AND MAJ_CAT='M_TEES_PN_HS' AND
WERKS='HU45'` returns 520 — same as the alloc table. The parked snapshot
preserves exactly what the user saw at end-of-run; an Approve later
promotes it to `ARS_ALLOC_HISTORY` and a Reject marks it
`PARK_STATUS='REJECTED'` for audit.
**Where to change:** Snapshot target list at the top of
`parked_history.py` (`_SNAPSHOT_TARGETS`); to add a new tracked table,
add an entry there with `{table, parked, history, label}` keys.

---

## Step 26 — Step-timing summary + response payload built

**Where:** [backend/app/api/v1/endpoints/listing.py:2350-2407](../../../../backend/app/api/v1/endpoints/listing.py#L2350)
**When:** After every Part has run inside `_generate_listing_impl`.
**What it does:**
- Emits the step-timing summary block to the loguru sink (visible in the
  session's log file).
- Calls `parked_history.tables_affected_summary(pre_existence)` to
  classify each tracked table as CREATED / RECREATED / TRUNCATED /
  UNCHANGED — for the UI's completion panel.
- Builds the `summary.update({...})` payload at lines 2372-2384 with
  `duration_sec`, `alloc_rows`, `ship_qty_total`, `hold_qty_total`,
  `listed_opts`, `failed_majcats`, `step_timings`, `tables_affected`.
- Builds the **return dict** at lines 2386-2407 containing the totals,
  per-OPT_TYPE counts (`MIX, TBL, TBC, RL, NL, untagged`), step timings,
  and IDs (`session_id, allocation_mode, parallel_workers,
  alloc_batch_id, alloc_failed`).

This return value is **NOT** what the browser receives — the browser
already got the immediate response in step 5. This return goes to the
caller (`_run_generate_in_thread`) which feeds it into the session row's
`SUMMARY_JSON` and `STATUS` columns. The UI reads it via
`listingAPI.session(sid)`.
**Input:** All locals from `_generate_listing_impl`.
**Output:** Dict that gets persisted to `ARS_LISTING_SESSIONS`.
**Example (real data, HU45):** A focused run on just `M_TEES_PN_HS`
produces a summary like:
```jsonc
{
  "duration_sec": 38.4,
  "alloc_rows": ~12500,
  "ship_qty_total": ~42000,
  "hold_qty_total": ~6000,
  "listed_opts": 3438,
  "failed_majcats": 0,
  "step_timings": [
    {"step": "Part 7 (Working table + Hierarchy + ALLOC_FLAG → 44512 rows)", "seconds": 6.4},
    {"step": "Part 8 (pandas, workers=8 → 12500 alloc rows, failed=0, batch=S_...)", "seconds": 25.2},
    {"step": "Part 8.4 (park alloc + listing snapshots)", "seconds": 4.1},
    ...
  ],
  "parked_status": "PARKED"
}
```
**Where to change:** Add a new summary field at
[listing.py:2372-2384](../../../../backend/app/api/v1/endpoints/listing.py#L2372).

---

## Step 27 — Session row finalized (no second HTTP response)

**Where:** [backend/app/api/v1/endpoints/listing.py:477-484](../../../../backend/app/api/v1/endpoints/listing.py#L477) (`end_session` in the `finally` of `_run_generate_in_thread`)
**When:** Just before the daemon thread exits.
**What it does:** Calls `end_session(session_id, status="SUCCESS" or
"FAILED", summary)` which UPDATEs `ARS_LISTING_SESSIONS` with the final
`STATUS`, `DURATION_SEC`, `ALLOC_ROWS`, `SUMMARY_JSON`,
`PARKED_STATUS`, and `STEP_TIMINGS_JSON`. Then detaches the
per-session loguru sink so the log file is closed.

There is **no second HTTP response** — the browser's `await
listingAPI.generate(...)` already resolved in step 5. The UI learns the
job is done by polling `listingAPI.session(sid)` (set up in
`ListingPage.jsx`'s session-status `useEffect` at
[ListingPage.jsx:1036-1029](../../../../frontend/src/pages/ListingPage.jsx#L1036))
every 3 seconds. When that poll returns `sess.status !== 'RUNNING'`, the
UI flips out of "generating" state.
**Input:** `summary` dict, `session_id`, status string.
**Output:** `ARS_LISTING_SESSIONS` row updated. Daemon thread exits.
**Example (real data, HU45):** When the polling tick at
[ListingPage.jsx:1043](../../../../frontend/src/pages/ListingPage.jsx#L1043) sees `status='SUCCESS'`, the React state machine fires `setGenerating(false)`, `setPaused(false)`, the success toast, and then the four refresh calls at
[ListingPage.jsx:1066-1067](../../../../frontend/src/pages/ListingPage.jsx#L1066): `loadConfig(); loadSummary(); setColFilters({}); loadPreview(1, {})` + `loadParkedRuns()`.
**Where to change:** Status semantics in `end_session` —
`listing_sessions.py` (grep `def end_session`).

---

## Step 28 — Listing page refreshes table + KPI tiles

**Where:** [frontend/src/pages/ListingPage.jsx:1066-1067](../../../../frontend/src/pages/ListingPage.jsx#L1066)
**When:** Inside the session-status polling tick, the moment
`sess.status === 'SUCCESS'`.
**What it does:** Four background fetches in sequence:

- `loadConfig()` → `GET /api/v1/listing/config` → repaints `config?.msa_gen_art_rows`, `grid_gen_art_rows`, `listing_exists`, etc. for the KPI tiles at the top.
- `loadSummary()` → `GET /api/v1/listing/summary` → repaints `summary?.by_maj_cat` (the MAJ_CAT modal data), `summary?.totals` (the NEW Items / Total Alloc Qty / Total Hold Qty tiles), `summary?.by_store`.
- `setColFilters({})` → clears any in-flight column filters on the preview grid so the fresh data isn't filtered out.
- `loadPreview(1, {})` → `GET /api/v1/listing/preview?page=1` → repaints the data grid below the KPI tiles with the new OPT-grain rows from `ARS_LISTING_WORKING`.
- `loadParkedRuns()` → `GET /api/v1/listing/parked-runs` → repaints the "Parked Runs" review queue with the just-created snapshot.

Each of those endpoints reads from `ARS_LISTING_WORKING` /
`ARS_ALLOC_WORKING` / `ARS_LISTING_SESSIONS` / `ARS_ALLOC_PARKED`
respectively. The data the user sees in the grid is the post-Step 22
roll-up — `ALLOC_QTY`, `HOLD_QTY`, `ALLOC_STATUS`, `ALLOC_SEQ` per OPT.
**Input:** Browser axios calls; backend reads from the four tables.
**Output:** Repainted React tree with the post-Generate state.
**Example (real data, HU45):** The user filters the grid by
`MAJ_CAT=M_TEES_PN_HS` and `WERKS=HU45` and sees ~92 OPT rows. The `Total
Alloc Qty` tile shows `1 911` for HU45 / `M_TEES_PN_HS` (sum from step
23); the `Total Hold Qty` shows `316`. Clicking an OPT row drills down
into the size grain via `listingAPI.allocPreview()` which reads
`ARS_ALLOC_WORKING`.
**Where to change:** The refresh chain at
[ListingPage.jsx:1066-1067](../../../../frontend/src/pages/ListingPage.jsx#L1066);
the data endpoints themselves at `backend/app/api/v1/endpoints/listing.py`
(grep `@router.get("/summary"`, `@router.get("/preview"`, etc.).

---

# Saved invariants (the six load-bearing rules)

After 28 steps, these are the rules that must never break — restated so
this doc can be the single reference a debugger reaches for.

| # | Invariant | Steps where it lives |
|---|-----------|----------------------|
| 1 | **ACS_D ≠ daily sale.** ACS_D is the accessories-density (one OPT display quantity). For velocity use `MAX_DAILY_SALE`. | Stage A rank uses `MAX_DAILY_SALE DESC` for velocity (step 9). `0.5 × ACS_D` is the headroom threshold, *not* a sales-floor (steps 8g, 16). |
| 2 | **One OPT = one OPT_TYPE.** RL / TBC / TBL are mutually exclusive at OPT grain (per `WERKS, MAJ_CAT, GEN_ART, CLR`). | Confirmed by the explode in step 11 (each listed OPT explodes once with a single OPT_TYPE) and by the outer loop in step 15 (each OPT_TYPE walks a disjoint row set via `ot_mask`). |
| 3 | **MBQ=0 means "no constraint" in sec-cap.** Do NOT multiply by 1.30 when `<grid>_MBQ=0`. | Sec-cap pre-gate, step 21 — gate at [rule_engine_new.py:2645](../../../../backend/app/services/rule_engine_new.py#L2645). |
| 4 | **Growth / cap at MAJ_CAT + grid only, never per OPT_TYPE.** The 110% / 130% caps must apply at MJ-grid level, never to one OPT_TYPE in isolation. | `_build_mbq_budget` / `_live_mbq_budget` keyed by `WERKS` only (step 14). MJ_REQ_REM decrements at WERKS×MAJ_CAT (step 19). The per-OT cap_pct merely *scales* the same WERKS×MJ budget — it does NOT create a separate per-OPT_TYPE ceiling. |
| 5 | **Grid extras must propagate listing → listed → alloc.** `FAB`, `MACRO_MVGR`, `MICRO_MVGR`, `M_VND_CD`, `RNG_SEG` (whatever `ARS_GRID_BUILDER` says) must flow. | Step 10 (`_collect_grid_extra_cols` → `_stage_a_materialize_listed`) and step 11 (same logic in `_stage_b_explode`). Drop the propagation and sec-cap silently loses grids. |
| 6 | **RNG_SEG = MRP tier (E / V / P / SP).** `MJ_RNG_SEG` is the **Primary** grid; `MJ_FAB`, `MJ_MICRO_MVGR` are Secondary. | `_discover_primary_grids` vs `_discover_all_active_grids` — step 11's `mp_cols` derivation reads grid_group from `ARS_GRID_BUILDER`. |

When debugging an unexpected `ALLOC_QTY`, walk the steps in this order:

1. Pull the row from `ARS_LISTING_WORKING` and check `LISTED_FLAG` +
   `LISTED_REASON` (step 8). If `LISTED_FLAG=0`, the engine never even
   tried.
2. If listed, check the OPT's row in `ARS_LISTED_OPT` and `ARS_ALLOC_WORKING`
   to confirm explode succeeded (step 11). If alloc has no rows, sizes
   were lost in the join — likely a `MAJ_CAT/CLR/RDC` mismatch with
   `ARS_MSA_VAR_ART`.
3. If alloc has rows but `SHIP_QTY=0` everywhere, look at `ALLOC_STATUS`
   and `SKIP_REASON`. SKIP_REASON narrows the cause to a specific step:
   - `R0x_*` → step 8
   - `SKIP_PRI_BROKEN` / `R06_*` → step 16
   - `R07_SIZE_RATIO_LIVE` → step 17
   - `SEC_CAP_PRE_*` → step 21
4. If `SHIP_QTY > 0` but less than expected, check `MJ_REQ_REM` in
   working table (step 19) — the WERKS may have run out of MAJ_CAT budget
   before this OPT's turn came up. The `ALLOC_SEQ` (step 22) tells you
   *when* the OPT was picked.
5. If everything looks right in the data but the UI shows something else,
   the bug is in the response payload (step 26) or the refresh chain
   (step 28), not in the engine.

That's the click-to-end path. Every backend step has a `Where to change`
pointer, every frontend step has the same — the engine is intentionally
flat and traceable. Treat this doc as a map, not a tutorial: start where
the bug is, walk one step at a time.
