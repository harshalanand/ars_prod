import HelpPage from '@/components/facons/HelpPage'

// Get Data — canonical spec: frontend/public/docs/manual/get_data.md.
const SECTIONS = [
  {
    id: 'what', emoji: '📥', title: 'What Get Data does',
    lead: 'It brings data **into** local SQL (Rep_data) from outside sources, on a schedule, when the source changes, or on demand, and records every run.',
    bullets: [
      'Phase 1 is **Snowflake**: create a view, then a sync job copies it into a local **GD_SF_** table.',
      'Excel, DataV2 and SAP follow in later phases, into the same jobs and history.',
      'All times are **IST**.',
    ],
  },
  {
    id: 'screens', emoji: '🧭', title: 'The screens',
    rows: [
      ['Overview', 'Sources, today’s runs, what runs next, and recent failures.'],
      ['Snowflake Views', 'Write a SELECT, **validate** it, preview 100 rows, then create it as a view in V2RETAIL.ARS_GETDATA. You can also browse every Snowflake schema read-only.'],
      ['Sync Jobs', 'Which view goes to which local table, how it loads, and when. **Run now** starts a run straight away.'],
      ['Run History', 'Every run with AUTO / MANUAL, who ran it, rows from Snowflake, rows loaded, time taken and any error. Export to Excel.'],
    ],
  },
  {
    id: 'views', emoji: '❄️', title: 'Creating a view — validate first',
    lead: 'Save stays switched off until the current SQL passes every check. The server checks again before it creates anything.',
    rows: [
      ['Read-only SELECT', 'Only SELECT or WITH, one statement. The module adds CREATE VIEW itself.'],
      ['Compiles', 'Snowflake must accept the query.'],
      ['Columns', 'Names must be unique (ignoring case) — give duplicates an alias. _GD_RUN_ID and _GD_LOADED_AT are reserved.'],
      ['Row count', 'Shown so you know the size before you schedule it.'],
      ['Where', 'Views are always created in **V2RETAIL.ARS_GETDATA** and named **V_GD_…** (added if you leave it out). Nothing else in Snowflake can be changed from here.'],
    ],
  },
  {
    id: 'modes', emoji: '🔁', title: 'Load modes',
    rows: [
      ['Full replace', 'Reloads everything. The new copy is loaded into a side table and swapped in only if its row count matches Snowflake — ARS never reads a half-loaded table, and a failed run leaves yesterday’s data in place.'],
      ['Incremental', 'Reads rows where the watermark column is **≥** the last loaded value and merges them on the key columns. The first run, and **Full reload**, load everything.'],
      ['Append', 'Adds every row of every run — a snapshot history. _GD_LOADED_AT tells the runs apart.'],
    ],
  },
  {
    id: 'onchange', emoji: '⚡', title: 'When the source changes',
    lead: 'Instead of fixed times, a job can run when its Snowflake tables change — but only when it is sensible to.',
    rows: [
      ['Watch these tables', '**Find tables** lists every table behind the source with its last change. Tick only the table(s) whose change should reload the job — a table that changes all day would reload it all day.'],
      ['Check every', 'How often ARS asks Snowflake (default 5 minutes). Asking costs no warehouse time.'],
      ['Wait until quiet', 'Runs only after the tables stop changing for this long (default 10 minutes), so a half-finished upstream reload is never copied. Several changes in a row become one run.'],
      ['Only between', 'The hours (and weekdays) a change may start a run, e.g. 06:00–22:00. A change outside them waits for the window to open.'],
      ['At most once every', 'The minimum gap between runs (default 1 hour).'],
      ['Safety run', 'Optional daily time when the job loads anyway — unless it already loaded in the last 23 hours. Insurance in case a change is missed.'],
      ['Status line', 'Under the trigger in Sync Jobs: up to date, waiting for quiet, waiting for the window, next run allowed at …, or an error (a watched table can’t be read — nothing runs until it is fixed).'],
    ],
  },
  {
    id: 'history', emoji: '🕘', title: 'Reading the run history',
    rows: [
      ['AUTO / MANUAL', 'AUTO = started by the schedule, its retry, a source change (“change · TABLE time”) or a safety run. MANUAL = Run now, with the user’s name.'],
      ['Stop', 'The ■ button on a running job ends the run within seconds and leaves the local table as it was. Turning a job off (power button) only stops future runs.'],
      ['Snowflake rows vs rows loaded', 'They must be equal. If not, the run fails with “Count mismatch” and the table is not changed.'],
      ['Skipped', 'A scheduled run found the previous run of the same job still going, so it did not start a second one.'],
      ['Retry', 'A failed scheduled run is retried once after the delay set on the job (default 15 minutes).'],
      ['Loader', '**Bulk copy** is the default and the fast way in; choose **Classic insert** on the job only if bulk copy gives trouble. A bulk job still uses classic when its data can’t go through bulk copy exactly — the run message says why. Both are checked the same way: row count, values per column and number totals must match Snowflake.'],
      ['Column names', 'In the job’s **Columns** section (after Check source) type a new name to **rename** a column in the local table, or untick Load to **skip** it. Full-replace tables get the new names on the next run; incremental and append tables are renamed in place, so no rows are lost. New Snowflake columns load under their own name, and the run says so.'],
      ['Empty result', 'If Snowflake returns **0 rows** for a full-replace job while the local table has data, the run fails and **keeps the data** — an emptied source is usually a mistake upstream. Tick “Allow an empty Snowflake result” on the job if empty is expected.'],
    ],
  },
  {
    id: 'good', emoji: 'ℹ️', title: 'Good to know',
    bullets: [
      'Schedules only fire while the ARS backend is running. A run missed while it was down starts once when it is back.',
      'Snowflake must be switched on in **Settings → Snowflake**.',
      'Deleting a job keeps its local table unless you tick “also drop”.',
    ],
  },
]

export default function GetDataHelpPage() {
  return <HelpPage title="Get Data · Help" tag="Guide" sections={SECTIONS}
    intro="A plain-language guide to Get Data — what it does, how each screen works, and how to read the run history." />
}
