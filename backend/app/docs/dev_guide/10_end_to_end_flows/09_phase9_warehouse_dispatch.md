# Phase 9 — Warehouse Dispatch

The decisions are made. Now physical product moves. ARS surfaces the
"what to ship" list; the warehouse executes; ARS tracks completion.

---

## Goal

Hand off approved allocations to warehouse operations as a pick-list,
track shipping progress, and update `ARS_pend_alc` as items leave the
warehouse so future allocations don't double-count them.

## Inputs

- `ARS_ALLOCATION_MASTER` rows where `status='approved'`
- `ARS_ALLOCATION_DETAIL` (the lines)
- `ARS_pend_alc` (live working table)

## Outputs

- Warehouse pick-list (CSV / printable / WMS feed)
- Updates to `ARS_pend_alc` as items ship
- Eventually: `ARS_ALLOCATION_MASTER.status='shipped'`

---

## Flowchart

```
┌────────────────────────────────────────────┐
│  Pending Allocation report                 │
│  (PendAlcReportPage.jsx)                   │
└──────────────────┬─────────────────────────┘
                   │
                   ▼
   ┌─────────────────────────────────────┐
   │  GET /reports/pending-allocation    │
   │  Returns ARS_pend_alc                │
   │  LEFT JOIN vw_master_product         │
   │  with descriptions, brands, etc.     │
   └──────────────────┬───────────────────┘
                      │
                      ▼
   Operations team filters by store / RDC
   Downloads as CSV → feeds to WMS / pick path
                      │
                      ▼
   ┌─────────────────────────────────────┐
   │  Warehouse picks, packs, ships       │
   │  (external system / paper / handheld)│
   └──────────────────┬───────────────────┘
                      │
                      ▼  ship event back to ARS
   POST /allocations/{code}/ship
       { lines: [{store, article, shipped_qty}, ...] }
                      │
                      ▼
   For each line:
     UPDATE ARS_pend_alc
        SET shipped_qty = shipped_qty + :s,
            QTY = QTY - :s
      WHERE allocation_code = :c AND ST_CD = :st AND MATNR = :a

     IF QTY = 0:
        DELETE FROM ARS_pend_alc WHERE ...

     INSERT INTO ARS_ALLOCATION_AUDIT (... action='SHIPPED' ...)
                      │
                      ▼
   When all lines shipped:
     UPDATE ARS_ALLOCATION_MASTER
        SET status='shipped', shipped_at=GETDATE()
```

---

## Step-by-step

### Step 1 — Operations pulls the list

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "$API/api/v1/reports/pending-allocation/download?WERKS=DH24&format=csv" \
  -o pick_DH24.csv
```

Output schema (typical):

```
RDC,ST_CD,MATNR,QTY,MAJ_CAT,DIV,SEG,GEN_ART_NUMBER,CLR,DESCRIPTION,BRAND
DH24,S001,12345678,5,MENS_APP,APP,APP,1234567,RED,Polo T-Shirt,Brand X
DH24,S001,55555555,3,MENS_APP,APP,APP,5555555,BLUE,Cargo Pants,Brand Y
...
```

This is what gets imported into the WMS or printed for hand picking.

### Step 2 — Warehouse picks

External to ARS — happens in the WMS.

### Step 3 — Ship event back to ARS

When a truck departs (or per-line as items are scanned):

```bash
curl -X POST "$API/api/v1/allocations/ALC_.../ship" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '[
    {"store_code":"DH24","article_number":"12345678","shipped_qty": 5},
    {"store_code":"DH24","article_number":"55555555","shipped_qty": 3}
  ]'
```

### Step 4 — pend_alc update logic

```python
@router.post("/allocations/{code}/ship")
def ship(code, lines, db = Depends(get_data_db),
         current_user = Depends(get_current_user)):
    with db.begin():
        for line in lines:
            # Decrement pending qty
            result = db.execute(text("""
                UPDATE ARS_pend_alc
                SET shipped_qty = ISNULL(shipped_qty, 0) + :sq,
                    QTY = QTY - :sq
                WHERE allocation_code = :c
                  AND ST_CD = :s
                  AND MATNR = :a
            """), {'c': code, 's': line.store_code, 'a': line.article_number,
                   'sq': line.shipped_qty})

            # Audit
            db.execute(text("""
                INSERT INTO ARS_ALLOCATION_AUDIT
                (allocation_code, action, store_code, article_number, shipped_qty, by, at)
                VALUES (:c, 'SHIPPED', :s, :a, :sq, :u, GETDATE())
            """), {...})

        # If all lines fully shipped, mark allocation shipped
        any_pending = db.execute(text("""
            SELECT COUNT(*) FROM ARS_pend_alc WHERE allocation_code = :c AND QTY > 0
        """), {'c': code}).scalar()
        if any_pending == 0:
            db.execute(text("""
                UPDATE ARS_ALLOCATION_MASTER
                SET status='shipped', shipped_at=GETDATE()
                WHERE allocation_code=:c
            """), {'c': code})
```

### Step 5 — Cleanup

`ARS_pend_alc` rows with `QTY = 0` should be archived periodically:

```sql
INSERT INTO ARS_pend_alc_history
SELECT * FROM ARS_pend_alc WHERE QTY = 0 AND shipped_qty > 0;

DELETE FROM ARS_pend_alc WHERE QTY = 0 AND shipped_qty > 0;
```

Run nightly or on a schedule.

---

## Pending Allocation report — performance considerations

Ops team pulls this report all day. Index well:

```sql
CREATE NONCLUSTERED INDEX IX_pend_alc_RDC_ST_MATNR
  ON ARS_pend_alc(RDC, ST_CD, MATNR);

CREATE NONCLUSTERED INDEX IX_pend_alc_status
  ON ARS_pend_alc(allocation_code)
  INCLUDE (RDC, ST_CD, MATNR, QTY);
```

Default display caps at 2000 rows for browser responsiveness. Power
users export full CSV for >2000 row scenarios.

---

## Examples

### Aggregate today's shipping volume

```sql
SELECT RDC,
       COUNT(DISTINCT allocation_code) AS allocations,
       SUM(QTY) AS units_remaining,
       SUM(shipped_qty) AS units_shipped_today
FROM ARS_pend_alc
WHERE created_at >= CAST(GETDATE() AS DATE)
GROUP BY RDC
ORDER BY units_shipped_today DESC;
```

### Find stuck allocations (approved but not shipped after N days)

```sql
SELECT M.allocation_code, M.approved_at,
       DATEDIFF(day, M.approved_at, GETDATE()) AS age_days,
       SUM(D.qty) AS units_pending
FROM ARS_ALLOCATION_MASTER M
JOIN ARS_ALLOCATION_DETAIL D ON M.allocation_code = D.allocation_code
WHERE M.status = 'approved'
  AND M.approved_at < DATEADD(day, -3, GETDATE())
GROUP BY M.allocation_code, M.approved_at
ORDER BY age_days DESC;
```

If the warehouse hasn't shipped after 3 days, escalate — capacity
issue or allocation was unrealistic.

---

## What can go wrong

### Failure A — Ship event arrives twice

Network retry → same ship event posted twice → pend_alc decremented
twice → goes negative.

**Fix:** Make the endpoint idempotent. Each ship event has a
`shipment_id`; the endpoint checks if already processed:

```python
existing = SELECT 1 FROM ARS_SHIPMENT_LOG WHERE shipment_id = :id
if existing: return APIResponse(success=True, message="already processed")
```

### Failure B — `pend_alc` shows 0 but allocation status still 'approved'

Mismatch between physical reality and ARS state. Run reconciliation:

```sql
SELECT M.allocation_code
FROM ARS_ALLOCATION_MASTER M
WHERE M.status = 'approved'
  AND NOT EXISTS (SELECT 1 FROM ARS_pend_alc P
                  WHERE P.allocation_code = M.allocation_code AND P.QTY > 0);

-- Manually mark each shipped
UPDATE ARS_ALLOCATION_MASTER SET status='shipped', shipped_at=GETDATE()
WHERE allocation_code IN (...);
```

### Failure C — Reports show yesterday's data

`ARS_pend_alc` not refreshing? Check:

```sql
SELECT MAX(created_at) FROM ARS_pend_alc;
SELECT MAX(updated_at) FROM ARS_pend_alc;
```

If both old, the engine isn't writing on new allocations. Phase 7 bug.

---

## Performance benchmarks

| Operation | Time |
|---|---|
| Pending Allocation page load (default) | <500 ms |
| Filter & re-render (client-side) | instant |
| Download CSV (10k rows) | 1-2 s |
| Ship event POST | <500 ms per call |

---

## When this phase is healthy

- Operations team finds the pick list within 30 sec of approval.
- Ship events flow through the API without errors.
- `ARS_pend_alc` count decreases through the day; goes to ~0 by end of day.
- Allocations move from `approved` → `shipped` within SLA.
- Audit trail is complete.
