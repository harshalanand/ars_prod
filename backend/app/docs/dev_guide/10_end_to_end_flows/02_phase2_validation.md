# Phase 2 — Pre-Process Validation (Data Checklist)

Phase 1 brought the data in. Phase 2 verifies it's actually usable
before the heavy calculations start. Five minutes of checking saves
hours of debugging garbage output.

---

## Goal

Confirm every table that downstream phases will read is fresh, complete,
internally consistent, and free of structural errors. Produce a green
"all clear" before allowing Phase 3 to start.

## Inputs

- Phase 1 successfully completed (all sync jobs are `completed`).
- `ARS_CHECKLIST` table populated with current rule definitions.

## Outputs

- A row in the checklist results table per (table, rule) with status:
  - 🟢 **Pass** — all good
  - 🟡 **Warn** — minor anomaly, can proceed
  - 🔴 **Fail** — blocking, fix before continuing
- A consolidated dashboard view: "X of Y tables are healthy."

---

## Flowchart

```
┌──────────────────────────────────────────────────────────────────┐
│           Data Checklist Page (frontend/DataChecklistPage.jsx)    │
└─────────────────────────────┬────────────────────────────────────┘
                              │
                              │ user clicks "Run Checklist"
                              │ (or scheduled cron at 06:30)
                              ▼
              POST /api/v1/checklist/run
                              │
                              ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  checklist_service.run_all_rules()                            │
   │                                                                 │
   │  For each rule in ARS_CHECKLIST:                                │
   │     ┌────────────────────────────────────────┐                │
   │     │  Rule type:                             │                │
   │     │   - freshness   (max_date >= today)     │                │
   │     │   - row_count   (count > min_threshold) │                │
   │     │   - not_null    (no nulls in PK cols)   │                │
   │     │   - referential (FK targets exist)      │                │
   │     │   - value_check (custom SQL must = X)   │                │
   │     └────────────────────────────────────────┘                │
   │                                                                 │
   │  Run rule's SQL → compare to expected → record result           │
   │  Insert one row in checklist_results                            │
   └──────────────────────────────────────────────────────────────┘
                              │
                              ▼
            Frontend polls / refresh shows the matrix:
            ┌─────────────────────┬───────┬───────┬───────┐
            │ Table               │ Fresh │ Count │ Refs  │
            ├─────────────────────┼───────┼───────┼───────┤
            │ ET_STORE_STOCK      │  🟢   │  🟢   │  🟢   │
            │ ET_MSA_STK          │  🟢   │  🟢   │  🟢   │
            │ MASTER_ALC_PEND     │  🟢   │  🟢   │  🟡   │
            │ Trend_Sales_Daily   │  🔴   │  🟢   │  🟢   │
            └─────────────────────┴───────┴───────┴───────┘
```

---

## Step-by-step breakdown

### Step 1 — Discover rules to run

#### What happens

Backend queries `ARS_CHECKLIST` for every active rule.

#### Where in code

- `app/services/checklist_service.py` → `_load_active_rules()`

#### Example: rule schema

```sql
SELECT * FROM ARS_CHECKLIST WHERE active = 1 ORDER BY display_order;
-- id  table_name        rule_type    rule_sql                                 expected
--  1  ET_STORE_STOCK    freshness    SELECT MAX([DATE]) FROM ET_STORE_STOCK   >= today
--  2  ET_STORE_STOCK    row_count    SELECT COUNT(*) FROM ET_STORE_STOCK      > 40000000
--  3  ET_MSA_STK        freshness    SELECT MAX([DATE]) FROM ET_MSA_STK       >= today
--  4  MASTER_ALC_PEND   freshness    SELECT MAX([DATE]) FROM MASTER_ALC_PEND  >= today-1
--  5  Trend_Sales_Daily freshness    SELECT MAX(REPORT_DATE) ...               >= today-1
--  6  vw_master_product not_null     SELECT COUNT(*) ... WHERE MAJ_CAT IS NULL = 0
```

### Step 2 — Run each rule

#### What happens

For each rule, the service runs the `rule_sql`, captures the result,
and compares to `expected`.

#### Where in code

`checklist_service.run_rule(rule)`:

```python
def run_rule(rule):
    actual = self.db.execute(text(rule.rule_sql)).scalar()
    if rule.rule_type == 'freshness':
        expected_date = parse_expected(rule.expected, today=date.today())
        passed = actual >= expected_date
    elif rule.rule_type == 'row_count':
        threshold = parse_expected(rule.expected)
        passed = compare(actual, threshold)
    elif rule.rule_type == 'not_null':
        passed = actual == 0
    ...
    severity = 'pass' if passed else rule.failure_severity   # 'warn' or 'fail'
    return {
        'rule_id': rule.id,
        'table_name': rule.table_name,
        'rule_type': rule.rule_type,
        'actual_value': actual,
        'expected': rule.expected,
        'severity': severity,
        'message': rule.message_template.format(actual=actual, expected=rule.expected),
        'run_at': datetime.utcnow(),
    }
```

#### Example: a freshness check executing

For rule 1 above (`ET_STORE_STOCK freshness`):

```sql
SELECT MAX([DATE]) FROM ET_STORE_STOCK;   -- returns 2026-04-30
```

Today is `2026-04-30`. `actual >= today` → 🟢 Pass.

If yesterday's pipeline failed and the answer is `2026-04-29`:
- Severity = `fail`
- Message = "ET_STORE_STOCK is 1 day stale (max date = 2026-04-29)"
- The dashboard shows red and Phase 3 should not proceed.

### Step 3 — Persist results

#### What happens

All rule results are bulk-inserted into `checklist_results`. Old runs
are kept for trending.

#### Schema

```sql
CREATE TABLE checklist_results (
    id BIGINT IDENTITY PRIMARY KEY,
    run_id NVARCHAR(20),                  -- one ID per checklist run
    rule_id INT,                          -- FK to ARS_CHECKLIST
    table_name NVARCHAR(200),
    rule_type NVARCHAR(50),
    actual_value NVARCHAR(MAX),
    expected NVARCHAR(MAX),
    severity NVARCHAR(20),                -- 'pass' | 'warn' | 'fail'
    message NVARCHAR(MAX),
    run_at DATETIME2 DEFAULT SYSUTCDATETIME()
);
CREATE INDEX IX_checklist_results_run_table
  ON checklist_results(run_id, table_name);
```

### Step 4 — Frontend renders the matrix

#### What happens

`DataChecklistPage.jsx` polls `GET /api/v1/checklist/last-run` every
10s while a run is in progress, then locks to a static view when
status is `completed`.

#### What the user sees

Per-table summary card:

```
┌──────────────────────────────────────┐
│  ET_STORE_STOCK              🟢 PASS │
│                                       │
│  Freshness    🟢  max date = today    │
│  Row count    🟢  47,830,221 rows     │
│  Refs         🟢  all in master       │
│                                       │
│  Last run: 06:31:24 (8.2 s)           │
└──────────────────────────────────────┘
```

A red card stops all "proceed" actions in the UI:

```
┌──────────────────────────────────────────────────────┐
│  Trend_Sales_Daily                          🔴 FAIL  │
│                                                       │
│  Freshness    🔴  max date = 2026-04-28 (2 days old) │
│  Row count    🟢  4,510,221 rows                     │
│                                                       │
│  → Cannot proceed to Contribution % until resolved   │
│  → Re-run Trend Upload for 2026-04-29 and 04-30      │
└──────────────────────────────────────────────────────┘
```

---

## Rule types in detail

### Type: `freshness`

Verifies a date column is at least as recent as `expected`.

```sql
-- Rule: max(DATE) must be today
rule_sql: SELECT CAST(MAX([DATE]) AS DATE) FROM ET_STORE_STOCK
expected: today
```

`today`, `today-1`, `today-7` are special tokens parsed by
`parse_expected`. Anything else is interpreted as a literal date.

### Type: `row_count`

Verifies row count meets a threshold.

```sql
rule_sql: SELECT COUNT_BIG(*) FROM ET_STORE_STOCK WHERE [DATE] = CAST(GETDATE() AS DATE)
expected: > 40000000
```

`expected` supports `>`, `<`, `=`, `>=`, `<=`, `between A and B`.

### Type: `not_null`

Counts rows that violate a NOT-NULL invariant.

```sql
rule_sql: SELECT COUNT(*) FROM vw_master_product WHERE MAJ_CAT IS NULL OR MAJ_CAT = ''
expected: = 0
```

If actual > 0 → fail. The message points to a follow-up query.

### Type: `referential`

Verifies foreign-key-like relationships (without FK constraints).

```sql
rule_sql: |
  SELECT COUNT(*) FROM ET_STORE_STOCK STK
  LEFT JOIN vw_master_product MP ON STK.MATNR = MP.ARTICLE_NUMBER
  WHERE MP.ARTICLE_NUMBER IS NULL
expected: = 0
```

### Type: `value_check`

Free-form SQL that must return a single value matching the expected.

```sql
rule_sql: |
  SELECT SUM(QTY)
  FROM MASTER_ALC_PEND
  WHERE QTY < 0
expected: = 0     -- no negative pending allocations
```

---

## Common changes — recipes

### Recipe: add a custom rule for a new table

```sql
INSERT INTO ARS_CHECKLIST (
    table_name, rule_type, rule_sql, expected,
    failure_severity, message_template, display_order, active
) VALUES (
    'ARS_LISTING_MASTER',
    'freshness',
    'SELECT MAX(updated_at) FROM ARS_LISTING_MASTER',
    'today',
    'fail',
    'Listing master is stale (last updated {actual}, expected {expected})',
    50,
    1
);
```

Run the checklist; the new rule appears.

### Recipe: incremental checklist (don't rescan everything)

Add `last_pass_at` column on `ARS_CHECKLIST`. Only re-run a rule if
the underlying table's `modify_date` (from `sys.tables`) is newer than
`last_pass_at`.

```python
def should_run(rule):
    last_pass = rule.last_pass_at
    table_modified = SELECT modify_date FROM sys.tables WHERE name = :t
    return last_pass is None or table_modified > last_pass
```

For very heavy validations, this saves >50% of runtime.

### Recipe: severity escalation by streak

A rule that has been red for 3 days in a row is more urgent than one
that just turned red. Track the streak in `checklist_streaks`:

```sql
CREATE TABLE checklist_streaks (
    rule_id INT PRIMARY KEY,
    consecutive_failures INT DEFAULT 0,
    last_severity NVARCHAR(20),
    last_run_at DATETIME2
);
```

On each run: increment `consecutive_failures` if still failing, reset
to 0 on pass. Surface a "🚨 3rd day failing" banner in the UI.

---

## What can go wrong

### Failure A — Checklist itself errors out

**Symptom:** The page shows "checklist run failed" and no table
results appear.

**Likely cause:** A malformed `rule_sql` raises a SQL error. The
service's outer try/except catches it but the run aborts.

**Fix:** Look at backend logs for the offending SQL. Test it manually
in SSMS. Mark the rule `active=0` until fixed.

### Failure B — All rules pass but Phase 3 still fails

**Symptom:** Checklist green, MSA returns garbage.

**Likely cause:** A rule that *should* exist doesn't. Examples we've
seen:
- Stock data is fresh but only for some stores (partial sync).
- Master view is fresh but missing 30% of MAJ_CATs.

**Fix:** Add the missing rule. If the issue keeps recurring, audit
the rule list against actual failures of the last 30 days.

### Failure C — Rule is too strict (false positive)

**Symptom:** Red rule but downstream phases are fine. Data is normal.

**Likely cause:** Threshold is wrong. E.g. `row_count > 40M` when the
business has shrunk and 35M is normal now.

**Fix:** Update `expected` on the rule. Consider auto-tuning: store
last 30 days of actuals; expected becomes `mean - 3σ`.

---

## Performance benchmarks

| Rule type | Typical runtime |
|---|---|
| `freshness` | <1 sec (max(date) on indexed column) |
| `row_count` | 1-5 sec (count over partition stats) |
| `not_null` | 5-30 sec (depends on table size + index) |
| `referential` | 10-60 sec (depends on join size) |
| `value_check` | varies wildly — author it carefully |

A typical full run of ~50 rules takes 2-5 minutes. If yours takes much
longer, profile each rule and rewrite the slow ones.

---

## When this phase is healthy

The checklist matrix is all green or has only `warn`-level rows. The
page shows a green banner: "✅ Ready to proceed to MSA Calculation".
Phase 3 can begin.
