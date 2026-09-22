# Central RDC Pool — Worked Examples

**Version:** 1.0 · **Date:** 2026-09-21
**Companion to:** the implementation plan and the execution runbook.
One dataset carried end-to-end, then each of the seven gaps shown as a concrete failure.

---

# PART 1 — THE DATASET

## 1.1 Store master (`Master_ALC_INPUT_ST_MASTER`)

| WERKS | RDC |
|---|---|
| HS11 | A |
| HS12 | A |
| HP04 | B |
| HP09 | *(blank)* |

## 1.2 Option under test

`MAJ_CAT = M_TEES_HS` · `GEN_ART_NUMBER = 1110116457` · `CLR = OFF_WHT` · pool `FRESH`

| SZ | VAR_ART |
|---|---|
| S | 5001 |
| M | 5002 |
| L | 5003 |

## 1.3 Warehouse stock (`ARS_MSA_VAR_ART`, FRESH)

| RDC | S (5001) | M (5002) | L (5003) | Total |
|---|---|---|---|---|
| A | 10 | 0 | 5 | **15** |
| B | 0 | 40 | 20 | **60** |
| **Clubbed** | **10** | **40** | **25** | **75** |

## 1.4 `ARS_RDC_FALLBACK` — as seeded today (2 RDCs live)

| OWN_RDC | FALLBACK_RDC | PRIORITY | IS_ACTIVE |
|---|---|---|---|
| `*` | A | 1 | 1 |
| `*` | B | 2 | 1 |

Only the two `'*'` rows exist. Per-RDC rows are pointless with two warehouses — the order after
"own" is forced. Resolution for each store:

| Store | Tag | `preference_order()` | `PREF_TIER` | Why |
|---|---|---|---|---|
| HS11 | A | **A, B** | `1` | own first; no `OWN_RDC='A'` rows, so tier `1` not `1F` |
| HS12 | A | **A, B** | `1` | |
| HP04 | B | **B, A** | `1` | |
| HP09 | *(blank)* | **A, B** | `2` | falls to the `'*'` rows |

## 1.5 Store requirement (after MBQ / I_ROD — engine output, unchanged by this feature)

| Store | Own RDC | S | M | L | Need |
|---|---|---|---|---|---|
| HS11 | A | 4 | 8 | 6 | 18 |
| HS12 | A | 4 | 8 | 12 | 24 |
| HP04 | B | 4 | 10 | 6 | 20 |
| | | | | | **62** |

---

# PART 2 — END-TO-END TRACE

## 2.1 Today — `All RDCs` with two separate pools

| # | Store | RDC | SZ | Need | Pool | Ship | Reason |
|---|---|---|---|---|---|---|---|
| 1 | HS11 | A | S | 4 | 10 | **4** | ok |
| 2 | HS11 | A | M | 8 | 0 | **0** | `POOL_EMPTY` — B holds 40, invisible |
| 3 | HS11 | A | L | 6 | 5 | **5** | short by 1 |
| 4 | HS12 | A | S | 4 | 6 | **4** | ok |
| 5 | HS12 | A | M | 8 | 0 | **0** | `POOL_EMPTY` |
| 6 | HS12 | A | L | 12 | 0 | **0** | `POOL_EMPTY` |
| 7 | HP04 | B | S | 4 | 0 | **0** | `POOL_EMPTY` — A holds 2, invisible |
| 8 | HP04 | B | M | 10 | 40 | **10** | ok |
| 9 | HP04 | B | L | 6 | 20 | **6** | ok |

**Shipped 29 of 62 (47 %). 46 pieces left idle.**

## 2.2 Step 1 — allocate from the clubbed pool (same order, same gates, same caps)

| # | Store | SZ | Need | Pool before | Ship | Pool after |
|---|---|---|---|---|---|---|
| 1 | HS11 | S | 4 | 10 | **4** | 6 |
| 2 | HS11 | M | 8 | 40 | **8** | 32 |
| 3 | HS11 | L | 6 | 25 | **6** | 19 |
| 4 | HS12 | S | 4 | 6 | **4** | 2 |
| 5 | HS12 | M | 8 | 32 | **8** | 24 |
| 6 | HS12 | L | 12 | 19 | **12** | 7 |
| 7 | HP04 | S | 4 | 2 | **2** | 0 |
| 8 | HP04 | M | 10 | 24 | **10** | 14 |
| 9 | HP04 | L | 6 | 7 | **6** | 1 |

**Shipped 60 of 62 (97 %).**

## 2.3 Step 2 — the split pass ledger (`SINGLE_PREFERRED`, own first)

Balances tracked per size. `A bal` / `B bal` are that size's balance when the line is processed.

| # | Store | Own | SZ | Qty | A bal | B bal | Covered by | ←A | ←B | A after | B after |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | HS11 | A | S | 4 | 10 | 0 | A alone (own) | **4** | — | 6 | 0 |
| 2 | HS11 | A | M | 8 | 0 | 40 | B alone | — | **8** | 0 | 32 |
| 3 | HS11 | A | L | 6 | 5 | 20 | B alone (A has 5 < 6) | — | **6** | 5 | 14 |
| 4 | HS12 | A | S | 4 | 6 | 0 | A alone (own) | **4** | — | 2 | 0 |
| 5 | HS12 | A | M | 8 | 0 | 32 | B alone | — | **8** | 0 | 24 |
| 6 | HS12 | A | L | 12 | 5 | 14 | B alone | — | **12** | 5 | 2 |
| 7 | HP04 | B | S | 2 | 2 | 0 | A alone (own B empty) | **2** | — | 0 | 0 |
| 8 | HP04 | B | M | 10 | 0 | 24 | B alone (own) | — | **10** | 0 | 14 |
| 9 | HP04 | B | L | 6 | 5 | 2 | **neither** → split, own B first | **4** | **2** | 1 | 0 |
| | | | | **60** | | | | **14** | **46** | | |

One split line (#9). Two cross-ships (#2/#3/#5/#6 A-stores served by B; #7 a B-store served by A).
`14 + 46 = 60` ✓ · no balance ever negative ✓

## 2.4 `ARS_ALLOC_RDC_SPLIT` — the actual rows written

Session `S-4471`, `ALLOC_TYPE = FRESH`.

| WERKS | VAR_ART | SZ | **SRC_RDC** | SHIP_QTY | HOLD_QTY | STORE_RDC | PREF_TIER | IS_CROSS |
|---|---|---|---|---|---|---|---|---|
| HS11 | 5001 | S | **A** | 4 | 0 | A | 1 | 0 |
| HS11 | 5002 | M | **B** | 8 | 0 | A | 1 | **1** |
| HS11 | 5003 | L | **B** | 6 | 0 | A | 1 | **1** |
| HS12 | 5001 | S | **A** | 4 | 0 | A | 1 | 0 |
| HS12 | 5002 | M | **B** | 8 | 0 | A | 1 | **1** |
| HS12 | 5003 | L | **B** | 12 | 0 | A | 1 | **1** |
| HP04 | 5001 | S | **A** | 2 | 0 | B | 1 | **1** |
| HP04 | 5002 | M | **B** | 10 | 0 | B | 1 | 0 |
| HP04 | 5003 | L | **B** | 2 | 0 | B | 1 | 0 |
| HP04 | 5003 | L | **A** | 4 | 0 | B | 1 | **1** |

10 rows for 9 allocation lines — line #9 produced two.

## 2.5 `ARS_ALLOC_WORKING` — still exactly 9 rows

The child table absorbs the split; the parent keeps its one-row-per-store-size grain that
parking, Approve and reporting depend on.

| WERKS | VAR_ART | SZ | SHIP_QTY | `RDC` *(store)* | `SRC_RDC` *(new)* | `SRC_SPLIT_CNT` |
|---|---|---|---|---|---|---|
| HS11 | 5001 | S | 4 | A | A | 1 |
| HS11 | 5002 | M | 8 | A | B | 1 |
| HS11 | 5003 | L | 6 | A | B | 1 |
| HS12 | 5001 | S | 4 | A | A | 1 |
| HS12 | 5002 | M | 8 | A | B | 1 |
| HS12 | 5003 | L | 12 | A | B | 1 |
| HP04 | 5001 | S | 2 | B | A | 1 |
| HP04 | 5002 | M | 10 | B | B | 1 |
| HP04 | 5003 | L | 6 | B | **MULTI** | **2** |

Note both columns coexist: `RDC` still means *the store's warehouse* and keeps every existing
report working; `SRC_RDC` means *the warehouse that ships*.

## 2.6 RDC-wise picking requirement

| RDC | S | M | L | **To pick** | Its stock | Left |
|---|---|---|---|---|---|---|
| A | 10 | 0 | 4 | **14** | 15 | 1 |
| B | 0 | 26 | 20 | **46** | 60 | 14 |
| | 10 | 26 | 24 | **60** | 75 | 15 |

## 2.7 Store dispatch summary

| Store | From A | From B | Total | Documents |
|---|---|---|---|---|
| HS11 | 4 (S4) | 14 (M8, L6) | 18 | 2 |
| HS12 | 4 (S4) | 20 (M8, L12) | 24 | 2 |
| HP04 | 6 (S2, L4) | 12 (M10, L2) | 18 | 2 |

---

# PART 3 — THE SEVEN GAPS, AS NUMBERS

## G2 🔴 — `ARS_PEND_ALC.RDC` misroutes the pend, and next run invents 32 pieces

`write_pend_alc` derives the warehouse from the **store master**, not from where the stock came
from. Applying today's logic to the split above:

| Line | Ships from | Store's RDC | Pend booked **today** | Pend booked **correctly** |
|---|---|---|---|---|
| HS11 S 4 | A | A | A | A ✓ |
| HS11 M 8 | B | A | **A ✗** | B |
| HS11 L 6 | B | A | **A ✗** | B |
| HS12 S 4 | A | A | A | A ✓ |
| HS12 M 8 | B | A | **A ✗** | B |
| HS12 L 12 | B | A | **A ✗** | B |
| HP04 S 2 | A | B | **B ✗** | A |
| HP04 M 10 | B | B | B | B ✓ |
| HP04 L 6 | B 2 + A 4 | B | **B 6 ✗** | B 2 + A 4 |

Totals are conserved (60 = 60) but **routed to the wrong warehouse**:

| | A | B |
|---|---|---|
| Pend booked today (by store RDC) | 42 | 18 |
| Pend that should be booked (by source) | **14** | **46** |

### What that does to the *next* MSA run

`FNL_Q = stock − pend − hold`, evaluated per `(RDC, size)`:

| RDC | SZ | Stock | Actually shipped from here | Should remain | Pend booked today | **MSA will show** | Error |
|---|---|---|---|---|---|---|---|
| A | S | 10 | 10 | 0 | 8 | 2 | **+2 phantom** |
| A | M | 0 | 0 | 0 | 16 | 0 *(clamped)* | deduction lost |
| A | L | 5 | 4 | 1 | 18 | 0 *(clamped)* | **−1 stranded** |
| B | S | 0 | 0 | 0 | 2 | 0 | — |
| B | M | 40 | 26 | 14 | 10 | 30 | **+16 phantom** |
| B | L | 20 | 20 | 0 | 6 | 14 | **+14 phantom** |

**Next run is offered 32 pieces that do not physically exist, and 1 real piece is stranded.**
Those 32 get allocated again, and the warehouse cannot pick them.

And the BDC file tells **RDC-A to pick 42 pieces** when A only has 14 to give.

**Fix:** `ISNULL(SPL.SRC_RDC, ISNULL(M.[RDC], H.[WERKS]))`. With no split rows (Own / Cross) the
expression returns today's value unchanged.

**Why this needs an audit, not just a patch:** 11 endpoints and 6 stored procs read that column.
Most *want* the source RDC — but `hold_dashboard`, for instance, may be answering *"what is
reserved for my stores?"*, which is a store question. Each consumer must be read and classified.

---

## G3 🔴 — two parked runs allocate the same 40 pieces

Two planners run overlapping MAJ_CATs. Neither run is approved yet, so no pend exists.

| | Session S-4471 | Session S-4472 | Physically available |
|---|---|---|---|
| **Today, `Own` (RDC-A planner / RDC-B planner)** | sees A: M **0** | sees B: M **40** | no overlap — *accidental isolation* |
| **After clubbing, `All RDCs`** | sees clubbed M **40** | sees clubbed M **40** | **40** |
| Allocates | 26 | 26 | |
| Committed | | **52** | **over-committed by 12** |

Today the isolation is real but accidental — it comes from the pools being separate. Clubbing
destroys it. You are protected only by the single-parked 409 guard
(`listing.py:603`) — **until someone switches on `allow_multi_parked`.**

**Fix:** refuse `allow_multi_parked` while `ALC_RDC_CENTRAL_POOL` is active.

---

## G1 🟠 — the reviewer and the warehouse read different numbers

`AlcReviewPage.jsx` pivots MAJ_CAT × RDC, where RDC is the **store's**.

| What | A | B |
|---|---|---|
| Alloc Review pivot shows (store RDC) | **42** | **18** |
| Warehouse actually picks (source RDC) | **14** | **46** |

A reviewer approves a run believing warehouse A is shipping 42 pieces. A receives a picklist for
14. Same column header, three-times-off number.

**Fix (cheap, now):** relabel the column `Store RDC`. **Fix (proper, later):** add a
source-RDC toggle to the pivot.

---

## G4 🟠 — a re-run corrupts the split table

`ARS_ALLOC_WORKING` is dropped and rebuilt every Generate; `ARS_ALLOC_RDC_SPLIT` is durable and
session-keyed. Re-running session `S-4471`:

| | Attempt 1 | Attempt 2 (say B's L stock arrived) | Table after |
|---|---|---|---|
| HP04 L 6 | B 2 + A 4 | B 6 *(covered whole)* | rows for **B 6** *and* stale **B 2 + A 4** |
| `Σ split.SHIP` | 60 | 60 | **68** |
| `Σ working.SHIP` | 60 | 60 | 60 |

Invariant I-1 breaks; the picklist over-states by 8. The other outcome is a hard PK violation on
`(S-4471, HP04, 5003, L, A, FRESH)`.

**Fix:** Part 8.37 opens with `DELETE FROM ARS_ALLOC_RDC_SPLIT WHERE SESSION_ID = :sid`.

---

## G5 🟠 — the pass could add 17 seconds to every run

| Metric | Reference run |
|---|---|
| Allocation lines | 8,421 |
| Split rows emitted (2 RDCs, ~0.5 % split) | ~8,459 |
| Ledger entries `(RDC, VAR_ART, SZ)` | ~2 × 12,000 = 24,000 |
| **Row-by-row INSERT** @ ~2 ms | **≈ 17 s** |
| **Bulk `executemany` / staging table** | **≈ 0.5 s** |

It runs inside the Generate request thread, between Part 8.36 and Part 8.4. Nobody set a budget.

**Fix:** target **≤ 15 s per 10,000 lines**, benchmark as V17, bulk-insert — never row-by-row.

---

## G6 🟠 — rollback, made concrete

Friday, three runs into the new mode, ops reports the picklist is wrong.

| Step | Action | Effect |
|---|---|---|
| 1 | Settings → Business Rules → `ALC_RDC_CENTRAL_POOL` → **inactive** | takes effect within the 30 s rule cache |
| 2 | Next Generate | `_CENTRAL_POOL` resolves false at run start → per-RDC pools, no Part 8.37 |
| 3 | Runs already approved | untouched; their split rows stay as the historical picking record |
| 4 | Deploy / migration / data repair | **none** |

This only works if **every** change is switch-guarded. Make it a code-review rule: *no change
may be written that the switch cannot undo.*

---

## G7 🟡 — RLS can hand the picker an incomplete picklist

A warehouse user at RDC-A has `rls_stores = {HS11, HS12}` — A's own stores.

The picklist for A is **14 pieces**: HS11 S4, HS12 S4, **HP04 S2 + L4**.

HP04 is a B-store, so it is not in this user's RLS set:

| Scoping choice | Picker sees | Outcome |
|---|---|---|
| **Store-scoped** (apply `rls_stores`) | 8 pcs — HS11 S4, HS12 S4 | HP04's 6 pieces **never picked**; store waits forever, no error anywhere |
| **RDC-scoped** (warehouse sees its whole picklist) | 14 pcs | correct — but the user sees a store outside their normal scope |

A silently incomplete picklist is worse than no picklist.

**Needs a business decision before Stage 4.** My recommendation: RDC-scoped for the picking
document specifically, store-scoped everywhere else.

---

# PART 4 — BR-RDC-11 IN ACTION (why holds never split)

TBL option, size M only. Stock **A = 3, B = 9**. Allocation: HS11 (own A) ship 5 + hold 2;
HP04 (own B) ship 4 + hold 1. SHIP is tagged before HOLD on each line; one shared ledger.

| # | Store | Own | Draw | Qty | A bal | B bal | Covered by | ←A | ←B | A after | B after |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | HS11 | A | SHIP | 5 | 3 | 9 | B alone (A has 3 < 5) | — | **5** | 3 | 4 |
| 2 | HS11 | A | HOLD | 2 | 3 | 4 | A alone (own) | **2** | — | 1 | 4 |
| 3 | HP04 | B | SHIP | 4 | 1 | 4 | B alone (own) | — | **4** | 1 | 0 |
| 4 | HP04 | B | HOLD | 1 | 1 | 0 | A alone (own B empty) | **1** | — | 0 | 0 |
| | | | | **12** | | | | **3** | **9** | ✓ | ✓ |

Every hold landed on **one** warehouse, so each writes a single
`ARS_NL_TBL_HOLD_TRACKING` row and the PK `(WERKS, VAR_ART, SZ, ALLOC_TYPE)` holds.

### What BR-RDC-11 prevents

Change line 2 to a hold of **5** with A at 3 and B at 4. A split hold would need:

| WERKS | VAR_ART | SZ | ALLOC_TYPE | RDC | HOLD_REM |
|---|---|---|---|---|---|
| HS11 | 5002 | M | FRESH | A | 3 |
| HS11 | 5002 | M | FRESH | B | 2 |

**Identical primary keys.** The insert fails, or one row silently overwrites the other and 2 or 3
pieces are reserved in the system but not in any warehouse.

Under BR-RDC-11 the hold takes the largest single source (B, 4), the remaining 1 is dropped, and
`ALLOC_REMARKS` records `RDC_HOLD_SHORT(alloc=5,held=4)`. A hold is advisory, so reducing it is
safe — unlike reducing a shipment.

### Two failure modes the shared ledger prevents

| Wrong approach | What breaks, on the data above |
|---|---|
| Hold tagged to the store's own RDC regardless of stock | HS11's 2 pcs tagged to A, which has 0 left → **phantom hold**; next run deducts stock that was never reserved |
| SHIP and HOLD tagged in two independent passes | Both see B = 9 and together book 12 from B → **double-booked stock** |

---

# PART 5 — THE UNTAGGED STORE (HP09)

Stock, L-size: A = 15, B = 20 → clubbed 35.

| Store | Tag | Allocated from clubbed pool | Allocated **today** |
|---|---|---|---|
| HS11 | A | 10 | 10 |
| HS12 | *(blank)* | **12** | **0** — blank tag matches no pool key |
| HP04 | B | 4 | 4 |
| HP09 | `ALL` | **3** | **0** — `'ALL'` is not a real RDC code |
| | | **29** | 14 |

Split pass, `PREF_TIER` from `ARS_RDC_FALLBACK`:

| # | Store | Tier / order | Need | A bal | B bal | Covered by | ←A | ←B |
|---|---|---|---|---|---|---|---|---|
| 1 | HS11 | `1` — own A, then `*` | 10 | 15 | 20 | A | **10** | — |
| 2 | HS12 | `2` — `*` = A,B | 12 | 5 | 20 | B | — | **12** |
| 3 | HP04 | `1` — own B, then `*` | 4 | 5 | 8 | B | — | **4** |
| 4 | HP09 | `2` — `*` = A,B | 3 | 5 | 4 | **either** | **3** | — |

**Only line 4 was decided by preference** — both warehouses could have served it, and the `'*'`
order put A first. Lines 1-3 were decided by availability alone.

| If HP09's order came from | Ships from | Store receives |
|---|---|---|
| a real tag `RDC-B` (tier 1) | B | 3 |
| `'*'` rows A,B (tier 2) | A | 3 |
| `'*'` rows B,A (tier 2) | B | 3 |
| empty table → largest-available (tier 3) | A (5 > 4) | 3 |

**An untagged store never loses stock. It only loses the ability to state a preference, and only
on lines where both warehouses could have served it.**
