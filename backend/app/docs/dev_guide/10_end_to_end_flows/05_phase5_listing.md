# Phase 5 — Listing Generation

Allocation rules need to know "is this article allowed at this store?"
That answer comes from listing. Phase 5 produces the eligibility map.

---

## Goal

Produce `ARS_LISTING_MASTER` and `PER_OPT_SALE` so allocation logic
in Phase 7 can look up: for (store, article), is this article listed
and what's its expected daily demand?

## Inputs

- `ARS_MSA_GEN_ART` (from Phase 3)
- The "primary" grid output (one with `use_for_opt_sale=true`)
- Sales window from `Trend_*` tables (for per-day-sale)

## Outputs

| Table | Grain | Used by |
|---|---|---|
| `ARS_LISTING_MASTER` | Store × Article | Allocation engine eligibility check |
| `PER_OPT_SALE` | Store × Article | Allocation engine demand signal |

---

## Master flowchart

```
┌─────────────────────────┐  ┌──────────────────────┐  ┌──────────────────┐
│  ARS_MSA_GEN_ART        │  │ Primary grid output  │  │ Trend_Sales_Daily│
│  (stock available)      │  │ (MBQ / OPT_CNT)      │  │ (sales history)  │
└──────────┬──────────────┘  └──────────┬───────────┘  └────────┬─────────┘
           │                              │                       │
           ▼                              ▼                       ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │   listing_service.run_generate(maj_cats):                        │
   │                                                                    │
   │   1. JOIN MSA + grid + sales → unified candidate dataframe        │
   │   2. Compute PER_DAY_SALE = SUM(SALES_30D) / 30                    │
   │   3. Compute OPT_CNT = MIN(MBQ, ROUND(PER_DAY_SALE * 7))           │
   │   4. Apply eligibility rules:                                       │
   │        STK_TTL > 0                                                  │
   │        AND PER_DAY_SALE > min_threshold                             │
   │        AND BRAND ∈ allowed_brands_for_store                         │
   │        AND DIVISION ∈ allowed_divisions_for_store                   │
   │   5. Apply rank rules:                                              │
   │        rank = SALES_CONTRIB × STK_AVAILABILITY                      │
   │        (× freshness decay if applicable)                            │
   │   6. Write outputs:                                                 │
   │        TRUNCATE ARS_LISTING_MASTER + INSERT                         │
   │        TRUNCATE PER_OPT_SALE + INSERT                               │
   └─────────────────────────────────────────────────────────────────┘
                                 │
                                 ▼
              ARS_LISTING_MASTER + PER_OPT_SALE ready
```

---

## Step-by-step

### Step 1 — Build candidate dataframe

```python
candidates = (
    msa_gen_art
      .merge(grid_output[['WERKS','GEN_ART','MBQ','OPT_CNT']],
             on=['WERKS','GEN_ART'], how='left')
      .merge(sales_30d, on=['WERKS','GEN_ART'], how='left')
)
candidates['PER_DAY_SALE'] = candidates['SALES_30D'].fillna(0) / 30.0
```

### Step 2 — Compute OPT_CNT (optimal count)

```python
candidates['DAYS_OF_COVER'] = 7
target_qty = (candidates['PER_DAY_SALE'] * candidates['DAYS_OF_COVER']).round()
candidates['OPT_CNT'] = candidates[['MBQ', target_qty]].min(axis=1)
```

`OPT_CNT` answers: "how many units would I want at this store to cover
7 days of demand, capped at the minimum buy quantity."

### Step 3 — Apply eligibility rules

```python
mask = (
    (candidates['STK_TTL'] > 0) &
    (candidates['PER_DAY_SALE'] > 0.1) &  # at least 1 unit / 10 days
    candidates['BRAND'].isin(allowed_brands) &
    candidates['DIVISION'].isin(allowed_divisions)
)
candidates['ELIGIBLE'] = mask
```

### Step 4 — Apply ranking

```python
candidates['SALES_CONTRIB'] = (
    candidates['SALES_30D'] /
    candidates.groupby('MAJ_CAT')['SALES_30D'].transform('sum')
)
candidates['STK_AVAILABILITY'] = candidates['STK_TTL'] / candidates['STK_TTL'].max()
candidates['RANK'] = candidates['SALES_CONTRIB'] * candidates['STK_AVAILABILITY']
```

Optional freshness decay:
```python
import numpy as np
candidates['RANK'] *= np.exp(-candidates['AGE_DAYS'] / 30)
```

### Step 5 — Write outputs

```python
listing_master = candidates.loc[candidates['ELIGIBLE'],
    ['WERKS','GEN_ART','VAR_ART','CLR','RANK','LISTED_AT']]
listing_master['IS_LISTED'] = 1

per_opt_sale = candidates[
    ['WERKS','GEN_ART','VAR_ART','CLR','PER_DAY_SALE','OPT_CNT','MBQ']
]

# Write via the upsert engine (TRUNCATE + INSERT semantics)
upsert_engine.upsert(table_name='ARS_LISTING_MASTER', df=listing_master, ...)
upsert_engine.upsert(table_name='PER_OPT_SALE',       df=per_opt_sale,   ...)
```

---

## Run modes — Generate vs Run

| Mode | When | Time | Behaviour |
|---|---|---|---|
| **Generate** | Rules changed, full rebuild needed | 1-2 min | TRUNCATE + INSERT |
| **Run** | Daily refresh, source data changed since last run | <30 s | UPSERT only changed rows |

Default to Run for daily flows. Generate after a rule change or
weekly as hygiene.

---

## Examples

### Trigger generate via API

```bash
curl -X POST "$API/api/v1/listing/generate" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{ "maj_cats": ["MENS_APP","WOMENS_APP"] }'
```

### Verify result

```sql
SELECT COUNT(*) AS rows_listed,
       SUM(CASE WHEN IS_LISTED = 1 THEN 1 ELSE 0 END) AS eligible,
       AVG(RANK) AS avg_rank
FROM ARS_LISTING_MASTER;

SELECT TOP 10 WERKS, GEN_ART, PER_DAY_SALE, OPT_CNT, MBQ
FROM PER_OPT_SALE
ORDER BY PER_DAY_SALE DESC;
```

---

## What can go wrong

### Failure A — Listing master is empty

- Source MSA was empty (Phase 3 issue).
- Eligibility threshold too aggressive (e.g. PER_DAY_SALE > 10
  filters out everyone).

**Detect:**
```sql
SELECT COUNT(*) FROM ARS_LISTING_MASTER;     -- should be ~50k+
SELECT COUNT(*) FROM ARS_MSA_GEN_ART;        -- should be ~50k+
```

### Failure B — OPT_CNT is unrealistic

If `OPT_CNT` for a store is way higher than the warehouse can supply,
allocation will downsize but the input was wrong. Cap inside the
listing service:

```python
candidates['OPT_CNT'] = candidates['OPT_CNT'].clip(upper=999)
```

### Failure C — Same article listed twice for same store

Cause: master view returns multiple variant rows (a SAP master sync
issue). Add dedupe before write:

```python
listing_master = listing_master.drop_duplicates(subset=['WERKS','GEN_ART','VAR_ART','CLR'])
```

---

## Performance benchmarks

| Step | Time (typical) |
|---|---|
| Read MSA + grid + sales | 5-10 s |
| Compute PER_DAY_SALE | 5-10 s |
| Apply rules + rank | 2-5 s |
| Write outputs | 30-60 s |
| **Generate end-to-end** | 1-2 min |
| **Run (incremental)** | <30 s |

---

## When this phase is healthy

- `ARS_LISTING_MASTER` row count within ±10% of yesterday.
- `PER_OPT_SALE` row count matches.
- No store has 0 listed articles (would indicate division/brand filter
  totally excluding it).
- Avg PER_DAY_SALE is positive non-zero.
