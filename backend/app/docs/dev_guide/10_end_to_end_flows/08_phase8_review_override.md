# Phase 8 — Review & Override

The engine produced numbers. Now a planner inspects them, overrides
outliers, and approves the result. The audit trail captures every
change.

---

## Goal

Surface any algorithmic decision that needs human judgment, capture
overrides with full audit, and approve or reject the allocation as
ready for warehouse pickup.

## Inputs

- `ARS_ALLOCATION_MASTER` (header) with `status='pending'`
- `ARS_ALLOCATION_DETAIL` (lines)
- Planner's domain knowledge (regional events, planned promotions, etc.)

## Outputs

- Updated `ARS_ALLOCATION_DETAIL` with corrected quantities
- New `ARS_ALLOCATION_AUDIT` rows for every change
- `ARS_ALLOCATION_MASTER.status` advanced to `approved` or `rejected`

---

## Flowchart

```
┌──────────────────────────────────────────┐
│  Allocation Detail page                   │
│  /allocations/<allocation_code>           │
└─────────────────┬─────────────────────────┘
                  │
                  ▼
   ┌──────────────────────────────────────┐
   │  Show allocation lines + filters     │
   │  Highlight rows with red:             │
   │   - qty == 0 for high-rank stores    │
   │   - qty > 95th percentile             │
   │   - over-allocation (qty > expected)  │
   └─────────────────┬────────────────────┘
                     │
                     ▼
   Planner edits a cell:
       new_qty for store X article Y
                     │
                     ▼
   POST /allocations/{code}/override
       { store_code, article_number, qty, reason }
                     │
                     ▼
   ┌──────────────────────────────────────┐
   │  allocation_engine.apply_override:    │
   │   1. Read current detail row          │
   │   2. UPDATE ARS_ALLOCATION_DETAIL     │
   │   3. INSERT ARS_ALLOCATION_AUDIT      │
   │   4. Adjust ARS_pend_alc accordingly  │
   └─────────────────┬────────────────────┘
                     │
                     ▼ (eventually)
   Planner clicks Approve:
       POST /allocations/{code}/approve
                     │
                     ▼
   UPDATE ARS_ALLOCATION_MASTER
   SET status='approved',
       approved_by=:user, approved_at=GETDATE();
                     │
                     ▼
   Operations team can now pull from
   Pending Allocation report (Phase 9)
```

---

## Step-by-step

### Step 1 — Review

Allocation detail page loads:

```sql
SELECT D.store_code, D.article_number, D.var_art, D.clr, D.qty,
       P.OPT_CNT AS desired,
       (D.qty - P.OPT_CNT) AS variance,
       L.RANK
FROM ARS_ALLOCATION_DETAIL D
LEFT JOIN PER_OPT_SALE P ON D.store_code = P.WERKS AND D.article_number = P.GEN_ART
LEFT JOIN ARS_LISTING_MASTER L ON D.store_code = L.WERKS AND D.article_number = L.GEN_ART
WHERE D.allocation_code = :code
ORDER BY ABS(D.qty - P.OPT_CNT) DESC;   -- biggest variances first
```

Variance highlights help planner focus on the rows worth checking
(out of potentially thousands).

### Step 2 — Override

User changes a cell. Frontend collects pending edits and on Save:

```bash
curl -X POST "$API/api/v1/allocations/ALC_.../override" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '[
    { "store_code":"DH24", "article_number":"1234567", "qty": 5, "reason": "Local promo planned" },
    { "store_code":"DH25", "article_number":"1234568", "qty": 0, "reason": "Inventory damage" }
  ]'
```

Backend processes each:

```python
for ovr in payload:
    old_qty = SELECT qty FROM ARS_ALLOCATION_DETAIL
              WHERE allocation_code = :c
                AND store_code = :s AND article_number = :a

    UPDATE ARS_ALLOCATION_DETAIL
    SET qty = :new_qty, updated_at = GETDATE()
    WHERE allocation_code = :c
      AND store_code = :s AND article_number = :a

    INSERT INTO ARS_ALLOCATION_AUDIT (
        allocation_code, action, store_code, article_number,
        old_qty, new_qty, reason, by, at
    ) VALUES (:c, 'OVERRIDE', :s, :a, :old, :new, :reason, :user, GETDATE())

    # Sync pend_alc to match
    UPDATE ARS_pend_alc SET QTY = :new_qty
    WHERE allocation_code = :c
      AND ST_CD = :s AND MATNR = :a
```

### Step 3 — Approve

```bash
curl -X POST "$API/api/v1/allocations/ALC_.../approve" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"notes": "Reviewed by Santosh"}'
```

```sql
UPDATE ARS_ALLOCATION_MASTER
SET status='approved', approved_by=:user, approved_at=GETDATE(),
    approval_notes=:notes
WHERE allocation_code=:c;

INSERT INTO ARS_ALLOCATION_AUDIT (allocation_code, action, by, at, details)
VALUES (:c, 'APPROVED', :user, GETDATE(), :notes);
```

---

## Override patterns common in retail

| Reason | Example |
|---|---|
| Local promo | "Buy-1-get-1 launching at DH24 Friday — bump qty 50%" |
| Damage / shrinkage | "DH25 has 30% loss rate — reduce 30%" |
| Stock-room space | "DH27 has no backroom — cap at 6 units" |
| Sister store | "DH28 doing soft launch this weekend — defer" |
| New launch curiosity | "Ship 1 to every store regardless of rank — see what sells" |

Build patterns into the engine over time. Each override that's "the
same kind" is a hint that a rule should be codified.

---

## Auditability

Every override row captures:

- `allocation_code` — which allocation
- `action` — `OVERRIDE` / `APPROVED` / `REJECTED` / `CANCELLED`
- `store_code, article_number` — what
- `old_qty, new_qty` — before / after
- `reason` — why (free text — encourage filling it in)
- `by, at` — who, when

```sql
-- All overrides for an allocation
SELECT store_code, article_number, old_qty, new_qty, reason, by, at
FROM ARS_ALLOCATION_AUDIT
WHERE allocation_code = 'ALC_...'
  AND action = 'OVERRIDE'
ORDER BY at;
```

Compliance / audit can reconstruct exactly who decided what.

---

## Examples

### Find allocations that needed lots of overrides (engine quality signal)

```sql
SELECT M.allocation_code,
       M.created_at,
       (SELECT COUNT(*) FROM ARS_ALLOCATION_AUDIT A
         WHERE A.allocation_code = M.allocation_code AND A.action = 'OVERRIDE') AS override_count,
       (SELECT COUNT(*) FROM ARS_ALLOCATION_DETAIL D
         WHERE D.allocation_code = M.allocation_code) AS line_count,
       CAST(override_count * 100.0 / NULLIF(line_count, 0) AS DECIMAL(5,1)) AS pct
FROM ARS_ALLOCATION_MASTER M
WHERE M.created_at > DATEADD(day, -30, GETDATE())
ORDER BY pct DESC;
```

If a particular type of allocation consistently has >20% override rate,
the engine logic is wrong — fix the rule rather than relying on humans.

---

## What can go wrong

### Failure A — Override didn't save

**Detect:**
```sql
SELECT * FROM ARS_ALLOCATION_AUDIT
WHERE allocation_code = 'ALC_...'
  AND action = 'OVERRIDE'
ORDER BY at DESC;
```

If your edit doesn't appear in the audit, it didn't commit. Look at
backend logs for the override endpoint.

### Failure B — Approve fails with permission error

The approve endpoint requires `ALLOC_APPROVE`. Verify the user's role
includes it.

### Failure C — Pending allocation table out of sync

If override updates DETAIL but not pend_alc, the warehouse sees the
old qty. The override service must update both atomically (or use a
trigger).

---

## When this phase is healthy

- Allocation `status='approved'` within 30 minutes of run.
- Override-rate per allocation under 10% (high override rate signals
  engine issues — escalate).
- Every override has a reason filled in.

Phase 9 — Warehouse Dispatch — kicks off.
