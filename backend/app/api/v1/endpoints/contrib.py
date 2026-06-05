"""
Contribution Percentage v2 API
==============================
Full reimplementation: Presets, Mappings, Execute pipeline, Review.

Tables (Rep_data):
  Cont_presets             – preset configs (months, avg_days, kpi_type, sequence)
  Cont_mappings            – SSN→suffix mapping rules
  Cont_mapping_assignments – links mappings to output columns
  Cont_Percentage_*        – output result tables

Endpoints:
  /contrib/config           – grouping columns, months, majcats
  /contrib/presets          – CRUD + reorder
  /contrib/mappings         – CRUD
  /contrib/assignments      – CRUD
  /contrib/execute          – run pipeline
  /contrib/review           – list/preview/export results
"""

import io, json, time, re, gc, threading, uuid, os, tempfile, zipfile
from datetime import datetime
from typing import Optional, List, Any
from collections import OrderedDict

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text, inspect
from loguru import logger

from app.database.session import get_data_engine
from app.schemas.common import APIResponse
from app.security.dependencies import get_current_user
from app.models.rbac import User

# ── Job Queue (in-memory) ────────────────────────────────────────────────────
_jobs: OrderedDict = OrderedDict()          # job_id → job dict
_job_lock = threading.Lock()
_job_queue = []                             # pending job_ids in order
_worker_running = False

router = APIRouter(prefix="/contrib", tags=["Contribution Percentage"])

# ── Constants ────────────────────────────────────────────────────────────────
TABLE_PREFIX       = "Cont_Percentage"
PRESET_TABLE       = "Cont_presets"
MAPPING_TABLE      = "Cont_mappings"
ASSIGNMENT_TABLE   = "Cont_mapping_assignments"
JOB_TABLE          = "Cont_jobs"
NUMERIC_SQL_TYPES  = {'int','bigint','smallint','tinyint','numeric','decimal','float','real','money','smallmoney'}
VALID_GROUPING     = ('CLR','SZ','RNG_SEG','M_VND_CD','MACRO_MVGR','MICRO_MVGR','FAB','WEAVE_2','M_YARN_02')


# ── Schemas ──────────────────────────────────────────────────────────────────

class PresetPayload(BaseModel):
    preset_name: str
    months: List[str] = []
    avg_days: int = 30
    kpi_type: str = "L30D"           # L18M, L30D or L7D
    description: str = ""

class PresetReorder(BaseModel):
    sequence: List[str]              # ordered list of preset names

class MappingPayload(BaseModel):
    mapping_name: str
    suffix_mapping: dict = {}        # { "SSN_VALUE": ["suffix1","suffix2"], ... }
    fallback_suffixes: List[str] = []
    description: str = ""

class AssignmentPayload(BaseModel):
    col_name: str
    mapping_name: str
    prefix: str = "INITIAL AUTO CONT%|"
    target: str = "Both"             # Store / Company / Both

class ExecutePayload(BaseModel):
    presets: List[str] = []          # empty = all
    majcats: List[str] = []          # empty = all
    grouping_column: str = "MACRO_MVGR"
    save_to_db: bool = False
    use_sequence: bool = True
    target: str = "Both"             # Store / Company / Both


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS – DDL & DB
# ══════════════════════════════════════════════════════════════════════════════

def _run(conn, sql, params=None):
    conn.execute(text(sql), params or {})
    conn.commit()

def _read_sql_nolock(query, engine, retries=2):
    """Read SQL with READ UNCOMMITTED isolation. Retries on connection drop."""
    for attempt in range(retries + 1):
        try:
            with engine.connect().execution_options(isolation_level="READ UNCOMMITTED") as conn:
                return pd.read_sql(text(query) if not isinstance(query, str) else query, conn)
        except Exception as e:
            if attempt < retries and ('10054' in str(e) or '08S01' in str(e) or 'Communication link' in str(e)):
                logger.warning(f"_read_sql_nolock: connection drop on attempt {attempt+1}, retrying in 2s...")
                time.sleep(2)
                engine.dispose()  # Reset connection pool
                continue
            raise

def _ensure_preset_table(engine):
    with engine.connect() as c:
        _run(c, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{PRESET_TABLE}')
            CREATE TABLE {PRESET_TABLE} (
                preset_name   NVARCHAR(255) PRIMARY KEY,
                preset_type   NVARCHAR(50),
                description   NVARCHAR(MAX),
                config_json   NVARCHAR(MAX),
                sequence_order INT DEFAULT 9999,
                created_date  DATETIME DEFAULT GETDATE(),
                modified_date DATETIME DEFAULT GETDATE()
            )
        """)
        # migration: add sequence_order if missing
        r = c.execute(text(f"SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='{PRESET_TABLE}' AND COLUMN_NAME='sequence_order'")).fetchone()
        if not r:
            _run(c, f"ALTER TABLE {PRESET_TABLE} ADD sequence_order INT DEFAULT 9999")
        # seed default L30D preset if absent
        exists = c.execute(text(f"SELECT 1 FROM {PRESET_TABLE} WHERE preset_name = 'L30D'")).fetchone()
        if not exists:
            cfg = json.dumps({"months": [], "avg_days": 30, "kpi_type": "L30D"})
            c.execute(text(
                f"INSERT INTO {PRESET_TABLE}(preset_name,preset_type,description,config_json,sequence_order) "
                f"VALUES(:n,:t,:d,:cfg,:s)"
            ), {"n": "L30D", "t": "L30D", "d": "Last 30 days", "cfg": cfg, "s": 0})
            c.commit()

def _ensure_mapping_table(engine):
    with engine.connect() as c:
        _run(c, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{MAPPING_TABLE}')
            CREATE TABLE {MAPPING_TABLE} (
                mapping_name  NVARCHAR(255) PRIMARY KEY,
                mapping_json  NVARCHAR(MAX),
                fallback_json NVARCHAR(MAX),
                description   NVARCHAR(MAX),
                created_date  DATETIME DEFAULT GETDATE(),
                modified_date DATETIME DEFAULT GETDATE()
            )
        """)

def _ensure_assignment_table(engine):
    with engine.connect() as c:
        _run(c, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{ASSIGNMENT_TABLE}')
            CREATE TABLE {ASSIGNMENT_TABLE} (
                id           INT IDENTITY(1,1) PRIMARY KEY,
                col_name     NVARCHAR(255) NOT NULL,
                mapping_name NVARCHAR(255) NOT NULL,
                prefix       NVARCHAR(255) NULL,
                target       NVARCHAR(20) NOT NULL DEFAULT 'Both',
                created_date  DATETIME DEFAULT GETDATE(),
                modified_date DATETIME DEFAULT GETDATE()
            )
        """)
        r = c.execute(text(f"SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='{ASSIGNMENT_TABLE}' AND COLUMN_NAME='target'")).fetchone()
        if not r:
            _run(c, f"ALTER TABLE {ASSIGNMENT_TABLE} ADD target NVARCHAR(20) NOT NULL DEFAULT 'Both'")

        # Migration: add is_active flag for radio-button selection.
        # Only one assignment can have is_active=1 at a time; that one is the
        # one Execute will use. When none is active, the engine falls back to
        # the legacy "most recent wins" behaviour.
        r = c.execute(text(f"SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='{ASSIGNMENT_TABLE}' AND COLUMN_NAME='is_active'")).fetchone()
        if not r:
            _run(c, f"ALTER TABLE {ASSIGNMENT_TABLE} ADD is_active BIT NOT NULL DEFAULT 0")
            # Seed: mark the most recent existing row as active so current setups don't break
            _run(c, f"""
                UPDATE {ASSIGNMENT_TABLE}
                SET is_active = 1
                WHERE id = (SELECT MAX(id) FROM {ASSIGNMENT_TABLE})
            """)


def _ensure_job_table(engine):
    with engine.connect() as c:
        _run(c, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{JOB_TABLE}')
            CREATE TABLE {JOB_TABLE} (
                job_id       NVARCHAR(50) PRIMARY KEY,
                status       NVARCHAR(20) NOT NULL DEFAULT 'pending',
                label        NVARCHAR(500),
                payload_json NVARCHAR(MAX),
                log_json     NVARCHAR(MAX),
                duration     FLOAT NULL,
                store_rows   INT DEFAULT 0,
                company_rows INT DEFAULT 0,
                store_file   NVARCHAR(500) NULL,
                company_file NVARCHAR(500) NULL,
                error        NVARCHAR(MAX) NULL,
                created_at   DATETIME DEFAULT GETDATE(),
                finished_at  DATETIME NULL
            )
        """)

def _persist_job(job):
    """Save job record to DB for persistence across restarts."""
    try:
        engine = get_data_engine()
        _ensure_job_table(engine)
        with engine.connect() as c:
            exists = c.execute(text(f"SELECT 1 FROM {JOB_TABLE} WHERE job_id=:id"), {"id": job["id"]}).fetchone()
            params = {
                "id": job["id"], "status": job.get("status",""),
                "label": job.get("label",""), "payload": json.dumps(job.get("payload",{})),
                "log": json.dumps(job.get("log",[])), "duration": job.get("duration"),
                "sr": job.get("store_rows",0), "cr": job.get("company_rows",0),
                "sf": job.get("store_file"), "cf": job.get("company_file"),
                "err": job.get("error"),
            }
            if exists:
                _run(c, f"""UPDATE {JOB_TABLE} SET status=:status, log_json=:log, duration=:duration,
                    store_rows=:sr, company_rows=:cr, store_file=:sf, company_file=:cf, error=:err,
                    finished_at=CASE WHEN :status IN ('completed','failed','cancelled') THEN GETDATE() ELSE finished_at END
                    WHERE job_id=:id""", params)
            else:
                _run(c, f"""INSERT INTO {JOB_TABLE}(job_id,status,label,payload_json,log_json,duration,store_rows,company_rows,store_file,company_file,error)
                    VALUES(:id,:status,:label,:payload,:log,:duration,:sr,:cr,:sf,:cf,:err)""", params)
    except Exception:
        pass  # Don't break job execution if persistence fails

def _load_persisted_jobs():
    """Load completed/failed jobs from DB on startup."""
    try:
        engine = get_data_engine()
        _ensure_job_table(engine)
        with engine.connect() as c:
            rows = c.execute(text(f"SELECT job_id, status, label, log_json, duration, store_rows, company_rows, store_file, company_file, error, created_at, finished_at FROM {JOB_TABLE} ORDER BY created_at DESC")).fetchall()
        for r in rows:
            jid = r[0]
            if jid not in _jobs:
                _jobs[jid] = {
                    "id": jid, "status": r[1], "label": r[2],
                    "log": json.loads(r[3]) if r[3] else [],
                    "duration": r[4], "store_rows": r[5] or 0, "company_rows": r[6] or 0,
                    "store_file": r[7], "company_file": r[8], "error": r[9],
                    "created_at": r[10].isoformat() if r[10] else None,
                    "finished_at": r[11].isoformat() if r[11] else None,
                    "progress": "done", "payload": {},
                    "store_columns": [], "company_columns": [],
                    "store_preview": [], "company_preview": [],
                }
    except Exception:
        pass

_jobs_loaded = False
def _lazy_load_jobs():
    global _jobs_loaded
    if not _jobs_loaded:
        _jobs_loaded = True
        try:
            _load_persisted_jobs()
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/config/grouping-columns", response_model=APIResponse)
def get_grouping_columns(current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    try:
        with engine.connect() as c:
            rows = c.execute(text("""
                SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME='VW_MASTER_PRODUCT' AND TABLE_SCHEMA='dbo'
                AND COLUMN_NAME IN ('CLR','SZ','RNG_SEG','M_VND_CD','MACRO_MVGR','MICRO_MVGR','FAB','WEAVE_2','M_YARN_02')
                ORDER BY ORDINAL_POSITION
            """)).fetchall()
        cols = [r[0] for r in rows] or ['MACRO_MVGR']
    except Exception:
        cols = ['MACRO_MVGR']
    return APIResponse(success=True, data={"columns": cols})


@router.get("/config/ssn-values", response_model=APIResponse)
def get_ssn_values(current_user: User = Depends(get_current_user)):
    """Return distinct SSN values from VW_MASTER_PRODUCT."""
    engine = get_data_engine()
    try:
        df = pd.read_sql("SELECT DISTINCT SSN FROM dbo.VW_MASTER_PRODUCT WITH (NOLOCK) WHERE SSN IS NOT NULL AND SSN <> '' ORDER BY SSN", engine)
        ssn_list = df['SSN'].astype(str).tolist()
    except Exception:
        ssn_list = []
    return APIResponse(success=True, data={"ssn_values": ssn_list})


_months_cache = {"data": None, "ts": 0}

@router.get("/config/months", response_model=APIResponse)
def get_available_months(current_user: User = Depends(get_current_user)):
    # Cache for 5 minutes — months rarely change
    now = time.time()
    if _months_cache["data"] and (now - _months_cache["ts"]) < 300:
        return APIResponse(success=True, data={"months": _months_cache["data"]})

    engine = get_data_engine()
    df = _read_sql_nolock("""
        SELECT DISTINCT STOCK_DATE FROM dbo.COUNT_STOCK_DATA_18M WITH (NOLOCK)
        WHERE COALESCE(KPI,'') NOT IN ('L7D','L30D') ORDER BY STOCK_DATE DESC
    """, engine)
    df['STOCK_DATE'] = pd.to_datetime(df['STOCK_DATE'])
    months = sorted([str(m) for m in df['STOCK_DATE'].dt.date.unique()], reverse=True)
    _months_cache["data"] = months
    _months_cache["ts"] = now
    return APIResponse(success=True, data={"months": months})


@router.get("/config/majcats", response_model=APIResponse)
def get_majcats(grouping_column: str = "MACRO_MVGR",
                current_user: User = Depends(get_current_user)):
    if grouping_column not in VALID_GROUPING:
        grouping_column = "MACRO_MVGR"
    engine = get_data_engine()
    table = f"Master_HIER_{grouping_column}"
    try:
        df = pd.read_sql(f"SELECT DISTINCT MAJ_CAT FROM dbo.{table} WITH (NOLOCK) WHERE SEG IN ('APP','GM') ORDER BY MAJ_CAT", engine)
    except Exception:
        df = pd.read_sql("SELECT DISTINCT MAJ_CAT FROM dbo.Master_HIER_MACRO_MVGR WITH (NOLOCK) WHERE SEG IN ('APP','GM') ORDER BY MAJ_CAT", engine)
    return APIResponse(success=True, data={"majcats": df['MAJ_CAT'].tolist()})


# ══════════════════════════════════════════════════════════════════════════════
#  PRESETS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/presets", response_model=APIResponse)
def list_presets(current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    _ensure_preset_table(engine)
    with engine.connect() as c:
        rows = c.execute(text(f"SELECT preset_name, preset_type, description, config_json, sequence_order FROM {PRESET_TABLE} ORDER BY sequence_order")).fetchall()
    presets = []
    for r in rows:
        cfg = json.loads(r[3]) if r[3] else {}
        presets.append({
            "preset_name": r[0], "preset_type": r[1], "description": r[2],
            "months": cfg.get("months", []), "avg_days": cfg.get("avg_days", 30),
            "kpi_type": cfg.get("kpi_type", "L30D"), "sequence_order": r[4],
        })
    return APIResponse(success=True, data={"presets": presets, "total": len(presets)})


@router.post("/presets", response_model=APIResponse)
def save_preset(payload: PresetPayload, current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    _ensure_preset_table(engine)
    cfg = json.dumps({"months": payload.months, "avg_days": payload.avg_days,
                       "kpi_type": payload.kpi_type, "description": payload.description})
    with engine.connect() as c:
        exists = c.execute(text(f"SELECT 1 FROM {PRESET_TABLE} WHERE preset_name=:n"), {"n": payload.preset_name}).fetchone()
        if exists:
            _run(c, f"UPDATE {PRESET_TABLE} SET config_json=:cfg, description=:d, modified_date=GETDATE() WHERE preset_name=:n",
                 {"cfg": cfg, "d": payload.description, "n": payload.preset_name})
        else:
            mx = c.execute(text(f"SELECT ISNULL(MAX(sequence_order),0) FROM {PRESET_TABLE}")).scalar()
            _run(c, f"INSERT INTO {PRESET_TABLE}(preset_name,preset_type,description,config_json,sequence_order) VALUES(:n,:t,:d,:cfg,:s)",
                 {"n": payload.preset_name, "t": payload.kpi_type, "d": payload.description, "cfg": cfg, "s": mx + 1})
    return APIResponse(success=True, message=f"Preset '{payload.preset_name}' saved.")


@router.delete("/presets/{name}", response_model=APIResponse)
def delete_preset(name: str, current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    with engine.connect() as c:
        _run(c, f"DELETE FROM {PRESET_TABLE} WHERE preset_name=:n", {"n": name})
    return APIResponse(success=True, message=f"Preset '{name}' deleted.")


@router.put("/presets/reorder", response_model=APIResponse)
def reorder_presets(payload: PresetReorder, current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    with engine.connect() as c:
        for idx, name in enumerate(payload.sequence, 1):
            c.execute(text(f"UPDATE {PRESET_TABLE} SET sequence_order=:s, modified_date=GETDATE() WHERE preset_name=:n"),
                      {"s": idx, "n": name})
        c.commit()
    return APIResponse(success=True, message="Sequence updated.")


# ══════════════════════════════════════════════════════════════════════════════
#  MAPPINGS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/mappings", response_model=APIResponse)
def list_mappings(current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    _ensure_mapping_table(engine)
    with engine.connect() as c:
        rows = c.execute(text(f"SELECT mapping_name, mapping_json, fallback_json, description FROM {MAPPING_TABLE} ORDER BY mapping_name")).fetchall()
    items = []
    for r in rows:
        items.append({
            "mapping_name": r[0],
            "suffix_mapping": json.loads(r[1]) if r[1] else {},
            "fallback_suffixes": json.loads(r[2]) if r[2] else [],
            "description": r[3],
        })
    return APIResponse(success=True, data={"mappings": items, "total": len(items)})


@router.post("/mappings", response_model=APIResponse)
def save_mapping(payload: MappingPayload, current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    _ensure_mapping_table(engine)
    mj = json.dumps(payload.suffix_mapping)
    fj = json.dumps(payload.fallback_suffixes)
    with engine.connect() as c:
        exists = c.execute(text(f"SELECT 1 FROM {MAPPING_TABLE} WHERE mapping_name=:n"), {"n": payload.mapping_name}).fetchone()
        if exists:
            _run(c, f"UPDATE {MAPPING_TABLE} SET mapping_json=:mj, fallback_json=:fj, description=:d, modified_date=GETDATE() WHERE mapping_name=:n",
                 {"mj": mj, "fj": fj, "d": payload.description, "n": payload.mapping_name})
        else:
            _run(c, f"INSERT INTO {MAPPING_TABLE}(mapping_name,mapping_json,fallback_json,description) VALUES(:n,:mj,:fj,:d)",
                 {"n": payload.mapping_name, "mj": mj, "fj": fj, "d": payload.description})
    return APIResponse(success=True, message=f"Mapping '{payload.mapping_name}' saved.")


@router.delete("/mappings/{name}", response_model=APIResponse)
def delete_mapping(name: str, current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    with engine.connect() as c:
        _run(c, f"DELETE FROM {MAPPING_TABLE} WHERE mapping_name=:n", {"n": name})
    return APIResponse(success=True, message=f"Mapping '{name}' deleted.")


# ══════════════════════════════════════════════════════════════════════════════
#  ASSIGNMENTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/assignments", response_model=APIResponse)
def list_assignments(current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    _ensure_assignment_table(engine)
    with engine.connect() as c:
        rows = c.execute(text(
            f"SELECT id, col_name, mapping_name, prefix, target, is_active FROM {ASSIGNMENT_TABLE} ORDER BY id"
        )).fetchall()
    items = [{"id": r[0], "col_name": r[1], "mapping_name": r[2], "prefix": r[3],
              "target": r[4], "is_active": bool(r[5])} for r in rows]
    return APIResponse(success=True, data={"assignments": items})


@router.post("/assignments", response_model=APIResponse)
def save_assignment(payload: AssignmentPayload, current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    _ensure_assignment_table(engine)
    with engine.connect() as c:
        _run(c, f"INSERT INTO {ASSIGNMENT_TABLE}(col_name,mapping_name,prefix,target) VALUES(:c,:m,:p,:t)",
             {"c": payload.col_name, "m": payload.mapping_name, "p": payload.prefix, "t": payload.target})
    return APIResponse(success=True, message="Assignment saved.")


@router.delete("/assignments/{aid}", response_model=APIResponse)
def delete_assignment(aid: int, current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    with engine.connect() as c:
        _run(c, f"DELETE FROM {ASSIGNMENT_TABLE} WHERE id=:id", {"id": aid})
    return APIResponse(success=True, message="Assignment deleted.")


@router.post("/assignments/{aid}/activate", response_model=APIResponse)
def activate_assignment(aid: int, current_user: User = Depends(get_current_user)):
    """Make this assignment the active one. Clears `is_active` on every other row."""
    engine = get_data_engine()
    _ensure_assignment_table(engine)
    with engine.connect() as c:
        exists = c.execute(text(f"SELECT 1 FROM {ASSIGNMENT_TABLE} WHERE id = :id"), {"id": aid}).fetchone()
        if not exists:
            raise HTTPException(404, "Assignment not found")
        _run(c, f"UPDATE {ASSIGNMENT_TABLE} SET is_active = 0")
        _run(c, f"UPDATE {ASSIGNMENT_TABLE} SET is_active = 1 WHERE id = :id", {"id": aid})
    return APIResponse(success=True, message=f"Assignment {aid} is now active.")


@router.post("/assignments/clear-active", response_model=APIResponse)
def clear_active_assignment(current_user: User = Depends(get_current_user)):
    """Clear all is_active flags — pipeline falls back to 'all assignments run'."""
    engine = get_data_engine()
    _ensure_assignment_table(engine)
    with engine.connect() as c:
        _run(c, f"UPDATE {ASSIGNMENT_TABLE} SET is_active = 0")
    return APIResponse(success=True, message="Active assignment cleared.")


# ══════════════════════════════════════════════════════════════════════════════
#  EXECUTE PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def _get_grouping_expr(engine, grouping_column):
    """Return (expr, dtype) for the grouping column based on its SQL type."""
    with engine.connect() as c:
        dtype = c.execute(text("""
            SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME='VW_MASTER_PRODUCT' AND COLUMN_NAME=:col
        """), {"col": grouping_column}).scalar()
    if dtype and dtype.lower() in NUMERIC_SQL_TYPES:
        return grouping_column, dtype
    return f"COALESCE(NULLIF({grouping_column},''),'NA')", dtype


def _get_master_columns(engine, grouping_column):
    table = f"Master_HIER_{grouping_column}"
    with engine.connect() as c:
        rows = c.execute(text(f"""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME=:t AND TABLE_SCHEMA='dbo'
        """), {"t": table}).fetchall()
    exclude = {"UPLOAD_DATETIME", "upload_datetime"}
    return [r[0] for r in rows if r[0] not in exclude]


def _compute_kpis(df, avg_days, grouping_column):
    """Compute all KPI columns on a DataFrame (store or company level)."""
    Q, V = 1000.0, 100000.0
    num_cols = df.select_dtypes(include="number").columns
    df[num_cols] = df[num_cols].astype("float32")

    op_q, cl_q = df['OP_STK_Q'], df['CL_STK_Q']
    op_v, cl_v = df['OP_STK_V'], df['CL_STK_V']

    df['0001_STK_Q'] = np.where((op_q==0)&(cl_q==0), 0, (op_q+cl_q)/np.where((op_q!=0)&(cl_q!=0), 2, 1))
    df['0001_STK_V'] = np.where((op_v==0)&(cl_v==0), 0, (op_v+cl_v)/np.where((op_v!=0)&(cl_v!=0), 2, 1))

    df['FIX'] = df['0001_STK_Q']*Q / np.where(df['AVG_DNSTY']!=0, df['AVG_DNSTY'], 1)
    df['DISP_AREA'] = np.maximum(df['APF']*df['FIX'], np.where(df['SALE_V']>0, 1, 0))
    df['GM_%'] = df['GM_V'] / np.where(df['SALE_V']!=0, df['SALE_V'], 1)

    pdsq = np.where(df['SALE_Q']>0, df['SALE_Q']/avg_days, 0)*Q
    pdsv = np.where(df['SALE_V']>0, df['SALE_V']/avg_days, 0)*V

    df['STR'] = np.where(pdsq==0, 0, df['0001_STK_Q']/pdsq*Q)
    df['SALES PSF'] = np.where(df['DISP_AREA']==0, 0, pdsv/df['DISP_AREA'])

    group_cols = ['MAJ_CAT']
    if 'ST_CD' in df.columns:
        group_cols.insert(0, 'ST_CD')

    grp = df.groupby(group_cols, dropna=False)
    sv_sum = grp['SALE_V'].transform('sum')
    da_sum = grp['DISP_AREA'].transform('sum')
    gv_sum = grp['GM_V'].transform('sum')

    df['SALE_PSF_MJ'] = np.where(da_sum==0, 0, (sv_sum*V/da_sum)/avg_days)
    df['SALES_PSF_ACH%'] = np.where(df['SALE_PSF_MJ']==0, 0, df['SALES PSF']/df['SALE_PSF_MJ'])
    df['GM PSF'] = np.where(df['DISP_AREA']==0, 0, (df['GM_V']*V/df['DISP_AREA'])/avg_days)
    df['GM_PSF_MJ'] = np.where(da_sum==0, 0, (gv_sum*V/da_sum)/avg_days)
    gm_psf_clip = np.maximum(df['GM PSF'], 0)
    gm_psf_mj_clip = np.maximum(df['GM_PSF_MJ'], 0)
    df['GM_PSF_ACH%'] = np.where(gm_psf_mj_clip==0, 0, gm_psf_clip/gm_psf_mj_clip)

    # Contribution %
    pos_mask_stk = df['0001_STK_Q'] > 0
    pos_mask_sal = df['SALE_V'] > 0
    stk_sum = df.loc[pos_mask_stk].groupby(group_cols)['0001_STK_Q'].transform('sum')
    sal_sum = df.loc[pos_mask_sal].groupby(group_cols)['SALE_V'].transform('sum')
    df['STOCK_CONT%'] = np.where(~pos_mask_stk, 0, df['0001_STK_Q']/stk_sum)
    df['SALE_CONT%'] = np.where(~pos_mask_sal, 0, df['SALE_V']/sal_sum)

    # ALGO
    gr = 2 if grouping_column == 'M_VND_CD' else 1
    algo_raw = df['SALE_CONT%'] * np.where(df['SALE_CONT%']<0.05, 5.0, 3.0)
    algo_adj = df['SALE_CONT%'] * (1 + (df['GM_PSF_ACH%']-1)*gr)
    df['ALGO'] = np.minimum(algo_raw, np.maximum(np.maximum(algo_adj, df['SALE_CONT%']*0.5), 0))
    algo_sum = grp['ALGO'].transform('sum')
    df['INITIAL AUTO CONT%'] = np.where(algo_sum==0, 0, df['ALGO']/algo_sum)

    # Normalize
    df.replace([np.inf, -np.inf], np.nan, inplace=True)
    kpi_cols = ['0001_STK_Q','0001_STK_V','FIX','DISP_AREA','GM_%','STR','SALES PSF',
                'SALE_PSF_MJ','SALES_PSF_ACH%','GM PSF','GM_PSF_MJ','GM_PSF_ACH%',
                'STOCK_CONT%','SALE_CONT%','ALGO','INITIAL AUTO CONT%']
    for col in kpi_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0).round(2)
    return df


def _load_base_kpi_data(engine, grouping_column, majcats, selected_presets, all_presets):
    """Pull the inner-aggregated stock data ONCE for all selected presets.

    The previous design ran one full scan of COUNT_STOCK_DATA_18M per preset.
    Now we union all required (KPI, STOCK_DATE) slices into a single query and
    keep KPI + STOCK_DATE as columns. Each preset later filters this df and
    runs only the OUTER AVG-over-STOCK_DATE step in pandas, which preserves
    the existing math (AVG of NULLIF / CASE-WHEN-SALE_Q semantics).

    Returns a df indexed by (KPI, STOCK_DATE, ST_CD, MAJ_CAT, grouping) with
    the SUM(qty)/1000 + SUM(val)/100000 columns. Empty df if no presets
    selected or no matching rows.
    """
    if not selected_presets:
        return pd.DataFrame()

    need_l7d = False
    need_l30d = False
    l18m_months: set = set()
    for pname in selected_presets:
        cfg = all_presets.get(pname, {})
        kt = cfg.get("kpi_type", "L30D")
        if kt == "L7D" or pname == "L7D":
            need_l7d = True
        elif kt == "L30D" or pname == "L30D":
            need_l30d = True
        else:
            for m in cfg.get("months", []):
                if m:
                    l18m_months.add(m)

    or_parts = []
    if need_l7d:
        or_parts.append("sal_stk.KPI = 'L7D'")
    if need_l30d:
        or_parts.append("sal_stk.KPI = 'L30D'")
    if l18m_months:
        ms = "','".join(sorted(l18m_months))
        or_parts.append(f"(sal_stk.KPI = 'L18M' AND sal_stk.STOCK_DATE IN ('{ms}'))")
    if not or_parts:
        return pd.DataFrame()
    combined_kpi_filter = "(" + " OR ".join(or_parts) + ")"

    where_parts = []
    if majcats:
        safe = "','".join(m.replace("'", "''") for m in majcats)
        where_parts.append(f"prod.MAJ_CAT IN ('{safe}')")
    where_clause = " AND ".join(where_parts) if where_parts else "1=1"

    grouping_expr, _ = _get_grouping_expr(engine, grouping_column)

    # Inner-aggregation only — KPI + STOCK_DATE are kept as columns so each
    # preset can pick its slice in pandas. SUM divisors and SEG filter match
    # the original per-preset query exactly.
    base_query = f"""
        SELECT sal_stk.KPI, sal_stk.STOCK_DATE,
               sal_stk.WERKS AS ST_CD,
               prod.MAJ_CAT, prod.{grouping_column},
               COALESCE(SUM(sal_stk.OP_STK_QTY)/1000,0)   AS OP_STK_Q,
               COALESCE(SUM(sal_stk.OP_STK_VAL)/100000,0) AS OP_STK_V,
               COALESCE(SUM(sal_stk.CL_STK_QTY)/1000,0)   AS CL_STK_Q,
               COALESCE(SUM(sal_stk.CL_STK_VAL)/100000,0) AS CL_STK_V,
               COALESCE(SUM(sal_stk.SALE_QTY)/1000,0)     AS SALE_Q,
               COALESCE(SUM(sal_stk.SALE_VAL)/100000,0)   AS SALE_V,
               COALESCE(SUM(sal_stk.GM_VAL)/100000,0)     AS GM_V
        FROM dbo.COUNT_STOCK_DATA_18M sal_stk WITH (NOLOCK)
        LEFT JOIN (
            SELECT ARTICLE_NUMBER AS MATNR, MAJ_CAT,
                   {grouping_expr} AS {grouping_column}, SEG
            FROM dbo.VW_MASTER_PRODUCT WITH (NOLOCK)
        ) prod ON sal_stk.MATNR = prod.MATNR
        WHERE {where_clause} AND prod.SEG IN ('APP','GM') AND {combined_kpi_filter}
        GROUP BY sal_stk.KPI, sal_stk.STOCK_DATE, sal_stk.WERKS,
                 prod.MAJ_CAT, prod.{grouping_column}
    """
    return _read_sql_nolock(base_query, engine)


def _process_single_preset(engine, preset_name, preset_cfg, majcats, grouping_column,
                           avg_density, apf, df_master_cache=None, base_data=None):
    """Process one preset: slice base data → merge → KPI → return (detail_df, agg_df, timing, df_master).

    df_master_cache: reuse master query result across presets (big optimization).
    base_data: pre-loaded inner-aggregated stock data shared across presets.
               Each preset filters this in pandas and does the OUTER AVG step,
               replacing N full scans of COUNT_STOCK_DATA_18M with one shared pull.
    """
    timing = {}
    where_parts = []
    if majcats:
        safe = "','".join(m.replace("'","''") for m in majcats)
        where_parts.append(f"MAJ_CAT IN ('{safe}')")
    where_clause = " AND ".join(where_parts) if where_parts else "1=1"

    months = preset_cfg.get("months", [])
    kpi_type = preset_cfg.get("kpi_type", "L30D")
    avg_days = preset_cfg.get("avg_days", 30)

    grouping_expr, grouping_dtype = _get_grouping_expr(engine, grouping_column)

    # Step 1: Pull this preset's slice from the shared base data (computed once
    # at the job level in _load_base_kpi_data). Replaces the per-preset SQL.
    # The OUTER AVG step happens here in pandas using the same NULLIF /
    # CASE-WHEN-SALE_Q rules the SQL had — see the masking block below.
    #
    # IMPORTANT: do NOT round the per-row inputs — values are in lakhs and
    # often < 0.005, which would round to 0.00 and hide real contribution at
    # the company level. Final 2-dp rounding happens at the end of _compute_kpis.
    t = time.time()
    gcols = ['ST_CD', 'MAJ_CAT', grouping_column]
    avg_cols_zero  = ['OP_STK_Q', 'OP_STK_V', 'CL_STK_Q', 'CL_STK_V']
    avg_cols_sale  = ['SALE_Q', 'SALE_V', 'GM_V']
    all_avg_cols   = avg_cols_zero + avg_cols_sale

    if base_data is None or base_data.empty:
        df_data = pd.DataFrame(columns=gcols + all_avg_cols)
    else:
        # Filter to this preset's KPI / STOCK_DATE rows.
        if kpi_type == "L7D" or preset_name == "L7D":
            slice_df = base_data[base_data['KPI'] == 'L7D']
        elif kpi_type == "L30D" or preset_name == "L30D":
            slice_df = base_data[base_data['KPI'] == 'L30D']
        else:
            slice_df = base_data[
                (base_data['KPI'] == 'L18M')
                & (base_data['STOCK_DATE'].astype(str).isin([str(m) for m in months]))
            ]

        if slice_df.empty:
            df_data = pd.DataFrame(columns=gcols + all_avg_cols)
        else:
            # Apply masks that reproduce SQL's NULLIF / CASE-WHEN semantics.
            #   AVG(NULLIF(col, 0))                       → mean ignoring zero rows of that col
            #   AVG(CASE WHEN SALE_Q <> 0 THEN col END)   → mean over rows where SALE_Q != 0
            masked = slice_df.copy()
            sale_zero_mask = (masked['SALE_Q'] == 0)
            for c in avg_cols_zero:
                if c in masked.columns:
                    masked.loc[masked[c] == 0, c] = np.nan
            for c in avg_cols_sale:
                if c in masked.columns:
                    masked.loc[sale_zero_mask, c] = np.nan

            df_data = (
                masked.groupby(gcols, dropna=False)[all_avg_cols]
                      .mean()      # pandas .mean() skips NaN, matching SQL AVG ignoring NULLs
                      .reset_index()
            )
    timing["sql_data"] = round(time.time()-t, 2)

    if df_data.empty:
        return pd.DataFrame(), pd.DataFrame(), timing, df_master_cache

    # Step 2: Master hierarchy (CROSS JOIN) — cached across presets
    t = time.time()
    if df_master_cache is not None:
        df_master = df_master_cache.copy()
        timing["sql_master"] = 0  # cached
    else:
        hier_table = f"Master_HIER_{grouping_column}"
        hier_cols_list = _get_master_columns(engine, grouping_column)
        if not hier_cols_list:
            return pd.DataFrame(), pd.DataFrame(), timing, None
        hier_select = ", ".join(f"A.[{c}]" for c in hier_cols_list)
        master_query = f"""
            SELECT B.ST_CD, B.ST_NM, {hier_select}
            FROM {hier_table} A WITH (NOLOCK)
            CROSS JOIN dbo.Master_STORE_PLAN B WITH (NOLOCK)
            WHERE {where_clause}
        """
        df_master = _read_sql_nolock(master_query, engine)
        timing["sql_master"] = round(time.time()-t, 2)

    hier_cols_list = _get_master_columns(engine, grouping_column)

    # Step 3: Merge
    t = time.time()
    for col in ['ST_CD', 'MAJ_CAT']:
        df_master[col] = df_master[col].astype(str).str.strip()
        df_data[col] = df_data[col].astype(str).str.strip()

    if grouping_dtype and grouping_dtype.lower() in NUMERIC_SQL_TYPES:
        df_master[grouping_column] = pd.to_numeric(df_master[grouping_column], errors='coerce')
        df_data[grouping_column] = pd.to_numeric(df_data[grouping_column], errors='coerce')
    else:
        df_master[grouping_column] = df_master[grouping_column].astype(str).str.strip()
        df_data[grouping_column] = df_data[grouping_column].astype(str).str.strip()

    df_merged = pd.merge(df_master, df_data, on=['ST_CD','MAJ_CAT', grouping_column], how='left').fillna(0)
    if df_merged.empty:
        return pd.DataFrame(), pd.DataFrame(), timing, df_master

    df_merged = pd.merge(df_merged, avg_density[['MAJ_CAT','AVG_DNSTY']], on='MAJ_CAT', how='left')
    df_merged = pd.merge(df_merged, apf, on='ST_CD', how='left')
    timing["merge"] = round(time.time()-t, 2)

    # Step 4: Aggregation (company-level)
    t = time.time()
    apf_cols = apf.columns.tolist()
    exclude_agg = set(['ST_NM','AVG_DNSTY'] + apf_cols)
    agg_map = {c:'sum' for c in df_merged.columns if c not in hier_cols_list and c not in exclude_agg}
    df_agg = df_merged.groupby(hier_cols_list, dropna=False).agg(agg_map).reset_index()
    try:
        df_agg = pd.merge(df_agg, avg_density[['MAJ_CAT','AVG_DNSTY']], on='MAJ_CAT', how='left')
        df_agg['APF'] = 25
    except Exception:
        pass
    timing["aggregate"] = round(time.time()-t, 2)

    # Step 5: Compute KPIs
    t = time.time()
    df_detail = _compute_kpis(df_merged, avg_days, grouping_column)
    if not df_agg.empty:
        df_agg = _compute_kpis(df_agg, avg_days, grouping_column)
    timing["kpi"] = round(time.time()-t, 2)

    return df_detail, df_agg, timing, df_master


def _combine_dataframes(dataframes, is_aggregated, grouping_column, engine):
    """Combine preset results horizontally on shared merge keys."""
    if not dataframes:
        return pd.DataFrame()

    fix_cols = _get_master_columns(engine, grouping_column)
    avg_density = _read_sql_nolock("SELECT * FROM master_avg_density WITH (NOLOCK)", engine)
    apf = _read_sql_nolock("SELECT ST_CD, APF, STATUS, REF_ST_CD, REF_ST_NM, REF_GRP_NEW, REF_GRP_OLD FROM Master_STORE_PLAN WITH (NOLOCK)", engine)
    apf_columns = apf.columns.tolist()

    if is_aggregated:
        all_common = fix_cols + [grouping_column, 'AVG_DNSTY'] + apf_columns
    else:
        all_common = ['ST_CD','ST_NM'] + fix_cols + [grouping_column, 'AVG_DNSTY'] + apf_columns

    merge_keys = list(dict.fromkeys(
        c for c in all_common
        if all(df is not None and not df.empty and c in df.columns for df in dataframes.values())
    ))
    if not merge_keys:
        return pd.DataFrame()

    dfs = []
    for preset_name, df in dataframes.items():
        if df is None or df.empty:
            continue
        df = df.copy().loc[:, ~df.columns.duplicated()]
        df = df.drop_duplicates(subset=merge_keys)
        rename = {c: f"{c}|{preset_name}" for c in df.columns if c not in merge_keys}
        df = df.rename(columns=rename)
        df = df[merge_keys + list(rename.values())]
        num = df.select_dtypes(include="number").columns
        df[num] = df[num].astype("float32")
        dfs.append(df)

    if not dfs:
        return pd.DataFrame()

    combined = dfs[0]
    for d in dfs[1:]:
        combined = combined.merge(d, on=merge_keys, how="outer", copy=False, sort=False)
        gc.collect()

    combined["Generated_Date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return combined


def _apply_auto_cont_derivations(df, engine=None, vendor_col=None):
    """Append `AUTO CONT% 2` and `AUTO CONT% (FINAL)` columns derived from the mapping output.

    Input-column resolution (in priority order):
      1. The `col_name` of the most-recently-added row in `Cont_mapping_assignments`
         (max id). This means whatever name the user typed in the Assignments UI
         is automatically the input — no hardcoding.
      2. Fallback: known names `AUTO CONT%` / `AUTO SEG CONT%` (case-insensitive)
         for backward compatibility with data created before this change.

    AUTO CONT% 2:
        input    when  ACT_INACT == 'ACT'  AND  input >= 0.01
        0        otherwise

    AUTO CONT% (FINAL):
        Row's AUTO CONT% 2 / SUM(AUTO CONT% 2 within same (RDC_CD, MAJ_CAT) bucket).
        Within every (RDC_CD, MAJ_CAT), all rows' AUTO CONT% (FINAL) add up to 100%.
        Falls back to MAJ_CAT-only grouping if RDC_CD is missing.
    """
    if df is None or df.empty:
        return df

    # Default to zero so the pipeline can't NPE on missing inputs
    df['AUTO CONT% 2']        = 0.0
    df['AUTO CONT% (FINAL)']  = 0.0
    df['BGT CONT% (FINAL)']   = 0.0

    # 1) Try the user-selected active assignment first; fall back to most-recent (highest id)
    input_col = None
    if engine is not None:
        try:
            with engine.connect() as c:
                # Active first
                active_rows = c.execute(text(
                    f"SELECT col_name FROM {ASSIGNMENT_TABLE} WHERE is_active = 1 ORDER BY id DESC"
                )).fetchall()
                rest_rows = c.execute(text(
                    f"SELECT col_name FROM {ASSIGNMENT_TABLE} WHERE is_active = 0 ORDER BY id DESC"
                )).fetchall()
            assignment_cols = [r[0] for r in active_rows if r[0]] + [r[0] for r in rest_rows if r[0]]
            # Pick the first one that actually exists in df (case-insensitive)
            lower_map = {c.lower(): c for c in df.columns}
            for name in assignment_cols:
                hit = lower_map.get(name.lower())
                if hit:
                    input_col = hit
                    break
        except Exception as e:
            logger.warning(f"[contrib] Could not query {ASSIGNMENT_TABLE} for AUTO CONT% input: {e}")

    # 2) Fallback to the canonical names for backward compatibility
    if input_col is None:
        lower_map = {c.lower(): c for c in df.columns}
        input_col = next((lower_map[k] for k in ('auto cont%', 'auto seg cont%') if k in lower_map), None)

    if not input_col:
        logger.info("[contrib] No mapping-output column found for AUTO CONT% 2 derivation. "
                    "AUTO CONT% 2 / (FINAL) left at 0. Configure an assignment in Mappings.")
        return df

    auto_seg = pd.to_numeric(df[input_col], errors='coerce').fillna(0)

    if 'ACT_INACT' in df.columns:
        act = df['ACT_INACT'].astype(str).str.strip().str.upper().eq('ACT')
    else:
        act = pd.Series(True, index=df.index)

    df['AUTO CONT% 2'] = np.where(act & (auto_seg >= 0.01), auto_seg, 0.0)

    if 'MAJ_CAT' not in df.columns:
        logger.warning("[contrib] MAJ_CAT missing — AUTO CONT% (FINAL) left at 0.")
        return df

    auto2 = pd.to_numeric(df['AUTO CONT% 2'], errors='coerce').fillna(0)

    # Sum AUTO CONT% 2 within (RDC_CD, MAJ_CAT) when RDC_CD is available; else within MAJ_CAT only
    group_cols = [c for c in ['RDC_CD', 'MAJ_CAT'] if c in df.columns]
    if not group_cols:
        return df
    grp_sum = auto2.groupby([df[c] for c in group_cols], dropna=False).transform('sum')

    df['AUTO CONT% (FINAL)'] = np.where(
        grp_sum == 0, 0.0,
        auto2 / grp_sum.replace(0, np.nan),
    )
    df['AUTO CONT% (FINAL)'] = pd.to_numeric(df['AUTO CONT% (FINAL)'], errors='coerce').fillna(0).round(4)
    df['AUTO CONT% 2']       = df['AUTO CONT% 2'].round(4)

    # ── BGT CONT% (FINAL) ──────────────────────────────────────────────────
    # Per row, decide based on the SUM of MERCH_INPUT within the chosen group:
    #   if group_sum > 0  → BGT CONT% (FINAL) = this row's MERCH_INPUT
    #   else              → BGT CONT% (FINAL) = this row's AUTO CONT% (FINAL)
    #
    # Grouping rules:
    #   - Store-level table (ST_CD present) + vendor_col known → (MAJ_CAT, vendor_col)
    #     so the merch decision is per (MAJCAT, vendor) — same decision across all
    #     stores carrying that vendor in that MAJCAT.
    #   - Otherwise                                            → MAJ_CAT only
    #     (company-level behaviour unchanged).
    df['BGT CONT% (FINAL)'] = 0.0
    if 'MAJ_CAT' in df.columns and 'MERCH_INPUT' in df.columns:
        merch = pd.to_numeric(df['MERCH_INPUT'], errors='coerce').fillna(0)
        store_level = 'ST_CD' in df.columns
        use_vendor  = store_level and vendor_col and vendor_col in df.columns
        if use_vendor:
            group_keys = [df['MAJ_CAT'], df[vendor_col]]
        else:
            group_keys = [df['MAJ_CAT']]
        merch_grp_sum = merch.groupby(group_keys, dropna=False).transform('sum')
        df['BGT CONT% (FINAL)'] = np.where(
            merch_grp_sum > 0,
            merch,
            pd.to_numeric(df['AUTO CONT% (FINAL)'], errors='coerce').fillna(0),
        )
        df['BGT CONT% (FINAL)'] = pd.to_numeric(df['BGT CONT% (FINAL)'], errors='coerce').fillna(0).round(4)
        if use_vendor:
            logger.debug(f"[contrib] BGT CONT% (FINAL) grouped by (MAJ_CAT, {vendor_col}) — store-level table")
    else:
        logger.warning("[contrib] MAJ_CAT or MERCH_INPUT missing — BGT CONT% (FINAL) left at 0.")

    # ── Trim intermediate columns from store-level output ──────────────────
    # At store level the calculation for these columns isn't meaningful —
    # only BGT CONT% (FINAL) is kept, and it's inherited from the company
    # table downstream. Company tables keep all columns unchanged.
    if 'ST_CD' in df.columns:
        drop_cols = ['AUTO CONT% 2', 'AUTO CONT% (FINAL)']
        if input_col and input_col in df.columns and input_col not in ('MAJ_CAT', 'ST_CD', 'MERCH_INPUT'):
            drop_cols.append(input_col)
        df.drop(columns=[c for c in drop_cols if c in df.columns], inplace=True, errors='ignore')

    return df


def _inherit_company_bgt_final(df_store, df_company_mem, engine, gc_col):
    """Overwrite df_store['BGT CONT% (FINAL)'] with the value from the company-level
    table for the same (MAJ_CAT, vendor) pair. This propagates the merchant's
    national decision down to every store row carrying that vendor.

    Lookup priority:
      1. `df_company_mem` (in-memory company frame from the same run, target=Both)
      2. Latest `Cont_Percentage_<gc>_CO_<month>` table in DB (target=Store standalone)
      3. None → fall back to df_store's locally-computed BGT CONT% (FINAL)

    Required columns in df_store: MAJ_CAT, vendor_col (=gc_col), BGT CONT% (FINAL).
    If any are missing, df_store is returned unchanged.

    Returns (df_store, inherited: bool). The flag is True only when a real
    company source was found and at least one row's BGT CONT% (FINAL) was
    overwritten — used to gate the V-0015 store contribution chain.
    """
    if df_store is None or df_store.empty:
        return df_store, False
    if 'BGT CONT% (FINAL)' not in df_store.columns:
        return df_store, False
    if 'MAJ_CAT' not in df_store.columns or not gc_col or gc_col not in df_store.columns:
        logger.info("[contrib] Cannot inherit company BGT CONT% (FINAL) — MAJ_CAT or grouping column missing in store frame.")
        return df_store, False

    company_bgt = None
    source = None
    needed = ['MAJ_CAT', gc_col, 'BGT CONT% (FINAL)']

    if df_company_mem is not None and not df_company_mem.empty and all(c in df_company_mem.columns for c in needed):
        company_bgt = df_company_mem[needed].copy()
        source = "in-memory company df"
    else:
        # Q1b: tables are now timestamped per execution (YYYY_MM_DD_HHMM), so
        # there's no single "this month's" CO table. Find the LATEST one for
        # this gc by lexicographic name DESC — the suffix is zero-padded so
        # name sort = chronological. Falls back to legacy YYYY_MM tables too.
        safe_gc = gc_col.upper().replace(' ', '_').replace('-', '_')
        co_table = None
        try:
            with engine.connect() as c:
                row = c.execute(text(
                    "SELECT TOP 1 TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_NAME LIKE :pat ORDER BY TABLE_NAME DESC"
                ), {"pat": f"{TABLE_PREFIX}_{safe_gc}_CO_%"}).fetchone()
            if not row:
                logger.info(
                    f"[contrib] No company table matching '{TABLE_PREFIX}_{safe_gc}_CO_*' — "
                    "store BGT CONT% (FINAL) keeps local value."
                )
                return df_store, False
            co_table = row[0]
            company_bgt = _read_sql_nolock(
                f"SELECT [MAJ_CAT], [{gc_col}], [BGT CONT% (FINAL)] FROM [{co_table}] WITH (NOLOCK)",
                engine,
            )
            source = co_table
        except Exception as e:
            logger.warning(f"[contrib] Could not read company table for inheritance: {e}")
            return df_store, False

    if company_bgt is None or company_bgt.empty:
        return df_store, False

    # Type-align merge keys
    df_store['MAJ_CAT'] = df_store['MAJ_CAT'].astype(str).str.strip()
    df_store[gc_col]    = df_store[gc_col].astype(str).str.strip()
    company_bgt['MAJ_CAT'] = company_bgt['MAJ_CAT'].astype(str).str.strip()
    company_bgt[gc_col]    = company_bgt[gc_col].astype(str).str.strip()

    # Drop duplicate (MAJ_CAT, vendor) pairs on the company side (defensive)
    company_bgt = company_bgt.drop_duplicates(subset=['MAJ_CAT', gc_col])
    company_bgt = company_bgt.rename(columns={'BGT CONT% (FINAL)': '_co_bgt_final'})

    before_cols = list(df_store.columns)
    df_store = df_store.merge(company_bgt, on=['MAJ_CAT', gc_col], how='left')
    co_vals = pd.to_numeric(df_store['_co_bgt_final'], errors='coerce')
    df_store['BGT CONT% (FINAL)'] = np.where(
        co_vals.notna(),
        co_vals,
        pd.to_numeric(df_store['BGT CONT% (FINAL)'], errors='coerce').fillna(0),
    )
    df_store['BGT CONT% (FINAL)'] = pd.to_numeric(df_store['BGT CONT% (FINAL)'], errors='coerce').fillna(0).round(4)
    df_store.drop(columns=['_co_bgt_final'], inplace=True, errors='ignore')

    matched = int(co_vals.notna().sum())
    logger.info(f"[contrib] BGT CONT% (FINAL) inherited from {source}: {matched}/{len(df_store)} store rows updated")
    return df_store, matched > 0


def _apply_store_contribution_chain(df, engine=None, vendor_col=None):
    """V-0015 store-level contribution chain (sheet ST-MJ-CAT-SEG).

    Runs only when ST_CD is present (store-level dataframe). Produces 13 columns
    that match the V-0015 store sheet, in the order they appear there:

        NAT CONT%, NAT CONT% @ MAJ,
        AUTO CONT%-1, BGT CONT%, RMN AUTO,
        BGT CONT%@MAJ_CAT, RMN AUTO @ MAJCAT, ALGO, AUTO CONT%-2,
        OLD ST CONT%,
        INT ST CONT%, INT-2 ST CONT%, FINAL ST CONT%

    NEW-store pipeline (AQ..BC in the Excel) is computed internally and the
    intermediate columns are NOT surfaced. Only the OLD-pipeline visible cols
    and the final INT/INT-2/FINAL stay.

    Inputs required in df:
        STATUS, SSN, ST_CD, MAJ_CAT, RNG_SEG, LISTING,
        REF_GRP_NEW, REF_GRP_OLD,
        BGT CONT% (FINAL) (will be renamed to NAT CONT%),
        INITIAL AUTO CONT%|<period> for L7D, L30D, SSN_TLM, SSN-2.
    """
    if df is None or df.empty or 'ST_CD' not in df.columns:
        return df
    if 'MAJ_CAT' not in df.columns:
        logger.warning("[contrib] Store chain skipped — MAJ_CAT missing")
        return df

    # ── NAT CONT% (renamed from inherited 'BGT CONT% (FINAL)') ──
    if 'BGT CONT% (FINAL)' in df.columns:
        df['NAT CONT%'] = pd.to_numeric(df['BGT CONT% (FINAL)'], errors='coerce').fillna(0).astype(float)
    elif 'NAT CONT%' not in df.columns:
        df['NAT CONT%'] = 0.0
    nat = pd.to_numeric(df['NAT CONT%'], errors='coerce').fillna(0).astype(float)

    # ── STATUS / SSN / LISTING input series ──
    status = df['STATUS'].astype(str).str.strip().str.upper() if 'STATUS' in df.columns else pd.Series([''] * len(df), index=df.index)
    is_old  = status.eq('OLD')
    is_new  = status.eq('NEW')
    is_old1 = status.eq('OLD-1')
    is_upc  = status.eq('UPC')

    ssn = df['SSN'].astype(str).str.strip().str.upper() if 'SSN' in df.columns else pd.Series([''] * len(df), index=df.index)
    is_w_pw = ssn.isin(['W', 'PW'])
    is_sao  = ssn.isin(['S', 'A', 'OC'])

    if 'LISTING' in df.columns:
        listing = pd.to_numeric(df['LISTING'], errors='coerce').fillna(0).astype(float)
    else:
        listing = pd.Series(1.0, index=df.index)

    # ── Period columns ── (best-effort match on suffix)
    def _find_period(*hints):
        norm = lambda s: s.upper().replace(' ', '').replace('-', '').replace('_', '')
        for c in df.columns:
            if not c.startswith('INITIAL AUTO CONT%|'):
                continue
            suf_norm = norm(c.split('|', 1)[1])
            for h in hints:
                if norm(h) in suf_norm:
                    return c
        return None

    def _period(col):
        if col and col in df.columns:
            return pd.to_numeric(df[col], errors='coerce').fillna(0).astype(float)
        return pd.Series(0.0, index=df.index)

    l7d     = _period(_find_period('L7D'))
    l30d    = _period(_find_period('L30D'))
    ssn_tlm = _period(_find_period('SSNTLM'))
    ssn2    = _period(_find_period('SSN2'))

    # SUMIFS within (ST_CD, MAJ_CAT)
    grp_keys = [df['ST_CD'], df['MAJ_CAT']]
    def _sum_within(series):
        return series.groupby(grp_keys, dropna=False).transform('sum')

    # ── 1. NAT CONT% @ MAJ ──
    df['NAT CONT% @ MAJ'] = _sum_within(nat).round(4)

    # ── 2. BGT CONT% ── (50% OLD, 70% else)
    bgt = pd.Series(np.where(is_old, nat * 0.5, nat * 0.7), index=df.index).astype(float)
    df['BGT CONT%'] = bgt.round(4)

    # ── 3. AUTO CONT%-1 (OLD pipeline) ──
    val_default = pd.concat(
        [pd.Series(0.0, index=df.index), l7d, l30d, ssn_tlm, ssn2], axis=1
    ).max(axis=1)
    auto1_old = np.where(is_w_pw, ssn2.clip(lower=0),
                np.where(is_sao, l30d.clip(lower=0), val_default))
    auto1 = pd.Series(np.where(is_old, auto1_old * listing, 0.0), index=df.index).astype(float)
    df['AUTO CONT%-1'] = auto1.round(4)

    # ── 4. RMN AUTO ──
    rmn = pd.Series(np.maximum(auto1 - bgt, 0.0), index=df.index)
    df['RMN AUTO'] = rmn.round(4)

    # ── 5. BGT @ MAJCAT, RMN @ MAJCAT ──
    bgt_maj = _sum_within(bgt)
    rmn_maj = _sum_within(rmn)
    df['BGT CONT%@MAJ_CAT'] = bgt_maj.round(4)
    df['RMN AUTO @ MAJCAT']        = rmn_maj.round(4)

    # ── 6. ALGO ──
    rmn_share = np.where(rmn_maj > 0, rmn.values / rmn_maj.replace(0, np.nan).values, 0.0)
    algo = pd.Series(rmn_share, index=df.index).fillna(0) * np.maximum(1 - bgt_maj, 0.0)
    df['ALGO'] = algo.round(4)

    # ── 7. AUTO CONT%-2 (OLD pipeline) ──
    auto2 = pd.Series(np.where(is_old, (algo + bgt) * listing, 0.0), index=df.index)
    df['AUTO CONT%-2'] = auto2.round(4)

    # ── 8. OLD ST CONT% ──
    auto2_maj = _sum_within(auto2)
    old_st = np.where(auto2_maj > 0, auto2.values / auto2_maj.replace(0, np.nan).values, 0.0)
    df['OLD ST CONT%'] = pd.Series(old_st, index=df.index).fillna(0).round(4)
    old_st_s = df['OLD ST CONT%']

    # ──────────────────────────────────────────────────────────────────────
    #  NEW-store pipeline — computed internally, not surfaced as columns
    # ──────────────────────────────────────────────────────────────────────

    # Peer-store reference: average OLD ST CONT% across stores where
    #   (peer.MAJ_CAT, peer.RNG_SEG, peer.REF_GRP_OLD) == (my.MAJ_CAT, my.RNG_SEG, my.REF_GRP_NEW)
    # AND peer.OLD ST CONT% > 0
    has_ref = 'REF_GRP_NEW' in df.columns and 'REF_GRP_OLD' in df.columns and 'RNG_SEG' in df.columns
    new_ref = pd.Series(0.0, index=df.index)
    if has_ref:
        pos_mask = old_st_s > 0
        peer = df.loc[pos_mask, ['MAJ_CAT', 'RNG_SEG', 'REF_GRP_OLD']].copy()
        peer['_old_ct'] = old_st_s.loc[pos_mask].values
        peer_avg = (peer.groupby(['MAJ_CAT', 'RNG_SEG', 'REF_GRP_OLD'], dropna=False)
                        ['_old_ct'].mean()
                        .rename('_peer_avg').reset_index()
                        .rename(columns={'REF_GRP_OLD': 'REF_GRP_NEW'}))
        left = df[['MAJ_CAT', 'RNG_SEG', 'REF_GRP_NEW']].reset_index().rename(columns={'index': '_orig_idx'})
        merged = left.merge(peer_avg, on=['MAJ_CAT', 'RNG_SEG', 'REF_GRP_NEW'], how='left')
        new_ref = pd.Series(
            pd.to_numeric(merged['_peer_avg'], errors='coerce').fillna(0).values,
            index=df.index,
        )
    new_ref = pd.Series(np.where(is_old, 0.0, new_ref.values), index=df.index)

    # ALGO CONT% (AR)
    new_ref_maj = _sum_within(new_ref)
    algo_cont = pd.Series(
        np.where(new_ref_maj > 0, new_ref.values / new_ref_maj.replace(0, np.nan).values, 0.0),
        index=df.index,
    ).fillna(0)

    # NEW AUTO-1 (AS)
    new_auto1_old1 = np.where(is_w_pw, ssn2.clip(lower=0),
                     np.where(is_sao, l30d.clip(lower=0), 0.0))
    new_auto1_new  = pd.concat([l7d, l30d], axis=1).max(axis=1).values
    new_auto1 = np.where(is_old, 0.0,
                np.where(is_old1, new_auto1_old1,
                np.where(is_new, new_auto1_new, val_default.values)))
    new_auto1 = new_auto1 * listing.values

    # NEW AUTO-2 (AT)
    new_auto2 = np.where(new_auto1 > 0, new_auto1, algo_cont.values * 0.5) * listing.values

    # RMN AUTO new (AU)
    rmn_new = pd.Series(
        np.where(is_old, 0.0, np.maximum(new_auto2 - bgt.values, 0.0)),
        index=df.index,
    )

    # ALGO new (AW)
    rmn_new_maj = _sum_within(rmn_new)
    rmn_new_share = np.where(rmn_new_maj > 0, rmn_new.values / rmn_new_maj.replace(0, np.nan).values, 0.0)
    algo_new = pd.Series(rmn_new_share, index=df.index).fillna(0) * np.maximum(1 - bgt_maj, 0.0)
    algo_new = pd.Series(np.where(is_old, 0.0, algo_new.values), index=df.index)

    # AUTO CONT%-2 new (AX)
    auto2_new = pd.Series(
        np.where(is_old, 0.0, (algo_new + bgt) * listing),
        index=df.index,
    )

    # NEW ST CONT% (AY)
    auto2_new_maj = _sum_within(auto2_new)
    ay = pd.Series(
        np.where(auto2_new_maj > 0, auto2_new.values / auto2_new_maj.replace(0, np.nan).values, 0.0),
        index=df.index,
    ).fillna(0)

    # AZ: OLD→0, UPC→AR, AY>0→AY, else→AR
    az = pd.Series(
        np.where(is_old, 0.0,
        np.where(is_upc, algo_cont.values,
        np.where(ay > 0, ay.values, algo_cont.values))),
        index=df.index,
    )

    # ALGO COINT% (BA)
    az_maj = _sum_within(az)
    ba = pd.Series(
        np.where(az_maj > 0, az.values / az_maj.replace(0, np.nan).values, 0.0),
        index=df.index,
    ).fillna(0)

    # BB (col 53): OLD→0, else (BA>0 ? BA : NAT) × LISTING
    bb = pd.Series(
        np.where(is_old, 0.0, np.where(ba > 0, ba.values, nat.values)) * listing.values,
        index=df.index,
    )

    # NEW ST CONT% (BC)
    bb_maj = _sum_within(bb)
    bc = pd.Series(
        np.where(bb_maj > 0, bb.values / bb_maj.replace(0, np.nan).values, 0.0),
        index=df.index,
    ).fillna(0)

    # ──────────────────────────────────────────────────────────────────────
    #  Final output columns
    # ──────────────────────────────────────────────────────────────────────

    # ── 9. INT ST CONT% (BD): OLD path or NEW path ──
    int_st = pd.Series(
        np.where(is_old, old_st_s.values, bc.values),
        index=df.index,
    )
    df['INT ST CONT%'] = int_st.fillna(0).round(4)

    # ── 10. INT-2 ST CONT% (BE): 1% threshold ──
    df['INT-2 ST CONT%'] = pd.Series(
        np.where(int_st < 0.01, 0.0, int_st.values), index=df.index,
    ).fillna(0).round(4)
    int2 = pd.to_numeric(df['INT-2 ST CONT%'], errors='coerce').fillna(0)

    # ── 11. FINAL ST CONT% (BF): normalise per (ST_CD, MAJ_CAT) ──
    int2_maj = _sum_within(int2)
    final_st = np.where(int2_maj > 0, int2.values / int2_maj.replace(0, np.nan).values, 0.0)
    df['FINAL ST CONT%'] = pd.Series(final_st, index=df.index).fillna(0).round(4)

    # Drop the inherited BGT CONT% (FINAL) — replaced by NAT CONT% in store table
    if 'BGT CONT% (FINAL)' in df.columns:
        df.drop(columns=['BGT CONT% (FINAL)'], inplace=True, errors='ignore')

    return df


def _apply_mapping_assignments(df, engine):
    """Apply mapping assignments to compute final columns.

    Selection rule:
      • Only the assignment row with `is_active = 1` runs.
      • If no row is active, falls back to legacy "all assignments run" behaviour
        so existing setups don't silently break.
    """
    _ensure_assignment_table(engine)
    _ensure_mapping_table(engine)
    with engine.connect() as c:
        active = c.execute(text(
            f"SELECT col_name, mapping_name, prefix, target FROM {ASSIGNMENT_TABLE} WHERE is_active = 1"
        )).fetchall()
        if active:
            assignments = [dict(r._mapping) for r in active]
            logger.info(f"[contrib] Using active assignment(s): {len(assignments)} row(s)")
        else:
            assignments = [dict(r._mapping) for r in c.execute(text(
                f"SELECT col_name, mapping_name, prefix, target FROM {ASSIGNMENT_TABLE}"
            ))]
            logger.info(f"[contrib] No active assignment — fallback to all {len(assignments)} row(s) (legacy mode)")
        mappings_raw = {r[0]: {"suffix_mapping": json.loads(r[1]) if r[1] else {}, "fallback_suffixes": json.loads(r[2]) if r[2] else []}
                        for r in c.execute(text(f"SELECT mapping_name, mapping_json, fallback_json FROM {MAPPING_TABLE}"))}

    for a in assignments:
        mname = a.get("mapping_name")
        if not mname or mname not in mappings_raw:
            continue
        m = mappings_raw[mname]
        prefix = a.get("prefix", "INITIAL AUTO CONT%|")
        col_name = a.get("col_name", "RESULT")

        nrows = len(df)
        ssn = df.get("SSN", pd.Series([None]*nrows))
        result = np.full(nrows, np.nan, dtype=float)

        for key, suffixes in m["suffix_mapping"].items():
            suf_list = suffixes if isinstance(suffixes, list) else [suffixes]
            full_cols = [prefix+s for s in suf_list if (prefix+s) in df.columns]
            if not full_cols:
                continue
            vals = df[full_cols].apply(pd.to_numeric, errors='coerce').to_numpy()
            row_max = np.nanmax(vals, axis=1)
            mask = (ssn == key).to_numpy()
            result[mask] = row_max[mask]

        fb_cols = [prefix+s for s in m["fallback_suffixes"] if (prefix+s) in df.columns]
        if fb_cols:
            fb_max = df[fb_cols].apply(pd.to_numeric, errors='coerce').max(axis=1).fillna(0).to_numpy()
        else:
            fb_max = np.zeros(nrows)

        has_map = ssn.isin(m["suffix_mapping"].keys()).to_numpy()
        # Compute the chosen value, then fill any leftover NaN with 0 so the
        # column never carries blanks into downstream steps (AUTO CONT% 2, FINAL, BGT).
        # NaN arises when an SSN matched a mapping key but every suffix column
        # for that row was NaN (typical after the outer-merge across presets).
        chosen = np.where(has_map, result, fb_max)
        df[col_name] = pd.to_numeric(pd.Series(chosen), errors='coerce').fillna(0).to_numpy()

    return df


def _save_to_db(engine, df, table_name, retries=3, progress_cb=None):
    """Save DataFrame to DB using raw pyodbc fast_executemany with retry on connection drop.

    Args:
        engine: SQLAlchemy engine
        df: DataFrame to save
        table_name: Target table name
        retries: Number of retry attempts on connection failure
        progress_cb: Optional callback(inserted_rows, total_rows) for progress tracking
    """
    # Clean data once (reused across retries)
    df_out = df.copy()
    df_out.replace([np.inf, -np.inf], np.nan, inplace=True)
    for c in df_out.select_dtypes(include=['float32']).columns:
        df_out[c] = df_out[c].astype('float64')
    for c in df_out.select_dtypes(include=['float64', 'float']).columns:
        df_out[c] = df_out[c].round(4)

    cols = list(df_out.columns)
    ncols = len(cols)

    # Build column defs with proper types — use FLOAT for numeric to avoid overflow (22003)
    col_defs = []
    for c in cols:
        dt = df_out[c].dtype
        if pd.api.types.is_float_dtype(dt) or pd.api.types.is_integer_dtype(dt):
            col_defs.append(f"[{c}] FLOAT NULL")
        else:
            col_defs.append(f"[{c}] NVARCHAR(450) NULL")

    # Convert NaN to None for all columns (vectorized) — do once
    for c in cols:
        mask = df_out[c].isna()
        if mask.any():
            df_out[c] = df_out[c].astype(object)
            df_out.loc[mask, c] = None

    # Pre-compute tuples once to avoid re-conversion on retry
    BATCH = 50000
    batches = []
    for start in range(0, len(df_out), BATCH):
        chunk = df_out.iloc[start:start+BATCH]
        batches.append(list(chunk.itertuples(index=False, name=None)))

    col_list = ", ".join(f"[{c}]" for c in cols)
    placeholders = ", ".join(["?"] * ncols)
    insert_sql = f"INSERT INTO [{table_name}] ({col_list}) VALUES ({placeholders})"

    last_err = None
    for attempt in range(retries + 1):
        try:
            if attempt > 0:
                wait = min(2 ** attempt, 10)
                logger.warning(f"_save_to_db retry {attempt}/{retries} for {table_name} after {wait}s wait")
                time.sleep(wait)
                engine.dispose()  # Reset connection pool on retry

            raw_conn = engine.raw_connection()
            try:
                cursor = raw_conn.cursor()
                cursor.fast_executemany = True

                # Create-if-not-exists (Q1b): per-execution timestamped table
                # names mean we shouldn't ever clobber a prior run. On the rare
                # collision (two runs in the same minute for the same gc), this
                # falls through to an append on the existing table.
                cursor.execute(
                    f"IF OBJECT_ID('{table_name}','U') IS NULL "
                    f"CREATE TABLE [{table_name}] ({', '.join(col_defs)})"
                )
                raw_conn.commit()

                # Insert in batches
                inserted = 0
                for batch_rows in batches:
                    cursor.executemany(insert_sql, batch_rows)
                    inserted += len(batch_rows)
                    if progress_cb:
                        progress_cb(inserted, len(df_out))

                raw_conn.commit()
                return  # Success
            finally:
                raw_conn.close()

        except Exception as e:
            last_err = e
            err_str = str(e)
            is_connection_error = any(code in err_str for code in ('10054', '08S01', 'Communication link', '08001', 'TCP Provider'))
            if attempt < retries and is_connection_error:
                continue
            raise last_err


# ── Job queue helpers ────────────────────────────────────────────────────────

def _update_job(job_id, persist=False, **kwargs):
    with _job_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)
            if persist:
                _persist_job(_jobs[job_id])

def _run_job(job_id):
    """Execute a single job (runs in worker thread)."""
    with _job_lock:
        job = _jobs.get(job_id)
        if not job or job["status"] == "cancelled":
            return
    _update_job(job_id, status="running", started_at=datetime.now().isoformat())
    logger.info(f"[Job {job_id}] Started — payload: {json.dumps(job.get('payload',{}), default=str)[:300]}")

    try:
        payload = job["payload"]
        engine = get_data_engine()
        gc_col = payload["grouping_column"]
        if gc_col not in VALID_GROUPING:
            gc_col = "MACRO_MVGR"

        _ensure_preset_table(engine)
        with engine.connect() as c:
            rows = c.execute(text(f"SELECT preset_name, config_json, sequence_order FROM {PRESET_TABLE} ORDER BY sequence_order")).fetchall()
        all_presets = {r[0]: json.loads(r[1]) if r[1] else {} for r in rows}

        selected = payload.get("presets") or list(all_presets.keys())
        if payload.get("use_sequence", True):
            seq_order = [r[0] for r in rows]
            selected = [p for p in seq_order if p in selected]

        majcats = payload.get("majcats") or []
        if not majcats:
            try:
                df_mj = pd.read_sql(f"SELECT DISTINCT MAJ_CAT FROM dbo.Master_HIER_{gc_col} WITH (NOLOCK) WHERE SEG IN ('APP','GM')", engine)
                majcats = df_mj['MAJ_CAT'].tolist()
            except Exception:
                majcats = []

        logger.info(f"[Job {job_id}] Loading master data (avg_density, APF)...")
        t_master = time.time()
        avg_density = _read_sql_nolock("SELECT * FROM master_avg_density WITH (NOLOCK)", engine)
        apf = _read_sql_nolock("SELECT ST_CD, APF, STATUS, REF_ST_CD, REF_ST_NM, REF_GRP_NEW, REF_GRP_OLD FROM Master_STORE_PLAN WITH (NOLOCK)", engine)
        master_load_time = round(time.time()-t_master, 2)
        logger.info(f"[Job {job_id}] Master data loaded in {master_load_time}s (avg_density: {len(avg_density)} rows, APF: {len(apf)} rows)")

        t0 = time.time()
        results = {}
        log = [{"step": "master_load", "duration": master_load_time}]
        df_master_cache = None  # Cache CROSS JOIN result across presets

        # Single base SQL pull — replaces N per-preset scans of COUNT_STOCK_DATA_18M.
        # Returns inner-aggregated rows keyed by (KPI, STOCK_DATE, ST_CD, MAJ_CAT,
        # grouping); each preset later filters this df in pandas and runs only
        # the OUTER AVG step.
        logger.info(f"[Job {job_id}] Loading shared base KPI data...")
        t_base = time.time()
        try:
            base_data = _load_base_kpi_data(engine, gc_col, majcats, selected, all_presets)
        except Exception as e:
            logger.error(f"[Job {job_id}] Base data load failed: {e}")
            base_data = pd.DataFrame()
        base_load_dur = round(time.time() - t_base, 2)
        logger.info(
            f"[Job {job_id}] Base data loaded in {base_load_dur}s "
            f"({len(base_data):,} inner-aggregated rows)"
        )
        log.append({"step": "base_kpi_data", "duration": base_load_dur, "rows": int(len(base_data))})

        cancelled = False
        for idx, pname in enumerate(selected, 1):
            while True:
                with _job_lock:
                    st = _jobs.get(job_id, {}).get("status")
                if st == "cancelled":
                    cancelled = True
                    log.append({"preset": pname, "status": "cancelled"})
                    break
                if st == "paused":
                    time.sleep(1)
                    continue
                break
            if cancelled:
                break

            _update_job(job_id, progress=f"{idx}/{len(selected)} {pname}")

            if pname not in all_presets:
                log.append({"preset": pname, "status": "skipped", "reason": "not found"})
                continue
            try:
                logger.info(f"[Job {job_id}] Processing preset {idx}/{len(selected)}: {pname}")
                t1 = time.time()
                df_det, df_agg, step_timing, df_master_cache = _process_single_preset(
                    engine, pname, all_presets[pname], majcats, gc_col,
                    avg_density, apf, df_master_cache, base_data=base_data)
                dur = round(time.time()-t1, 2)
                if df_det.empty:
                    logger.warning(f"[Job {job_id}] Preset {pname} returned empty in {dur}s")
                    log.append({"preset": pname, "status": "empty", "duration": dur, "timing": step_timing})
                else:
                    logger.info(f"[Job {job_id}] Preset {pname}: {len(df_det):,} rows in {dur}s")
                    results[pname] = {"detail": df_det, "aggregated": df_agg}
                    log.append({"preset": pname, "status": "ok", "rows": len(df_det),
                                "duration": dur, "timing": step_timing})
            except Exception as e:
                logger.error(f"[Job {job_id}] Preset {pname} failed: {e}")
                log.append({"preset": pname, "status": "error", "error": str(e)[:500]})

        if not results:
            _update_job(job_id, persist=True, status="failed", log=log, finished_at=datetime.now().isoformat(), error="No data produced")
            return

        # Combine
        logger.info(f"[Job {job_id}] Combining {len(results)} presets...")
        _update_job(job_id, progress="combining presets...")
        detail_dfs = {k: v["detail"] for k, v in results.items()}
        agg_dfs = {k: v["aggregated"] for k, v in results.items() if not v["aggregated"].empty}

        target = payload.get("target", "Both")
        t = time.time()
        df_store = _combine_dataframes(detail_dfs, False, gc_col, engine) if target != "Company" else pd.DataFrame()
        df_company = _combine_dataframes(agg_dfs, True, gc_col, engine) if target != "Store" and agg_dfs else pd.DataFrame()
        combine_dur = round(time.time()-t, 2)
        logger.info(f"[Job {job_id}] Combined in {combine_dur}s — store: {len(df_store):,} rows/{len(df_store.columns) if not df_store.empty else 0} cols, company: {len(df_company):,} rows/{len(df_company.columns) if not df_company.empty else 0} cols")
        log.append({"step": "combine", "duration": combine_dur,
                     "store_cols": len(df_store.columns) if not df_store.empty else 0,
                     "company_cols": len(df_company.columns) if not df_company.empty else 0})

        # Mapping assignments
        t = time.time()
        if not df_store.empty:
            df_store = _apply_mapping_assignments(df_store, engine)
        if not df_company.empty:
            df_company = _apply_mapping_assignments(df_company, engine)
        log.append({"step": "mappings", "duration": round(time.time()-t, 2)})

        # Derived contribution columns (AUTO CONT% 2, AUTO CONT% (FINAL), BGT CONT% (FINAL))
        # Pass gc_col as the vendor column so BGT CONT% (FINAL) can group by
        # (MAJ_CAT, vendor) when running on store-level data.
        t = time.time()
        if not df_store.empty:
            df_store = _apply_auto_cont_derivations(df_store, engine, vendor_col=gc_col)
        if not df_company.empty:
            df_company = _apply_auto_cont_derivations(df_company, engine, vendor_col=gc_col)
        log.append({"step": "auto_cont_derivations", "duration": round(time.time()-t, 2)})

        # Store-level BGT CONT% (FINAL) inherits from the company table for the
        # same (MAJ_CAT, vendor). Falls back to local value when no company table
        # exists. This enforces the workflow: run Company → merchant decides →
        # run Store → store rows pick up the national decision.
        co_inherited = False
        if not df_store.empty:
            t = time.time()
            df_store, co_inherited = _inherit_company_bgt_final(df_store, df_company, engine, gc_col)
            log.append({"step": "store_inherit_company_bgt", "duration": round(time.time()-t, 2),
                        "inherited": co_inherited})

        # V-0015 store contribution chain — 13 derived columns ending at FINAL ST CONT%.
        # Only run when a real company source was found (in-memory df_company or
        # an existing Cont_Percentage_<gc>_CO_<month> table). When the user runs
        # target=Store alone and no company table exists, the chain is skipped so
        # the store table doesn't carry meaningless V-0015 columns.
        if not df_store.empty and co_inherited:
            t = time.time()
            df_store = _apply_store_contribution_chain(df_store, engine, vendor_col=gc_col)
            log.append({"step": "store_contribution_chain", "duration": round(time.time()-t, 2)})
        elif not df_store.empty:
            logger.info("[contrib] Skipping V-0015 store chain — no company source found. "
                        "Run target=Company first (with save_to_db=true) to enable the chain.")
            log.append({"step": "store_contribution_chain", "skipped": True,
                        "reason": "no_company_source"})

        compute_dur = round(time.time()-t0, 2)

        # ── Save preview + pickle FIRST so user can view/download immediately ──
        _update_job(job_id, progress="preparing results...")
        t = time.time()
        tmp_dir = os.path.join(tempfile.gettempdir(), "contrib_jobs")
        os.makedirs(tmp_dir, exist_ok=True)
        store_file = company_file = None
        if not df_store.empty:
            store_file = os.path.join(tmp_dir, f"{job_id}_store.pkl")
            df_store.to_pickle(store_file)
        if not df_company.empty:
            company_file = os.path.join(tmp_dir, f"{job_id}_company.pkl")
            df_company.to_pickle(company_file)
        log.append({"step": "save_temp", "duration": round(time.time()-t, 2)})

        # Mark as COMPLETED now — user can view preview + download
        _update_job(job_id, persist=True,
            status="completed",
            log=list(log),
            duration=compute_dur,
            store_rows=len(df_store),
            company_rows=len(df_company),
            store_columns=list(df_store.columns) if not df_store.empty else [],
            company_columns=list(df_company.columns) if not df_company.empty else [],
            store_preview=json.loads(df_store.head(200).to_json(orient="records", date_format="iso")) if not df_store.empty else [],
            company_preview=json.loads(df_company.head(200).to_json(orient="records", date_format="iso")) if not df_company.empty else [],
            finished_at=datetime.now().isoformat(),
            store_file=store_file,
            company_file=company_file,
        )

        # ── Save to DB AFTER marking complete (with retry) ──
        if payload.get("save_to_db"):
            _update_job(job_id, progress="saving to database...")
            # Per-execution timestamped suffix so each run produces its own
            # snapshot (Q1b — was YYYY_MM, overwrote same-month runs). Both
            # Store and Company tables of this job share the same ts_tag so
            # they pair up cleanly for downstream inheritance.
            ts_tag = datetime.now().strftime('%Y_%m_%d_%H%M')
            safe_gc = gc_col.upper().replace(' ','_').replace('-','_')

            def _make_progress_cb(label):
                def cb(inserted, total):
                    _update_job(job_id, progress=f"saving {label}: {inserted:,}/{total:,} rows")
                return cb

            try:
                if not df_store.empty:
                    t = time.time()
                    tbl = f"{TABLE_PREFIX}_{safe_gc}_{ts_tag}"
                    logger.info(f"[Job {job_id}] Saving store to DB: {tbl} ({len(df_store):,} rows)")
                    _save_to_db(engine, df_store, tbl, retries=3, progress_cb=_make_progress_cb("store"))
                    dur_s = round(time.time()-t, 2)
                    logger.info(f"[Job {job_id}] Store saved in {dur_s}s")
                    log.append({"action": "saved_store", "table": tbl, "rows": len(df_store),
                                 "duration": dur_s})
                if not df_company.empty:
                    t = time.time()
                    tbl = f"{TABLE_PREFIX}_{safe_gc}_CO_{ts_tag}"
                    logger.info(f"[Job {job_id}] Saving company to DB: {tbl} ({len(df_company):,} rows)")
                    _save_to_db(engine, df_company, tbl, retries=3, progress_cb=_make_progress_cb("company"))
                    dur_c = round(time.time()-t, 2)
                    logger.info(f"[Job {job_id}] Company saved in {dur_c}s")
                    log.append({"action": "saved_company", "table": tbl, "rows": len(df_company),
                                 "duration": dur_c})
                total_dur = round(time.time()-t0, 2)
                _update_job(job_id, persist=True, log=log, duration=total_dur, progress="saved to DB")
            except Exception as save_err:
                logger.error(f"[Job {job_id}] Save to DB failed after retries: {save_err}")
                log.append({"action": "save_error", "error": str(save_err)[:500]})
                _update_job(job_id, persist=True, log=log, progress="save failed")

    except Exception as e:
        logger.error(f"[Job {job_id}] FAILED: {e}")
        _update_job(job_id, persist=True, status="failed", error=str(e)[:1000], finished_at=datetime.now().isoformat())


JOB_AUTO_DELETE_DELAY = 1800  # seconds after completion before auto-delete (30 min — gives users time to download)

def _auto_delete_job(job_id, delay=JOB_AUTO_DELETE_DELAY):
    """Auto-delete a completed/failed job after a delay to allow frontend to fetch results."""
    time.sleep(delay)
    with _job_lock:
        job = _jobs.pop(job_id, None)
        if job_id in _job_queue:
            _job_queue.remove(job_id)
    if job:
        # Clean up temp files
        for key in ("store_file", "company_file"):
            path = job.get(key)
            if path and os.path.exists(path):
                try: os.remove(path)
                except: pass
        # Delete from DB
        try:
            engine = get_data_engine()
            with engine.connect() as conn:
                conn.execute(text(f"DELETE FROM {JOB_TABLE} WHERE job_id = :jid"), {"jid": job_id})
                conn.commit()
        except Exception:
            pass
        logger.info(f"[Job {job_id}] Auto-deleted after {delay}s")


def _start_job(job_id):
    """Start a job in its own thread for parallel execution."""
    def wrapper():
        _run_job(job_id)
        with _job_lock:
            if job_id in _job_queue:
                _job_queue.remove(job_id)
        gc.collect()
        # Schedule auto-delete after completion
        with _job_lock:
            job = _jobs.get(job_id)
        if job and job.get("status") in ("completed", "failed", "cancelled"):
            threading.Thread(target=_auto_delete_job, args=(job_id,), daemon=True).start()
    t = threading.Thread(target=wrapper, daemon=True)
    t.start()


@router.post("/execute", response_model=APIResponse)
def execute_pipeline(payload: ExecutePayload, current_user: User = Depends(get_current_user)):
    """Create a background job to run the contribution pipeline."""
    job_id = str(uuid.uuid4())[:8]
    presets_label = ", ".join(payload.presets[:3]) if payload.presets else "all"
    if len(payload.presets) > 3:
        presets_label += f" +{len(payload.presets)-3}"

    job = {
        "id": job_id,
        "status": "pending",
        "payload": payload.dict(),
        "label": f"{payload.grouping_column} | {presets_label}",
        "created_at": datetime.now().isoformat(),
        "started_at": None,
        "finished_at": None,
        "progress": "queued",
        "log": [],
        "duration": None,
        "store_rows": 0, "company_rows": 0,
        "store_columns": [], "company_columns": [],
        "store_preview": [], "company_preview": [],
        "error": None,
    }
    with _job_lock:
        _jobs[job_id] = job
        _job_queue.append(job_id)

    _start_job(job_id)
    logger.info(f"[Job {job_id}] Created — grouping={payload.grouping_column}, presets={presets_label}, save_to_db={payload.save_to_db}, target={payload.target}")

    return APIResponse(success=True, message=f"Job {job_id} started", data={"job_id": job_id})


@router.get("/jobs", response_model=APIResponse)
def list_jobs(current_user: User = Depends(get_current_user)):
    """List all jobs (most recent first)."""
    _lazy_load_jobs()
    with _job_lock:
        jobs = list(reversed(_jobs.values()))
    # Return summary without heavy preview data
    summaries = []
    for j in jobs:
        summaries.append({
            "id": j["id"], "status": j["status"], "label": j.get("label",""),
            "progress": j.get("progress"), "created_at": j.get("created_at"),
            "started_at": j.get("started_at"), "finished_at": j.get("finished_at"),
            "duration": j.get("duration"), "error": j.get("error"),
            "store_rows": j.get("store_rows", 0), "company_rows": j.get("company_rows", 0),
        })
    return APIResponse(success=True, data={"jobs": summaries})


@router.get("/jobs/{job_id}", response_model=APIResponse)
def get_job(job_id: str, current_user: User = Depends(get_current_user)):
    """Get full job details including preview data."""
    _lazy_load_jobs()
    with _job_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return APIResponse(success=True, data={"job": job})


@router.post("/jobs/{job_id}/cancel", response_model=APIResponse)
def cancel_job(job_id: str, current_user: User = Depends(get_current_user)):
    """Cancel a pending or running job."""
    with _job_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job["status"] in ("pending", "running", "paused"):
            job["status"] = "cancelled"
            job["finished_at"] = datetime.now().isoformat()
            logger.info(f"[Job {job_id}] Cancelled")
    return APIResponse(success=True, message=f"Job {job_id} cancelled")


@router.post("/jobs/{job_id}/pause", response_model=APIResponse)
def pause_job(job_id: str, current_user: User = Depends(get_current_user)):
    """Pause a running job. It will wait at the next preset boundary."""
    with _job_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job["status"] == "running":
            job["status"] = "paused"
    return APIResponse(success=True, message=f"Job {job_id} paused")


@router.post("/jobs/{job_id}/resume", response_model=APIResponse)
def resume_job(job_id: str, current_user: User = Depends(get_current_user)):
    """Resume a paused job."""
    with _job_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job["status"] == "paused":
            job["status"] = "running"
    return APIResponse(success=True, message=f"Job {job_id} resumed")


MAX_ROWS_PER_FILE = 800_000

def _download_from_table(table_name, label):
    """Download from DB table — split by DIV/SEG for large files. Used by both Execute and Review."""
    engine = get_data_engine()

    with engine.connect() as conn:
        total = conn.execute(text(f"SELECT COUNT(*) FROM [{table_name}] WITH (NOLOCK)")).scalar()

    if total <= MAX_ROWS_PER_FILE:
        def csv_stream():
            first = True
            for chunk in pd.read_sql(f"SELECT * FROM [{table_name}] WITH (NOLOCK)", engine, chunksize=100000):
                yield chunk.to_csv(index=False, header=first)
                first = False
        return StreamingResponse(csv_stream(), media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={label}.csv"})
    else:
        # Split by SEG/DIV
        with engine.connect() as conn:
            cols = [r[0] for r in conn.execute(text(f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='{table_name}'")).fetchall()]
        has_seg = 'SEG' in cols
        has_div = 'DIV' in cols

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            if has_seg and has_div:
                segs = pd.read_sql(f"SELECT DISTINCT SEG FROM [{table_name}] WITH (NOLOCK)", engine)['SEG'].tolist()
                for seg_val in segs:
                    s_seg = re.sub(r'[^A-Za-z0-9_-]', '_', str(seg_val))[:30]
                    if str(seg_val).upper() == 'GM':
                        parts = []
                        for chunk in pd.read_sql(f"SELECT * FROM [{table_name}] WITH (NOLOCK) WHERE SEG='{seg_val}'", engine, chunksize=100000):
                            parts.append(chunk.to_csv(index=False, header=len(parts)==0))
                        zf.writestr(f"{label}_SEG_{s_seg}.csv", "".join(parts))
                    else:
                        divs = pd.read_sql(f"SELECT DISTINCT DIV FROM [{table_name}] WITH (NOLOCK) WHERE SEG='{seg_val}'", engine)['DIV'].tolist()
                        for div_val in divs:
                            s_div = re.sub(r'[^A-Za-z0-9_-]', '_', str(div_val))[:30]
                            parts = []
                            for chunk in pd.read_sql(f"SELECT * FROM [{table_name}] WITH (NOLOCK) WHERE SEG='{seg_val}' AND DIV='{div_val}'", engine, chunksize=100000):
                                parts.append(chunk.to_csv(index=False, header=len(parts)==0))
                            zf.writestr(f"{label}_{s_seg}_{s_div}.csv", "".join(parts))
            elif has_div:
                divs = pd.read_sql(f"SELECT DISTINCT DIV FROM [{table_name}] WITH (NOLOCK)", engine)['DIV'].tolist()
                for div_val in divs:
                    s_div = re.sub(r'[^A-Za-z0-9_-]', '_', str(div_val))[:30]
                    parts = []
                    for chunk in pd.read_sql(f"SELECT * FROM [{table_name}] WITH (NOLOCK) WHERE DIV='{div_val}'", engine, chunksize=100000):
                        parts.append(chunk.to_csv(index=False, header=len(parts)==0))
                    zf.writestr(f"{label}_{s_div}.csv", "".join(parts))
            else:
                parts = []
                for chunk in pd.read_sql(f"SELECT * FROM [{table_name}] WITH (NOLOCK)", engine, chunksize=100000):
                    parts.append(chunk.to_csv(index=False, header=len(parts)==0))
                zf.writestr(f"{label}.csv", "".join(parts))
        buf.seek(0)
        return StreamingResponse(buf, media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={label}.zip"})


@router.get("/jobs/{job_id}/download/{result_type}")
def download_job_result(job_id: str, result_type: str,
                        current_user: User = Depends(get_current_user)):
    """Download job result — from pkl if available, else from DB table."""
    logger.info(f"[Job {job_id}] Download requested: {result_type}")
    # Ensure persisted jobs are loaded — covers the case where the backend
    # restarted after the job ran and the in-memory _jobs dict is empty.
    _lazy_load_jobs()
    with _job_lock:
        job = _jobs.get(job_id)

    # Try pkl file first (fast, local)
    if job:
        file_key = "store_file" if result_type == "store" else "company_file"
        pkl_path = job.get(file_key)
        if pkl_path and os.path.exists(pkl_path):
            label = f"contrib_{result_type}_{job_id}"
            df = pd.read_pickle(pkl_path)
            if len(df) <= MAX_ROWS_PER_FILE:
                def csv_stream():
                    yield df.head(0).to_csv(index=False)
                    for i in range(0, len(df), 100000):
                        yield df.iloc[i:i+100000].to_csv(index=False, header=False)
                return StreamingResponse(csv_stream(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={label}.csv"})
            else:
                # Large file — write split ZIP
                buf = io.BytesIO()
                has_seg = 'SEG' in df.columns
                has_div = 'DIV' in df.columns
                with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                    if has_seg and has_div:
                        for seg_val, seg_grp in df.groupby('SEG'):
                            s_seg = re.sub(r'[^A-Za-z0-9_-]', '_', str(seg_val))[:30]
                            if str(seg_val).upper() == 'GM':
                                zf.writestr(f"{label}_SEG_{s_seg}.csv", seg_grp.to_csv(index=False))
                            else:
                                for div_val, div_grp in seg_grp.groupby('DIV'):
                                    s_div = re.sub(r'[^A-Za-z0-9_-]', '_', str(div_val))[:30]
                                    zf.writestr(f"{label}_{s_seg}_{s_div}.csv", div_grp.to_csv(index=False))
                    elif has_div:
                        for div_val, grp in df.groupby('DIV'):
                            s_div = re.sub(r'[^A-Za-z0-9_-]', '_', str(div_val))[:30]
                            zf.writestr(f"{label}_{s_div}.csv", grp.to_csv(index=False))
                    else:
                        for i in range(0, len(df), MAX_ROWS_PER_FILE):
                            zf.writestr(f"{label}_part{i//MAX_ROWS_PER_FILE+1}.csv",
                                        df.iloc[i:i+MAX_ROWS_PER_FILE].to_csv(index=False))
                buf.seek(0)
                return StreamingResponse(buf, media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename={label}.zip"})

    # Fallback: read from DB table (after auto-delete or restart)
    gc_col = job.get("payload", {}).get("grouping_column", "MACRO_MVGR") if job else "MACRO_MVGR"
    month_tag = datetime.now().strftime('%Y_%m')
    safe_gc = gc_col.upper().replace(' ','_').replace('-','_')
    if result_type == "company":
        table_name = f"{TABLE_PREFIX}_{safe_gc}_CO_{month_tag}"
    else:
        table_name = f"{TABLE_PREFIX}_{safe_gc}_{month_tag}"

    # Check the table actually exists before querying — give a useful error otherwise.
    engine = get_data_engine()
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME=:t"
        ), {"t": table_name}).fetchone()
    if not exists:
        raise HTTPException(
            410,
            f"Job result is no longer available. The temporary file expired after "
            f"{JOB_AUTO_DELETE_DELAY//60} minutes and no '{table_name}' table exists "
            f"in the database. Re-run the job with 'Save to database' checked, or "
            f"download within {JOB_AUTO_DELETE_DELAY//60} minutes of completion.")

    label = f"contrib_{result_type}"
    try:
        return _download_from_table(table_name, label)
    except Exception as e:
        raise HTTPException(500, f"Download failed: {str(e)[:200]}")


@router.delete("/jobs/{job_id}", response_model=APIResponse)
def delete_job(job_id: str, current_user: User = Depends(get_current_user)):
    """Delete a job and its temp files."""
    with _job_lock:
        job = _jobs.pop(job_id, None)
        if job_id in _job_queue:
            _job_queue.remove(job_id)
    if not job:
        # Still try to delete from DB even if not in memory
        pass

    # Clean up temp files
    if job:
        for key in ("store_file", "company_file"):
            path = job.get(key)
            if path:
                for ext in ('', '.csv'):
                    f = path if not ext else path.replace('.pkl', ext)
                    if f and os.path.exists(f):
                        try: os.remove(f)
                        except: pass

    # Delete from DB
    try:
        engine = get_data_engine()
        with engine.connect() as conn:
            conn.execute(text(f"DELETE FROM {JOB_TABLE} WHERE job_id = :jid"), {"jid": job_id})
            conn.commit()
    except Exception:
        pass

    return APIResponse(success=True, message=f"Job {job_id} deleted")


# ══════════════════════════════════════════════════════════════════════════════
#  REVIEW
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/review/tables", response_model=APIResponse)
def list_result_tables(current_user: User = Depends(get_current_user)):
    engine = get_data_engine()
    try:
        insp = inspect(engine)
        all_tables = insp.get_table_names()
        tables = sorted([t for t in all_tables if t.upper().startswith(TABLE_PREFIX.upper())])
    except Exception:
        tables = []
    return APIResponse(success=True, data={"tables": tables, "total": len(tables)})


FILTER_COLUMNS = ['ST_CD', 'ST_NM', 'SEG', 'DIV', 'SUB_DIV', 'MAJ_CAT', 'SSN', 'ACT_INACT', 'STATUS']

@router.get("/review/preview/{table_name}", response_model=APIResponse)
def preview_table(table_name: str, request: Request, limit: int = Query(500),
                  current_user: User = Depends(get_current_user)):
    """Preview table with optional server-side filters.
    Filters passed as query params: f_SEG=APP,GM&f_MAJ_CAT=FW_W_SHOES
    """
    if not table_name.upper().startswith(TABLE_PREFIX.upper()):
        raise HTTPException(400, "Invalid table name")
    engine = get_data_engine()
    safe = table_name.replace("'","").replace(";","")

    # Get all columns in this table
    with engine.connect() as conn:
        all_cols = {r[0] for r in conn.execute(text(
            f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='{safe}'"
        )).fetchall()}

    # Parse filters from query params (f_COL=val1,val2)
    filters = {}
    for key, val in request.query_params.items():
        if key.startswith("f_") and val:
            col = key[2:]
            if col in all_cols:
                filters[col] = [v.strip() for v in val.split(",") if v.strip()]

    where = _build_where_clause(filters, all_cols)
    where_sql = f" WHERE {where}" if where else ""

    df = pd.read_sql(f"SELECT TOP {limit} * FROM [{safe}] WITH (NOLOCK){where_sql}", engine)
    for c in df.select_dtypes(include=['float64', 'float32', 'float']).columns:
        df[c] = df[c].round(4)

    with engine.connect() as c:
        total = c.execute(text(f"SELECT COUNT(*) FROM [{safe}] WITH (NOLOCK)")).scalar()
        filtered_total = c.execute(text(f"SELECT COUNT(*) FROM [{safe}] WITH (NOLOCK){where_sql}")).scalar() if where else total

    # Build filter options: distinct values from the FULL table (always unfiltered)
    filter_options = {}
    for fc in FILTER_COLUMNS:
        if fc in all_cols:
            try:
                vals = pd.read_sql(f"SELECT DISTINCT [{fc}] FROM [{safe}] WITH (NOLOCK) WHERE [{fc}] IS NOT NULL ORDER BY [{fc}]", engine)
                filter_options[fc] = vals[fc].astype(str).tolist()
            except Exception:
                pass

    return APIResponse(success=True,
        data={
            "columns": list(df.columns), "total_rows": total,
            "filtered_rows": filtered_total,
            "preview": json.loads(df.to_json(orient="records", date_format="iso")),
            "filter_options": filter_options,
        })


@router.get("/review/download/{table_name}")
def download_table(table_name: str, current_user: User = Depends(get_current_user)):
    if not table_name.upper().startswith(TABLE_PREFIX.upper()):
        raise HTTPException(400, "Invalid table name")
    safe = table_name.replace("'","").replace(";","")
    return _download_from_table(safe, safe)


@router.delete("/review/tables/{table_name}", response_model=APIResponse)
def delete_result_table(table_name: str, current_user: User = Depends(get_current_user)):
    if not table_name.upper().startswith(TABLE_PREFIX.upper()):
        raise HTTPException(400, "Invalid table name")
    engine = get_data_engine()
    safe = table_name.replace("'","").replace(";","")
    with engine.connect() as c:
        _run(c, f"IF OBJECT_ID('{safe}','U') IS NOT NULL DROP TABLE [{safe}]")
    return APIResponse(success=True, message=f"Table '{safe}' deleted.")


# ══════════════════════════════════════════════════════════════════════════════
#  REVIEW — Background Export Jobs
# ══════════════════════════════════════════════════════════════════════════════

_export_jobs: OrderedDict = OrderedDict()   # export_id → job dict
_export_lock = threading.Lock()


class ExportCancelled(Exception):
    """Raised inside _run_export_job when the user cancels the export.
    Bubbles up to the worker's outer except so we can mark status='cancelled'
    and clean up the partial file."""


def _check_export_cancel(export_id: str) -> None:
    """Soft-cancel checkpoint — call between chunk iterations. Raises
    ExportCancelled if the user requested cancellation via the cancel endpoint."""
    with _export_lock:
        job = _export_jobs.get(export_id)
        if job and job.get("cancel_requested"):
            raise ExportCancelled()

def _build_where_clause(filters, table_cols=None):
    """Build SQL WHERE clause from filter dict. Only includes columns that exist in the table."""
    clauses = []
    for col, vals in filters.items():
        if not vals or not isinstance(vals, list):
            continue
        safe_col = col.replace("'", "").replace(";", "")
        if table_cols and safe_col not in table_cols:
            continue
        escaped = [v.replace("'", "''") for v in vals]
        in_list = ",".join(f"'{v}'" for v in escaped)
        clauses.append(f"[{safe_col}] IN ({in_list})")
    return " AND ".join(clauses) if clauses else ""


def _run_export_job(export_id):
    """Background thread: export a table to a file on disk."""
    with _export_lock:
        job = _export_jobs.get(export_id)
        if not job:
            return
        job["status"] = "running"
        job["started_at"] = datetime.now().isoformat()

    table_name = job["table_name"]
    filters = job.get("filters", {})
    logger.info(f"[Export {export_id}] Starting export of {table_name}" + (f" filters={filters}" if filters else ""))

    try:
        engine = get_data_engine()
        safe = table_name.replace("'", "").replace(";", "")

        # Get table columns for filter validation
        with engine.connect() as conn:
            table_cols = [r[0] for r in conn.execute(text(f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='{safe}'")).fetchall()]

        # Build WHERE clause from filters
        where = _build_where_clause(filters, table_cols)
        where_sql = f" WHERE {where}" if where else ""

        # Get total rows (with filters)
        with engine.connect() as conn:
            total = conn.execute(text(f"SELECT COUNT(*) FROM [{safe}] WITH (NOLOCK){where_sql}")).scalar()
        with _export_lock:
            job["total_rows"] = total
        logger.info(f"[Export {export_id}] {table_name}: {total:,} rows to export")

        # Prepare output directory
        tmp_dir = os.path.join(tempfile.gettempdir(), "contrib_exports")
        os.makedirs(tmp_dir, exist_ok=True)

        base_query = f"SELECT * FROM [{safe}] WITH (NOLOCK){where_sql}"

        if total <= MAX_ROWS_PER_FILE:
            # Single CSV file
            out_path = os.path.join(tmp_dir, f"{export_id}_{safe}.csv")
            written = 0
            with open(out_path, 'w', newline='', encoding='utf-8') as f:
                first = True
                for chunk in pd.read_sql(base_query, engine, chunksize=100000):
                    _check_export_cancel(export_id)
                    f.write(chunk.to_csv(index=False, header=first))
                    first = False
                    written += len(chunk)
                    with _export_lock:
                        job["processed_rows"] = written
            with _export_lock:
                job["file_path"] = out_path
                job["file_name"] = f"{safe}.csv"
                job["content_type"] = "text/csv"
        else:
            # ZIP with splits
            out_path = os.path.join(tmp_dir, f"{export_id}_{safe}.zip")
            has_seg = 'SEG' in table_cols
            has_div = 'DIV' in table_cols

            written = 0
            with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                if has_seg and has_div:
                    seg_query = f"SELECT DISTINCT SEG FROM [{safe}] WITH (NOLOCK){where_sql}"
                    segs = pd.read_sql(seg_query, engine)['SEG'].tolist()
                    for seg_val in segs:
                        _check_export_cancel(export_id)
                        s_seg = re.sub(r'[^A-Za-z0-9_-]', '_', str(seg_val))[:30]
                        seg_where = f"{where} AND " if where else ""
                        if str(seg_val).upper() == 'GM':
                            parts = []
                            for chunk in pd.read_sql(f"SELECT * FROM [{safe}] WITH (NOLOCK) WHERE {seg_where}SEG='{seg_val}'", engine, chunksize=100000):
                                _check_export_cancel(export_id)
                                parts.append(chunk.to_csv(index=False, header=len(parts)==0))
                                written += len(chunk)
                                with _export_lock:
                                    job["processed_rows"] = written
                            zf.writestr(f"{safe}_SEG_{s_seg}.csv", "".join(parts))
                        else:
                            divs = pd.read_sql(f"SELECT DISTINCT DIV FROM [{safe}] WITH (NOLOCK) WHERE {seg_where}SEG='{seg_val}'", engine)['DIV'].tolist()
                            for div_val in divs:
                                _check_export_cancel(export_id)
                                s_div = re.sub(r'[^A-Za-z0-9_-]', '_', str(div_val))[:30]
                                parts = []
                                for chunk in pd.read_sql(f"SELECT * FROM [{safe}] WITH (NOLOCK) WHERE {seg_where}SEG='{seg_val}' AND DIV='{div_val}'", engine, chunksize=100000):
                                    _check_export_cancel(export_id)
                                    parts.append(chunk.to_csv(index=False, header=len(parts)==0))
                                    written += len(chunk)
                                    with _export_lock:
                                        job["processed_rows"] = written
                                zf.writestr(f"{safe}_{s_seg}_{s_div}.csv", "".join(parts))
                elif has_div:
                    div_query = f"SELECT DISTINCT DIV FROM [{safe}] WITH (NOLOCK){where_sql}"
                    divs = pd.read_sql(div_query, engine)['DIV'].tolist()
                    for div_val in divs:
                        _check_export_cancel(export_id)
                        s_div = re.sub(r'[^A-Za-z0-9_-]', '_', str(div_val))[:30]
                        div_where = f"{where} AND " if where else ""
                        parts = []
                        for chunk in pd.read_sql(f"SELECT * FROM [{safe}] WITH (NOLOCK) WHERE {div_where}DIV='{div_val}'", engine, chunksize=100000):
                            _check_export_cancel(export_id)
                            parts.append(chunk.to_csv(index=False, header=len(parts)==0))
                            written += len(chunk)
                            with _export_lock:
                                job["processed_rows"] = written
                        zf.writestr(f"{safe}_{s_div}.csv", "".join(parts))
                else:
                    parts = []
                    for chunk in pd.read_sql(base_query, engine, chunksize=100000):
                        _check_export_cancel(export_id)
                        parts.append(chunk.to_csv(index=False, header=len(parts)==0))
                        written += len(chunk)
                        with _export_lock:
                            job["processed_rows"] = written
                    zf.writestr(f"{safe}.csv", "".join(parts))

            with _export_lock:
                job["file_path"] = out_path
                job["file_name"] = f"{safe}.zip"
                job["content_type"] = "application/zip"

        file_size = os.path.getsize(out_path)
        with _export_lock:
            job["status"] = "completed"
            job["file_size"] = file_size
            job["finished_at"] = datetime.now().isoformat()
            job["duration"] = round(time.time() - job["_start_time"], 2)
        logger.info(f"[Export {export_id}] Completed: {out_path} ({file_size:,} bytes, {job['duration']}s)")

    except ExportCancelled:
        # User clicked Cancel — exit cleanly, remove the partial file, and
        # mark status='cancelled' (distinct from 'failed' so the UI can tell
        # the difference).
        logger.info(f"[Export {export_id}] Cancelled by user")
        try:
            if 'out_path' in locals() and out_path and os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        with _export_lock:
            job["status"] = "cancelled"
            job["finished_at"] = datetime.now().isoformat()
            job["duration"] = round(time.time() - job["_start_time"], 2)
            job["file_path"] = None
    except Exception as e:
        logger.error(f"[Export {export_id}] Failed: {e}")
        with _export_lock:
            job["status"] = "failed"
            job["error"] = str(e)[:500]
            job["finished_at"] = datetime.now().isoformat()


class ExportPayload(BaseModel):
    filters: dict = {}   # { "SEG": ["APP","GM"], "DIV": ["KIDS"] }

@router.post("/review/export/{table_name}", response_model=APIResponse)
def start_export_job(table_name: str, body: ExportPayload = ExportPayload(),
                     current_user: User = Depends(get_current_user)):
    """Start a background export job for a review table with optional filters."""
    if not table_name.upper().startswith(TABLE_PREFIX.upper()):
        raise HTTPException(400, "Invalid table name")
    safe = table_name.replace("'", "").replace(";", "")

    export_id = f"exp_{str(uuid.uuid4())[:8]}"
    job = {
        "id": export_id,
        "table_name": safe,
        "filters": body.filters,
        "status": "pending",
        "total_rows": 0,
        "processed_rows": 0,
        "file_path": None,
        "file_name": None,
        "content_type": None,
        "file_size": None,
        "error": None,
        "created_at": datetime.now().isoformat(),
        "started_at": None,
        "finished_at": None,
        "duration": None,
        "cancel_requested": False,
        "_start_time": time.time(),
    }
    with _export_lock:
        _export_jobs[export_id] = job

    t = threading.Thread(target=_run_export_job, args=(export_id,), daemon=True)
    t.start()

    filter_desc = f" with filters: {body.filters}" if body.filters else ""
    logger.info(f"[Export {export_id}] Created for table {safe}{filter_desc}")
    return APIResponse(success=True, message=f"Export job {export_id} started", data={"export_id": export_id})


@router.get("/review/exports", response_model=APIResponse)
def list_export_jobs(current_user: User = Depends(get_current_user)):
    """List all export jobs (most recent first)."""
    with _export_lock:
        jobs = list(reversed(_export_jobs.values()))
    summaries = []
    for j in jobs:
        summaries.append({
            "id": j["id"], "table_name": j["table_name"], "status": j["status"],
            "total_rows": j.get("total_rows", 0), "processed_rows": j.get("processed_rows", 0),
            "file_size": j.get("file_size"), "error": j.get("error"),
            "created_at": j.get("created_at"), "finished_at": j.get("finished_at"),
            "duration": j.get("duration"),
        })
    return APIResponse(success=True, data={"exports": summaries})


@router.get("/review/exports/{export_id}", response_model=APIResponse)
def get_export_job(export_id: str, current_user: User = Depends(get_current_user)):
    """Get export job status."""
    with _export_lock:
        job = _export_jobs.get(export_id)
    if not job:
        raise HTTPException(404, "Export job not found")
    return APIResponse(success=True, data={"export": {
        "id": job["id"], "table_name": job["table_name"], "status": job["status"],
        "total_rows": job.get("total_rows", 0), "processed_rows": job.get("processed_rows", 0),
        "file_size": job.get("file_size"), "error": job.get("error"),
        "created_at": job.get("created_at"), "finished_at": job.get("finished_at"),
        "duration": job.get("duration"),
    }})


@router.get("/review/exports/{export_id}/download")
def download_export_job(export_id: str, current_user: User = Depends(get_current_user)):
    """Download completed export file."""
    with _export_lock:
        job = _export_jobs.get(export_id)
    if not job:
        raise HTTPException(404, "Export job not found")
    if job["status"] != "completed":
        raise HTTPException(400, f"Export not ready (status: {job['status']})")
    if not job.get("file_path") or not os.path.exists(job["file_path"]):
        raise HTTPException(404, "Export file not found")

    def file_stream():
        with open(job["file_path"], "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)  # 1MB chunks
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(file_stream(), media_type=job.get("content_type", "application/octet-stream"),
        headers={"Content-Disposition": f"attachment; filename={job['file_name']}"})


@router.post("/review/exports/{export_id}/cancel", response_model=APIResponse)
def cancel_export_job(export_id: str, current_user: User = Depends(get_current_user)):
    """Request cancellation of a running export. The worker checks the flag at
    each chunk boundary and exits cleanly. Returns immediately — actual status
    transitions to 'cancelled' on the next checkpoint."""
    with _export_lock:
        job = _export_jobs.get(export_id)
        if not job:
            raise HTTPException(404, "Export job not found")
        if job["status"] not in ("pending", "running"):
            return APIResponse(success=True,
                message=f"Export {export_id} already {job['status']}; nothing to cancel")
        job["cancel_requested"] = True
    logger.info(f"[Export {export_id}] Cancel requested")
    return APIResponse(success=True,
        message="Cancel signal sent; export will stop at the next chunk boundary")


@router.delete("/review/exports/{export_id}", response_model=APIResponse)
def delete_export_job(export_id: str, current_user: User = Depends(get_current_user)):
    """Delete an export job and its file."""
    with _export_lock:
        job = _export_jobs.pop(export_id, None)
    if job and job.get("file_path") and os.path.exists(job["file_path"]):
        try:
            os.remove(job["file_path"])
        except Exception:
            pass
    return APIResponse(success=True, message=f"Export {export_id} deleted")


# ══════════════════════════════════════════════════════════════════════════════
#  CONTRIBUTION REPORT — MAJ_CAT Cockpit (Design A)
#
#  One MAJ_CAT at a time. Shows the full 5-step contribution chain per vendor:
#    L7D | L30D | AUTO CONT% | AUTO CONT% 2 | AUTO CONT% (FINAL) | MERCH_INPUT
#    | BGT CONT% (FINAL)
#
#  Merchant edits MERCH_INPUT inline; live 100% sum check at the bottom of the
#  grid. Edits persist in `Cont_Report_Merch` (separate from source data).
# ══════════════════════════════════════════════════════════════════════════════

COCKPIT_MERCH_TABLE = "Cont_Report_Merch"

# Columns from a Cont_Percentage_* table that are NOT the per-vendor grouping column.
# Used to detect the grouping column at runtime (whatever's left over is the vendor key).
_COCKPIT_IDENTITY = {
    "SEG", "DIV", "SUB_DIV", "MAJ_CAT", "RNG_SEG", "RDC_CD", "RDC_NM",
    "ST_CD", "ST_NM", "SSN", "ACT_INACT", "MAJ-CAT STS", "RNG_SEG STS",
    "APF", "AVG_DNSTY", "MERCH_INPUT", "LISTING", "Generated_Date",
    "AUTO CONT%", "AUTO SEG CONT%", "Auto cont%",
    "AUTO CONT% 2", "AUTO CONT% (FINAL)", "BGT CONT% (FINAL)",
}


def _ensure_cockpit_merch_table(engine):
    with engine.connect() as c:
        _run(c, f"""
            IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='{COCKPIT_MERCH_TABLE}')
            CREATE TABLE {COCKPIT_MERCH_TABLE} (
                id            INT IDENTITY(1,1) PRIMARY KEY,
                table_name    NVARCHAR(255) NOT NULL,
                maj_cat       NVARCHAR(150) NOT NULL,
                vendor_cd     NVARCHAR(100) NOT NULL,
                merch_input   FLOAT         NULL,
                modified_by   NVARCHAR(255) NULL,
                modified_at   DATETIME      DEFAULT GETDATE(),
                CONSTRAINT UQ_{COCKPIT_MERCH_TABLE} UNIQUE (table_name, maj_cat, vendor_cd)
            )
        """)


def _detect_vendor_col(columns):
    """Return the grouping column used in this result table — anything that isn't a
    known identity column, an INITIAL/STOCK/SALE/ALGO KPI column, or a derived one."""
    # Exclude the identity set, any pipe-suffixed period columns, and known masters.
    for c in columns:
        if c in _COCKPIT_IDENTITY:
            continue
        if "|" in c:
            continue
        if c.startswith(("OP_STK_", "CL_STK_", "0001_STK_", "STR", "FIX",
                          "DISP_AREA", "GM_", "SALES_PSF", "SALE_PSF", "STOCK_CONT",
                          "SALE_CONT", "ALGO", "INITIAL AUTO")):
            continue
        # First survivor is our grouping column (M_VND_CD, MACRO_MVGR, etc.)
        return c
    return None


def _vendor_label_col(vendor_cd_col):
    """For M_VND_CD → M_VND_NM, for MACRO_MVGR → none, etc."""
    if vendor_cd_col == "M_VND_CD":
        return "M_VND_NM"
    return None


def _fetch_cockpit_merch(engine, table_name):
    """Pre-load merchant overrides for a table → dict[(maj_cat, vendor_cd)] -> merch_input."""
    _ensure_cockpit_merch_table(engine)
    df = _read_sql_nolock(
        f"SELECT maj_cat, vendor_cd, merch_input FROM {COCKPIT_MERCH_TABLE} WITH (NOLOCK) "
        f"WHERE table_name = '{table_name.replace(chr(39), chr(39)*2)}'", engine)
    out = {}
    for _, r in df.iterrows():
        if pd.notna(r["merch_input"]):
            out[(str(r["maj_cat"]), str(r["vendor_cd"]))] = float(r["merch_input"])
    return out


@router.get("/report/tables", response_model=APIResponse)
def report_list_tables(current_user: User = Depends(get_current_user)):
    """List `Cont_Percentage_*` tables with their detected grouping column and row count."""
    engine = get_data_engine()
    try:
        insp = inspect(engine)
        names = sorted([t for t in insp.get_table_names() if t.upper().startswith(TABLE_PREFIX.upper())])
    except Exception:
        names = []
    out = []
    with engine.connect() as c:
        for t in names:
            try:
                cols = [r[0] for r in c.execute(text(
                    f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='{t}'"
                )).fetchall()]
                rc = c.execute(text(f"SELECT COUNT(*) FROM [{t}] WITH (NOLOCK)")).scalar() or 0
                out.append({
                    "table_name": t,
                    "level": "store" if "ST_CD" in cols else "company",
                    "vendor_col": _detect_vendor_col(cols),
                    "rows": int(rc),
                })
            except Exception:
                pass
    return APIResponse(success=True, data={"tables": out})


@router.get("/report/majcats", response_model=APIResponse)
def report_list_majcats(table: str = Query(...), current_user: User = Depends(get_current_user)):
    """List MAJ_CATs in a result table with vendor counts and AUTO CONT% (FINAL) sums."""
    if not table.upper().startswith(TABLE_PREFIX.upper()):
        raise HTTPException(400, "Invalid table name")
    engine = get_data_engine()
    safe = table.replace("'", "").replace(";", "")

    with engine.connect() as c:
        cols = [r[0] for r in c.execute(text(
            f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='{safe}'"
        )).fetchall()]
    if "MAJ_CAT" not in cols:
        raise HTTPException(400, "Table has no MAJ_CAT column")

    final_col = "AUTO CONT% (FINAL)" if "AUTO CONT% (FINAL)" in cols else None
    select_extra = f", SUM(COALESCE([{final_col}], 0)) AS auto_sum" if final_col else ", NULL AS auto_sum"
    df = _read_sql_nolock(
        f"SELECT MAJ_CAT, COUNT(*) AS vendor_count {select_extra} "
        f"FROM [{safe}] WITH (NOLOCK) GROUP BY MAJ_CAT ORDER BY MAJ_CAT",
        engine,
    )
    items = []
    for _, r in df.iterrows():
        items.append({
            "maj_cat": r["MAJ_CAT"],
            "vendor_count": int(r["vendor_count"]),
            "auto_sum": round(float(r["auto_sum"]), 4) if pd.notna(r["auto_sum"]) else None,
        })
    return APIResponse(success=True, data={"maj_cats": items, "has_final": bool(final_col)})


@router.get("/report/cockpit", response_model=APIResponse)
def report_cockpit(table: str = Query(...), maj_cat: str = Query(...),
                   current_user: User = Depends(get_current_user)):
    """Return the vendor rows for one (table, maj_cat) with the full contribution chain
    plus the merchant's persisted override (if any).

    Response shape:
      {
        "vendor_col": "M_VND_CD",
        "vendor_label_col": "M_VND_NM" | null,
        "periods": ["L7D","L30D",...],
        "rows": [{...}],
        "totals": {"auto_final": float, "merch": float, "bgt_final": float}
      }
    """
    if not table.upper().startswith(TABLE_PREFIX.upper()):
        raise HTTPException(400, "Invalid table name")
    engine = get_data_engine()
    safe = table.replace("'", "").replace(";", "")

    with engine.connect() as c:
        all_cols = [r[0] for r in c.execute(text(
            f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='{safe}'"
        )).fetchall()]
    if not all_cols:
        raise HTTPException(404, "Table not found")
    if "MAJ_CAT" not in all_cols:
        raise HTTPException(400, "Table has no MAJ_CAT column")

    vendor_cd = _detect_vendor_col(all_cols)
    if not vendor_cd:
        raise HTTPException(400, "Could not detect a vendor grouping column in this table")
    vendor_nm = _vendor_label_col(vendor_cd) if _vendor_label_col(vendor_cd) in all_cols else None

    # Find the per-period INITIAL AUTO CONT% columns
    periods = []
    for c in all_cols:
        if c.startswith("INITIAL AUTO CONT%|"):
            p = c.split("|", 1)[1]
            periods.append(p)
    # Cap to first 2 periods for the cockpit display (L7D + L30D in usual sequence)
    display_periods = periods[:2]

    select_cols = []
    for c in ("SEG", "DIV", "SUB_DIV", "MAJ_CAT", "RDC_CD", "SSN", "ACT_INACT",
              vendor_cd, vendor_nm, "MERCH_INPUT",
              "AUTO CONT%", "Auto cont%", "AUTO SEG CONT%",
              "AUTO CONT% 2", "AUTO CONT% (FINAL)", "BGT CONT% (FINAL)"):
        if c and c in all_cols and c not in select_cols:
            select_cols.append(c)
    for p in display_periods:
        col = f"INITIAL AUTO CONT%|{p}"
        if col in all_cols:
            select_cols.append(col)

    bracketed = ", ".join(f"[{c}]" for c in select_cols)
    safe_mc = maj_cat.replace("'", "''")
    df = _read_sql_nolock(
        f"SELECT {bracketed} FROM [{safe}] WITH (NOLOCK) WHERE MAJ_CAT = '{safe_mc}'", engine)

    if df.empty:
        return APIResponse(success=True, data={
            "vendor_col": vendor_cd, "vendor_label_col": vendor_nm,
            "periods": display_periods, "rows": [], "totals": {"auto_final": 0, "merch": 0, "bgt_final": 0}
        })

    merch_overrides = _fetch_cockpit_merch(engine, safe)

    # Normalise mapped column to a single 'auto_cont_mapped' field
    mapped_col = next((c for c in ("AUTO CONT%", "Auto cont%", "AUTO SEG CONT%") if c in df.columns), None)

    rows_out = []
    for _, r in df.iterrows():
        vcd = str(r.get(vendor_cd, ""))
        merch_override = merch_overrides.get((str(maj_cat), vcd))
        row = {
            "vendor_cd":  r.get(vendor_cd),
            "vendor_nm":  r.get(vendor_nm) if vendor_nm else None,
            "SEG":        r.get("SEG"),
            "DIV":        r.get("DIV"),
            "SUB_DIV":    r.get("SUB_DIV"),
            "SSN":        r.get("SSN"),
            "ACT_INACT":  r.get("ACT_INACT"),
            "MERCH_INPUT":      _num_or_none(r.get("MERCH_INPUT")),
            "MERCH_INPUT_OVR":  merch_override,            # persisted override (if any)
            "auto_cont_mapped": _num_or_none(r.get(mapped_col)) if mapped_col else None,
            "AUTO CONT% 2":     _num_or_none(r.get("AUTO CONT% 2")),
            "AUTO CONT% (FINAL)": _num_or_none(r.get("AUTO CONT% (FINAL)")),
            "BGT CONT% (FINAL)":  _num_or_none(r.get("BGT CONT% (FINAL)")),
        }
        for p in display_periods:
            row[f"INITIAL_AUTO_{p}"] = _num_or_none(r.get(f"INITIAL AUTO CONT%|{p}"))
        rows_out.append(row)

    totals = {
        "auto_final": round(sum((r["AUTO CONT% (FINAL)"] or 0) for r in rows_out), 4),
        "merch":      round(sum(((r["MERCH_INPUT_OVR"] if r["MERCH_INPUT_OVR"] is not None else r["MERCH_INPUT"]) or 0) for r in rows_out), 4),
        "bgt_final":  round(sum((r["BGT CONT% (FINAL)"] or 0) for r in rows_out), 4),
    }
    return APIResponse(success=True, data={
        "vendor_col": vendor_cd,
        "vendor_label_col": vendor_nm,
        "periods": display_periods,
        "rows": rows_out,
        "totals": totals,
    })


def _num_or_none(v):
    try:
        if v is None or pd.isna(v):
            return None
        return float(v)
    except Exception:
        return None


class CockpitMerchItem(BaseModel):
    vendor_cd: str
    merch_input: Optional[float] = None  # null clears the override


class CockpitMerchBulk(BaseModel):
    table_name: str
    maj_cat: str
    items: List[CockpitMerchItem]


@router.post("/report/cockpit/save", response_model=APIResponse)
def report_cockpit_save(payload: CockpitMerchBulk, current_user: User = Depends(get_current_user)):
    """Bulk upsert merchant inputs for a (table, MAJ_CAT) — one row per vendor."""
    if not payload.table_name.upper().startswith(TABLE_PREFIX.upper()):
        raise HTTPException(400, "Invalid table name")
    if not payload.items:
        return APIResponse(success=True, message="No items", data={"saved": 0})

    engine = get_data_engine()
    _ensure_cockpit_merch_table(engine)
    user = getattr(current_user, "username", None) or getattr(current_user, "user_name", None) or "unknown"

    with engine.connect() as c:
        saved = 0
        for it in payload.items:
            params = {
                "tn": payload.table_name, "mc": payload.maj_cat,
                "vc": it.vendor_cd, "v": it.merch_input, "u": user,
            }
            _run(c, f"""
                MERGE {COCKPIT_MERCH_TABLE} AS tgt
                USING (SELECT :tn AS table_name, :mc AS maj_cat, :vc AS vendor_cd) AS src
                ON  tgt.table_name = src.table_name
                AND tgt.maj_cat    = src.maj_cat
                AND tgt.vendor_cd  = src.vendor_cd
                WHEN MATCHED THEN
                    UPDATE SET merch_input = :v, modified_by = :u, modified_at = GETDATE()
                WHEN NOT MATCHED THEN
                    INSERT (table_name, maj_cat, vendor_cd, merch_input, modified_by, modified_at)
                    VALUES (:tn, :mc, :vc, :v, :u, GETDATE());
            """, params)
            saved += 1
    return APIResponse(success=True, message=f"Saved {saved} merchant input(s)",
                       data={"saved": saved})


# ══════════════════════════════════════════════════════════════════════════════
#  CONTRIBUTION REPORT — Full-table paginated view (replaces MAJ_CAT cockpit)
#
#  Shows the whole Cont_Percentage_* table as-is, paginated 500 rows at a time,
#  with curated columns (only the contribution-relevant ones). A "show_all"
#  toggle reveals every column for power users / debugging.
# ══════════════════════════════════════════════════════════════════════════════

def _select_curated_columns(all_cols, is_store, vendor_col, vendor_name_col):
    """Return the curated default column list (skip per-period KPIs and vendor code)."""
    out = []

    # Identity columns
    for c in ['ST_CD', 'ST_NM', 'STATUS', 'SEG', 'DIV', 'SUB_DIV', 'MAJ_CAT', 'RNG_SEG', 'RDC_CD']:
        if c in all_cols and c not in out:
            out.append(c)

    # Vendor name only — drop the code. For non-M_VND_CD groupings show the value.
    if vendor_name_col and vendor_name_col in all_cols:
        out.append(vendor_name_col)
    elif vendor_col and vendor_col != 'M_VND_CD' and vendor_col in all_cols:
        out.append(vendor_col)

    # Stable per-row attributes
    for c in ['SSN', 'ACT_INACT', 'APF', 'AVG_DNSTY', 'LISTING', 'MERCH_INPUT']:
        if c in all_cols and c not in out:
            out.append(c)

    # Reference period inputs (just two — L30D + SSN_TLM if available)
    if 'INITIAL AUTO CONT%|L30D' in all_cols:
        out.append('INITIAL AUTO CONT%|L30D')
    for c in all_cols:
        if c.startswith('INITIAL AUTO CONT%|') and 'SSN' in c.upper():
            out.append(c)
            break

    # Contribution chain — different per level
    if is_store:
        chain = ['NAT CONT%', 'NAT CONT% @ MAJ', 'BGT CONT%',
                 'AUTO CONT%-1', 'RMN AUTO', 'BGT CONT%@MAJ_CAT', 'RMN AUTO @ MAJCAT',
                 'ALGO', 'AUTO CONT%-2', 'OLD ST CONT%',
                 'INT ST CONT%', 'INT-2 ST CONT%', 'FINAL ST CONT%']
    else:
        chain = ['Auto cont%', 'AUTO CONT%', 'AUTO SEG CONT%',
                 'AUTO CONT% 2', 'AUTO CONT% (FINAL)', 'BGT CONT% (FINAL)']
    for c in chain:
        if c in all_cols and c not in out:
            out.append(c)

    if 'Generated_Date' in all_cols:
        out.append('Generated_Date')

    return out


@router.get("/report/page", response_model=APIResponse)
def report_page(table: str = Query(...),
                page: int = Query(1, ge=1),
                page_size: int = Query(500, ge=1, le=5000),
                majcat: Optional[str] = Query(None),
                seg: Optional[str] = Query(None),
                status: Optional[str] = Query(None),
                q: Optional[str] = Query(None),
                col_filters: Optional[str] = Query(None, description="JSON object {col: [v1,v2,...]} for generic in-header filters"),
                cols: Optional[str] = Query(None, description="Comma-separated explicit column list. When provided, overrides show_all/curated logic."),
                show_all: bool = Query(False),
                current_user: User = Depends(get_current_user)):
    """Paginated report view for a Cont_Percentage_* table.

    Defaults to a curated column set (essential contribution columns + identity).
    Pass `show_all=true` to reveal every column. Supports filtering by MAJ_CAT,
    SEG, STATUS, and a free-text vendor-name search.
    """
    if not table.upper().startswith(TABLE_PREFIX.upper()):
        raise HTTPException(400, "Invalid table name")
    engine = get_data_engine()
    safe = table.replace("'", "").replace(";", "")

    with engine.connect() as c:
        all_cols = [r[0] for r in c.execute(text(
            f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='{safe}'"
        )).fetchall()]
    if not all_cols:
        raise HTTPException(404, "Table not found")

    is_store = 'ST_CD' in all_cols
    vendor_col = _detect_vendor_col(all_cols)
    vendor_name_col = _vendor_label_col(vendor_col) if _vendor_label_col(vendor_col) in all_cols else None

    # Column selection priority:
    #   1. Explicit `cols` list (from the column picker on the frontend)
    #   2. `show_all=true` → every column in the table
    #   3. Curated default from _select_curated_columns
    if cols:
        # Preserve user's order; filter to columns that actually exist.
        wanted = [c.strip() for c in cols.split(",") if c.strip()]
        all_cols_lower = {c.lower(): c for c in all_cols}
        selected = []
        for c in wanted:
            actual = all_cols_lower.get(c.lower())
            if actual and actual not in selected:
                selected.append(actual)
        # If user accidentally sent an entirely-invalid list, fall back to curated
        if not selected:
            selected = _select_curated_columns(all_cols, is_store, vendor_col, vendor_name_col)
    else:
        selected = all_cols if show_all else _select_curated_columns(all_cols, is_store, vendor_col, vendor_name_col)
    selected = [c for c in selected if c in all_cols]  # safety filter
    # Dedupe while preserving order. Some curated-column branches don't check
    # membership before appending (vendor name/code, period KPIs), so the same
    # column could otherwise appear twice — which would propagate into the SQL
    # SELECT and break df.to_json(orient='records') with "columns must be unique".
    selected = list(dict.fromkeys(selected))

    # Build WHERE — majcat/seg/status accept comma-separated lists so the
    # in-table filter dropdowns (multi-select) translate to `IN (...)`.
    def _in_clause(raw: str, col: str) -> Optional[str]:
        if not raw:
            return None
        vals = [v.strip() for v in raw.split(",") if v.strip()]
        if not vals:
            return None
        quoted = ",".join("'" + v.replace("'", "''") + "'" for v in vals)
        return f"[{col}] IN ({quoted})"

    where_parts = []
    if majcat and 'MAJ_CAT' in all_cols:
        c = _in_clause(majcat, 'MAJ_CAT')
        if c: where_parts.append(c)
    if seg and 'SEG' in all_cols:
        c = _in_clause(seg, 'SEG')
        if c: where_parts.append(c)
    if status and 'STATUS' in all_cols:
        c = _in_clause(status, 'STATUS')
        if c: where_parts.append(c)
    if q and vendor_name_col:
        safe_v = q.replace("'", "''").replace("%", "")
        where_parts.append(f"[{vendor_name_col}] LIKE '%{safe_v}%'")
    # Generic per-column filters (in-header multi-select). Accepts a JSON map
    # {col: [v1, v2, ...]}. Each entry becomes an IN-clause if the col exists
    # in the table. Silently ignored if JSON is malformed.
    if col_filters:
        try:
            cf = json.loads(col_filters)
            if isinstance(cf, dict):
                for col, vals in cf.items():
                    if not isinstance(vals, list) or not vals:
                        continue
                    if col not in all_cols:
                        continue
                    clause = _in_clause(",".join(str(v) for v in vals), col)
                    if clause:
                        where_parts.append(clause)
        except (json.JSONDecodeError, ValueError):
            pass
    where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""

    with engine.connect() as c:
        total = c.execute(text(f"SELECT COUNT(*) FROM [{safe}] WITH (NOLOCK){where_sql}")).scalar() or 0

    # Distinct filter options (for in-header multi-select) — computed on first
    # page request only. We cover low-cardinality identity-style columns plus
    # the table's grouping column (vendor_col) so the user can filter on
    # MAJ_CAT / SEG / STATUS / SSN / ACT_INACT / DIV / SUB_DIV / RNG_SEG /
    # RDC_CD / <grouping column> when they exist in the table.
    filter_options = {}
    if page == 1:
        filterable_cols = ['SEG', 'MAJ_CAT', 'STATUS', 'SSN', 'ACT_INACT',
                           'DIV', 'SUB_DIV', 'RNG_SEG', 'RDC_CD']
        if vendor_col and vendor_col not in filterable_cols:
            filterable_cols.append(vendor_col)
        with engine.connect() as c:
            for fc in filterable_cols:
                if fc not in all_cols:
                    continue
                try:
                    vals = [r[0] for r in c.execute(text(
                        f"SELECT DISTINCT [{fc}] FROM [{safe}] WITH (NOLOCK) WHERE [{fc}] IS NOT NULL ORDER BY [{fc}]"
                    )).fetchall()]
                    # Skip very-high-cardinality columns — multi-select on 1000+
                    # values is unusable; keep them out of the response so the
                    # frontend won't render a filter icon there.
                    if len(vals) > 500:
                        continue
                    filter_options[fc] = [str(v) for v in vals if v is not None]
                except Exception:
                    pass

    offset = (page - 1) * page_size
    order_col = vendor_name_col or 'MAJ_CAT' if 'MAJ_CAT' in all_cols else selected[0]
    bracketed = ", ".join(f"[{c}]" for c in selected)
    rows_sql = (
        f"SELECT {bracketed} FROM [{safe}] WITH (NOLOCK){where_sql} "
        f"ORDER BY [{order_col}] "
        f"OFFSET {offset} ROWS FETCH NEXT {page_size} ROWS ONLY"
    )
    df = _read_sql_nolock(rows_sql, engine)
    for col in df.select_dtypes(include=['float64', 'float32', 'float']).columns:
        df[col] = df[col].round(4)

    return APIResponse(success=True, data={
        "columns": selected,
        "rows": json.loads(df.to_json(orient='records', date_format='iso')),
        "total": int(total),
        "page": page,
        "page_size": page_size,
        "is_store": is_store,
        "vendor_col": vendor_col,
        "vendor_name_col": vendor_name_col,
        "all_columns": all_cols,
        "filter_options": filter_options,
    })
