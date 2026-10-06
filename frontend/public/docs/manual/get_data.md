# Get Data — outside sources → local SQL, on a schedule

## BRD (why)
ARS reads its inputs from local SQL Server (Rep_data on HOPC866). More and more of
V2's data now lives outside it — above all in **Snowflake** (V2RETAIL: BRONZE, SILVER,
GOLD, ARS_GOLD …), and later in Excel files, DataV2 and SAP. Getting that data in
has meant one-off scripts and manual copies: no schedule, no record of what ran, and
no way to tell whether a table is today's or last week's.

Get Data is the one place that brings outside data **into** local SQL:

- define the data once (a Snowflake view),
- copy it into a local table **daily on a schedule** or **on demand**,
- keep a **history of every run** — automatic or manual, who ran it, and how many
  rows came across — so a planner can trust the table before using it.

Phase 1 (built) is Snowflake. Excel (phase 2), DataV2 (phase 3, source to confirm)
and SAP (phase 4, absorbs SAP → Data Pulls) plug into the same jobs and history.

## FSD (rules)

### Screens (sidebar → Get Data)
| Screen | Route | What it does |
|---|---|---|
| Overview | `/get-data/overview` | Sources, today's runs (auto · manual · success · failed · rows), next scheduled runs, failures in the last 7 days, 7-day table. |
| Snowflake Views | `/get-data/snowflake/views` | Write a SELECT → Validate → Preview 100 rows → Create view. Browse every schema read-only. |
| Sync Jobs | `/get-data/snowflake/jobs` | Source → local table, load mode, IST schedule, retry, Run now / Full reload, live progress. |
| Run History | `/get-data/runs` | Every run with AUTO/MANUAL, run by, Snowflake rows, rows loaded, time, status, details, Excel export. |
| Excel · DataV2 · SAP | `/get-data/excel` … | Placeholders describing the later phases. |
| Help | `/get-data/help` | Plain-language guide. |

### Snowflake views — validate first, then create
- Views are created **only** in `V2RETAIL.ARS_GETDATA` and named `V_GD_*` (prefix added
  when missing). The schema is created on first use (`CREATE SCHEMA IF NOT EXISTS`).
  Nothing else in Snowflake can be changed from the module; any other schema is a
  read-only source.
- The user writes only the **SELECT / WITH body**; the module wraps it in
  `CREATE OR REPLACE VIEW … COMMENT = '<description>' AS <body>`.
- **Validation gate** (all must pass; the server re-runs it on Create, whatever the UI did):
  1. *Read-only SELECT* — body starts with SELECT/WITH (after comments), one statement
     (no `;` outside string literals/comments).
  2. *Compiles* — `SELECT * FROM (<body>) LIMIT 0` succeeds in Snowflake.
  3. *Columns* — names unique ignoring case (SQL Server is case-insensitive) and not
     `_GD_RUN_ID` / `_GD_LOADED_AT`.
  4. *Row count* — `SELECT COUNT(*) FROM (<body>)`.
- The Save button stays off until the **current** SQL passed validation; editing the SQL
  after validating switches it off again.
- Every save writes a version row (`GD_VIEW_VERSION`: created / replaced / dropped, SQL,
  columns, rows, who, when). A view created outside the app is listed as “made outside
  app”; opening it recovers the body from `GET_DDL`, and saving it brings it under
  version control.
- **Drop** is refused while a sync job uses the view.

### Sync jobs
- A job = source (`DB.SCHEMA.NAME`, any Snowflake view or table) → local table in
  Rep_data. Local tables **always** start with `GD_SF_` (added when missing); names
  ending `__STG` / `__OLD` are refused (load internals). One job per local table.
- Metadata tables never use the `GD_SF_` prefix (`GD_VIEW`, `GD_VIEW_VERSION`,
  `GD_JOB`, `GD_RUN`), so a job can never overwrite them.
- **Check source** (count only, nothing loaded): source readable, column names,
  key/watermark columns exist, watermark type orderable, row count, and the
  Snowflake → SQL Server type for every column.
- Every loaded table gets two audit columns: `_GD_RUN_ID` (the run that wrote the row)
  and `_GD_LOADED_AT` (UTC).
- **Columns** (`GD_JOB.COLUMN_MAP`, JSON `[{source, name, load}]`): after Check source the
  form lists every Snowflake column. Type a **new local name** to rename it, untick
  **Load** to skip it (a skipped column is not even read from Snowflake). Rules, checked in
  the form and again on save: letters, digits and `_`, starting with a letter (so
  `_GD_*` can't be used), ≤ 120 characters, unique ignoring case, at least one column
  loaded, key and watermark columns can't be skipped (they are always picked by their
  Snowflake name). A form left untouched stores no mapping.
  - Full replace: the next run builds the table with the new names.
  - Incremental / append: the table keeps its rows, so a renamed column is renamed in place
    (`sp_rename`) using `GD_JOB.APPLIED_COLUMN_MAP` — the names the table had after its last
    good run (tables loaded before mappings existed use the Snowflake names). A skipped
    column stays in the table and is NULL for new rows.
  - A Snowflake column the mapping doesn't list loads under its own name and the run
    message says so ("not in the job's Columns list"); a mapped column that has vanished
    from Snowflake is noted, not an error. Mapped names are reserved first, so a newcomer
    with the same name gets `_2`, never the mapped column.
  - `GD_RUN.COLUMN_MAP` records the names each run used (`{source: local name or null}`).
- **Loader** (`GD_JOB.LOADER_PREF`): **Bulk copy** (default) or **Classic insert**, chosen
  in the job form. Bulk still falls back to classic, with the reason in the run, when the
  data can't go through bcp exactly (see step 5). Classic is never upgraded to bulk.
- **Empty-source guard** (`GD_JOB.ALLOW_EMPTY`, default off): a full replace (or full
  reload) that gets **0 rows** from Snowflake while the local table has rows **fails and
  keeps the rows**. Tick "Allow an empty Snowflake result to empty the local table" on the
  job when an empty source is legitimate. Incremental and append are not affected — 0 new
  rows is normal there.

### Load modes
| Mode | Each run | Notes |
|---|---|---|
| Full replace | Whole source → stage table → swap in | The live table changes only after the row-count check passes. |
| Incremental | Rows with `watermark ≥ last loaded value` → MERGE on key columns | First run and **Full reload** load everything. `≥` re-reads the boundary so late rows for the last day/time are caught; the MERGE makes re-reads harmless. Duplicate keys in one batch keep the row with the highest watermark. NULL keys match NULL keys. |
| Append | Whole source → INSERT | Snapshot history; `_GD_LOADED_AT` separates runs. No full reload (it would erase the history). |

Changing a job's source, local table, mode or watermark column clears the stored
watermark, so the next incremental run loads everything.

### One run, step by step
1. **Lock** — `GD_JOB.LOCK_RUN_ID` is taken under `UPDLOCK, HOLDLOCK`. A live lock →
   Run now gets HTTP 409, a scheduled run is recorded as `skipped`. A lock whose
   heartbeat is older than 10 min is taken over and that run marked failed.
2. **Read columns** — `SELECT * FROM <source> LIMIT 0`.
3. **Query (once)** — the data SELECT; Snowflake's row count for *that* result is
   `SOURCE_ROWS` and its query id is `SF_QUERY_ID` (query tag `ARS_GET_DATA`, session
   TIMEZONE UTC, `arrow_number_to_decimal` on so decimals stay exact).
4. **Profile** — one pass over that query's stored result (`RESULT_SCAN`), so a heavy
   view is computed once, not twice. Per column: non-null count; text: max length,
   values that aren't plain ASCII (printable, tab, CR, LF), values containing 0x1E / 0x1F / NUL;
   numbers: sum (and max |x| for NUMBER(>18,0)); floats: finite count and sum.
5. **Load** — into a fresh heap `GD_SF_<X>__STG`, by one of two loaders:
   - **Bulk copy (default)** — Arrow batches from Snowflake are encoded to UTF-8 chunk
     files (1,000,000 rows) with fields separated by 0x1F and rows by 0x1E, so text with
     line breaks (descriptions, VARIANT JSON) loads unchanged, and loaded by `bcp … -C 65001
     -h TABLOCK,CHECK_CONSTRAINTS`, 3 chunks in parallel. Each chunk is one bcp
     transaction, so a failed chunk commits nothing and is retried once. NULL = empty
     field, `''` = one NUL byte. Takes every type except BINARY: numbers, text, dates,
     timestamps, BOOLEAN, TIME (as `HH:MM:SS.ffffff`), VARIANT / OBJECT / ARRAY (JSON text).
     About 96,000 rows/s measured, against 7,800–13,000 for the classic loader.
   - **Classic insert (fallback)** — pyodbc `fast_executemany` in batches of 20,000,
     committing each batch. Used, with the reason written in the run message, when the
     job is set to Classic, bcp isn't installed (e.g. Azure), `GET_DATA_FAST_LOADER=0`,
     no bcp login works, a column is BINARY, or a text value contains the separators
     themselves (0x1E / 0x1F / NUL).
   `GD_RUN.LOADER` records `bulk` or `classic`.
6. **Verify** — the stage table must reproduce Snowflake's own figures: `COUNT(*)` =
   `SOURCE_ROWS` = rows fetched, the non-null count of every column, the exact sum of
   every number column (floats within 10⁻⁸ × Σ|x|, since float totals depend on the
   order they're added in). Otherwise: stage dropped, run **failed** with
   “Count mismatch” / “Checksum mismatch”, live table untouched.
7. **Apply** — replace: optional clustered index on the key columns, then in one
   transaction rename live → `__OLD`, stage → live; drop `__OLD` after commit.
   Incremental / append: add new columns and widen text/integer columns, then MERGE /
   INSERT, drop stage.
8. **Record** — counts, watermark from → to, duration, message on `GD_RUN`; last status
   / rows / watermark on `GD_JOB`; lock released.

### Type mapping (Snowflake → SQL Server)
| Snowflake | Local |
|---|---|
| NUMBER(p≤9, 0) | INT |
| NUMBER(10–18, 0) | BIGINT |
| NUMBER(>18, 0) | BIGINT, or DECIMAL(38,0) when a value exceeds ±9×10¹⁸ |
| NUMBER(p, s>0) | DECIMAL(p, s) |
| FLOAT | FLOAT (NaN / ±Inf → NULL) |
| VARCHAR(n ≤ 4000) | VARCHAR(n) when every value is plain ASCII (printable, tab, CR, LF), else NVARCHAR(n) |
| VARCHAR wider | same choice; width = measured max × 1.25, rounded up to 10/20/50/100/255/500/1000/2000/4000, else (N)VARCHAR(MAX); all-NULL → 255 |
| DATE | DATE |
| TIMESTAMP_NTZ / LTZ / TZ | DATETIME2(6), time-zoned values converted to UTC |
| TIME | NVARCHAR(20) |
| BOOLEAN | BIT |
| BINARY | VARBINARY(MAX) |
| VARIANT / OBJECT / ARRAY / other | NVARCHAR(MAX) (JSON text) |

### Scheduling (IST)
- Schedule times are **IST wall-clock**; `NEXT_RUN_AT` / `RETRY_AT` are stored in UTC
  (IST = UTC+05:30, no daylight saving). Math reuses `compute_next_run` from the report
  scheduler: daily (one or more times), weekly (weekdays + times), monthly (days + times;
  31 clamps to month end), every N hours (1–24, optional IST window).
- The Get Data scheduler (`get_data_scheduler.py`) ticks every 30 s in every uvicorn
  worker: reconcile dead runs → claim due jobs with a guarded UPDATE (so a job fires
  once across 4 workers) → start AUTO runs. At most 3 runs load at once; the rest wait
  with their heartbeat alive.
- A run missed while the backend was down fires **once** at the next tick.
- **Retry**: a failed AUTO run (attempt 1) of an enabled job with retry on is retried
  once after `RETRY_DELAY_MIN` (default 15, allowed 5–240); the retry is attempt 2. A
  success clears any pending retry. Manual runs are never retried automatically.
- Run now returns at once with the run id; the page polls for progress (step, rows
  loaded of Snowflake rows).

### Run history (`GD_RUN`)
One row per run: `RUN_TYPE` AUTO | MANUAL, `TRIGGERED_BY` (user or `scheduler`),
`ATTEMPT`, `FULL_RELOAD`, `STATUS` running | success | failed | skipped, `STEP`,
`SOURCE_ROWS`, `ROWS_LOADED`, `ROWS_INSERTED`, `ROWS_UPDATED`, `TARGET_ROWS_BEFORE`,
`TARGET_ROWS_AFTER`, `WATERMARK_FROM`, `WATERMARK_TO`, `SF_QUERY_ID`, `STARTED_AT`,
`HEARTBEAT_AT`, `COMPLETED_AT`, `DURATION_MS`, `MESSAGE`. Job name, source, table and
mode are copied onto the run so history survives a deleted job.
Filters: IST date range, job, trigger, status. Summary: auto, manual, rows loaded
(successful runs), failed, skipped.

### Permissions
- Module visibility: `MOD_GET_DATA` (seeded to all active roles once, like every new
  module; untick per role in Settings → Roles).
- Manage (create/drop views, create/edit/enable/delete jobs): SUPER_ADMIN, ADMIN, or
  `GET_DATA_MANAGE`.
- Run now / Full reload / Validate / Preview / Check source: the above or `GET_DATA_RUN`.

### Tables (Rep_data, self-healing — `get_data_schema.py`, reference `038_get_data_module.sql`)
`GD_VIEW`, `GD_VIEW_VERSION`, `GD_JOB`, `GD_RUN`, plus one `GD_SF_*` table per job.

### API (`/api/v1/get-data`)
`GET overview` · `GET snowflake/status|schemas|objects|columns|views|views/{name}` ·
`POST snowflake/validate|preview|views` · `DELETE snowflake/views/{name}` ·
`GET|POST jobs` · `POST jobs/test` · `GET|PUT|DELETE jobs/{id}` · `POST jobs/{id}/enable` ·
`POST jobs/{id}/run?full_reload=` · `GET runs` · `GET runs/{id}` · `GET scheduler/status`.

### Depends on
- Settings → Snowflake (the single app-wide connection, `snowflake_config_service`),
  switched **on**, and `snowflake-connector-python[pandas]` installed on the server.
- The backend process running (schedules live inside it).
- For the bulk loader: `bcp` (Microsoft ODBC Driver 18 command-line tools) on the server.
  Without it every run uses the classic loader — slower, same result. Chunk files go to
  the system temp folder and are deleted as each chunk loads. Tuning:
  `GET_DATA_BCP_PARALLEL` (3), `GET_DATA_CHUNK_ROWS` (1,000,000),
  `GET_DATA_FAST_LOADER=0` to switch it off.
- **bcp login** (`GET_DATA_BCP_AUTH`, default `windows`): Windows authentication (`-T`,
  the account running the backend) when SQL Server accepts it — checked every 10 min,
  so a login added by the DBA is used without a restart. Until then: the app's SQL login
  with the password passed on bcp's standard input, never on its command line (the
  process list is readable by any local user). The run message names the login used.
  With `-h "TABLOCK,CHECK_CONSTRAINTS"` the Windows login needs only `SELECT, INSERT`
  on schema `dbo` of the data DB — no ALTER, no sysadmin. DBA script:
  ```sql
  CREATE LOGIN [V2RD\santosh.kumar3] FROM WINDOWS;
  USE [Rep_Data];
  CREATE USER [V2RD\santosh.kumar3] FOR LOGIN [V2RD\santosh.kumar3];
  GRANT SELECT, INSERT ON SCHEMA::dbo TO [V2RD\santosh.kumar3];
  ```
  (Use the account the backend runs under — today `V2RD\santosh.kumar3` on HOPC575.)

## Recorded rules
- 2026-09-30 · Views are created only in `V2RETAIL.ARS_GETDATA` as `V_GD_*`; every other
  Snowflake object is read-only to the module.
- 2026-09-30 · Validate before create: the server re-runs the full validation on every
  Create — the UI's gate is a convenience, not the control.
- 2026-09-30 · `SOURCE_ROWS` is the row count of the exact result that was loaded
  (cursor rowcount of the data query), not a separate COUNT(*), so a source changing
  between two queries can't cause a false mismatch.
- 2026-09-30 · A count mismatch fails the run and leaves the live table unchanged
  (replace mode swaps only after the check).
- 2026-09-30 · `GD_SF_` is reserved for synced data tables; metadata tables use `GD_`
  without `SF_`.
- 2026-09-30 · Incremental reads `watermark ≥ last value` (not `>`), relying on the MERGE
  for idempotence; timestamps compare in UTC (Snowflake session TIMEZONE = UTC).
- 2026-09-30 · Schedules are IST; storage is UTC; a missed run fires once on the next
  tick; a failed AUTO run retries once.
- 2026-09-30 · One clock: every "now" (next run, retry time, lock staleness, today's
  bounds) is read from SQL Server (`SELECT SYSUTCDATETIME()`), never the app server.
  The app server (HOPC575) ran 295 s behind SQL Server (HOPC866), so a schedule saved
  within that window was already overdue in SQL and re-fired on every tick (job 1 ran
  7 times in 90 s; job 4 started on save).
- 2026-10-01 · Bulk loader (bcp) is the default; the classic loader is the fallback and
  the run message says why it was used. Both loaders load the same specs, and on
  250,000 synthetic rows covering every type, the two tables were identical (EXCEPT
  both ways = 0).
- 2026-10-01 · Every run now verifies per-column non-null counts and number sums against
  Snowflake's own figures, not only the row count, before the live table is touched.
- 2026-10-01 · The source view is computed once per run: the profile reads the data
  query's stored result (`RESULT_SCAN`) instead of re-running the view.
- 2026-10-01 · bcp never takes a password on its command line. It logs in with Windows
  authentication when SQL Server accepts the backend's account, else with the SQL login
  via stdin. On 2026-10-01 HOPC866 refused `V2RD\Santosh.Kumar3` (18456, no login), so
  runs use the SQL login until the DBA adds it.
- 2026-10-01 · Bulk files end rows with 0x1E, not LF, so line breaks inside values load
  through bulk copy. DIM_PRODUCT had LF in 82 values across ARTICLE_DESC, GEN_ART_DESC,
  VENDOR_DESIGN_NO and M_FAB_1, and was forced to classic (18–24 min) until this change.
  TIME and VARIANT/OBJECT/ARRAY now go through bulk too; only BINARY stays classic.
- 2026-10-01 · Loader is a job setting (bulk default, classic optional).
- 2026-10-03 · Column mapping: rename or skip Snowflake columns per job. Renames on
  incremental/append tables happen in place (rows kept); new Snowflake columns load under
  their own name automatically. Tested: replace 3 renamed + 1 skipped identical through bulk
  and classic; incremental WERKS → ST_CD → STORE with all 1,000 rows and values kept.
- 2026-10-01 · Empty-source guard: a full replace that gets 0 rows while the local table
  has rows fails and keeps them, unless the job allows empty. Added after run 57: the
  Snowflake source AKS_GOLD.GLD_MONTH_PLAN_APPROVED was emptied upstream at 09:31:53 IST
  (3,676,320 → 0 rows, Time Travel retention 14 days) and the next run wiped 3,708,288
  local rows.
- 2026-10-01 · Text is VARCHAR when every value in the batch is plain ASCII
  (`[ -~\t\r\n]*` — printable, tab, CR, LF), else NVARCHAR. Incremental/append turn a VARCHAR column into NVARCHAR the
  first time a batch carries non-ASCII text, and widen only when the batch's measured
  longest value no longer fits (an all-NULL column never widens anything).
