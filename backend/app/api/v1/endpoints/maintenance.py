"""
Maintenance Endpoints
=====================
Superadmin-only API for TempDB monitoring, manual cleanup, aggressive
reclaim, trend history, and session diagnostics.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger

from app.core.config import get_settings
from app.security.dependencies import get_current_user
from app.services.tempdb_cleanup_service import tempdb_cleaner
from app.database.session import get_data_engine

settings = get_settings()
router = APIRouter(prefix="/maintenance", tags=["Maintenance"])


def _require_superadmin(current_user=Depends(get_current_user)):
    """Restrict access to superadmin accounts only."""
    role_codes = set(getattr(current_user, "role_codes", []) or [])
    if "SUPER_ADMIN" not in role_codes:
        raise HTTPException(status_code=403, detail="Superadmin access required")
    return current_user


@router.get("/tempdb/status", summary="TempDB cleanup service status")
def get_tempdb_cleanup_status(_user=Depends(_require_superadmin)):
    """Return the cleanup service configuration and stats from the last run."""
    return tempdb_cleaner.status


@router.get("/tempdb/history", summary="Recent TempDB cleanup runs (for trend chart)")
def get_tempdb_history(_user=Depends(_require_superadmin)):
    """Return the in-memory history of recent cleanup runs."""
    return {"history": tempdb_cleaner.history}


@router.post("/tempdb/cleanup", summary="Trigger TempDB cleanup now")
def trigger_tempdb_cleanup(
    dry_run: bool = Query(False, description="Preview orphaned tables without dropping them"),
    _user=Depends(_require_superadmin),
):
    """
    Trigger a TempDB cleanup immediately (blocking).
    Use dry_run=true to see what would be dropped without making any changes.
    """
    logger.info(f"Manual TempDB cleanup triggered by admin (dry_run={dry_run})")
    try:
        stats = tempdb_cleaner.run_now(dry_run=dry_run)
        return {"success": True, "stats": stats}
    except Exception as exc:
        logger.error(f"Manual TempDB cleanup failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/tempdb/aggressive-shrink", summary="Force aggressive shrink now")
def trigger_tempdb_aggressive_shrink(_user=Depends(_require_superadmin)):
    """
    Force an aggressive reclaim regardless of current size:
    flush procedure/system caches then hard SHRINKFILE every tempdb data
    file to the configured target size.
    Use when the periodic TRUNCATEONLY is not releasing enough space.
    """
    logger.warning("Manual aggressive TempDB shrink triggered by admin")
    try:
        stats = tempdb_cleaner.aggressive_shrink_now()
        return {"success": True, "stats": stats}
    except Exception as exc:
        logger.error(f"Manual aggressive TempDB shrink failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/tempdb/size", summary="Current TempDB file sizes + usage breakdown")
def get_tempdb_size(_user=Depends(_require_superadmin)):
    """
    Return per-file allocation and a tempdb-wide usage breakdown from
    sys.dm_db_file_space_usage (user objects, internal, version store, free).
    FILEPROPERTY requires tempdb context, so we USE tempdb on a dedicated
    autocommit connection and invalidate it after.
    """
    engine = get_data_engine()
    shrink_fairy = None
    try:
        shrink_fairy = engine.raw_connection()
        pyodbc_conn = shrink_fairy.driver_connection
        pyodbc_conn.autocommit = True
        cursor = pyodbc_conn.cursor()
        cursor.execute("USE tempdb")

        # Per-file allocation
        cursor.execute("""
            SELECT
                f.name                                           AS file_name,
                f.type_desc                                      AS file_type,
                f.size * 8.0 / 1024                             AS allocated_mb,
                FILEPROPERTY(f.name, 'SpaceUsed') * 8.0 / 1024  AS used_mb,
                (f.size - FILEPROPERTY(f.name, 'SpaceUsed')) * 8.0 / 1024 AS free_mb
            FROM tempdb.sys.database_files f
            WHERE f.type_desc IN ('ROWS', 'LOG');
        """)
        cols = [c[0] for c in cursor.description]
        files = [dict(zip(cols, row)) for row in cursor.fetchall()]

        # DB-wide breakdown (MB) — tells you where the space went
        cursor.execute("""
            SELECT
                SUM(user_object_reserved_page_count)     * 8.0 / 1024 AS user_objects_mb,
                SUM(internal_object_reserved_page_count) * 8.0 / 1024 AS internal_objects_mb,
                SUM(version_store_reserved_page_count)   * 8.0 / 1024 AS version_store_mb,
                SUM(mixed_extent_page_count)             * 8.0 / 1024 AS mixed_extent_mb,
                SUM(unallocated_extent_page_count)       * 8.0 / 1024 AS unallocated_mb
            FROM sys.dm_db_file_space_usage;
        """)
        row = cursor.fetchone()
        breakdown = {
            "user_objects_mb":      float(row[0] or 0),
            "internal_objects_mb":  float(row[1] or 0),
            "version_store_mb":     float(row[2] or 0),
            "mixed_extent_mb":      float(row[3] or 0),
            "unallocated_mb":       float(row[4] or 0),
        }

        return {"files": files, "breakdown": breakdown}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        # Don't leak the "USE tempdb" context back to the pool.
        if shrink_fairy:
            try:
                shrink_fairy.invalidate()
            except Exception:
                pass


@router.get("/tempdb/sessions", summary="Top tempdb-consuming sessions")
def get_tempdb_sessions(_user=Depends(_require_superadmin)):
    """Top 10 live sessions ranked by tempdb pages allocated — for diagnostics."""
    try:
        return {"sessions": tempdb_cleaner.top_sessions()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/tempdb/alert/clear", summary="Clear the current TempDB alert")
def clear_tempdb_alert(_user=Depends(_require_superadmin)):
    """Dismiss the current ALERT banner. Auto-re-raises if size stays over threshold."""
    tempdb_cleaner.clear_alert()
    return {"success": True}


@router.get("/tempdb/long-transactions", summary="Open transactions pinning tempdb space")
def get_long_transactions(_user=Depends(_require_superadmin)):
    """
    Return open transactions against tempdb (database_id = 2) or any DB,
    oldest first. A long-running transaction is the usual cause of a
    bloated version_store and stuck SHRINKFILE.

    Returns rows from sys.dm_tran_database_transactions joined with
    sys.dm_exec_sessions + sys.dm_exec_requests.
    """
    engine = get_data_engine()
    raw_conn = engine.raw_connection()
    try:
        cursor = raw_conn.cursor()
        cursor.execute("""
            SELECT TOP 20
                s.session_id,
                ISNULL(r.status, 'sleeping')                      AS status,
                ISNULL(s.login_name, '')                          AS login_name,
                ISNULL(s.host_name, '')                           AS host_name,
                ISNULL(s.program_name, '')                        AS program_name,
                DB_NAME(dt.database_id)                           AS database_name,
                dt.database_id,
                DATEDIFF(MINUTE, dt.database_transaction_begin_time, GETDATE())
                                                                   AS mins_open,
                dt.database_transaction_begin_time                AS begin_time,
                dt.database_transaction_log_bytes_used / 1024.0 / 1024.0
                                                                   AS log_mb,
                dt.database_transaction_log_record_count          AS log_records,
                s.last_request_start_time                         AS last_request_start,
                ISNULL(r.command, '')                             AS command,
                ISNULL(r.wait_type, '')                           AS wait_type
            FROM sys.dm_tran_database_transactions dt
            INNER JOIN sys.dm_tran_session_transactions st
                   ON dt.transaction_id = st.transaction_id
            INNER JOIN sys.dm_exec_sessions s
                   ON st.session_id = s.session_id
            LEFT  JOIN sys.dm_exec_requests r
                   ON s.session_id = r.session_id
            WHERE s.session_id > 50
            ORDER BY dt.database_transaction_begin_time ASC;
        """)
        cols = [c[0] for c in cursor.description]
        rows = []
        for row in cursor.fetchall():
            d = dict(zip(cols, row))
            for k in ("begin_time", "last_request_start"):
                if d.get(k):
                    d[k] = d[k].isoformat()
            rows.append(d)
        return {"transactions": rows}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        raw_conn.close()


@router.post("/tempdb/kill-session/{session_id}", summary="KILL a SQL session")
def kill_session(session_id: int, _user=Depends(_require_superadmin)):
    """
    Issue a KILL against the given SQL Server session_id. Use this when a
    long-running transaction is pinning tempdb space and you cannot wait
    for it to finish. Only sessions with session_id > 50 (user sessions)
    can be killed; system sessions are rejected.
    """
    if session_id <= 50:
        raise HTTPException(
            status_code=400,
            detail="Refusing to kill system session (session_id <= 50)",
        )

    engine = get_data_engine()
    shrink_fairy = None
    try:
        shrink_fairy = engine.raw_connection()
        pyodbc_conn = shrink_fairy.driver_connection
        pyodbc_conn.autocommit = True  # KILL cannot run in a user transaction
        cursor = pyodbc_conn.cursor()
        logger.warning(f"KILL {session_id} issued by admin")
        cursor.execute(f"KILL {int(session_id)}")
        return {"success": True, "session_id": session_id}
    except Exception as exc:
        logger.error(f"KILL {session_id} failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if shrink_fairy:
            try:
                shrink_fairy.invalidate()
            except Exception:
                pass


# =============================================================================
# Database File Maintenance (Rep_Data log + data files)
# =============================================================================
# Whitelist of databases the UI is allowed to manage. Derived from settings
# so deployments with different DB names work without code changes.
# Comparisons are case-insensitive (SQL Server default collation).
def _managed_dbs() -> dict:
    """Return {UPPER: actual_name} so case-insensitive lookup is O(1)."""
    return {
        settings.DB_NAME.upper():      settings.DB_NAME,
        settings.DATA_DB_NAME.upper(): settings.DATA_DB_NAME,
    }


# Sane bounds for shrink target — prevents an admin from accidentally setting
# the log to 1 MB (would cause autogrow storms) or 1 TB (no-op).
_MIN_SHRINK_MB = 64
_MAX_SHRINK_MB = 65536  # 64 GB


def _validate_db(db_name: str) -> str:
    """Case-insensitive allowlist check. Returns the canonical case."""
    managed = _managed_dbs()
    canonical = managed.get(db_name.upper())
    if not canonical:
        raise HTTPException(
            status_code=400,
            detail=f"Database '{db_name}' is not managed. Allowed: {sorted(managed.values())}",
        )
    return canonical


@router.get("/db/files", summary="Per-database file sizes + recovery model")
def get_db_files(_user=Depends(_require_superadmin)):
    """
    Return file allocation + recovery model + log_reuse_wait_desc for the
    managed ARS databases. Read from sys.master_files (server-wide view) so
    a single connection can report on every DB without USE statements.
    """
    engine = get_data_engine()
    raw_conn = engine.raw_connection()
    try:
        cursor = raw_conn.cursor()
        managed_names = list(_managed_dbs().values())
        placeholders = ",".join("?" * len(managed_names))
        cursor.execute(f"""
            SELECT
                d.name                                            AS db_name,
                d.recovery_model_desc                             AS recovery_model,
                d.log_reuse_wait_desc                             AS log_reuse_wait,
                d.state_desc                                      AS state,
                mf.name                                           AS file_name,
                mf.type_desc                                      AS file_type,
                mf.size       * 8.0 / 1024                        AS allocated_mb,
                CASE
                    WHEN mf.max_size = -1 THEN -1
                    WHEN mf.max_size =  0 THEN 0
                    ELSE mf.max_size * 8.0 / 1024
                END                                               AS max_size_mb,
                mf.growth                                         AS growth_raw,
                mf.is_percent_growth                              AS is_percent_growth,
                mf.physical_name                                  AS physical_path
            FROM sys.master_files mf
            INNER JOIN sys.databases d ON d.database_id = mf.database_id
            WHERE d.name IN ({placeholders})
            ORDER BY d.name, mf.type_desc DESC, mf.file_id;
        """, managed_names)
        cols = [c[0] for c in cursor.description]
        rows = [dict(zip(cols, r)) for r in cursor.fetchall()]

        # Group by database for an easier UI render
        by_db = {}
        for r in rows:
            db = r["db_name"]
            entry = by_db.setdefault(db, {
                "db_name":        db,
                "recovery_model": r["recovery_model"],
                "log_reuse_wait": r["log_reuse_wait"],
                "state":          r["state"],
                "data_mb":        0.0,
                "log_mb":         0.0,
                "files":          [],
            })
            entry["files"].append({
                "file_name":     r["file_name"],
                "file_type":     r["file_type"],
                "allocated_mb":  float(r["allocated_mb"] or 0),
                "max_size_mb":   float(r["max_size_mb"] or 0),
                "physical_path": r["physical_path"],
            })
            if r["file_type"] == "ROWS":
                entry["data_mb"] += float(r["allocated_mb"] or 0)
            elif r["file_type"] == "LOG":
                entry["log_mb"] += float(r["allocated_mb"] or 0)
        return {"databases": list(by_db.values())}
    except Exception as exc:
        logger.error(f"get_db_files failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        raw_conn.close()


@router.post("/db/{db_name}/checkpoint", summary="Force a CHECKPOINT")
def db_checkpoint(db_name: str, _user=Depends(_require_superadmin)):
    """
    Issue a CHECKPOINT against the target DB. In SIMPLE recovery this also
    truncates the inactive portion of the log so SHRINKFILE can reclaim it.
    """
    db_name = _validate_db(db_name)
    engine = get_data_engine()
    shrink_fairy = None
    try:
        shrink_fairy = engine.raw_connection()
        pyodbc_conn = shrink_fairy.driver_connection
        pyodbc_conn.autocommit = True
        cursor = pyodbc_conn.cursor()
        cursor.execute(f"USE [{db_name}]")
        cursor.execute("CHECKPOINT")
        logger.info(f"CHECKPOINT issued on {db_name} by admin")
        return {"success": True, "db_name": db_name}
    except Exception as exc:
        logger.error(f"CHECKPOINT on {db_name} failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if shrink_fairy:
            try:
                shrink_fairy.invalidate()
            except Exception:
                pass


@router.post("/db/{db_name}/shrink-log", summary="Shrink the log file")
def db_shrink_log(
    db_name: str,
    target_mb: int = Query(4096, ge=_MIN_SHRINK_MB, le=_MAX_SHRINK_MB,
                           description="Target log size in MB"),
    _user=Depends(_require_superadmin),
):
    """
    CHECKPOINT then SHRINKFILE on the log file of `db_name`. Will fail with
    a clear message if the log is held by LOG_BACKUP (DB is in FULL recovery
    and no log backup has been taken) — switch to SIMPLE or take a log
    backup first.

    Returns size before/after and the bytes freed.
    """
    db_name = _validate_db(db_name)
    engine = get_data_engine()
    shrink_fairy = None
    try:
        shrink_fairy = engine.raw_connection()
        pyodbc_conn = shrink_fairy.driver_connection
        pyodbc_conn.autocommit = True
        cursor = pyodbc_conn.cursor()
        cursor.execute(f"USE [{db_name}]")

        # Find the log file's logical name and current size
        cursor.execute("""
            SELECT name, size * 8.0 / 1024 AS mb
            FROM sys.database_files
            WHERE type_desc = 'LOG';
        """)
        log_files = cursor.fetchall()
        if not log_files:
            raise HTTPException(status_code=500, detail="No log file found")

        # Capture log_reuse_wait so we can give a useful error
        cursor.execute(
            "SELECT log_reuse_wait_desc FROM sys.databases WHERE name = ?",
            db_name,
        )
        wait_desc = (cursor.fetchone() or [""])[0] or ""

        results = []
        for log_name, before_mb in log_files:
            current_mb = float(before_mb or 0)
            passes = 0
            error: str | None = None
            try:
                # Up to 3 CHECKPOINT+SHRINK passes — when the active VLF sits
                # at the end of the file the first SHRINK only reorganises;
                # the next CHECKPOINT marks it inactive so a second SHRINK
                # can release it. 3 passes covers the common cases.
                for _ in range(3):
                    cursor.execute("CHECKPOINT")
                    cursor.execute(f"DBCC SHRINKFILE (N'{log_name}', {int(target_mb)})")
                    cursor.execute(
                        "SELECT size * 8.0 / 1024 FROM sys.database_files WHERE name = ?",
                        log_name,
                    )
                    new_mb = float(cursor.fetchone()[0] or 0)
                    passes += 1
                    if new_mb <= target_mb + 16 or new_mb >= current_mb:
                        # Either reached target (within 16 MB tolerance for VLF
                        # boundaries) or no further progress this pass — stop.
                        current_mb = new_mb
                        break
                    current_mb = new_mb
            except Exception as inner:
                error = str(inner)

            entry = {
                "file_name":  log_name,
                "before_mb":  float(before_mb or 0),
                "after_mb":   current_mb,
                "freed_mb":   float(before_mb or 0) - current_mb,
                "passes":     passes,
            }
            if error:
                entry["error"] = error
            results.append(entry)

        total_freed = sum(r.get("freed_mb", 0) for r in results)
        logger.info(
            f"shrink-log on {db_name}: target={target_mb}MB freed={total_freed}MB "
            f"reuse_wait={wait_desc}"
        )

        # If we couldn't free anything and the cause is LOG_BACKUP, return 409
        if total_freed <= 1 and wait_desc == "LOG_BACKUP":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Log on {db_name} is held by LOG_BACKUP. Either take a log "
                    f"backup or switch recovery to SIMPLE before shrinking."
                ),
            )

        return {
            "success":         True,
            "db_name":         db_name,
            "target_mb":       target_mb,
            "log_reuse_wait":  wait_desc,
            "files":           results,
            "total_freed_mb":  total_freed,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"shrink-log on {db_name} failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if shrink_fairy:
            try:
                shrink_fairy.invalidate()
            except Exception:
                pass


@router.post("/db/{db_name}/clear-log-backup-wait", summary="Auto-clear LOG_BACKUP wait")
def db_clear_log_backup_wait(
    db_name: str,
    target_mb: int = Query(4096, ge=64, le=131072,
                           description="Target log size after shrink"),
    _user=Depends(_require_superadmin),
):
    """
    One-shot resolver for `log_reuse_wait_desc = LOG_BACKUP`. Switches the DB to
    SIMPLE recovery (so the log auto-truncates on every CHECKPOINT), forces a
    CHECKPOINT, then SHRINKFILE the log to `target_mb`.

    The DB is left in SIMPLE — that's the entire point: no more LOG_BACKUP
    waits, no recurring space pressure. Point-in-time recovery between full
    backups is given up in exchange.
    """
    db_name = _validate_db(db_name)
    engine = get_data_engine()
    shrink_fairy = None
    try:
        shrink_fairy = engine.raw_connection()
        pyodbc_conn = shrink_fairy.driver_connection
        pyodbc_conn.autocommit = True
        cur = pyodbc_conn.cursor()

        cur.execute(
            "SELECT recovery_model_desc, log_reuse_wait_desc "
            "FROM sys.databases WHERE name = ?", db_name,
        )
        row = cur.fetchone() or (None, None)
        recovery_before, wait_before = row[0], row[1]

        cur.execute(f"USE [{db_name}]")
        cur.execute("""
            SELECT name, size * 8.0 / 1024 FROM sys.database_files
            WHERE type_desc = 'LOG'
        """)
        log_files = cur.fetchall()
        if not log_files:
            raise HTTPException(status_code=500, detail="No log file found")
        log_name, before_mb = log_files[0][0], float(log_files[0][1] or 0)

        # 1. SIMPLE recovery — required to make the log self-truncating
        if recovery_before != "SIMPLE":
            cur.execute(f"ALTER DATABASE [{db_name}] SET RECOVERY SIMPLE")
            logger.warning(
                f"clear-log-backup-wait: {db_name} switched FULL→SIMPLE by admin"
            )

        # 2. CHECKPOINT marks the active VLF inactive so SHRINK can release it
        cur.execute("CHECKPOINT")

        # 3. Up to 3 SHRINK passes — first pass usually only reorganises,
        #    the follow-ups release the now-inactive VLFs.
        current_mb = before_mb
        for _ in range(3):
            cur.execute("CHECKPOINT")
            cur.execute(f"DBCC SHRINKFILE (N'{log_name}', {int(target_mb)}) WITH NO_INFOMSGS")
            cur.execute(
                "SELECT size * 8.0 / 1024 FROM sys.database_files WHERE name = ?",
                log_name,
            )
            new_mb = float(cur.fetchone()[0] or 0)
            if new_mb <= target_mb + 16 or new_mb >= current_mb:
                current_mb = new_mb
                break
            current_mb = new_mb

        return {
            "success":         True,
            "db_name":         db_name,
            "recovery_before": recovery_before,
            "recovery_after":  "SIMPLE",
            "wait_before":     wait_before,
            "before_mb":       before_mb,
            "after_mb":        current_mb,
            "freed_mb":        before_mb - current_mb,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"clear-log-backup-wait on {db_name} failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if shrink_fairy:
            try:
                shrink_fairy.invalidate()
            except Exception:
                pass


@router.post("/db/{db_name}/recovery", summary="Set recovery model")
def db_set_recovery(
    db_name: str,
    model: str = Query(..., pattern="^(SIMPLE|FULL|BULK_LOGGED)$",
                       description="Target recovery model"),
    confirm: bool = Query(False, description="Required to be true"),
    _user=Depends(_require_superadmin),
):
    """
    Switch recovery model on the target DB. SIMPLE makes the log auto-truncate
    at every checkpoint (no point-in-time recovery). FULL/BULK_LOGGED require
    log backups or the log will fill (you'll get error 9002).

    Requires confirm=true to avoid accidental clicks from the UI.
    """
    db_name = _validate_db(db_name)
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm=true is required (changing recovery model has consequences)",
        )

    engine = get_data_engine()
    shrink_fairy = None
    try:
        shrink_fairy = engine.raw_connection()
        pyodbc_conn = shrink_fairy.driver_connection
        pyodbc_conn.autocommit = True
        cursor = pyodbc_conn.cursor()
        cursor.execute(f"ALTER DATABASE [{db_name}] SET RECOVERY {model}")
        logger.warning(f"Recovery model on {db_name} set to {model} by admin")
        return {"success": True, "db_name": db_name, "recovery_model": model}
    except Exception as exc:
        logger.error(f"set recovery on {db_name} failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if shrink_fairy:
            try:
                shrink_fairy.invalidate()
            except Exception:
                pass


@router.post("/db/{db_name}/backup-log", summary="Take a transaction log backup")
def db_backup_log(
    db_name: str,
    backup_path: str = Query(..., min_length=4,
                             description="Full path to .trn file (e.g. E:\\\\SQLBackups\\\\Rep_Data_log.trn)"),
    _user=Depends(_require_superadmin),
):
    """
    BACKUP LOG so the log can be reused. Required when the DB is in FULL
    recovery and you don't want to switch to SIMPLE. Path must already exist
    and be writable by the SQL Server service account.
    """
    db_name = _validate_db(db_name)
    # Disallow path-traversal-ish input; backup_path is user input that flows
    # into a string-formatted T-SQL statement. Restrict to plausible shapes.
    if any(ch in backup_path for ch in ("'", '"', ";", "\n", "\r")):
        raise HTTPException(status_code=400, detail="backup_path contains illegal characters")

    engine = get_data_engine()
    shrink_fairy = None
    try:
        shrink_fairy = engine.raw_connection()
        pyodbc_conn = shrink_fairy.driver_connection
        pyodbc_conn.autocommit = True
        cursor = pyodbc_conn.cursor()
        cursor.execute(
            f"BACKUP LOG [{db_name}] TO DISK = ? WITH NOFORMAT, NOINIT, COMPRESSION, CHECKSUM",
            backup_path,
        )
        logger.info(f"BACKUP LOG {db_name} -> {backup_path} by admin")
        return {"success": True, "db_name": db_name, "backup_path": backup_path}
    except Exception as exc:
        logger.error(f"backup-log on {db_name} failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if shrink_fairy:
            try:
                shrink_fairy.invalidate()
            except Exception:
                pass


@router.post("/db/{db_name}/shrink-data", summary="Shrink a data file (rarely needed)")
def db_shrink_data(
    db_name: str,
    file_name: str = Query(..., description="Logical data file name (e.g. Rep_Data)"),
    target_mb: int = Query(..., ge=_MIN_SHRINK_MB, le=_MAX_SHRINK_MB,
                           description="Target file size in MB"),
    _user=Depends(_require_superadmin),
):
    """
    SHRINKFILE on a ROWS data file. Use sparingly — shrinking data files
    causes index fragmentation. Useful only when a large amount of data was
    deleted and the disk needs reclaiming.
    """
    db_name = _validate_db(db_name)
    engine = get_data_engine()
    shrink_fairy = None
    try:
        shrink_fairy = engine.raw_connection()
        pyodbc_conn = shrink_fairy.driver_connection
        pyodbc_conn.autocommit = True
        cursor = pyodbc_conn.cursor()
        cursor.execute(f"USE [{db_name}]")
        cursor.execute(
            "SELECT size * 8.0 / 1024 FROM sys.database_files "
            "WHERE name = ? AND type_desc = 'ROWS'",
            file_name,
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=400, detail=f"Data file '{file_name}' not found")
        before_mb = float(row[0] or 0)
        cursor.execute(f"DBCC SHRINKFILE (N'{file_name}', {int(target_mb)}) WITH NO_INFOMSGS")
        cursor.execute(
            "SELECT size * 8.0 / 1024 FROM sys.database_files WHERE name = ?",
            file_name,
        )
        after_mb = float(cursor.fetchone()[0] or 0)
        logger.info(f"shrink-data {db_name}.{file_name}: {before_mb:.0f} -> {after_mb:.0f} MB")
        return {
            "success":   True,
            "db_name":   db_name,
            "file_name": file_name,
            "before_mb": before_mb,
            "after_mb":  after_mb,
            "freed_mb":  before_mb - after_mb,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"shrink-data on {db_name}.{file_name} failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if shrink_fairy:
            try:
                shrink_fairy.invalidate()
            except Exception:
                pass


@router.post("/db/{db_name}/set-log-maxsize", summary="Cap log autogrowth")
def db_set_log_maxsize(
    db_name: str,
    max_mb: int = Query(..., ge=512, le=131072,
                        description="Max log size in MB (512 - 131072). Use 0 for UNLIMITED (NOT recommended)."),
    _user=Depends(_require_superadmin),
):
    """
    Cap log autogrowth so a single runaway transaction can't fill the disk.
    Sets MAXSIZE on the LOG file via ALTER DATABASE ... MODIFY FILE.
    """
    db_name = _validate_db(db_name)
    engine = get_data_engine()
    shrink_fairy = None
    try:
        shrink_fairy = engine.raw_connection()
        pyodbc_conn = shrink_fairy.driver_connection
        pyodbc_conn.autocommit = True
        cursor = pyodbc_conn.cursor()
        cursor.execute(f"USE [{db_name}]")
        cursor.execute(
            "SELECT name FROM sys.database_files WHERE type_desc = 'LOG'"
        )
        log_files = [r[0] for r in cursor.fetchall()]
        if not log_files:
            raise HTTPException(status_code=500, detail="No log file found")

        for log_name in log_files:
            cursor.execute(
                f"ALTER DATABASE [{db_name}] "
                f"MODIFY FILE (NAME = N'{log_name}', MAXSIZE = {int(max_mb)} MB)"
            )
        logger.info(f"set-log-maxsize {db_name} -> {max_mb} MB by admin")
        return {"success": True, "db_name": db_name, "max_mb": max_mb, "files": log_files}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"set-log-maxsize on {db_name} failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if shrink_fairy:
            try:
                shrink_fairy.invalidate()
            except Exception:
                pass


@router.get("/disk", summary="Free space on each SQL data drive")
def get_disk_space(_user=Depends(_require_superadmin)):
    """
    Per-volume free space, derived from sys.dm_os_volume_stats. One row per
    distinct disk volume that hosts a managed-DB file.
    """
    engine = get_data_engine()
    raw_conn = engine.raw_connection()
    try:
        cursor = raw_conn.cursor()
        managed_names = list(_managed_dbs().values())
        placeholders = ",".join("?" * len(managed_names))
        cursor.execute(f"""
            SELECT DISTINCT
                vs.volume_mount_point                    AS mount_point,
                vs.logical_volume_name                   AS volume_name,
                vs.total_bytes        / 1024.0 / 1024    AS total_mb,
                vs.available_bytes    / 1024.0 / 1024    AS free_mb,
                CAST(100.0 * vs.available_bytes / NULLIF(vs.total_bytes, 0) AS DECIMAL(5,2))
                                                         AS free_pct
            FROM sys.master_files mf
            CROSS APPLY sys.dm_os_volume_stats(mf.database_id, mf.file_id) vs
            INNER JOIN sys.databases d ON d.database_id = mf.database_id
            WHERE d.name IN ({placeholders})
            ORDER BY mount_point;
        """, managed_names)
        cols = [c[0] for c in cursor.description]
        volumes = []
        for r in cursor.fetchall():
            d = dict(zip(cols, r))
            volumes.append({
                "mount_point": d["mount_point"],
                "volume_name": d["volume_name"],
                "total_mb":    float(d["total_mb"] or 0),
                "free_mb":     float(d["free_mb"] or 0),
                "used_mb":     float(d["total_mb"] or 0) - float(d["free_mb"] or 0),
                "free_pct":    float(d["free_pct"] or 0),
            })
        return {"volumes": volumes}
    except Exception as exc:
        logger.error(f"get_disk_space failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        raw_conn.close()


@router.post("/reclaim-all", summary="Emergency: free everything possible NOW")
def reclaim_all(_user=Depends(_require_superadmin)):
    """
    One-click panic button. Runs in the request thread so the UI gets a
    single result back:
      1. CHECKPOINT every managed DB.
      2. SHRINKFILE the log of every managed DB to AUTO_FREE_LOG_TARGET_MB
         (if recovery is SIMPLE, or already truncatable).
      3. Aggressive tempdb shrink (FREEPROCCACHE + FREESYSTEMCACHE +
         hard SHRINKFILE on every tempdb data file).
      4. Drop ALL ARS ## global temp tables (no age threshold).

    Designed for the case where the disk is nearly full and you need every
    byte back ASAP.
    """
    target_log_mb = getattr(settings, "AUTO_FREE_LOG_TARGET_MB", 4096)
    auto_resolve = getattr(settings, "AUTO_RESOLVE_LOG_BACKUP_WAIT", True)
    summary = {
        "checkpointed":      [],
        "log_shrunk":        [],
        "log_skipped":       [],
        "auto_simpled":      [],   # DBs we flipped FULL→SIMPLE to clear LOG_BACKUP wait
        "tempdb_freed_mb":   0.0,
        "orphans_dropped":   0,
        "errors":            [],
    }

    engine = get_data_engine()
    shrink_fairy = None
    try:
        shrink_fairy = engine.raw_connection()
        pyodbc_conn = shrink_fairy.driver_connection
        pyodbc_conn.autocommit = True
        cur = pyodbc_conn.cursor()

        # 1 + 2: CHECKPOINT + log shrink for every managed DB
        for db in _managed_dbs().values():
            try:
                cur.execute(f"USE [{db}]")
                cur.execute("CHECKPOINT")
                summary["checkpointed"].append(db)

                cur.execute(
                    "SELECT recovery_model_desc, log_reuse_wait_desc "
                    "FROM sys.databases WHERE name = ?", db,
                )
                rec, wait = (cur.fetchone() or (None, None))

                cur.execute(
                    "SELECT name, size * 8.0 / 1024 FROM sys.database_files WHERE type_desc = 'LOG'"
                )
                logs = cur.fetchall()
                for log_name, before_mb in logs:
                    before_mb = float(before_mb or 0)
                    if rec != "SIMPLE" and wait == "LOG_BACKUP":
                        if auto_resolve:
                            # Flip to SIMPLE so the log self-truncates from now on,
                            # eliminating the LOG_BACKUP wait entirely.
                            try:
                                cur.execute(f"ALTER DATABASE [{db}] SET RECOVERY SIMPLE")
                                cur.execute(f"USE [{db}]")
                                cur.execute("CHECKPOINT")
                                summary["auto_simpled"].append({"db": db, "from": rec})
                                logger.warning(
                                    f"reclaim-all: {db} switched {rec}→SIMPLE to clear LOG_BACKUP wait"
                                )
                                rec = "SIMPLE"
                                wait = None
                            except Exception as exc:
                                summary["log_skipped"].append({
                                    "db": db, "file": log_name,
                                    "reason": f"auto-SIMPLE failed: {exc}",
                                })
                                continue
                        else:
                            summary["log_skipped"].append({
                                "db": db, "file": log_name,
                                "reason": f"recovery={rec}, log_reuse_wait={wait}",
                            })
                            continue
                    try:
                        # Up to 3 CHECKPOINT+SHRINK passes
                        current_mb = before_mb
                        for _ in range(3):
                            cur.execute("CHECKPOINT")
                            cur.execute(f"DBCC SHRINKFILE (N'{log_name}', {int(target_log_mb)}) WITH NO_INFOMSGS")
                            cur.execute(
                                "SELECT size * 8.0 / 1024 FROM sys.database_files WHERE name = ?",
                                log_name,
                            )
                            new_mb = float(cur.fetchone()[0] or 0)
                            if new_mb <= target_log_mb + 16 or new_mb >= current_mb:
                                current_mb = new_mb
                                break
                            current_mb = new_mb
                        summary["log_shrunk"].append({
                            "db": db, "file": log_name,
                            "before_mb": before_mb, "after_mb": current_mb,
                            "freed_mb": before_mb - current_mb,
                        })
                    except Exception as exc:
                        summary["errors"].append({"step": f"shrink_{db}_{log_name}", "error": str(exc)})
            except Exception as exc:
                summary["errors"].append({"step": f"checkpoint_{db}", "error": str(exc)})

        # 3 + 4: Aggressive tempdb reclaim through the existing service so
        #        history + alerts are properly recorded.
        try:
            tempdb_stats = tempdb_cleaner.aggressive_shrink_now()
            summary["tempdb_freed_mb"] = float(tempdb_stats.get("mb_freed") or 0)
            summary["orphans_dropped"] = int(tempdb_stats.get("dropped_count") or 0)
            summary["tempdb_mb_before"] = tempdb_stats.get("tempdb_mb_before")
            summary["tempdb_mb_after"]  = tempdb_stats.get("tempdb_mb_after")
        except Exception as exc:
            summary["errors"].append({"step": "tempdb_aggressive", "error": str(exc)})

        logger.warning(
            f"reclaim-all by admin: shrunk {len(summary['log_shrunk'])} log file(s), "
            f"tempdb freed {summary['tempdb_freed_mb']:.0f} MB, "
            f"dropped {summary['orphans_dropped']} orphan(s), "
            f"{len(summary['errors'])} error(s)"
        )
        return {"success": True, "summary": summary}
    except Exception as exc:
        logger.error(f"reclaim-all failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if shrink_fairy:
            try:
                shrink_fairy.invalidate()
            except Exception:
                pass
