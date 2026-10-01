# GRT ALC · Bin-to-Bin (B2B) — ARS Manual

*Send GRT stock that is sitting in warehouse bins to the stores that are short of it,
and hand the warehouse a pick list: which bin, which article, how many, to which store.*

> **Status (2026-10-01): all 5 phases built.** Overview, Upload Data, Settings, Build
> MBQ, Run Allocation, Sessions & Pick List, Gap Report, Store × Article Extract and Help
> are live under **GRT ALC** in the sidebar (`/bin-alloc/*`). Next: three matching cycles
> beside the Streamlit tool, then retire it (§L).
>
> **Source:** the `GRT_ART_ALLOC` Streamlit tool on HOPC866 and its docs 00–05
> (tables `B2B_*`). ARS rebuilds it natively with the prefix **`ARS_B2B_*`**, so both
> can run side by side on the same database without touching each other's tables.
>
> **Replaces** the July 2026 "Bin Allocation" spec (whole-carton, no-split, threshold
> matching). That spec was never built; see Recorded rules.

---

## BRD — Why this exists

GRT (goods returned / transferred) stock lands in warehouse bins, one article per bin
row. Stores keep asking for more of the same categories. Today a planner runs the
Streamlit tool on one machine, one warehouse at a time, and nobody else can see what
was sent, why a bin stayed full, or which settings were used.

**What goes wrong today (measured on live data, 29 Sep 2026):**

| Problem | Example |
|---|---|
| Stores silently dropped | The 1 Sep workbook's REQ asks for 5.2 M units; 195 stores (2.4 M units, 46.5%) are missing from Store Master, so the tool never considers them — no error. |
| Warehouse lost | Bin codes in the workbooks carry no warehouse (`A2-0101-B1`), so a DH24 bin and a DW01 bin with the same code look identical. |
| Columns swapped | The 31 Aug, 1 Sep and 8 Sep DH24 workbooks have Store Master's `RDC` and `OLD\NEW` data in each other's columns (the 26 Sep workbook is correct). |
| Cross-warehouse shipping by accident | The warehouse rule defaults to **Any warehouse**; one recent session shipped 51.1% of units across warehouses. |
| Settings invisible | 5 of the 19 settings were never saved, so the tool quietly used code defaults. |

**Objectives:**

| # | Objective | How ARS meets it |
|---|---|---|
| B1 | No silent data loss | Every upload is checked first; problems are shown with counts, units and examples before anything is written. |
| B2 | Right warehouse | Each workbook declares its warehouse; bins are stored as `DH24-A2-0101-B1`. |
| B3 | All-or-nothing loads | One database transaction per load; cancel or error leaves every table unchanged. |
| B4 | Visible settings | Each setting shows where its value came from (Streamlit / default / changed here). |
| B5 | Speed | Workbook read once, staged, then loaded in 10,000-row batches (1.35 M rows in 52 s). |
| B6 | Audit | Every upload, build and session is a row with who, when, settings used and results. |

---

## FSD — How it works

```text
 1 Upload Data ──► 2 Settings ──► 3 Build MBQ ──► 4 Run Allocation ──► 5 Sessions & Pick List
 Bin Master        19 values      store × article   three caps,           picks + leftovers
 Store Master      Demand/Alloc   target & shortfall bins → stores         per bin, per store
 REQ
```

### A. Inputs (Step 1 · Upload Data)

One Excel workbook with three sheets. Pick it from the network folder or upload it.

| Sheet | Table | Grain | Key columns |
|---|---|---|---|
| Bin Master | `ARS_B2B_BIN_MASTER` | article × bin | `ART, BIN, QTY, MAJ_CAT, SIZE` + `BIN_RDC` |
| STORE MASTER | `ARS_B2B_STORE_MASTER` | store | `STORE_CODE, ST_NM, OLD_NEW, RDC` |
| REQ | `ARS_B2B_REQ` | store × category × size | `STORE_CODE, MAJ_CAT, SIZE, ACC_D, REQ` |

**Header aliases** (a note, not an error): `ARTICLE`/`ARTICLE_NUMBER` → `ART`,
`ACS_D` → `ACC_D`, `STORE_CODE`/`ST_CD` → `Store_Code`.

**Modes:** **Overwrite** empties the chosen tables and loads the file. **Append** adds
rows on top (use it only to add a second warehouse's Bin Master and Store Master).

**Warehouse of the bins.** The page pre-fills it from the file name (`DH24.xlsx` → DH24).
Every bin code without a warehouse-shaped prefix (`^[A-Z]{2}\d{2}-`) is stored with it:
`A2-0101-B1` → `DH24-A2-0101-B1`, and `BIN_RDC = DH24`. A bin that already has a prefix
keeps it. No warehouse known → the check blocks (`BIN_WAREHOUSE`).

**Swapped Store Master columns.** If `RDC` holds OLD/NEW/UPC values and `OLD\NEW` holds
warehouse codes, the check blocks (`STORE_SWAP`) and offers **"Swap them for this load
and check again"**. Nothing is swapped without that click.

### B. Validate first, then load

A check reads the workbook once, cleans it, runs every check, and stages the clean
sheets as parquet (`backend/uploads/b2b_stage/<upload id>/`, git-ignored). **Load** then
writes the staged data — the workbook is never read twice.

| Level | Meaning | Load allowed? |
|---|---|---|
| Must fix (block) | The data would load wrong | No |
| Warning | It loads, but something will be lost or doubled — read it | Yes |
| Note | For information | Yes |

| Code | Level | What it catches | Example |
|---|---|---|---|
| SHEET | block | A sheet is missing | No sheet named like "REQ" |
| COLS | block | A required column is missing | `article` empty in the DW01 draft |
| WIDTH | block | A value longer than the column | a 25-character BIN |
| NUMBER | block/warn | Text in a number column | `QTY = "12 pcs"` |
| BLANK | block | A key column is empty | REQ row with no STORE_CODE |
| NEGATIVE | warn | A negative quantity | `QTY = -3` |
| STORE_SWAP | block | RDC and OLD\NEW swapped | RDC column says `OLD` |
| BIN_WAREHOUSE | block | Bins with no known warehouse | file named `bins.xlsx`, no prefix |
| BIN_PREFIX | note | Bins that will get the prefix | 1,867 bins → `DH24-…` |
| G5 | block | Duplicate store code | `HB05` twice |
| APPEND_KEY | block | Append adds a store already loaded | use Overwrite |
| G4 | block | An article under two categories or sizes | ART 1102 in M_TEES and M_SHIRT |
| DUP_BIN | warn | Same article + bin on two rows (stock doubles) | `1102 @ DH24-A2-0101` |
| G8 | warn | Append would load an article + bin twice | second copy of DH24 |
| ZERO_QTY | note | Bin rows holding zero | ignored later |
| DUP_REQ | warn | Same store × category × size twice | requirement doubles |
| ACC_D | warn | ACC_D blank → target zero | |
| G1 | warn | REQ stores missing from Store Master | 199 stores, their units can never ship |
| G2_DRIFT | warn | Same category spelled two ways | `M-TEES` ≠ `M_TEES` |
| G2 | warn | Categories only in one sheet | carry bags (stock, no REQ) |
| G2_SIZE | warn | Stock in a size the REQ never asks for | cap is zero, cannot ship |
| G3 | warn | Bins in a warehouse with no stores | DW01 bins, only DH24 stores |

**Load gates** (`start_load`): the upload must be CHECKED with 0 blocking checks; the
check must be under 24 hours old; no other upload may have been loaded since the check
(the comparisons would be stale); the staged data must still exist. A failed gate marks
the upload **EXPIRED** and deletes its stage.

**Load:** one raw-connection transaction — `TRUNCATE` (Overwrite) and all inserts in
10,000-row batches, then one commit. SQL Server's TRUNCATE is transactional, so a
cancel or error rolls **every** table back. A cancelled load returns to CHECKED and can
be loaded again; a failed load is FAILED.

Upload statuses: `QUEUED → CHECKING → CHECKED → LOADING → LOADED`, or `FAILED`,
`CANCELLED` (check cancelled), `EXPIRED`. One job runs at a time.

### C. Settings (Step 2)

19 values in `ARS_B2B_SETTING`. On first use they are copied from the Streamlit tool's
`B2B_MBQ_SETTING` (`SEED_SOURCE = LEGACY`); a key it never saved gets the code default
(`DEFAULT`); a value saved on the page is `USER`. A save validates every value first
and writes nothing if any is bad.

| Group | Key | Default | Meaning |
|---|---|---|---|
| Demand | SHORT_DAYS | 60 | Norm days for fast sizes |
| Demand | LONG_DAYS | 90 | Norm days for other sizes |
| Demand | SHORT_SZ_LIST | A,A_MIX,NA | Which sizes are fast |
| Demand | TREAT_MAJCAT_MIX_AS_SHORT | On | `*MIX` categories use fast days |
| Demand | DEFAULT_SALE_COVER_DAYS | 30 | Cover days where REQ has none |
| Demand | TREAT_MISSING_STK_AS_ZERO | Off | No stock row → stock 0 |
| Demand | CONT_SOURCE_MODE | HYBRID | Size share from REQ, master, or both |
| Demand | CONT_APPLY | On | Split ACC_D by size share |
| Demand | CONT_FULL_SZ_LIST | A,NA | Sizes forced to 100% (MASTER mode only) |
| Demand | CONT_FALLBACK | 1 | Share when no row exists (MASTER mode only) |
| Demand | MBQ_MIN_WHEN_CONT | 1 | Smallest target when share > 0 |
| Demand | MBQ_PRUNE_TO_REQ | On | Build only rows a store can take |
| Demand | MBQ_MIN_REQ_UNITS | 1 | Minimum REQ to build a row |
| Allocation | ALLOC_PRIORITY | SHORTFALL_DESC | Who is served first |
| Allocation | ALLOC_MIN_QTY | 1 | Smallest line |
| Allocation | ALLOC_FILL_MODE | GREEDY | Greedy or round robin |
| Allocation | ALLOC_FAIR_BASIS | PROPORTIONAL | Re-order between articles (round robin only) |
| Allocation | ALLOC_BIN_PICK | MAX_CONSUMPTION | Which bin a unit comes from |
| Allocation | ALLOC_CROSS_RDC | ANY | Which warehouse may ship (Any / Home first / Home only) |

A **Demand** change makes the MBQ build out of date (rebuild in Step 3). An
**Allocation** change only needs the next run. `ALLOC_CHUNK_BY` from the tool is dropped.

### D. Demand — MBQ (Step 3, built 2026-09-30)

One row per store × article in `ARS_B2B_ART_MBQ`: every article in Bin Master against
every store in Store Master, pruned (`MBQ_PRUNE_TO_REQ`) to the store + category + size
lines whose REQ is at least `MBQ_MIN_REQ_UNITS`. A REQ store missing from Store Master
gets **no rows at all** — that JOIN is the Store Master gate (upload check G1).

```text
NORM_DAYS   = SHORT_DAYS if the size is in SHORT_SZ_LIST (or a *MIX category) else LONG_DAYS
CONT_EFF    = the size share, per CONT_SOURCE_MODE (HYBRID: store/company Master_CONT_SZ
              curve where positive, else the store's REQ mix; a size with REQ 0 → 0;
              rescaled so each store + category adds up to 100%)
ACC_D_EFF   = ACC_D × CONT_EFF                (ACC_D is per store + category: MAX over sizes)
MBQ_RAW     = ACC_D_EFF + (ACC_D_EFF ÷ NORM_DAYS) × SALE_COVER_DAYS
MBQ         = MBQ_RAW, lifted to MBQ_MIN_WHEN_CONT when CONT_EFF > 0
MBQ_ROUNDED = ROUND(MBQ, 0)
SHORTFALL   = MAX(MBQ_ROUNDED − STK_TTL, 0)   ← what allocation may fill
EXCESS      = MAX(STK_TTL − MBQ_ROUNDED, 0)
```

SHORTFALL and EXCESS come off the **rounded** target — stock moves in whole units — and
are blank when `STK_TTL` is (no grid row, `TREAT_MISSING_STK_AS_ZERO` off).
`STK_TTL` = SUM of `ARS_GRID_MJ_VAR_ART.STK_TTL` per (WERKS, ARTICLE_NUMBER) — the key
is not unique in the grid. `SZ` = the grid's SZ, else the bin's size (`SZ_SOURCE`).
`SALE_COVER_DAYS` = the REQ line's value, else `DEFAULT_SALE_COVER_DAYS` (the workbook
has no such column, so today every row uses the default).

Example (HM29 / 1122103517001, build 2): ACC_D 16, share 100%, size A → 60 days, cover
10 → MBQ = 16 + 16 ÷ 60 × 10 = 18.67 → 19. Stock 1 → **SHORTFALL 18**.

**How the build runs** (`b2b_mbq_service.build_into`), a background job with progress
and cancel, one at a time, never while an upload is loading (and a load never starts
while a build runs):

1. Temp tables, as the tool: `#art` (one row per article), `#need` (REQ ⋈ Store Master,
   pruned), `#acc`, `#stk` (grid, filtered to Store Master stores and Bin Master
   articles), the size-share tables for the mode.
2. **Rows go into `ARS_B2B_ART_MBQ_STAGE`**, never into the live table: a clustered
   columnstore, inserted on 16 threads (`BUILD_MAXDOP`), then the primary key
   (STORE_CODE, ART) is added. The live table stays readable the whole time.
3. **Checks on the staged rows** (validate first, then create):
   | Code | Level | What |
   |---|---|---|
   | MBQ_FORMULA | must pass | every row's SHORTFALL/EXCESS = MBQ_ROUNDED vs STK_TTL, MBQ_ROUNDED = ROUND(MBQ), nothing negative, nothing set where stock is unknown |
   | MBQ_SHARES | must pass | every store + category size mix adds up to 100% (± 0.00005); n/a in MASTER mode |
   | MBQ_STOCK | warn/info | rows with no grid row — counted as zero stock (on), or left out (off) — and the shortfall that rests on it |
   | MBQ_ACC_D | warn | rows with no ACC_D, so no target |
   | MBQ_UNREACHABLE | warn/info | bin stock no store can receive, by the tool's leftover reasons (NOT_IN_REQ, NO_SEASON_REQ, NO_STORE_MASTER, NO_STORE_NEED) |
4. A build that fails a must-pass check is **never switched in**. One that passes
   replaces the live table in one metadata-only transaction (`TRUNCATE` + `ALTER TABLE
   … SWITCH`): readers wait an instant, and a failure or cancel before it changes
   nothing — the previous build stays in use.
5. `ARS_B2B_MBQ_BUILD` records the upload it read, the 13 demand settings, when the
   grid and Master_CONT_SZ last changed, seconds per step, the summary (totals, by
   category, size-share sources, unreachable stock) and the checks.

**Freshness** (Build MBQ page and Overview G7): *out of date* when a newer upload was
loaded or a demand setting changed since the build; *stock has moved* when the grid
changed after it (from `sys.dm_db_index_usage_stats`, falling back to the table's
`modify_date` when an ALTER has reset that DMV).

**The page** (`/bin-alloc/mbq`): Build/Rebuild with live progress and Cancel;
freshness banner; tiles (rows, rows short, shortfall, bin stock and how much of the
shortfall it covers, excess, stock no store can take); the checks; demand against bin
stock by category; the settings the build used (changes since highlighted); where the
size shares came from; build history; a paged, filterable demand table; and **"Why this
number?"** — click a row (or type store + article) to walk it through every step, or to
see why it has no row at all (store not in Store Master, article not in Bin Master, REQ
below the minimum).

**Measured** (12.7M rows, upload 8, 30 Sep): build 131 s end to end; the write is the
cost, disk-bound. Columnstore on 16 threads: 75 s and 0.9 GB against 170 s and 6.2 GB
for the tool's rowstore + indexes; ROW / PAGE compression were slower (228 s / 258 s).
Browse pages 0.2–0.7 s (the page is chosen on the key and sort columns, then full rows
fetched by key; `OPTION (RECOMPILE)` lets the top-N sort see the page size).

**Parity with the tool** (`backend/scripts/b2b_mbq_parity.py`): the ARS build run over
the tool's own `B2B_*` inputs gives the same **19,061,643** rows as its `B2B_ART_MBQ`,
with 0 differences on every input-derived column (CONT, CONT_EFF, ACC_D_EFF, …), on
NORM_DAYS / MBQ_RAW / MBQ / MBQ_ROUNDED, and on SHORTFALL / EXCESS wherever the stock is
the same. The only differences are the grid moving after the tool's build (182,635
grid rows appeared, 171,881 went, net +131,545 pcs).

### E. Allocation (Step 4, built 2026-10-01)

Each unit sent respects three caps at once:
1. ≤ the store's SHORTFALL for that article;
2. ≤ the store's REQ for that category + size (shared by all articles in it);
3. ≤ what the bins hold (per warehouse × article).

Example: MBQ 12, store holds 0 → shortfall 12; its REQ for the category + size is 50;
the bins hold 100 → it gets **12**. The 50 is shared by every article of that
category + size at that store; the 100 by every store wanting the article.

**The choices** (page `/bin-alloc/run`; saved settings pre-fill all but one):

| Choice | Options |
|---|---|
| How stock is shared | **In priority order** (GREEDY: each line takes all it can) · **Share — furthest from its own REQ first** (ROUND_ROBIN + PROPORTIONAL) · **Share — fewest units so far first** (+ FLAT) · **Share — priority order, no re-order** (+ NONE) |
| Which warehouse may ship — **required, no default (G11)** | **Own RDC only** (SAME) · **Own RDC first** (HOME_FIRST) · **All RDCs** (ANY). The page shows which warehouses hold bins and which stores have none at home |
| Who is served first | Biggest shortfall · Highest size share · Biggest target; then the smallest line (also the step when sharing) |
| Which bin each unit comes from | **Fewest picks** (MAX_CONSUMPTION: the smallest bin covering the line, else largest bins first) · **Bin order** |
| Dry run | computes and checks everything, writes no lines |

Ranking, ties (… then STORE_CODE, ART), both fill modes, the warehouse pools and both
bin picks are the tool's `allocate.py`, rule for rule.

**Gates before a run:** a demand build exists and is not out of date (newer upload,
changed demand setting); no upload loading, no build running, no other session
running. While a session runs, a load or a build is refused too.

**How it runs** (`b2b_alloc_engine.allocate_all`): one task per category on 8 worker
processes, biggest first. That is exact, not approximate: no cap crosses a category
(an article has one category and one size — upload check G4 — the REQ cap is keyed on
category, and a bin holds one article), so each task carries all the state its own
decisions need, and its bins are touched by no other task. Each task reads only its
category (index `IX_ARS_B2B_BIN_MASTER_CAT`, REQ's MAJ_CAT index, and a narrow read of
the demand table), allocates, and picks its bins. ALLOC_SEQ is then numbered once over
every line, as the tool's `_finalise` does.

**Measured** (build 3, 12.5M candidates, 474 categories, DB on HOPC866 read from
HOPC575): the tool 243–496 s; ARS **68–92 s**. The run is network-bound — the per-category
arithmetic is under a second; reading candidates is the cost. Reading only the columns a
decision uses, codes as VARCHAR, doubled throughput (93k → 200k rows/s across 8
readers); bigger TDS packets did not help; a category-ordered copy did not help (the
server scans in 0.02 s). RDC, MAJ_CAT and BIN_SIZE come from what the task already
holds (Store Master, its category, Bin Master), SZ / STK_TTL are fetched once for the
lines that received stock (`enrich`).

**Parity with the tool** (`backend/scripts/b2b_alloc_parity.py`, which imports the
tool's own `allocate.py` read-only and feeds both engines the same tables): every
category, tool defaults — **114,014 lines, 204,143 picks, 273,937 units identical**, every
column, every skip count. Eight more combinations on 40 categories (both fill modes, all
fair bases, all warehouse rules, both bin picks, all priorities, min qty 1–3): all
identical. The script refuses to report if the demand or inputs change mid-test.

**Writing** (not on a dry run): lines → `ARS_B2B_ALLOC`, picks and leftovers →
`ARS_B2B_BIN_PLAN`, all in **one transaction**, then the checks run on the stored rows
before commit. Cancel or failure → nothing written. Sessions are never overwritten; a
session is deleted (all three tables, one transaction) only on request, typing DELETE.

**G9 — balance checks on every session** (dry runs get the in-memory ones):

| Code | Checks |
|---|---|
| G9_PICKS | pick-list units = allocated units, nothing untraceable |
| G9_REQ | no store × category × size over its REQ |
| G9_BINS | no bin gives more than it holds |
| G9_SHORT | no line over the store's shortfall |
| G9_RECONCILE | picks + leftovers = Bin Master, bin by bin (stored runs) |
| G9_RDC | under Own RDC only, no pick crosses warehouses |

A session with a failed check is kept, marked FAIL; its export is blocked (Phase 4).

**Recorded on the session:** the build it read (G10), every choice, workers, supply /
allocated / left / cross-warehouse units, lines, picks, leftover rows, stores served,
skip counts by reason, the checks, leftover pieces by LEFT_REASON, the categories that
received most, seconds per step.

`STILL_SENDABLE = MIN(RESIDUAL_SHORT, REQ_CAP_LEFT)` is the only column that answers
"can more ship?". Under sharing, ART_BIN_LEFT / REQ_CAP_LEFT / ALLOC_CUM are end-of-run
balances (allocation is interleaved), as in the tool.

### F. Sessions & Pick List (Step 5, built 2026-10-01)

`ARS_B2B_BIN_PLAN` holds picks (`ROW_TYPE = ALLOC`: bin, article, store, `PICK_SEQ`,
`BIN_QTY_LEFT` counting down) and leftovers (`ROW_TYPE = UNALLOC`) in one table, so
one sum reconciles against the bins. Both `STORE_RDC` and `BIN_RDC` are kept.

`LEFT_REASON` on a leftover: `NO_STORE_MASTER` (stores asked, but none is in Store
Master — **fix: reload Store Master**), `NO_SEASON_REQ` (every store asked for zero),
`NOT_IN_REQ` (category not in REQ — bags, hangers), `NO_SHORTFALL`, `REQ_CAP_FULL`,
`PARTIAL`, `NO_STORE_NEED` (catch-all).

**The page** (`/bin-alloc/sessions?id=N`, `b2b_sessions_service`):

- **Session bar** — pick any session; *Compare with…*; *Pick-list workbook*, *Pick list
  CSV*, *Leftovers CSV*; *Delete* (type DELETE; removes the session from all three tables
  and its cached files).
- **Header** — every choice the run was given, the totals (allocated of supply, left,
  lines, pick rows, stores served and how many got nothing, cross-warehouse units and
  share), the balance checks as chips, and **Where the stock went**: units and stores
  per bin warehouse → store warehouse, cross pairs marked.
- **Pick list** — in warehouse **walking order** (BIN_RDC, BIN, ART, PICK_SEQ), filters
  by store, bin, article, category, bin warehouse, cross-warehouse only; cross rows tinted.
- **Lines** — store × article in the order served, with target, stock, shortfall, REQ
  cap, units sent, what the article had left, STILL_SENDABLE. A row opens its story.
- **Leftovers** — pieces left by reason (cards that filter), then every bin position.
- **Why no stock?** — a store, or a store × article:
  1. in Store Master? (else nothing can ever reach it)
  2. what the session sent it, by category; for a store alone, what it was short of
     and its 20 biggest shortfalls that got nothing (each clickable)
  3. the article: bin stock at the session, the store's demand row (none = REQ below the
     minimum), its line and bins if it got one
  4. if it got none and the session's inputs are still current (same build, nothing
     newer loaded), **its category is replayed exactly** with a tracer
     (`run_category(…, trace=)`): the replay must equal what the session stored (lines
     and units) — shown as a check — and then says what happened at the store's turn:
     the article was gone (and which stores ahead took it), its REQ for the category +
     size was already filled (and by which articles), the warehouse rule kept it out, or
     the line would have been below the smallest. Under sharing: who shared it, and
     whether stock or the REQ ran out first. When the data has moved on, it says so and
     does not guess.
- **Settings used** — every choice, the build and upload it read, workers, seconds per
  step, skip counts.
- **Compare** — two stored sessions: totals and their change, store × article lines (same,
  changed, only in one), stores that gain / lose most, categories that change most; a
  warning when the two read different demand builds.

**Export** — locked unless the session is a stored run (not a dry run) whose balance
checks all passed. Built once per session, kept under `backend/uploads/b2b_export/`
(git-ignored; a session never changes). The **workbook**: Summary (choices, totals,
checks), *Pick list* in walking order with a CROSS column, *Store totals*, *Lines*,
*Leftovers by reason × category*. The full leftover list (≈440k rows) is a separate CSV:
xlsxwriter manages ~5k rows a second here, and it doubled the workbook time (measured
137 s → 65 s without it). Pick list CSV 5 s, leftovers CSV 9 s.

Measured on session 5 (114,014 lines, 204,143 picks): pages 0.13–0.3 s; store walk 4.8 s;
article replay 5–6 s; compare 0.5 s; workbook 65 s the first time, then instant.

### G. Overview page

Pipeline stepper, four tiles, **Needs attention** (live checks on the loaded tables:
G1, G2_DRIFT, G2, G3, G4, G8, G6, G11 warehouse rule, G7), recent uploads, and a
side-by-side count of the Streamlit tool's `B2B_*` tables. While a load runs it returns
only the load's progress (the tables are locked).

### J. Gap Report (built 2026-10-01, `b2b_gap_service`)

The tool's `gap_report.py`, same sheets and meaning, read from `ARS_B2B_*`. The key is
(MAJ_CAT, ISNULL(SIZE, '')) — the build's and the REQ cap's — and REQ is split into
**servable** (store in Store Master) and **orphan** (not: never fillable). COVER_PCT is
against servable REQ.

| Sheet | Shows |
|---|---|
| 01_Summary | headline totals and every verdict class |
| 02_Coverage_Matrix | every category + size in either sheet with its verdict: **A** stock, no REQ · **B** REQ only from orphan stores · **C** REQ, no stock · **D** REQ, not enough stock · **E** stock above REQ · **F** balanced |
| 03–06 | slices of 02: C, D (with an even-share figure), A (out of season vs not merchandise), E |
| 07_Stores_Missing_Master | REQ that can never be filled, by store |
| 08_Stores_No_REQ | Store Master stores that asked for nothing |
| 09_Store_Summary | per store for a session: asked, received, unmet, fill % |
| 10_Store_Cat_Gap | per store × category × size for a session: unmet REQ (capped at 50,000 rows) |
| 11_Key_Mismatch | category / size values in one sheet only — silent join failures |
| 12_Leftover_Reasons | a session's leftovers by reason |
| **13_Warehouse_Balance** (new) | per RDC: bin stock, stores, servable REQ, and for a session units from own bins, from other RDCs, and sent to other RDCs' stores |

The matrix is built once into a temp table and every sheet slices it (the tool rebuilt
it per sheet). Results are cached per (latest load, session); 4–8 s to build, 14 s for
the workbook. Page `/bin-alloc/gap-report`: session picker (or none), headline tiles,
verdict cards, the warehouse balance, a tab per sheet (first 200 rows), CSV per sheet,
the whole report as Excel.

On upload 8: **682,616 of the 1,019,722 bin pcs (67%) are in category + sizes where stock
already exceeds servable REQ** (verdict E, 82,437 units of REQ); the categories stores
want (D) hold 193,608 pcs against 6.2M REQ. That — not the settings — is why about 27%
of the stock allocates. DW01 has 198 stores, 1.98M servable REQ and no bins.

### K. Store × Article Extract (built 2026-10-01, `b2b_extract_service`)

The tool's `extract.py`: every row of a vetted table for a store list and an article
list. Vetted = the ten `ARS_B2B_*` tables and the two sources (the grid, Master_CONT_SZ);
anything else is refused before SQL is built. Store / article / session columns are found
per table (STORE_CODE / ST_CD / WERKS; ART / ARTICLE_NUMBER); a table without one ignores
that list and says so. Lists are staged in indexed temp tables (`COLLATE
DATABASE_DEFAULT`), so any length works. Codes are cleaned as they arrive from Excel
(`1111090026.0` → `1111090026`, spaces, case — the database compares without case).
**Count first**: the page shows the row count, which codes matched, and **lists the codes
not found**, then downloads CSV or Excel (Excel refused above 1,048,575 rows). The grid's
ARTICLE_NUMBER is BIGINT: the codes are converted, not the column, so it still seeks.

### L. Retiring the Streamlit tool

Per the plan: run both on the **same workbook** with the **same settings, written down**,
three cycles in a row; each time compare the demand (`b2b_mbq_parity.py` — same rows and
values) and the allocation (`b2b_alloc_parity.py` — same lines and picks). Three matching
cycles, then the tool is switched off and its `B2B_*` tables are left as they are.
Matching so far: demand on 19.06M rows (30 Sep); allocation on the full data and 8
combinations (1 Oct).

### H. Tables

| Table | Grain | Written by |
|---|---|---|
| `ARS_B2B_UPLOAD` | one per check/load | Step 1 |
| `ARS_B2B_BIN_MASTER` | article × bin | Step 1 |
| `ARS_B2B_STORE_MASTER` | store | Step 1 |
| `ARS_B2B_REQ` | store × category × size | Step 1 |
| `ARS_B2B_SETTING` | setting | Step 2 |
| `ARS_B2B_MBQ_BUILD` | one per build | Step 3 |
| `ARS_B2B_ART_MBQ` | store × article (clustered columnstore, PK nonclustered) | Step 3 |
| `ARS_B2B_SESSION` | one per run | Step 4 |
| `ARS_B2B_ALLOC` | session × store × article | Step 4 |
| `ARS_B2B_BIN_PLAN` | session × bin × article × store | Step 4 |

DDL: `backend/scripts/037_b2b_module.sql` (idempotent), applied by
`b2b_schema.ensure_tables()` on first use; **Repair tables** on the Overview re-runs it.

### I. Code map

| Area | File |
|---|---|
| DDL | `backend/scripts/037_b2b_module.sql`, `app/services/b2b_schema.py` |
| Settings | `app/services/b2b_settings.py` |
| Upload / check / load | `app/services/b2b_upload_service.py` |
| Overview | `app/services/b2b_overview_service.py` |
| Build MBQ, browse, explain | `app/services/b2b_mbq_service.py` |
| Allocation engine (pure) | `app/services/b2b_alloc_engine.py` |
| Run sessions, gates, G9, write | `app/services/b2b_alloc_service.py` |
| Review: lines, picks, leftovers, why, compare, export | `app/services/b2b_sessions_service.py` |
| Gap Report | `app/services/b2b_gap_service.py` |
| Store × Article Extract | `app/services/b2b_extract_service.py` |
| Parity with the tool | `backend/scripts/b2b_mbq_parity.py`, `backend/scripts/b2b_alloc_parity.py` |
| API (`/b2b`) | `app/api/v1/endpoints/b2b.py` |
| Pages | `frontend/src/pages/b2b/*`, `components/b2b/*`, `BinAllocHelpPage.jsx` |

---

## Phases

| Phase | Scope | Status |
|---|---|---|
| 1 | Tables, settings, upload with checks, overview, help | **Built 2026-09-29** |
| 2 | Build MBQ as a job, build record, checks, browse, "why this number" | **Built 2026-09-30** — parity with the tool proved |
| 3 | Allocation engine (parallel by category), sessions, G9, dry run, run page | **Built 2026-10-01** — parity with the tool proved (full run + 8 combinations) |
| 4 | Sessions, pick list, leftovers, why no stock, compare, export | **Built 2026-10-01** |
| 5 | Gap report (13 sheets), store × article extract, help rewritten | **Built 2026-10-01** |

**Open decisions:** D2 owner of the `ARS_BIN_*_DHYANU` tables; D4 whether DH24 and
DW01 may ship to each other (sets the ALLOC_CROSS_RDC default); confirm `ACS_D` is the
same figure as `ACC_D`; install `python-calamine` for faster workbook reads (63–122 s
with openpyxl today).

---

## Recorded rules

- **2026-07-24** — Original "Bin Allocation" spec (whole-bin, no split, eligibility
  thresholds) and sidebar scaffold. Superseded 2026-09-29; never built.
- **2026-09-29** — Module rebuilt as **GRT ALC · Bin-to-Bin**, ported from the
  `GRT_ART_ALLOC` Streamlit tool. Tables use the **`ARS_B2B_*`** prefix so both tools
  share HOPC866 without conflict. Routes stay under `/bin-alloc/*`; the July
  placeholders (bin-master, requirement, results, log) redirect to the new pages.
- **2026-09-29** — **Validate first, then load.** A check stages clean data; a load
  writes only a CHECKED upload with 0 blocking checks, under 24 h old, with no newer
  load since the check. Otherwise the upload is EXPIRED and its stage deleted.
- **2026-09-29** — **One transaction per load.** TRUNCATE + all inserts commit together;
  cancel or error rolls back every table (proved: cancel at REQ row 10,000 left all three
  tables unchanged). A cancelled load returns to CHECKED and can be loaded again.
- **2026-09-29** — **Bins carry their warehouse.** Each workbook declares its warehouse
  (pre-filled from the file name); unprefixed bin codes are stored as `<RDC>-<code>` with
  `BIN_RDC` set. A combined DH24 + DW01 load is Overwrite with one workbook, then Append
  the other warehouse's Bin Master and Store Master.
- **2026-09-29** — **Swapped Store Master columns block by default** (`STORE_SWAP`); the
  user must choose "swap for this load". The 31 Aug, 1 Sep and 8 Sep DH24 workbooks
  need it; the 26 Sep workbook does not.
- **2026-09-29** — Header aliases accepted with a note: `ARTICLE`/`ARTICLE_NUMBER` → ART,
  `ACS_D` → ACC_D, `STORE_CODE`/`ST_CD` → Store_Code. ACS_D = ACC_D is assumed, not yet
  confirmed by the business.
- **2026-09-30** — **Build MBQ ported statement for statement** from the tool's
  `build_derived.py`, so the same inputs give the same numbers; proved on 19,061,643 rows
  (`b2b_mbq_parity.py`). SHORTFALL / EXCESS are off **MBQ_ROUNDED**, as the tool computes
  them (its SQL comment says MBQ; its code and docs say MBQ_ROUNDED).
- **2026-09-30** — **Stage, check, then switch.** A build writes `ARS_B2B_ART_MBQ_STAGE`,
  runs MBQ_FORMULA and MBQ_SHARES there, and only then replaces the live table with
  `ALTER TABLE … SWITCH`. A failed check, error or cancel leaves the previous build in
  use. (TRUNCATE + a parallel columnstore insert in one transaction deadlocks on its own
  threads — measured — which is why the rows are never written into the live table.)
- **2026-09-30** — `ARS_B2B_ART_MBQ` is a **clustered columnstore** with a nonclustered
  PK (STORE_CODE, ART), written at MAXDOP 16: 2.3× faster and 7× smaller than the tool's
  rowstore. The tool's `(SHORTFALL) INCLUDE` index is not recreated: 98.5% of rows are short.
- **2026-09-30** — Build 2 (upload 8, DH24 bins): 12,715,693 rows; 84,179,222 units short
  against 1,019,722 pcs in bins (1.2% cover). **91.8% of rows have no grid stock row** and,
  with TREAT_MISSING_STK_AS_ZERO = On (the tool's saved value), count as zero stock —
  78.9M of the 84.2M shortfall rests on that assumption. 142,651 pcs (14%) of bin stock have
  no store that can receive them (NO_SEASON_REQ 140,779, NOT_IN_REQ 1,872).
- **2026-09-29** — Settings are seeded from the tool's `B2B_MBQ_SETTING` (LEGACY) or the
  code default (DEFAULT), and every value shows its source. A save validates all values
  before writing any. `ALLOC_CHUNK_BY` is dropped.
- **2026-10-01** — **Allocation ported rule for rule** from the tool's `allocate.py` and
  proved identical on the full data (114,014 lines, 204,143 picks) and on 8 combinations
  of every choice. Each category runs as its own task on 8 processes — exact, because no
  cap crosses a category.
- **2026-10-01** — **The warehouse rule is required on every run** (G11): the API refuses a
  run without it and the page pre-selects nothing. The saved ALLOC_CROSS_RDC no longer
  decides anything silently. Overview G11 now warns only when a stored session actually
  sends stock across warehouses.
- **2026-10-01** — **G9 on every session**, in memory and again on the stored rows inside
  the write transaction; G9_RECONCILE proves picks + leftovers = Bin Master bin by bin. A
  session that fails is kept and marked; export will be blocked.
- **2026-10-01** — With only DH24 bins loaded, **Own RDC first moves 52.7% of units across
  warehouses — the same as All RDCs** — because the 198 DW01 stores have no bins at home;
  **Own RDC only** gives those stores nothing. Loading the DW01 Bin Master (the tool holds
  587,955 rows) changes this; decision D4 is still open.
- **2026-10-01** — The demand table is read narrow (codes as VARCHAR, three numbers):
  the database is across the network and the read is bytes-bound. Run time 68–92 s against
  the tool's 243–496 s on the same data.
- **2026-10-01** — **"Why no stock?" replays, it does not guess.** When a session's inputs
  are still current, the article's category is re-run with a tracer; the replay must equal
  the stored category (lines and units) before its reason is shown. The tracer is one
  comparison per candidate when off; parity with the tool was re-proved after adding it
  (8 combinations, identical). When the data has moved on, the walk says the turn cannot
  be replayed.
- **2026-10-01** — **Export only after the checks.** Workbook and CSVs are refused for a
  dry run or a session with a failed balance check (HTTP 409 with the reason). Files are
  built once and kept; deleting a session removes them.
- **2026-10-01** — Test sessions 5 (in priority order · All RDCs) and 6 (sharing · Own RDC
  first) were created to prove Phase 4; their notes say they can be deleted. Comparing them:
  sharing adds 455 units and 153,688 lines, and moves 7,059 fewer units across warehouses.
- **2026-10-01** — **Gap Report ported** (12 sheets, same meaning) plus **13_Warehouse_Balance**.
  The coverage matrix is built once per report and cached per (latest load, session).
  Finding on upload 8: 67% of bin stock sits where stock already exceeds servable REQ, so
  most GRT stock cannot be allocated under any settings.
- **2026-10-01** — **Store × Article Extract ported**: vetted tables only, temp-table lists,
  count before fetch; adds Excel-code cleaning and the list of codes not found.
- **2026-10-01** — The July placeholder page (`BinAllocPlaceholderPage.jsx`) is removed:
  every GRT ALC route now has its page. Help rewritten for the whole module.
