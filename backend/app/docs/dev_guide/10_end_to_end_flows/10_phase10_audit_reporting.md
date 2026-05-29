# Phase 10 — Audit & Reporting

The day's flow is done. Phase 10 isn't a separate workflow — it runs
**continuously through every other phase**. This page documents what
gets captured and how to query it.

---

## Goal

Provide a complete, queryable record of every change to ARS data,
every allocation decision, every override, and every system action —
for compliance, troubleshooting, and trend analysis.

## Inputs

Audit rows produced by every other phase. Phase 10 is a consumer, not
a producer.

## Outputs

- Daily audit summary
- Trend dashboards on activity
- Compliance-grade history of every decision

---

## Where audit happens

```
Phase 1 (Ingestion)
   ↓ writes audit_log row per upload (BULK_UPLOAD action)

Phase 2 (Validation)
   ↓ writes checklist_results rows per rule

Phase 3 (MSA)
   ↓ writes audit_log summary (action='MSA_RUN')

Phase 4 (Grids)
   ↓ writes ARS_GRID_BUILDER updates (last_run_*) + audit_log

Phase 5 (Listing)
   ↓ audit_log (action='LISTING_GENERATE')

Phase 6 (Contribution)
   ↓ ARS_CONTRIB_JOBS row + audit_log

Phase 7 (Allocation)
   ↓ ARS_ALLOCATION_MASTER + DETAIL + AUDIT (action='CREATED')

Phase 8 (Override)
   ↓ ARS_ALLOCATION_AUDIT (action='OVERRIDE' / 'APPROVED')

Phase 9 (Dispatch)
   ↓ ARS_ALLOCATION_AUDIT (action='SHIPPED')
```

---

## The two main audit tables

### `audit_log` (system DB)

Every data write across the whole system.

```sql
audit_log:
  id                 BIGINT IDENTITY PK
  table_name         NVARCHAR(200)
  action_type        NVARCHAR(50)    -- INSERT|UPDATE|DELETE|BULK_UPLOAD|DDL_*|MSA_RUN|...
  record_primary_key NVARCHAR(500)   -- "store=DH24|article=12345"
  old_data           NVARCHAR(MAX)   -- JSON
  new_data           NVARCHAR(MAX)   -- JSON
  changed_columns    NVARCHAR(MAX)   -- JSON ["QTY","STATUS"]
  changed_by         NVARCHAR(100)
  source             NVARCHAR(50)    -- UI|UPLOAD|API|RFC|JOB
  batch_id           NVARCHAR(50)    -- group rows from same operation
  ip_address         NVARCHAR(50)
  user_agent         NVARCHAR(500)
  duration_ms        INT
  notes              NVARCHAR(MAX)
  created_at         DATETIME2 DEFAULT SYSUTCDATETIME()

  INDEX IX_audit_log_table_batch (table_name, batch_id, created_at)
  INDEX IX_audit_log_user_at (changed_by, created_at)
  INDEX IX_audit_log_pk (table_name, record_primary_key)
```

### `ARS_ALLOCATION_AUDIT` (data DB)

Allocation-specific trail at finer grain than audit_log.

```sql
ARS_ALLOCATION_AUDIT:
  id              BIGINT IDENTITY PK
  allocation_code NVARCHAR(50)
  action          NVARCHAR(20)   -- CREATED|OVERRIDE|APPROVED|REJECTED|SHIPPED|CANCELLED
  store_code      NVARCHAR(20)
  article_number  NVARCHAR(50)
  old_qty         FLOAT
  new_qty         FLOAT
  reason          NVARCHAR(500)
  by              NVARCHAR(100)
  at              DATETIME2 DEFAULT SYSUTCDATETIME()
  details         NVARCHAR(MAX)  -- JSON for header-level events
```

---

## Useful queries

### "Who changed this row, and when?"

```sql
SELECT created_at, changed_by, action_type,
       changed_columns,
       JSON_VALUE(old_data, '$.QTY') AS old_qty,
       JSON_VALUE(new_data, '$.QTY') AS new_qty,
       source, ip_address
FROM audit_log
WHERE table_name = 'ARS_ALLOCATION_DETAIL'
  AND record_primary_key LIKE '%store=DH24|article=12345%'
ORDER BY created_at DESC;
```

### "What did Santosh do today?"

```sql
SELECT created_at, table_name, action_type,
       SUBSTRING(record_primary_key, 1, 80) AS pk,
       source
FROM audit_log
WHERE changed_by = 'santosh'
  AND created_at >= CAST(GETDATE() AS DATE)
ORDER BY created_at;
```

### "How many allocations were overridden last week?"

```sql
SELECT CAST(at AS DATE) AS day,
       COUNT(DISTINCT allocation_code) AS allocs_with_override,
       COUNT(*) AS override_count
FROM ARS_ALLOCATION_AUDIT
WHERE action = 'OVERRIDE'
  AND at >= DATEADD(day, -7, GETDATE())
GROUP BY CAST(at AS DATE)
ORDER BY day;
```

### "Which articles get overridden most often?" (signals engine quality)

```sql
SELECT article_number,
       COUNT(*) AS override_count,
       AVG(ABS(new_qty - old_qty)) AS avg_change_magnitude
FROM ARS_ALLOCATION_AUDIT
WHERE action = 'OVERRIDE'
  AND at >= DATEADD(day, -30, GETDATE())
GROUP BY article_number
ORDER BY override_count DESC;
```

If a few articles dominate the list, their allocation rule needs work.

### "Reconstruct an allocation's complete history"

```sql
SELECT at, action, store_code, article_number,
       old_qty, new_qty, reason, by
FROM ARS_ALLOCATION_AUDIT
WHERE allocation_code = 'ALC_2026_04_30_GRADE1'
ORDER BY at;
```

Reads like a story:
- 09:42 CREATED (system, 1234 lines)
- 10:15 OVERRIDE store=DH24 article=12345 5→8 reason="Local promo"
- 10:18 OVERRIDE store=DH27 article=55555 0→2 reason="Soft launch"
- 10:30 APPROVED by="santosh"
- 14:22 SHIPPED store=DH24 article=12345 qty=8

---

## Daily summaries (build a small dashboard)

```sql
-- Today's activity
SELECT
    (SELECT COUNT(*) FROM upload_jobs
       WHERE status='completed' AND completed_at >= CAST(GETDATE() AS DATE)) AS uploads_today,
    (SELECT COUNT(*) FROM ARS_ALLOCATION_MASTER
       WHERE created_at >= CAST(GETDATE() AS DATE)) AS allocations_today,
    (SELECT SUM(qty) FROM ARS_ALLOCATION_DETAIL D
       JOIN ARS_ALLOCATION_MASTER M ON D.allocation_code = M.allocation_code
      WHERE M.created_at >= CAST(GETDATE() AS DATE)) AS units_allocated,
    (SELECT COUNT(*) FROM ARS_ALLOCATION_AUDIT
      WHERE action='OVERRIDE' AND at >= CAST(GETDATE() AS DATE)) AS overrides_today,
    (SELECT COUNT(*) FROM ARS_ALLOCATION_MASTER
      WHERE status='shipped' AND shipped_at >= CAST(GETDATE() AS DATE)) AS allocs_shipped_today;
```

Wire this into a Trends dashboard or a daily Slack post.

---

## Compliance / forensic queries

### "Did this user have permission for this action?"

```sql
SELECT u.username,
       p.code AS permission_code,
       a.action_type, a.created_at
FROM audit_log a
JOIN rbac_users u ON a.changed_by = u.username
JOIN rbac_user_roles ur ON u.id = ur.user_id
JOIN rbac_role_permissions rp ON ur.role_id = rp.role_id
JOIN rbac_permissions p ON rp.permission_id = p.id
WHERE a.id = :audit_id;
```

### "Show every change to QTY in the last 24h that wasn't done from UI"

```sql
SELECT created_at, changed_by, source, ip_address,
       record_primary_key,
       JSON_VALUE(old_data, '$.QTY') AS old_qty,
       JSON_VALUE(new_data, '$.QTY') AS new_qty
FROM audit_log
WHERE table_name LIKE 'ARS_ALLOCATION_%'
  AND changed_columns LIKE '%QTY%'
  AND created_at > DATEADD(hour, -24, GETDATE())
  AND source NOT IN ('UI')
ORDER BY created_at DESC;
```

Catches direct API hits or back-office adjustments that bypassed the UI.

---

## Archive strategy

`audit_log` grows fast — millions of rows in weeks. Keep it lean:

```sql
-- Move rows older than 6 months to archive
INSERT INTO audit_log_history
SELECT * FROM audit_log
WHERE created_at < DATEADD(month, -6, GETDATE());

DELETE FROM audit_log
WHERE created_at < DATEADD(month, -6, GETDATE());
```

Same for `ARS_ALLOCATION_AUDIT`. Schedule monthly.

---

## Trend dashboard usage

The Trends section (`/trends/dashboard`) can plot any audit table:

- Daily upload count (uploads_today over 30 days)
- Override rate trend (overrides per allocation over time)
- User activity (changed_by counts)

Pre-aggregate into `Trend_Audit_Daily` for fast queries:

```sql
CREATE TABLE Trend_Audit_Daily (
  REPORT_DATE DATE,
  metric NVARCHAR(50),    -- 'upload_count', 'allocation_count', 'override_count', ...
  WERKS NVARCHAR(20) NULL,
  value FLOAT,
  PRIMARY KEY (REPORT_DATE, metric, WERKS)
);

-- Refresh nightly:
INSERT INTO Trend_Audit_Daily SELECT
  CAST(created_at AS DATE), 'upload_count', NULL, COUNT(*)
FROM audit_log
WHERE action_type = 'BULK_UPLOAD' AND created_at >= DATEADD(day, -1, GETDATE())
GROUP BY CAST(created_at AS DATE);
```

---

## What can go wrong

### Failure A — Audit row missing

For every write, an audit row should exist. If you suspect missing:

```sql
-- Find allocation_codes with no AUDIT row
SELECT M.allocation_code
FROM ARS_ALLOCATION_MASTER M
LEFT JOIN ARS_ALLOCATION_AUDIT A
       ON A.allocation_code = M.allocation_code
      AND A.action = 'CREATED'
WHERE A.id IS NULL
  AND M.created_at > DATEADD(day, -7, GETDATE());
```

If non-empty: a code path is bypassing the audit insert. Find it and
fix it. The audit is the system of record.

### Failure B — `audit_log` slow to query

Almost certainly missing indexes. The minimum set:
- `(table_name, batch_id, created_at)`
- `(changed_by, created_at)`
- `(table_name, record_primary_key)`

### Failure C — Disk filling up from `audit_log`

Apply the archive strategy above. If the table is still huge after
archiving 6 months, your write rate is higher than expected — investigate
which `source` is producing the most rows.

---

## Performance

`audit_log` writes are typically **sub-millisecond** per row when the
indexes are right. The summary writes from `_bulk_upsert` are async
batches so they don't block the main upsert.

If audit writes ever appear in your slow-query top-N, the indexes are
fragmented. Rebuild them.

---

## When this phase is healthy

- Every `ARS_ALLOCATION_MASTER` row has a `CREATED` audit row.
- Daily audit-summary numbers match expectations:
  - 5-10 uploads
  - 50-200 allocations
  - 5-15% override rate
  - 80%+ approved-then-shipped rate
- `audit_log` size grows linearly, not explosively.
- Archive job runs successfully each month.

This is the closing phase of the daily flow. Tomorrow's day starts
with Phase 1 again.
