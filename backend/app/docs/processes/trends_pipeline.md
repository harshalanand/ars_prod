---
title: Trends — Uploading & Charting Side Data
category: Trends
order: 70
source: backend/app/api/v1/endpoints/trends.py
last_reviewed: 2026-04-20
---

# Trends

> **═══ USER GUIDE ═══**

## In plain English

Trends is **the side garage** for all the datasets that don't drive the allocation directly but you still want to look at — market-share reports, competitor pricing snapshots, promo calendars, customer footfall, seasonal forecasts, anything weekly/monthly.

It has nothing to do with MSA, grids, or allocation. It's a place to **upload**, **store by version**, **browse**, and **chart** auxiliary data.

## When to use this

Use Trends when you want to:

- Track a **recurring dataset** over time (e.g. weekly market share).
- Share a **snapshot** with the team without emailing Excel around.
- Compare **two versions** of the same dataset to see what changed.
- Plug numbers into a **dashboard chart** for executive review.

You do **not** use Trends to upload ARS inputs like `ET_STORE_STOCK` — those go through Data Management → Upload.

## The 4 stages

```
1. UPLOAD     → Turn a CSV/Excel into Trend_<name> table.
                System columns are auto-added (VERSION, REPORT_DATE, who uploaded, etc.)

2. REVIEW     → Browse tables, preview rows, inspect schema.

3. ADMIN      → Manage versions, columns, ownership.
                Pin a version as "current", delete old versions, rename.

4. DASHBOARD  → Query stored trends, render charts.
```

Each upload creates a **new version** of the same table — old versions stay, so you build up a time-series automatically.

## Step-by-step — uploading a trend

### 1. Prepare the file
- CSV or Excel.
- First row = column headers.
- Don't include any "TOTAL" rows — just the raw data.
- Keep column names simple (letters, numbers, underscore).

### 2. Go to Trends → Upload
Sidebar → **Trends** → **Upload**.

Fill in:
- **Trend name** — short identifier. E.g. `market_share`. This becomes table `Trend_market_share`.
- **Report date** — the date the data represents (not today's upload date).
- **File** — browse and select.

### 3. Click Upload
Server:
1. Reads the file.
2. Infers column types.
3. Looks up existing versions for this trend name.
4. If first time → creates `Trend_<name>`. Otherwise appends with `VERSION = MAX + 1`.
5. Prepends system columns automatically.
6. Returns row count and version number.

### 4. Review
Sidebar → **Trends** → **Review**.

You'll see a list of trend tables with:
- Row counts
- Latest report date
- Who uploaded
- Column list (introspected)

Click any table to preview rows.

### 5. Chart on Dashboard
Sidebar → **Trends** → **Dashboard**.

Pick a trend table → pick filters / group-by / metric columns → chart renders. Useful for quick executive reports or weekly stand-ups.

## What system columns get added

When you upload, ARS **always prepends** these columns:

| Column | Value |
|---|---|
| `VERSION` | Auto-incremented per trend name (1 for first upload, 2 for next) |
| `REPORT_DATE` | The date you chose in the form |
| `UPLOAD_DATETIME` | Server time when the file was accepted |
| `UPLOADED_BY` | Current logged-in user |
| `SYSTEM_IP` | IP address of the client that uploaded |
| `SOURCE_FILE` | Original filename |

These let you trace exactly *which upload* a given row came from — useful for audit and rollback.

## A worked example

You upload `weekly_market_share_2026W14.csv`:
```
STORE_CITY, BRAND, MARKET_SHARE_PCT
Bengaluru, BrandA, 41.2
Bengaluru, BrandB, 18.7
Chennai,   BrandA, 37.9
...
```

Form: `trend_name = market_share`, `report_date = 2026-04-01`.

Resulting table `Trend_market_share` has columns:
```
VERSION INT, REPORT_DATE DATE, UPLOAD_DATETIME DATETIME,
UPLOADED_BY NVARCHAR(100), SYSTEM_IP NVARCHAR(64), SOURCE_FILE NVARCHAR(260),
STORE_CITY NVARCHAR(255), BRAND NVARCHAR(255), MARKET_SHARE_PCT FLOAT
```

Next week, you upload `weekly_market_share_2026W15.csv` — same trend name — and rows appear with `VERSION = 2, REPORT_DATE = 2026-04-08`.

A dashboard query "latest report date, group by brand, sum market share" returns 2026-04-08 data because that's the highest version.

## Common questions (FAQ)

**Q: If I upload a file with slightly different columns, what happens?**
The upload refuses and tells you which columns don't match. Either rename the columns in your file, or delete the trend table and start fresh (loses history).

**Q: Can I delete just one version?**
Yes, via Admin → pick version → delete. The others stay.

**Q: Can I rename a trend table?**
Admin → rename. This renames the SQL table. Dashboard queries follow automatically, but any external queries hardcoded to the old name will break.

**Q: Dashboard shows old numbers even after I uploaded a new version.**
The dashboard defaults to the **latest** version by `UPLOAD_DATETIME`. If you pinned an older version as "current" in Admin, unpin it. Refresh the dashboard.

**Q: Is there RLS on trends?**
By default, any user with `TRENDS_VIEW` can read. Admin actions need `TRENDS_ADMIN`. Row-level restrictions (e.g., a city leader only seeing their own city) aren't enforced automatically — talk to the dev team if you need that.

## Troubleshooting — "I see X"

| You see | Meaning | Fix |
|---|---|---|
| "Invalid column name" on upload | Column header has symbol (`&`, `/`, space, dash) | Clean the file's header row, re-upload. |
| Numbers imported as text | Excel stored them with leading zeros or thousand separators | Clean in source or force format in Excel before export. |
| Table balloons in size | No retention policy — old versions piling up | Admin → delete old versions regularly. |
| Access denied to admin page | Missing `TRENDS_ADMIN` permission | Ask Super Admin to grant. |

## Verification

```sql
-- All trend tables
SELECT t.name, t.create_date, t.modify_date
FROM sys.tables t
WHERE t.name LIKE 'Trend_%'
ORDER BY t.modify_date DESC;

-- Version history for one trend
SELECT VERSION,
       COUNT(*)         AS rows_,
       MAX(REPORT_DATE) AS report_date,
       MAX(UPLOAD_DATETIME) AS uploaded_at,
       MAX(UPLOADED_BY) AS uploader
FROM Trend_market_share
GROUP BY VERSION
ORDER BY VERSION DESC;

-- Latest version's data
SELECT TOP 100 * FROM Trend_market_share
WHERE VERSION = (SELECT MAX(VERSION) FROM Trend_market_share);
```

## Settings you can change

| Setting | Effect |
|---|---|
| Pin a specific version as "current" | Dashboard uses that one even if newer uploads exist |
| Delete old versions | Clean up disk |
| Archive a trend | Renames + marks as inactive; removed from default UI list |

---

> **═══ TECHNICAL REFERENCE ═══**

## Behind the scenes — for developers

- Endpoints: `backend/app/api/v1/endpoints/trends.py` — `/upload`, `/review`, `/admin`, `/dashboard`.
- Tables created on demand: `Trend_<user-supplied suffix>`.
- System columns are prepended consistently — see the `SYSTEM_COLUMNS` constant.
- Dashboard query DSL: simple JSON `{filter, group_by, metric}`.

## How to update this doc

Update when new system columns are added, when a new admin action is introduced, or when the dashboard query DSL extends. Bump `last_reviewed`.
