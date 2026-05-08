# Create Table

Lets an admin design a brand-new table interactively, then runs the
`CREATE TABLE` against the data DB. Used when you need a custom table
that isn't part of the standard ARS schema.

---

## What it does

Translates a UI form (table name, columns, types, primary key) into a
parameterised `CREATE TABLE` statement, runs it, and registers the new
table so it appears in All Tables immediately.

## Where it lives

| Layer | File |
|---|---|
| Page | `frontend/src/pages/CreateTablePage.jsx` |
| API | `backend/app/api/v1/endpoints/tables.py` → `POST /tables` |
| Service | `backend/app/services/table_mgmt_service.py` → `create_table()` |
| Permission | `TABLE_CREATE` |

## Step-by-step (logic flow)

```
1. User fills the form
   ┌─────────────────────────────┐
   │ Table name: my_test         │
   │ Columns:                    │
   │  ┌────────┬────────┬────┐  │
   │  │ name   │ type   │ pk │  │
   │  ├────────┼────────┼────┤  │
   │  │ id     │ BIGINT │ ✓  │  │
   │  │ sku    │ NVARC… │    │  │
   │  │ qty    │ FLOAT  │    │  │
   │  └────────┴────────┴────┘  │
   └─────────────────────────────┘
                │
                ▼
2. Frontend validates client-side:
     - table name matches /^[A-Za-z][A-Za-z0-9_]*$/
     - at least one PK
     - no duplicate column names

3. POST /api/v1/tables with payload:
   {
     "table_name": "my_test",
     "columns": [
       {"name":"id","type":"BIGINT","nullable":false,"primary_key":true},
       {"name":"sku","type":"NVARCHAR(50)","nullable":false},
       {"name":"qty","type":"FLOAT","nullable":true}
     ]
   }

4. Backend re-validates (don't trust the client):
     - permission check (TABLE_CREATE)
     - table-name regex check
     - reject reserved words (sys*, INFORMATION_SCHEMA*)
     - check table doesn't already exist

5. Build CREATE TABLE SQL:
   CREATE TABLE [my_test] (
       [id]  BIGINT       NOT NULL,
       [sku] NVARCHAR(50) NOT NULL,
       [qty] FLOAT        NULL,
       CONSTRAINT [PK_my_test] PRIMARY KEY ([id])
   )

6. Execute on data DB inside a transaction.

7. Audit log entry: action_type=DDL_CREATE_TABLE, by, timestamp, full DDL.

8. Return success → frontend redirects to All Tables.
```

## Why each safeguard exists

| Safeguard | Stops |
|---|---|
| Regex on table name | SQL injection (`my_table; DROP DATABASE Rep_Data;`) |
| Reject reserved prefixes | Accidentally shadowing system catalogs |
| Check exists first | Confusing "table already exists" errors mid-DDL |
| Transaction wrap | Partial DDL on failure |
| Audit log entry | "Who made this table?" forensics |

---

## Example: programmatic table creation

```bash
curl -X POST "$API/api/v1/tables" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "table_name": "scratch_pad_2026",
    "columns": [
      {"name":"id","type":"BIGINT","nullable":false,"primary_key":true},
      {"name":"event_at","type":"DATETIME2","nullable":false},
      {"name":"payload","type":"NVARCHAR(2000)","nullable":true},
      {"name":"score","type":"DECIMAL(10,4)","nullable":true}
    ]
  }'
```

Response on success:
```json
{
  "success": true,
  "message": "Table 'scratch_pad_2026' created.",
  "data": { "table_name": "scratch_pad_2026", "row_count": 0 }
}
```

---

## Type catalog (what the dropdown should expose)

| Category | Types | Notes |
|---|---|---|
| **Integer** | TINYINT, SMALLINT, INT, BIGINT | Use BIGINT for IDs |
| **Decimal** | DECIMAL(p,s), NUMERIC(p,s), FLOAT, REAL | DECIMAL for money, FLOAT for stats |
| **Text** | NVARCHAR(N), VARCHAR(N), NCHAR(N), CHAR(N) | Default to NVARCHAR(500) |
| **Date/Time** | DATE, DATETIME2, DATETIMEOFFSET | DATETIME2 over DATETIME (better precision) |
| **Boolean** | BIT | 0 / 1 |
| **Binary** | VARBINARY(N), VARBINARY(MAX) | Avoid MAX unless truly needed |

### What you should never default to

- **NVARCHAR(MAX)** — LOB type, defeats `fast_executemany`, makes upserts 10× slower and uses huge memory.
- **TEXT / NTEXT / IMAGE** — deprecated, replaced by `*VARCHAR(MAX)` decades ago.
- **DATETIME** — old type with millisecond rounding bugs. Use `DATETIME2`.

## Indexing right after creation

The form lets you pick a PK but not other indexes. **Every** column you
plan to filter/sort by should have an index. Add them via Table
Management page after creation:

```sql
-- Range index for date queries
CREATE NONCLUSTERED INDEX IX_scratch_event_at
  ON scratch_pad_2026(event_at);

-- Covering index for a known query pattern
CREATE NONCLUSTERED INDEX IX_scratch_event_at_score
  ON scratch_pad_2026(event_at)
  INCLUDE (score);
```

---

## Common changes

### Add support for IDENTITY columns

`table_mgmt_service.create_table` builds the column DDL. Add a flag:

```python
if col.get('identity'):
    parts.append(f"[{col['name']}] {col['type']} IDENTITY(1,1) NOT NULL")
```

Frontend: add an "auto-increment" checkbox on the column row. Only
allow it on integer types and only one per table.

### Pre-populate with a template

Add a "Template" dropdown in `CreateTablePage.jsx` that pre-fills the
form:

```js
const TEMPLATES = {
  'audit-style': [
    { name: 'id',         type: 'BIGINT',         nullable: false, primary_key: true, identity: true },
    { name: 'created_at', type: 'DATETIME2',      nullable: false },
    { name: 'created_by', type: 'NVARCHAR(100)',  nullable: false },
    { name: 'payload',    type: 'NVARCHAR(MAX)',  nullable: true },
  ],
  'lookup-style': [
    { name: 'code',  type: 'NVARCHAR(50)',  nullable: false, primary_key: true },
    { name: 'label', type: 'NVARCHAR(200)', nullable: false },
  ],
}
```

### Add foreign-key support

Extend the column form with a "References" field. Backend appends:

```sql
CONSTRAINT [FK_<tbl>_<col>] FOREIGN KEY ([<col>])
    REFERENCES [<other_tbl>]([<other_col>])
```

Be careful: FK columns need matching types. Validate server-side.

---

## Failure modes & how the UI should react

| Backend error | Frontend should | Why |
|---|---|---|
| 403 (no permission) | Show "You need TABLE_CREATE" | User-friendly |
| 409 (already exists) | Suggest a unique name or "Open existing" | Data loss-avoidance |
| 400 (invalid type) | Highlight the offending row | Quick fix |
| 500 (DB error) | Show full error + reload All Tables | Sometimes the table DID get created |

---

## Performance & limits

- **Don't loop CREATE TABLE in a script.** Each invokes a Sch-M lock on `sys.objects`. Bulk-create, if needed, in a single transaction.
- **Up to ~1,024 columns per table** (SQL Server limit). The UI should warn at ~100.
- **Wide tables (>50 columns) are an anti-pattern.** They thrash the buffer pool. Split into related tables.

---

## Audit trail

Every successful create writes:

```json
{
  "table_name": "scratch_pad_2026",
  "action_type": "DDL_CREATE_TABLE",
  "changed_by": "santosh",
  "source": "UI",
  "ip_address": "...",
  "details": "{\"columns\": [...], \"ddl\": \"CREATE TABLE ...\"}"
}
```

Useful when someone asks "where did this table come from?" — query
`audit_log` filtered by `action_type='DDL_CREATE_TABLE'`.
