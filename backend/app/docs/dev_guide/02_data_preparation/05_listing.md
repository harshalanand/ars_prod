# Listing

Decides which articles are "listed" (eligible for allocation) at which
stores. Output feeds `PER_OPT_SALE` calc table that the allocation
engine reads.

---

## What it does

Consumes MSA outputs + grid outputs + sales history → applies
business rules (rank cutoff, division eligibility, brand restrictions)
→ writes the per-store-per-article eligibility map.

If an article isn't listed at a store, no allocation rule will send it
there.

## Files

| Layer | File |
|---|---|
| Page | `frontend/src/pages/ListingPage.jsx` |
| Logs page | `frontend/src/pages/ListingLogsPage.jsx` |
| API | `app/api/v1/endpoints/listing.py` |
| Service | `app/services/listing_service.py` |
| Calc helpers | `app/services/grid_calculations.py` (`calculate_per_day_sale`) |
| Permission | View is open; `LISTING_RUN` for run actions |

## Two run modes

| Mode | What it does |
|---|---|
| **Generate** | Build the listing master from MSA + grid outputs (full rebuild) |
| **Run** | Apply listing rules to today's allocation candidates (incremental) |

### Generate flow

```
1. Read MSA outputs (ARS_MSA_GEN_ART)
2. Read primary grid output (the one with use_for_opt_sale=true)
3. Compute per-day sale (calculate_per_day_sale):
       PER_DAY_SALE = AVG_SALES_LAST_30_DAYS / 30
4. Compute optimal count (OPT_CNT):
       OPT_CNT = MIN(MBQ, ROUND(PER_DAY_SALE * 7))   # 7-day cover
5. Apply listing rules:
       eligible = STK_TTL > 0
                  AND PER_DAY_SALE > threshold
                  AND brand IN allowed_brands
                  AND division IN allowed_divisions
6. Write ARS_LISTING_MASTER + PER_OPT_SALE
```

### Run flow (incremental)

```
1. Find rows in MSA output added/changed since last run
2. Apply rules to those rows only
3. Upsert into ARS_LISTING_MASTER
```

## Why incremental matters

Generate is heavy (15-30 min on full data). Run is fast (<1 min) for
typical daily refreshes. Default to Run; only Generate when rules
change or output is corrupted.

## Files = where each step lives

| Step | Where |
|---|---|
| Per-day sale calc | `grid_calculations.calculate_per_day_sale` |
| Optimal count calc | `listing_service._compute_opt_cnt` (or similar) |
| Eligibility rules | `listing_service._apply_eligibility` |
| Output write | `listing_service._write_listing` |

## Example: trigger generate as a job

```bash
curl -X POST "$API/api/v1/listing/generate" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{ "maj_cats": ["MENS_APP", "WOMENS_APP"] }'
```

Returns `job_id`; appears in Jobs Dashboard with progress.

## Example: trigger an incremental run

```bash
curl -X POST "$API/api/v1/listing/run" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

(No body needed if using the most recent generate as baseline.)

## Common changes

### Recipe: tweak the listing-rank formula

`listing_service._compute_rank` (default ranks by sales contribution
× stock availability). To favour newness:

```python
# Multiply by exponential decay on age
df['RANK'] = df['SALES_CONTRIB'] * df['STK_AVAILABILITY'] \
           * np.exp(-df['AGE_DAYS'] / 30)
```

### Recipe: add a new eligibility rule

```python
# In _apply_eligibility:
df['ELIGIBLE_NEW_RULE'] = (df['BRAND_TIER'] == 'PREMIUM') & (df['STK_TTL'] > 5)
df['ELIGIBLE'] &= df['ELIGIBLE_NEW_RULE']
```

Document the new rule in the page's tooltip.

### Recipe: per-store listing override

Currently rules are global. To allow store-specific overrides:

1. Add `ARS_LISTING_OVERRIDES` table:
   ```sql
   CREATE TABLE ARS_LISTING_OVERRIDES (
     store_code NVARCHAR(20),
     article_number NVARCHAR(50),
     forced_listed BIT,
     forced_unlisted BIT,
     reason NVARCHAR(500),
     created_by NVARCHAR(100),
     created_at DATETIME2 DEFAULT SYSUTCDATETIME(),
     PRIMARY KEY (store_code, article_number)
   );
   ```
2. After applying global rules, LEFT JOIN overrides and apply:
   ```python
   df.loc[df['FORCED_LISTED']  == 1, 'ELIGIBLE'] = True
   df.loc[df['FORCED_UNLISTED']== 1, 'ELIGIBLE'] = False
   ```
3. UI to manage overrides in the Listing page or a dedicated screen.

## Performance reference

| Phase | Wall time |
|---|---|
| Read MSA + grid output | 5-10 s |
| Compute per-day sale (50k articles × 30 days) | 5-10 s |
| Apply eligibility rules | 2-5 s (vectorised pandas) |
| Write output (chunked upsert) | 30-60 s |
| **Generate end-to-end** | 1-2 min for typical workload |
| **Run (incremental, ~1k changed rows)** | <30 s |

## Watch for

- **Stale grid output**: if you Run before the grid that feeds OPT_CNT
  has been re-run, listing uses yesterday's MBQ. Always run grids
  before listing.
- **Sales data freshness**: `calculate_per_day_sale` uses Trend_*
  tables. If yesterday's Trend upload failed, today's listing is wrong.
  Check Data Checklist.
- **Manual overrides drift**: if you implement overrides, audit them
  monthly; "forced_listed" tends to accumulate.

## Index recommendations

```sql
CREATE INDEX IX_LISTING_MASTER_STORE ON ARS_LISTING_MASTER(store_code);
CREATE INDEX IX_PER_OPT_SALE_STORE_ART ON PER_OPT_SALE(store_code, article_number);
```

`PER_OPT_SALE` is hit by every allocation run; index it well.
