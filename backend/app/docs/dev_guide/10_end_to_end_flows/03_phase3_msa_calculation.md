# Phase 3 — MSA Stock Calculation

The most central calculation in ARS. Turns raw stock + pending
allocations into the three master output tables every later phase
reads. If MSA is wrong, every downstream allocation is wrong.

---

## Goal

Produce three trusted "ready-to-use" tables:

- `ARS_MSA_TOTAL` — at the **(RDC, MAJ_CAT)** grain
- `ARS_MSA_GEN_ART` — at the **(RDC, MAJ_CAT, GEN_ART)** grain
- `ARS_MSA_VAR_ART` — at the **(RDC, MAJ_CAT, GEN_ART, VAR_ART, CLR)** grain

Each row contains: stock per SLOC, total stock, pending alloc, final
quantity available (`FNL_Q = max(STK - PEND, 0)`).

## Inputs

- Phase 1 + 2 successful (fresh data, all checks green).
- `ARS_STORE_SLOC_SETTINGS` configured (which SLOCs are active).
- `MSA_Filter_Config` has at least one named preset (or user fills
  filters manually).

## Outputs

| Table | Grain | Why |
|---|---|---|
| `ARS_MSA_TOTAL` | RDC × MAJ_CAT | Top-level allocation rules read this |
| `ARS_MSA_GEN_ART` | RDC × MAJ_CAT × GEN_ART | Article-level decisions |
| `ARS_MSA_VAR_ART` | RDC × MAJ_CAT × GEN_ART × Variant × CLR | Final pick-list grain |

---

## Master flowchart

```
┌────────────────────────────────────────────────────────────────────┐
│  ET_MSA_STK   +   MASTER_ALC_PEND   +   vw_master_product           │
│  (raw stock)      (pending allocs)      (article master)            │
└────────────┬────────────────┬────────────────┬───────────────────────┘
             │                │                │
             ▼                ▼                ▼
   ┌────────────────────────────────────────────────────┐
   │  STEP 1 — Filter SLOC                                │
   │     SLOC ∈ ARS_STORE_SLOC_SETTINGS where STATUS=ACT │
   └─────────────┬───────────────────────────────────────┘
                 ▼
   ┌────────────────────────────────────────────────────┐
   │  STEP 2 — Normalize                                  │
   │     Trim/uppercase keys; coalesce QTY → 0; text → 'NA' │
   └─────────────┬───────────────────────────────────────┘
                 ▼
   ┌────────────────────────────────────────────────────┐
   │  STEP 3 — Fill missing dimensions                    │
   │     ISNULL(MP.col, fallback) ensures every row has   │
   │     a complete (RDC, MAJ_CAT, GEN_ART, VAR_ART, CLR) │
   └─────────────┬───────────────────────────────────────┘
                 ▼
   ┌────────────────────────────────────────────────────┐
   │  STEP 4 — SEG = [APP, GM]                            │
   │     Drop rows where SEG not in (APP, GM)             │
   └─────────────┬───────────────────────────────────────┘
                 ▼
   ┌────────────────────────────────────────────────────┐
   │  STEP 5 — Pivot by SLOC                              │
   │     Long → Wide. Each SLOC becomes a column.         │
   │     Add STK_TTL = SUM across all SLOCs.              │
   └─────────────┬───────────────────────────────────────┘
                 ▼
   ┌────────────────────────────────────────────────────┐
   │  STEP 6 — Merge MASTER_ALC_PEND                      │
   │     LEFT JOIN on (RDC, MATNR) → adds PEND column     │
   └─────────────┬───────────────────────────────────────┘
                 ▼
   ┌────────────────────────────────────────────────────┐
   │  STEP 7 — Compute FNL_Q                              │
   │     FNL_Q = MAX(STK_TTL - PEND, 0)                   │
   └─────────────┬───────────────────────────────────────┘
                 ▼
   ┌────────────────────────────────────────────────────┐
   │  STEP 8 — Generate color variants                    │
   │     Each VAR_ART explodes per CLR present in master  │
   └─────────────┬───────────────────────────────────────┘
                 ▼
   ┌────────────────────────────────────────────────────┐
   │  STEP 9 — Aggregate                                  │
   │     Roll up to TOTAL / GEN_ART / VAR_ART grain       │
   │     Write three output tables                        │
   └─────────────┬───────────────────────────────────────┘
                 ▼
        ARS_MSA_TOTAL  +  ARS_MSA_GEN_ART  +  ARS_MSA_VAR_ART
```

---

## Step-by-step breakdown — what happens, how, why

### Step 1 — Filter SLOC

#### What happens

We only keep rows whose SLOC is configured as active in `ARS_STORE_SLOC_SETTINGS`.

#### Logic

A planner controls which SLOCs participate in allocation. A new SLOC
arriving from SAP first has to be "approved" via Store SLOC Validation
(Phase setup). Until then it's `STATUS='INACTIVE'` and gets filtered out.

This prevents random new locations (e.g. testing stockrooms) from
polluting the calculation.

#### Code reference

```python
# msa_service.py
def _filter_active_slocs(self, df):
    active = self.db.execute(text("""
        SELECT SLOC FROM ARS_STORE_SLOC_SETTINGS WHERE UPPER(STATUS) = 'ACTIVE'
    """)).scalars().all()
    return df[df['SLOC'].isin(active)]
```

#### Example

Source has 30 distinct SLOCs. Only 18 are active in settings. After
this step, df rows = source rows × (18/30) = 60% retention.

### Step 2 — Normalize

#### What happens

```python
df['MATNR'] = df['MATNR'].astype(str).str.strip()
df['WERKS'] = df['WERKS'].astype(str).str.strip().str.upper()
df['QTY'] = pd.to_numeric(df['QTY'], errors='coerce').fillna(0)
df['SEG'] = df['SEG'].fillna('NA')
```

#### Why this matters

- `'  DH24 '` and `'DH24'` look the same to a human but PIVOT will
  treat them as two different SLOCs. Trim everything.
- Uppercase keys mean `'V01'` and `'v01'` collapse correctly.
- `pd.to_numeric(errors='coerce')` turns garbage like `'NA'` into NaN,
  then `fillna(0)` converts to a clean numeric.
- Text NULL stays as `'NA'` so it doesn't disappear in groupbys
  (pandas drops NaN keys in groupby by default).

### Step 3 — Fill missing dimensions

#### What happens

JOIN with master to get every dimension column. For misses, fill with
sane defaults so the row is still aggregatable.

```sql
SELECT
    STK.WERKS                                                      AS RDC,
    STK.MATNR,
    ISNULL(MP.MAJ_CAT, 'UNCAT')                                    AS MAJ_CAT,
    ISNULL(TRY_CAST(MP.GEN_ART_NUMBER AS BIGINT), 0)               AS GEN_ART,
    ISNULL(MP.VAR_ART_NUMBER, 'NA')                                AS VAR_ART,
    ISNULL(MP.CLR, 'NA')                                           AS CLR,
    ISNULL(MP.SEG, 'NA')                                           AS SEG,
    STK.SLOC,
    STK.QTY
FROM ET_MSA_STK STK
LEFT JOIN vw_master_product MP ON STK.MATNR = MP.ARTICLE_NUMBER
WHERE STK.SLOC IN (<active_slocs>);
```

#### Why LEFT JOIN, not INNER JOIN

INNER would drop articles missing from the master (e.g. legacy SKUs
that were sold but never re-added to master). LEFT keeps them with
`MAJ_CAT='UNCAT'` so we can see the volume in audit reports.

#### Why ISNULL with `'NA'` strings

A NULL key blows up the pivot. `'NA'` keeps the row alive; the
allocation engine treats `'NA'` MAJ_CAT as a no-op (no listing → no
allocation).

### Step 4 — SEG = [APP, GM]

#### What happens

Drop everything that isn't apparel or general merchandise.

```python
df = df[df['SEG'].isin(['APP', 'GM'])]
```

#### Why

ARS today only allocates these two segments. Other segments (HOME,
FOOD, ETHNIC) have separate replenishment flows. Including them here
creates noise in the output and confuses planners.

#### How to extend

If a new segment becomes ARS-managed, edit `_seg_filter` to include it:
```python
ALLOWED_SEGS = ['APP', 'GM', 'HOME']    # NEW
df = df[df['SEG'].isin(ALLOWED_SEGS)]
```

### Step 5 — Pivot by SLOC

#### What happens

The long-format `(RDC, MATNR, SLOC, QTY)` becomes wide:

```
BEFORE                              AFTER
RDC   MATNR     SLOC  QTY           RDC   MATNR     V01  V02_FRESH  V04  ... STK_TTL
DH24  12345  V01     150           DH24  12345     150  80         0    ... 230
DH24  12345  V02_FRESH 80           DH26  55555     20   0          15   ... 35
DH26  55555  V01     20
DH26  55555  V04     15
```

#### Code

```python
pivoted = df.pivot_table(
    index=['RDC', 'MATNR', 'MAJ_CAT', 'GEN_ART', 'VAR_ART', 'CLR'],
    columns='SLOC',
    values='QTY',
    aggfunc='sum',
    fill_value=0,
).reset_index()
pivoted['STK_TTL'] = pivoted[active_slocs].sum(axis=1)
```

#### Why pivot

Allocation rules are per-store-location decisions. "How much V01 stock?"
is a far easier question with V01 as a column than with SLOC=V01 in
the WHERE clause every time.

#### Performance gotcha

If active_slocs has 30+ entries and df has 50M rows, the pivot needs
a lot of memory. Consider doing the pivot in SQL instead of pandas:

```sql
SELECT RDC, MATNR, MAJ_CAT, GEN_ART, VAR_ART, CLR,
       SUM(CASE WHEN SLOC = 'V01'       THEN QTY ELSE 0 END) AS V01,
       SUM(CASE WHEN SLOC = 'V02_FRESH' THEN QTY ELSE 0 END) AS V02_FRESH,
       ...
       SUM(QTY) AS STK_TTL
FROM filtered_stock
GROUP BY RDC, MATNR, MAJ_CAT, GEN_ART, VAR_ART, CLR;
```

### Step 6 — Merge MASTER_ALC_PEND

#### What happens

LEFT JOIN pending allocations on `(RDC, MATNR)`. If an article has
nothing pending, PEND defaults to 0.

```sql
SELECT pivoted.*, ISNULL(P.PEND_TOTAL, 0) AS PEND
FROM <pivoted_temp> AS pivoted
LEFT JOIN (
    SELECT RDC, MATNR, SUM(QTY) AS PEND_TOTAL
    FROM MASTER_ALC_PEND
    WHERE [DATE] >= DATEADD(day, -7, GETDATE())
    GROUP BY RDC, MATNR
) AS P ON pivoted.RDC = P.RDC AND pivoted.MATNR = P.MATNR;
```

#### Why "last 7 days only"

Older pending allocations are likely stuck (e.g. cancelled but never
cleaned up). Including them inflates PEND and depresses FNL_Q,
starving allocation. 7 days is the SLA the warehouse promises.

### Step 7 — Compute FNL_Q

#### What happens

```python
df['FNL_Q'] = (df['STK_TTL'] - df['PEND']).clip(lower=0)
```

#### Why clip at 0

If PEND > STK_TTL (which can happen during the brief moment after a
new allocation but before stock decreases), we don't want negative
"available" quantity. 0 means "nothing more to allocate from here"
which is safe.

#### Audit log

Per-row negative-clip events are summarised in the audit:
```python
clipped = (df['STK_TTL'] < df['PEND']).sum()
audit.log_data_change(
    table_name='ARS_MSA_INTERNAL', changed_by='msa-job',
    details={'rows_clipped_to_zero': int(clipped)}
)
```

This catches "we have a chronic over-allocation issue" trends.

### Step 8 — Generate color variants

#### What happens

For grids that allocate by colour, every (VAR_ART) needs to expand to
all (VAR_ART, CLR) combinations the master knows about. This step
explodes the rows.

```python
# Before:
# VAR_ART=12345  STK_TTL=100

# Master says VAR_ART 12345 has CLR ∈ {RED, BLUE, BLACK}.

# After:
# VAR_ART=12345 CLR=RED    STK_TTL=<allocated portion of 100>
# VAR_ART=12345 CLR=BLUE   STK_TTL=<...>
# VAR_ART=12345 CLR=BLACK  STK_TTL=<...>
```

#### Logic for the split

If the source has CLR-level data: keep as-is. Otherwise split equally
across known colours, OR use last week's colour-level sales as the split
ratio (more accurate but heavier).

```python
# Equal-split (simple):
for clr in master_colours[var_art]:
    new_rows.append({
        ..., 'CLR': clr,
        'STK_TTL': original_stk / len(master_colours[var_art])
    })

# Sales-weighted (better):
weights = sales_by_clr_last_week[var_art]
for clr, w in weights.items():
    new_rows.append({
        ..., 'CLR': clr,
        'STK_TTL': original_stk * (w / weights.sum())
    })
```

### Step 9 — Aggregate to three grains

#### What happens

```python
# ARS_MSA_VAR_ART — finest grain, just write what we have
arr_msa_var_art = df  # already at this grain

# ARS_MSA_GEN_ART — roll up over VAR_ART + CLR
arr_msa_gen_art = df.groupby(
    ['RDC', 'MAJ_CAT', 'GEN_ART']
).agg({**{s: 'sum' for s in active_slocs},
       'STK_TTL': 'sum', 'PEND': 'sum', 'FNL_Q': 'sum'}).reset_index()

# ARS_MSA_TOTAL — roll up over GEN_ART
arr_msa_total = df.groupby(['RDC', 'MAJ_CAT']).agg(...).reset_index()
```

Then write each via `upsert_engine.upsert(...)` (TRUNCATE + INSERT for
full-refresh semantics).

---

## Example: trigger MSA from CLI

```bash
TOKEN=...

# Use the most-recent saved preset (is_last_used=1)
JOB=$(curl -s -X POST "$API/api/v1/msa-stock/calculate" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "date": "2026-04-30",
    "filters": {
      "ST_CD": ["DH24","DH25","DH26"],
      "SLOC":  ["V01","V02_FRESH","V04"],
      "SEG":   ["APP","GM"]
    },
    "threshold": 1,
    "save_to_db": true
  }' | jq -r .data.job_id)

# Poll
while true; do
  S=$(curl -s -H "Authorization: Bearer $TOKEN" "$API/api/v1/jobs/$JOB" | jq -r .data.status)
  echo "$S"; [ "$S" = "completed" ] || [ "$S" = "failed" ] && break
  sleep 10
done

# Verify outputs
curl -s -H "Authorization: Bearer $TOKEN" "$API/api/v1/tables" \
  | jq '.data[] | select(.table | startswith("ARS_MSA_")) | {table, rows}'
```

Expected output (typical day):
```
{"table":"ARS_MSA_TOTAL",   "rows":1840}     # ~320 stores × ~6 maj_cats
{"table":"ARS_MSA_GEN_ART", "rows":47800}    # × ~26 gen_arts per maj_cat
{"table":"ARS_MSA_VAR_ART", "rows":165500}   # × ~3-4 colour variants
```

---

## What can go wrong

### Failure A — Output table empty

**Symptom:** `ARS_MSA_TOTAL` has 0 rows.

**Likely causes:**
1. SLOC filter too narrow (no active SLOCs match source data).
2. SEG filter dropped everything (source had no APP/GM rows).
3. Master JOIN missed everything (master view stale or down).

**Detect:**
```sql
-- How many source rows survived each step?
SELECT 'raw' AS step, COUNT(*) FROM ET_MSA_STK WHERE [DATE] = '2026-04-30'
UNION ALL
SELECT 'after_sloc', COUNT(*) FROM ET_MSA_STK WHERE [DATE] = '2026-04-30'
  AND SLOC IN (SELECT SLOC FROM ARS_STORE_SLOC_SETTINGS WHERE STATUS='ACTIVE')
UNION ALL
SELECT 'with_master', COUNT(*) FROM ET_MSA_STK STK
  INNER JOIN vw_master_product MP ON STK.MATNR = MP.ARTICLE_NUMBER
  WHERE STK.[DATE] = '2026-04-30'
    AND STK.SLOC IN (SELECT SLOC FROM ARS_STORE_SLOC_SETTINGS WHERE STATUS='ACTIVE')
    AND MP.SEG IN ('APP','GM');
```

The step where row count drops to 0 is your suspect.

### Failure B — FNL_Q is negative for some rows

**Symptom:** Despite the clip, a downstream report shows FNL_Q < 0.

**Likely cause:** Pandas dtype quirk — `STK_TTL - PEND` produced a
float that wasn't compared to 0 before write.

**Fix:** Audit step 7's code; ensure `clip(lower=0)` is the last
operation before write.

### Failure C — Same article appears twice in `ARS_MSA_VAR_ART`

**Symptom:** Composite key `(RDC, MAJ_CAT, GEN_ART, VAR_ART, CLR)` is
duplicated.

**Likely cause:** Step 8's color expansion uses a wrong source. The
master had multiple "active" CLR records for the same VAR_ART (perhaps
due to a SAP data load issue).

**Fix:** Add a dedup at the end of step 8:
```python
df = df.drop_duplicates(subset=['RDC', 'MAJ_CAT', 'GEN_ART', 'VAR_ART', 'CLR'])
```

---

## Performance benchmarks

| Step | Typical time | Volume |
|---|---|---|
| Filter SLOC | <1 s | trivial |
| Normalize | 5-10 s | df-bound |
| Fill dims (JOIN master) | 10-30 s | join over 50M source × 500k master |
| SEG filter | 2-5 s | df-bound |
| Pivot | 30-90 s | wide pivot, memory-heavy |
| Merge PEND | 5-10 s | join with small table |
| FNL_Q compute | <1 s | vectorised |
| Color variants | 5-30 s | depends on master colour count |
| Aggregate + write | 30-60 s | three TRUNCATE + INSERT |
| **Total** | **2-5 min** | typical day |

If Phase 3 takes longer:
1. Source `ET_MSA_STK` doubled in size? Check row counts against history.
2. View `vw_master_product` slow? Re-create it with proper indexes.
3. SLOC count exploded? Each new active SLOC = another pivot column.

---

## When this phase is healthy

- All three output tables refreshed with today's date as their newest
  audit timestamp.
- Row counts within ±10% of yesterday's.
- No "rows_clipped_to_zero" alert above ~5% of total.
- Job duration < 5 minutes.

If green, Phase 4 (Grid Building) can begin.
