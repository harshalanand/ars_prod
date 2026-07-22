"""
Dev Sync Service — one-way table refresh from PROD (HOPC866) → DEV (ARSDBPRO).

Key design points (see docs/DEV_SYNC_PLAN.md):
  • Dev Sync has its OWN explicit source + target connections, independent of which
    server the ARS app itself runs on. So the app can stay pointed at PROD and this
    tool still pushes PROD → DEV.
      - source_server (default hopc866) — read-only.
      - target_server (default arsdbpro) — the only thing ever written.
  • Movement = server-side `INSERT … SELECT` over a LINKED SERVER created ON the
    target pointing at the source. The sync SQL executes on the target.
  • Connection settings live in app_settings.json (block "dev_sync") — NOT in a DB
    table — so nothing is created on prod. The per-table config list lives in
    ARS_DEV_SYNC_TABLES on the TARGET (dev) data DB.
  • Modes:
      - incremental : append rows with key > MAX(key on dev). Cheap; daily.
      - full        : TRUNCATE + INSERT in a transaction. Weekly; reconciles drift.
  • Skip: a table whose prod & dev row counts already match is SKIPPED (no update)
    unless `force` is set.
  • Safety guards:
      - Refuses to run if source_server == target_server (can't write prod).
      - Every table runs in its own transaction → a failure rolls back just it.
"""
from __future__ import annotations

import os
import json
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, date

import pyodbc
from loguru import logger

from app.core.config import get_settings, APP_SETTINGS_FILE

TABLES_TABLE = "ARS_DEV_SYNC_TABLES"

# Tables we flag as "don't normally sync" (dev regenerates / doesn't need them).
# Only a hint for the UI; the user can still add + activate anything.
EXCLUDE_HINTS = (
    "_HISTORY", "_PARKED", "_ARCHIVE", "AuditLog", "_AUDIT",
    "ARS_MSA_TOTAL", "ARS_MSA_GEN_ART", "ARS_MSA_VAR_ART",
    "ARS_GRID_MJ", "ARS_LISTING", "ARS_ALLOC_WORKING", "ARS_ALLOC_PARKED",
    "ARS_pend_alc", "ARS_LISTED_OPT", "ARS_CALC_", "ARS_ONESIZE_ALLOCATION",
    "ARS_NL_TBL_HOLD", "allocation_results", "Cont_Percentage_",
    "rbac_", "ARSS_", "AppUsers",
)


# ══════════════════════════════════════════════════════════════════════════════
# Table categorisation  (input | reference | config | output | history | identity)
# ══════════════════════════════════════════════════════════════════════════════
# recommended-to-sync = input + reference + config (the things alloc/listing read).
def classify(db: str, table: str) -> Tuple[str, bool]:
    """Return (category, recommended_to_sync) for a table. Precedence matters."""
    t = table
    U = t.upper()

    # 1) history / archive / audit — huge, dev keeps its own
    if (U.endswith("_HISTORY") or "_ARCHIVE" in U or U == "AUDITLOG"
            or U.endswith("_AUDIT") or U.endswith("_PARKED")
            or U in ("ET_STORE_STOCK_1307",)):
        return "history", False

    # 2) identity / app plumbing — dev keeps its own users/roles.
    #    (app_config is ARS runtime config, not plumbing — excluded here.)
    if (U.startswith("RBAC_") or U.startswith("ARSS_")
            or (U.startswith("APP") and U != "APP_CONFIG")
            or U in ("ARS_USER", "ARS_ROLE", "ARS_ROLE_PERMISSION", "ARS_PERMISSION",
                     "COMPANIES", "AUDIT_LOG", "AUDIT_LOGS")):
        return "identity", False

    # 3) raw inputs (daily SAP extract via SSIS — already on both, sync verifies)
    if (U.startswith("ET_") or U.startswith("COUNT_STOCK")
            or U.startswith("ALLOCATION_MRDC") or U.startswith("MASTER_ALC_")
            or U == "MASTER_ART_BROADER_MENU"
            or U in ("STORE_STOCK", "STORE_SALES", "WAREHOUSE_STOCK")):
        return "input", True

    # 3b) settings tables are config even when the name starts ARS_MSA_/ARS_STORE_
    if U.endswith("_SETTINGS"):
        return "config", True

    # 4) engine outputs — dev regenerates by running the pipeline.
    #    (ARS_GRID_BUILDER / ARS_GRID_HIERARCHY are config, handled in step 6.)
    if (U.startswith("ARS_MSA_") or U.startswith("ARS_GRID_MJ")
            or U.startswith("ARS_LISTING") or U.startswith("ARS_ALLOC")
            or U.startswith("ARS_CALC_") or U.startswith("ARS_PEND_ALC")
            or U.startswith("ARS_NL_TBL_HOLD") or U.startswith("CONT_PERCENTAGE_")
            or U.startswith("LISTING_COMPILE")
            or U in ("ARS_LISTED_OPT", "ARS_ONESIZE_ALLOCATION",
                     "ALLOCATION", "ALLOCATION_RESULTS", "ARS_BDC_HISTORY")):
        return "output", False

    # 5) reference / master data (maintained upstream, alloc/listing reads it)
    if (U.startswith("MASTER") or U.startswith("RETAIL_")
            or U in ("MASTER_AVG_DENSITY",)):
        return "reference", True

    # 6) config / rules / settings (the tables that drive alloc/listing behaviour)
    if (U.endswith("_SETTINGS") or "MERGE_RULE" in U or U == "ARS_GRID_BUILDER"
            or U == "ARS_GRID_HIERARCHY" or "SEC_CAP" in U
            or U.startswith("ARS_CHECKLIST") or U.startswith("ARS_DATA_DICTIONARY")
            or U.startswith("ARS_STORE_RANKING") or "BDC" in U
            or U.startswith("CONT_") or U == "CONT_PRESETS" or U == "APP_CONFIG"
            or U.startswith("DISPLAY_")):
        return "config", True

    return "other", False


# ══════════════════════════════════════════════════════════════════════════════
# Connection settings — persisted in app_settings.json  (block "dev_sync")
# ══════════════════════════════════════════════════════════════════════════════
_DEFAULTS = {
    "source_server": "hopc866",
    "source_user": "",
    "source_pwd": "",
    "target_server": "arsdbpro",
    "target_user": "",
    "target_pwd": "",
    "target_system_db": "Claude",
    "target_data_db": "Rep_data",
    "link_server_name": "HOPC866",
    "incr_enabled": False, "incr_hour": 6,
    "full_enabled": False, "full_weekday": 7, "full_hour": 2,
    "last_incr_at": None, "last_full_at": None,
}


def _load_cfg() -> Dict[str, Any]:
    cfg = dict(_DEFAULTS)
    try:
        if os.path.exists(APP_SETTINGS_FILE):
            with open(APP_SETTINGS_FILE, "r", encoding="utf-8") as f:
                blk = (json.load(f) or {}).get("dev_sync", {}) or {}
                cfg.update({k: v for k, v in blk.items() if v is not None})
    except Exception as e:
        logger.warning(f"dev_sync: could not read config: {e}")
    # Default source/target creds to the app's own DB creds if left blank.
    appdb = get_settings()._db()
    if not cfg.get("source_user"):
        cfg["source_user"] = appdb["username"]
    if not cfg.get("source_pwd"):
        cfg["source_pwd"] = appdb["password"]
    if not cfg.get("target_user"):
        cfg["target_user"] = appdb["username"]
    if not cfg.get("target_pwd"):
        cfg["target_pwd"] = appdb["password"]
    return cfg


def _save_cfg(patch: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    try:
        if os.path.exists(APP_SETTINGS_FILE):
            with open(APP_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
    except Exception:
        data = {}
    blk = data.get("dev_sync", {}) or {}
    for k, v in patch.items():
        if v is not None and v != "":
            blk[k] = v
    data["dev_sync"] = blk
    with open(APP_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return _load_cfg()


def _managed_dbs(cfg: Dict[str, Any]) -> List[str]:
    return [cfg["target_system_db"], cfg["target_data_db"]]


def get_settings_row() -> Dict[str, Any]:
    cfg = _load_cfg()
    out = dict(cfg)
    out["source_pwd_set"] = bool(cfg.get("source_pwd"))
    out["target_pwd_set"] = bool(cfg.get("target_pwd"))
    out.pop("source_pwd", None)
    out.pop("target_pwd", None)
    return out


def save_settings(payload: Dict[str, Any]) -> Dict[str, Any]:
    allowed = ("source_server", "source_user", "source_pwd",
               "target_server", "target_user", "target_pwd",
               "target_system_db", "target_data_db", "link_server_name",
               "incr_enabled", "incr_hour", "full_enabled", "full_weekday", "full_hour")
    patch = {k: payload[k] for k in allowed if k in payload}
    _save_cfg(patch)
    return get_settings_row()


# ══════════════════════════════════════════════════════════════════════════════
# Connections
# ══════════════════════════════════════════════════════════════════════════════
def _mk_conn(server: str, user: str, pwd: str, db: str = "master",
             autocommit: bool = True) -> pyodbc.Connection:
    drv = get_settings().DB_DRIVER
    cs = (f"DRIVER={{{drv}}};SERVER={server};DATABASE={db};UID={user};PWD={pwd};"
          "TrustServerCertificate=yes;Encrypt=no;Connection Timeout=15;")
    return pyodbc.connect(cs, autocommit=autocommit)


def _target_conn(db: Optional[str] = None, autocommit: bool = True) -> pyodbc.Connection:
    cfg = _load_cfg()
    if not (cfg.get("target_user") and cfg.get("target_pwd")):
        raise RuntimeError("Set target (dev) username/password first.")
    return _mk_conn(cfg["target_server"], cfg["target_user"], cfg["target_pwd"],
                    db or cfg["target_data_db"], autocommit)


def _source_conn(db: str = "master") -> pyodbc.Connection:
    cfg = _load_cfg()
    if not (cfg.get("source_user") and cfg.get("source_pwd")):
        raise RuntimeError("Set source (prod) username/password first.")
    return _mk_conn(cfg["source_server"], cfg["source_user"], cfg["source_pwd"],
                    db, autocommit=True)


def test_connection(which: str, server: str, user: str, pwd: Optional[str]) -> Dict[str, Any]:
    cfg = _load_cfg()
    if not pwd:
        pwd = cfg.get("source_pwd") if which == "source" else cfg.get("target_pwd")
    con = _mk_conn(server, user, pwd)
    cur = con.cursor()
    cur.execute("SELECT @@SERVERNAME, CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(50))")
    name, ver = cur.fetchone()
    con.close()
    return {"server_name": name, "version": ver, "connected_to": server}


# ══════════════════════════════════════════════════════════════════════════════
# Linked-server setup (on the TARGET, pointing at the source)
# ══════════════════════════════════════════════════════════════════════════════
def setup_linkserver() -> Dict[str, Any]:
    cfg = _load_cfg()
    src, link = cfg["source_server"], cfg["link_server_name"]
    if cfg["source_server"].strip().lower() == cfg["target_server"].strip().lower():
        raise RuntimeError(
            f"source_server and target_server are both '{src}'. Set target_server to "
            f"the DEV server (arsdbpro) so prod is never written.")
    con = _target_conn(autocommit=True)   # procs below cannot run in a transaction
    try:
        cur = con.cursor()
        cur.execute(
            "IF EXISTS (SELECT 1 FROM sys.servers WHERE name = ?) "
            "EXEC sp_dropserver @server = ?, @droplogins = 'droplogins';", link, link)
        created, errs = None, []
        if link.strip().lower() == src.strip().lower():
            try:
                cur.execute("EXEC sp_addlinkedserver @server=?, @srvproduct=N'SQL Server';", link)
                created = "SQL Server (native)"
            except Exception as e:
                errs.append(f"native: {e}")
        if not created:
            for prov in ("MSOLEDBSQL", "SQLNCLI11", "SQLOLEDB"):
                try:
                    cur.execute("EXEC sp_addlinkedserver @server=?, @srvproduct=N'', "
                                "@provider=?, @datasrc=?;", link, prov, src)
                    created = prov
                    break
                except Exception as e:
                    errs.append(f"{prov}: {e}")
        if not created:
            raise RuntimeError("could not create linked server — " + " | ".join(errs))
        cur.execute("EXEC sp_addlinkedsrvlogin @rmtsrvname=?, @useself=N'FALSE', "
                    "@locallogin=NULL, @rmtuser=?, @rmtpassword=?;",
                    link, cfg["source_user"], cfg["source_pwd"])
        cur.execute("EXEC sp_serveroption ?, 'rpc out', 'true';", link)
        cur.execute("EXEC sp_serveroption ?, 'remote proc transaction promotion', 'false';", link)
        cur.execute(f"SELECT name FROM OPENQUERY([{link}], 'SELECT @@SERVERNAME AS name')")
        remote = cur.fetchone()[0]
        return {"link_server": link, "remote_server": remote, "provider": created,
                "target_server": cfg["target_server"]}
    finally:
        con.close()


# ══════════════════════════════════════════════════════════════════════════════
# Metadata helpers (raw pyodbc cursors; three-part local / four-part linked)
# ══════════════════════════════════════════════════════════════════════════════
def _counts(cur, db: str, link: Optional[str] = None) -> Dict[str, int]:
    prefix = f"[{link}]." if link else ""
    cur.execute(f"""
        SELECT t.name, ISNULL(SUM(p.rows),0)
        FROM {prefix}[{db}].sys.tables t
        LEFT JOIN {prefix}[{db}].sys.partitions p
               ON p.object_id = t.object_id AND p.index_id IN (0,1)
        GROUP BY t.name""")
    return {r[0]: int(r[1]) for r in cur.fetchall()}


def _one_count(cur, db: str, table: str, link: Optional[str] = None) -> int:
    prefix = f"[{link}]." if link else ""
    cur.execute(f"""
        SELECT ISNULL(SUM(p.rows),0)
        FROM {prefix}[{db}].sys.partitions p
        JOIN {prefix}[{db}].sys.tables t ON t.object_id = p.object_id
        WHERE t.name = ? AND p.index_id IN (0,1)""", table)
    return int(cur.fetchone()[0] or 0)


def _cols(cur, db: str, table: str, link: Optional[str] = None) -> List[str]:
    prefix = f"[{link}]." if link else ""
    cur.execute(f"SELECT COLUMN_NAME FROM {prefix}[{db}].INFORMATION_SCHEMA.COLUMNS "
                f"WHERE TABLE_NAME = ? ORDER BY ORDINAL_POSITION", table)
    return [r[0] for r in cur.fetchall()]


def _identity(cur, db: str, table: str) -> Optional[str]:
    cur.execute(f"""SELECT c.name FROM [{db}].sys.identity_columns c
        JOIN [{db}].sys.tables t ON t.object_id = c.object_id
        WHERE t.name = ?""", table)
    r = cur.fetchone()
    return r[0] if r else None


def _guess_key(cols: List[str], identity: Optional[str]) -> Optional[str]:
    if identity:
        return identity
    low = {c.lower(): c for c in cols}
    for cand in ("updated_at", "modified_at", "created_at", "created", "updated",
                 "last_updated", "id"):
        if cand in low:
            return low[cand]
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Config table (ARS_DEV_SYNC_TABLES) — lives on the TARGET data DB
# ══════════════════════════════════════════════════════════════════════════════
def ensure_tables():
    con = _target_conn(autocommit=True)
    try:
        cur = con.cursor()
        cur.execute(f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{TABLES_TABLE}')
            CREATE TABLE {TABLES_TABLE} (
                id             INT IDENTITY(1,1) PRIMARY KEY,
                db_name        NVARCHAR(100)  NOT NULL,
                table_name     NVARCHAR(200)  NOT NULL,
                category       NVARCHAR(30)   NULL,
                is_active      BIT            NOT NULL DEFAULT 0,
                sync_mode      NVARCHAR(20)   NOT NULL DEFAULT 'full',
                incr_key_col   NVARCHAR(200)  NULL,
                exclude_reason NVARCHAR(400)  NULL,
                last_sync_at   DATETIME       NULL,
                last_full_at   DATETIME       NULL,
                last_mode      NVARCHAR(20)   NULL,
                src_rows       BIGINT         NULL,
                tgt_rows       BIGINT         NULL,
                last_status    NVARCHAR(20)   NULL,
                last_message   NVARCHAR(MAX)  NULL,
                discovered_at  DATETIME       NOT NULL DEFAULT GETDATE(),
                updated_at     DATETIME       NOT NULL DEFAULT GETDATE(),
                CONSTRAINT UQ_{TABLES_TABLE} UNIQUE (db_name, table_name)
            )""")
        # add category column if the table pre-existed without it
        cur.execute(f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
                           WHERE TABLE_NAME='{TABLES_TABLE}' AND COLUMN_NAME='category')
            ALTER TABLE {TABLES_TABLE} ADD category NVARCHAR(30) NULL""")
    finally:
        con.close()


# ══════════════════════════════════════════════════════════════════════════════
# Discovery / comparison  (source read directly — no linked server needed)
# ══════════════════════════════════════════════════════════════════════════════
def discover() -> Dict[str, Any]:
    cfg = _load_cfg()
    ensure_tables()
    dbs = _managed_dbs(cfg)
    result: Dict[str, Any] = {
        "compare": [], "new": [], "missing_on_dev": [], "orphan_on_dev": [],
        "errors": [], "link_server": cfg["link_server_name"],
        "source_server": cfg["source_server"], "target_server": cfg["target_server"],
        "same_server": cfg["source_server"].strip().lower() == cfg["target_server"].strip().lower(),
    }
    if result["same_server"]:
        result["errors"].append(
            f"source_server and target_server are both '{cfg['source_server']}'. "
            f"Set target_server to the DEV server (arsdbpro).")

    # config (target)
    configured: Dict[Tuple[str, str], bool] = {}
    try:
        tcon = _target_conn(autocommit=True); tcur = tcon.cursor()
        tcur.execute(f"SELECT db_name, table_name, is_active FROM {TABLES_TABLE}")
        configured = {(r[0], r[1]): bool(r[2]) for r in tcur.fetchall()}
    except Exception as e:
        result["errors"].append(f"target config read: {e}")
        tcon = tcur = None

    scon = scur = None
    try:
        scon = _source_conn(); scur = scon.cursor()
    except Exception as e:
        result["errors"].append(f"source connection failed: {e}")

    try:
        for db in dbs:
            src, tgt = {}, {}
            if scur is not None:
                try:
                    src = _counts(scur, db)
                except Exception as e:
                    result["errors"].append(f"source [{db}]: {e}")
            if tcur is not None:
                try:
                    tgt = _counts(tcur, db)
                except Exception as e:
                    result["errors"].append(f"target [{db}]: {e}")

            for t in sorted(src.keys()):
                src_rows, on_dev = src[t], t in tgt
                dev_rows = tgt.get(t)
                in_cfg = (db, t) in configured
                cat, rec = classify(db, t)
                entry = {
                    "db_name": db, "table_name": t, "category": cat, "recommended": rec,
                    "src_rows": src_rows, "dev_rows": dev_rows, "on_dev": on_dev,
                    "in_config": in_cfg, "is_active": configured.get((db, t), False),
                    "match": on_dev and dev_rows == src_rows,
                    "excluded_hint": any(h in t for h in EXCLUDE_HINTS),
                }
                result["compare"].append(entry)
                if not in_cfg:
                    result["new"].append(entry)

            for (db2, t), _a in configured.items():
                if db2 == db and t not in tgt:
                    result["missing_on_dev"].append({"db_name": db, "table_name": t})
            for t in tgt:
                if t not in src and (db, t) in configured:
                    result["orphan_on_dev"].append({"db_name": db, "table_name": t})
    finally:
        if scon:
            scon.close()
        if tcon:
            tcon.close()

    cmp = result["compare"]
    result["summary"] = {
        "total": len(cmp),
        "matched": sum(1 for x in cmp if x["match"]),
        "differ": sum(1 for x in cmp if x["on_dev"] and not x["match"]),
        "new": len(result["new"]),
        "missing": len(result["missing_on_dev"]),
        "by_category": {c: sum(1 for x in cmp if x["category"] == c)
                        for c in ("input", "reference", "config", "output", "history", "identity", "other")},
    }
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Config CRUD
# ══════════════════════════════════════════════════════════════════════════════
def add_tables(items: List[Dict[str, Any]]) -> int:
    cfg = _load_cfg()
    ensure_tables()
    added = 0
    scon = scur = None
    try:
        scon = _source_conn(); scur = scon.cursor()
    except Exception:
        scur = None
    tcon = _target_conn(autocommit=True); tcur = tcon.cursor()
    try:
        for it in items:
            db, t = it["db_name"], it["table_name"]
            tcur.execute(f"SELECT 1 FROM {TABLES_TABLE} WHERE db_name=? AND table_name=?", db, t)
            if tcur.fetchone():
                continue
            cat, _rec = classify(db, t)
            key = it.get("incr_key_col")
            if not key and scur is not None:
                try:
                    key = _guess_key(_cols(scur, db, t), _identity(scur, db, t))
                except Exception:
                    key = None
            mode = it.get("sync_mode") or ("incremental" if key else "full")
            tcur.execute(
                f"INSERT INTO {TABLES_TABLE} (db_name, table_name, category, is_active, sync_mode, incr_key_col) "
                f"VALUES (?,?,?,?,?,?)",
                db, t, cat, 1 if it.get("is_active") else 0, mode, key)
            added += 1
    finally:
        tcon.close()
        if scon:
            scon.close()
    return added


def list_tables() -> List[Dict[str, Any]]:
    ensure_tables()
    con = _target_conn(autocommit=True); cur = con.cursor()
    try:
        cur.execute(f"SELECT * FROM {TABLES_TABLE} ORDER BY category, db_name, table_name")
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            for k in ("last_sync_at", "last_full_at", "discovered_at", "updated_at"):
                if isinstance(d.get(k), (datetime, date)):
                    d[k] = d[k].isoformat()
            d["is_active"] = bool(d["is_active"])
            out.append(d)
        return out
    finally:
        con.close()


def update_table(tid: int, payload: Dict[str, Any]) -> None:
    allowed = ["is_active", "sync_mode", "incr_key_col", "exclude_reason"]
    sets, vals = [], []
    for f in allowed:
        if f in payload:
            sets.append(f"{f}=?")
            vals.append(payload[f])
    if not sets:
        return
    sets.append("updated_at=GETDATE()")
    vals.append(tid)
    con = _target_conn(autocommit=True); cur = con.cursor()
    try:
        cur.execute(f"UPDATE {TABLES_TABLE} SET {', '.join(sets)} WHERE id=?", *vals)
    finally:
        con.close()


def delete_table(tid: int) -> None:
    con = _target_conn(autocommit=True); cur = con.cursor()
    try:
        cur.execute(f"DELETE FROM {TABLES_TABLE} WHERE id=?", tid)
    finally:
        con.close()


def bulk_toggle(table_ids: List[int], active: bool) -> int:
    if not table_ids:
        return 0
    ids = ",".join(str(int(i)) for i in table_ids)
    con = _target_conn(autocommit=True); cur = con.cursor()
    try:
        cur.execute(f"UPDATE {TABLES_TABLE} SET is_active=?, updated_at=GETDATE() WHERE id IN ({ids})",
                    1 if active else 0)
        return cur.rowcount
    finally:
        con.close()


def bulk_delete(table_ids: List[int]) -> int:
    """Remove the given rows from the sync config list."""
    if not table_ids:
        return 0
    ids = ",".join(str(int(i)) for i in table_ids)
    con = _target_conn(autocommit=True); cur = con.cursor()
    try:
        cur.execute(f"DELETE FROM {TABLES_TABLE} WHERE id IN ({ids})")
        return cur.rowcount
    finally:
        con.close()


def clear_all() -> int:
    """Remove EVERY table from the sync config list (start-fresh).
    Only touches the config list on the target — no business data is deleted."""
    ensure_tables()
    con = _target_conn(autocommit=True); cur = con.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) FROM {TABLES_TABLE}")
        n = int(cur.fetchone()[0] or 0)
        cur.execute(f"DELETE FROM {TABLES_TABLE}")
        return n
    finally:
        con.close()


# ══════════════════════════════════════════════════════════════════════════════
# The sync
# ══════════════════════════════════════════════════════════════════════════════
def _fmt_key(v: Any) -> str:
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (datetime, date)):
        return "'" + v.isoformat(sep=" ") + "'"
    try:
        float(v)
        return str(v)
    except Exception:
        return "'" + str(v).replace("'", "''") + "'"


def _sync_one(cur, db: str, table: str, mode: str, incr_key: Optional[str],
              link: str, force: bool = False) -> Dict[str, Any]:
    """Sync one table (own transaction). cur = autocommit target cursor."""
    tgt = f"[{db}].[dbo].[{table}]"
    src = f"[{link}].[{db}].[dbo].[{table}]"

    src_n = _one_count(cur, db, table, link)
    tgt_n = _one_count(cur, db, table, None)
    if not force and src_n == tgt_n:
        return {"mode": "skipped", "rows_written": 0,
                "src_rows": src_n, "tgt_rows": tgt_n, "skipped": True}

    tgt_cols = _cols(cur, db, table, None)
    src_cols = set(_cols(cur, db, table, link))
    cols = [c for c in tgt_cols if c in src_cols]
    if not cols:
        raise RuntimeError("no common columns between source and target")
    col_list = ", ".join(f"[{c}]" for c in cols)
    ident = _identity(cur, db, table)
    has_ident = bool(ident and ident in cols)

    cur.execute("BEGIN TRANSACTION")
    try:
        if mode == "incremental" and incr_key:
            cur.execute(f"SELECT MAX([{incr_key}]) FROM {tgt}")
            mx = cur.fetchone()[0]
            where = "" if mx is None else f" WHERE [{incr_key}] > {_fmt_key(mx)}"
            if has_ident:
                cur.execute(f"SET IDENTITY_INSERT {tgt} ON")
            cur.execute(f"INSERT INTO {tgt} ({col_list}) SELECT {col_list} FROM {src}{where}")
            n = cur.rowcount
            if has_ident:
                cur.execute(f"SET IDENTITY_INSERT {tgt} OFF")
        else:
            mode = "full"
            try:
                cur.execute(f"TRUNCATE TABLE {tgt}")
            except Exception:
                cur.execute(f"DELETE FROM {tgt}")
            if has_ident:
                cur.execute(f"SET IDENTITY_INSERT {tgt} ON")
            cur.execute(f"INSERT INTO {tgt} ({col_list}) SELECT {col_list} FROM {src}")
            n = cur.rowcount
            if has_ident:
                cur.execute(f"SET IDENTITY_INSERT {tgt} OFF")
        cur.execute("COMMIT")
    except Exception:
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return {"mode": mode, "rows_written": int(n or 0),
            "src_rows": _one_count(cur, db, table, link),
            "tgt_rows": _one_count(cur, db, table, None)}


def run_sync(mode: str = "incremental", table_ids: Optional[List[int]] = None,
             force: bool = False) -> Dict[str, Any]:
    cfg = _load_cfg()
    ensure_tables()
    link = cfg["link_server_name"]

    # ---- Prod-safety guard: compare CONFIGURED hosts, not @@SERVERNAME ----
    if cfg["source_server"].strip().lower() == cfg["target_server"].strip().lower():
        raise RuntimeError(
            f"Refusing to run: source and target are both '{cfg['source_server']}'. "
            f"Set target_server to the DEV server (arsdbpro) in the connection card.")

    con = _target_conn(autocommit=True)
    cur = con.cursor()
    # verify linked server reachable
    try:
        cur.execute(f"SELECT name FROM OPENQUERY([{link}], 'SELECT @@SERVERNAME AS name')")
        remote = cur.fetchone()[0]
    except Exception as e:
        con.close()
        raise RuntimeError(f"Linked server [{link}] not reachable — run 'Save & setup "
                           f"linked server' first: {e}")

    where = ""
    if table_ids:
        where = " AND id IN (" + ",".join(str(int(i)) for i in table_ids) + ")"
    cur.execute(f"SELECT id, db_name, table_name, sync_mode, incr_key_col "
                f"FROM {TABLES_TABLE} WHERE is_active=1{where} ORDER BY db_name, table_name")
    rows = cur.fetchall()

    results, ok, skipped, err = [], 0, 0, 0
    try:
        for rid, db, table, tmode, key in rows:
            run_mode = "full" if mode == "full" else (tmode or "full")
            try:
                res = _sync_one(cur, db, table, run_mode, key, link, force=force)
                was_skip = res.get("skipped")
                status = "skipped" if was_skip else "ok"
                msg = "identical — prod & dev row counts match" if was_skip else None
                cur.execute(f"""UPDATE {TABLES_TABLE}
                    SET last_status=?, last_message=?, last_mode=?, src_rows=?, tgt_rows=?,
                        last_sync_at=GETDATE(),
                        last_full_at=CASE WHEN ?='full' THEN GETDATE() ELSE last_full_at END,
                        updated_at=GETDATE()
                    WHERE id=?""",
                    status, msg, res["mode"], res["src_rows"], res["tgt_rows"], res["mode"], rid)
                skipped += 1 if was_skip else 0
                ok += 0 if was_skip else 1
                results.append({"id": rid, "db_name": db, "table_name": table,
                                "status": status, **res})
            except Exception as e:
                m = str(e)[:1000]
                logger.warning(f"dev-sync {db}.{table} failed: {m}")
                try:
                    cur.execute(f"UPDATE {TABLES_TABLE} SET last_status='error', last_message=?, "
                                f"last_sync_at=GETDATE(), updated_at=GETDATE() WHERE id=?", m, rid)
                except Exception:
                    pass
                err += 1
                results.append({"id": rid, "db_name": db, "table_name": table,
                                "status": "error", "message": m})
    finally:
        con.close()

    _save_cfg({"last_full_at" if mode == "full" else "last_incr_at":
               datetime.now().isoformat(timespec="seconds")})

    return {"mode": mode, "tables": len(rows), "ok": ok, "skipped": skipped,
            "errors": err, "results": results,
            "source_server": cfg["source_server"], "target_server": cfg["target_server"],
            "remote_server": remote}
