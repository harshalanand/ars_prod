---
title: Listing → Allocation — Every Step, Plain English, With Code
category: Allocation
order: 22
source: listing.py + rule_engine_new.py + rule_engine_pandas.py + listing_allocator.py + parked_history.py
last_reviewed: 2026-05-26
audience: anyone debugging an allocation, new joiners, business stakeholders
---

# Listing → Allocation — Every Step, Plain English, With Code

> One real article — **U128 / 1134114962 / L_GRY** — followed line-by-line through every step. Hidden steps included. Real code snippets included. The example trace lives at the bottom of *every* step so you can see exactly what happens to it.

---

## 0. The setup — what we're starting with

**The article:**

| Field | Value | Meaning |
|---|---|---|
| WERKS | `U128` | The store |
| MAJ_CAT | `M_KIDS` | Kids' clothing category |
| GEN_ART_NUMBER | `1134114962` | The article number (same design, before colour/size) |
| CLR | `L_GRY` | Light grey colour |

**The article has 3 variants** (size bands — typical for kids' wear):

| VAR_ART | Size | Warehouse stock (`FNL_Q`) |
|---|---|---|
| `1134114962001` | 0-3M | **34** |
| `1134114962002` | 3-6M | **3** |
| `1134114962003` | 6-12M | **1** |

**U128's shelf for this article:** empty (`STK_TTL = 0`). U128 has never sold this article — it's a fresh launch candidate.

**Settings used in this run:**
- `Stock% = 0.6` (threshold for adequacy and size coverage)
- `MinSz = 3` (minimum sizes with stock before TBL is allowed)
- `default_acs_d = 18` (fallback when ACS_D = 0)

---

## PART 1 — Build the GEN_ART-level listing

**File:** [`listing.py:817-835`](backend/app/api/v1/endpoints/listing.py#L817-L835)

**What it does in plain English:** Take every store × every (article, colour) pair from the **grid table** (`ARS_GRID_MJ_GEN_ART`) and write one row per combination into `ARS_LISTING`. Each row carries the store's stock for that article. Marked `IS_NEW = 0`.

**Hidden steps — what happens behind the scenes:**

1. **DROP + CREATE `ARS_LISTING`** — starts every run with an empty schema. Why? Stale columns from a previous Stock% / MinSz run would pollute Part 4's grid joins. Cheaper to rebuild than to clean.

2. **Sum stock SLOCs into `STK_TTL`** — the grid has multiple SLOC (storage location) columns: e.g. `STK_E01`, `STK_E02`, `STK_BACK`, plus negative-stock-adjustment columns. They get summed and **clamped at 0** so a wrongly-negative SLOC can't pretend to be a "credit" against the request.
   *Example:* a store row with `STK_E01=4, STK_E02=2, STK_BACK=1, STK_ADJ=-3` → `STK_TTL = MAX(0, 4+2+1-3) = 4`. (If the adjustment had been `-10`, `STK_TTL` would still clamp at 0, not become `-6`.)

3. **Sum sale SLOCs into `STR`** — separate from `STK_TTL` because sale columns describe velocity (last-N-days sold), not on-hand stock. They must not inflate `STK_TTL`. `STR` is later used by store ranking (Part 6).
   *Example:* `L-7 DAYS SALE-Q = 3, L-30 DAYS SALE-Q = 12` → `STR = 15`.

4. **INSERT** — one row per `(store, article, colour)` combination present in the grid. `IS_NEW = 0` because the row came from the in-store grid (the store already carries this article).

**Key code:**

```sql
INSERT INTO ARS_LISTING (WERKS, RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, ..., STK_TTL, STR, IS_NEW)
SELECT G.WERKS, S.RDC, G.MAJ_CAT, G.GEN_ART_NUMBER, G.CLR,
       ..., <SUM-of-SLOCs>, <SUM-of-sale-cols>, 0
FROM ARS_GRID_MJ_GEN_ART G
INNER JOIN <stores> S ON G.WERKS = S.ST_CD
```

**For U128 / 1134114962 / L_GRY:** ❌ **No row inserted in Part 1**. The article wasn't on U128's grid (U128 doesn't currently sell it). U128 is a Part 2 candidate.

---

## PART 2 — Add MSA-only options

**File:** [`listing.py:837-868`](backend/app/api/v1/endpoints/listing.py#L837-L868)

**What it does in plain English:** Find articles that exist in the warehouse (`ARS_MSA_TOTAL`) but **don't** exist in the store's grid yet. Add them as new candidates with empty stock. Marked `IS_NEW = 1`.

**Hidden steps — what happens behind the scenes:**

1. **`SELECT DISTINCT` from MSA** — collapse `ARS_MSA_TOTAL` (one row per RDC × article) to just the unique `(MAJ_CAT, GEN_ART_NUMBER, CLR)` triples. We don't care which RDC has the stock yet — only that *some* RDC has it.
   *Example:* MSA might have 3 rows for `(M_KIDS, 1134114962, L_GRY)` — one per warehouse RDC. The DISTINCT collapses them to 1.

2. **`CROSS JOIN` with stores** — multiply the unique articles by every active store. For 38k MSA triples × 320 stores → 12.2M candidate rows.

3. **`NOT EXISTS` anti-join** — keep only candidates that don't *already* live in `ARS_LISTING` from Part 1. This prevents duplicates: if U128 already carries `1116111940 / WHT`, Part 2 won't re-insert it.

4. **Stock columns = 0, `IS_NEW = 1`** — the store has never sold this article. `STK_TTL = 0` naturally; the SLOC columns are also zero.

**Why this design:** The same article is offered to every store. Some stores already have it (Part 1 caught those); the others are new launch candidates (Part 2 picks them up). Together they cover the full universe.

**Key code:**

```sql
INSERT INTO ARS_LISTING (WERKS, RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, ..., STK_TTL, IS_NEW)
SELECT S.WERKS, S.RDC, M.MAJ_CAT, M.GEN_ART_NUMBER, M.CLR, ..., 0, 1
FROM (SELECT DISTINCT MAJ_CAT, GEN_ART_NUMBER, CLR FROM ARS_MSA_TOTAL) M
CROSS JOIN <stores> S
WHERE NOT EXISTS (
    SELECT 1 FROM ARS_LISTING L
    WHERE L.WERKS = S.WERKS AND L.MAJ_CAT = M.MAJ_CAT
      AND L.GEN_ART_NUMBER = M.GEN_ART_NUMBER AND L.CLR = M.CLR
)
```

**For U128 / 1134114962 / L_GRY:** ✅ **1 row inserted.** `STK_TTL = 0`, `IS_NEW = 1`. The article is now a candidate; the warehouse has stock for it but U128 has never carried it.

---

## PART 2.5 — Build indexes (skipped for small listings)

**File:** [`listing.py:870-892`](backend/app/api/v1/endpoints/listing.py#L870-L892)

**What it does:** Build 3-/4-column indexes on `ARS_LISTING` to speed up the joins in Parts 3–4. Skipped if listing < 5,000 rows (the index overhead beats the gain).

**For U128:** Indexes built (production has > 200k rows).

---

## PART 3.5 — Fetch ACS_D + ALC_D

**File:** [`listing.py:904-929`](backend/app/api/v1/endpoints/listing.py#L904-L929)

**What it does in plain English:** For each row, look up "how many pieces does this store sell per day in this category?" (`ACS_D`) and "how many days of stock to plan for?" (`ALC_D`) from `ARS_CALC_ST_MAJ_CAT`.

**Hidden steps — what happens behind the scenes:**

1. **Source already exists.** `ARS_CALC_ST_MAJ_CAT` is built by the **Contribution pipeline** *before* Listing Generation runs. If you re-run Contrib upstream, these numbers change. If you skip Contrib, the values are stale.

2. **One row per `(ST_CD, MAJ_CAT)`.** The table is small (320 stores × 242 MAJ_CATs ≈ 77k rows). The `UPDATE` is a fast indexed equi-join — no row explosion.

3. **What `ACS_D` and `ALC_D` actually mean:**
   - `ACS_D` = *"how many pieces of this MAJ_CAT does this store sell per day, on average"*. Computed upstream as `total_sales / sales_days`. Used as the demand baseline.
   - `ALC_D` = *"allocation density"* — how many days of stock the store should hold. Together they drive `OPT_MBQ = ACS_D + ALC_D × <…>` in Part 4c.

4. **NULLs left behind.** Stores with no Contrib history (brand-new stores) get `NULL` for both columns. Downstream code defends with `ISNULL(NULLIF(ACS_D, 0), default_acs_d)` — falls back to 18 by default.

**Example for U128 / M_KIDS:**
- Contrib pipeline computed: last 90 days, U128 sold 900 M_KIDS pieces in 90 days → `ACS_D = 10`.
- `ALC_D` configured to 1 (one day's display).
- After Part 3.5: row has `ACS_D = 10`, `ALC_D = 1`.

If Contrib had been skipped or the store is new, both would be NULL, and Part 4c would compute MBQ using `default_acs_d = 18`.

**Key code:**

```sql
UPDATE L SET L.ACS_D = C.ACS_D, L.ALC_D = C.ALC_D
FROM ARS_LISTING L
INNER JOIN ARS_CALC_ST_MAJ_CAT C
    ON L.WERKS = C.ST_CD AND L.MAJ_CAT = C.MAJ_CAT
```

**For U128 / 1134114962 / L_GRY:** `ACS_D ≈ 10` pieces/day for M_KIDS at U128. `ALC_D ≈ 1` (typical).

---

## PART 3.5a — LISTING flag, I_ROD, CLR limits, FOCUS flags

**File:** [`listing.py:931-996`](backend/app/api/v1/endpoints/listing.py#L931-L996)

**What it does in plain English:** Stamp each row with the store/category settings that the allocator will need later — *"is this MAJ_CAT switched on for this store?"*, *"how many rounds of display does the allocator run?"*, *"is this a focus option that gets priority?"*. The values come from two sources and are applied in a **two-step cascade**.

**The cascade — article-level beats category-level.**

Each column has a 2-level fallback:

```
   ┌────────────────────────────────────────┐
   │ Step 1: ARS_CALC_ST_MAJ_CAT            │  ← coarse-grained
   │   (store × MAJ_CAT)                    │     defaults
   │   sets: LISTING, I_ROD,                │
   │         CLR_MIN, CLR_MAX               │
   └────────────────────┬───────────────────┘
                        │
                        ▼
   ┌────────────────────────────────────────┐
   │ Step 2: ARS_CALC_ST_ART (cascade)      │  ← fine-grained
   │   (store × MAJ_CAT × GEN_ART × CLR)    │     override
   │   overrides: LISTING, I_ROD,           │
   │              FOCUS_W_CAP, FOCUS_WO_CAP │
   │   Only if value present AND ≠ '' / '0' │
   └────────────────────┬───────────────────┘
                        │
                        ▼
   ┌────────────────────────────────────────┐
   │ Step 3: hard override                  │  ← always wins
   │   CLR IN ('A', 'A_MIX') → I_ROD = 2    │
   └────────────────────────────────────────┘
```

**Columns set in each step:**

| Step | Source table | Grain | Columns it writes |
|---|---|---|---|
| 1 | `ARS_CALC_ST_MAJ_CAT` | `(WERKS, MAJ_CAT)` | `LISTING`, `I_ROD`, `CLR_MIN`, `CLR_MAX` |
| 2 | `ARS_CALC_ST_ART` (cascade) | `(WERKS, MAJ_CAT, GEN_ART, [CLR])` | `LISTING` (override), `I_ROD` (override), `FOCUS_W_CAP`, `FOCUS_WO_CAP` |
| 3 | (literal CASE) | All rows | `I_ROD = 2` when `CLR IN ('A', 'A_MIX')` |

**Column meanings:**

| Column | Meaning |
|---|---|
| `LISTING` | 1 = this OPT is administratively enabled at this store (filter applied in Part 7). |
| `I_ROD` | Implied Rounds of Display — how many "rounds" the allocator runs for this OPT. RL/TBC usually 1; TBL usually 2 (one ALLOC + one HOLD). |
| `CLR_MIN`, `CLR_MAX` | Per-category colour-count limits — informational, used by colour-cap logic downstream. |
| `FOCUS_W_CAP` | 1 = focus option with cap; drives priority tier 2 in Stage A. |
| `FOCUS_WO_CAP` | 1 = focus option without cap; drives priority tier 1 (highest) in Stage A. |

**Hidden steps:**

1. **DDL guard** — `ALTER TABLE ARS_LISTING ADD [col] FLOAT NULL` for each of the 6 columns. Wrapped in try/except so re-runs are idempotent.
2. **Step 1 `UPDATE`** — equi-join on `(WERKS, MAJ_CAT)`. Always overwrites — even with NULL — because this is the baseline.
3. **Step 2 `UPDATE`** — equi-join on `(WERKS, MAJ_CAT, GEN_ART_NUMBER, [CLR])`. Uses a **per-column CASE** so each column is overridden independently:
   ```sql
   L.[LISTING] = CASE
       WHEN A.[LISTING] IS NOT NULL
        AND LTRIM(RTRIM(CAST(A.[LISTING] AS NVARCHAR(50)))) NOT IN ('', '0')
           THEN TRY_CAST(A.[LISTING] AS FLOAT)
       ELSE L.[LISTING]                  -- keep Step 1's value
   END
   ```
   The `NOT IN ('', '0')` guard is important: a missing article-level setting (NULL, empty string, or literal '0') leaves the MAJ_CAT-level default untouched. Only a *real* override fires.
4. **Step 3 hard override** — unconditional `UPDATE` setting `I_ROD = 2` for the 'A' / 'A_MIX' colour codes. This is a display-density floor: aggregated colour buckets need an extra round of display capacity. Runs after Step 2, so it beats both cascade sources.

**Key code (Steps 1 + 2):**

```sql
-- Step 1: MAJ_CAT baseline
UPDATE L SET L.LISTING = TRY_CAST(C.LISTING AS FLOAT),
             L.I_ROD   = TRY_CAST(C.I_ROD   AS FLOAT),
             L.CLR_MIN = TRY_CAST(C.CLR_MIN AS FLOAT),
             L.CLR_MAX = TRY_CAST(C.CLR_MAX AS FLOAT)
FROM ARS_LISTING L
INNER JOIN ARS_CALC_ST_MAJ_CAT C
    ON L.WERKS = C.ST_CD AND L.MAJ_CAT = C.MAJ_CAT;

-- Step 2: article-level cascade override
UPDATE L SET
    L.LISTING      = CASE WHEN A.LISTING      IS NOT NULL AND ... THEN A.LISTING      ELSE L.LISTING      END,
    L.I_ROD        = CASE WHEN A.I_ROD        IS NOT NULL AND ... THEN A.I_ROD        ELSE L.I_ROD        END,
    L.FOCUS_W_CAP  = CASE WHEN A.FOCUS_W_CAP  IS NOT NULL AND ... THEN A.FOCUS_W_CAP  ELSE L.FOCUS_W_CAP  END,
    L.FOCUS_WO_CAP = CASE WHEN A.FOCUS_WO_CAP IS NOT NULL AND ... THEN A.FOCUS_WO_CAP ELSE L.FOCUS_WO_CAP END
FROM ARS_LISTING L
INNER JOIN ARS_CALC_ST_ART A
    ON L.WERKS = A.ST_CD AND L.MAJ_CAT = A.MAJ_CAT
   AND L.GEN_ART_NUMBER = A.GEN_ART_NUMBER
   AND L.CLR = A.CLR;

-- Step 3: hard override for aggregated-colour codes
UPDATE ARS_LISTING SET I_ROD = 2
WHERE UPPER(LTRIM(RTRIM(CLR))) IN ('A', 'A_MIX');
```

**Notes:**

- **`MANUAL_DENSITY` is NOT enriched here.** It's an article-level density override used by Part 4c (`ACS_D` override), sourced separately. Don't expect to see it in Part 3.5a's columns.
- **Index seek vs scan.** Step 2's per-column CASE was chosen specifically to keep the 4-column equi-join clean. The old approach used a single OR-heavy WHERE that blocked index seeks on the 6M-row join.
- **What if both sources are missing?** All four columns stay NULL → downstream Part 3.6 treats NULL `LISTING` as `1` (`ISNULL(TRY_CAST([LISTING] AS INT), 1) = 1`) and NULL `I_ROD` as `1` (`ISNULL(I_ROD, 1)` in Stage A.5).

**For U128 / 1134114962 / L_GRY:**

| Step | What happens | Result |
|---|---|---|
| 1 | `ARS_CALC_ST_MAJ_CAT` has `(U128, M_KIDS)` row with `LISTING=1, I_ROD=2, CLR_MIN=2, CLR_MAX=5` | Baseline set |
| 2 | `ARS_CALC_ST_ART` lookup for `(U128, M_KIDS, 1134114962, L_GRY)` — no row (new article at U128) | No override; Step 1 values stay |
| 3 | `CLR = 'L_GRY'` (not 'A' or 'A_MIX') | No I_ROD hard override (the value remains the `I_ROD=2` from Step 1) |

Final: `LISTING = 1`, `I_ROD = 2`, `CLR_MIN = 2`, `CLR_MAX = 5`, `FOCUS_W_CAP = NULL`, `FOCUS_WO_CAP = NULL`.

> **Why `I_ROD = 2` here**: the M_KIDS MAJ_CAT baseline in `ARS_CALC_ST_MAJ_CAT` is configured to 2 for U128 — this is what feeds Stage C's round-2 logic in the TBL pass. Note that `I_ROD = 2` is **not** automatic for all TBL rows; it's whatever Step 1 / Step 2 / Step 3 produced. A different MAJ_CAT (or a CLR='A'/'A_MIX' override) might yield `I_ROD = 1` or `I_ROD = 3`.

---

## PART 3.5b — AUTO_GEN_ART_SALE

**File:** [`listing.py:990-1011`](backend/app/api/v1/endpoints/listing.py#L990-L1011)

**What it does:** Pull `SAL_PD` (per-day sale) from `MASTER_GEN_ART_SALE` into `AUTO_GEN_ART_SALE`. This is article-level sales velocity (not store-level).

**For U128 / 1134114962 / L_GRY:** Populated from the master table.

---

## PART 3.5c — AGE

**File:** [`listing.py:1014-1036`](backend/app/api/v1/endpoints/listing.py#L1014-L1036)

**What it does:** Populate `AGE` (days since the article was first stocked) from `MASTER_GEN_ART_AGE`.

**Hidden quirk:** If the master table is missing or has no entry, `AGE` stays NULL — downstream logic handles NULL as "unknown / new".

**For U128 / 1134114962 / L_GRY:** `AGE` = small (newly arrived in catalogue).

---

## PART 3.54 — RL_HOLD_QTY from prior-run holds

**File:** [`listing.py:1038-1093`](backend/app/api/v1/endpoints/listing.py#L1038-L1093)

**What it does in plain English:** Look at the **warehouse-side hold tracking** table (`ARS_NL_TBL_HOLD_TRACKING`). If a previous run reserved stock for this (store, article, colour), pick up the `HOLD_REM` value as `RL_HOLD_QTY`. This is *carry-over* from earlier cycles.

**Hidden steps — what happens behind the scenes:**

1. **What is a "hold"?** When a TBL allocation ships stock to a store in a *previous* cycle, the system also reserves extra warehouse stock for that store's follow-up top-ups. That reserved stock lives in `ARS_NL_TBL_HOLD_TRACKING` as one row per `(WERKS, VAR_ART, SZ)` with `HOLD_REM` showing what's left to ship.

2. **Only open holds counted (`IS_CLOSED = 0`).** Closed holds were either fully consumed (the store has the stock now and it shows in `STK_TTL`) or aged out. Counting them would double-count.
   *Example:* a hold row with `HOLD_QTY_INITIAL = 5, HOLD_REM = 2, IS_CLOSED = 0` → contributes 2. A second row with `HOLD_QTY_INITIAL = 3, HOLD_REM = 0, IS_CLOSED = 1` → contributes 0.

3. **Aggregate to GEN_ART grain.** Hold tracking is at `(WERKS, VAR_ART, SZ)` (variant × size). The listing is at `(WERKS, MAJ_CAT, GEN_ART, CLR)`. SUM across all variants × sizes of the same article into one `RL_HOLD_QTY` value.
   *Example:* a store has 3 hold rows for article 1116111940 / WHT: VAR_001 size M (HOLD_REM=2) + VAR_001 size L (HOLD_REM=3) + VAR_002 size M (HOLD_REM=1) → `RL_HOLD_QTY = 6`.

4. **Why this matters.** A non-zero `RL_HOLD_QTY` is treated by Part 3.6 OPT_TYPE classification as *"there is something pending for this store"* — it can rescue a row from MIX even when `MSA_FNL_Q = 0`. It also lets RL fire for an article that would otherwise look empty.

**For U128 / 1134114962 / L_GRY:** No prior holds for this article at U128 (article never shipped here before). `RL_HOLD_QTY = 0`.

**Key code:**

```sql
;WITH HoldAgg AS (
    SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR,
           SUM(HOLD_REM) AS hold_qty
    FROM ARS_NL_TBL_HOLD_TRACKING
    WHERE IS_CLOSED = 0
    GROUP BY WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR
)
UPDATE L SET L.RL_HOLD_QTY = ISNULL(H.hold_qty, 0)
FROM ARS_LISTING L
LEFT JOIN HoldAgg H ON L.WERKS=H.WERKS AND L.MAJ_CAT=H.MAJ_CAT
                   AND L.GEN_ART_NUMBER=H.GEN_ART_NUMBER AND L.CLR=H.CLR
```

**For U128 / 1134114962 / L_GRY:** `RL_HOLD_QTY = 0` (no prior holds for this article at U128).

---

## PART 3.55 — MSA_FNL_Q + VAR_COUNT + VAR_FNL_COUNT

**File:** [`listing.py:1095-1161`](backend/app/api/v1/endpoints/listing.py#L1095-L1161)

**What it does:**
1. `MSA_FNL_Q` ← warehouse stock available, from `ARS_MSA_GEN_ART.FNL_Q`.
2. `VAR_COUNT` ← number of variants this `(GEN_ART, CLR)` has in `ARS_MSA_VAR_ART`.
3. `VAR_FNL_COUNT` ← number of those variants with `FNL_Q > 0` (i.e. warehouse actually has stock).

**Hidden steps — what happens behind the scenes:**

1. **Two separate joins, two different grains.**
   - `MSA_FNL_Q` comes from `ARS_MSA_GEN_ART` (GEN_ART grain — one row per `(RDC, MAJ_CAT, GEN_ART, CLR)`). It's the **total** warehouse stock for the article.
   - `VAR_COUNT` and `VAR_FNL_COUNT` come from `ARS_MSA_VAR_ART` (variant grain — one row per `(RDC, MAJ_CAT, GEN_ART, CLR, VAR_ART)`). They count *how many variants* exist, and how many have stock.

2. **Why count variants separately?** Because the **60% size-coverage rule** runs against variants, not against total stock. An article with 100 units total all in one variant is *worse* than 30 units spread across 3 variants — the spread one provides better in-store experience.
   *Example:* `MSA_FNL_Q = 100` but only `VAR_FNL_COUNT = 1` of `VAR_COUNT = 3` → coverage = 33%. The rule will treat this as sparse, even though the total stock is high.

3. **The three numbers all feed downstream gates:**
   - `MSA_FNL_Q` → R04 (empty-warehouse check) and OPT_TYPE branch 1/3/4/5.
   - `VAR_FNL_COUNT / VAR_COUNT` → R07 (60% size coverage) and Part 3.6 MIX(b).
   - `VAR_FNL_COUNT < min_size_count` → the absolute-count half of R07.

4. **Filter sentinel.** Rows with `VAR_COUNT = 0` (no variants resolved — e.g. a recently-created article) bypass the 60% rule downstream because `0/0` can't be evaluated.

**For U128 / 1134114962 / L_GRY:**
- `ARS_MSA_GEN_ART` row for `(R1, M_KIDS, 1134114962, L_GRY)` has `FNL_Q = 38` → `MSA_FNL_Q = 38`.
- `ARS_MSA_VAR_ART` has 3 rows for the 3 variants with `FNL_Q = 34, 3, 1` → `VAR_COUNT = 3`, `VAR_FNL_COUNT = 3` (all three > 0).
- Size coverage = `3/3 = 100%` ≥ 60% ✅

**Key code:**

```sql
UPDATE L SET L.MSA_FNL_Q = ISNULL(M.FNL_Q, 0)
FROM ARS_LISTING L
LEFT JOIN ARS_MSA_GEN_ART M
  ON L.MAJ_CAT=M.MAJ_CAT AND L.GEN_ART_NUMBER=M.GEN_ART_NUMBER AND L.CLR=M.CLR;

;WITH VarAgg AS (
  SELECT MAJ_CAT, GEN_ART_NUMBER, CLR,
         COUNT(*) AS var_count,
         SUM(CASE WHEN FNL_Q > 0 THEN 1 ELSE 0 END) AS var_fnl_count
  FROM ARS_MSA_VAR_ART GROUP BY MAJ_CAT, GEN_ART_NUMBER, CLR
)
UPDATE L SET L.VAR_COUNT=V.var_count, L.VAR_FNL_COUNT=V.var_fnl_count
FROM ARS_LISTING L INNER JOIN VarAgg V ON ...
```

**For U128 / 1134114962 / L_GRY:**
- `MSA_FNL_Q = 38` (34 + 3 + 1 across the 3 variants)
- `VAR_COUNT = 3`
- `VAR_FNL_COUNT = 3` (all 3 variants have some warehouse stock)
- **Size coverage = 3/3 = 100% ≥ 60%** ✅

---

## PART 3.6 — Tag OPT_TYPE (MIX / RL / TBC / TBL)

**File:** [`listing.py:1196-1226`](backend/app/api/v1/endpoints/listing.py#L1196-L1226)

**What it does in plain English:** This is the **most important decision** in the whole pipeline. The 6 branches in order — first match wins:

```
1. Empty store AND empty warehouse AND no hold?            → MIX (nothing to do)
2. Sparse sizes? (ratio < 60% OR count < MinSz)            → MIX (bad shopping experience)
3. Adequate stock AND warehouse has supply?                → RL  (top up)
4. Some stock but below target AND supply exists?          → TBC (partial top-up)
5. Empty store AND warehouse/hold has supply?              → TBL (fresh launch)
6. Otherwise                                               → MIX
```

**Key code:**

```sql
UPDATE ARS_LISTING SET OPT_TYPE = CASE
    -- MIX (a)
    WHEN ISNULL(STK_TTL,0) < {threshold} * ISNULL(NULLIF(ACS_D,0), {default_acs})
     AND ISNULL(MSA_FNL_Q,0) = 0 AND ISNULL(RL_HOLD_QTY,0) = 0
        THEN 'MIX'
    -- MIX (b): sparse colour fill
    WHEN ISNULL(VAR_COUNT,0) > 0
     AND (CAST(ISNULL(VAR_FNL_COUNT,0) AS FLOAT) / VAR_COUNT < {threshold}
          OR ISNULL(VAR_FNL_COUNT,0) < {min_size_count})
        THEN 'MIX'
    -- RL
    WHEN (ISNULL(STK_TTL,0) >= {threshold} * ISNULL(NULLIF(ACS_D,0), {default_acs})
          OR ISNULL(RL_HOLD_QTY,0) > 0)
     AND ISNULL(MSA_FNL_Q,0) > 0
        THEN 'RL'
    -- TBC
    WHEN ISNULL(STK_TTL,0) > 0
     AND STK_TTL < {threshold} * ISNULL(NULLIF(ACS_D,0), {default_acs})
     AND (ISNULL(MSA_FNL_Q,0) > 0 OR ISNULL(RL_HOLD_QTY,0) > 0)
        THEN 'TBC'
    -- TBL
    WHEN ISNULL(STK_TTL,0) <= 0
     AND (ISNULL(MSA_FNL_Q,0) > 0 OR ISNULL(RL_HOLD_QTY,0) > 0)
        THEN 'TBL'
    ELSE 'MIX'
END
```

**For U128 / 1134114962 / L_GRY:** Walk each branch:

| Branch | Check | Result |
|---|---|---|
| 1 MIX(a) | `STK_TTL=0 < 6` ✅ AND `MSA=0`? **No (38)** | Skip |
| 2 MIX(b) | `3/3 = 100% < 60%`? **No.** Or `3 < 3`? **No** | Skip |
| 3 RL | `STK_TTL=0 ≥ 6`? **No.** Or `HOLD>0`? **No** | Skip |
| 4 TBC | `STK_TTL=0 > 0`? **No** | Skip |
| 5 TBL | `STK_TTL=0 ≤ 0` ✅ AND `MSA=38 > 0` ✅ | **MATCH → TBL** |

→ **`OPT_TYPE = TBL`** ✅

---

## PART 3.7 — MIX aggregation

**File:** [`listing.py:1266-1389`](backend/app/api/v1/endpoints/listing.py#L1266-L1389)

**What it does:** Collapse all MIX rows into a single row per `(WERKS, MAJ_CAT)` (or other grain depending on `mix_mode`), summing numeric columns. This shrinks the listing dramatically since MIX rows can be tens of thousands.

**For U128 / 1134114962 / L_GRY:** ❌ Not affected (it's TBL, not MIX).

---

## PART 4 (pre-resolve) — MP columns onto listing

**File:** [`listing.py:1391-1476`](backend/app/api/v1/endpoints/listing.py#L1391-L1476)

**What it does:** Pre-fetch master-product columns (`FAB`, `MACRO_MVGR`, `MICRO_MVGR`, `M_VND_CD`, `RNG_SEG`, …) from `vw_master_product` onto `ARS_LISTING` so the next Part-4 joins are direct (no repeated 5M-row MP joins).

**For U128 / 1134114962 / L_GRY:** MP columns populated (specific values don't matter for this trace; they're used by grid joins next).

---

## PART 4a — Grid column joins

**File:** [`listing.py:1478-1546`](backend/app/api/v1/endpoints/listing.py#L1478-L1546)

**What it does:** For each active grid in `ARS_GRID_BUILDER` (MJ + secondary grids like MJ_FAB, MJ_MVGR), JOIN its `MBQ`, `STK_TTL`, `CONT`, `OPT_CNT`, `DISP_Q` columns onto the listing. The result: every row knows its MBQ at every hierarchy level (MJ-level, FAB-level, MVGR-level, etc.).

**For U128 / 1134114962 / L_GRY:** `MJ_MBQ`, `MJ_STK_TTL`, `MJ_CONT` populated for U128 / M_KIDS.

---

## PART 4b — PER_OPT_SALE

**File:** [`listing.py:1548-1578`](backend/app/api/v1/endpoints/listing.py#L1548-L1578)

**What it does:** Copy `PER_OPT_SALE` from whichever grid is flagged `use_for_opt_sale = 1` (typically MJ-level). This is the "expected daily sale per option".

---

## PART 4c — OPT_MBQ, OPT_REQ, OPT_MBQ_WH, MAX_DAILY_SALE

**File:** [`listing.py:1580-1739`](backend/app/api/v1/endpoints/listing.py#L1580-L1739)

**What it does in plain English:** Compute *how many pieces of this article U128 should ultimately have on the shelf*, per option. This is **the per-store target**.

**Hidden steps — what happens behind the scenes:**

1. **`MANUAL_DENSITY` override (first).** Before computing anything, the system checks `MANUAL_DENSITY` at the `(WERKS, GEN_ART, CLR)` grain. If a merchant has manually set a density value for this specific article-at-this-store, that value **replaces `ACS_D`** for the OPT_MBQ computation. Merchants use this to force a higher target on hot articles.
   *Example:* default `ACS_D = 10` for U128 / M_KIDS. But if a merchant set `MANUAL_DENSITY = 25` for U128 / 1134114962 / L_GRY (treat this article like 25 pieces/day demand), the override kicks in. For our case, no manual density exists → `ACS_D = 10` stays.

2. **Compute `OPT_MBQ`** — the target shelf quantity per OPT per store.
   - Formula: `OPT_MBQ = ROUND(ACS_D + ALC_D × <density adjustment>, 0)`.
   - Plain English: "how many pieces should this store keep on the shelf so they don't run out before the next replenishment".
   *Example:* `ACS_D = 10, ALC_D = 1, density adjustment factor ≈ 1.2` → `OPT_MBQ ≈ 22`.

3. **Compute `OPT_REQ`** — the unmet portion = how much we need to ship just to *reach* target (no warehouse buffer).
   - `OPT_REQ = MAX(0, OPT_MBQ − STK_TTL)`. The `MAX(0, …)` prevents negative requests when a store is over-stocked.
   *Example:* `OPT_MBQ = 22, STK_TTL = 0` → `OPT_REQ = 22`. If `STK_TTL` were 25 (over-stocked), `OPT_REQ = 0`.

4. **Compute `OPT_MBQ_WH`** — same as `OPT_MBQ` but with extra warehouse-hold buffer for TBL launches.
   - `OPT_MBQ_WH = OPT_MBQ + hold_days × per-day-sale`. The buffer ensures the warehouse keeps reserve stock for the next top-up.
   *Example:* `OPT_MBQ = 22, hold_days = 14, per-day-sale = 0.5` → `OPT_MBQ_WH = 22 + 7 = 29`.

5. **Compute `OPT_REQ_WH`** — buffered version of OPT_REQ.
   - `OPT_REQ_WH = MAX(0, OPT_MBQ_WH − STK_TTL)`. This is what the allocator's waterfall actually uses (so the TBL hold is built into the pool draw).
   *Example:* `OPT_MBQ_WH = 29, STK_TTL = 0` → `OPT_REQ_WH = 29`. (Round 1 of TBL will try to take 29 from the pool — some goes to ALLOC, rest to HOLD.)

6. **Populate `MAX_DAILY_SALE`** — per-day sale velocity (from `AUTO_GEN_ART_SALE` populated in Part 3.5b). Used as a Stage A tiebreaker for OPT_PRIORITY_RANK.
   *Example:* `MAX_DAILY_SALE ≈ 0.5` (an average baby-clothing article sells ~half a piece per day across stores).

**Why this order matters:** `MANUAL_DENSITY` MUST override `ACS_D` *before* OPT_MBQ is computed — otherwise the manual target would have no effect. `OPT_MBQ_WH ≥ OPT_MBQ` is invariant — break that and the TBL HOLD math goes negative.

**For U128 / 1134114962 / L_GRY:**
- `OPT_MBQ = 22`
- `OPT_REQ = 22 − 0 = 22` (shelf is empty)
- `OPT_MBQ_WH > 22` (includes hold buffer)
- `OPT_REQ_WH > 22` likewise
- `MAX_DAILY_SALE` populated

---

## PART 4d — ART_EXCESS

**File:** [`listing.py:1741-1770`](backend/app/api/v1/endpoints/listing.py#L1741-L1770)

**What it does:** `ART_EXCESS = MAX(0, STK_TTL − eff_mult × OPT_MBQ)` per row — measures how much a store is *over-stocked*. MIX rows excluded.

**For U128 / 1134114962 / L_GRY:** `ART_EXCESS = MAX(0, 0 − k × 22) = 0` (no excess — shelf is empty).

---

## PART 4e — Per-grid REQ with excess deduction

**File:** [`listing.py:1772-1862`](backend/app/api/v1/endpoints/listing.py#L1772-L1862)

**What it does:** For each grid, aggregate `ART_EXCESS` per hierarchy keys, deduct from `{grid}_STK_TTL`, then compute `{grid}_REQ = MAX(0, {grid}_MBQ − deducted_stk)`.

**For U128 / 1134114962 / L_GRY:** `MJ_REQ` populated for U128 × M_KIDS based on `MJ_MBQ − adjusted_MJ_STK_TTL`.

---

## PART 5 — Final indexes

**File:** [`listing.py:1864-1873`](backend/app/api/v1/endpoints/listing.py#L1864-L1873)

**What it does:** Extra WERKS/RDC indexes for the Part-6/7/8 queries.

---

## PART 6 — Store ranking

**File:** [`listing.py:1875-1939`](backend/app/api/v1/endpoints/listing.py#L1875-L1939)

**What it does in plain English:** Decide *which store gets first crack at the warehouse pool*. Stores with bigger unmet demand and lower current fill rate rank higher.

**Hidden steps — what happens behind the scenes:**

1. **Aggregate to one row per `(MAJ_CAT, WERKS)`.** A store has many articles in M_KIDS; we want one *store-level* number per category. The system uses `MAX` of `MJ_REQ`, `MJ_MBQ`, `MJ_STK_TTL`, `ACS_D` — they're identical across rows of the same `(WERKS, MAJ_CAT)` so MAX picks the one good value while collapsing duplicates. Also computes `FILL_RATE = MJ_STK / MJ_MBQ` for each store.
   *Example:* U128 has 87 M_KIDS articles in the listing → collapse to 1 row: `MJ_REQ = 480, MJ_MBQ = 600, MJ_STK = 120, FILL_RATE = 120/600 = 0.20`. Another store HN14 in M_KIDS: `MJ_REQ = 50, MJ_MBQ = 600, MJ_STK = 550, FILL_RATE = 0.92`.

2. **Compute `REQ_RANK`** — `DENSE_RANK OVER (PARTITION BY MAJ_CAT ORDER BY MJ_REQ ASC)`. Note the `ASC` — smaller request = lower rank number = served first. **Counterintuitive but deliberate**: a store with low `MJ_REQ` already has most of what it needs, so it gets first crack at any remaining pool to be "almost full" rather than leaving it 70% short.
   *Example:* HN14 with `MJ_REQ = 50` gets `REQ_RANK = 1`. U128 with `MJ_REQ = 480` gets a higher number (say `REQ_RANK = 250`).

3. **Compute `FILL_RANK`** — `DENSE_RANK OVER (PARTITION BY MAJ_CAT ORDER BY FILL_RATE DESC)`. Higher fill = lower rank = served first. Symmetric logic to REQ_RANK.
   *Example:* HN14 fill = 0.92 → `FILL_RANK = 1`. U128 fill = 0.20 → `FILL_RANK = 200` or so.

4. **Compute `W_SCORE`** — weighted sum of both. Default weights `req_weight = 0.4, fill_weight = 0.6`. Both feeds favor near-full stores.
   `W_SCORE = REQ_RANK × 0.4 + FILL_RANK × 0.6`
   *Example:* HN14 → `1 × 0.4 + 1 × 0.6 = 1.0`. U128 → `250 × 0.4 + 200 × 0.6 = 220.0`.

5. **Assign `ST_RANK`** — `ROW_NUMBER OVER (PARTITION BY MAJ_CAT ORDER BY W_SCORE DESC, WERKS ASC)`. **`DESC`** here — because lower W_SCORE = better, so ordering DESC by W_SCORE puts WORST stores first. Wait — that's wrong. Let me re-check.
   Actually the SQL is `ORDER BY W_SCORE DESC` but combined with the way W_SCORE is computed from `DENSE_RANK ASC` (low rank = good), a high W_SCORE means BAD store (high rank number). The `DESC` ordering then puts bad stores FIRST — `ST_RANK = 1` goes to the most-needy store, not the most-full.
   *Example:* If U128 has W_SCORE = 220 and HN14 has W_SCORE = 1.0, then sorting DESC: U128 gets `ST_RANK = 1` (served first), HN14 gets `ST_RANK = 320` (served last).
   The waterfall in Stage C uses `ST_RANK` as a tiebreaker — lower `ST_RANK` = takes pool first.

6. **Write `ARS_STORE_RANKING` then back-fill `ARS_LISTING.ST_RANK`.** Two writes: first build the per-store table for audit / UI, then propagate the rank back to every listing row so Stage C can use it without a re-join.

**Why this design:** The rank reflects "this store is most behind on this MAJ_CAT" — those stores get to pick warehouse pool first when scarcity hits. It's a fairness mechanism for unequal stores.

**For U128 / 1134114962 / L_GRY:** `ARS_STORE_RANKING` row for `(M_KIDS, U128)` set; back-filled onto every U128 / M_KIDS row in `ARS_LISTING`. Real value depends on how U128 compares to the other 319 stores' M_KIDS shortfalls.

**Key code:**

```sql
;WITH StoreAgg AS (
    SELECT MAJ_CAT, WERKS, MAX(MJ_REQ) AS MJ_REQ, MAX(MJ_MBQ) AS MJ_MBQ,
           MAX(MJ_STK_TTL) AS MJ_STK, MAX(ACS_D) AS ACS_D,
           CASE WHEN MAX(MJ_MBQ)=0 THEN 0
                ELSE ROUND(MAX(MJ_STK_TTL)/NULLIF(MAX(MJ_MBQ),0), 4) END AS FILL_RATE
    FROM ARS_LISTING WHERE OPT_TYPE <> 'MIX'
    GROUP BY MAJ_CAT, WERKS
), Ranked AS (
    SELECT *,
        DENSE_RANK() OVER (PARTITION BY MAJ_CAT ORDER BY MJ_REQ ASC)    AS REQ_RANK,
        DENSE_RANK() OVER (PARTITION BY MAJ_CAT ORDER BY FILL_RATE DESC) AS FILL_RANK
    FROM StoreAgg
)
SELECT *, REQ_RANK*0.4 + FILL_RANK*0.6 AS W_SCORE,
       ROW_NUMBER() OVER (PARTITION BY MAJ_CAT
           ORDER BY REQ_RANK*0.4+FILL_RANK*0.6 DESC, WERKS ASC) AS ST_RANK
INTO ARS_STORE_RANKING FROM Ranked;
```

**For U128 / 1134114962 / L_GRY:** `ST_RANK` assigned — let's say **U128 gets rank 4 within M_KIDS** (depends on the specific data).

---

## PART 7 — Filter to working table + ALLOC_FLAG

**File:** [`listing.py:1941-2210`](backend/app/api/v1/endpoints/listing.py#L1941-L2210)

**What it does in plain English:** Trim `ARS_LISTING` down to only the rows worth allocating, and stamp `ALLOC_FLAG`. The output is `ARS_LISTING_WORKING`.

**Filter conditions (all must pass):**

| Condition | Why |
|---|---|
| `MSA_FNL_Q > 0 OR RL_HOLD_QTY > 0` | Something to ship |
| `OPT_REQ_WH ≥ 1` | The request rounds to ≥ 1 piece |
| `OPT_TYPE ≠ 'TBL' OR VAR_COUNT = 0 OR VAR_FNL_COUNT/VAR_COUNT ≥ threshold [OR VAR_FNL_COUNT ≥ MinSz]` | TBL must have ≥ 60% size coverage (or absolute count ≥ MinSz). Rows with `VAR_COUNT=0` bypass the gate (no variants resolved yet — handled downstream by Stage B explode). |
| `LISTING = 1` | Administratively enabled |
| `MJ_DISP_Q > 0` | **(new 2026-05-25)** Store must have positive MAJ_CAT display capacity. If the store has zero display slots for this category, no OPT can be allocated there. |

**Hidden steps — what happens behind the scenes (after the SELECT INTO):**

1. **Growth scaling — create `MJ_MBQ_REV` and `MJ_REQ_REV`.** If the UI knob `mj_req_growth_pct` is set to something other than 100 (e.g. 110 for a 10% lift), create sibling columns:
   - `MJ_MBQ_REV = MJ_MBQ × mj_req_growth_pct / 100`
   - `MJ_REQ_REV = MAX(0, MJ_MBQ_REV − MJ_STK_TTL)` (re-derived from the scaled MBQ)
   The original `MJ_MBQ` is **never mutated** — it's the source-of-truth target for next cycle's calculations. The REV columns are what every downstream engine consumer (revalidate, OPT_MJ_REQ gate, store-broken pre-band) reads, so the growth lift flows everywhere with zero extra code.
   *Example:* U128 / M_KIDS has `MJ_MBQ = 600, MJ_STK_TTL = 120`. With `mj_req_growth_pct = 110` → `MJ_MBQ_REV = 660, MJ_REQ_REV = MAX(0, 660 − 120) = 540`. (At growth=100, both REV columns equal their non-REV counterparts — back-compat.)

2. **Hierarchy columns sync.** Refresh the columns of `ARS_GRID_HIERARCHY` table (`WERKS, MAJ_CAT, <plus one column per active non-article grid>`). Used downstream to figure out the cap/REQ tracking per grouping level.
   *Example:* If active grids include MJ, MJ_FAB, MJ_MICRO_MVGR, the hierarchy table will have columns `WERKS, MAJ_CAT, FAB, MICRO_MVGR`.

3. **Stamp `ALLOC_FLAG`.** Set to 1 only when `PRI_CT% ≥ 100` — i.e., the OPT's *Primary contribution coverage* is full (every primary grid cell the OPT touches has full contribution coverage).
   ```sql
   UPDATE ARS_LISTING_WORKING
   SET ALLOC_FLAG = CASE WHEN ISNULL([PRI_CT%], 0) >= 100 THEN 1 ELSE 0 END
   ```
   *Example:* U128 / 1134114962 / L_GRY: `PRI_CT% = 100` → `ALLOC_FLAG = 1`. If `PRI_CT% = 75` (incomplete coverage), `ALLOC_FLAG = 0` and the row would have gone to fallback — but fallback was removed in 2026-05-16, so 0 now effectively means "skip".

**For U128 / 1134114962 / L_GRY:** Row materialized into `ARS_LISTING_WORKING` with `MJ_MBQ_REV = 660, MJ_REQ_REV = 540, ALLOC_FLAG = 1`. Ready for Part 8.

**Key code (the ALLOC_FLAG stamp):**

```sql
UPDATE ARS_LISTING_WORKING
SET ALLOC_FLAG = CASE WHEN ISNULL([PRI_CT%], 0) >= 100 THEN 1 ELSE 0 END
```

**For U128 / 1134114962 / L_GRY:**
- `MSA_FNL_Q = 38 > 0` ✅
- `OPT_REQ_WH > 1` ✅
- TBL: `3/3 = 100% ≥ 0.6` ✅
- `LISTING = 1` ✅
- Row **kept** in `ARS_LISTING_WORKING`.
- `PRI_CT% = 100` → **`ALLOC_FLAG = 1`** ✅

---

## STAGE A.1 — Add rule-engine columns

**File:** [`rule_engine_new.py:253`](backend/app/services/rule_engine_new.py#L253) `_stage_a_add_columns`

**What it does:** ALTER `ARS_LISTING_WORKING` adding `LISTED_FLAG`, `LISTED_REASON`, `OPT_PRIORITY_TIER`, `OPT_PRIORITY_RANK`, `MSA_FNL_Q_REM`, `PRI_CT_REM`, `OPT_STATUS`, `ALLOC_QTY`, `HOLD_QTY`, …

**For U128 / 1134114962 / L_GRY:** Columns added with default NULL/0.

---

## STAGE A.2 — Apply rules R01–R09

**File:** [`rule_engine_new.py:285`](backend/app/services/rule_engine_new.py#L285) `_stage_a_apply_rules`

**What it does in plain English:** Build a comma-separated "reason string" from every rule that fails. If the string is empty → `LISTED_FLAG = 1` (eligible). Otherwise `LISTED_FLAG = 0` with the reasons.

**The rules (each appends a token to LISTED_REASON if it fails):**

| Rule | Check | Token |
|---|---|---|
| R01 | `LISTING ≠ 1` | `R01_LISTING;` |
| R02 | `OPT_TYPE = 'MIX'` | `R02_NOT_MIX;` |
| R04 | `MSA_FNL_Q ≤ 0 AND RL_HOLD_QTY ≤ 0` | `R04_MSA_POS;` |
| R05 | `OPT_REQ_WH < 1` | `R05_REQ_POS;` |
| R06 | `PRI_CT% < 100 AND ALLOC_FLAG ≠ 1 AND OPT_TYPE IN (enforced types)` | `R06_PRI_100;` |
| R07 | `OPT_TYPE='TBL' AND VAR_FNL/VAR_COUNT < threshold AND VAR_FNL < min_size_count` | `R07_VAR_RATIO_TBL;` |
| R09 | `MJ_MBQ × cap_factor − MJ_STK_TTL < 0.5 × ACS_D` | `R09_HEADROOM_TRIVIAL;` |

**Key code:**

```sql
UPDATE ARS_LISTING_WORKING SET
    LISTED_REASON = <R01-piece> + <R02-piece> + <R04-piece> + ... + <R09-piece>,
    LISTED_FLAG = CASE WHEN LEN(LISTED_REASON) = 0 THEN 1 ELSE 0 END
```

**For U128 / 1134114962 / L_GRY:** Walk every rule:

| Rule | Check | Result |
|---|---|---|
| R01 | `LISTING=1` | Pass ('') |
| R02 | `OPT_TYPE='TBL' ≠ 'MIX'` | Pass |
| R04 | `MSA=38 > 0` | Pass |
| R05 | `OPT_REQ_WH ≥ 1` | Pass |
| R06 | `PRI_CT%=100 ≥ 100` OR `ALLOC_FLAG=1` | Pass |
| R07 | `3/3 = 100% ≥ 60%` AND `3 ≥ MinSz` | Pass |
| R09 | `MJ_MBQ × 1.0 − MJ_STK_TTL ≥ 0.5 × ACS_D=5` | Pass |

→ `LISTED_REASON = ''` → **`LISTED_FLAG = 1`** ✅

---

## STAGE A.3 — Assign priority tier

**File:** [`rule_engine_new.py:474`](backend/app/services/rule_engine_new.py#L474) `_stage_a_assign_tier`

**What it does:** `OPT_PRIORITY_TIER` for `LISTED_FLAG = 1` rows:

```sql
CASE WHEN FOCUS_WO_CAP = 1 THEN 1   -- focus, no cap → highest priority
     WHEN FOCUS_W_CAP  = 1 THEN 2   -- focus, with cap
     ELSE 3 END                     -- regular
```

**For U128 / 1134114962 / L_GRY:** Focus flags = 0 → `OPT_PRIORITY_TIER = 3`.

---

## STAGE A.4 — Assign priority rank

**File:** [`rule_engine_new.py:487`](backend/app/services/rule_engine_new.py#L487) `_stage_a_assign_rank`

**What it does:** Per `(WERKS, OPT_TYPE, MAJ_CAT)` partition, sort options by:

1. `OPT_PRIORITY_TIER` ASC
2. `SEC_CT%` DESC
3. `MAX_DAILY_SALE` DESC
4. `OPT_REQ_WH` DESC
5. `GEN_ART_NUMBER`, `CLR` (tiebreaker)

Then `OPT_PRIORITY_RANK = ROW_NUMBER`.

**Key code:**

```sql
;WITH R AS (
    SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR,
           ROW_NUMBER() OVER (
               PARTITION BY WERKS, ISNULL(OPT_TYPE,''), MAJ_CAT
               ORDER BY ISNULL(OPT_PRIORITY_TIER,3) ASC,
                        ISNULL(TRY_CAST([SEC_CT%] AS FLOAT),0) DESC,
                        ISNULL(MAX_DAILY_SALE,0) DESC,
                        ISNULL(OPT_REQ_WH,0) DESC,
                        GEN_ART_NUMBER ASC, ISNULL(CLR,'') ASC
           ) AS rk
    FROM ARS_LISTING_WORKING WHERE LISTED_FLAG = 1
)
UPDATE W SET W.OPT_PRIORITY_RANK = R.rk
FROM ARS_LISTING_WORKING W INNER JOIN R ON ...
```

**For U128 / 1134114962 / L_GRY:** Within `(U128, TBL, M_KIDS)`, this article gets `OPT_PRIORITY_RANK = 4` (depends on how many other TBL OPTs U128 has with higher `SEC_CT%` / sales velocity).

---

## STAGE A.5 — Materialize ARS_LISTED_OPT

**File:** [`rule_engine_new.py:679`](backend/app/services/rule_engine_new.py#L679) `_stage_a_materialize_listed`

**What it does:** SELECT all `LISTED_FLAG = 1` rows from `ARS_LISTING_WORKING` INTO a new table `ARS_LISTED_OPT`. This is the *clean input* to Stage B.

**For U128 / 1134114962 / L_GRY:** **1 row materialized** with all enriched columns.

---

## STAGE B.1 — Explode to VAR_ART × SZ

**File:** [`rule_engine_new.py:719`](backend/app/services/rule_engine_new.py#L719) `_stage_b_explode`

**What it does in plain English:** Each row of `ARS_LISTED_OPT` (GEN_ART grain) is JOINed with `ARS_MSA_VAR_ART` (variant grain) and the size dimension to produce **one row per (WERKS, MAJ_CAT, GEN_ART, CLR, VAR_ART, SZ)** in `ARS_ALLOC_WORKING`. **This is where VAR_ART first enters the data**.

**Hidden steps — what happens behind the scenes:**

1. **The row count multiplies.** One listing row at GEN_ART grain becomes `VAR_COUNT × sizes_per_variant` rows in alloc grain. For an article with 3 variants × 1 size each → 3 alloc rows. For an article with 3 variants × 5 sizes each → 15 alloc rows.
   *Example:* U128 / 1134114962 / L_GRY: 1 listing row → **3 alloc rows** (each VAR_ART has exactly 1 SZ in this case).

2. **JOIN to `ARS_MSA_VAR_ART` for variant-level columns.** Every alloc row gets variant-specific data: `FNL_Q` per variant (NOT the GEN_ART roll-up), `SZ_STK` (per-size store stock from the variant grid).
   *Example:* The 3 alloc rows get `FNL_Q = 34, 3, 1` respectively (not all 38 — the GEN_ART-level total stays only on `ARS_LISTING_WORKING`).

3. **Carry-overs from the listing row.** These columns are duplicated onto every alloc row (one value × 3 rows for our example):
   - `OPT_MBQ, OPT_MBQ_WH, OPT_REQ_WH` — per-OPT targets (still GEN_ART grain values, will be split by `CONT` in Stage B.3).
   - All master-product attribute columns: `FAB, MACRO_MVGR, MICRO_MVGR, M_VND_CD, RNG_SEG, M_YARN_02, WEAVE_2, …` — needed downstream by the secondary-grid cap pre-gate and revalidation.
   - `ALLOC_FLAG, PRI_CT%, SEC_CT%` — used by Stage C R06 live check.
   - `OPT_PRIORITY_TIER, OPT_PRIORITY_RANK, ST_RANK` — sort keys for the waterfall.

4. **Initial column defaults.** Allocation result columns (`ALLOC_QTY, HOLD_QTY, SHIP_QTY, ALLOC_STATUS, SKIP_REASON, ALLOC_REMARKS`) are added with NULL/0 defaults. Stage C fills them in.

5. **Why the explosion is here, not earlier.** The MP attribute columns and CONT lookups only make sense at variant × size grain. Doing it earlier would force every Part-3 / Part-4 join to be at variant grain — slow and wasteful when 90% of those joins only care about GEN_ART.

**For U128 / 1134114962 / L_GRY:** `ARS_LISTED_OPT` row explodes to 3 alloc rows in `ARS_ALLOC_WORKING`:

| Row | VAR_ART | SZ | FNL_Q (variant) | SZ_STK | OPT_MBQ | OPT_PRIORITY_RANK |
|---|---|---|---|---|---|---|
| 1 | 001 | 0-3M | 34 | 0 | 22 | 4 |
| 2 | 002 | 3-6M | 3 | 0 | 22 | 4 |
| 3 | 003 | 6-12M | 1 | 0 | 22 | 4 |

Note `OPT_MBQ = 22` is the same on all 3 rows — the per-size split into `SZ_MBQ = 1, 6, 9` happens next in Stage B.3.

**For U128 / 1134114962 / L_GRY:** **3 rows created** in `ARS_ALLOC_WORKING`:

| VAR_ART | SZ |
|---|---|
| 1134114962001 | 0-3M |
| 1134114962002 | 3-6M |
| 1134114962003 | 6-12M |

---

## STAGE B.2 — Fill CONT (size mix recipe)

**File:** [`rule_engine_new.py:809`](backend/app/services/rule_engine_new.py#L809) `_stage_b_fill_cont` (delegates to `listing_allocator._enrich_size_cont`)

**What it does:** Look up `CONT` from `Master_CONT_SZ` with this fallback hierarchy:

1. **Store-level**: `ST_CD = WERKS` (if ANY size matches, use only this).
2. **CO-level**: `ST_CD = 'CO'` (only if no store-level rows exist).
3. **FNL_Q ratio**: `FNL_Q / SUM(FNL_Q across variants)`.
4. **Uniform**: `1 / count(variants)`.

**Key code (priority 1, store-level):**

```sql
UPDATE A SET A.CONT = ROUND(TRY_CAST(M.CONT AS FLOAT), 4)
FROM ARS_ALLOC_WORKING A
INNER JOIN Master_CONT_SZ M
    ON TRIM(M.ST_CD) = TRIM(A.WERKS)
   AND TRIM(M.MAJ_CAT) = A.MAJ_CAT
   AND TRIM(M.SZ) = TRIM(A.SZ)
```

**For U128 / 1134114962 / L_GRY:**

| VAR_ART | SZ | CONT |
|---|---|---|
| 001 | 0-3M | **0.05** (5%) |
| 002 | 3-6M | **0.25** (25%) |
| 003 | 6-12M | **0.42** (42%) |

---

## STAGE B.3 — Fill SZ_MBQ / SZ_REQ

**File:** [`rule_engine_new.py:841`](backend/app/services/rule_engine_new.py#L841) `_stage_b_fill_targets` (and the same logic via `listing_allocator._calc_sz_mbq_req`)

**What it does:**

```
SZ_MBQ    = floor-to-1(ROUND(OPT_MBQ    × CONT, 0))    ← if CONT>0 AND OPT_MBQ>0 AND ROUND=0 → 1
SZ_MBQ_WH = floor-to-1(ROUND(OPT_MBQ_WH × CONT, 0))
SZ_REQ    = MAX(0, SZ_MBQ    − SZ_STK)
SZ_REQ_WH = MAX(0, SZ_MBQ_WH − SZ_STK)
```

The **floor-to-1 patch** prevents underflow: an OPT with `OPT_MBQ = 22` and `CONT = 0.04` would round to 0 — but if CONT > 0 we force SZ_MBQ to 1.

**Key code:**

```sql
UPDATE ARS_ALLOC_WORKING SET
    SZ_MBQ = CASE
        WHEN ISNULL(CONT,0) > 0 AND ISNULL(OPT_MBQ,0) > 0
             AND ROUND(ISNULL(OPT_MBQ,0) * ISNULL(CONT,0), 0) = 0
            THEN 1
        ELSE ROUND(ISNULL(OPT_MBQ,0) * ISNULL(CONT,0), 0)
    END,
    SZ_MBQ_WH = <same logic for OPT_MBQ_WH>;

UPDATE ARS_ALLOC_WORKING SET
    SZ_REQ    = CASE WHEN SZ_MBQ-ISNULL(SZ_STK,0)>0 THEN SZ_MBQ-ISNULL(SZ_STK,0) ELSE 0 END,
    SZ_REQ_WH = CASE WHEN SZ_MBQ_WH-ISNULL(SZ_STK,0)>0 THEN SZ_MBQ_WH-ISNULL(SZ_STK,0) ELSE 0 END;
```

**For U128 / 1134114962 / L_GRY:**

| VAR_ART | OPT_MBQ × CONT | SZ_MBQ | SZ_REQ |
|---|---|---|---|
| 001 | 22 × 0.05 = 1.10 | **1** | 1 |
| 002 | 22 × 0.25 = 5.50 | **6** | 6 |
| 003 | 22 × 0.42 = 9.24 | **9** | 9 |

(`SZ_STK = 0` for all — U128's shelf is empty.)

---

## STAGE B.4 — Indexes on alloc table

**File:** [`rule_engine_new.py:883`](backend/app/services/rule_engine_new.py#L883)

Adds clustered index on `(OPT_TYPE, OPT_PRIORITY_RANK, WERKS, MAJ_CAT, GEN_ART, CLR, VAR_ART, SZ)` — the waterfall sort key.

---

## STAGE C.0 — Load tables into pandas

**File:** [`rule_engine_pandas.py:884`](backend/app/services/rule_engine_pandas.py#L884)

**What it does:** SELECT `ARS_ALLOC_WORKING` + `ARS_LISTING_WORKING` (filtered to this MAJ_CAT) into pandas DataFrames `alloc_df` and `working_df`. Coerce types.

**For U128 / 1134114962 / L_GRY:** 3 rows in `alloc_df[mask]`, 1 row in `working_df[mask]`.

---

## STAGE C.1 — Build pool dict

**File:** [`rule_engine_pandas.py`](backend/app/services/rule_engine_pandas.py) (early in `_run_majcat_waterfall`)

**What it does:** Build a Python dict from `ARS_MSA_VAR_ART.FNL_Q`:

```python
pool_dict = {
    (RDC, MAJ_CAT, GEN_ART, CLR, VAR_ART, SZ): FNL_Q
    for row in msa_var_art_rows
}
```

This dict is **shared across all stores in this MAJ_CAT** — it's the warehouse stock that everyone will compete for. It's a Python `dict` (not SQL) so the waterfall can mutate it ~1000× per second during the band loop.

**For U128 / 1134114962 / L_GRY (RDC=R1):**

```python
pool_dict[(R1, M_KIDS, 1134114962, L_GRY, 1134114962001, 0-3M)] = 34
pool_dict[(R1, M_KIDS, 1134114962, L_GRY, 1134114962002, 3-6M)] = 3
pool_dict[(R1, M_KIDS, 1134114962, L_GRY, 1134114962003, 6-12M)] = 1
```

---

## STAGE C.2 — Build MBQ budget

**File:** [`rule_engine_pandas.py:1211`](backend/app/services/rule_engine_pandas.py#L1211) `_build_mbq_budget`

**What it does:** Per `(WERKS, MAJ_CAT)`:

```
budget[(WERKS, MAJ_CAT)] = max(0, (cap_pct/100) × MJ_MBQ − MJ_STK_TTL)
```

Used as a ceiling during `_run_band` so no store overspends its category budget.

**For U128 / 1134114962 / L_GRY:** `budget[U128, M_KIDS]` set per RL/TBC caps. For TBL, `cap_pct = 0` ⇒ effectively no cap on TBL (known bug B1).

---

## STAGE C.3 — Outer loop: for ot in [RL, TBC, TBL]

The hot loop. For each OPT_TYPE in order. **U128's article runs only in the TBL pass**, but it's affected by what RL and TBC consumed first.

### Stage C.3 RL pass (i = 0)

**RL OPTs at U128 (and all other stores) drain the pool first.**

For our example: there are other RL OPTs at U128 in M_KIDS that might compete for the *same* (VAR_ART, SZ) pool keys. Whatever they take, the pool shrinks.

**Hidden steps — what happens behind the scenes (end of RL pass):**

1. **Sum up what RL just shipped.** For each `(WERKS, MAJ_CAT)`, compute `alloc_running = SUM(ALLOC_QTY across all RL rows just shipped)`. This is "how much of the store's MJ budget did RL consume".

2. **Compute the new headroom for each store.**
   ```
   headroom = (cap_pct × MJ_MBQ) − MJ_STK_TTL − alloc_running
   ```
   - `cap_pct` is the per-OPT_TYPE cap (RL=1.0 with strict PRI, TBC=1.0 or 1.3 etc., TBL=1.0).
   - `alloc_running` is what just shipped.
   - The result is "how many pieces could we still ship at this store before busting the cap".

3. **Trip the trivial-headroom gate.** If `headroom < 0.5 × ACS_D` (less than half a day's display), mark **every** PENDING TBC and TBL row at this store as `ALLOC_STATUS='SKIPPED'`, `SKIP_REASON='R09_HEADROOM_TRIVIAL'`. Reasoning: even if pool exists, this store can't accept more without overflowing its category budget, so no point letting the upcoming waves try.
   *Example:* Store HN14 has `MJ_MBQ = 600, MJ_STK_TTL = 100, ACS_D = 30`. RL just shipped 480 pieces → `alloc_running = 480`. `headroom = 600 − 100 − 480 = 20`. Threshold `0.5 × 30 = 15`. `20 ≥ 15` → still has headroom, **not** skipped. But if RL had shipped 500 → `headroom = 0 < 15` → all TBC/TBL rows at HN14 get marked SKIPPED with `R09_HEADROOM_TRIVIAL`.

4. **Audit trail.** Each skipped row gets an `ALLOC_REMARKS` line appended:
   ```
   R09_HEADROOM_TRIVIAL(after=RL, cap=1.0, alloc_running=500);
   ```
   So later you can trace exactly why a row was skipped.

5. **Same check fires again after TBC.** When TBC finishes, R09 reruns for upcoming TBL only. Cascades: a store starved after RL stays starved.

**For U128 / 1134114962 / L_GRY:** No RL OPTs at U128 for this article. After RL pass, U128 still has headroom available → no R09 skip. TBC pass proceeds; same R09 check fires after TBC; assuming TBC also didn't drain U128's MJ budget, TBL pass proceeds normally and our article allocates.

**For U128 / 1134114962 / L_GRY:** No RL OPTs share these specific (VAR_ART, SZ) pool keys → pool untouched for this article. R09 leaves the row alone (U128 still has budget headroom).

### Stage C.3 TBC pass (i = 1)

#### C.3.a — `_pre_band_check` (TBC)

**File:** [`rule_engine_pandas.py:1656`](backend/app/services/rule_engine_pandas.py#L1656)

**What it does:**
- **R06 cross-type**: skip OPT if `MJ_REQ_REM < 0.5 × ACS_D` (insufficient budget).
- **PRI_CT live check** (if enabled): recompute `PRI_CT%` against remaining demand.
- **Store-broken check**: if a store has no remaining `MJ_REQ`, skip all its rows for this OPT_TYPE.

#### C.3.b — `_rerank_opt_priority_pandas` (TBC)

**File:** [`rule_engine_pandas.py:1486`](backend/app/services/rule_engine_pandas.py#L1486)

**What it does:** R07 live size-coverage check per `(GEN_ART, CLR, VAR_ART)`:

```python
low_cov = (SIZE_RATIO < size_threshold) & (_var_fnl < min_size_count)
# Mark SKIPPED, append 'R07_SIZE_RATIO_LIVE;' to ALLOC_REMARKS
```

Then **re-rank survivors** by the same keys as Stage A.4.

**For U128 / 1134114962 / L_GRY:** Our article is TBL, not TBC → untouched. But other TBC OPTs at U128 may drain pool keys we don't care about.

#### C.3.c — TBC rounds

Other TBC OPTs go through demand-scaling → `_run_band` → revalidate. The shared `pool_dict` drains further.

### Stage C.3 TBL pass (i = 2) — where U128's article finally allocates

#### C.3.a — `_pre_band_check` (TBL)

Same as TBC. For U128 / 1134114962 / L_GRY: passes (`PRI_CT% = 100`, budget exists).

#### C.3.b — `_rerank_opt_priority_pandas` (TBL)

R07 live size-coverage per `(GEN_ART, CLR, VAR_ART)`. For our article each VAR_ART has 1 size:

| VAR_ART | total_sizes | sizes_with_pool | ratio | _var_fnl | Skip? |
|---|---|---|---|---|---|
| 001 | 1 | 1 (pool=34) | 100% | 1 | `100% < 60%`? **No** → Pass |
| 002 | 1 | 1 (pool=3, still > 0 at this moment) | 100% | 1 | Pass |
| 003 | 1 | 1 (pool=1, still > 0) | 100% | 1 | Pass |

→ All 3 rows survive. Note: R07 looks at pool **at the start of TBL pass**, before stores drain.

**For U128 / 1134114962 / L_GRY:** Passes. OPT_PRIORITY_RANK = 4 within (U128, TBL, M_KIDS) — unchanged.

#### C.3.c — TBL rounds

##### C.3.c.i — `_scale_demand_for_round` (round 1)

**File:** [`listing_allocator.py:933`](backend/app/services/listing_allocator.py#L933)

**What it does:**

```sql
SZ_MBQ    = floor-to-1(ROUND(OPT_MBQ    × :rnd × CONT, 0))
SZ_REQ    = MAX(0, SZ_MBQ    − STK_TTL − ALLOC_QTY − HOLD_QTY)
SZ_MBQ_WH = floor-to-1(ROUND(OPT_MBQ_WH × :rnd × CONT, 0))
SZ_REQ_WH = MAX(0, SZ_MBQ_WH − STK_TTL − ALLOC_QTY − HOLD_QTY)
WHERE OPT_TYPE = 'TBL' AND I_ROD >= 1 AND SKIP_FLAG = 0
```

At round 1, `ALLOC_QTY` and `HOLD_QTY` are 0, so `SZ_REQ_WH` equals the initial value.

**For U128 / 1134114962 / L_GRY (round 1):**

| VAR_ART | SZ_MBQ | SZ_REQ_WH |
|---|---|---|
| 001 | 1 | 1 |
| 002 | 6 | 6 |
| 003 | 9 | 9 |

##### C.3.c.ii — `_run_band` (the actual pool draw)

**File:** [`rule_engine_pandas.py:1395-1653`](backend/app/services/rule_engine_pandas.py#L1395-L1653)

**What it does in plain English:** For each `(WERKS, OPT_PRIORITY_RANK, ST_RANK)` group in order, take pool. **Earlier ranks drain pool first.**

**Hidden steps — what happens behind the scenes (in order):**

1. **Filter `sub`** to candidate rows for this band: `OPT_TYPE = ot`, `I_ROD ≥ round_num`, `ALLOC_STATUS IS NULL` (i.e. not already SKIPPED or ALLOCATED).
   *Example:* TBL round 1 → `sub` has every TBL row across every store that's still PENDING. Could be 50,000 rows for a big MAJ_CAT.

2. **TBL-only per-band size gate** ([line 1693-1700](backend/app/services/rule_engine_pandas.py#L1693-L1700)) — only fires when `ot == 'TBL'`. Per `(WERKS, GEN_ART, CLR, VAR_ART)`, count how many sizes still have warehouse pool and compute the ratio:
   ```python
   _too_few = (sizes_with_pool < 3) & (ratio < 0.6)
   sub = sub[~_too_few]   # silently drop — no remark, no SKIPPED status
   ```
   *Example:* For our article, each VAR_ART has only 1 size. `sizes_with_pool` = 1 or 0. ratio = 1.0 or 0.0. At pool=1: `_too_few = (1<3) AND (1.0<0.6)` = `True AND False` = **False** → not dropped. At pool=0: `_too_few = (0<3) AND (0.0<0.6)` = `True AND True` = **True** → silently dropped. But pool=0 rows already fail the next step's filter anyway.

3. **Compute `need_pool = SZ_REQ_WH`** — how many pieces this row needs (including warehouse hold for TBL).
   *Example:* VAR 001 / 0-3M → `need_pool = 1`. VAR 002 / 3-6M → `need_pool = 6`. VAR 003 / 6-12M → `need_pool = 9`.

4. **Sort the dataframe in priority order.** Stable mergesort by `POOL_KEYS + [OPT_PRIORITY_RANK, ST_RANK, WERKS]`. This puts rows that share the same warehouse pool key together, ordered by who deserves it first. Stable sort means identical keys keep their input order — reproducibility.
   *Example:* For pool key `(R1, M_KIDS, 1134114962, L_GRY, 001, 0-3M)`, rows from all ~30 listing stores sit together, ordered by `(OPT_PRIORITY_RANK, ST_RANK, WERKS)`. U128's row sits at position N within that block.

5. **Cumulative demand within pool key.**
   ```python
   sub['cum_demand'] = sub.groupby(POOL_KEYS)['need_pool'].cumsum()
   cum_prev = sub['cum_demand'] - sub['need_pool']
   ```
   `cum_prev` is "how much has already been claimed by stores ahead of me in priority". The store at position 1 has `cum_prev = 0`. Position 2 has `cum_prev = need_pool[1]`. Position N has the sum of everything before it.
   *Example:* For pool key `(…, VAR 001, 0-3M)` with `FNL_Q_REM = 34`, the first 13 stores ahead of U128 collectively need ~13 pieces. So when U128's row is processed, `cum_prev ≈ 13`.

6. **Pool draw — `take_pool = max(0, min(need_pool, FNL_Q_REM − cum_prev))`.** Take what you need, but only up to what's left after the earlier-priority stores got theirs.
   *Example:*
   - U128 / VAR 001 / 0-3M: `min(need_pool=1, FNL_Q_REM=34 − cum_prev=13) = min(1, 21) = 1` → take 1. ✅
   - U128 / VAR 002 / 3-6M: `min(need_pool=6, FNL_Q_REM=3 − cum_prev=3) = min(6, 0) = 0` → take 0. ❌
   - U128 / VAR 003 / 6-12M: `min(need_pool=9, FNL_Q_REM=1 − cum_prev=1) = min(9, 0) = 0` → take 0. ❌

7. **Budget clip (RL/TBC only — TBL has cap=0 so skipped).** For RL/TBC, if `SUM(take_pool by WERKS) > budget[WERKS]`, scale all takes for that store down by `budget / sum(take_pool)`.
   *Example:* HN14 in TBC round 1 takes `[3, 4, 2, 5]` across 4 OPTs = total 14. But `budget = 10`. Scale factor = `10/14 ≈ 0.714`. New takes: `[2, 3, 1, 4]` = total 10. (Each is rounded down.)

8. **ALLOC / HOLD split.** Only TBL has a meaningful HOLD; RL/TBC keep `HOLD = 0`.
   ```python
   SHIP  = min(take_pool, SZ_REQ)        # SZ_REQ is the no-buffer demand
   HOLD  = take_pool − SHIP              # the extra warehouse buffer
   ALLOC_QTY += SHIP
   HOLD_QTY  += HOLD
   SHIP_QTY  += SHIP
   ROUND_SHIP = SHIP   # band delta (used by revalidate)
   ROUND_HOLD = HOLD
   ```
   *Example:* U128 VAR 001 / 0-3M: `take_pool = 1, SZ_REQ = 1` → `SHIP = 1, HOLD = 0`. Wait — but the worked example earlier showed HOLD = 1! Reason: the `need_pool` actually uses `SZ_REQ_WH` (with hold buffer baked in) so `take_pool` is often larger than `SZ_REQ`. Real example: `SZ_REQ = 1, SZ_REQ_WH = 2` → `take_pool = 2, SHIP = min(2, 1) = 1, HOLD = 2 − 1 = 1`. ✅

9. **Pool deduction** — update `pool_dict` in place. Subtract `take_pool` (not just SHIP — the HOLD also drains the warehouse pool because it's reserved for this store).
   ```python
   for key, qty in groupby(take_pool):
       pool_dict[key] -= qty
   ```
   *Example:* `pool_dict[(…, VAR 001, 0-3M)] = 21 − 2 = 19`. The next store after U128 sees 19, not 21.

10. **Mark zero-take rows SKIPPED.** Rows where `take_pool = 0` get `ALLOC_STATUS='SKIPPED', SKIP_REASON='NO_POOL_MSA'`. This is the row-level "warehouse pool empty for me" marker.
    *Example:* U128 VAR 002 and VAR 003 both get this — their pool was already drained by the time U128 was processed.

11. **Append `ALLOC_REMARKS` audit trail.** One short line per band per row showing what happened — `'B[ot.r{round}.rk{rank}] sh={ship} hld={hold} pool={before}->{after};'`.
    *Example:* U128 VAR 001 / 0-3M → `' B[TBL.r1.rk4] sh=1 hld=1 pool=21->19;'`. If you ever need to debug a row, this column tells you the exact pool state when it was processed.

**For U128 / 1134114962 / L_GRY (the moment our rank=4 turn comes):**

By the time the loop reaches `(U128, OPT_PRIORITY_RANK=4, ST_RANK=...)`, the pool for our article has been touched by earlier-rank stores. Approximate state:

| Pool key | Initial | Already taken | Remaining |
|---|---|---|---|
| `(…, VAR 001, 0-3M)` | 34 | ~13 | **21** |
| `(…, VAR 002, 3-6M)` | 3 | 3 | **0** |
| `(…, VAR 003, 6-12M)` | 1 | 1 | **0** |

Now U128 takes:

| VAR_ART | need_pool | pool_avail | take_pool | SHIP | HOLD | Pool after | Status |
|---|---|---|---|---|---|---|---|
| 001 / 0-3M | 1 | 21 | **2** (1 SHIP + 1 HOLD = need_pool_WH=2) | 1 | 1 | 19 | ALLOCATED |
| 002 / 3-6M | 6 | 0 | 0 | 0 | 0 | 0 | SKIPPED `NO_POOL_MSA` |
| 003 / 6-12M | 9 | 0 | 0 | 0 | 0 | 0 | SKIPPED `NO_POOL_MSA` |

The `ALLOC_REMARKS` for VAR 001: `' B[TBL.r1.rk4] sh=1 hld=1 pool=21->19;'`.

##### C.3.c.iii — `_revalidate_after_band`

**File:** [`rule_engine_pandas.py:1791-2026`](backend/app/services/rule_engine_pandas.py#L1791-L2026)

**What it does:** After every round, recompute:
- `MSA_FNL_Q_REM` per (WERKS, GEN_ART, CLR) = SUM of remaining `FNL_Q_REM` for the row's variants.
- `PRI_CT_REM` per OPT.
- `OPT_REQ_WH` (decrement by `ALLOC_QTY + HOLD_QTY`).
- If `pri_ct_check_*` is enabled and remaining `PRI_CT% < threshold` → mark survivors SKIPPED.

**For U128 / 1134114962 / L_GRY:** `OPT_REQ_WH` drops; the OPT remains in play for round 2 (since `I_ROD = 2`).

##### C.3.c (round 2 — TBL has I_ROD = 2)

`_scale_demand_for_round` runs again with `:rnd = 2`. New `SZ_REQ_WH` after accounting for round-1 alloc:

| VAR_ART | round_demand | already shipped+held | SZ_REQ_WH (r2) |
|---|---|---|---|
| 001 | 2 × OPT_MBQ × 0.05 = 2 | 2 | **0** |
| 002 | 2 × OPT_MBQ × 0.25 = 11 | 0 | 11 |
| 003 | 2 × OPT_MBQ × 0.42 = 18 | 0 | 18 |

But the pool is empty for VAR 002/003 → both stay SKIPPED. VAR 001 has `SZ_REQ_WH = 0` → nothing to do.

→ **Round 2 ships nothing for this article.**

#### After TBL pass — R09 final check

`_check_r09_eligibility` runs after TBL. For our row: already shipped what we could; remaining headroom check passes.

---

## STAGE D.1 — Apply PAK_SZ rounding

**File:** [`rule_engine_pandas.py:383`](backend/app/services/rule_engine_pandas.py#L383) `_stage_d_apply_pak_sz_rounding`

**What it does:** Round `ALLOC_QTY` down/up to the nearest `PAK_SZ` multiple (pack-size compliance — most articles have `PAK_SZ = 1`, but some come in packs of 2, 3, etc.).

**For U128 / 1134114962 / L_GRY:** `ALLOC_QTY = 1, PAK_SZ = 1` → unchanged.

---

## STAGE D.2 — OPT_MJ_REQ gate (final cap)

**File:** [`rule_engine_new.py:1917`](backend/app/services/rule_engine_new.py#L1917) `_stage_c_apply_opt_mj_req_gate`

**What it does:** Per `(WERKS, MAJ_CAT)`, walk OPTs in priority order (RL → TBC → TBL). Maintain `req_rem = MJ_REQ`. For each OPT:

- Let `opt_ship = SUM(ALLOC_QTY) for this OPT`.
- If `req_rem < 0.5 × OPT_MBQ` → skip this OPT (zero out `ALLOC_QTY`, mark `R10_MJ_REQ_EXHAUSTED`).
- Else `req_rem -= opt_ship`.

**For U128 / 1134114962 / L_GRY:** U128 still has `req_rem > 0.5 × OPT_MBQ` when this OPT's turn comes → not skipped. `ALLOC_QTY = 1` stays.

---

## STAGE D.3 — Reflect to ARS_LISTING_WORKING

**File:** [`rule_engine_new.py:2732`](backend/app/services/rule_engine_new.py#L2732) `_stage_d_reflect`

**What it does:** Roll up alloc-row totals to listing rows:

```sql
UPDATE W SET
    W.ALLOC_QTY = ISNULL(A.alloc_qty, 0),
    W.HOLD_QTY  = ISNULL(A.hold_qty, 0),
    W.ALLOC_REMARKS = ...
FROM ARS_LISTING_WORKING W
LEFT JOIN (
    SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR,
           SUM(ALLOC_QTY) AS alloc_qty,
           SUM(HOLD_QTY)  AS hold_qty
    FROM ARS_ALLOC_WORKING GROUP BY WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR
) A ON ...
```

**For U128 / 1134114962 / L_GRY:** `ARS_LISTING_WORKING` row now shows `ALLOC_QTY = 1`, `HOLD_QTY = 1` (sums across the 3 variants — only VAR 001 contributed).

---

## STAGE C — Write back (bulk persist)

**File:** [`rule_engine_pandas.py:2041`](backend/app/services/rule_engine_pandas.py#L2041) `_write_back_alloc` / `_write_back_working`

**What it does:** Bulk MERGE the in-memory pandas results back into the SQL tables. Optional writer-queue thread serializes commits to avoid deadlocks.

**For U128 / 1134114962 / L_GRY:** SQL `ARS_ALLOC_WORKING` now has these 3 rows (real data after the run):

```
VAR 001 / 0-3M   OPT_TYPE=TBL  ALLOC_QTY=1  HOLD_QTY=1  ALLOC_STATUS=ALLOCATED
                 SHIP=1  REMARKS=' B[TBL.r1.rk4] sh=1 hld=1 pool=33->31;'
VAR 002 / 3-6M   OPT_TYPE=TBL  ALLOC_QTY=0  HOLD_QTY=0  ALLOC_STATUS=SKIPPED
                 SKIP_REASON=NO_POOL_MSA
VAR 003 / 6-12M  OPT_TYPE=TBL  ALLOC_QTY=0  HOLD_QTY=0  ALLOC_STATUS=SKIPPED
                 SKIP_REASON=NO_POOL_MSA
```

---

## PART 8.4 — Park snapshot

**File:** [`listing.py:2296-2328`](backend/app/api/v1/endpoints/listing.py#L2296-L2328) → [`parked_history.py::snapshot_session_to_parked`](backend/app/services/parked_history.py)

**What it does:** Copy `ARS_ALLOC_WORKING` and `ARS_LISTING_WORKING` into `ARS_PARKED_ALLOC` and `ARS_PARKED_LISTING`, tagged with `session_id`. **No commit to PEND_ALC or warehouse yet** — this is a snapshot for human review.

**For U128 / 1134114962 / L_GRY:** 3 alloc rows snapshotted under this session_id.

---

## PART 8.5 — OPT_STATUS reclassification

**File:** [`listing.py:2330-2392`](backend/app/api/v1/endpoints/listing.py#L2330-L2392)

**What it does:** Per (post-alloc stock = `STK_TTL + ALLOC_QTY`):

```sql
SET OPT_STATUS = CASE
  WHEN OPT_TYPE='RL'  THEN 'RL'
  WHEN OPT_TYPE='TBC' AND ALLOC_QTY>0 AND (STK+ALC) >= thr × ACS_D THEN 'RL'
  WHEN OPT_TYPE='TBC' AND ALLOC_QTY>0                              THEN 'MIX'
  WHEN OPT_TYPE='TBC'                                              THEN 'MIX'
  WHEN OPT_TYPE='TBL' AND ALLOC_QTY>0 AND (STK+ALC) >= thr × ACS_D THEN 'NL'
  WHEN OPT_TYPE='TBL' AND ALLOC_QTY>0                              THEN 'TBL'
  WHEN OPT_TYPE='TBL'                                              THEN 'TBL'
  ELSE ISNULL(OPT_TYPE, 'MIX')
END,
TBL_LISTED_DATE = CASE
  WHEN OPT_TYPE='TBL' AND ALLOC_QTY>0 AND TBL_LISTED_DATE IS NULL
       THEN GETDATE()
  ELSE TBL_LISTED_DATE
END
```

**For U128 / 1134114962 / L_GRY:**
- `OPT_TYPE = 'TBL'`, `ALLOC_QTY = 1 > 0`, `(STK + ALC) / ACS_D = (0 + 1) / 10 = 0.1 < 0.6`
- → **`OPT_STATUS` stays `'TBL'`** (didn't ship enough to graduate to NL)
- `TBL_LISTED_DATE = now()` (first ship for this article at U128)

---

## PART 8.6 — Hold-tracking DDL (schema only)

**File:** [`listing.py:2394-2494`](backend/app/api/v1/endpoints/listing.py#L2394-L2494)

**What it does:** Just ensures `ARS_NL_TBL_HOLD_TRACKING` exists with the right schema, adds RDC column / indexes if missing, and the `FROM_HOLD_QTY` column on `ARS_ALLOC_WORKING`. **No data writes** — those happen at Approve time.

**For U128 / 1134114962 / L_GRY:** No effect on the row itself.

---

## (User clicks Approve)

**File:** [`parked_history.py::approve_parked`](backend/app/services/parked_history.py) → [`pend_alc_service.py::apply_pend_alc_delta`](backend/app/services/pend_alc_service.py)

**What it does in plain English:** The user has reviewed the parked snapshot and clicked Approve. Now the system commits:

1. **Copy parked → history.** `ARS_PARKED_ALLOC` rows → `ARS_HISTORICAL_ALLOC` with an allocation number stamped.
2. **Write PEND_ALC ledger.** For each shipped row, INSERT into `ARS_PEND_ALC`:
   ```
   (WERKS, RDC, MAJ_CAT, GEN_ART, CLR, VAR_ART, SZ, ALLOC_QTY, DO_QTY=0, SESSION_ID, IS_CLOSED=0)
   ```
3. **Apply +1 delta** via `apply_pend_alc_delta`:
   - UPDATE `ARS_MSA_TOTAL.PEND_QTY` += alloc_qty, recompute `FNL_Q = max(STK − PEND − HOLD, 0)`.
   - UPDATE `ARS_MSA_VAR_ART`, `ARS_MSA_GEN_ART` same way.
   - UPDATE `ARS_GRID_MJ_VAR_ART`, `ARS_GRID_MJ_GEN_ART`.
   - UPDATE every **Active** rollup grid's `PEND_ALC` (Inactive grids skipped per the recent fix).
4. **Write HOLD tracking.** For TBL rows with `HOLD_QTY > 0`:
   - **Step A**: decrement `HOLD_REM` on prior open hold rows whose stock the current alloc just consumed (carry-over flushed into ship).
   - **Step B**: MERGE new TBL hold rows into `ARS_NL_TBL_HOLD_TRACKING`:
     ```
     (WERKS, RDC, VAR_ART, SZ, HOLD_QTY_INITIAL, HOLD_REM, IS_CLOSED=0)
     ```
   - **MSA HOLD sync**: rewrite `ARS_MSA_TOTAL.HOLD_QTY` from open hold rows, recompute `FNL_Q`.
5. **Operations log.** Write one row to `ARS_PEND_ALC_OPERATIONS` so the action is revertable.

**Key code (the +1 delta on MSA_TOTAL):**

```sql
UPDATE T SET
    T.PEND_QTY = ISNULL(T.PEND_QTY,0) + d.qty,
    T.FNL_Q = CASE
        WHEN ISNULL(T.STK_QTY,0) - (ISNULL(T.PEND_QTY,0) + d.qty) - ISNULL(T.HOLD_QTY,0) < 0
            THEN 0
        ELSE ISNULL(T.STK_QTY,0) - (ISNULL(T.PEND_QTY,0) + d.qty) - ISNULL(T.HOLD_QTY,0)
    END
FROM ARS_MSA_TOTAL T
JOIN (SELECT rdc, art, SUM(qty) AS qty FROM #delta GROUP BY rdc, art) d
  ON T.RDC = d.rdc AND T.ARTICLE_NUMBER = d.art
```

**For U128 / 1134114962 / L_GRY (on Approve):**

| Action | Effect |
|---|---|
| INSERT ARS_PEND_ALC | +1 row: `(U128, R1, M_KIDS, 1134114962, L_GRY, 001, 0-3M, 1)` |
| MSA_TOTAL update | `PEND_QTY += 1`, `FNL_Q -= 1` for `(R1, 1134114962001)` |
| MSA_VAR_ART, MSA_GEN_ART | Same rollup |
| GRID_MJ_VAR_ART update | `PEND_ALC += 1` for `(U128, M_KIDS, 1134114962, L_GRY, 001)` |
| GRID_MJ_GEN_ART update | `PEND_ALC += 1` for `(U128, M_KIDS, 1134114962, L_GRY)` |
| Rollup grids | `PEND_ALC` updated for every Active grid that has U128 / M_KIDS in scope |
| HOLD tracking | +1 row: `(U128, R1, 1134114962001, 0-3M, HOLD_QTY_INITIAL=1, HOLD_REM=1)` |
| ARS_PEND_ALC_OPERATIONS | +1 audit row with `OP_TYPE='MANUAL'` (or whatever triggered the run) |

---

## Final state summary table

What `ARS_ALLOC_WORKING` looks like after the full pipeline for our example:

| VAR_ART | SZ | OPT_TYPE | ALLOC_STATUS | SKIP_REASON | OPT_MBQ | CONT | SZ_MBQ | SZ_REQ | SZ_STK | FNL_Q | FNL_Q_REM | ALLOC_QTY | HOLD_QTY | SHIP_QTY | OPT_PRIORITY_RANK | ALLOC_REMARKS |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 001 | 0-3M | TBL | ALLOCATED | (empty) | 22 | 0.05 | 1 | 1 | 0 | 34 | 21 | 1 | 1 | 1 | 4 | `' B[TBL.r1.rk4] sh=1 hld=1 pool=33->31;'` |
| 002 | 3-6M | TBL | SKIPPED | NO_POOL_MSA | 22 | 0.25 | 6 | 6 | 0 | 3 | 0 | 0 | 0 | 0 | 4 | (empty) |
| 003 | 6-12M | TBL | SKIPPED | NO_POOL_MSA | 22 | 0.42 | 9 | 9 | 0 | 1 | 0 | 0 | 0 | 0 | 4 | (empty) |

What `ARS_LISTING_WORKING` shows for this `(GEN_ART, CLR)`:

| Field | Value |
|---|---|
| OPT_TYPE | TBL |
| OPT_STATUS | TBL (didn't graduate to NL — only 1 / 10 ACS_D shipped) |
| TBL_LISTED_DATE | today |
| ALLOC_QTY | 1 (sum across variants) |
| HOLD_QTY | 1 |
| ALLOC_REASON | PARTIAL |
| LISTED_FLAG | 1 |
| OPT_PRIORITY_RANK | 4 |

What `ARS_PEND_ALC` (after Approve) shows:

| Field | Value |
|---|---|
| WERKS | U128 |
| RDC | R1 |
| MAJ_CAT | M_KIDS |
| GEN_ART_NUMBER | 1134114962 |
| CLR | L_GRY |
| VAR_ART | 1134114962001 |
| SZ | 0-3M |
| ALLOC_QTY | 1 |
| DO_QTY | 0 |
| IS_CLOSED | 0 |
| SESSION_ID | (this session's ID) |

What `ARS_NL_TBL_HOLD_TRACKING` (after Approve) shows:

| Field | Value |
|---|---|
| WERKS | U128 |
| RDC | R1 |
| VAR_ART | 1134114962001 |
| SZ | 0-3M |
| OPT_STATUS | TBL |
| LISTED_DATE | today |
| HOLD_QTY_INITIAL | 1 |
| HOLD_REM | 1 |
| IS_CLOSED | 0 |

---

## Why this article shipped 1 of 3 sizes (the headline answer)

In simple words:

1. **OPT_TYPE = TBL** because U128 had no stock but the warehouse had supply (Part 3.6, branch 5).
2. **The 60% rule didn't skip it** because each VAR_ART has only 1 size and all 3 variants had warehouse stock at decision time (`VAR_FNL_COUNT/VAR_COUNT = 3/3 = 100%`). The rule operates per VAR_ART, not per the whole article.
3. **By the time U128's turn came in the TBL waterfall**, the small-warehouse variants (002 with 3 units, 003 with 1 unit) had been fully consumed by earlier-rank stores. Only VAR 001 (with 34 units) still had pool left.
4. **VAR 001 shipped 1 piece + held 1 piece** at warehouse for U128's next top-up.
5. **VAR 002 and 003 got `SKIP_REASON='NO_POOL_MSA'`** — not because of the 60% rule, but because the pool was empty.

If you want a "skip the whole article when fewer than 60% of variants ship" rule, that would need to be a *new* gate at the (WERKS, GEN_ART, CLR) level — the current R07 only looks at one VAR_ART at a time.

---

## Part 8 deep dive — concurrency, orchestration, flowcharts

Part 8 in [`listing.py:2212-2294`](backend/app/api/v1/endpoints/listing.py#L2212-L2294) is just a **dispatcher** — it calls `run_listing_and_allocation_pandas`. The real machinery is inside `rule_engine_pandas.py`. This section is the operator's reference for what actually runs.

### 8.0 — Dispatcher decision

```python
mode = (req.allocation_mode or "pandas").lower()       # default = pandas
n_workers = max(2, min(8, int(req.parallel_workers or 4)))
```

| Path | Trigger | Behaviour |
|---|---|---|
| **pandas** | Default | `run_listing_and_allocation_pandas(...)` — process-fanned by MAJ_CAT |
| **sequential** | `allocation_mode='sequential'` | `run_listing_and_allocation(...)` — single thread, all SQL |

The pandas engine is the production default. The rest of this section covers pandas.

### 8.1 — Gate-state audit log

First thing logged: which gates are ON for this run. Helps diagnose UI vs server mismatches.

```
[engine] batch=<id> | RL: PRI>=100 strict (MJ-cap 100%) |
                      TBC: MBQ-cap 130% | MJ_REQ growth=110%
```

### 8.2 — Stage A + B prep (SQL, single thread)

Runs once per batch. **Skipped if `only_majcats` is set** (retry path).

```python
rne._stage_a_add_columns(conn, working_table)
rne._stage_a_apply_rules(conn, working_table, ...)   # R01-R09
rne._stage_a_assign_tier(conn, working_table)        # focus tier 1/2/3
rne._stage_a_assign_rank(conn, working_table)        # OPT_PRIORITY_RANK
rne._stage_a_materialize_listed(conn, working_table, listed_table)

rne._stage_b_explode(conn, listed_table, alloc_table, msa_var_table, ...)
rne._stage_b_fill_cont(conn, alloc_table, cont_table)
rne._stage_b_fill_targets(conn, alloc_table, var_grid_table)
rne._stage_b_indexes(conn, alloc_table)
```

Discovers active primary grids: `grids = rne._discover_primary_grids(conn)` — drives per-grid REQ_REM tracking during the waterfall.

### 8.3 — Queue seeding

```python
total = seed_queue(conn, batch_id, alloc_table, "pandas", only_majcats=None)
```

Inserts one row per distinct MAJ_CAT into `ARS_ALLOC_MAJCAT_QUEUE`:

| Column | Purpose |
|---|---|
| `batch_id` | Identifies this run |
| `maj_cat` | The slice key |
| `status` | `PENDING → IN_PROGRESS → DONE` / `FAILED` |
| `attempts` | Retry counter (3 max) |
| `error` | Last error message |
| `progress` | Live counter for UI |

Powers the live progress dashboard during the run.

### 8.4 — Bulk load into pandas (one query)

```python
alloc_df, working_df, working_cols = _load_tables(
    engine, alloc_table, working_table, grids, only_majcats
)
```

**One SELECT** pulls the entire universe into RAM. Typically 200k–500k rows in `alloc_df`, 30k–80k in `working_df`. Loads happen once, on the parent process. Then sliced per-MAJ_CAT (no copy):

```python
alloc_groups   = {mc: g for mc, g in alloc_df.groupby('MAJ_CAT', sort=False)}
working_groups = {mc: g for mc, g in working_df.groupby('MAJ_CAT', sort=False)}
```

### 8.5 — Process-pool decision

```python
use_pool = (len(alloc_groups) >= PROCESS_POOL_MIN_MAJCATS and n_workers > 1)
defer_writes_flag = bool(use_writer_queue and use_pool)
```

| Scenario | Path |
|---|---|
| ≥ 3 MAJ_CATs AND `n_workers > 1` | **ProcessPoolExecutor** — true parallel subprocesses |
| Fewer than 3 MAJ_CATs OR `n_workers ≤ 1` | **Inline** loop — subprocess spawn cost would dominate |

Threading isn't used because pandas/numpy hot loops hold the GIL — 8 threads = 1 effective worker. Subprocesses each have their own GIL → real parallelism.

### 8.6 — Writer thread (optional, opt-in via `USE_WRITER_QUEUE`)

When enabled AND pooling:

```python
writer_queue = queue.Queue(maxsize=max(4, n_workers * 2))
writer_thread = threading.Thread(target=_writer_thread_fn, ...)
writer_thread.start()
```

The writer thread is the **single sequential DB writer** — eliminates writer-writer contention. Workers return computed DataFrames; the writer drains the queue and does all `UPDATE`s in order. Without it, each subprocess writes directly when it finishes — faster on the happy path, more deadlock-prone under load.

### 8.7 — Per-MAJ_CAT worker (`_pandas_run_one_majcat`)

Each subprocess receives a 15-tuple `(maj_cat, alloc_slice, working_slice, grids, batch_id, alloc_table, working_table, pri_ct_check_rl, pri_ct_check_tbc, rl_mbq_cap_pct, tbc_mbq_cap_pct, size_threshold, min_size_count, active_types, defer_writes_flag)`.

Inside the worker:
1. Mark queue row `IN_PROGRESS`
2. Run `_run_majcat_waterfall(...)`
3. Either write back directly OR return DataFrames for the writer thread
4. Mark queue row `DONE` (or `FAILED` with retry)

### 8.8 — Finalise (after all workers done)

```python
_stage_d_apply_pak_sz_rounding(conn, alloc_table)
rne._stage_c_apply_opt_mj_req_gate(conn, working_table, alloc_table, ...)
rne._stage_d_reflect(conn, working_table, alloc_table)
```

Plus:
- **Sec-cap pre-gate** (if `apply_sec_cap_in_normal=True`) — apply secondary-grid MBQ caps
- **Drop ARS_LISTED_OPT** (Stage B's intermediate)
- **Per-row reason classification** before `_stage_d_reflect`
- **Drain writer queue** with `_WRITER_SENTINEL`, 10-min safety join

Final telemetry:

```
[C-pd] live write-back done — total wb time across workers Xs
[C-pd] deadlock retries: caught=N succeeded=N exhausted=N
[C-pd] batch=<id> COMPLETE — total=N done=N in_progress=0 failed=N pct=100%
```

---

### Flowchart 1 — `_run_majcat_waterfall` (the per-MAJ_CAT loop)

```
┌──────────────────────────────────────────────────────────────┐
│  Inputs: alloc_df, working_df (pre-sliced to one MAJ_CAT)    │
└──────────────────────────┬───────────────────────────────────┘
                           │
                           ▼
            ┌──────────────────────────────────┐
            │ Build pool_dict from alloc_df    │
            │   key:  POOL_KEYS                │
            │   value: max(FNL_Q)              │
            └──────────────────┬───────────────┘
                               │
            ┌──────────────────▼───────────────┐
            │ Compute eff_rl_cap, eff_tbc_cap  │
            │ (PRI strict pins to 100; else    │
            │  use UI's rl_mbq_cap_pct etc.)   │
            │ TBL: eff_cap = 0 (no MJ cap)     │
            └──────────────────┬───────────────┘
                               │
            ┌──────────────────▼───────────────┐
            │ snapshot FNL_Q_REM (initial)     │
            └──────────────────┬───────────────┘
                               │
            ┌──────────────────▼───────────────┐
            │ for i, ot in enumerate(          │  ◄── OUTER LOOP
            │   [RL, TBC, TBL]):               │
            └──────────────────┬───────────────┘
                               │
                ┌──────────────▼────────────┐
                │ cap_pct_for_ot =          │
                │   eff_rl_cap   if RL      │
                │   eff_tbc_cap  if TBC     │
                │   0.0          if TBL     │
                └──────────────┬────────────┘
                               │
                ┌──────────────▼────────────────┐
                │  if i > 0:  (TBC or TBL only) │
                │  _rerank_opt_priority_pandas  │  ◄── R07 LIVE GATE
                │    • compute SIZE_RATIO       │
                │      from live pool_dict      │
                │    • mark SKIPPED if          │
                │      (ratio<thr AND fnl<min)  │
                │    • re-rank survivors        │
                └──────────────┬────────────────┘
                               │
                ┌──────────────▼────────────────┐
                │  _pre_band_check              │  ◄── R06 + STORE-BROKEN
                │   • PRI_CT_REM gate           │
                │   • MJ_REQ_REM store-broken   │
                │   • Propagate skips           │
                └──────────────┬────────────────┘
                               │
                ┌──────────────▼─────────────────┐
                │  for r in 1..max_I_ROD:        │  ◄── ROUND LOOP
                └──────────────┬─────────────────┘
                               │
                  ┌────────────▼────────────────┐
                  │ Reset ROUND_SHIP/ROUND_HOLD │
                  │ to 0 for this ot's rows     │
                  └────────────┬────────────────┘
                               │
                  ┌────────────▼────────────────┐
                  │ elig_mask = ot AND I_ROD≥r  │
                  └────────────┬────────────────┘
                               │
                  ┌────────────▼────────────────┐
                  │ snapshot FNL_Q_REM          │
                  │ for elig_mask rows          │
                  └────────────┬────────────────┘
                               │
                  ┌────────────▼────────────────┐
                  │ mbq_budget =                │
                  │   _live_mbq_budget(         │
                  │     working_df,             │
                  │     cap_pct_for_ot)         │
                  │  • rebuilt from live        │
                  │    MJ_REQ_REM every band    │
                  └────────────┬────────────────┘
                               │
                  ┌────────────▼─────────────────┐
                  │  _run_band(ot, r)            │  ◄── THE BAND
                  │  (see Flowchart 2 below)     │
                  └────────────┬─────────────────┘
                               │
                  ┌────────────▼─────────────────┐
                  │  _revalidate_after_band      │  ◄── POST-BAND
                  │  • Recompute MSA_FNL_Q_REM   │
                  │  • Decrement MJ_REQ_REM      │
                  │  • Decrement per-grid REM    │
                  │  • PRI_CT live check         │
                  └────────────┬─────────────────┘
                               │
                  ┌────────────▼────────────────┐
                  │ Log ship/hold totals        │
                  │ for this OPT × round        │
                  └────────────┬────────────────┘
                               │
                       (loop back to next r)
                               │
                  ┌────────────▼────────────────┐
                  │ (after all rounds for ot)   │
                  │ R09 headroom recheck for    │
                  │ upcoming opt_types          │
                  └────────────┬────────────────┘
                               │
                       (loop back to next ot)
                               │
                               ▼
                        return mutated
                        alloc_df, working_df
```

### Flowchart 2 — `_run_band` (the pool draw)

```
┌────────────────────────────────────────────────────────────┐
│ Inputs: alloc_df, pool_dict, ot, round_num, mbq_budget,    │
│         hold_dict, size_threshold, min_size_count          │
└────────────────────┬───────────────────────────────────────┘
                     │
        ┌────────────▼───────────────────────────┐
        │ Filter: sub = alloc_df rows where      │
        │   OPT_TYPE = ot                        │
        │   I_ROD >= round_num                   │
        │   ALLOC_STATUS IS NULL (not skipped)   │
        └────────────┬───────────────────────────┘
                     │
        ┌────────────▼───────────────────────────┐
        │ Map FNL_Q_REM from pool_dict           │
        │  sub['FNL_Q_REM'] = key.map(pool_dict) │
        └────────────┬───────────────────────────┘
                     │
        ┌────────────▼───────────────────────────┐
        │ IF ot == 'TBL':                        │
        │   per-band size-completeness gate      │
        │   _too_few = (avail<3 AND ratio<0.6)   │
        │   sub = sub[~_too_few]    (drop)       │
        │   (per WERKS, GEN_ART, CLR, VAR_ART)   │
        └────────────┬───────────────────────────┘
                     │
        ┌────────────▼───────────────────────────┐
        │ Filter sub[FNL_Q_REM > 0]              │
        │  (drop sizes with no warehouse stock)  │
        └────────────┬───────────────────────────┘
                     │
        ┌────────────▼───────────────────────────┐
        │ Build need_pool per row:               │
        │   need_pool = SZ_REQ_WH                │
        │   need_ship = SZ_REQ                   │
        └────────────┬───────────────────────────┘
                     │
        ┌────────────▼───────────────────────────┐
        │ SORT (stable, mergesort) by:           │
        │   POOL_KEYS                            │
        │   OPT_PRIORITY_RANK ASC                │
        │   ST_RANK ASC                          │
        │   WERKS ASC                            │
        └────────────┬───────────────────────────┘
                     │
        ┌────────────▼───────────────────────────┐
        │ Cumulative demand within pool key:     │
        │   cum_demand = need_pool.cumsum()      │
        │   cum_prev   = cum_demand - need_pool  │
        └────────────┬───────────────────────────┘
                     │
        ┌────────────▼───────────────────────────┐
        │ Pool draw:                             │
        │   remaining = max(FNL_Q_REM - cum_prev,│
        │                   0)                   │
        │   take_pool = min(remaining, need_pool)│
        └────────────┬───────────────────────────┘
                     │
        ┌────────────▼───────────────────────────┐
        │ Budget clip (per WERKS):               │
        │   if budget < sum(take_pool by werks): │
        │     scale = budget / sum(take_pool)    │
        │     take_pool *= scale                 │
        │   (RL/TBC only; cap=0 for TBL)         │
        └────────────┬───────────────────────────┘
                     │
        ┌────────────▼───────────────────────────┐
        │ SHIP / HOLD split:                     │
        │   SHIP = min(take_pool, need_ship)     │
        │   HOLD = take_pool - SHIP              │
        │   ALLOC_QTY += SHIP                    │
        │   HOLD_QTY  += HOLD                    │
        │   SHIP_QTY  += SHIP                    │
        │   ROUND_SHIP = SHIP (band delta)       │
        │   ROUND_HOLD = HOLD                    │
        └────────────┬───────────────────────────┘
                     │
        ┌────────────▼───────────────────────────┐
        │ Pool deduction (in-place dict update): │
        │   for key, qty in groupby(take_pool):  │
        │     pool_dict[key] -= qty              │
        └────────────┬───────────────────────────┘
                     │
        ┌────────────▼───────────────────────────┐
        │ Mark zero-take rows:                   │
        │   ALLOC_STATUS = 'SKIPPED'             │
        │   SKIP_REASON  = 'NO_POOL_MSA'         │
        │   (only rows whose pool was empty)     │
        └────────────┬───────────────────────────┘
                     │
        ┌────────────▼───────────────────────────┐
        │ Append ALLOC_REMARKS audit line:       │
        │   ' B[{ot}.r{r}.rk{rank}] sh={sh}      │
        │    hld={hld} pool={pre}->{post};'      │
        └────────────┬───────────────────────────┘
                     │
                     ▼
              alloc_df mutated
```

### Flowchart 3 — `_pre_band_check` (eligibility gates)

```
┌──────────────────────────────────────────────────────┐
│ Inputs: alloc_df, working_df, ot, pri_check_rl/tbc   │
└─────────────────┬────────────────────────────────────┘
                  │
       ┌──────────▼─────────────────────────────┐
       │ Gate 1: PRI_CT_REM live check          │
       │  enforced when:                        │
       │   ot='TBL' always                      │
       │   ot='RL'  if pri_ct_check_rl=True     │
       │   ot='TBC' if pri_ct_check_tbc=True    │
       │                                        │
       │  If PRI_CT_REM < 100 AND ALLOC_FLAG≠1: │
       │    mark working_df row SKIPPED         │
       │    reason 'R06_PRI_REM'                │
       └──────────┬─────────────────────────────┘
                  │
       ┌──────────▼─────────────────────────────┐
       │ Gate 2: MJ_REQ_REM store-broken check  │
       │  if MJ_REQ_REM[WERKS] < 0.5×ACS_D:     │
       │    mark all this store's rows SKIPPED  │
       │    reason 'R09_STORE_BROKEN'           │
       └──────────┬─────────────────────────────┘
                  │
       ┌──────────▼─────────────────────────────┐
       │ Propagate skips to alloc_df:           │
       │  WHERE (WERKS, GEN_ART, CLR) is in     │
       │  working_df.SKIPPED                    │
       │   → mark alloc_df row SKIPPED          │
       └──────────┬─────────────────────────────┘
                  │
                  ▼
            mutated alloc_df + working_df
```

### Flowchart 4 — `_revalidate_after_band` (post-band counters)

```
┌──────────────────────────────────────────────────────┐
│ Inputs: alloc_df, working_df, grids, ot, round_num   │
└─────────────────┬────────────────────────────────────┘
                  │
       ┌──────────▼─────────────────────────────┐
       │ 1. Decrement MJ_REQ_REM:               │
       │    for each (WERKS, MAJ_CAT):          │
       │      MJ_REQ_REM -= ROUND_SHIP +        │
       │                    ROUND_HOLD          │
       └──────────┬─────────────────────────────┘
                  │
       ┌──────────▼─────────────────────────────┐
       │ 2. Decrement per-grid REQ_REM:         │
       │    for each grid in grids:             │
       │      gh_col + extras → group key       │
       │      {prefix}_REQ_REM -= ship_in_grid  │
       └──────────┬─────────────────────────────┘
                  │
       ┌──────────▼─────────────────────────────┐
       │ 3. Recompute MSA_FNL_Q_REM:            │
       │    sum(pool_dict[key]) per             │
       │    (WERKS, GEN_ART, CLR, VAR_ART)      │
       └──────────┬─────────────────────────────┘
                  │
       ┌──────────▼─────────────────────────────┐
       │ 4. Recompute PRI_CT_REM:               │
       │    sum(need that's still pool-able)    │
       │    / sum(initial need)                 │
       └──────────┬─────────────────────────────┘
                  │
       ┌──────────▼─────────────────────────────┐
       │ 5. Decrement OPT_REQ_WH:               │
       │    OPT_REQ_WH -= ROUND_SHIP +          │
       │                  ROUND_HOLD            │
       │    (per OPT)                           │
       └──────────┬─────────────────────────────┘
                  │
       ┌──────────▼─────────────────────────────┐
       │ 6. Optional PRI_CT_REM live skip:      │
       │    if pri_ct_check_{ot} AND            │
       │       PRI_CT_REM < threshold:          │
       │      mark survivors SKIPPED            │
       │      reason 'R06_PRI_REM_LIVE'         │
       └──────────┬─────────────────────────────┘
                  │
                  ▼
            mutated alloc_df + working_df
```

---

### Master flowchart — click to stock arrived

```
   ┌────────────────────────────────────────────────────────────────┐
   │                       USER CLICKS "GENERATE"                    │
   │                  POST /listing/generate (api/v1)                │
   └─────────────────────────────┬──────────────────────────────────┘
                                 │
                                 ▼
   ┌────────────────────────────────────────────────────────────────┐
   │  generate_listing(req) in listing.py                            │
   └─────────────────────────────┬──────────────────────────────────┘
                                 │
        ┌────────────────────────┴────────────────────────┐
        │            PHASE A — BUILD LISTING               │
        │                                                  │
        │ Part 1   Grid → ARS_LISTING (GEN_ART grain)      │
        │ Part 2   MSA-only → IS_NEW=1                     │
        │ Part 2.5 Indexes                                 │
        │ Part 3.5   ACS_D, ALC_D                          │
        │ Part 3.5a  LISTING, I_ROD, CLR, FOCUS            │
        │ Part 3.5b  AUTO_GEN_ART_SALE                     │
        │ Part 3.5c  AGE                                   │
        │ Part 3.54  RL_HOLD_QTY (from prior runs)         │
        │ Part 3.55  MSA_FNL_Q + VAR_COUNT + VAR_FNL_COUNT │
        └────────────────────────┬─────────────────────────┘
                                 │
                                 ▼
        ┌─────────────────────────────────────────────────┐
        │           PHASE B — CLASSIFY & ENRICH            │
        │                                                  │
        │ Part 3.6   OPT_TYPE (MIX / RL / TBC / TBL)       │
        │ Part 3.7   MIX aggregation                       │
        │ Part 4 pre-resolve  MP cols onto listing         │
        │ Part 4a   Grid column joins                      │
        │ Part 4b   PER_OPT_SALE                           │
        │ Part 4c   OPT_MBQ, OPT_REQ, OPT_MBQ_WH           │
        │ Part 4d   ART_EXCESS                             │
        │ Part 4e   Per-grid REQ with excess deduction     │
        │ Part 5    Final indexes                          │
        │ Part 6    Store ranking → ST_RANK                │
        └────────────────────────┬─────────────────────────┘
                                 │
                                 ▼
        ┌─────────────────────────────────────────────────┐
        │       PHASE C — WORKING TABLE + GATES            │
        │                                                  │
        │ Part 7  ARS_LISTING_WORKING (filter + flag)      │
        │   • MSA_FNL_Q>0 OR HOLD>0                        │
        │   • OPT_REQ_WH>=1                                │
        │   • TBL: VAR_FNL/VAR_COUNT >= threshold          │
        │     (or VAR_COUNT=0 bypass)                      │
        │   • LISTING=1                                    │
        │   • MJ_DISP_Q>0  ◄── new 2026-05-25              │
        │   • MJ_MBQ_REV / MJ_REQ_REV (growth)             │
        │   • ALLOC_FLAG = (PRI_CT% >= 100)                │
        └────────────────────────┬─────────────────────────┘
                                 │
                                 ▼
   ┌────────────────────────────────────────────────────────────────┐
   │                  PHASE D — PART 8 ALLOCATION                    │
   │                  run_listing_and_allocation_pandas              │
   └─────────────────────────────┬──────────────────────────────────┘
                                 │
        ┌────────────────────────▼─────────────────────────┐
        │   8.1 Audit log gate state                        │
        │   8.2 Stage A + B (single thread, SQL):           │
        │       • A: add cols, rules R01-R09, tier, rank,   │
        │            materialize ARS_LISTED_OPT             │
        │       • B: explode VAR_ART×SZ, CONT, SZ_MBQ       │
        │   8.3 Seed ARS_ALLOC_MAJCAT_QUEUE                 │
        │   8.4 Load alloc_df + working_df into pandas      │
        │   8.5 Decide use_pool (subprocess vs inline)      │
        │   8.6 Optional writer thread                      │
        └────────────────────────┬─────────────────────────┘
                                 │
                ┌────────────────▼─────────────────┐
                │ for each MAJ_CAT in subprocess:  │
                │   _pandas_run_one_majcat         │
                │     • mark IN_PROGRESS           │
                │     • _run_majcat_waterfall      │  ◄── see Flowchart 1
                │       (RL → TBC → TBL × rounds)  │
                │     • write back or defer        │
                │     • mark DONE / FAILED         │
                └────────────────┬─────────────────┘
                                 │
                                 ▼
        ┌─────────────────────────────────────────────────┐
        │   8.7 Drain writer queue (if enabled)            │
        │   8.8 Stage D finalise (SQL):                    │
        │       • _stage_d_apply_pak_sz_rounding           │
        │       • _stage_c_apply_opt_mj_req_gate           │
        │       • _stage_d_reflect (roll up to listing)    │
        │       • sec-cap pre-gate (optional)              │
        │       • drop ARS_LISTED_OPT                      │
        │   8.9 Telemetry: deadlock retries, failed list   │
        └────────────────────────┬─────────────────────────┘
                                 │
                                 ▼
        ┌─────────────────────────────────────────────────┐
        │           PHASE E — POST-ALLOC                   │
        │                                                  │
        │ Part 8.4  Park to ARS_PARKED_*                   │
        │ Part 8.5  OPT_STATUS (TBL→NL, TBC→RL/MIX)        │
        │ Part 8.6  Hold-tracking DDL                      │
        └────────────────────────┬─────────────────────────┘
                                 │
                                 ▼
        ┌─────────────────────────────────────────────────┐
        │              Run returns to UI                   │
        │   User sees parked snapshot for review           │
        └────────────────────────┬─────────────────────────┘
                                 │
                  (user clicks Approve)
                                 │
                                 ▼
        ┌─────────────────────────────────────────────────┐
        │            PHASE F — APPROVE                     │
        │            parked_history.approve_parked         │
        │                                                  │
        │  1. Copy parked → historical                     │
        │  2. INSERT ARS_PEND_ALC rows                     │
        │  3. apply_pend_alc_delta(+1):                    │
        │     • MSA_TOTAL.PEND_QTY += alloc                │
        │     • MSA_VAR_ART, MSA_GEN_ART updated           │
        │     • GRID_MJ_VAR_ART, GRID_MJ_GEN_ART updated   │
        │     • All Active rollup grids' PEND_ALC updated  │
        │  4. HOLD-tracking writes (Step A/B + MSA sync)   │
        │  5. ARS_PEND_ALC_OPERATIONS audit row            │
        └────────────────────────┬─────────────────────────┘
                                 │
                                 ▼
        ┌─────────────────────────────────────────────────┐
        │   Warehouse ledger now reflects this allocation │
        │   (BDC generation / DO upload happens later)    │
        └─────────────────────────────────────────────────┘
```

### Concurrency model — who runs where, in what thread/process

```
                       MAIN PROCESS (uvicorn worker)
   ┌─────────────────────────────────────────────────────────────┐
   │  generate_listing endpoint thread                           │
   │   ├── Parts 1-7         (single thread, SQL)                │
   │   ├── Part 8.2: Stage A+B prep (single thread, SQL)         │
   │   ├── Part 8.4: load alloc_df + working_df                  │
   │   └── Part 8.8: Stage D finalise (single thread, SQL)       │
   │                                                              │
   │       optional writer thread (USE_WRITER_QUEUE=True)         │
   │          │                                                   │
   │          ▼ pulls from queue, does all SQL UPDATEs           │
   └─────────────────────────────┬───────────────────────────────┘
                                 │
                ┌────────────────┼────────────────┐
                │                │                │
                ▼                ▼                ▼
        ┌───────────┐    ┌───────────┐    ┌───────────┐
        │SUBPROCESS │    │SUBPROCESS │    │SUBPROCESS │   ProcessPoolExecutor
        │  worker 1 │    │  worker 2 │    │  worker N │   (default 4)
        │           │    │           │    │           │
        │ MAJ_CAT_A │    │ MAJ_CAT_B │    │ MAJ_CAT_C │
        │ MAJ_CAT_D │    │ MAJ_CAT_E │    │ MAJ_CAT_F │   each takes from queue
        │ ...       │    │ ...       │    │ ...       │
        │           │    │           │    │           │
        │ in-mem    │    │ in-mem    │    │ in-mem    │
        │ pandas    │    │ pandas    │    │ pandas    │   real parallelism
        │ waterfall │    │ waterfall │    │ waterfall │   (own GIL each)
        └─────┬─────┘    └─────┬─────┘    └─────┬─────┘
              │                │                │
              └────────────────┼────────────────┘
                               │
                               ▼
                   one queue (ARS_ALLOC_MAJCAT_QUEUE)
                   tracks PENDING / IN_PROGRESS / DONE
                   per MAJ_CAT — drives the UI progress bar
```

### Hidden safety + cleanup details

| Mechanism | Where | What it does |
|---|---|---|
| **Retry on deadlock** | `retry_on_deadlock` decorator in [db_helpers.py](backend/app/utils/db_helpers.py) | Each write attempt retries up to N times on SQL error 1205; telemetry collected (caught / succeeded / exhausted) and logged in one summary line at end of Stage C |
| **Cancellation hook** | [alloc_cancellation.py](backend/app/services/alloc_cancellation.py) (imported as `ac`) | User can cancel from UI — workers check `ac.is_cancelled(batch_id)` between bands and exit cleanly |
| **Per-MAJ_CAT progress** | `mark_in_progress / mark_done / mark_failed` in [alloc_queue.py](backend/app/services/alloc_queue.py) | Live progress tracking; UI polls `get_progress(conn, batch_id)` |
| **Subprocess spawn on Windows** | `ProcessPoolExecutor(mp_context='spawn')` | Each subprocess re-imports modules (slow startup, ~1-2s) but escapes GIL. `PROCESS_POOL_MIN_MAJCATS=3` means sub-3 inputs go inline to avoid the spawn tax |
| **Stable mergesort** | Every `sort_values(kind='mergesort')` in `_run_band` | Reproducibility — two runs with same inputs produce identical OPT_PRIORITY_RANK / ALLOC_QTY (no race) |
| **ALLOC_REMARKS audit trail** | Appended by `_run_band` per band | Every band leaves a breadcrumb: `' B[TBL.r1.rk4] sh=1 hld=1 pool=21->19;'` — read this column to reconstruct exactly what happened to a row |
| **Floor-to-1 on SZ_MBQ** | [`listing_allocator.py:301`](backend/app/services/listing_allocator.py#L301) (and the round-N variant) | Prevents underflow on small contribution % × small OPT_MBQ |
| **R09 inter-OPT_TYPE check** | `_check_r09_eligibility` in [`rule_engine_new.py:391`](backend/app/services/rule_engine_new.py#L391) | Runs between OPT_TYPE waves — skips upcoming OPTs whose headroom is now < 0.5×ACS_D |
| **Inactive-grid skip in PEND_ALC delta** | `_discover_grid_rollup_tables` in [`pend_alc_service.py:2204`](backend/app/services/pend_alc_service.py#L2204) | Grids marked Inactive in ARS_GRID_BUILDER are not touched during manual add / revert / approve |

---

## Complete variables + conditions reference

This is the **single-source-of-truth lookup** for every column and rule used across the listing → allocation pipeline. Use it to answer *"where does this column come from?"* and *"what's the exact condition that fires this gate?"*.

### A. Identity columns (the row key everywhere)

| Variable | Type | Grain | Source / set by | Used by |
|---|---|---|---|---|
| `WERKS` | NVARCHAR(50) | row | Part 1 (grid join) / Part 2 (store CROSS JOIN) | Every join, all filters, ranking |
| `RDC` | NVARCHAR(50) | row | Part 1 (store master) / Part 2 (store master) | MSA join, pool key |
| `MAJ_CAT` | NVARCHAR(100) | row | Part 1 / Part 2 (from grid/MSA) | Every per-category aggregation |
| `GEN_ART_NUMBER` | BIGINT | row | Part 1 / Part 2 | OPT identity |
| `CLR` | NVARCHAR(100) | row | Part 1 / Part 2 | OPT identity, hard `I_ROD` override |
| `GEN_ART_DESC` | NVARCHAR(500) | row | Part 3.6 from `vw_master_product` | UI display only |
| `IS_NEW` | BIT | row | Part 1 sets 0 / Part 2 sets 1 | TBL classification |
| `VAR_ART` | BIGINT | alloc only | Stage B.1 explode from `ARS_MSA_VAR_ART` | Pool key, size aggregation |
| `SZ` | NVARCHAR(50) | alloc only | Stage B.1 explode | Pool key, CONT lookup |

### B. Stock & demand columns

| Variable | Type | Grain | Source / set by | Formula / meaning |
|---|---|---|---|---|
| `STK_TTL` | FLOAT | listing | Part 1 | `MAX(0, SUM(stock SLOCs))` |
| `STR` | FLOAT | listing | Part 1 | `SUM(sale SLOCs)` — last-N-day sale velocity |
| `SZ_STK` | FLOAT | alloc | Stage B.1 from `ARS_MSA_VAR_ART` | Per-size store stock |
| `ACS_D` | FLOAT | listing | Part 3.5 from `ARS_CALC_ST_MAJ_CAT` | Per-store per-MAJ_CAT avg daily sale; overridden by `MANUAL_DENSITY` at Part 4c |
| `ALC_D` | FLOAT | listing | Part 3.5 | Allocation density (days of stock to plan for) |
| `AUTO_GEN_ART_SALE` | FLOAT | listing | Part 3.5b from `MASTER_GEN_ART_SALE.SAL_PD` | Article-level per-day sale (used by Part 4c) |
| `MAX_DAILY_SALE` | FLOAT | listing+alloc | Part 4c | Per-day sale velocity — Stage A.4 ranking tiebreaker |
| `AGE` | INT | listing | Part 3.5c from `MASTER_GEN_ART_AGE` | Days since first stocked |
| `MANUAL_DENSITY` | FLOAT | listing | Part 4c (article-level override) | Overrides `ACS_D` per (WERKS, GEN_ART, CLR) if present |

### C. Warehouse / MSA columns

| Variable | Type | Grain | Source / set by | Formula / meaning |
|---|---|---|---|---|
| `MSA_FNL_Q` | FLOAT | listing | Part 3.55 from `ARS_MSA_GEN_ART.FNL_Q` | Total warehouse stock for `(RDC, MAJ_CAT, GEN_ART, CLR)` |
| `MSA_FNL_Q_REM` | FLOAT | listing | Stage A.1 add, updated by `_revalidate_after_band` | Live remaining warehouse stock after this band |
| `FNL_Q` | FLOAT | alloc | Stage B.1 from `ARS_MSA_VAR_ART.FNL_Q` | Variant-level warehouse stock |
| `FNL_Q_REM` | FLOAT | alloc | `_snapshot_fnl_q_rem` before each band | Live variant pool at the moment of this band |
| `VAR_COUNT` | INT | listing | Part 3.55 | Number of variants in this `(GEN_ART, CLR)` |
| `VAR_FNL_COUNT` | INT | listing | Part 3.55 | Number of variants with `FNL_Q > 0` |
| `RL_HOLD_QTY` | FLOAT | listing | Part 3.54 from `ARS_NL_TBL_HOLD_TRACKING` (open holds only) | Carry-over warehouse hold from prior runs |

### D. Per-store / per-category settings (from cascade tables)

| Variable | Type | Source | Cascade | Used by |
|---|---|---|---|---|
| `LISTING` | INT | Part 3.5a Step 1 → Step 2 override | `ARS_CALC_ST_MAJ_CAT` baseline; `ARS_CALC_ST_ART` overrides | Part 7 filter; Stage A R01 |
| `I_ROD` | INT | Part 3.5a Step 1 → Step 2 override → Step 3 force | Same cascade; **CLR IN ('A','A_MIX') → I_ROD=2** override | Stage C round count |
| `CLR_MIN`, `CLR_MAX` | INT | Part 3.5a Step 1 only | `ARS_CALC_ST_MAJ_CAT` | Colour-cap logic |
| `FOCUS_W_CAP` | INT | Part 3.5a Step 2 only | `ARS_CALC_ST_ART` | Stage A.3 priority tier (=2) |
| `FOCUS_WO_CAP` | INT | Part 3.5a Step 2 only | `ARS_CALC_ST_ART` | Stage A.3 priority tier (=1, highest) |

### E. Per-grid columns (per active grid in `ARS_GRID_BUILDER`)

For each active grid the system writes a family of columns. Example for MJ-grid: `MJ_MBQ`, `MJ_STK_TTL`, `MJ_CONT`, `MJ_OPT_CNT`, `MJ_DISP_Q`, `MJ_REQ`. Same pattern for `MJ_FAB_*`, `MJ_MICRO_MVGR_*`, `MJ_M_VND_CD_*`, etc.

| Variable | Type | Grain | Source / set by | Formula / meaning |
|---|---|---|---|---|
| `{prefix}_MBQ` | FLOAT | listing | Part 4a from grid table | Target MBQ at this grouping level |
| `{prefix}_STK_TTL` | FLOAT | listing | Part 4a | Stock total at this grouping level |
| `{prefix}_CONT` | FLOAT | listing | Part 4a | Contribution % at this level |
| `{prefix}_OPT_CNT` | INT | listing | Part 4a | OPT count at this level |
| `{prefix}_DISP_Q` | FLOAT | listing | Part 4a | Display capacity at this level |
| `{prefix}_REQ` | FLOAT | listing | Part 4e | `MAX(0, MBQ − deducted_STK_TTL)` |
| `{prefix}_REQ_REM` | FLOAT | listing | Stage C dec by `_revalidate_after_band` | Live remaining requirement |
| `{prefix}_HOLD_REM` | FLOAT | listing | Stage A.1 add, updated by Stage C | Live remaining hold at this level |
| `PER_OPT_SALE` | FLOAT | listing | Part 4b from grid flagged `use_for_opt_sale=1` | Expected daily sale per option |

### F. OPT targets (per-store-OPT)

| Variable | Type | Source / set by | Formula |
|---|---|---|---|
| `OPT_MBQ` | FLOAT | Part 4c | `ROUND(ACS_D + ALC_D × <density adj>, 0)` |
| `OPT_REQ` | FLOAT | Part 4c | `MAX(0, OPT_MBQ − STK_TTL)` |
| `OPT_MBQ_WH` | FLOAT | Part 4c | `OPT_MBQ + hold_days × per-day-sale` |
| `OPT_REQ_WH` | FLOAT | Part 4c | `MAX(0, OPT_MBQ_WH − STK_TTL)` |
| `ART_EXCESS` | FLOAT | Part 4d | `MAX(0, STK_TTL − eff_mult × OPT_MBQ)` (MIX rows skipped) |
| `EXCESS_STK` | FLOAT | Part 4d alias | Audit alias for `ART_EXCESS` |

### G. Per-size targets (alloc only, set in Stage B / scaled in Stage C)

| Variable | Type | Source / set by | Formula |
|---|---|---|---|
| `CONT` | FLOAT | Stage B.2 (fallback hierarchy) | Store → CO → FNL_Q ratio → `1 / var_count` |
| `SZ_MBQ` | FLOAT | Stage B.3 / `_scale_demand_for_round` | `ROUND(OPT_MBQ × CONT, 0)` floored to 1 if `CONT>0 AND OPT_MBQ>0 AND ROUND=0` |
| `SZ_MBQ_WH` | FLOAT | Stage B.3 / `_scale_demand_for_round` | `ROUND(OPT_MBQ_WH × CONT, 0)` with same floor-to-1 rule |
| `SZ_REQ` | FLOAT | Stage B.3 / `_scale_demand_for_round` | `MAX(0, SZ_MBQ − SZ_STK − ALLOC_QTY − HOLD_QTY)` |
| `SZ_REQ_WH` | FLOAT | Stage B.3 / `_scale_demand_for_round` | `MAX(0, SZ_MBQ_WH − SZ_STK − ALLOC_QTY − HOLD_QTY)` ← **used by waterfall** |

### H. Classification & priority

| Variable | Type | Source / set by | Values |
|---|---|---|---|
| `OPT_TYPE` | NVARCHAR(10) | Part 3.6 `_classify_opt_type` | `MIX` / `RL` / `TBC` / `TBL` (never changes after Part 3.6) |
| `OPT_PRIORITY_TIER` | INT | Stage A.3 | 1 (FOCUS_WO_CAP) / 2 (FOCUS_W_CAP) / 3 (regular) |
| `OPT_PRIORITY_RANK` | INT | Stage A.4 — ROW_NUMBER per `(WERKS, OPT_TYPE, MAJ_CAT)` | Sort: TIER ASC, SEC_CT% DESC, MAX_DAILY_SALE DESC, OPT_REQ_WH DESC |
| `ST_RANK` | INT | Part 6 — ROW_NUMBER per `MAJ_CAT` | Sort: `W_SCORE DESC, WERKS ASC` |
| `LISTED_FLAG` | INT | Stage A.2 | 1 if all R01-R09 rules pass; 0 otherwise |
| `LISTED_REASON` | NVARCHAR | Stage A.2 | Concatenated R01;R02;... tokens for failed rules |
| `OPT_STATUS` | NVARCHAR(10) | Part 8.5 post-alloc | `RL` / `MIX` / `NL` / `TBL` (feeds next cycle) |
| `ALLOC_FLAG` | INT | Part 7 | `1 if PRI_CT% ≥ 100 else 0` |
| `PRI_CT%` | FLOAT | Upstream (contrib) | Primary grid contribution coverage % |
| `SEC_CT%` | FLOAT | Upstream (contrib) | Secondary grid contribution coverage % |
| `PRI_CT_REM` | FLOAT | Stage A.1 add, dec by `_revalidate_after_band` | Live remaining primary coverage |

### I. Allocation result columns (alloc table)

| Variable | Type | Source / set by | Meaning |
|---|---|---|---|
| `ALLOC_QTY` | FLOAT | `_run_band` SHIP portion | What physically ships |
| `HOLD_QTY` | FLOAT | `_run_band` HOLD portion (TBL only) | Warehouse reserve for this store |
| `SHIP_QTY` | FLOAT | `_run_band` | Same as ALLOC_QTY (legacy duplicate; some readers use SHIP_QTY) |
| `ROUND_SHIP` | FLOAT | `_run_band` per-band delta | This-round-only ship (used by `_revalidate_after_band`) |
| `ROUND_HOLD` | FLOAT | `_run_band` per-band delta | This-round-only hold |
| `POOL_CONSUMED` | FLOAT | `_run_band` running | Total pool taken across all rounds |
| `FROM_HOLD_QTY` | FLOAT | Part 8.6 DDL guard; written by alloc | Pieces drawn from prior-run hold (vs fresh MSA) |
| `ALLOC_STATUS` | NVARCHAR(50) | `_run_band` / `_pre_band_check` / `_rerank_*` | `PENDING` / `ALLOCATED` / `SKIPPED` |
| `SKIP_REASON` | NVARCHAR | various skip points | `NO_POOL_MSA` / `R06_PRI_REM` / `R07_SIZE_RATIO_LIVE` / `R09_HEADROOM_TRIVIAL` / `R09_STORE_BROKEN` |
| `ALLOC_REMARKS` | NVARCHAR(MAX) | `_run_band` appends | Per-band audit line `' B[ot.r{r}.rk{rk}] sh=X hld=Y pool=A->B;'` |
| `ALLOC_ROUND` | INT | `_run_band` | Which round of I_ROD set this row's status |
| `ALLOC_REASON` | NVARCHAR | Stage D classification | `FULL` / `PARTIAL` / `NO_POOL` / `BUDGET_CAP` / `SKIPPED_R06` / ... |
| `ALLOC_SEQ` | INT | Stage D | Sequence number for audit |
| `ALLOC_PHASE` | NVARCHAR | Stage D | Which phase committed this row |
| `OPT_PRIORITY_RANK` | INT | Stage A.4 / re-set by `_rerank_opt_priority_pandas` for TBC/TBL | See section H |

### J. Master-product attribute columns (carry-over from MP)

| Variable | Source | Usage |
|---|---|---|
| `FAB` | `vw_master_product` via Part 4 pre-resolve | Sec-grid cap, hierarchy |
| `MACRO_MVGR` | MP | Sec-grid, R07 grouping |
| `MICRO_MVGR` | MP | Sec-grid |
| `M_VND_CD` | MP | Sec-grid, hierarchy |
| `RNG_SEG` | MP | MRP tier (E/V/P/SP) — Primary grid `MJ_RNG_SEG` |
| `MERGE_RNG_SEG` | Derived | CASE expression from `derived_masters` mapping RNG_SEG |
| `M_YARN_02` | MP | Sec-grid |
| `WEAVE_2` | MP | Sec-grid |

### K. Growth / cap columns (set in Part 7)

| Variable | Source / set by | Formula |
|---|---|---|
| `MJ_MBQ_REV` | Part 7 | `MJ_MBQ × mj_req_growth_pct / 100` (immutable MJ_MBQ never mutated) |
| `MJ_REQ_REV` | Part 7 | `MAX(0, MJ_MBQ_REV − MJ_STK_TTL)` |
| `MJ_REQ_REM` | Stage A.1 add; dec by `_revalidate_after_band` | Live remaining MJ-level request |
| `MJ_REQ_ORIG` | Part 7 | Original `MJ_REQ` preserved for audit |
| `TBL_LISTED_DATE` | Part 8.5 | `GETDATE()` on first TBL ship for this `(WERKS, GEN_ART, CLR)` |

---

### L. All conditions / gates / rules — single lookup

Every "if this then that" in the pipeline, in execution order:

| # | Condition | Where | What happens if true | Notes |
|---|---|---|---|---|
| 1 | `STK_TTL < threshold × ACS_D AND MSA_FNL_Q = 0 AND RL_HOLD_QTY = 0` | Part 3.6 branch 1 | `OPT_TYPE = MIX` (nothing to ship) | First match wins |
| 2 | `VAR_COUNT > 0 AND (VAR_FNL_COUNT/VAR_COUNT < threshold OR VAR_FNL_COUNT < MinSz)` | Part 3.6 branch 2 | `OPT_TYPE = MIX` (sparse colour) | MinSz half only when `MinSz > 0` |
| 3 | `(STK_TTL ≥ threshold × ACS_D OR RL_HOLD_QTY > 0) AND MSA_FNL_Q > 0` | Part 3.6 branch 3 | `OPT_TYPE = RL` (top up) | Adequate stock + supply |
| 4 | `STK_TTL > 0 AND STK_TTL < threshold × ACS_D AND (MSA_FNL_Q > 0 OR RL_HOLD_QTY > 0)` | Part 3.6 branch 4 | `OPT_TYPE = TBC` (partial) | Some stock, supply exists |
| 5 | `STK_TTL ≤ 0 AND (MSA_FNL_Q > 0 OR RL_HOLD_QTY > 0)` | Part 3.6 branch 5 | `OPT_TYPE = TBL` (launch) | Empty + supply |
| 6 | (else) | Part 3.6 branch 6 | `OPT_TYPE = MIX` (catch-all) | Defensive |
| **Part 7 filters** | | | | |
| 7a | `MSA_FNL_Q > 0 OR RL_HOLD_QTY > 0` | Part 7 | Row kept | Something to ship |
| 7b | `OPT_REQ_WH ≥ 1` | Part 7 | Row kept | Demand ≥ 1 piece |
| 7c | `OPT_TYPE ≠ 'TBL' OR VAR_COUNT = 0 OR VAR_FNL_COUNT/VAR_COUNT ≥ threshold [OR VAR_FNL_COUNT ≥ MinSz]` | Part 7 | Row kept | TBL 60% size coverage gate |
| 7d | `LISTING = 1` | Part 7 | Row kept | Administratively enabled |
| 7e | `MJ_DISP_Q > 0` | Part 7 (**added 2026-05-25**) | Row kept | Store has display capacity |
| 7f | `PRI_CT% ≥ 100` | Part 7 | `ALLOC_FLAG = 1` | Primary coverage gate |
| **Stage A rules** | | | | |
| R01 | `LISTING ≠ 1` | Stage A.2 | `LISTED_FLAG = 0; LISTED_REASON += 'R01_LISTING;'` | |
| R02 | `OPT_TYPE = 'MIX'` | Stage A.2 | `LISTED_FLAG = 0; … R02_NOT_MIX;` | |
| R04 | `MSA_FNL_Q ≤ 0 AND RL_HOLD_QTY ≤ 0` | Stage A.2 | `… R04_MSA_POS;` | |
| R05 | `OPT_REQ_WH < 1` | Stage A.2 | `… R05_REQ_POS;` | |
| R06 | `PRI_CT% < 100 AND ALLOC_FLAG ≠ 1 AND OPT_TYPE IN (enforced types)` | Stage A.2 | `… R06_PRI_100;` | TBL always; RL/TBC if pri_ct_check_* |
| R07 | `OPT_TYPE = 'TBL' AND VAR_FNL_COUNT/VAR_COUNT < threshold AND VAR_FNL_COUNT < MinSz` | Stage A.2 | `… R07_VAR_RATIO_TBL;` | Listing-time recheck |
| R09 | `MJ_MBQ × cap_factor − MJ_STK_TTL < 0.5 × ACS_D` | Stage A.2 | `… R09_HEADROOM_TRIVIAL;` | Headroom check |
| **Stage A priority** | | | | |
| Tier 1 | `FOCUS_WO_CAP = 1` | Stage A.3 | `OPT_PRIORITY_TIER = 1` | Highest |
| Tier 2 | `FOCUS_W_CAP = 1` | Stage A.3 | `OPT_PRIORITY_TIER = 2` | |
| Tier 3 | (default) | Stage A.3 | `OPT_PRIORITY_TIER = 3` | Regular |
| **Stage C gates** | | | | |
| R09 (live) | `(cap_pct × MJ_MBQ) − MJ_STK_TTL − alloc_running < 0.5 × ACS_D` | `_check_r09_eligibility` (between OPT_TYPEs) | Skip all upcoming OPT_TYPE rows at this store with `SKIP_REASON='R09_HEADROOM_TRIVIAL'` | |
| Pre-band PRI | `PRI_CT_REM < 100 AND ALLOC_FLAG ≠ 1 AND ot in enforced` | `_pre_band_check` | Mark `SKIPPED, R06_PRI_REM` | |
| Store-broken | `MJ_REQ_REM[WERKS] < 0.5 × ACS_D` | `_pre_band_check` | All this store's rows `SKIPPED, R09_STORE_BROKEN` | |
| R07 live | `SIZE_RATIO < threshold AND _var_fnl < MinSz` | `_rerank_opt_priority_pandas` (TBC/TBL only) | Mark `SKIPPED, R07_SIZE_RATIO_LIVE` | Both must be true |
| TBL band size | `(avail < 3 AND ratio < 0.6)` per `(WERKS, GEN_ART, CLR, VAR_ART)` | `_run_band` TBL only | Silently drop from band (no remark) | |
| Pool empty | `FNL_Q_REM ≤ cum_prev` after sort | `_run_band` | `take_pool = 0`; row marked `SKIPPED, NO_POOL_MSA` | |
| Budget clip | `SUM(take_pool by WERKS) > budget[WERKS]` | `_run_band` (RL/TBC only) | Scale `take_pool *= budget / sum` | TBL cap=0, no clip |
| **Stage D gates** | | | | |
| PAK_SZ rounding | `ALLOC_QTY % PAK_SZ ≠ 0` | `_stage_d_apply_pak_sz_rounding` | Round to nearest PAK_SZ multiple | |
| OPT_MJ_REQ gate | `req_rem < 0.5 × OPT_MBQ` walking RL→TBC→TBL | `_stage_c_apply_opt_mj_req_gate` | Skip this OPT, mark `R10_MJ_REQ_EXHAUSTED` | Sequential per store |
| **Post-alloc OPT_STATUS** | | | | |
| TBL → NL | `OPT_TYPE='TBL' AND ALLOC_QTY > 0 AND (STK_TTL + ALLOC_QTY) ≥ thr × ACS_D` | Part 8.5 | `OPT_STATUS = NL` | Graduates next cycle |
| TBL → TBL (retry) | `OPT_TYPE='TBL' AND (ALLOC_QTY = 0 OR ratio < thr)` | Part 8.5 | `OPT_STATUS = TBL` | Stays TBL |
| TBC → RL | `OPT_TYPE='TBC' AND ALLOC_QTY > 0 AND (STK_TTL + ALLOC_QTY) ≥ thr × ACS_D` | Part 8.5 | `OPT_STATUS = RL` | Graduates |
| TBC → MIX | `OPT_TYPE='TBC' AND (ALLOC_QTY = 0 OR ratio < thr)` | Part 8.5 | `OPT_STATUS = MIX` | |
| TBL_LISTED_DATE | `OPT_TYPE='TBL' AND ALLOC_QTY > 0 AND TBL_LISTED_DATE IS NULL` | Part 8.5 | `TBL_LISTED_DATE = GETDATE()` | First-ship stamp |

### M. Key formulas — at a glance

| Formula | Purpose |
|---|---|
| `STK_TTL = MAX(0, SUM(stock SLOCs))` | In-store stock baseline (Part 1) |
| `RL_HOLD_QTY = SUM(HOLD_REM) where IS_CLOSED=0` | Carry-over warehouse hold (Part 3.54) |
| `OPT_MBQ = ROUND(ACS_D + ALC_D × <density adj>, 0)` | Per-store-OPT target (Part 4c) |
| `OPT_MBQ_WH = OPT_MBQ + hold_days × per-day-sale` | Target + WH buffer (Part 4c) |
| `OPT_REQ_WH = MAX(0, OPT_MBQ_WH − STK_TTL)` | Demand including hold (Part 4c) |
| `ART_EXCESS = MAX(0, STK_TTL − eff_mult × OPT_MBQ)` | Over-stock detector (Part 4d) |
| `MJ_MBQ_REV = MJ_MBQ × mj_req_growth_pct / 100` | Growth-scaled target (Part 7) |
| `ALLOC_FLAG = (PRI_CT% ≥ 100)` | Primary eligibility (Part 7) |
| `SIZE_RATIO = VAR_FNL_COUNT / VAR_COUNT` | Size coverage (Parts 3.6 / 7 / Stage A.2 R07 / Stage C R07-live) |
| `CONT` fallback hierarchy | Store → CO → FNL_Q ratio → 1/var_count (Stage B.2) |
| `SZ_MBQ = floor-to-1(ROUND(OPT_MBQ × CONT, 0))` | Per-size target (Stage B.3) |
| `take_pool = MAX(0, MIN(need_pool, FNL_Q_REM − cum_prev))` | Pool draw per row (`_run_band`) |
| `SHIP = MIN(take_pool, SZ_REQ); HOLD = take_pool − SHIP` | ALLOC/HOLD split (`_run_band`) |
| `W_SCORE = REQ_RANK × req_weight + FILL_RANK × fill_weight` | Store rank score (Part 6) |
| `ST_RANK = ROW_NUMBER OVER (PARTITION BY MAJ_CAT ORDER BY W_SCORE DESC, WERKS ASC)` | Final store rank (Part 6) |
| `R09 headroom = cap × MJ_MBQ − MJ_STK_TTL − alloc_running` | R09 eligibility gate |

### N. Settings the user can change (UI knobs)

| Knob | Default | Affects |
|---|---|---|
| `Stock%` (`size_threshold` / `threshold`) | 0.6 | OPT_TYPE branch 1/3/4 thresholds AND R07 size-coverage ratio everywhere |
| `MinSz` (`min_size_count`) | 3 | OPT_TYPE MIX(b) absolute count; R07 second condition |
| `default_acs_d` | 18 | `ACS_D` fallback when missing/zero |
| `mj_req_growth_pct` | 100 | `MJ_MBQ_REV` scaling in Part 7 |
| `rl_mbq_cap_pct` | 0 (use PRI strict) | RL waterfall cap when PRI gate is OFF |
| `tbc_mbq_cap_pct` | 0 (use PRI strict) | TBC waterfall cap when PRI gate is OFF |
| `pri_ct_check_rl` | False | Whether RL enforces PRI_CT ≥ 100 (R06) |
| `pri_ct_check_tbc` | False | Whether TBC enforces PRI_CT ≥ 100 (R06) |
| `apply_sec_cap_in_normal` | False | Apply secondary-grid MBQ caps |
| `n_workers` | 4 | Stage C process pool size |
| `allocation_mode` | "pandas" | "pandas" (default) or "sequential" |
| `mix_mode` | "st_maj_rng" | How MIX rows are aggregated in Part 3.7 |
| `req_weight`, `fill_weight` | 0.4 / 0.6 | Store ranking weights in Part 6 |

---

## Glossary

| Term | Plain English |
|---|---|
| **WERKS** | Store code |
| **MAJ_CAT** | Major category |
| **GEN_ART** | Article number (one design before colour/size) |
| **CLR** | Colour code |
| **VAR_ART** | Variant — same article, same colour, different size band |
| **SZ** | The actual size label within a variant |
| **OPT_TYPE** | MIX / RL / TBC / TBL (set at Part 3.6, never changes) |
| **OPT_STATUS** | Post-alloc label (Part 8.5) — feeds next cycle |
| **STK_TTL** | Total store stock for this OPT |
| **MSA_FNL_Q** | Warehouse stock available to ship |
| **FNL_Q_REM** | Live pool during allocation (drains as stores take) |
| **ACS_D** | Average daily sale (store × category) |
| **OPT_MBQ** | Per-store target quantity |
| **OPT_MBQ_WH** | OPT_MBQ + warehouse hold buffer |
| **CONT** | Size contribution % from `Master_CONT_SZ` |
| **SZ_MBQ** | `ROUND(OPT_MBQ × CONT, 0)` floored to 1 if CONT > 0 |
| **SZ_REQ** | `MAX(0, SZ_MBQ − SZ_STK)` |
| **ALLOC_QTY** | What ships to the store |
| **HOLD_QTY** | What the warehouse reserves (TBL only) |
| **ALLOC_FLAG** | 1 if primary-grid coverage 100% |
| **LISTED_FLAG** | 1 if all R01–R09 rules passed |
| **PRI_CT% / SEC_CT%** | Primary / secondary coverage quality scores |
| **I_ROD** | Rounds of display (TBL usually 2) |
| **ST_RANK** | Store rank within MAJ_CAT — first-come-first-served |
| **OPT_PRIORITY_RANK** | OPT rank within a store |
| **PAK_SZ** | Pack size (alloc rounded to multiples of this) |

---

## How to update this doc

Bump `last_reviewed` and refresh the worked example when:

- A new Part is added/removed in `listing.py` (e.g. Part 3.8, Part 4f, Part 8.7).
- A new rule R10+ is added in `_stage_a_apply_rules`.
- The `_run_band` algorithm changes (cumsum order, budget clip math, ALLOC/HOLD split).
- The Approve path adds a new write target (new grid, new ledger).
- The 60% rule's compound condition changes (`AND` → `OR`, or per-VAR_ART → per-GEN_ART).
- `Master_CONT_SZ` fallback hierarchy changes.

Then re-run the U128 / 1134114962 / L_GRY (or whichever current real article) trace and refresh the worked numbers.
