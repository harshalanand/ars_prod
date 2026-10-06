# ARS — what DEV has that this code does not

**Compared:** 2026-09-28 · **Do not change code from this document — review, decide, then port one item at a time.**

## First: which folder is which

The folder names mislead. Evidence from the code and from the *ARS Module Change Ledger* (comparison run 2026-09-12):

| Path | What it really is |
|---|---|
| `\\192.168.150.100\ars_prod` | **LIVE PROD** — branch `ars_v2_prod` @ `9bbb97a` |
| `D:\ARS_PROD\ars_prod` | **DEV / V3.0** — branch `ars_v2` @ `5946116`, **never pushed** |
| `D:\PROJECTS\ars_v3` *(this code)* | **PROD lineage + Central RDC Pool** @ `922bb3b` |

So despite its name, `D:\ARS_PROD\ars_prod` is the **V3.0 development line** carrying 16 modules of work. This code sits on the PROD line. The related artifacts are **ARS V3.0 Release Brief**, **ARS Module Change Ledger** and **ARS Divergence Dossier**.

## The thing to understand before porting

> **Turning every new switch OFF does *not* give you today's behaviour.**

DEV contains two classes of change:

| Class | Behaviour |
|---|---|
| **Flag-gated** — `ALC_CAP_LADDER`, `ALC_REV_MBQ_GROWTH_SRC`, growth-matrix enable, `use_irod_eligibility` | OFF really is identical to today |
| **No flag at all** — 7 corrections | **Fire on the first run whatever the flags say** |

With every flag off, a run is **today's behaviour plus seven corrections**. That is the point of the upgrade — but expect different numbers, not identical ones.

### Status key
`NOT PORTED` · `IN PROGRESS` · `PORTED` (merged **and** validated on HOPC866) · `SKIP` (record the reason)

### Verification key
✅ **Verified** — I read both codebases and confirmed it
📋 **From ledger** — described in the artifacts, not yet confirmed in code by me

---

# M01 — Listing Engine
**Risk: MOVES STOCK · no flag** · `listing.py` · migration `035`

### 1.1 `VAR_COUNT` counts the wrong thing ✅
**Ours:** counts variant-article **rows**. That table holds several articles per size, so it measures rows, not sizes.
**DEV:** the planned size ladder per category — not size-managed → `1`; found in `Master_CONT_SZ` → distinct live sizes; otherwise the observed ladder, logged as a master-data gap.

> Category `M_TEES_HS`, real size ladder **7**, option stocked in **4** sizes.
> **Ours:** `VAR_COUNT` = anywhere from 3 to 3,095 (article rows) → ratio `4 / 3095 = 0.001` → reads as catastrophic coverage.
> **DEV:** `VAR_COUNT = 7`, `VAR_FNL_COUNT = 4` → ratio `4 / 7 = 0.57` — a number that means something.

*Verified:* `Master_CONT_SZ` appears 4× in DEV's `listing.py`, **0× in ours**.
**Status: NOT PORTED**

### 1.2 The size gate reads the wrong setting ✅
**Ours:** `_t = float(req.stock_threshold_pct or 0.6)` — that's the **display-cover** setting.
**DEV:** `_t = float(req.size_threshold or 0.6)` — the **size** rule, matching Stage A.

> Last real run used `size_threshold = 0.5` and `stock_threshold_pct = 0.6`.
> Ours judges size coverage at **60%**; it should judge at **50%**. Two stages, two different rulebooks.

*Verified:* line 3031 in ours vs line 3002 in DEV.
**Status: NOT PORTED**

### 1.3 Parking moves from Part 8.4 to Part 8.56 📋
**Ours:** parks at **8.4** — *before* `OPT_STATUS` is stamped and *before* holds are released, so two patches exist to back-fill the parked copy.
**DEV:** parks at **8.56**, after both. Deletes the patches instead of maintaining them.

> Approve a run today and the history row can carry a status that was corrected after parking. DEV captures the final state once.

**Status: NOT PORTED**

---

# M02 — Allocation Rule Engine
**Risk: MOVES STOCK · no flag + one switch** · `rule_engine` ×3

### 2.1 R07 — the size check that could never fail ✅ ⚠ **biggest single change**
**Ours** appends the min-size clause **unconditionally**:
```python
f" AND ISNULL([VAR_FNL_COUNT],0) < {min_size_count} "
```
**DEV** only adds it when it is actually set:
```python
_r07_min = (f" AND ISNULL([VAR_FNL_COUNT],0) < {min_size_count} "
            if int(min_size_count or 0) > 0 else "")
```

> At the common setting `min_size_count = 0` ours generates `AND VAR_FNL_COUNT < 0`.
> You can never hold fewer than zero sizes → always false → joined by `AND` → **R07 can never fire.**
> TBL options that Part 6.6 already marked `TBL_SIZE_LT_60` stayed `LISTED_FLAG = 1` and allocated anyway.
> **Reads like:** a lock fitted upside-down. The door was never shut.

*Verified:* ours line 393, DEV lines 418-419. DEV's own comment dates the fix **2026-08-26**.
**Expect fewer TBL options listed, and total shipped quantity to drop for affected categories.**
**Status: NOT PORTED**

### 2.2 Cap Ladder — three rounds with family borrowing ✅
Behind `ALC_CAP_LADDER`, default **OFF**, self-disabling when sec-cap specs are unavailable.

`R1` base at 100% → `R2` at growth % → `R3` borrow from the merge family's *remaining* budget.

> Store `HJ08`, `L_KURTI_ST`. Wall `MJ_RNG_SEG` is full. Family `MJ_MERGE_RNG_SEG` has **500** spare. Option needs **80**.
> **OFF** → ships `0`, `SKIP_REASON = SEC_CAP[MJ_RNG_SEG]` — identical to today
> **SHADOW** → ships `0`, but stamps `LADDER_SHADOW_WOULD_SHIP(+80)` so you learn it was reachable
> **ON** → ships `80`, stamps `LADDER_OVERFLOW[MJ_MERGE_RNG_SEG](fam_budget=500…)`
> Had the family held only 50, `ON` ships nothing and stamps `LADDER_EXHAUSTED`. **The family ceiling is never breached.**

*Verified:* `_build_sec_state`, `_ladder_pass`, `_ladder_reset_leftovers`, `_ladder_stamp_round`, `_mjr_rebuild`, `_family_lend`, `_resolve_grid_families` — all absent here.
**Status: NOT PORTED · Risk: HIGH — same files as our RDC pool fix**

### 2.3 New R00 — re-assert eligibility inside Stage A 📋
DEV adds `R00` re-asserting the Layer 1 eligibility verdict inside Stage A. Defaults `True`; bites under audit mode.
**Status: NOT PORTED**

### 2.4 ⚠ FS-11 stamp — *we* are ahead here 📋
The ledger records that **PROD has the FS-11 `ALLOC_ROUND` stamp fix which DEV reverted.** We carry it (commit `6dbf67b`).

> **Do not lose this when merging.** Alloc Review (M06) depends on it — built on the reverted stamp, the new report misreports the round.

**Status: KEEP OURS — verify after any engine merge**

---

# M03 — Sec-Cap Growth Matrix
**Risk: behind a switch** · `sec_cap_growth_matrix.py` · migrations `031`–`034`

### 3.1 Growth bands instead of one flat % ✅
**Ours:** one flat percentage per grid, typically **130%**, applied to every grain underneath it whatever its size.
**DEV:** resolved from a contribution band table.

| Share of category | May grow to |
|---|---|
| 0–5% | 300% |
| 5–10% | 250% |
| 10–15% | 200% |
| 15–30% | 150% |
| 30%+ | 130% |

> Cotton at 40% of the category → 130%. Flooding the shop with more cotton is risky.
> Linen at 3% → 300%. Tripling 3 pieces is still only 9.

**Status: NOT PORTED**

### 3.2 Contribution comes from the preset, not from what happened to be listed ✅
**Ours:** where the matrix path ran, contribution was the grain's share of whatever was **listed** that week.
**DEV:** always the **maintained preset**.

> A yarn contributing **0.5%** of the category, in a week when none of its siblings were listed.
> **Ours:** `working_df` is loaded `WHERE LISTED_FLAG = 1` → siblings absent → this grain is **100%** of what's there → `cont% = 100` → collects the **tightest** band.
> **The smallest, most neglected line is punished hardest** — the exact inverse of the matrix's intent.
> **DEV:** `cont% = 0.5` from the preset regardless of listing → widest band.

DEV's code carries the instruction *"Do not reintroduce it."*
**Status: NOT PORTED**

### 3.3 Three audit columns — see *why* it was refused ✅
`<dim>_CONT_PCT`, `<dim>_GROWTH_PCT`, `<dim>_GROWTH_SRC` (`MATRIX` / `GRID_FLAT` / `FLAT`).

> `15 planned × 200% = 30 allowed`; store holds `14`; option wants `22` → `14 + 22 = 36 > 30` → **refused**. All four numbers on screen instead of guessed.

**Status: NOT PORTED · Risk: LOW — additive columns**

### 3.4 `ALC_REV_MBQ_GROWTH_SRC` — make `REV_MBQ` match the real ceiling ✅
**Ours:** every grid multiplies by the one slider, so the ceiling **shown** is not the ceiling **enforced**.

> Screen says ceiling **16** (slider 110%); engine actually enforces **30** (matrix 200%). The reviewer blames the wrong number.

**DEV:** `AUTO` resolves growth per grid, so the number displayed *is* the number that blocks. `FLAT` keeps today's behaviour.
**Status: NOT PORTED · default FLAT is safe**

---

# M04 — Merge Rules
**Risk: MOVES STOCK · no flag** · `merge_rules.py` · `derived_masters.py` · migrations `027`, `028`

### 4.1 A dimension could only be merged one way ✅
> You set `E, V → EV` ✅ then add `V, P → VP` → **`V → EV` silently disappears.** No error.

Because the key was `(source_col, source_value)`. DEV adds `merge_no`; the key widens to `(source_col, merge_no, source_value)`.

> `merge_no 1: V → EV` → column `MERGE_RNG_SEG`
> `merge_no 2: V → VP` → column `MERGE2_RNG_SEG`
> Both live, both derived, parent recoverable from the name.

Plus whole-merge-table preview, activate and delete.
**Status: NOT PORTED**

### 4.2 ⚠ Merge grids inherit their parent — a dormant wall starts enforcing ✅
**Ours:** auto-created merge grids are born **Inactive** (so "Run All Active" skips them forever — the merge silently does nothing) and **Primary** by table default (which **locks the sec-cap checkbox**, so the cap can never be configured).
**DEV:** a merge grid copies **every** classification field from its base — status, group, sec-cap flag, cap %, weight.

> Grid `MJ_MERGE_RNG_SEG`:
> | Setting | Today | After merge |
> |---|---|---|
> | Group | Primary | **Secondary** |
> | Ceiling cap | Off — no limit | **On — 130%** |
>
> Store target 100 pcs. Today allocation can reach 150, 200, unlimited. After: **hard ceiling 130**.

> **Scale: roughly 415,000 category-store combinations carry no cap today.**
> **The inheritance is unconditional and there is no on/off switch.** It fires on the first run after go-live. A grid sitting at `sec_cap_applicable = 0` inherits `1` and **starts acting as a cap wall with every flag off.**

Migration `028` can pre-align this but is deliberately left in **dry-run** until someone sets its flag to 1 — and the cap applies at runtime regardless.
**Status: NOT PORTED · NEEDS BUSINESS SIGN-OFF**

---

# M05 — Grid Builder
**Risk: opt-in per category** · `grid_builder.py`

### 5.1 `PAK_SZ_APPLICABLE` — turn pack rounding off per category ✅
**Ours:** pack rounding applies to every category. The only lever is the article's own `PAK_SZ` in MSA master data — which every MSA run rewrites.
**DEV:** a `Y`/`N` per category, read only when the allocation table is built. Master data untouched. `NULL` reads as `Y`, so nothing changes until someone sets an `N`.

> Store needs **4 pieces**, article carries `PAK_SZ = 6`.
> **Ours:** `floor((4 + 0.5×6) / 6) × 6 = 6` → 2 pieces the store never asked for, because the category isn't actually pack-managed.
> **DEV:** `PAK_SZ_APPLICABLE = 'N'` → effective `PAK_SZ = 1` → `floor((4 + 0.5) / 1) × 1 = 4`.

*Verified:* 18 matches in DEV, **0 here**.
**Status: NOT PORTED**

### 5.2 Protect grids that others depend on 📋
**Ours:** deactivating or deleting a base grid silently orphans the merge grids built on it.
**DEV:** deactivation **warns** and names dependents; deletion is **blocked** while they exist.

> Why warn on deactivate but block on delete: deactivation is reversible, deletion drops the output table and is not.

**Status: NOT PORTED**

---

# M06 — Allocation Review *(new module)*
**Risk: additive, read-only** · `alc_review.py` · 8 endpoints · 8 components · migration `035`

### 6.1 The judgement report ✅
**Ours:** the system shows **what** was allocated. The reasoning is scattered across skip reasons, remarks, run logs, grid caps, eligibility flags and the MSA pool, with nothing joining them. Answering *"why this quantity"* means hand-written SQL against a 29M-row archive — differently by each analyst.
**DEV:** session summary, hierarchy drill, `OPT_TYPE` × `OPT_STATUS` lifecycle matrix, ranked blockers, full option dossier, export.

> After R07 starts firing, a merchandiser asks why store `HP04` didn't get a kurti it got last week.
> **Ours:** open SSMS, join listing → alloc → MSA across 29M rows. **~2 days**, and a different answer depending who ran it.
> **DEV:** click the option in the dossier → `R07_SIZE_RATIO — sizes 2/7, ratio 0.29, threshold 0.60`. **~30 seconds**, and the answer is *"the system was right."*

Performance: one cube at the finest shared grain — 5M rows collapse to 168k, cached by how mutable each source is. Measured 250,143 grains in **9.0s**; cold load 21.1s; every load after **0.01s**.

*Verified:* `alc_review.py` absent here entirely.
**Status: NOT PORTED · this is the instrument you validate M01/M02 with — port it early**

### 6.2 `OPT_STATUS` on the archive tables ✅
**Ours:** `OPT_STATUS` lives only on `ARS_LISTING_WORKING`. Parked and History snapshots carry `OPT_TYPE` (pre-run) but not the verdict.

> Open last Tuesday's run and ask what an option ended as — it was never saved. Review had to re-derive it.

Rows archived before the stamp stay `NULL` on purpose; Review treats `NULL` as "derive it" and labels it so.
**Status: NOT PORTED · migration `035` · M06 depends on it**

### 6.3 ⚠ `LST_OPT_STATUS_STAMP` — three blank screens, one cause 📋
Allocation Review's lifecycle matrix, Data Dictionary status definitions, and the Grid Report status columns all come up **blank**. It looks like three bugs. It is one switch.

> **0 of 8,900,054** history rows carry a status stamp, because the rule that writes it is switched **off**. The empty result is the *correct* answer to the question being asked.
> **Reads like:** asking a shop to list every colour of shirt it sells, when the shop has no shirts.

**Fix: turn on that one rule and all three light up together.**
**Status: NOT PORTED · check whether this rule exists here at all**

---

# M07 — Backup & Restore Manager *(new module)*
**Risk: additive, separate** · `backup.py` · 26 endpoints · 4 pages · migration `030`

### 7.1 Backups from the screen ✅
**Ours:** backups are scripts somebody has to remember to run, on a server somebody has to log into. Nobody is certain when the last good one was taken or where it is. At ~620 GB a full backup is expensive enough that it gets skipped, and restoring is hand-typed under pressure.
**DEV:** named profiles, a scheduler that starts with the app, run history with logs, a file inventory, per-volume free space, a generated `RESTORE` script, and a guarded restore.

> **Full** backup of `Rep_Data` ≈ **620 GB** — expensive → run rarely → gap when you need it
> **Partial** (history trimmed to shells) — **13.7 GB** — cheap enough to run before every release wave

> Deliberate omissions: **no download endpoint** and **no unguarded restore**.
> Caveat: the partial method cannot recreate stored-but-broken procedures. It is a release net, **not** a replacement for a full backup before a major change.

**Status: NOT PORTED · self-contained**

### 7.2 `restore_preserve.py` — don't lose dev-only tables ✅
A restore replaces the **whole** database, destroying anything that exists only on the destination.

> You built `MY_TEST_TABLE` while developing. You restore last night's backup. **It's gone.**

Flow: `detect → preserve → restore → reapply → verify`. Preserves **before** touching anything — if preserve fails, the restore never starts. **Never deletes the held copy on failure.**
**Status: NOT PORTED**

---

# M08 — Business Rules
**Risk: additive, registry only** · `business_rules.py` · migration `033`

### 8.1 A fourth value type: `multi` ✅
**Ours:** three shapes — on/off, a number, one-of-a-list. No way to express *"any subset of these"*, and no way to tell **selected nothing** apart from **not configured** — which mean opposite things.
**DEV:** a `multi` type — comma-separated subset, stored de-duplicated and in the order the choices declare, so the value is stable however the UI submits it.

> *Which segments switch pack rounding off?*
> **Ours:** one flag per segment — doesn't scale, and can't express "none" at all.
> **DEV:** one rule, `value_type = multi`, `choices = APP,GM,…`
> value `''` → active, nothing selected *(a real decision)*
> rule inactive → don't manage this at all *(a different one)*

> **Trap carried in DEV's code:** pyodbc binds a parameter over 2,000 characters as `ntext`, and SQL Server refuses `ntext` in any comparison. **Cast the parameter, not just the column** — this previously broke every rule toggle with a 500.

*Verified:* `'multi'` present in DEV, absent here.
**Status: NOT PORTED**

### 8.2 Turning a rule off shows its consequence first 📋
**Status: NOT PORTED**

---

# M09 — Report Generation & Snowflake Sync
**Risk: additive, reports only** · `snowflake_sync.py` · `report_engine.py`

### 9.1 Chunked reads, retry, idempotency ✅
**Ours:** whole files read into memory before loading → the largest and most valuable reports hit out-of-memory. A transient network drop fails a target with no retry. A re-run duplicates rows. A new source column breaks the load.

> | Action | Ours | DEV |
> |---|---|---|
> | Run report | 5M rows | 5M rows |
> | Run it again | **10M — duplicated** | 5M — replaced |
> | Connection drops | **whole run fails** | retries that piece |

*Verified:* 14 `chunk` matches in DEV, **0 here**.
**Status: IN PROGRESS** — code merged 2026-10-03; pending one live Snowflake run (append, then re-run same session) from our backend.

### 9.2 Row counts were double-counted ✅
> **Ours:** `files[] = [ the CSV (1.2M rows), the Snowflake upload summary (1.2M rows) ]` → reported **2.4M** — the same rows counted twice.
> **DEV:** count exported **files** only; in DIRECT mode fall back to the upload entries → **1.2M**.

**Status: IN PROGRESS** — code merged 2026-10-03.

### 9.3 Run-time parameter overrides 📋
**Ours:** running a report for a different month means editing the saved definition and remembering to change it back.
**DEV:** overrides applied at queue time; the saved definition is never mutated. Plus live progress weighted by each step's last duration, a duplicate-report action, and an optional DIRECT mode with no intermediate CSV.

> DIRECT mode is opt-in and deliberately **slower** — it processes rows in Python rather than pandas' optimised CSV path. It exists for when writing the CSV is itself the problem.
> **When running a report, always choose a schema explicitly** — leaving it blank sends data to a shared area that gets swept clean automatically.

**Status: IN PROGRESS** — code merged 2026-10-03.

### 9.4 Schedules fired on the wrong clock and the wrong frequency ✅
**Ours:** the form saves every field, so a daily/weekly/monthly config always carries `every_n_hours: 2`, and `compute_next_run` took the hourly branch whenever that key existed. Times were also UTC.
> Same config `{"freq":"daily","times":["07:00"],"every_n_hours":2,"start":"12:00","end":"23:00"}`:
> **Ours:** `Sat 12:00, 14:00, 16:00, 18:00, 20:00, 22:00` (UTC → 17:30 IST onward)
> **DEV:** `Sun 07:00, Mon 07:00, Tue 07:00 …` (server local)
>
> HOPC866: weekly `ST_RANKING` (#86) ran ~9× per burst at 16:00/18:00/20:00/22:00 on 2026-08-31.

`compute_next_run` is shared with SAP Pull and Get Data → `sap_scheduler_service.py` moves with it (else SAP pulls fire 5h30m late). Get Data passes IST `now` explicitly, so it is unaffected. On startup DEV re-bases every enabled report `NEXT_RUN_AT` to local; SAP pull schedules are **not** re-based (one early fire possible).
**Status: IN PROGRESS** — code merged 2026-10-03; ST_RANKING re-based to `Mon 2026-10-05 13:22` on HOPC866.

### 9.5 Split steps uploaded the wrong file to Snowflake ✅
**Ours:** `_file_for_source` wants an exact base-name match, else the first `glob(<source>*.csv)` hit.
> Folder: `ARS_GRID_MJ_SEG-APP_DIV-KIDS.csv`, `…_SEG-APP_DIV-MENS.csv`, `…_SEG-GM_DIV-HOME.csv`, `ARS_GRID_MJ_MERGE_RNG_SEG.csv`
> **Ours** (source `ARS_GRID_MJ`): loads `ARS_GRID_MJ_MERGE_RNG_SEG.csv` — another report's data — and skips all three of its own.
> **DEV** `_files_for_source`: loads the three `_SEG-*` files; a sibling whose name merely starts with the base is excluded.

**Status: IN PROGRESS** — code merged 2026-10-03.

---

# M10 — Data Dictionary, Registry & Help
**Risk: additive, documentation** · migrations `034`, `036`

### 10.1 The `OPT_MBQ` formula was written backwards ✅
> **Ours:** *"OPT_MBQ = sum of SZ_MBQ over all its sizes"*
> **Truth:** `OPT_MBQ = ROUND(ACS_D + rate × ALC_D, 0)` — and `SZ_MBQ` is produced by **splitting OPT_MBQ across the sizes**.
>
> A 100-piece T-shirt target:
> Old text claims `15+30+30+15+10 → 100`
> Reality is `100 → S 15, M 30, L 30, XL 15, XXL 10`
>
> Both readings give 100, so the error looked harmless — but anyone troubleshooting from the old text searches in entirely the wrong place.
> **Reads like:** a recipe that says *"bake the cake to find out the ingredients."*

This matters more than a doc nit: **Allocation Review renders dictionary prose directly in its hover cards**, so the wrong explanation appears beside real numbers.
**Status: NOT PORTED · migration `036`**

### 10.2 A registry — defined once, imported ✅
**Ours:** reason codes explained differently on each surface; upload formats described in a template and implemented separately in a parser.
**DEV:** screens, reason codes and upload specs **defined once and imported**, *because prose cannot be imported by the thing it describes*. Plus per-screen help, an eligibility dossier, and **a test that fails when the manual and the code disagree**.
**Status: NOT PORTED**

### 10.3 New docs ✅
`eligibility.md` (*"why did this option get stock — and why did that one not?"*), 8 per-screen help pages under `manual/pages/`, migration `034` for the sec-cap column definitions.
**Status: NOT PORTED**

---

# M11 — Dashboard & Performance
**Risk: additive, read-only fix** · `ars_dashboard.py`

### 11.1 The sessions endpoint exceeded the proxy timeout 📋
**Ours:** **126.8s**, past the 120s proxy timeout — users see a failure on the first screen they open.

> **Why:** the filter sat **after** both `GROUP BY`s, on a derived alias:
> `… ) x WHERE CAST(x.ts AS DATE) BETWEEN @from AND @to`
> → scan and aggregate all 29M rows **before** the date applies — and `CAST(col AS DATE)` can't use an index either.
> **DEV:** each branch gets `WHERE ts_col >= @from AND ts_col < @to_excl` — an index seek on the bare column, before aggregation. **126.8s → 8.0s.**

Measured on 8.9M rows: exact date **4.7× faster**, last 7 days 2.8×, last 90 days 2.7×. "No date filter" is 1.2× *slower* on purpose — nothing to filter early, so you only pay a small planning cost. **Rows returned were identical in every test.**

### 11.2 Fails outright on a freshly restored database 📋
On any database where no run has started since `ALLOC_TYPE` was introduced, the column doesn't exist and the endpoint fails. DEV probes `INFORMATION_SCHEMA` before selecting lazily-created columns.
**Status: NOT PORTED**

---

# M12 — Upload & Templates
**Risk: additive · ⚠ DEPENDS ON M04** · `file_upload_service.py` · `upload_template.py`

### 12.1 Uploading a derived column silently corrupts it ✅
**Ours:** nothing stops a user uploading values into a **derived** merge column — and the upload **succeeds**. The values sit there contradicting the base column until the next refresh silently overwrites them. Reports built in that window are wrong with no indication.

> | Column | Who owns it | Should be |
> |---|---|---|
> | `MAJ_CAT` | you | accepted |
> | `MBQ` | you | accepted |
> | `MERGE_RNG` | calculated from `MBQ` | **ignored** |
>
> **Ours:** screen reports *Success*; the bad column went in; survives until next refresh.
> **DEV:** column dropped, base uploaded normally, derivation refreshed immediately, response says *"MERGE_RNG_SEG was ignored (derived)"*.

Dropped rather than rejected on purpose: a whole-file rejection punishes a user whose sheet is mostly correct because an export happened to include the derived column.

> ### ⚠ **Merge M04 before M12**
> The guard calls a helper that **only M04 provides**. Merged alone, M12 reports "Success" on every upload **while protecting nothing** — the call fails, gets logged quietly, and the upload carries on.
> **Reads like:** a security guard posted at the door, but nobody gave him the keys — and he never mentions it.

**Status: NOT PORTED · BLOCKED ON M04**

### 12.2 Templates generated from the same specs the parsers read ✅
**Status: NOT PORTED**

---

# M13 — FA & CONS
**Risk: additive · net deletion (−30 lines)**

### 13.1 One alias list instead of three ✅
**Ours:** the MBQ master and UPC store-list uploaders each hold **their own** list of accepted column headings, maintained by hand, separate from the template the user downloads.

> **Ours:** template says `"MBQ Qty"`, parser expects `"MBQ_QTY"` → the user fills in the template they were given, the upload is rejected, **and they can't tell whose fault it is.**
> **DEV:** one tuple, three readers (template + both parsers). Adding an alias is a one-line change.

Every other file in this module differs only by line endings.
**Status: NOT PORTED**

---

# M14 — SQL Reporting Procedures
**Risk: additive, reports only** · 2 changed · 3 new

### 14.1 Provenance on every row ✅
**Ours:** output carries numbers but no provenance — two extracts days apart are indistinguishable once the file leaves the system.
**DEV:** `SESSION_ID` and `APPROVED_AT` on every row, plus `OPT_STATUS` columns built at run time from the values actually present.

> ⚠ **Ordinal shift:** the two new session columns sit at **positions 3–4**, moving everything after them by two. **Any consumer reading by position must be rechecked.**

**Status: NOT PORTED**

### 14.2 `MJ_PER_OPT_MBQ` — make the report reconcile with the engine 📋
`SAL_PD` is the **category** total daily sale; the engine's MBQ is **per option**.

> **Ours:** no per-option figure at grid grain → mean absolute error vs the engine, over 184,735 rows: **14.15 pcs**
> **DEV:** `MJ_PER_OPT_MBQ = ACS_D + (SAL_PD / OPT_CNT) × ALC_D`
> validated on `HJ08 / L_KURTI_ST`: `0.4393` vs `0.4311` → mean absolute error **0.67 pcs**

*Note:* `MJ_PER_OPT_MBQ` matched **1× in both** codebases — check whether ours is the full implementation.
**Status: NOT PORTED — verify first**

### 14.3 Three new procedures ✅
| Proc | What it gives you |
|---|---|
| `usp_ars_grid_var_art` | variant-article grid report with full hierarchy (SEG / DIV / SUB_DIV / SSN / RNG_SEG); `@WERKS` and `@MAJ_CAT` accept a **comma list** — `'M_JEANS,L_JEANS'` |
| `usp_var_art_alc_rpt` | variant-article allocation report by SEG / DIV / session |
| `usp_park_temp` | parked-session report by SEG / DIV / session |

Columns discovered at execution time, so one definition serves both servers.
**Status: NOT PORTED**

---

# M15 — UI Platform
**Risk: additive, front end only** · `ConfirmDialog.jsx` · ~35 files

### 15.1 Real confirmation dialogs ✅
**Ours:** **55** native `window.confirm` calls plus 4 prompts and 2 alerts. One line of plain text, no styling, no way to state consequences.

> *"Remove a checklist item"* and *"kill a live SQL session, rolling back its transaction"* **look identical.**

**DEV:** one convention with tones, bulleted consequences, the exact target, a *"what is **not** affected"* note, and **type-to-confirm** for irreversible actions.

> **Ours:** `confirm("Approve session 20260812_131757_589?")` — the user cannot see that this writes six tables.
> **DEV:**
> ```
> Approve session 20260812_131757_589?
> All 6 snapshots move to history and leave the parked queue
>   • ARS_ALLOC_HISTORY        • ARS_MSA_TOTAL_HISTORY
>   • ARS_LISTING_WORKING_HISTORY • ARS_MSA_GEN_ART_HISTORY
>   • ARS_LISTING_HISTORY      • ARS_MSA_VAR_ART_HISTORY
> [ Approve session ]
> ```

Deleting a user also lists what **survives**: *"Their role grants and live sessions go with the account. **Audit rows they created are kept.**"* Killing a SQL session requires the id to be **typed** before the button unlocks.

> **Known gaps in DEV:** "Kill session" and "Permanently delete session" on Listing Logs still lack the type-to-confirm gate, and the Dev Sync page still uses old browser pop-ups.

*Verified:* 36 files use it in DEV; **32 pages here still use native `confirm()`**.
**Status: NOT PORTED · LOW risk but WIDE — one mechanical sweep**

---

# M16 — Dev Sync Manager
**Risk: ⚠ REGRESSION — we have it, DEV deleted it** ✅

### 16.1 DEV removed six files we still have
**Ours:** one-way table refresh **PROD → DEV**, superadmin only. Discovery, per-table config, incremental append and full reconcile, row-count skip, bulk operations, two schedules. Source is read-only, it **refuses to run when source and target are the same server**, and each table runs in its own transaction.
**DEV:** six files gone, plus the `dev_sync` block in `app_settings.json` (servers, linked-server name, both schedules). Removed from the router, routes and sidebar.

> **Backup does not replace it:**
> | | Dev Sync | Backup + Restore |
> |---|---|---|
> | granularity | per table | whole database |
> | frequency | daily incremental | per restore |
> | keeps dev-local data | **YES** — only listed tables touched | **NO** — replaces everything |

*Verified:* `dev_sync_service.py` and `dev_sync.py` exist here, absent in DEV.
**Status: KEEP OURS — decide explicitly: restore, retire on record, or build the gap. Do not let it disappear as a side effect of merging.**

---

# M17 — Central RDC Pool *(ours — DEV does not have it)*
`rdc_split_service.py` · `rdc_split_report.py` · `ARS_ALLOC_RDC_SPLIT` · 21 validation scripts · full spec set

Club stock across all warehouses into one pool, allocate with unchanged rules, then tag which warehouse ships each line. Validated at 455 stores × 21 MAJ_CATs: **+38.1%** (36,426 → 50,311 pcs), 45.3% cross-shipped, Own byte-identical across 198,132 rows.

**Status: OURS — must survive the merge. Also carries the FS-11 stamp fix DEV reverted (see 2.4).**

---

# Port order

| # | Module | Why here |
|---|---|---|
| 1 | `run_migrations.py` | everything else is applied through it |
| 2 | **M10** Dictionary + docs | no code impact, immediate value |
| 3 | **M06.2** `OPT_STATUS` archive (`035`) | M06 depends on it |
| 4 | **M06** Alloc Review | **the instrument you validate M01/M02 with** |
| 5 | **M11** Dashboard performance | read-only, fixes a user-visible timeout |
| 6 | **M03** Growth matrix (`031`–`034`) | additive; Cap Ladder builds on it |
| 7 | **M04** Merge Rules (`027`, `028`) | ⚠ needs the 130% sign-off first |
| 8 | **M12** Upload guard | **only after M04** |
| 9 | **M07** Backup Manager | self-contained |
| 10 | **M08** `multi` rule type · **M13** FA/CONS | low risk |
| 11 | **M05** Grid Builder | opt-in, defaults inert |
| 12 | **M14** SQL procs | watch the ordinal shift |
| 13 | **M15** ConfirmDialog | wide but mechanical |
| 14 | **M01 + M02** Listing + Rule engine | **hardest — moves stock, same files as M17** |
| — | **M16** Dev Sync | decide separately — do not delete by accident |
| — | Table cleanup (`028_cleanup`) | **do not run blind** — 47 tables, dry-run only, needs its own decision |

## Before go-live — five decisions

1. **Merge M04 before M12** — sequencing trap; M12 alone protects nothing while reporting success
2. **Reset two switches to safe defaults** — `ALC_CAP_LADDER` → `OFF`, `ALC_REV_MBQ_GROWTH_SRC` → `FLAT`. Until then any comparison measures the wrong thing
3. **Sign off the 130% ceiling** — ~415,000 combinations, no on/off switch
4. **Switch on `LST_OPT_STATUS_STAMP`** — one rule, three blank screens
5. **Run one full allocation comparison against a restored production snapshot** — the engine fixes are proven by tests, not by real before/after output. **Nobody has yet measured how many pieces actually move.** This is the single biggest open gap

## Validating each port

```bash
python scripts/replay_session.py --source <session_id> --mode own
python scripts/compare_three_way.py --base <a> --own <b> --all <c>
python scripts/diag_own_drift.py <before_sid> <after_sid>
```

`replay_session.py` re-runs a past session's exact parameters and **refuses to run if any non-scope key differs**. Remember that **parking does not consume stock — only approving does** — so two parked runs back to back see identical stock and compare cleanly. Note the source grid is rebuilt through the day, so compare runs from the same window.
