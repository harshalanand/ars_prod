# Dev-server task: chunk the Approve/Revert promote so large sessions stop failing with "transaction log full"

Paste everything below into a Claude Code session on the dev server.

---

## Context

On prod (`HOPC866` / `Rep_Data`), Approve on listing session `20260810_152630_463`
failed on every attempt for ~4 hours (17:43 → 19:34 on 2026-08-10). Root cause is
**not** a rule-engine or data problem — it is transaction-log exhaustion in
`backend/app/services/parked_history.py`.

That one session was rescued **by hand at the data level on 2026-08-10 20:27** (per-store
chunked promote run outside the app, then `approve_parked()` for the tail). **No
application code was changed.** Your job is to productionise that workaround so it
never needs doing by hand again.

### Root cause

`approve_parked()` (`parked_history.py:715-723`) promotes all four snapshot targets
inside ONE transaction:

| target | rows for that session |
|---|---|
| alloc | 2,751,299 |
| listing_working | 5,075,949 |
| listing | 5,075,949 |
| msa_total | 148,155 |

`Rep_Data` is SIMPLE recovery and its log file is **hard-capped at 16 GB**
(`sys.database_files`: `size` = `max_size` = 16384 MB, so it cannot autogrow). A log
cannot truncate while a transaction is open, so an INSERT + DELETE of 5.07M rows over
a 232-column table blows the cap:

```
Msg 9002 — The transaction log for database 'Rep_Data' is full due to 'ACTIVE_TRANSACTION'
```

Failures landed on `INSERT INTO ARS_LISTING_WORKING_HISTORY` (17:47:08) and on
`DELETE FROM ARS_LISTING_WORKING_PARKED` (18:07:02, 19:34:12). Sessions of 2.35M and
2.82M rows approved fine earlier the same day — the ceiling sits somewhere near 3M rows.

### Two things that make the symptom misleading

1. **Approve is NOT atomic across targets, despite the docstring saying it is.**
   `_reconcile_history_columns()` calls `conn.commit()` after each
   `ALTER TABLE … ADD COLUMN` (`parked_history.py:313`). When the loop reaches target N
   and adds a column, that commit flushes target N-1's already-completed INSERT+DELETE.
   So the log-full failure left the session **half-promoted**: alloc committed into
   history, the other three still parked, and — because the `APPROVE` op row is written
   only at the very end — no row in `ARS_PEND_ALC_OPERATIONS`, empty `ARS_PEND_ALC`, hold
   tracking never applied, MSA never deducted.
2. Retrying while a doomed ~20-minute attempt is still grinding returns
   `sp_getapplock -1` → *"another approve already in progress"*. That is a collision
   symptom, not the cause. The real error is `Msg 9002` in `backend/logs/app.log`.

### The revert path has the same bug

`_demote_one_within_conn()` (`parked_history.py:501-556`) does an unchunked
`INSERT INTO parked SELECT … FROM history` + `DELETE FROM history` for the whole
session. It already failed this way in prod on 2026-08-10 15:05 for session
`20260810_092457_375`:

```
[revert] APPROVE history→parked demote failed: … The transaction log for database
'Rep_Data' is full due to 'ACTIVE_TRANSACTION'
```

Fix both directions in one change.

---

## Required changes

All in `backend/app/services/parked_history.py`.

### 1. Chunk `_promote_one_within_conn()` (`:447-498`)

Move parked → history in committed batches so the log truncates between them. Follow the
pattern `reject_parked()` already uses at `:938-965` (`DELETE TOP (BATCH)` … `conn.commit()`
in a loop, idempotent because of the `PARK_STATUS='PARKED'` filter).

The hand-run on prod chunked by `WERKS` — 369 chunks of ~13.7k rows, ~3,000 rows/s,
5.07M rows in ~1,615s for `listing_working` and ~348s for `listing`. Either partition by
`WERKS` or use `DELETE TOP (N) … OUTPUT deleted.* INTO history` (verified safe: these
tables have **no triggers and no identity/computed columns**). `OUTPUT … INTO` is the
better shape — it needs no partition key and moves each batch atomically. Prefer it
unless you hit a blocker.

**Critical:** INSERT and DELETE for a given batch must commit **together**. Never a
batch where rows landed in history but weren't deleted from parked (duplicates on
retry), or deleted from parked without landing in history (data loss).

### 2. Replace the whole-session `NOT EXISTS` guard

The current guard is
`AND NOT EXISTS (SELECT 1 FROM history H WHERE H.SESSION_ID = :sid)` — "insert nothing if
this session has *any* history row". That is incompatible with chunking: after batch 1
commits, every later batch would insert 0 rows while the unconditional
`DELETE FROM parked WHERE SESSION_ID = :sid` still deletes them. **Silent data loss.**

Same hazard in `_demote_one_within_conn()`: its `NOT EXISTS` is on the parked side, its
`DELETE FROM history` is unconditional.

Per-batch atomicity (item 1) supplies the idempotency the guard was there for — rows leave
parked in the same transaction that puts them in history, so a retry naturally resumes
from what's left. Keep the `sp_getapplock` serialisation in `approve_parked()` as the
cross-request guard.

### 3. Chunk `_demote_one_within_conn()` (`:501-556`) the same way

Same batching, same commit-together rule, same guard replacement, direction reversed
(history → parked, `PARK_STATUS='PARKED'`).

### 4. Fix the atomicity claims in the docstrings

The module docstring (`:27-32`) and `approve_parked()`'s (`:572-578`) both promise
all-or-nothing across targets. That has never been true because of the reconcile commit at
`:313`, and chunking makes it emphatically untrue. Replace with what actually holds:
per-batch atomic, resumable, `sp_getapplock` + the durable `APPROVE`-op guard prevent
double-approve. Same for `revert_approved_to_parked()` (`:559-563`, "Atomic across
targets").

Do **not** try to restore real cross-target atomicity — that is what caused this bug.

### 5. Surface the real error to the UI

`Msg 9002` currently reaches the user as a generic 400. Detect it in
`approve_parked()`'s `except` and return something actionable, e.g. *"Transaction log
full — session too large to promote in one pass"*, so the next person doesn't spend hours
re-clicking Approve.

---

## Do NOT

- **Do not pre-promote all four targets** as a shortcut. `approve_parked()`
  short-circuits on `len(already_in_history) == len(_SNAPSHOT_TARGETS)` (`:701-708`) and
  returns `already_approved` **without** running `write_pend_alc`, the hold pre-snapshot,
  hold tracking Step A/B, the ±1 MSA/Grid delta, MSA hold sync, or the `APPROVE` op row.
  The session would look approved and be functionally unapproved. (The prod rescue
  deliberately left `msa_total` parked for exactly this reason.)
- **Do not just raise `Rep_Data_log` MAXSIZE.** D: has 682 GB free so it would work today,
  but it only relocates the ceiling — a 10M-row session finds it again. Mention it in the
  PR as an optional prod-side safety net, not as the fix.
- **Do not touch the rule engine, MSA, or hold logic.** This is purely the
  parked→history data-movement layer.

---

## Acceptance criteria

1. A session of ≥5M listing rows approves end-to-end on dev without `Msg 9002`.
2. Peak log usage stays flat during the promote (sample `sys.dm_db_log_space_usage`
   mid-run) instead of climbing to 100%.
3. Post-approve invariants, per target: `parked = 0`, `history = original parked count`.
4. The approve tail all ran — verify, don't trust the return value:
   - `ARS_PEND_ALC` row count and `SUM(ALLOC_QTY)` match the session's `ALLOC_ROWS` and
     `SHIP_QTY_TOTAL` in `ARS_LISTING_SESSIONS` (prod reference: 366,179 rows /
     1,473,765 units, matched exactly)
   - one `APPROVE` row in `ARS_PEND_ALC_OPERATIONS` with `REVERTED_AT IS NULL`
   - hold tracking Step A/B row counts non-zero, pre-approve snapshot present
5. **Kill-and-resume test:** kill the process mid-promote, re-run Approve, and confirm it
   resumes to a correct final state with no duplicated and no lost rows.
6. **Double-click test:** two concurrent Approve POSTs → one proceeds, the other gets
   `already_approved` or the applock message. Never two `APPROVE` op rows, never 2×
   `ARS_PEND_ALC` rows.
7. Revert-approve on the same large session completes without `Msg 9002`, and restores
   parked/history to their pre-approve counts.

Note `ARS_LISTING_SESSIONS.PARKED_STATUS` stays `'PARKED'` even after a successful
approve — that is a generate-time stamp approve never updates, true of all previously
approved sessions. Don't "fix" it as part of this task; the UI's pending check reads
`ARS_ALLOC_PARKED`.

## Per CLAUDE.md

Update `frontend/public/docs/manual/listing.md` (FSD + `## Recorded rules`) with the
chunked promote/demote behaviour and the fact that promote is per-batch atomic rather
than all-or-nothing. Keep `.claude/agents/ars_flow_kb/listing.md` consistent — the
dossier wins.
