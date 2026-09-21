# FSD — I_ROD-aware option eligibility (`OPT_REQ_ROD`)

| | |
|---|---|
| **Document** | Functional Specification |
| **Module** | Listing (Part 4c, Part 6.6) + Rule engine (Stage A) |
| **Rule IDs** | **BR-16** (business), **FS-10** (functional) |
| **Date** | 2026-08-13 |
| **Status** | Approved for implementation — NOT yet implemented |
| **Evidence run** | `20260812_131757_589` (236 stores, 3,239,936 OPT rows, 56 min) |
| **Worked example** | `WERKS=HS11`, `GEN_ART_NUMBER=1241092810`, `CLR=SKY_BLU`, `MAJ_CAT=LS_NAIL_PAINT` |

---

## 1. Purpose

Make the option-level eligibility gate account for `I_ROD` (the maximum number of
dispatch rounds an option is allowed), so that an option whose store stock covers one
round of MBQ but not its full `I_ROD` entitlement can enter allocation and receive the
remaining rounds.

Today the gate tests a single round. Options are therefore rejected as "no demand" while
the allocation engine — had it been allowed to see them — would have shipped stock in
round 2 and beyond.

## 2. As-is behaviour

### 2.1 The two gates

The same predicate is evaluated twice, in two different components:

| # | Layer | Location | Predicate | Failure output |
|---|---|---|---|---|
| 1 | Listing Part 6.6 | `backend/app/api/v1/endpoints/listing.py:2784` | `ISNULL(OPT_REQ_WH,0) >= 1` | `ELIG_FLAG=0`, `ELIG_REASON='NO_DEMAND'` |
| 2 | Rule engine Stage A | `backend/app/services/rule_engine_new.py:342` | `ISNULL(OPT_REQ_WH,0) < 1` fires | `LISTED_FLAG=0`, `LISTED_REASON='R05_REQ_POS;'` |

A third consumer applies the same filter outside the generate path:

| # | Layer | Location | Predicate |
|---|---|---|---|
| 3 | `/listing-build` endpoint | `listing.py:4660` | `ISNULL(OPT_REQ_WH,0) >= :min_req` (default 1) |

### 2.2 The quantity being tested

`listing.py:2231-2238` and `:2344-2350`, both in Part 4c:

```sql
OPT_REQ    = MAX(0, ROUND(OPT_MBQ    - STK_TTL, 0))
OPT_REQ_WH = MAX(0, ROUND(OPT_MBQ_WH - STK_TTL, 0))
```

`OPT_MBQ_WH` equals `OPT_MBQ` for every OPT_TYPE except `TBL`, which alone receives the
`hold_days` uplift (`listing.py:2252-2256`). Neither formula references `I_ROD`.

### 2.3 Where `I_ROD` actually applies

Only inside the allocation engine, at size grain. Per round `r`
(`rule_engine_per_opt.py:835-841`):

```python
need_ship = max(r * SZ_MBQ - SZ_STK - SHIP_QTY, 0)
if ot == 'TBL':
    tbl_cum   = SZ_MBQ_WH + (r - 1) * SZ_MBQ
    need_pool = max(tbl_cum - SZ_STK - POOL_CONSUMED, 0)
    need_pool = 0 where need_ship == 0
else:
    need_pool = max(r * SZ_MBQ - SZ_STK - POOL_CONSUMED, 0)
```

The band mask is `OPT_TYPE == ot AND I_ROD >= r AND ALLOC_STATUS not in
('SKIPPED','INELIGIBLE')` (`rule_engine_per_opt.py:795-799`). There is **no `SZ_REQ > 0`
filter**, and a size that receives nothing in round 1 is never stamped `SKIPPED` — it only
gets an audit remark (`rule_engine_per_opt.py:1504-1518`). A row admitted to the engine
is therefore still live for round 2 even if round 1 gives it nothing.

### 2.4 Worked example — current outcome

Session `20260812_131757_589`, `ARS_LISTING_WORKING_HISTORY`:

```
OPT_TYPE   = RL       STK_TTL    = 12      I_ROD     = 2
OPT_MBQ    = 9        OPT_REQ    = 0       MSA_FNL_Q = 612
OPT_MBQ_WH = 9        OPT_REQ_WH = 0       VAR_COUNT = 1   (SZ_APPLICABLE = N)
ELIG_FLAG  = 0        ELIG_REASON  = NO_DEMAND
ALLOC_STATUS = INELIGIBLE            ALLOC_REASON = INELIGIBLE_R05_REQ_POS;
```

`OPT_REQ_WH = MAX(0, 9 − 12) = 0` → both gates reject → **0 rows in
`ARS_ALLOC_HISTORY`** for this OPT. The engine never evaluated it.

## 3. Gap

`I_ROD = 2` states the store is entitled to two rounds — a target of `2 × 9 = 18` against
stock of 12, i.e. a genuine shortfall of 6 units, with 612 units available in the RDC.
The gate discards the option on a 1-round test that `I_ROD` explicitly overrides.

The manual already documents the intended behaviour
(`frontend/public/docs/manual/listing.md:156`): *"within an OPT, I_ROD rounds scale demand
(`OPT_MBQ × N`)"*. The size-level target honours this; the eligibility gate does not.

## 4. To-be behaviour

### 4.1 BR-16 — business rule

> An option is eligible on demand grounds when its store stock falls short of its **full
> `I_ROD` entitlement**, not merely its first round. The entitlement mirrors the
> allocation engine's own per-round target so that the gate admits exactly the options
> the engine can ship to — no more, no less.

### 4.2 FS-10 — functional rule

A new column `OPT_REQ_ROD` is computed on `ARS_LISTING` in Part 4c:

```sql
OPT_REQ_ROD = MAX(0, ROUND(
    CASE WHEN ISNULL(OPT_TYPE,'') = 'TBL'
         THEN ISNULL(OPT_MBQ_WH,0) + (ISNULL(NULLIF(I_ROD,0),1) - 1) * ISNULL(OPT_MBQ,0)
         ELSE ISNULL(NULLIF(I_ROD,0),1) * ISNULL(OPT_MBQ,0)
    END - ISNULL(STK_TTL,0), 0))
```

Rules embedded in the formula, each deliberate:

| Element | Reason |
|---|---|
| `TBL` branch counts the hold buffer **once** (`MBQ_WH + (I_ROD−1) × MBQ`) | Mirrors `rule_engine_per_opt.py:837`. A plain `I_ROD × OPT_MBQ_WH` would multiply `hold_days` by the round count and inflate TBL demand. |
| `NULLIF(I_ROD,0)` → treated as 1 | 6,651 R05-blocked rows in the evidence run carry `I_ROD = 0`; without this they would evaluate to a negative entitlement. |
| `MAX(0, …)` | Same clamp as `OPT_REQ` / `OPT_REQ_WH`; a surplus never becomes negative demand. |
| `ROUND(…, 0)` | Consistent with the existing two columns. |

Placement: immediately after the `OPT_REQ_WH` UPDATE at `listing.py:2350`. This is the
earliest point at which `OPT_MBQ`, `OPT_MBQ_WH`, `OPT_TYPE` (set in Part 3.6) and `I_ROD`
(set in Part 3.5a) are all populated.

### 4.3 Gate changes

| Gate | From | To |
|---|---|---|
| Layer 1 `gate_demand` (`listing.py:2784`) | `ISNULL(OPT_REQ_WH,0) >= 1` | `ISNULL(OPT_REQ_ROD,0) >= 1` |
| Layer 2 R05 (`rule_engine_new.py:342`) | `ISNULL(OPT_REQ_WH,0) < 1` | `ISNULL(OPT_REQ_ROD,0) < 1` |
| Layer 3 `/listing-build` (`listing.py:4660`) | `ISNULL(OPT_REQ_WH,0) >= :min_req` | `ISNULL(OPT_REQ_ROD,0) >= :min_req` |

Each site falls back to `OPT_REQ_WH` when the column is absent, so pre-existing snapshot
tables remain readable.

**Both layers must read the same switch.** If Layer 1 admits a row and Layer 2 does not
(or vice versa), rows are silently dropped at Part 7 whenever `shift_all_to_working` is
off, because that shift filters on `ELIG_FLAG = 1`.

### 4.4 Reason codes — unchanged

`NO_DEMAND` and `R05_REQ_POS` keep their names and their positions in the CASE ladder.
Only the arithmetic behind them changes, so existing dashboards, saved queries and the
`ALLOC_REASON` string format continue to work. The semantics tighten from *"needs nothing
in round 1"* to *"needs nothing across all I_ROD rounds"*.

### 4.5 Label alignment — `NO_REQ`

`SZ_REQ` remains a 1-round figure (`rule_engine_new.py:1026-1030`). Once I_ROD-aware
admission is live, a newly admitted option that ships nothing because the MSA pool is
empty would be labelled `NO_REQ` (`SZ_REQ <= 0`) instead of the truthful `NO_POOL_MSA`,
because the classifier tests `SZ_REQ` before the pool.

The `NO_REQ` branch at `rule_engine_pandas.py:1162-1163` and `rule_engine_new.py:2733-2734`
therefore switches from `ISNULL(SZ_REQ,0) <= 0` to the I_ROD target already used by the
`ALREADY_STOCKED` branch directly above it. `NO_REQ` becomes unreachable by construction —
an option with no I_ROD demand is `ALREADY_STOCKED`, and anything else that ships zero is
a supply problem. This is a label-only change; no quantity is affected.

## 5. Out of scope

Explicitly **not** changed by this FSD:

| Item | Reason |
|---|---|
| `OPT_REQ`, `OPT_REQ_WH` | Feed excess/`EXCESS_STK` arithmetic, `OPT_PRIORITY_RANK` ordering (`rule_engine_new.py:539`, `:633`), dashboards and exports. Redefining them would silently reorder dispatch and change every published report. |
| `SZ_REQ`, `SZ_REQ_WH` | Not used as a gate by the live per-OPT engine; only for the `NO_REQ` label, handled in §4.5. |
| `MJ_REQ`, `MJ_MBQ`, R09 headroom, `*_mj_req_cap_pct`, sec-caps | Remain 1-round. Consequence: the newly admitted options compete inside today's MAJ_CAT budget rather than enlarging it. Deliberate — measure first, decide separately. |
| `OPT_PRIORITY_RANK` ordering | Untouched, because `OPT_REQ_WH` is untouched. |
| Allocation engine (`rule_engine_per_opt.py`) | Requires no change; §2.3 confirms an admitted row is shipped correctly by the existing round loop. |

## 6. Impact analysis — evidence run `20260812_131757_589`

Counting only rows where **R05 was the sole blocker** (rows also failing R04/R06/R09 gain
nothing from this change):

| Metric | Value |
|---|---|
| OPT rows in run | 3,239,936 |
| Eligible today | 925,296 |
| R05 fired (any combination) | 390,507 |
| **R05 as sole blocker** | **59,301** |
| **Newly admitted by FS-10** | **15,145** |
| …of which `MSA_FNL_Q > 0` | 15,012 |
| …by OPT_TYPE | **RL 15,145**, TBC 0, TBL 0 |
| Remaining 44,156 sole-R05 rows | Stocked at or above full I_ROD target — correctly still rejected |

TBL and TBC are unaffected: their stock position always clears the 1-round test, so they
never reach this gate.

### 6.1 Volume and runtime

The evidence run produced 647,052 alloc rows across 316,211 OPTs — **2.05 sizes per OPT** —
in 3,387.5 s. The per-OPT engine is sequential, so cost scales with OPT count:

- **+15,145 OPTs (+4.8%)** ≈ +31,000 alloc rows ≈ **+3 minutes**.

### 6.2 Rejected alternative — disabling the rule

Setting `RULE_R05_REQ_POS = False` and `gate_demand = "1=1"` would admit all 59,301
sole-R05 rows, of which 44,156 provably cannot ship (stock ≥ full I_ROD target; they would
return `ALREADY_STOCKED` after consuming engine time). Cost ≈ +19% OPTs, ≈ +10 minutes,
for zero additional dispatch versus FS-10.

The theoretical argument for removing the gate — that OPT-level stock masks starved
individual sizes — was tested against the run and does not hold at scale:

```
Σ OPT-level gap  (I_ROD × OPT_MBQ − STK_TTL) = 7,336,290
Σ size-level gap (Σ I_ROD × SZ_MBQ − SZ_STK) = 4,898,471     (33% lower)
OPTs where size gap > OPT gap = 7,316 of 316,211  (2.3%)
```

Deferred: catching that 2.3% would require the gate to join `ARS_GRID_MJ_VAR_ART` for
per-size store stock at listing time, since `SZ_MBQ` does not exist until Stage B — after
admission. Out of scope; quantify the commercial value of those 7,316 options first.

## 7. Configuration

| Setting | Location | Default | Behaviour |
|---|---|---|---|
| `RULE_R05_USE_IROD` | `rule_engine_new.py` flags block (`:31-40`) | `False` | Module-level kill switch. |
| `use_irod_eligibility` | `GenerateRequest` (`listing.py:75`) | `False` | Per-run override; recorded in `ARS_RUN_PARAMS_AUDIT` and `ARS_LISTING_SESSIONS.REQUEST_JSON`. Threaded to the engine alongside the other rule params at `listing.py:3249-3277`. |

With both at default the deployed code is behaviourally identical to today —
`OPT_REQ_ROD` is computed and visible, but no gate consumes it.

## 8. Data propagation

| Table | Mechanism |
|---|---|
| `ARS_LISTING` | Idempotent `ALTER TABLE … ADD` in Part 4c; the table is dropped and recreated every run. |
| `ARS_LISTING_WORKING` | Add `OPT_REQ_ROD` to `_FINAL_KEEP_COLS` (`listing.py:49-68`). **Required** — the `_FINAL_KEEP_SUFFIX = {"_REQ"}` pattern does not match a name ending in `_ROD`. |
| `ARS_LISTING_PARKED` / `_WORKING_PARKED` / `_HISTORY` / `ARS_LISTING_WORKING_HISTORY` | Reconciled automatically by `_reconcile_history_columns()` in `parked_history.py`. |
| Exports | Column lists at `listing.py:5546` and `:5748`. |
| `ARS_DATA_DICTIONARY` | New seed row (`data_dictionary.py`, next to the `OPT_MBQ_WH / OPT_REQ_WH` entry at `:173-174`). |

No standalone migration script is required.

## 9. Verification

### 9.1 Static check — no run needed

Against any existing `ARS_LISTING`, compare admissions under both formulas and confirm the
delta is entirely rows with a positive I_ROD entitlement:

```sql
SELECT OPT_TYPE, ISNULL(NULLIF(I_ROD,0),1) AS i_rod,
       SUM(CASE WHEN ISNULL(OPT_REQ_WH,0) >= 1 THEN 1 ELSE 0 END)  AS admitted_today,
       SUM(CASE WHEN ISNULL(OPT_REQ_ROD,0) >= 1 THEN 1 ELSE 0 END) AS admitted_after
FROM ARS_LISTING WITH (NOLOCK)
GROUP BY OPT_TYPE, ISNULL(NULLIF(I_ROD,0),1)
ORDER BY OPT_TYPE, i_rod;
```

Expected: identical counts at `I_ROD <= 1`; `admitted_after >= admitted_today` everywhere;
no row where `OPT_REQ_WH >= 1` but `OPT_REQ_ROD = 0`.

### 9.2 Paired runs

Same parameters, flag off then on. Compare:

1. `ELIG_FLAG = 1` count — expect ≈ +15k on comparable data, all RL.
2. `COUNT(*)` and `SUM(ALLOC_QTY)` in `ARS_ALLOC_WORKING`.
3. Per `(WERKS, MAJ_CAT)`: `SUM(ALLOC_QTY) <= rl_mj_req_cap_pct% × MJ_REQ` must still hold
   for every store — the MAJ_CAT budget is unchanged, so no store may breach it.
4. No row carries `SKIP_REASON = 'NO_REQ'` (§4.5 makes it unreachable).
5. Runtime increase within ~10%.

### 9.3 Acceptance criteria

| # | Criterion |
|---|---|
| AC-1 | With the flag off, results are byte-identical to a pre-change run on the same inputs. |
| AC-2 | `HS11 / 1241092810 / SKY_BLU` (I_ROD=2, OPT_MBQ=9, STK_TTL=12, pool 612) shows `OPT_REQ_ROD = 6`, `ELIG_FLAG = 1`, `ALLOC_STATUS = ALLOCATED`, `ALLOC_QTY = 6` (subject to `PAK_SZ` rounding). |
| AC-3 | No option with `STK_TTL >= I_ROD × OPT_MBQ` is admitted. |
| AC-4 | TBL demand is unchanged — `hold_days` is not multiplied by `I_ROD`. Verify a TBL row with `I_ROD >= 2`: `OPT_REQ_ROD = OPT_MBQ_WH + (I_ROD−1) × OPT_MBQ − STK_TTL`. |
| AC-5 | `OPT_REQ`, `OPT_REQ_WH`, `MJ_REQ`, `SZ_REQ` and `OPT_PRIORITY_RANK` are numerically unchanged between the paired runs. |
| AC-6 | No `(WERKS, MAJ_CAT)` exceeds its `MJ_REQ` cap. |

## 10. Rollback

Set `use_irod_eligibility = False` (or `RULE_R05_USE_IROD = False`) and re-run. The column
continues to be written but no gate reads it; behaviour reverts exactly. No schema
rollback needed — `OPT_REQ_ROD` is additive and nullable.

## 11. Documentation to update on implementation

- `frontend/public/docs/manual/listing.md` — FSD section + `## Recorded rules` entry.
- `backend/app/docs/BRD_ARS_V2.md` — R05 row in the rule table (`:378`).
- `ARS_DATA_DICTIONARY` — `OPT_REQ_ROD` seed row.

## 12. Open items

| # | Item | Owner |
|---|---|---|
| 1 | Whether `MJ_REQ` / R09 headroom should also become I_ROD-aware. Deferred pending the paired-run measurement in §9.2. | Business |
| 2 | Whether the 7,316 size-starved options (§6.2) justify a size-aware gate. | Business |
