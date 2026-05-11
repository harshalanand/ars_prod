"""
reset_service.py
================
Resets the application to a "fresh, zero-transactional-data" state.

How it stays current as new tables are added
---------------------------------------------
The list of tables to clear is NOT hardcoded. Each run rediscovers candidate
tables in BOTH databases via INFORMATION_SCHEMA and matches them against:

  1. EXPLICIT_TRANSACTIONAL — known transactional tables (exact names).
  2. PATTERN_TRANSACTIONAL  — regex patterns for the naming conventions
                              the app uses for parked / history / working /
                              MSA result / allocation engine / job tables.
  3. PROTECTED              — masters, RBAC, RLS, config, and presets that
                              MUST NEVER be cleared. A protected name (or
                              pattern hit) wins over a transactional match.

So when a new table is created via a code path that follows these
conventions (e.g. `ARS_NEW_FEATURE_PARKED`, `alloc_xxx`, `*_HISTORY`,
`*_WORKING`), the next reset run picks it up automatically. Add new
explicit names below if a table doesn't fit a convention.

TRUNCATE vs DELETE
------------------
For each candidate table the service inspects sys.foreign_keys:

  • If NO incoming foreign keys → TRUNCATE (fast, resets identity).
  • If incoming foreign keys     → DELETE (TRUNCATE would fail).

If TRUNCATE fails for any reason (rare: table is being replicated, etc.),
the service falls back to DELETE and reports it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.database.session import get_data_engine, get_system_engine


# ---------------------------------------------------------------------------
# Classification — what to clear, what to keep
# ---------------------------------------------------------------------------

# Exact table names that are always transactional. Database-prefixed entries
# ("system:upload_jobs") restrict the rule to one DB; bare entries match in
# either DB.
EXPLICIT_TRANSACTIONAL: List[str] = [
    # ─── Claude (system) DB ─────────────────────────────────────────────
    "system:upload_jobs",
    "system:export_jobs",
    "system:msa_storage_jobs",
    "system:data_change_log",
    "system:audit_log",

    # ─── Rep_data (data) DB ─────────────────────────────────────────────
    "data:ARS_LISTING_SESSIONS",
    "data:ARS_ALLOC_QUEUE",
    "data:ARS_PEND_ALC",
    "data:ARS_BDC_HISTORY",
    "data:ARS_PEND_ALC_OPERATIONS",
    "data:ARS_PEND_ALC_SCHEDULE_AUDIT",
    "data:ARS_NL_TBL_HOLD_TRACKING",
    "data:ARS_NL_TBL_HOLD_TRACKING_SNAPSHOT",
    "data:ARS_NL_TBL_HOLD_SNAPSHOT_SESSIONS",
    "data:ARS_LISTED_OPT",
    "data:ARS_LISTING",
    "data:ARS_LISTING_WORKING",
    "data:ARS_ALLOC_WORKING",
    "data:ARS_MSA_TOTAL",
    "data:ARS_MSA_GEN_ART",
    "data:ARS_MSA_VAR_ART",
]

# Regex patterns (case-insensitive) — anything matching is treated as
# transactional unless also protected. Anchored to the full table name.
PATTERN_TRANSACTIONAL: List[str] = [
    r"^ARS_.*_PARKED$",         # ARS_*_PARKED
    r"^ARS_.*_HISTORY$",        # ARS_*_HISTORY
    r"^ARS_.*_WORKING$",        # ARS_*_WORKING (working snapshots)
    r"^ARS_MSA_.*$",            # ARS_MSA_TOTAL / GEN_ART / VAR_ART (and parked/history)
    r"^ARS_PEND_ALC.*$",        # pending alloc family
    r"^ARS_BDC_.*$",            # BDC audit family
    r"^ARS_LISTING.*$",         # any listing-derived working/result tables
    r"^ARS_ALLOC.*$",           # any alloc working/queue tables
    r"^alloc_runs$",            # allocation engine v2 outputs
    r"^alloc_budget_cascade$",
    r"^alloc_article_scores$",
    r"^alloc_option_assignments$",
    r"^alloc_variant_assignments$",
    r"^alloc_delivery_orders$",
    r"^alloc_run_summary$",
    r"^upload_jobs$",
    r"^export_jobs$",
    r"^msa_storage_jobs$",
    r"^data_change_log$",
    r"^audit_log$",
]

# Master / config / RBAC tables that must NEVER be cleared. Both exact names
# and patterns are honored. A protected hit always wins.
PROTECTED_EXACT: List[str] = [
    # RBAC
    "rbac_users", "rbac_roles", "rbac_permissions",
    "rbac_user_roles", "rbac_role_permissions",
    # RLS
    "rls_stores", "rls_user_store_access", "rls_user_region_access",
    "rls_user_category_access", "rls_column_restrictions",
    # Retail masters
    "retail_division", "retail_sub_division", "retail_major_category",
    "retail_gen_article", "retail_variant_article",
    "retail_size_master", "retail_color_master",
    # Upstream feeds (refilled by sync)
    "store_stock", "store_sales", "warehouse_stock", "ST_MASTER",
    # Allocation engine config
    "alloc_score_config", "alloc_engine_settings",
    # Misc config
    "ARS_SLOC_SETTINGS", "table_permissions",
    "MASTER_GEN_ART_AGE", "MASTER_ALC_PEND",
    # Contribution presets (user-built scenarios)
    "Cont_presets", "Cont_mappings", "Cont_mapping_assignments",
    # Settings / preferences
    "user_preferences", "table_editor_settings", "export_settings",
    # MSA tracking — keep as audit trail (toggle via include_msa_tracking)
    "MSA_Calculation_Sequence", "MSA_Column_Definitions",
    # Schedules — keep user-configured automations
    "ARS_PEND_ALC_SCHEDULE",
]
PROTECTED_PATTERNS: List[str] = [
    r"^rbac_.*$",
    r"^rls_.*$",
    r"^retail_.*$",
    r"^MASTER_.*$",
    r".*_settings$",
    r".*_preferences$",
    r"^Cont_.*$",
]

# When include_msa_tracking=True the caller asks for a "deep" reset — the
# MSA sequence audit and user schedules are also cleared.
DEEP_RESET_RELEASE: List[str] = [
    "MSA_Calculation_Sequence",
    "MSA_Column_Definitions",
    "ARS_PEND_ALC_SCHEDULE",
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class TableResult:
    db: str               # "system" | "data"
    table: str
    method: str           # "TRUNCATE" | "DELETE" | "SKIPPED" | "ERROR"
    rows_before: int = 0
    rows_after: int = 0
    error: Optional[str] = None
    reason: Optional[str] = None  # why skipped, e.g. "protected"


@dataclass
class ResetReport:
    dry_run: bool
    include_msa_tracking: bool
    cleared: List[TableResult] = field(default_factory=list)
    skipped: List[TableResult] = field(default_factory=list)
    errors: List[TableResult] = field(default_factory=list)

    def to_dict(self) -> Dict:
        def _r(r: TableResult) -> Dict:
            return {
                "db": r.db, "table": r.table, "method": r.method,
                "rows_before": r.rows_before, "rows_after": r.rows_after,
                "error": r.error, "reason": r.reason,
            }
        return {
            "dry_run": self.dry_run,
            "include_msa_tracking": self.include_msa_tracking,
            "totals": {
                "cleared": len(self.cleared),
                "rows_deleted": sum(r.rows_before for r in self.cleared),
                "skipped":  len(self.skipped),
                "errors":   len(self.errors),
            },
            "cleared": [_r(r) for r in self.cleared],
            "skipped": [_r(r) for r in self.skipped],
            "errors":  [_r(r) for r in self.errors],
        }


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def _matches_any(name: str, patterns: List[str]) -> bool:
    for p in patterns:
        if re.match(p, name, flags=re.IGNORECASE):
            return True
    return False


def _is_protected(name: str, include_msa_tracking: bool) -> bool:
    # Exact match first
    if name in PROTECTED_EXACT:
        # If user asked for a deep reset, release a few specific tables
        if include_msa_tracking and name in DEEP_RESET_RELEASE:
            return False
        return True
    # Pattern match
    return _matches_any(name, PROTECTED_PATTERNS)


def _is_transactional(name: str, db_label: str) -> bool:
    explicit_keys = {f"{db_label}:{name}", name}
    for k in EXPLICIT_TRANSACTIONAL:
        if k in explicit_keys:
            return True
    return _matches_any(name, PATTERN_TRANSACTIONAL)


# ---------------------------------------------------------------------------
# Per-engine work
# ---------------------------------------------------------------------------

def _list_tables(engine: Engine) -> List[str]:
    sql = (
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_TYPE = 'BASE TABLE' "
        "ORDER BY TABLE_NAME"
    )
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(text(sql)).fetchall()]


def _has_incoming_fk(engine: Engine, table: str) -> bool:
    """Return True if any other table has a FK pointing AT this table —
    in that case TRUNCATE will fail and we must use DELETE."""
    sql = """
        SELECT COUNT(*)
        FROM sys.foreign_keys fk
        INNER JOIN sys.tables t ON fk.referenced_object_id = t.object_id
        WHERE t.name = :t
    """
    with engine.connect() as conn:
        return (conn.execute(text(sql), {"t": table}).scalar() or 0) > 0


def _row_count(engine: Engine, table: str) -> int:
    try:
        with engine.connect() as conn:
            return conn.execute(text(f"SELECT COUNT(*) FROM [{table}]")).scalar() or 0
    except Exception:
        return -1


def _has_identity(engine: Engine, table: str) -> bool:
    sql = """
        SELECT OBJECTPROPERTY(OBJECT_ID(:t), 'TableHasIdentity')
    """
    with engine.connect() as conn:
        return bool(conn.execute(text(sql), {"t": table}).scalar() or 0)


def _execute_clear(
    engine: Engine, table: str, method_pref: str, reseed_identity: bool
) -> Tuple[str, Optional[str]]:
    """Run the clear; return (method_used, error_or_None)."""
    raw = engine.raw_connection()
    try:
        raw.autocommit = False
        cur = raw.cursor()
        used = method_pref
        try:
            if method_pref == "TRUNCATE":
                cur.execute(f"TRUNCATE TABLE [{table}]")
            else:
                cur.execute(f"DELETE FROM [{table}]")
            raw.commit()
        except Exception as e:
            raw.rollback()
            # Fall back from TRUNCATE → DELETE on any failure
            if method_pref == "TRUNCATE":
                try:
                    cur.execute(f"DELETE FROM [{table}]")
                    raw.commit()
                    used = "DELETE"
                except Exception as e2:
                    raw.rollback()
                    return ("ERROR", f"TRUNCATE failed ({e}); DELETE failed ({e2})")
            else:
                return ("ERROR", str(e))

        if reseed_identity and used in ("TRUNCATE", "DELETE"):
            try:
                cur.execute(f"DBCC CHECKIDENT('[{table}]', RESEED, 0)")
                raw.commit()
            except Exception as e:
                # Reseed isn't critical — log and move on
                logger.debug(f"reseed identity failed for {table}: {e}")

        return (used, None)
    finally:
        try:
            raw.close()
        except Exception:
            pass


def _process_engine(
    engine: Engine,
    db_label: str,
    dry_run: bool,
    include_msa_tracking: bool,
    report: ResetReport,
) -> None:
    """Discover, classify, and clear all candidate tables in one engine."""
    try:
        tables = _list_tables(engine)
    except Exception as e:
        logger.error(f"[{db_label}] failed to list tables: {e}")
        report.errors.append(TableResult(
            db=db_label, table="<list_tables>", method="ERROR", error=str(e)
        ))
        return

    for tbl in tables:
        # Protected wins
        if _is_protected(tbl, include_msa_tracking):
            report.skipped.append(TableResult(
                db=db_label, table=tbl, method="SKIPPED", reason="protected"
            ))
            continue
        # Must look transactional
        if not _is_transactional(tbl, db_label):
            report.skipped.append(TableResult(
                db=db_label, table=tbl, method="SKIPPED",
                reason="not classified as transactional"
            ))
            continue

        rows_before = _row_count(engine, tbl)
        # Pick TRUNCATE if no incoming FKs (and table not empty has no impact);
        # otherwise DELETE.
        try:
            method_pref = "DELETE" if _has_incoming_fk(engine, tbl) else "TRUNCATE"
        except Exception as e:
            logger.warning(f"[{db_label}] FK check failed for {tbl}: {e}")
            method_pref = "DELETE"

        if dry_run:
            report.cleared.append(TableResult(
                db=db_label, table=tbl, method=f"WOULD_{method_pref}",
                rows_before=rows_before, rows_after=rows_before,
            ))
            continue

        try:
            reseed = _has_identity(engine, tbl)
        except Exception:
            reseed = False

        used, err = _execute_clear(engine, tbl, method_pref, reseed)
        if err:
            report.errors.append(TableResult(
                db=db_label, table=tbl, method="ERROR",
                rows_before=rows_before, error=err,
            ))
            logger.error(f"[{db_label}] {tbl} → {err}")
        else:
            after = _row_count(engine, tbl)
            report.cleared.append(TableResult(
                db=db_label, table=tbl, method=used,
                rows_before=rows_before, rows_after=after,
            ))
            logger.info(
                f"[{db_label}] {tbl} → {used} "
                f"(before={rows_before}, after={after})"
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def reset_transactional_data(
    dry_run: bool = True,
    include_msa_tracking: bool = False,
) -> ResetReport:
    """
    Reset the application to zero-transactional-data state.

    Args:
        dry_run: If True, only report what WOULD happen — no writes.
        include_msa_tracking: If True, also clear MSA_Calculation_Sequence,
            MSA_Column_Definitions, and ARS_PEND_ALC_SCHEDULE. These are
            normally protected because they double as audit / user config.

    Returns:
        ResetReport. Use `.to_dict()` for JSON-friendly output.
    """
    report = ResetReport(dry_run=dry_run, include_msa_tracking=include_msa_tracking)

    logger.info(
        f"reset_transactional_data starting "
        f"(dry_run={dry_run}, include_msa_tracking={include_msa_tracking})"
    )

    _process_engine(get_data_engine(),   "data",   dry_run, include_msa_tracking, report)
    _process_engine(get_system_engine(), "system", dry_run, include_msa_tracking, report)

    summary = report.to_dict()["totals"]
    logger.info(
        f"reset_transactional_data done — cleared={summary['cleared']} "
        f"rows={summary['rows_deleted']} skipped={summary['skipped']} "
        f"errors={summary['errors']}"
    )
    return report
