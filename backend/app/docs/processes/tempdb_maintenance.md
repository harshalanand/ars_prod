---
title: TempDB Maintenance — Keeping SQL Server Lean
category: Admin
order: 90
source: backend/app/services/tempdb_cleanup_service.py, backend/app/api/v1/endpoints/maintenance.py, frontend/src/pages/TempDBAdminPage.jsx
last_reviewed: 2026-04-23
---

# TempDB Maintenance

> **═══ USE CASE & PURPOSE ═══**

## Why this page exists

ARS runs heavy SQL workloads — MSA calculations pivot tens of millions of rows, the Listing pipeline builds large `##` global temp tables, and exports stream big result sets. Every one of those operations borrows space from **SQL Server's `tempdb`**.

`tempdb` is the shared scratchpad of the whole SQL Server instance. When it fills up:

- **Every query slows down** — SQL Server spills sorts/joins to disk, hashes to disk, version-store bloats.
- **Jobs fail** — `Could not allocate space for object … in database 'tempdb' because the 'PRIMARY' filegroup is full`.
- **The disk fills up** — eventually the SQL Server host runs out of free space and the service refuses new connections.

Most shops find out the hard way: a planner starts an allocation at 6 p.m., tempdb grows to 20+ GB overnight, and by morning nothing works.

**This page is the control panel that keeps tempdb lean, automatically.**

## What the agent does on its own

A background daemon (`tempdb_cleaner`) runs inside the ARS backend. Every **5 minutes** it:

1. **Drops orphaned `##` temp tables** — ARS names its scratch tables `##upsert_temp_…`, `##merge_output_…`, `##bulk_stage_…`, etc. If a job crashes or its connection is reset, the table survives and leaks space. The agent finds tables older than `DB_TEMPDB_ORPHAN_AGE_MINUTES` (default 15 min) and drops them.
2. **Runs `DBCC SHRINKFILE(… , TRUNCATEONLY)`** on every tempdb data file. TRUNCATEONLY is a safe, fragmentation-free reclaim: it only gives back space already at the tail of the file.
3. **Escalates to aggressive shrink** if the total tempdb size crosses `DB_TEMPDB_AGGRESSIVE_THRESHOLD_MB` (default 20 GB). Aggressive mode:
   - `DBCC FREEPROCCACHE` — clears the plan cache so SQL Server drops its hold on tempdb pages.
   - `DBCC FREESYSTEMCACHE('ALL')` — clears system caches.
   - `DBCC SHRINKFILE(name, target_MB)` — hard-shrink every data file to the configured target (default 4 GB per file).
4. **Raises an ALERT** if size stays above `DB_TEMPDB_ALERT_THRESHOLD_MB` (default 40 GB) after cleanup. The alert banner appears at the top of this page until dismissed or until size drops below threshold.
5. **Records history** — last 96 runs (≈ 8 hours) are kept in memory for the trend chart.

You don't have to do anything. The agent runs quietly in the background. The page just gives you **visibility and manual override** when it's not enough.

## When to come to this page

| Symptom | What to do here |
|---|---|
| "My allocation job is slow/stalling" | Check **Long-Running Transactions** — a blocking txn is often the cause. |
| "Disk is filling up on the SQL server" | Look at the top **Allocated** card. If ≥ 20 GB, click **Aggressive Shrink**. |
| "Red ALERT banner is showing" | The agent says tempdb is above 40 GB and its own cleanup didn't help. Open **Long-Running Transactions** → KILL the culprit → wait a minute → **Run Cleanup Now**. |
| "I want to preview what cleanup would drop" | Click **Dry Run** — lists orphan candidates, changes nothing. |
| "Is the agent actually running?" | The **Service** KPI card shows `Running` (green) and when it last ran. |

## The 5 space-breakdown cards — read them like a doctor

When tempdb is big, the breakdown tells you **who's holding the space**. Each bucket needs a different remedy:

| Card | What it is | If it's the big one… |
|---|---|---|
| **User Objects** | Explicit `#` / `##` temp tables created by queries | A session has a zombie temp table. Find it in **Top Sessions** or **Long-Running Transactions** → KILL. |
| **Internal Work** | Work tables for sorts, hashes, spills | A big query is running. Let it finish, or KILL if it's wedged. |
| **Version Store** | Row versions for RCSI / snapshot isolation | A long-running transaction is holding back the cleanup. This is the **most common** cause of stuck shrink. Find it in **Long-Running Transactions** → KILL. |
| **Mixed Extents** | Pages shared across small allocations | Usually small. Nothing to act on. |
| **Free** | Unused space inside the file | Already reclaimable. If this is big but "Allocated" stays high, click **Aggressive Shrink**. |

## Manual actions — when and why

### Dry Run
Lists which orphan `##` tables would be dropped right now, but changes nothing. Safe. Use to confirm the agent's filters aren't matching a live temp table your job is currently using.

### Run Cleanup Now
Same as a scheduled cycle, but on demand. Drops orphans + TRUNCATEONLY shrink. Use after you've killed a stuck session to reclaim its space immediately instead of waiting 5 minutes.

### Aggressive Shrink
Flushes plan + system caches, then hard-shrinks every data file to the configured target size. Use when:

- Total allocated is well above 20 GB and you need space **now**.
- The periodic TRUNCATEONLY cycles are not releasing anything (Free card is small).

**Side effect:** for 30–60 seconds after, queries may feel slower because SQL Server recompiles plans that were just cleared from cache.

### KILL (Long-Running Transactions table)
Terminates a specific SQL Server session and rolls back its open transaction. Use when:

- A session has been open for 10+ minutes and you've identified it as the culprit.
- The session is blocking an ARS job (MSA, Listing, Allocation).
- You've verified with the planner that the session is safe to kill (or nobody owns it).

**Guardrails:** the API refuses to kill system sessions (`session_id ≤ 50`).

## What to do when the agent can't fix it (the nuclear option)

Rare — but if tempdb is pinned by system-internal state the agent can't touch, nothing beats a clean slate:

1. Announce a 30–60 second SQL outage (tell all users to stop submitting jobs).
2. On the SQL Server host, restart the service:
   ```
   net stop MSSQLSERVER && net start MSSQLSERVER
   ```
3. tempdb is **recreated from scratch** on every restart at its configured base size. All bloat disappears.

This is the only 100% guaranteed release. Do it off-hours.

## Preventing regrowth

If tempdb kept growing back to 15+ GB, the physical file sizes are wrong. A one-time fix (run as `sa` in SSMS):

```sql
USE master;
ALTER DATABASE tempdb MODIFY FILE (NAME = tempdev, SIZE = 2048MB, FILEGROWTH = 256MB);
ALTER DATABASE tempdb MODIFY FILE (NAME = temp3,   SIZE = 2048MB, FILEGROWTH = 256MB);
ALTER DATABASE tempdb MODIFY FILE (NAME = temp4,   SIZE = 2048MB, FILEGROWTH = 256MB);
ALTER DATABASE tempdb MODIFY FILE (NAME = temp5,   SIZE = 2048MB, FILEGROWTH = 256MB);
ALTER DATABASE tempdb MODIFY FILE (NAME = temp6,   SIZE = 2048MB, FILEGROWTH = 256MB);
ALTER DATABASE tempdb MODIFY FILE (NAME = temp7,   SIZE = 2048MB, FILEGROWTH = 256MB);
ALTER DATABASE tempdb MODIFY FILE (NAME = temp8,   SIZE = 2048MB, FILEGROWTH = 256MB);
-- Autogrowth in fixed MB, NOT percent. Restart SQL Server for the new baseline to take effect.
```

After the restart, tempdb starts at 16 GB baseline (8 files × 2 GB) and only grows in 256 MB steps — much easier for the agent to keep under control.

## Who can see this page

Superadmin only. The backend enforces `"SUPER_ADMIN"` role on every endpoint; the sidebar link is hidden for everyone else. Planners and analysts never see it.

## Configuration knobs

All tunable via `.env` (no code changes required):

| Setting | Default | What it does |
|---|---|---|
| `DB_TEMPDB_CLEANUP_INTERVAL_MINUTES` | `5` | How often the agent wakes up. |
| `DB_TEMPDB_ORPHAN_AGE_MINUTES` | `15` | Temp tables younger than this are assumed live and skipped. |
| `DB_TEMPDB_AGGRESSIVE_THRESHOLD_MB` | `20480` (20 GB) | Size at which the agent auto-escalates to aggressive shrink. |
| `DB_TEMPDB_ALERT_THRESHOLD_MB` | `40960` (40 GB) | Size at which the red ALERT banner appears. |
| `DB_TEMPDB_AGGRESSIVE_TARGET_MB` | `4096` (4 GB) | Target size per data file during aggressive shrink. |
| `DB_TEMPDB_HISTORY_SIZE` | `96` | Points kept for the trend chart (96 × 5 min = 8 h). |

## Bottom line

This page exists so nobody has to SSMS into the SQL server at 11 p.m. to babysit tempdb. The agent handles routine pressure automatically; when it can't (long-running txn, bloated version store), the page gives a superadmin the 2–3 buttons needed to recover in under a minute — without opening Management Studio.
