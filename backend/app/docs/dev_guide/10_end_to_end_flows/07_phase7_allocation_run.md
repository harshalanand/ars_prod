# Phase 7 — Allocation Engine Run

The decision phase. With listing eligible, contribution computed, and
warehouse stock known, the allocation engine produces "ship X units of
Y to store Z."

---

## Goal

For each (article, store) eligible per Phase 5, decide how many units
to ship from warehouse. Honour min/max constraints, store grades, and
warehouse capacity. Persist as `ARS_ALLOCATION_MASTER` (header) +
`ARS_ALLOCATION_DETAIL` (lines) + `ARS_ALLOCATION_AUDIT`.

## Inputs

- `ARS_LISTING_MASTER` — eligibility flag per (store, article)
- `PER_OPT_SALE` — desired per-day demand per (store, article)
- `ARS_CONTRIB_RESULTS_<jobid>` — contribution % per article
- `ARS_WAREHOUSE_STOCK` — what's available to ship
- Allocation spec (from UI / API): type, products, stores, constraints

## Outputs

| Table | Purpose |
|---|---|
| `ARS_ALLOCATION_MASTER` | Header — one row per allocation run |
| `ARS_ALLOCATION_DETAIL` | Lines — one per (store, article) decision |
| `ARS_ALLOCATION_AUDIT` | Trail of every change made |
| `ARS_pend_alc` | Live pending — what hasn't shipped yet |

---

## Flowchart — score-based engine v2

```
                  POST /allocation-engine/run
                            │
                            ▼
   ┌─────────────────────────────────────────────────────┐
   │  allocation_engine.run(spec):                        │
   │                                                       │
   │  STEP 1 — Build candidate set                        │
   │      LEFT JOIN listing × per_opt_sale × contrib       │
   │      Filter: ELIGIBLE = 1, target stores in spec      │
   │                                                       │
   │  STEP 2 — Compute desired qty per row                │
   │      desired = OPT_CNT × contribution_share          │
   │      (subject to min_per_store, max_per_store)        │
   │                                                       │
   │  STEP 3 — Pull warehouse stock                        │
   │      WH = SUM(qty) per article in ARS_WAREHOUSE_STOCK │
   │                                                       │
   │  STEP 4 — Score-based waterfall                       │
   │      For each article (sorted by warehouse stock):    │
   │        rank stores by score (sales × need × grade)    │
   │        allocate proportionally until WH stock = 0     │
   │                                                       │
   │  STEP 5 — Enforce constraints                         │
   │      - per-store cap                                   │
   │      - warehouse balance (no negative)                 │
   │      - listing flag (re-check)                         │
   │      Iterate until stable                              │
   │                                                       │
   │  STEP 6 — Persist                                     │
   │      INSERT MASTER (1 row)                            │
   │      INSERT DETAIL (N rows, bulk)                     │
   │      INSERT AUDIT (header)                             │
   │      INSERT pend_alc (N rows)                          │
   └─────────────────────────────────────────────────────┘
                            │
                            ▼
                allocation_code returned
```

## Five allocation types

| Type | Logic |
|---|---|
| **store_grade** | Distribute by A/B/C/D ratios; e.g. A=4, B=2, C=1, D=1 → 50% / 25% / 12.5% / 12.5% |
| **size_curve** | Apply size × colour grid; preserves merchandise integrity |
| **stock_based** | Allocate proportional to current store stock (top-up restocking) |
| **sales_based** | Proportional to recent sales velocity (chase the hits) |
| **manual** | User specifies quantities directly; no math |

For new launches: `store_grade` is the default. For re-orders:
`sales_based`. For seasonal stuff: `stock_based`.

---

## Step-by-step (score-based engine v2)

### Step 1 — Candidate set

```sql
SELECT
    L.WERKS, L.GEN_ART, L.VAR_ART, L.CLR,
    P.PER_DAY_SALE, P.OPT_CNT, P.MBQ,
    C.STORE_CONTRIB_<MAJ_CAT> AS contrib_pct,
    SG.STORE_GRADE
FROM ARS_LISTING_MASTER L
LEFT JOIN PER_OPT_SALE P
    ON L.WERKS = P.WERKS AND L.GEN_ART = P.GEN_ART
LEFT JOIN ARS_CONTRIB_RESULTS_<jobid> C
    ON L.WERKS = C.WERKS AND L.GEN_ART = C.GEN_ART
LEFT JOIN ARS_STORE_GRADE SG ON L.WERKS = SG.STORE_CODE
WHERE L.IS_LISTED = 1
  AND L.GEN_ART IN (<spec.products>)
  AND L.WERKS IN (<spec.stores>);
```

### Step 2 — Desired quantity

```python
candidates['desired'] = candidates['OPT_CNT'] * candidates['contrib_pct'] / 100.0

# Clip to bounds
lo = spec.min_per_store
hi = spec.max_per_store
candidates['desired'] = candidates['desired'].clip(lower=lo, upper=hi)
```

### Step 3 — Warehouse stock

```sql
SELECT GEN_ART, VAR_ART, CLR, SUM(QTY) AS available
FROM ARS_WAREHOUSE_STOCK
WHERE GEN_ART IN (<spec.products>)
GROUP BY GEN_ART, VAR_ART, CLR;
```

### Step 4 — Allocate via waterfall

For each article (in priority order — typically warehouse-stock-rich first):

```python
def waterfall(article_rows, available):
    # Rank each store row by composite score
    article_rows['score'] = (
        0.5 * article_rows['PER_DAY_SALE'] / article_rows['PER_DAY_SALE'].max()
      + 0.3 * article_rows['contrib_pct'] / 100
      + 0.2 * grade_score(article_rows['STORE_GRADE'])
    )
    article_rows = article_rows.sort_values('score', ascending=False)

    # Allocate desired qty top-down until stock runs out
    article_rows['allocated'] = 0
    remaining = available
    for idx, row in article_rows.iterrows():
        give = min(row['desired'], remaining)
        article_rows.at[idx, 'allocated'] = give
        remaining -= give
        if remaining <= 0: break
    return article_rows
```

### Step 5 — Constraints loop

If any store exceeds its individual `max_per_store`, redistribute the
excess to other eligible stores. Iterate until stable:

```python
def enforce_constraints(allocations, spec, max_iters=5):
    for _ in range(max_iters):
        over = allocations[allocations['allocated'] > spec.max_per_store]
        if over.empty: break
        excess = (over['allocated'] - spec.max_per_store).sum()
        allocations.loc[over.index, 'allocated'] = spec.max_per_store
        # Redistribute excess back to candidates with desired > allocated
        candidates = allocations[allocations['allocated'] < allocations['desired']]
        ...
    return allocations
```

### Step 6 — Persist

```sql
-- Header
INSERT INTO ARS_ALLOCATION_MASTER (
    allocation_code, type, products_count, stores_count,
    total_qty, status, created_by, created_at
) VALUES (:code, 'store_grade', :pcnt, :scnt, :qty, 'pending', :user, GETDATE());

-- Detail (bulk via fast_executemany)
INSERT INTO ARS_ALLOCATION_DETAIL (
    allocation_code, store_code, article_number, var_art, clr, qty
) VALUES (?, ?, ?, ?, ?, ?);   -- per row, batched

-- Audit
INSERT INTO ARS_ALLOCATION_AUDIT (
    allocation_code, action, by, at, details
) VALUES (:code, 'CREATED', :user, GETDATE(), :details_json);

-- Pending
INSERT INTO ARS_pend_alc (allocation_code, ...)
SELECT :code, ... FROM ARS_ALLOCATION_DETAIL WHERE allocation_code = :code;
```

---

## Examples

### CLI: store-grade allocation

```bash
ALLOC=$(curl -s -X POST "$API/api/v1/allocation-engine/run" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "type":"store_grade",
    "products":["1234567","1234568"],
    "stores":["DH24","DH25","DH26","DH27"],
    "grade_ratios":{"A":4,"B":2,"C":1,"D":1},
    "min_per_store":1, "max_per_store":12,
    "contrib_results_job":"CTR_abc123def0"
  }' | jq -r .data.allocation_code)

echo "Allocation: $ALLOC"

# Inspect
curl -s -H "Authorization: Bearer $TOKEN" \
  "$API/api/v1/allocations/$ALLOC" | jq '.data | {total_qty, lines: .detail | length}'
```

### Compare to plan

```sql
SELECT D.store_code, D.qty, P.OPT_CNT, P.PER_DAY_SALE
FROM ARS_ALLOCATION_DETAIL D
JOIN PER_OPT_SALE P ON D.store_code = P.WERKS AND D.article_number = P.GEN_ART
WHERE D.allocation_code = 'ALC_2026_04_30_GRADE1'
ORDER BY D.qty DESC;
```

---

## What can go wrong

### Failure A — Allocation total exceeds warehouse stock

**Cause:** A bug in the waterfall (allocated more than `available`)
or the warehouse stock query was stale.

**Detect:**
```sql
SELECT D.article_number,
       SUM(D.qty) AS allocated,
       (SELECT SUM(QTY) FROM ARS_WAREHOUSE_STOCK W WHERE W.GEN_ART = D.article_number) AS available
FROM ARS_ALLOCATION_DETAIL D
WHERE D.allocation_code = 'ALC_...'
GROUP BY D.article_number
HAVING SUM(D.qty) > (SELECT SUM(QTY) FROM ARS_WAREHOUSE_STOCK W WHERE W.GEN_ART = D.article_number);
```

If non-empty → bug. Roll back the allocation, fix the engine.

### Failure B — Some stores got 0 even though listed

**Likely cause:** Their score was lower than warehouse capacity could
support; they got cut off by the waterfall.

**Detect:**
```sql
SELECT L.WERKS
FROM ARS_LISTING_MASTER L
LEFT JOIN ARS_ALLOCATION_DETAIL D
    ON D.allocation_code = 'ALC_...'
   AND D.store_code     = L.WERKS
WHERE L.IS_LISTED = 1
  AND L.GEN_ART = '1234567'
  AND D.qty IS NULL;
```

**Fix:** Increase warehouse stock, lower OPT_CNT for top stores, or
manually override.

### Failure C — Negative qty in detail

Should never happen. Add a guard:

```python
allocations['allocated'] = allocations['allocated'].clip(lower=0)
```

---

## Performance

| Phase | Typical time |
|---|---|
| Build candidates (50k rows × 300 stores) | 5-15 s |
| Warehouse pull | 2-5 s |
| Waterfall (50k articles) | 30-60 s with vectorised math |
| Constraint loop | 5-15 s per iteration, max 5 iterations |
| Persist (50k allocation lines) | 30-60 s via fast_executemany |
| **Total** | **2-15 min** depending on size |

---

## When this phase is healthy

- `allocation_code` returned within minutes.
- `ARS_ALLOCATION_DETAIL.SUM(qty) ≤ ARS_WAREHOUSE_STOCK.SUM(qty)` for every article.
- 0 rows with negative qty.
- Audit trail row exists for the allocation.
- Pending allocation table populated.

Phase 8 — Review & Override — can begin.
