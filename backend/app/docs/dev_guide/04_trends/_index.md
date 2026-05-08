# Trends — Developer Reference

The Trends section is for **time-series analytics**. Three pages:
**Dashboard, Upload, Review**.

Trend tables follow a naming convention: `Trend_<source>_<grain>` —
e.g. `Trend_Sales_Daily`, `Trend_Stock_Weekly`. Each row has a
`REPORT_DATE` column.

## Files involved

| Layer | File |
|---|---|
| Routes | `app/api/v1/endpoints/trends.py` |
| Service | `app/services/trend_service.py` |
| Pages | `frontend/src/pages/Trend*.jsx` (Dashboard, Upload, Review, Admin) |
| API helper | `trendsAPI` in `services/api.js` |

---

## 9.1 Dashboard — `TrendDashboardPage.jsx`

### What it does

Configurable charts over Trend_* tables. Two view modes:

| Mode | Output |
|---|---|
| **Summary** | Bar/pie chart of values aggregated across the date range |
| **Trend** | Line/area chart by date grain (day/week/month/quarter/year), optional second-dim breakdown |

### Configuration knobs

| Knob | Effect |
|---|---|
| Grouping column | What dimensions to aggregate by |
| Metrics | Which numeric columns to plot |
| Aggregation | `SUM` / `AVG` / `COUNT` / `MAX` / `MIN` |
| Top N | Cap to top N groups by primary metric |
| Sorting | Asc / desc on primary metric |
| Date range | `REPORT_DATE` filter |
| Column filters | Per-column `IN` filter |

### Example: render a "top 20 stores by sales over last 30 days"

Frontend calls:
```js
trendsAPI.summary({
  table: 'Trend_Sales_Daily',
  grouping: 'WERKS',
  metrics: ['SALES_AMT'],
  agg: 'SUM',
  top_n: 20,
  date_from: '2026-04-01',
  date_to: '2026-04-30',
})
```

Backend translates to:
```sql
SELECT TOP 20 WERKS, SUM(SALES_AMT) AS sales_total
FROM Trend_Sales_Daily WITH (NOLOCK)
WHERE REPORT_DATE BETWEEN :from AND :to
GROUP BY WERKS
ORDER BY sales_total DESC
```

### Performance

- **Cross-year aggregations** on a 50M-row trend table are slow. Pre-aggregate at month-grain into a `Trend_Sales_Monthly` and route long ranges to that.
- **Distinct value lookups** for filter dropdowns repeat on every visit. Cache for 5 minutes.
- **Chart rendering** with composed (multi-series stacked) types is O(n²) in the Recharts library. Limit Top N to a hard ceiling (200) on the backend.

---

## 9.2 Upload — `TrendUploadPage.jsx`

Upload a Trend file with conflict detection.

### Three conflict modes

| Mode | What it does | When to use |
|---|---|---|
| **Append** | Insert new rows; ignore conflicts | Daily incremental loads |
| **Upsert** | Update on PK match, insert on miss | Re-running a partial day |
| **Replace** | Delete rows for the report_date, then insert | Regenerating after a fix |

### Example: upload via API

```bash
curl -X POST "$API/api/v1/trends/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@sales_2026_04_29.csv" \
  -F "table_name=Trend_Sales_Daily" \
  -F "report_date=2026-04-29" \
  -F "mode=replace"
```

### Performance gotcha

`Replace` mode does a `DELETE WHERE REPORT_DATE = ?` then `INSERT`. On a
50M-row table, the DELETE alone is heavy on the transaction log (FULL
recovery on Azure SQL). Consider:

```sql
-- Better than a single DELETE on FULL recovery
WHILE 1=1
BEGIN
  DELETE TOP (10000) FROM Trend_Sales_Daily
   WHERE REPORT_DATE = '2026-04-29';
  IF @@ROWCOUNT = 0 BREAK;
END
```

---

## 9.3 Review — `TrendReviewPage.jsx`

Browse a Trend_* table with filters, distinct-value dropdowns, and
download.

### Why distinct-value dropdowns are slow

The page fires `SELECT DISTINCT col FROM Trend_* WITH (NOLOCK)` per
column. On a 50M-row table with 10k+ unique values per column, this is
seconds per dropdown.

### Fixes

1. **Cache** distinct values for 5 min per (table, column).
2. **Server-side typeahead** instead of preloading the full dropdown.
3. **ag-Grid server-side row model** for tables >100k rows; client-side
   row model chokes past that.

### Example: switch the page to server-side rows

`TrendReviewPage.jsx` → set `rowModelType: 'serverSide'` on the
ag-Grid options and implement `getRows` to call a paginated backend
endpoint. Add the endpoint in `trends.py` returning `{rows, total_count,
filter_model_supported}`.

---

## 9.4 Admin — `TrendAdminPage.jsx`

Hidden gem — admin-only screen for managing the list of Trend tables,
their schemas, and report-date conventions.

### When to use it

- Adding a new Trend feed from SAP (e.g. `Trend_Customer_Daily`).
- Renaming a table (handle carefully — Dashboard configs reference the
  table name).
- Auditing data freshness across all Trend tables.

---

## How to add a new Trend table

1. **DDL** — create the table on the data DB. Required columns:
   - PK columns (whatever uniquely identifies a row)
   - `REPORT_DATE DATE NOT NULL`
   - Indexed on `REPORT_DATE` (covering index over the columns most often filtered).
2. **Register it** — Trend Admin page or insert a row into the
   `trend_tables` registry (if used).
3. **Upload** — first load via Trend Upload with `mode=append`.
4. **Verify** — Review page shows the data; Dashboard finds the table
   in the picker.

### Index recommendation

```sql
CREATE NONCLUSTERED INDEX IX_Trend_Sales_Daily_RD_WERKS
ON Trend_Sales_Daily (REPORT_DATE, WERKS)
INCLUDE (SALES_AMT, QTY);
```

This single index speeds up nearly every Dashboard query that filters
by date and groups by store.

---

## Cross-cutting Trends advice

- **Don't query Trend tables on every page load.** Cache.
- **Always filter by `REPORT_DATE`** — without it, queries scan the
  whole table.
- **Trend tables grow forever.** Plan an archival strategy
  (`Trend_Sales_Daily_Archive` table for rows older than 1 year) or
  partitioning.
- **Treat Trend uploads as critical-path** — they feed Contribution %.
  A failed Trend upload silently breaks the next contribution run.
