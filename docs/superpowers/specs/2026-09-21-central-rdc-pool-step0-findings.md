# Central RDC Pool — Step 0 Findings

**Date:** 2026-09-21 · **Database:** `Rep_Data` @ `ARS` · **Spec:** v1.5, Part C Step 0
**Captured by:** `backend/scripts/step0_rdc_baseline.py` (read-only)

---

## 1. Status of the four Step 0 tasks

| Task | Result |
|---|---|
| **V1 — measure defect D-1** | ✅ done (reframed — see §4) |
| **Own baseline (V5)** | ✅ captured — `docs/baselines/own_20260919_125317_275/` |
| **Cross baseline (V19)** | 🔴 **NOT POSSIBLE** — all Cross data purged (§6) |
| **O1, O2, O6** | ⬜ still with the business · **O5 closed** (§7) |

---

## 2. The estate, measured

| | Value |
|---|---|
| Live RDCs | **`DW01`**, **`DH24`** — exactly 2 |
| Stores | **521** — `DW01` 284, `DH24` 237 |
| **Untagged stores** | **0** — confirms spec O4 is insurance, not a live gap |
| MSA option-rows | `DH24` 103,950 · `DW01` 36,431 |
| MSA stock (`FNL_Q`) | `DH24` 3,440,854 · `DW01` 2,968,215 · **total 6,409,069 pcs** |

### Ledger sizes — spec §B7.3 verified

| Table | Spec said | Actual | `SRC_RDC` exists? |
|---|---|---|---|
| `ARS_PEND_ALC` | 17,781,533 | **17,948,674** | no |
| `ARS_NL_TBL_HOLD_TRACKING` | 2,750,316 | **2,750,316** ✅ | no |
| `ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT` | 6,558,761 | **6,558,761** ✅ | no |
| `Master_ALC_PEND` | 385,014 | **385,014** ✅ | no |
| `ARS_FACONS_PEND` | 2,170 | **2,170** ✅ | no |

Only `ARS_PEND_ALC` has moved (+167k since the spec was written — normal growth).
**The §B7.3 ALTERs are all still required; none has been applied.**

---

## 3. 🔴 Finding A — `All RDCs` has never been run in production

| STATUS | RDC_MODE | Sessions | Last |
|---|---|---|---|
| SUCCESS | **own** | **224** | 2026-09-19 |
| CANCELLED | own | 39 | 2026-09-19 |
| FAILED | own | 25 | 2026-09-05 |
| SUCCESS | **cross** | **4** | 2026-07-25 |
| KILLED | own | 1 | 2026-07-09 |
| — | **all** | **0** | **never** |

**Zero `all` sessions in 293 runs**, even though `ListingPage.jsx:756` defaults `rdcMode` to `'all'`. Planners switch to `Own` every single time.

### What this changes

| | Effect |
|---|---|
| **Defect D-1 has never actually bitten** | It only manifests in `all` mode. The BRD's "today ships 29 of 62" is a **correct model of what would happen**, not an observed production loss. §A2 should say so |
| **M8 risk is lower than assessed** | Nobody relies on today's `all` behaviour, so changing its meaning breaks no existing habit |
| **M8 is still required** | A new or hurried planner who leaves the default gets central pooling with no warning. The kill switch and the on-screen badge stay in |
| **V1 must be reframed** | There is no `all` session to measure. Measured as *latent exposure* instead — §4 |

---

## 4. V1 — defect exposure, measured on the live listing

Source: `ARS_LISTING_WORKING` as left by session `20260919_125317_275` (own, 451 stores).

| Metric | Rows | % |
|---|---|---|
| Listing rows total | **3,382,403** | 100 % |
| Option held at **both** RDCs (D-1 ambiguity zone) | **2,107,683** | 62.3 % |
| Other RDC holds stock the store **cannot see** | **913,837** | 27.0 % |
| **Store's own RDC has ZERO, other RDC has stock** | **403,732** | **11.9 %** |

That last row is the business case in one number: **403,732 store-option rows are dead today purely because of the warehouse boundary.** These are the `POOL_EMPTY` lines that central pooling converts into shipments.

### Option exclusivity — the structural cause

Options with `FNL_Q > 0`:

| | Options | Visible to |
|---|---|---|
| Only at `DH24` | **7,660** | 237 stores — invisible to `DW01`'s 284 |
| Only at `DW01` | **3,643** | 284 stores — invisible to `DH24`'s 237 |
| At both | 1,344 | all 521 |

**11,303 of 12,647 stocked options (89 %) exist at exactly one warehouse** and are therefore invisible to roughly half the chain. This is not an edge case — it is the normal state of the assortment.

---

## 5. Own baseline captured

`docs/baselines/own_20260919_125317_275/` — session of 2026-09-19, 451 stores, FRESH.

| Table | Rows | Size |
|---|---|---|
| `ARS_ALLOC_PARKED` | 0 *(already promoted)* | — |
| `ARS_ALLOC_HISTORY` | **1,139,285** | 70.3 MB |
| `ARS_PEND_ALC` | **124,427** | 2.1 MB |
| `ARS_NL_TBL_HOLD_TRACKING` (open set) | **234,285** | 2.3 MB |

Each file gzipped, SHA-256'd, plus a **numeric fingerprint** (per-column sums) in `manifest.json`. V5 compares the fingerprint first — a mismatch localises the defect to a column immediately; the CSV is there to find the row.

### The baseline is internally consistent

| Check | Value |
|---|---|
| `ARS_LISTING_SESSIONS.SHIP_QTY_TOTAL` | 496,871 |
| `ARS_ALLOC_HISTORY` Σ `SHIP_QTY` | **496,871** ✅ |
| `ARS_ALLOC_HISTORY` Σ `ALLOC_QTY` | **496,871** ✅ |
| `ARS_PEND_ALC` Σ `PEND_QTY` | **496,871** ✅ |
| Σ `HOLD_QTY` this session | 0 *(no holds)* |
| Open hold ledger | 346,258 pcs across 234,285 rows |

Allocation → history → pend conserves exactly. That is the property V5 will re-assert after the change.

**Note:** `ARS_ALLOC_WORKING` has **no `SESSION_ID` column** — it is dropped and rebuilt by `SELECT … INTO` every run. This independently confirms correction **M3**: `SRC_RDC` cannot be added there by `ALTER TABLE`. The durable evidence is `ARS_ALLOC_PARKED` / `_HISTORY`.

Files are gitignored (70 MB); `manifest.json` is committed.

---

## 6. 🔴 Finding B — no Cross baseline exists, and one cannot be recovered

All four Cross sessions ran on **2026-07-25** and were tiny (1-2 stores, 6-9 rows, 13-18 pcs).

| Session | History rows | Parked rows |
|---|---|---|
| `20260725_155350_586` | 0 | 0 |
| `20260725_154910_574` | 0 | 0 |
| `20260725_154711_595` | 0 | 0 |
| `20260725_154511_483` | 0 | 0 |

**Oldest surviving history session: `20260812_084837_616`.** The 30-day history TTL purged every Cross row weeks ago.

### Consequence

**V19 (Cross unchanged with the switch ON vs OFF) and the Cross half of V5 cannot be run.** There is nothing to diff against.

### Options

| # | Option | Assessment |
|---|---|---|
| **A** | **Run one small Cross session now** and baseline it — 2 stores, 1 MAJ_CAT, a few minutes | **Recommended.** Cheap, and it restores the gate |
| B | Drop Cross from the release gates | Not advisable — Cross shares the `else` branch that Own's changes sit beside, so it is exactly where a leak would land |
| C | Treat Cross as out of support | A business decision, not a technical one. 4 runs in 14 months suggests it is barely used — worth asking |

**Recommendation: A.** Until it is done, Step 2 should not start, because the `_rdc_join` edit (M2) touches the branch Cross shares with `all`.

---

## 7. O5 — CLOSED, no change required

**Question:** does `ARS_FACONS_PEND` need `SRC_RDC` too?
**Answer: no.** Three independent reasons, all verified in code:

| Evidence | Source |
|---|---|
| Self-contained FA/CONS pipeline — *"its OWN tables… the core ARS Pending Allocation tables / logic are NOT touched"* | `facons_pend_service.py:1-12` |
| `approve_session` seeds it from `ARS_FACONS_ALLOC_ART` / `_REF`, taking `rdc` from the FA/CONS allocation header — never from `ARS_ALLOC_HISTORY` or the store master | `facons_pend_service.py:107` |
| **Zero references** in `msa_service.py` or `alloc_pool.py` — it does not participate in the MSA `FNL_Q` deduction | grep, both files |

Folded into v1.5 §B7.3 and the open-items list.

---

## 8. Step 0 exit status

| Gate | State |
|---|---|
| Own baseline captured and checksummed | ✅ |
| **Cross baseline captured** | 🔴 **blocked — needs one fresh Cross run (§6)** |
| D-1 quantified | ✅ 403,732 blocked rows · 89 % of options single-warehouse |
| Ledger counts verified against spec | ✅ |
| O5 answered | ✅ no change required |
| **O1** — can picking execute a two-source line? | ⬜ business |
| **O6** — confirm BR-RDC-12 (holds never split) | ⬜ business |
| **O2** — `ALC_RDC_PRIORITY` value: `"DW01,DH24"` or `"DH24,DW01"`? | ⬜ business |

**Step 0 is 80 % complete.** Step 1 (schema + switch, zero behaviour change) can begin as soon as O2 is answered. **Step 2 must wait for the Cross baseline.**

---

## 9. Spec amendments arising

| Section | Amendment |
|---|---|
| §A2 | Note that D-1 is a modelled defect — `All RDCs` has never run in production (0 of 293 sessions) |
| §A2 / §A7 | Add the measured exposure: 403,732 blocked rows; 11,303 of 12,647 options single-warehouse |
| §B7.3 | `ARS_PEND_ALC` is now 17,948,674 rows |
| §B13 / V19 | Cross gate depends on a baseline that does not exist — add the fresh-run prerequisite |
| Part C Step 0 | Add "run and baseline one small Cross session" as an explicit task |
| §B16 | O5 closed |
