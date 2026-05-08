# Contribution % — Presets

A **preset** is a saved calculation recipe. "Last 30 days, daily avg
over 30 days, KPI=L30D." Presets get reused across many contribution
runs so the math is consistent.

---

## Schema

`ARS_CONTRIB_PRESETS`:

| Column | Example | Purpose |
|---|---|---|
| `id` | 1 | PK |
| `name` | `L30D` | Display name |
| `months` | 1 | How many months of sales to include |
| `avg_days` | 30 | Days used for "average sale per day" |
| `kpi_type` | `L30D` | Which trailing-window flavour |
| `description` | "Last 30 days, fast-moving SKUs" | Documentation |
| `seq` | 1 | Order in fallback chain |
| `created_at`, `updated_at` | | |

## Files

| Layer | File |
|---|---|
| Page | `frontend/src/pages/ContribPresetsPage.jsx` |
| API | `app/api/v1/endpoints/contrib.py` (`/contrib/presets/*`) |
| Permission | `CONTRIB_PRESETS` |

## Operations

| Op | Endpoint |
|---|---|
| List | `GET /contrib/presets` |
| Create | `POST /contrib/presets` |
| Update | `PUT /contrib/presets/{id}` |
| Delete | `DELETE /contrib/presets/{id}` |
| Reorder | `POST /contrib/presets/reorder` body=[{id, seq}] |

## Example: create + reorder

```bash
# Create
curl -X POST "$API/api/v1/contrib/presets" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "name": "L7D_HOT",
    "months": 0,
    "avg_days": 7,
    "kpi_type": "L7D",
    "description": "Last 7 days only — for hot launches"
  }'

# Reorder (seq drives fallback priority)
curl -X POST "$API/api/v1/contrib/presets/reorder" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '[{"id":1,"seq":1},{"id":2,"seq":2},{"id":3,"seq":3}]'
```

## Logic — what `kpi_type` means in the engine

`contrib_service` looks up `kpi_type` to pick the SQL window:

| kpi_type | Window definition |
|---|---|
| `L7D` | last 7 days from today |
| `L30D` | last 30 days |
| `L18M` | last 18 months |
| `MTD` | month-to-date |
| `LAST_FULL_MONTH` | first → last day of the previous calendar month |

If a preset's `kpi_type` is unknown, the engine logs a warning and
falls back to `L30D`.

## Common changes

### Recipe: add a new KPI flavour (e.g. `L90D`)

`contrib_service.py` → look for `_window_for_kpi(kpi_type)`. Add:

```python
def _window_for_kpi(kpi_type, today=None):
    today = today or datetime.utcnow().date()
    if kpi_type == 'L7D':   return today - timedelta(days=7), today
    if kpi_type == 'L30D':  return today - timedelta(days=30), today
    if kpi_type == 'L90D':  return today - timedelta(days=90), today    # NEW
    if kpi_type == 'L18M':  return today - timedelta(days=540), today
    ...
```

Test by creating a preset with `kpi_type='L90D'` and running it.

### Recipe: add preset versioning

Today, editing a preset overwrites the old definition silently.
Versioning preserves history:

1. New table:
   ```sql
   CREATE TABLE ARS_CONTRIB_PRESETS_HISTORY (
     id INT IDENTITY PRIMARY KEY,
     preset_id INT NOT NULL,
     name NVARCHAR(100),
     months INT, avg_days INT, kpi_type NVARCHAR(20),
     description NVARCHAR(500), seq INT,
     archived_at DATETIME2 DEFAULT SYSUTCDATETIME(),
     archived_by NVARCHAR(100)
   );
   ```
2. Before each UPDATE in the API: copy current row into history.
3. Add a "Show history" button on the page that lists past versions
   and lets you restore.

### Recipe: optimistic concurrency on reorder

Two admins reordering at once today = last-write-wins, possibly
inconsistent ordering. Add a `version` column and check on update:

```sql
UPDATE ARS_CONTRIB_PRESETS
SET seq = :seq, version = version + 1
WHERE id = :id AND version = :expected_version
```

If 0 rows affected → 409, refresh, retry.

## Performance

Tiny table (typically <50 rows). No indexing concerns. The list
endpoint is fast enough to call uncached.
