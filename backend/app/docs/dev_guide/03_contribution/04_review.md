# Contribution % — Review

Browse, filter, preview, download, and delete `ARS_CONTRIB_RESULTS_*`
tables produced by Execute.

---

## What it does

Lists every result table whose name starts with `ARS_CONTRIB_RESULTS_`,
lets the user pick one, applies dynamic filters per column, previews
the first ~1000 rows, and offers download / delete.

## Files

| Layer | File |
|---|---|
| Page | `frontend/src/pages/ContribReviewPage.jsx` |
| API | `app/api/v1/endpoints/contrib.py` (`/contrib/result-tables/*`) |
| Permission | `CONTRIB_REVIEW` |

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /contrib/result-tables` | List names + row counts |
| `POST /contrib/result-tables/{table}/preview` | Paginated preview with filters |
| `GET /contrib/result-tables/{table}/download` | Streamed CSV |
| `DELETE /contrib/result-tables/{table}` | Drop the table |

## Common changes

### Recipe: auto-cleanup old result tables

```sql
-- Drop result tables older than 30 days
DECLARE @sql NVARCHAR(MAX) = '';
SELECT @sql = @sql + 'DROP TABLE [' + name + ']; '
FROM sys.tables
WHERE name LIKE 'ARS_CONTRIB_RESULTS_%'
  AND create_date < DATEADD(day, -30, GETDATE());
EXEC sp_executesql @sql;
```

Wire as a daily cron or a "Cleanup" button on the page.

### Recipe: cap preview row count

For huge result tables (>1M rows), preview times out. Cap server-side:

```python
@router.post("/result-tables/{table}/preview")
def preview(table, payload):
    ...
    sql = f"SELECT TOP 1000 * FROM [{table}] WHERE {where_clause}"
    ...
```

Tell the user the cap on the page.

## Performance

- `GET /contrib/result-tables` uses `sys.partitions` for row counts (instant).
- Preview with filters benefits from indexes on filter columns; if
  filtering is slow, add an index when the result table is created in `contrib_service`.
