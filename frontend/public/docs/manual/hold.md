# Hold Process & Manage — ARS Manual

## BRD — Why this exists

New-launch and to-be-launched merchandise cannot be shipped to stores at full velocity on day one. When ARS allocates a **NL** (New Launch) or **TBL** (To-Be-Launched) option, part of the calculated quantity is deliberately **held back at the warehouse** instead of being shipped. That reserved quantity is the store's ramp-up buffer: it is earmarked for that store × variant × size, but only released into shipping over the ramp-up window as real sell-through justifies it. Without the hold, a single first allocation would flush the whole launch buy into stores that have no sales history to size against.

The **Hold Tracking** table is the ledger of that reserved stock. Category and replenishment planners use the **Hold Dashboard** to see how much stock is sitting on hold, how old it is, which stores/articles/RDCs it belongs to, and to **release** (clear) or **top-up** (revise) holds by hand when the ramp-up plan changes.

In the pipeline, hold sits *downstream of allocation and alongside pending*. Held quantity is subtracted from available stock in MSA exactly like pending is: `FNL_Q = max(STK − PEND − HOLD, 0)`. So every unit on hold is a unit the next allocation run will *not* re-offer — which is the whole point.

Who uses it: replenishment/category planners (dashboard + clear/revise), and the allocation engine (reads `HOLD_REM` to net down availability).

## FSD — How it works

### Inputs & outputs

| Table | Role | Grain |
|---|---|---|
| `ARS_ALLOC_HISTORY` | Source of hold changes at approve time | one row per allocation line (SESSION_ID, WERKS, VAR_ART, SZ, OPT_TYPE …) |
| `ARS_ALLOC_RDC_SPLIT_HISTORY` (else `ARS_ALLOC_RDC_SPLIT_PARKED`) | Split rows carrying the hold on an All-RDCs line — first source of the hold's `SRC_RDC` | split rows per source warehouse |
| `ARS_NL_TBL_HOLD_TRACKING` | The hold ledger (created/consumed here) | `WERKS × VAR_ART × SZ × ALLOC_TYPE` |
| `Master_ALC_INPUT_ST_MASTER` | Maps store `WERKS` → the store's own `RDC` (fills the tracker's `RDC`; last-resort fallback for `SRC_RDC`) | one row per store |
| `ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT` + `ARS_NL_TBL_HOLD_SNAPSHOT_SESSIONS` | Pre-approve snapshot for revert (carries `SRC_RDC`) | scoped to keys touched by the session |
| `ARS_MSA_TOTAL / VAR_ART / GEN_ART` | Consume `HOLD_REM` into `HOLD_QTY` → `FNL_Q`, at the hold's `SRC_RDC` | (RDC, ARTICLE) |
| `ARS_MSA_VAR_ART.HOLD_QTY` | Reconciliation target | (RDC, VAR_ART) |
| `ARS_PEND_ALC_OPERATIONS` | Audit of every clear/revise | one row per op |

### Rules & invariants

- **Grain is `(WERKS, VAR_ART, SZ, ALLOC_TYPE)`** (typed pools, Jul 2026). A single 3-part key can hold BOTH a legacy row (`ALLOC_TYPE=''`) and a typed row (`FRESH`/`GRT`). One session = one type, so `MAX(ALLOC_TYPE)` per key is safe.
- **`SRC_RDC` reserves, `RDC` describes.** `SRC_RDC` = the warehouse the hold's pieces are reserved at (drives the MSA `HOLD_QTY` deduction); `RDC` = the destination store's own warehouse (store master, descriptive only). Every reader resolves the warehouse as `COALESCE(NULLIF(SRC_RDC,''), NULLIF(RDC,''), store master RDC)` (`pend_alc_service.hold_src_rdc_sql` / `msa_service._hold_src_expr`); the fallback is exact for rows written before `SRC_RDC` (their `RDC` held the reserving warehouse).
- **One tracker row = one warehouse.** The PK has no warehouse column, so a row can name only one `SRC_RDC`. An **open** row (`IS_CLOSED=0, HOLD_REM>0`) keeps its existing warehouse when a later session adds to it — its pieces are physically there; a closed row being re-opened takes the session's `SRC_RDC`.
- **`HOLD_REM = 0` ⇒ `IS_CLOSED = 1`**. A closed row is inert; a re-hold (Step B MERGE, or revise) re-opens it with a fresh initial.
- **`ALLOC_TYPE` propagation**: sec-cap grid extras (FAB/MACRO_MVGR/MICRO_MVGR/M_VND_CD/RNG_SEG) are NOT stored on the tracker — the tracker is keyed on physical WERKS/VAR_ART/SZ only. Sec-cap context lives upstream in listing/alloc; do not attempt to reconstruct it from the tracker.
- **DO upload does NOT release hold** (decoupled by product decision). `HOLD_REM` stays at its post-listing value regardless of DO shipping. Hold is released only by Step A consumption (next allocation's RL/TBC draw) or by an explicit **Clear Hold**.
- **`ARS_NL_TBL_HOLD_TRACKING` carries no session/run metadata.** Session attribution for revert lives entirely in the two snapshot tables.
- **Blank `SZ` in a clear/revise request matches EVERY size** for that `(WERKS, VAR_ART)`.
- **Reason is mandatory** on every clear and revise (400 otherwise).

### Lifecycle & operations — how and why

The tracker's authoritative lifecycle is `active → held (HOLD_REM>0) → consumed/released (IS_CLOSED=1) | reverted`. Every transition is either an approve-time write, a snapshot/revert, or an adhoc dashboard action.

**1. Create / consume hold at APPROVE (`parked_history._apply_hold_tracking_from_history`)**
- *Why*: hold must commit at the same lifecycle event as PEND — i.e. when a listing session is Approved — not during Generate. This keeps hold and pending in lock-step so MSA sees a consistent netting.
- *How*: reads `ARS_ALLOC_HISTORY` for the approved `SESSION_ID`.
  - **Step A** — RL/TBC alloc lines *consumed* existing warehouse hold: decrement `HOLD_REM` by `FROM_HOLD_QTY`. Drains **legacy-first**: pass 1 drains the `ALLOC_TYPE=''` row up to the consumed qty; pass 2 applies the remainder (`consumed − legacy absorbed`) to the run's own typed row. Hits 0 ⇒ `IS_CLOSED=1`, stamp `CLOSED_DATE`.
  - **Step B** — TBL alloc lines *created* new hold: `MERGE` on `(WERKS, VAR_ART, SZ, ALLOC_TYPE)`. `RDC` = store master. `SRC_RDC` = the split rows carrying the hold (`ARS_ALLOC_RDC_SPLIT_HISTORY`, else `_PARKED`) → `ARS_ALLOC_HISTORY.SRC_RDC` unless `MULTI` → the run RDC → store master. Matched+closed ⇒ re-open with `R.hold_qty` as new initial and the session's `SRC_RDC`; matched+open ⇒ accumulate and **keep** the row's existing `SRC_RDC`; not matched ⇒ insert with `OPT_STATUS='TBL'`.
- *Validate-before-create gate*: **snapshot first.** Before Step A/B mutate the tracker, `approve_parked` writes a **scoped** pre-approve snapshot (only `(WERKS, VAR_ART, SZ)` keys appearing in this session's `ARS_ALLOC_HISTORY`) into `ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT`, plus a session-marker row in `..._SESSIONS`. The snapshot is **idempotent** (skips if the session marker already exists) so a re-approve never clobbers the true pre-state. No snapshot ⇒ revert refuses to touch the table.

**2. Release / cancel hold — Clear Hold (`apply_hold_clear`, `POST /hold-dashboard/clear-hold[-file]`)**
- *Why*: a planner decides held stock should become shippable now (ramp-up finished early, or the launch was pulled) without waiting for the next allocation to consume it.
- *How*: per row, `release_qty` semantics —
  - omitted / `≥ HOLD_REM` → **full close** (`HOLD_REM=0, IS_CLOSED=1`).
  - `0 < release_qty < HOLD_REM` → **partial release** (`HOLD_REM −= release_qty`).
  Then re-aggregates MSA `HOLD_QTY` from the tracker for the affected `(hold SRC_RDC, ARTICLE)` and recomputes `FNL_Q`, so the released qty is allocatable on the next run without a full MSA rebuild. File variant accepts CSV/Excel with `WERKS, VAR_ART, [SZ], [RELEASE_QTY]`.
- *Validate-before-create gate*: **reason required** (400 if blank); `rows` non-empty. Logged to `ARS_PEND_ALC_OPERATIONS` as `OP_TYPE='HOLD_CLEAR'` with per-row pre-images (`old_hold_rem`) so the op is revertable.

**3. Top-up / re-open hold — Revise Hold (`apply_hold_revise`, `POST /hold-dashboard/revise-hold[-file]`)**
- *Why*: planner needs to reserve *more* than allocation held, or re-hold a row that was released too early.
- *How*: increases both `HOLD_REM` and `HOLD_QTY_INITIAL` by `add_qty` on every matching tracker row; a **closed row is re-opened** with `add_qty` as its new initial. Re-syncs MSA `HOLD_QTY/FNL_Q` for the affected `(hold SRC_RDC, ARTICLE)`. File variant: `WERKS, VAR_ART, ADD_QTY, [SZ]`.
- *Validate-before-create gate*: **reason required**; `add_qty > 0` (rows with `add_qty ≤ 0` are dropped). Logged as `OP_TYPE='HOLD_REVISE'`.

**4. Park / Unpark (revert) — `reject_parked` → `_revert_hold_tracking`**
- *Why*: an approved session is rejected/reverted ("approved by mistake"); the tracker must return to its exact pre-approval state.
- *How*: only runs if a snapshot session-marker exists (`snapped>0`). Then, scoped to keys touched by this session's `ARS_ALLOC_HISTORY`: (1) **delete** rows this run inserted (present live, absent in snapshot); (2) **restore** pre-existing rows to snapshot values, `SRC_RDC` included; (3) clean up this session's snapshot rows.
- *Validate-before-create gate*: **no snapshot ⇒ no-op** (session predates the feature; the table is left untouched rather than wiped). The touched-key scope prevents a scoped snapshot from wiping untouched rows.

**5. Hold Dashboard — read-only views (`hold_dashboard.py`)**
- `GET /summary` (KPI cards: open/closed rows & qty, consumed, distinct stores/articles/SKUs, oldest open days), `/by-store`, `/by-rdc` (joins store master for WERKS→RDC), `/by-article`, `/by-status` (OPT_STATUS: NL/TBL/…), `/by-age` (0-7…90+ day buckets on `LISTED_DATE`), `/timeline` (daily created vs closed), `/detail` (paginated, filterable), `/detail/export` (streamed CSV).
- **Reconciliation (`GET /reconciliation`)**: cross-checks three numbers — tracker open `SUM(HOLD_REM)` where `IS_CLOSED=0`, last run's `SUM(HOLD_QTY)` in `ARS_ALLOC_WORKING`, and `SUM(HOLD_QTY)` in `ARS_MSA_VAR_ART`. `tracker_vs_msa_drift = tracker_open − msa_hold` should be ~0 once MSA Step 6.5 has run on the latest sequence; non-zero drift flags a tracker/MSA sync gap to investigate.

### Formulas

```
# MSA netting (Step 9) — hold reduces available stock exactly like pending
FNL_Q = max(STK_QTY - PEND_QTY - HOLD_QTY, 0)

# Step A (approve): RL/TBC consume hold, legacy-first
pass1: HOLD_REM('')     -= min(from_hold_qty, HOLD_REM(''))
pass2: HOLD_REM(typed)  -= max(from_hold_qty - legacy_open, 0)
IS_CLOSED = 1, CLOSED_DATE = now   when HOLD_REM <= 0

# Step B (approve): TBL create hold (MERGE)
R.RDC     = store master RDC
R.SRC_RDC = split rows carrying the hold (history, else parked)
            → ARS_ALLOC_HISTORY.SRC_RDC unless 'MULTI' → run RDC → store master
matched & closed : HOLD_QTY_INITIAL = HOLD_REM = R.hold_qty ; IS_CLOSED = 0 ; SRC_RDC = R.SRC_RDC
matched & open   : HOLD_QTY_INITIAL += R.hold_qty ; HOLD_REM += R.hold_qty   (SRC_RDC kept)
not matched      : INSERT (…, RDC=R.RDC, SRC_RDC=R.SRC_RDC, HOLD_QTY_INITIAL=R.hold_qty, HOLD_REM=R.hold_qty, OPT_STATUS='TBL', IS_CLOSED=0)

# Warehouse every reader deducts the hold from (MSA, alloc_pool, re-syncs)
hold_wh = COALESCE(NULLIF(SRC_RDC,''), NULLIF(RDC,''), store master RDC)

# Clear Hold
full close   : HOLD_REM = 0, IS_CLOSED = 1
partial      : HOLD_REM -= release_qty          (0 < release_qty < HOLD_REM)

# Revise Hold
HOLD_REM += add_qty ; HOLD_QTY_INITIAL += add_qty   (re-opens if was closed)

# Dashboard consumed metric
consumed_qty = closed_initial + max(open_initial - open_qty, 0)
```

### Key columns

| Column | Meaning | Formula / source |
|---|---|---|
| `WERKS` | Destination store | from `ARS_ALLOC_HISTORY.WERKS` |
| `SRC_RDC` | Warehouse the hold is reserved at (drives MSA `HOLD_QTY`) | Step B: split rows (history, else parked) → `ARS_ALLOC_HISTORY.SRC_RDC` unless `MULTI` → run RDC → store master; kept on an open row, replaced on re-open |
| `RDC` | Destination store's own warehouse (descriptive only) | store master (`Master_ALC_INPUT_ST_MASTER`) via WERKS; stamped in Step B |
| `VAR_ART` / `SZ` / `CLR` | Variant / size / colour | alloc history |
| `MAJ_CAT`, `GEN_ART_NUMBER` | Category / generic article | alloc history |
| `OPT_STATUS` | Hold origin tag (`TBL`, NL, …) | set to `'TBL'` on Step B insert |
| `ALLOC_TYPE` | Typed pool (`FRESH`/`GRT`/`''`) | `MAX(ALLOC_TYPE)` of the session's alloc rows |
| `HOLD_QTY_INITIAL` | Qty first held (baseline) | Step B `hold_qty`; bumped by revise |
| `HOLD_REM` | Qty still on hold (drives MSA) | initial − consumed − released |
| `IS_CLOSED` | 1 when `HOLD_REM=0` | set by Step A / clear / re-set by re-hold |
| `LISTED_DATE` | Hold creation date (age basis) | `GETDATE()` on Step B insert |
| `CLOSED_DATE` | When it closed | `GETDATE()` when `HOLD_REM` hits 0 |
| `LAST_REMARKS`, `LAST_UPDATED_BY` | Adhoc audit (lazy cols) | added by clear/revise endpoints |

## Recorded rules
<!-- ars_flow appends dated bullets below. One rule per line. -->
- 2026-07-08 — `ARS_NL_TBL_HOLD_TRACKING` carries NO session/run metadata: PK is `(WERKS, VAR_ART, SZ, ALLOC_TYPE)` and non-qty cols are `RDC`, `OPT_STATUS`, dates, `IS_CLOSED`. Session attribution for revert lives entirely in `..._SNAPSHOT` + `..._SNAPSHOT_SESSIONS`. Why: any per-run tag cannot ride the tracking table without a schema change.
- 2026-07-09 — DO upload (`apply_do_deductions`) is decoupled from hold release: it never touches `ARS_NL_TBL_HOLD_TRACKING`. `HOLD_REM` (and therefore MSA `HOLD_QTY`, listing `RL_HOLD_QTY`, dashboard) over-states held qty until a next-run RL/TBC Step-A consumption or an explicit Clear Hold releases it. Why: hold = ramp-up reservation, independent of physical shipping.
- 2026-07-09 — Hold is created/consumed at APPROVE, not at Generate: `_apply_hold_tracking_from_history` reads `ARS_ALLOC_HISTORY` (post-promotion) for the session. Pre-approve snapshot is idempotent + scoped to touched keys; revert refuses if no snapshot marker exists. Why: symmetric with PEND write; safe against re-approve and scoped-snapshot wipe.
- 2026-10-14 — **Hold tracker: `RDC` = destination store's own warehouse, `SRC_RDC` = the warehouse the hold is reserved at** (`ARS_NL_TBL_HOLD_TRACKING` + `_SNAPSHOT`; code on branch `for_merge`, not yet deployed or migrated). Readers use `COALESCE(NULLIF(SRC_RDC,''), NULLIF(RDC,''), store master)` (`hold_src_rdc_sql` / `msa_service._hold_src_expr` / `alloc_pool.hold_agg_sql`) — exact for rows written before `SRC_RDC` (their `RDC` held the reserving warehouse) or by a server still on old code. Step B (`parked_history._apply_hold_tracking_from_history`): `RDC` = store master; `SRC_RDC` = split rows carrying the hold (history, else parked) → `ARS_ALLOC_HISTORY.SRC_RDC` unless `MULTI` → run RDC → store master. On MATCHED a hold still open (`IS_CLOSED=0, HOLD_REM>0`) **keeps** its warehouse (one tracker row = one warehouse; its pieces are physically there); a closed row being re-opened takes the session's `SRC_RDC`. Snapshot + revert carry `SRC_RDC`; hold clear / revise MSA re-sync, MSA Steps 8 / 12 / universe source C and `bootstrap_msa_hold_sync` key on hold `SRC_RDC` (the session-scoped re-sync now scopes by ARTICLE across all warehouses — it scoped by the store master's RDC and missed holds reserved at another warehouse). Report proc `ALLRDC_usp_ars_grid_report.sql`: HCLOSE by tracker `SRC_RDC` (fallback `RDC`); HADD by `ARS_ALLOC_HISTORY.SRC_RDC` unless `MULTI` (fallback `RDC`); HCONS stays on `RDC` (alloc history doesn't carry the consumed hold's warehouse) — the proc file is changed but **not yet deployed to the DB**. Reverses commit `8274ede` (2026-10-05), which had written the reserving warehouse into `RDC`. Rollout: `backend/scripts/041_src_rdc_concept.py` (see pendalc.md). Known limits: one hold row can name only one warehouse (PK `WERKS, VAR_ART, SZ, ALLOC_TYPE`); `ALLRDC_usp_ars_msa_master`'s `alloc_fh` still attributes FROM_HOLD consumption by store RDC. see also: msa.md, pendalc.md
