# Port checklist — DEV (`D:\ARS_PROD\ars_prod`) → ours (`D:\PROJECTS\ars_v3`)

Working tracker. The **why** and the worked examples live in [PORT_FROM_ARS_PROD.md](PORT_FROM_ARS_PROD.md); this file is what you tick off.

**Rule: one item at a time.** Port → run → compare → tick. Never batch two engine items.

```
[ ] not started     [~] in progress     [x] done + validated     [-] skipped (write why)
```

---

## Read this before item 1 — three facts that change the plan

**1. HOPC866 is shared, and DEV has already seeded rules into it.**

```
rule_key                   active   value              wired
ALC_CAP_LADDER             True     OFF                True
ALC_REV_MBQ_GROWTH_SRC     True     AUTO               True
LST_OPT_STATUS_STAMP       True     (null)             True
```

`ALC_CAP_LADDER` and `ALC_REV_MBQ_GROWTH_SRC` **do not exist in our seed** — DEV's code wrote them into the same database. They are inert for us today because our code never reads them, but they will become live the moment we port M02/M03. `ALC_REV_MBQ_GROWTH_SRC` is on **AUTO**; the Release Brief says it must be **FLAT** before any comparison.

**2. `LST_OPT_STATUS_STAMP` is already ON here.** So the "three blank screens" problem in *our* environment is **not** the rule — it is the missing archive column (P-03 below):

```
ARS_LISTING_PARKED has OPT_STATUS column: False
```

**3. Two things run the other way — do not lose them.**

| | Ours | DEV |
|---|---|---|
| **FS-11** `ALLOC_ROUND` stamp fix | ✅ have it | ❌ reverted it |
| **Dev Sync Manager** (6 files) | ✅ have it | ❌ deleted it |
| **Central RDC Pool** (M17) | ✅ ours | ❌ doesn't exist |

---

## Wave 0 — foundation (no behaviour change)

### `[ ] P-01 · Migration runner`
Everything below is applied through it.
- **Copy:** `backend/scripts/run_migrations.py`
- **Validate:** `python scripts/run_migrations.py --list` shows 027–036 as pending
- **Risk:** none

### `[ ] P-02 · Data Dictionary — OPT_MBQ formula written backwards`
> Dictionary says *"OPT_MBQ = sum of SZ_MBQ over all its sizes."*
> Truth: `OPT_MBQ = ROUND(ACS_D + rate × ALC_D, 0)` — `SZ_MBQ` is produced by **splitting** OPT_MBQ across sizes.
> A 100-pc target: old text claims `15+30+30+15+10 → 100`; reality is `100 → S 15, M 30, L 30, XL 15, XXL 10`. Both give 100, so it looked harmless — but anyone troubleshooting searches in the wrong place.

- **Apply:** migrations `034`, `036`
- **Validate:** Settings → Data Dictionary → `OPT_MBQ` shows the new formula
- **Risk:** none · **Depends:** P-01

---

## Wave 1 — the instruments (port these *before* the engine)

### `[ ] P-03 · OPT_STATUS on the archive tables`
**Verified:** `ARS_LISTING_PARKED` has **no** `OPT_STATUS` column here.
> Open last Tuesday's run and ask what an option ended as — never saved. Review has to re-derive it.

- **Apply:** migration `035`
- **Validate:** `SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='ARS_LISTING_PARKED' AND COLUMN_NAME='OPT_STATUS'` → 1. Run one small listing, approve, confirm the history row carries a status.
- **Risk:** low · **Depends:** P-01 · **Blocks:** P-04

### `[ ] P-04 · Allocation Review module`
**This is the instrument you validate the engine fixes with. Port it before M01/M02.**
> A merchandiser asks why store `HP04` didn't get a kurti it got last week.
> **Ours:** open SSMS, join listing → alloc → MSA across 29M rows. ~2 days, different answer per analyst.
> **DEV:** click the option → `R07_SIZE_RATIO — sizes 2/7, ratio 0.29, threshold 0.60`. ~30 seconds.

- **Copy:** `backend/app/api/v1/endpoints/alc_review.py`, `frontend/src/components/alcReview/` (8 files), register in `router.py` + `App.jsx` + sidebar
- **Validate:** open a past session; drill category → option → size; lifecycle matrix populated (needs P-03)
- **⚠ Depends on FS-11** — we have it, DEV reverted it. Confirm `ALLOC_ROUND` still stamps correctly after any engine merge, or Review misreports the round.
- **Risk:** low (new files) · **Depends:** P-03

### `[ ] P-05 · Dashboard — sessions endpoint exceeds the proxy timeout`
> The date filter sat **after** both `GROUP BY`s on a derived alias:
> `… ) x WHERE CAST(x.ts AS DATE) BETWEEN @from AND @to`
> → aggregate all 29M rows first, and `CAST(col AS DATE)` can't use an index.
> **DEV:** `WHERE ts_col >= @from AND ts_col < @to_excl` per branch — index seek before aggregation. **126.8s → 8.0s.**

- **Edit:** `backend/app/api/v1/endpoints/ars_dashboard.py`
- **Validate:** time the sessions endpoint for "exact date" — expect ~4.7× faster and **identical rows**
- **Also:** probes `INFORMATION_SCHEMA` before selecting lazily-created columns, so a freshly restored DB no longer errors
- **Risk:** low (read-only)

---

## Wave 2 — additive features

### `[ ] P-06 · Sec-cap growth matrix`
> Cotton at 40% of category → 130%. Linen at 3% → 300% (tripling 3 pieces is still 9).

| Share | Growth |
|---|---|
| 0–5% | 300% |
| 5–10% | 250% |
| 10–15% | 200% |
| 15–30% | 150% |
| 30%+ | 130% |

- **Copy:** `backend/app/services/sec_cap_growth_matrix.py` · **Apply:** `031`, `032`
- **Validate:** enable flag stays **off** after migration; a run produces identical output
- **Risk:** medium · **Depends:** P-01

### `[ ] P-07 · Contribution from the preset, not from what was listed`
> A yarn contributing **0.5%**, in a week when no sibling was listed.
> **Ours:** `working_df` loaded `WHERE LISTED_FLAG = 1` → siblings absent → grain is **100%** of what's there → `cont% = 100` → **tightest** band. The smallest line is punished hardest — the inverse of the intent.
> **DEV:** `cont% = 0.5` from the preset regardless of listing → widest band.

DEV's code carries: *"Do not reintroduce it."*
- **Validate:** pick a low-contribution grain, confirm its `<dim>_CONT_PCT` matches the preset and not the listed share
- **Risk:** medium · **Depends:** P-06

### `[ ] P-08 · REV_MBQ shows the ceiling that is actually enforced`
> Screen says ceiling **16** (slider 110%); engine enforces **30** (matrix 200%). Reviewer blames the wrong number.

- **Apply:** `033` (`ALC_REV_MBQ_GROWTH_SRC`)
- **⚠ Set it to `FLAT` first.** It is currently **AUTO** in our database.
- **Validate:** on `FLAT`, output identical to today. Flip to `AUTO` only as a deliberate, separate step.
- **Risk:** low (display only)

### `[ ] P-09 · Merge Rules — several merge tables per dimension`
> Set `E,V → EV` ✅ then add `V,P → VP` → **`V → EV` silently disappears.** No error.
> **DEV:** `merge_no 1: V → EV` → `MERGE_RNG_SEG`; `merge_no 2: V → VP` → `MERGE2_RNG_SEG`. Both live.

- **Edit:** `endpoints/merge_rules.py`, `services/derived_masters.py` · **Apply:** `027`
- **Validate:** create a second merge table; confirm the first survives
- **Risk:** medium (schema) · **Depends:** P-01 · **Blocks:** P-11

### `[ ] P-10 · ⚠ Merge grids inherit their parent — NEEDS BUSINESS SIGN-OFF`
> **Ours:** merge grids born **Inactive** (so "Run All Active" skips them forever — the merge silently does nothing) and **Primary** by default (which **locks the sec-cap checkbox**, so its cap can never be set).
> **After:** grid `MJ_MERGE_RNG_SEG` goes Primary → **Secondary**, cap Off → **On, 130%**.
> Store target 100 pcs: today allocation can reach 150, 200, unlimited. After: **hard ceiling 130**.

> **Scale: ~415,000 category-store combinations carry no cap today.**
> **There is no on/off switch.** It fires on the first run. Migration `028` can pre-align but is deliberately dry-run until its flag is set to 1 — and the cap applies at runtime regardless.

- **Do not port until Business/Planning sign off.**
- **Validate:** count affected grids *before* porting; A/B one MAJ_CAT to measure the drop
- **Risk:** **HIGH — moves stock** · **Depends:** P-09

### `[ ] P-11 · Upload guard — derived columns`
> **Ours:** upload a sheet containing `MERGE_RNG_SEG` → **accepted, no warning** → base says `V`, merge column says something else → contradiction until the next refresh wipes it. Reports built in that window are wrong with no indication.
> **DEV:** column dropped, base uploaded normally, derivation refreshed, response says *"MERGE_RNG_SEG was ignored (derived)"*.

> **⚠ MUST come after P-09/P-10.** The guard calls a helper **only M04 provides**. Ported alone it reports "Success" on every upload **while protecting nothing** — the call fails, is logged quietly, and the upload proceeds.

- **Risk:** low · **Depends:** P-09 (hard blocker)

### `[ ] P-12 · Backup & Restore Manager`
> Full backup of `Rep_Data` ≈ **620 GB** — expensive → run rarely → gap when you need it.
> Partial (history trimmed to shells) — **13.7 GB** — cheap enough to run before every release wave.

- **Copy:** `endpoints/backup.py`, `services/backup_service.py`, `services/restore_preserve.py`, 4 pages, `components/backup/` · **Apply:** `030`
- **Note:** no download endpoint and no unguarded restore — both deliberate. Partial mode cannot recreate stored-but-broken procs; it is a release net, not a substitute for a full backup.
- **Risk:** low (self-contained) · **Depends:** P-01

### `[ ] P-13 · Business Rules — a fourth value type, `multi``
> *Which segments switch pack rounding off?* Ours can't express "any subset", and can't tell **selected nothing** from **not configured** — opposite meanings.
> **DEV:** `value_type = multi`; value `''` → active, nothing selected *(a real decision)*; rule inactive → don't manage this at all *(a different one)*.

> **Trap in DEV's code:** pyodbc binds a parameter over 2,000 chars as `ntext`, and SQL Server refuses `ntext` in any comparison. **Cast the parameter, not just the column** — this previously broke every rule toggle with a 500.

- **Risk:** low

### `[ ] P-14 · FA & CONS — one alias list instead of three`
> Template says `"MBQ Qty"`, parser expects `"MBQ_QTY"` → the user fills in the template they were given, upload rejected, **and they can't tell whose fault it is.**

Net **−30 lines**: duplication removed, not layered over.
- **Risk:** low

### `[ ] P-15 · Grid Builder — PAK_SZ_APPLICABLE per category`
**Verified:** 18 matches in DEV, **0 here**.
> Store needs **4 pieces**, article carries `PAK_SZ = 6`.
> **Ours:** `floor((4 + 0.5×6) / 6) × 6 = 6` → 2 pieces nobody asked for.
> **DEV:** `PAK_SZ_APPLICABLE = 'N'` → effective `PAK_SZ = 1` → **4**.

`NULL` reads as `Y`, so nothing changes until someone sets an `N`.
- **Also:** deactivating a base grid **warns** and names dependents; deleting is **blocked** while they exist (deactivation is reversible, deletion drops the output table).
- **Risk:** low (inert by default)

### `[~] P-16 · Report Generation — chunked reads, retry, idempotency`
> | Action | Ours | DEV |
> |---|---|---|
> | Run report | 5M rows | 5M rows |
> | Run again | **10M — duplicated** | 5M — replaced |
> | Connection drops | **whole run fails** | retries that piece |

Also fixes the double count: ours reports `CSV (1.2M) + upload summary (1.2M) = 2.4M` for **the same rows**.
- **Risk:** low (reports only)
- **Code merged 2026-10-03** — whole files from DEV: `report_engine.py`, `report_scheduler_service.py`, `snowflake_sync.py`, `report_gen.py`, `sap_scheduler_service.py`, `ReportGenerationPage.jsx`, `main.jsx`, new `components/ui/ConfirmDialog.jsx`; `api.js` = 2 lines only (`duplicate`, `runNow(id, params)`). `snowflake_config_service.py` kept OURS (Get Data needs `connect(**overrides)`).
- **Three more bugs the merge fixed** (not listed above):
  - Daily/weekly/monthly schedules ran **every 2 h, 12:00–23:00** — the form always saves `every_n_hours: 2`. HOPC866 proof: weekly `ST_RANKING` (#86) fired ~9× per burst at 16/18/20/22:00 on 2026-08-31.
  - Schedule times were **UTC** (07:00 ran at 12:30 IST). Now server-local; startup re-bases every report `NEXT_RUN_AT`.
  - A split step uploaded the **wrong report's file** to Snowflake — source `ARS_GRID_MJ` loaded `ARS_GRID_MJ_MERGE_RNG_SEG.csv` and skipped its own `_SEG-*` files.
- **`sap_scheduler_service.py` must move with it** — `compute_next_run` is shared; without it SAP pulls fire 5h30m late. Get Data is unaffected (passes IST `now` explicitly).
- **Shared-DB note:** OURS (8080) and DEV (8000) both run the report + SAP schedulers on the same HOPC866 tables. Before the merge ours always lost the race (UTC clock); now both use local time, so the atomic claim decides which process runs each schedule.
- **Validated:** imports, `vite build`, function-level outputs identical to DEV, `NEXT_RUN_AT` re-base on HOPC866. **Pending:** one live run with Snowflake output (append + re-run) from our backend.

### `[ ] P-17 · SQL procedures`
**17a — `MJ_PER_OPT_MBQ` is wrong here. Verified:**
```sql
-- OURS  (SAL_PD is the CATEGORY total — never divided)
ISNULL(g.ACS_D,0) + ISNULL(g.SAL_PD,0) * ISNULL(g.ALC_D,0)

-- DEV
CASE WHEN ISNULL(g.OPT_CNT,0) > 0
     THEN ISNULL(g.ACS_D,0) + (ISNULL(g.SAL_PD,0) / g.OPT_CNT) * ISNULL(g.ALC_D,0)
     ELSE ISNULL(g.ACS_D,0) END
```
> Mean absolute error vs the engine over 184,735 rows: **14.15 → 0.67 pcs.**

**17b —** `SESSION_ID` + `APPROVED_AT` on every row. ⚠ **They sit at positions 3–4, shifting every later column by two. Any consumer reading by position must be rechecked.**

**17c —** three new procs: `usp_ars_grid_var_art` (accepts a comma list — `'M_JEANS,L_JEANS'`), `usp_var_art_alc_rpt`, `usp_park_temp`.
- **Risk:** medium (17b), low (17a, 17c)

### `[~] P-18 · ConfirmDialog across the UI`
**Verified:** 36 files in DEV; **32 pages here still use native `confirm()`**.
- **2026-10-03 (with P-16):** `components/ui/ConfirmDialog.jsx` + `<ConfirmHost/>` in `main.jsx` are in; only Report Generation uses it so far. The other pages are still the sweep.
> *"Remove a checklist item"* and *"kill a live SQL session, rolling back its transaction"* currently **look identical**.
> **DEV:** approving a session lists all six tables it writes; deleting a user says what **survives** (*"Audit rows they created are kept"*); killing a session requires the id **typed**.

- **Known gaps in DEV:** Listing Logs' "Kill session" / "Permanently delete session" still lack type-to-confirm, and Dev Sync still uses old pop-ups.
- **Risk:** low but **wide** — do it as one sweep, not spread across other items

---

## Wave 3 — the engine (**moves stock** — do these last, one at a time)

> Port **P-04 first**. Without Allocation Review you cannot see what these did.

### `[ ] P-19 · R07 — the size check that can never fire` ⚠ **biggest single change**
**Verified —** ours (`rule_engine_new.py:393`):
```python
f" AND ISNULL([VAR_FNL_COUNT],0) < {min_size_count} "     # unconditional
```
DEV (`:418`):
```python
_r07_min = (f" AND ISNULL([VAR_FNL_COUNT],0) < {min_size_count} "
            if int(min_size_count or 0) > 0 else "")
```
> Your last real run used `min_size_count = 0`, so ours generates `AND VAR_FNL_COUNT < 0` — impossible. Joined by `AND`, **R07 never fires**. TBL options already marked `TBL_SIZE_LT_60` stayed `LISTED_FLAG = 1` and allocated anyway.
> **Reads like:** a lock fitted upside-down. The door was never shut.

- **Expect:** fewer TBL options listed, **total shipped quantity drops** for affected categories
- **Validate:** `replay_session.py` before/after on the same stock window; count `R07_VAR_RATIO_TBL` skips (0 today)
- **Risk:** **HIGH — moves stock**

### `[ ] P-20 · Part 6.6 reads the wrong threshold`
**Verified —** ours line 3031: `_t = float(req.stock_threshold_pct or 0.6)` *(display cover)*
DEV line 3002: `_t = float(req.size_threshold or 0.6)` *(the size rule)*
> Last run: `size_threshold = 0.5`, `stock_threshold_pct = 0.6`. Ours judges size coverage at **60%** when the rule says **50%**. Two stages, two rulebooks — stock shipped while the audit recorded it as rejected.

- **Risk:** **HIGH — moves stock** · pair with P-21

### `[ ] P-21 · VAR_COUNT counts rows, not sizes`
**Verified:** `Master_CONT_SZ` appears 4× in DEV's `listing.py`, **0× here**.
> `M_TEES_HS`, real ladder **7**, option stocked in **4** sizes.
> **Ours:** `VAR_COUNT` = 3 … 3,095 (article rows) → `4 / 3095 = 0.001` → reads as catastrophic coverage.
> **DEV:** `VAR_COUNT = 7`, `VAR_FNL_COUNT = 4` → `4 / 7 = 0.57` — a number that means something.

Three branches: not size-managed → `1`; in `Master_CONT_SZ` → distinct live sizes; else the observed ladder, logged as a master-data gap.
- **Risk:** **HIGH — moves stock** · **P-19/P-20/P-21 are one family; validate together after all three**

### `[ ] P-22 · Parking moves Part 8.4 → 8.56`
**Verified:** "Part 8.56" appears 7× in DEV, 0× here.
> Ours parks **before** `OPT_STATUS` is stamped and **before** holds are released, so two patches back-fill the parked copy. DEV parks after both and deletes the patches.

- **Risk:** medium · **Depends:** P-03

### `[ ] P-23 · New R00 — re-assert eligibility inside Stage A`
**Verified:** present in DEV, absent here. Defaults `True`; bites under audit mode.
- **Risk:** medium

### `[ ] P-24 · Cap Ladder + family lending` — **hardest item, do it last**
> Store `HJ08`, `L_KURTI_ST`. Wall `MJ_RNG_SEG` full. Family `MJ_MERGE_RNG_SEG` has **500** spare. Option needs **80**.
> **OFF** → ships `0`, `SKIP_REASON = SEC_CAP[MJ_RNG_SEG]` — identical to today
> **SHADOW** → ships `0`, stamps `LADDER_SHADOW_WOULD_SHIP(+80)`
> **ON** → ships `80`, stamps `LADDER_OVERFLOW[MJ_MERGE_RNG_SEG](fam_budget=500…)`
> Family held only 50? `ON` ships nothing, stamps `LADDER_EXHAUSTED`. **The family ceiling is never breached.**

- **Copy:** `_build_sec_state`, `_ladder_pass`, `_ladder_reset_leftovers`, `_ladder_stamp_round`, `_mjr_rebuild` (`rule_engine_pandas.py`); `_family_lend`, `_resolve_grid_families` (`rule_engine_per_opt.py`)
- **⚠ Same two files as our RDC pool fix.** DEV is +301 / +246 lines there. Hand-merge — not `git merge -X`.
- **Keep `ALC_CAP_LADDER = OFF`, then run SHADOW for a week before considering ON.**
- **Risk:** **HIGHEST** · **Depends:** P-06, P-07, P-09, P-10

---

## Decide separately (not a port)

### `[ ] D-01 · Dev Sync Manager — DEV deleted it, we have it`
> | | Dev Sync | Backup + Restore |
> |---|---|---|
> | granularity | per table | whole database |
> | keeps dev-local data | **YES** | **NO** — replaces everything |

Restore it, retire it on record, or build the gap — but **decide**, rather than letting it disappear as a merge side-effect.

### `[ ] D-02 · Dead-table cleanup — do NOT run blind`
47 tables, 2.5 MB, from a 2026-08-07 read-only audit. **Nothing has been executed.** The script demands a full backup of both `Rep_Data` and `Claude` first, and its own caveats note usage stats cover only ~3 weeks and that `dev_sync` reads every table, so read timestamps are **noise, not proof of use**.

### `[ ] D-03 · Reset `ALC_REV_MBQ_GROWTH_SRC` to FLAT`
Currently **AUTO** in our database, seeded by DEV. Until reset, any production-vs-staging comparison measures the wrong thing.

### `[ ] D-04 · Push DEV's `5946116``
It is **local-only** in `D:\ARS_PROD\ars_prod` and has never reached the remote. Until it does, nothing can be rebased onto it and the two lines keep drifting.

---

## How to validate every port

```bash
# 1. capture a before-run
python scripts/replay_session.py --source <session_id> --mode own

# 2. port ONE item, restart the backend

# 3. capture an after-run
python scripts/replay_session.py --source <session_id> --mode own

# 4. compare
python scripts/diag_own_drift.py <before_sid> <after_sid>
```

**Three things that will mislead you if you forget them:**

1. **Parking does not consume stock — only approving does.** Two parked runs back to back see identical stock. An approved run in between drains the pool and every later number is wrong. *(We measured this: 36,426 pcs → 5,481 pcs after one approval.)*
2. **The source grid is rebuilt through the day.** Part 1 moved 960,087 → 966,661 → 967,048 in one morning. Compare runs from the same window, and use Part 1's row count as the validity gate — **if it differs, the comparison is not valid.**
3. **`replay_session.py` refuses to run if any non-scope key differs**, so before/after stays honest. Trust that guard rather than hand-editing parameters.

---

## Progress

| Wave | Items | Done |
|---|---|---|
| 0 — foundation | P-01 … P-02 | 0 / 2 |
| 1 — instruments | P-03 … P-05 | 0 / 3 |
| 2 — additive | P-06 … P-18 | 0 / 13 |
| 3 — engine | P-19 … P-24 | 0 / 6 |
| decisions | D-01 … D-04 | 0 / 4 |
| **Total** | | **0 / 28** |
