# Bin Allocation — ARS Manual

*Assign pre-packed DC/vendor bins to the best-fit store, without splitting a bin.*

> **Status:** Spec + navigable sidebar scaffold only (2026-07-24). A new **Bin
> Allocation** section sits **above Adhoc** in the sidebar with five placeholder
> pages (Bin Master · Requirement · Run Allocation · Results & Export ·
> Eligibility Log), all routed to `BinAllocPlaceholderPage`. **No backend / engine
> yet.** This dossier is the canonical spec — build to it.
>
> **Source drafts:** `BRD-Bin-Allocation.md` and `FSD-Bin-Allocation.md` (V2 Retail,
> 24 Jul 2026, owner Akash Agarwal). The **algorithm** in those drafts is adopted
> as-is; the **deployment model** is redesigned ARS-native (see the feasibility note
> immediately below) — the drafts specify a client-only browser tool, which this
> spec supersedes.

---

## Feasibility note — why ARS-native, not client-only (decided 2026-07-24)

The source FSD (§7) specifies a **client-side-only** tool: a React app with a Web
Worker engine, Excel upload for all inputs, and `localStorage` persistence, with an
explicit constraint of "no server-side allocation engine." That is the **opposite**
of every other ARS module and was rejected for this build. The owner chose the
**server-side, ARS-native** model.

| Source FSD (client-only) | Problem in ARS | ARS-native decision |
|---|---|---|
| Web Worker engine in the browser | Every ARS engine (MSA, rule engine, auto-cont, FA&CONS) runs server-side in FastAPI over SQL Server; 50k bins in browser memory is fragile (the FSD itself lists "browser crash" as a top risk) | Engine is a backend service run as a **Jobs Dashboard** job |
| REQ **uploaded** from Excel | REQ (open store requirement) is already computed by the allocation pipeline; re-uploading duplicates live data and goes stale immediately | REQ is **pulled from the latest allocation output**; Excel upload only as a fallback |
| `localStorage` persistence, auto-save every 500 rows | No DB audit trail — violates the ARS "100% of decisions logged" standard | Persist to `ARS_BIN_ALLOC_*` tables; resume from DB job state, not the browser |
| No RBAC, single-planner | Everything else is permission-gated | `BIN_ALLOC_VIEW` / `BIN_ALLOC_RUN` permissions |
| Pause/Resume = a JS flag | Not durable across refresh/crash | Pause/Resume/Stop = **job state** transitions persisted in the session row |

What is kept verbatim from the drafts: the **allocation algorithm** — greedy
best-fit, **no bin splitting**, eligibility % = matched qty ÷ total bin qty, fixed
and cascading thresholds, the two strategies, excess tracking, and the eligibility
audit log. Only the runtime moves from browser to server.

---

## BRD — Why this exists

Bin allocation assigns pre-packed inventory bins (received from the DC / vendor) to
the most appropriate retail stores, such that each bin is allocated **in full to a
single store** (bin integrity preserved — no splitting at the DC), the bin's
contents match the store's open requirement (REQ) at the chosen level, excess to any
store is minimised, and unallocated bins are clearly surfaced for review.

**Pain today:** allocation of thousands of bins across hundreds of stores is done in
Excel with manual VLOOKUPs — hours per cycle, inconsistent "which store fits this
bin" decisions between planners, no audit of *why* a bin went to a store or stayed
unallocated, untracked over-allocation, and lost work if the laptop crashes mid-cycle.

**Objectives (success metric):**

| # | Objective | Metric |
|---|---|---|
| B1 | Reduce planner effort per cycle | ~6 h → < 30 min |
| B2 | Improve bin-to-store fit | ≥ 85% of bins aligned at the primary threshold |
| B3 | Minimise excess per store | excess % < 5% of aligned qty |
| B4 | Full auditability | 100% of decisions logged (eligibility log) |
| B5 | Zero data loss on long cycles | job state persisted; resumable from DB |

**In scope:** bin master (uploaded), REQ (from alloc output or uploaded), store
scope; allocation at **MAJ_CAT / SIZE / MAJ_CAT+SIZE**; two strategies (**one bin →
all stores**, **one store → all bins**); **fixed and cascading** eligibility
thresholds; export of aligned/unaligned/summary/log; pause/resume/stop/partial-export
on a running job.

**Out of scope:** creating the bins at the DC (upstream), dispatch/transport/GRN
(downstream), bin/store master maintenance, and multi-planner concurrent editing of
the same session.

---

## FSD — How it will work (ARS-native)

```text
  Bin Master (upload → ARS_BIN_MASTER)
  REQ (pull from alloc output ─or─ upload → ARS_BIN_REQ)
  Store scope (store master / alloc scope)
            │
            ▼
   Run config (level · strategy · threshold pattern · store order)
            │  POST /bin-alloc/run  → enqueue Jobs Dashboard job
            ▼
   Bin Allocation Engine (backend service, job worker)
   greedy best-fit · no split · fixed/cascading thresholds
            │  progress → job row;  pause/resume/stop → job state
            ▼
   ARS_BIN_ALLOC_ALIGNED / _UNALIGNED / _LOG  (+ ARS_BIN_ALLOC_SESSION)
            │
            ▼
   Results & Export (XLSX / CSV / JSON, incl. partial)  +  audit_log
```

### A. Inputs & master data

**A0. Allocation key.** For each bin row and each REQ row build `ALLOC_KEY`:
- level `maj_cat` → `UPPER(TRIM(MAJ_CAT))`
- level `size` / `maj_cat_size` → `UPPER(TRIM(MAJ_CAT)) + '|' + UPPER(TRIM(SIZE))`

  ⚠ **MAJ_CAT `[A-Z]-` prefix hazard** applies here too — grid/MSA/listing prefix
  ~0.4% of categories with a dashed letter, alloc/hold do not. If REQ is pulled from
  the alloc side and bins carry a listing-side MAJ_CAT, the keys silently mismatch.
  Normalise the prefix on **both** sides when building `ALLOC_KEY`.

**A1. `ARS_BIN_MASTER`** *(new)* — uploaded bin pack list. Grain **BIN × MAJ_CAT ×
SIZE** (multiple rows per bin, one per category/size combination).

| Column | Meaning |
|---|---|
| `SESSION_ID` | the run this bin set belongs to |
| `BIN` | unique bin identifier (a physical pre-packed carton) |
| `MAJ_CAT` | major category |
| `SIZE` | garment size (required for `size` / `maj_cat_size` levels) |
| `QTY` | quantity in the bin for this (MAJ_CAT, SIZE) row |
| `ALLOC_KEY` | derived (A0) |

**A2. `ARS_BIN_REQ`** *(new)* — open store requirement at the chosen level.
**Default source = the latest allocation output** (open REQ per store × MAJ_CAT[× SIZE]),
not an upload; Excel upload is a fallback for what-if runs.

| Column | Meaning |
|---|---|
| `SESSION_ID` | the run |
| `ST_CD` (`Store_Code`) | store |
| `MAJ_CAT`, `SIZE` | grain |
| `REQ` | open requirement |
| `REMAINING_REQ` | working balance drained as bins align (seeded = REQ) |
| `ST_NM`, `SEG` | store name / segment, for tagging |
| `ALLOC_KEY` | derived (A0) |

**A3. Store scope** — the stores eligible this run, and their evaluation order for
*one bin → all stores*. Reuse the store master / alloc store scope; order mode is
`input_sequence` (store-master order) or `random`.

### B. Run configuration

| Setting | Values | Default |
|---|---|---|
| Allocation strategy | `one_bin_all_stores` \| `one_store_all_bins` | `one_bin_all_stores` |
| Allocation level | `maj_cat` \| `size` \| `maj_cat_size` | `maj_cat` |
| Eligibility threshold (start) | 1–100 | 75 |
| Threshold pattern | `fixed` \| `cascading` | `fixed` |
| Minimum threshold (cascading) | 1–100 | 70 |
| Threshold reduction step | 1–50 | 5 |
| Store selection mode | `input_sequence` \| `random` | `input_sequence` |
| Include eligibility log | boolean | false |

### C. Eligibility (the rule, unchanged from the drafts)

For a candidate (bin, store) pair at threshold **T**:
1. `potentialAlignedQty` = Σ over each bin row of `MIN(row.QTY, REMAINING_REQ[row.ALLOC_KEY])`.
2. `totalQtyInBin` = Σ of all row `QTY` in the bin.
3. `eligibility% = potentialAlignedQty ÷ totalQtyInBin × 100`.
4. Eligible when `eligibility% ≥ T`.

### D. Phased processing

- Level `maj_cat_size` runs a **SIZE phase first** (tight match), then a **MAJ_CAT
  phase** on the residual unaligned bins.
- Levels `maj_cat` / `size` run that single phase only.
- Unaligned bins from an earlier phase feed the next phase.

### E. Strategies

**E1. One bin → all stores** (bins in bin-master order): for each bin, evaluate every
store with remaining REQ, pick the **highest eligibility %** among the eligible (ties
→ earlier store in master order, unless `random`), align the bin, deduct
`REMAINING_REQ`, remove the bin from the pool. No eligible store → bin stays
unaligned for this threshold.

**E2. One store → all bins** (stores in master order): build the store's
`REMAINING_REQ` per `ALLOC_KEY`, iterate candidate bins sharing any of the store's
keys, align each eligible bin to the store and deduct, stop when REQ is fully consumed
or candidates are exhausted.

### F. Cascading threshold

When `pattern = cascading`, build a decreasing sequence from the start threshold down
to `minThreshold` in steps of `reductionStep` (ensuring `minThreshold` appears exactly
once). Run the phase strategy over the currently **unaligned** bins at each threshold;
stop early when no unaligned bins remain. Bins aligned at a higher threshold are
**locked** — cascading only retries the residue.

### G. Business rules (from BRD §7)

| ID | Rule |
|---|---|
| R-1 | A bin is unique by `BIN`. |
| R-2 | A store is unique by `ST_CD`. |
| R-3 | `ALLOC_KEY` is case-insensitive, trimmed (A0). |
| R-4 | eligibility % = potential aligned qty ÷ total bin qty × 100. |
| R-5 | Cascading re-attempts *unaligned* bins only; aligned bins are locked. |
| R-6 | Aligning a bin deducts its aligned qty from the store's `REMAINING_REQ`. |
| R-7 | A store with `REMAINING_REQ = 0` is skipped in later evaluations. |
| R-8 | Eligibility ties → earlier store in master order (unless `random`). |
| R-9 | **No bin splitting** — a bin is fully assigned to one store or stays unaligned. |

### H. Aligned-bin output row (`ARS_BIN_ALLOC_ALIGNED`)

Per aligned bin row: `BIN, MAJ_CAT, SIZE, QTY, ALLOC_KEY`, `ST_CD_TAG, ST_NM_TAG,
REQ_SEG_TAG`, `REQ_ORIGINAL_FOR_STORE_MAJ_CAT`, `REV_REQ, MIN_BIN_QTY_OR_REV_QTY`,
`TOTAL_MIN_BIN_REV_QTY_BIN_WISE, TOTAL_QTY_BIN_WISE`, `ALIGNED_QTY,
BIN_QTY_AFTER_ALIGNMENT`, `CONT_PERCENT`, `EXCESS` (= bin qty − REQ consumed),
`STRATEGY_USED, PHASE, THRESHOLD_USED`. Unaligned bins → `ARS_BIN_ALLOC_UNALIGNED`.

### I. Job lifecycle (replaces the FSD's Web-Worker + localStorage model)

The run is a **Jobs Dashboard** job (same pattern as MSA / auto-cont / rule-engine
runs), one `ARS_BIN_ALLOC_SESSION` row per run holding config + live counters.

- **Progress** — the worker updates the session row per threshold level (current
  phase, current threshold, bins processed/aligned/unaligned, aligned qty, excess,
  excess %, elapsed). The page polls it (no browser-held state).
- **Pause / Resume / Stop** — job-state transitions on the session row, checked at
  each store/bin boundary; a stopped or paused job leaves a valid partial result.
- **Persistence / resume** — all aligned rows are already in `ARS_BIN_ALLOC_ALIGNED`;
  a refresh or crash rehydrates from the session row, so "recover from accidental
  refresh" is inherent, not a localStorage buffer.
- **Eligibility log** — when enabled, `ARS_BIN_ALLOC_LOG` captures every (bin, store)
  evaluation (phase, bin, store, potential aligned qty, total bin qty, eligibility %,
  threshold, eligible flag, cascading step). Capped (default 5,000) to bound size;
  the cap being hit is surfaced, never silent.

### J. Export

Reuse the existing export infrastructure: multi-sheet **XLSX** (Aligned · Unaligned ·
Summary · Eligibility Log if requested), per-dataset **CSV**, full-payload **JSON**,
and an aligned-only **partial CSV** available at any time (including on a paused/
stopped job).

### K. RBAC & audit

- Permissions: **`BIN_ALLOC_VIEW`** (see pages/results) and **`BIN_ALLOC_RUN`**
  (start/pause/stop a run, export). Section is gated like other ARS modules.
- Every run + control action writes to `audit_log` (who ran what, config, counts).
  This is the DB audit trail the client-only draft could not provide.

---

## Traceability (BRD → FSD)

| BRD | FSD |
|---|---|
| BR-01 Input Data | A1, A2, A3 |
| BR-02 Allocation Level | A0, D |
| BR-03 Allocation Strategy | E1, E2 |
| BR-04 Eligibility Threshold | C |
| BR-05 Cascading Threshold | F |
| BR-06 Store Selection Order | E1, B |
| BR-07 No Bin Splitting | G/R-9, E1, E2 |
| BR-08 Excess Tracking | H |
| BR-09 Audit Log | I (eligibility log), K |
| BR-10 Pause/Resume/Stop | I |
| BR-11 Partial Export | J |
| BR-12 Background Processing | I (Jobs Dashboard job — not a Web Worker) |
| BR-13 Progress Persistence | I (DB session row — not localStorage) |
| BR-14 Output | H, J |
| BR-15 Data Volume | I (server-side; not browser-memory bound) |

---

## Forward plan / deferred (not built in the scaffold pass)

1. Migration script (next number in `backend/scripts/`): `ARS_BIN_MASTER`,
   `ARS_BIN_REQ`, `ARS_BIN_ALLOC_SESSION`, `ARS_BIN_ALLOC_ALIGNED`,
   `ARS_BIN_ALLOC_UNALIGNED`, `ARS_BIN_ALLOC_LOG` + `BIN_ALLOC_VIEW`/`BIN_ALLOC_RUN`
   permissions.
2. Backend service + endpoints (`endpoints/bin_alloc.py`, prefix `/bin-alloc`):
   bin-master upload (confirm-before-commit, mirror the MBQ upload), REQ pull from
   alloc output (+ upload fallback), `run`, `pause`/`resume`/`stop`, `progress`,
   results, export.
3. The allocation engine (greedy best-fit, no split, phased, cascading) as a job
   worker — reuse the Jobs Dashboard run/poll pattern.
4. Replace the five placeholder pages with real UIs (upload, config + run console with
   live progress, results with aligned/unaligned tabs + export, eligibility log).
5. REQ-source reconciliation with the `[A-Z]-` MAJ_CAT prefix hazard (A0).

---

## Recorded rules

- **2026-07-24** — Module created as a spec + navigable sidebar scaffold. New **Bin
  Allocation** section placed **above Adhoc** (`Sidebar.jsx` `binAllocItems`, icon
  `Container`), five placeholder pages under `/bin-alloc/*` routed to
  `BinAllocPlaceholderPage` in `App.jsx`. No backend / engine yet.
- **2026-07-24** — **Deployment model redesigned ARS-native** (owner decision). The
  source FSD's client-only model (Web Worker + Excel-only inputs + localStorage, "no
  server-side engine") is **rejected**; the engine runs **server-side as a Jobs
  Dashboard job**, inputs/outputs live in `ARS_BIN_ALLOC_*` SQL tables, REQ is pulled
  from the **latest allocation output** (upload is fallback only), the run is
  **RBAC-gated** (`BIN_ALLOC_VIEW`/`_RUN`) and **audit-logged**, and pause/resume/stop
  are durable **job-state** transitions. The **algorithm** (greedy best-fit, no bin
  split, fixed/cascading eligibility thresholds, two strategies, excess tracking,
  eligibility log) is adopted verbatim from the drafts.
- **2026-07-24** — `ALLOC_KEY` must normalise the **`[A-Z]-` MAJ_CAT prefix** on both
  the bin and REQ sides, since REQ is sourced from the alloc side (no prefix) while
  bins may carry a listing-side MAJ_CAT (prefixed) — otherwise keys silently mismatch
  and bins go unaligned.
