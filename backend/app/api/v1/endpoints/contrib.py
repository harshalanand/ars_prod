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
VALID_GROUPING     = ('CLR','SZ','RNG_SEG','M_VND_CD','MACRO_MVGR','MICRO_MVGR','FAB')


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
                AND COLUMN_NAME IN ('CLR','SZ','RNG_SEG','M_VND_CD','MACRO_MVGR','MICRO_MVGR','FAB')
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
        rows = c.execute(text(f"SELECT id, col_name, mapping_name, prefix, target FROM {ASSIGNMENT_TABLE} ORDER BY id")).fetchall()
    items = [{"id": r[0], "col_name": r[1], "mapping_name": r[2], "prefix": r[3], "target": r[4]} for r in rows]
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
    df['GM_PSF_ACH%'] = np.where(df['GM_PSF_MJ']==0, 0, df['GM PSF']/df['GM_PSF_MJ'])

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
    df['ALGO'] = np.minimum(algo_raw, np.maximum(algo_adj, 0))
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


def _process_single_preset(engine, preset_name, preset_cfg, majcats, grouping_column,
                           avg_density, apf, df_master_cache=None):
    """Process one preset: query → merge → KPI → return (detail_df, agg_df, timing, df_master).
    df_master_cache: reuse master query result across presets (big optimization)."""
    timing = {}
    where_parts = []
    if majcats:
        safe = "','".join(m.replace("'","''") for m in majcats)
        where_parts.append(f"MAJ_CAT IN ('{safe}')")
    where_clause = " AND ".join(where_parts) if where_parts else "1=1"

    months = preset_cfg.get("months", [])
    kpi_type = preset_cfg.get("kpi_type", "L30D")
    avg_days = preset_cfg.get("avg_days", 30)

    if kpi_type == "L7D" or preset_name == "L7D":
        date_filter = "sal_stk.KPI = 'L7D'"
    elif kpi_type == "L30D" or preset_name == "L30D":
        date_filter = "sal_stk.KPI = 'L30D'"
    else:
        ms = "','".join(months)
        date_filter = f"sal_stk.STOCK_DATE IN ('{ms}') AND sal_stk.KPI = 'L18M'"

    grouping_expr, grouping_dtype = _get_grouping_expr(engine, grouping_column)

    # Step 1: Data query (stock + product join)
    t = time.time()
    data_query = f"""
        SELECT ST_CD, MAJ_CAT, {grouping_column},
               ROUND(AVG(OP_STK_Q), 2) AS OP_STK_Q, ROUND(AVG(OP_STK_V), 2) AS OP_STK_V,
               ROUND(AVG(CL_STK_Q), 2) AS CL_STK_Q, ROUND(AVG(CL_STK_V), 2) AS CL_STK_V,
               ROUND(AVG(SALE_Q), 2) AS SALE_Q, ROUND(AVG(SALE_V), 2) AS SALE_V, ROUND(AVG(GM_V), 2) AS GM_V
        FROM (
            SELECT sal_stk.STOCK_DATE, sal_stk.WERKS AS ST_CD,
                   prod.MAJ_CAT, prod.{grouping_column},
                   COALESCE(SUM(sal_stk.OP_STK_QTY)/1000,0) AS OP_STK_Q,
                   COALESCE(SUM(sal_stk.OP_STK_VAL)/100000,0) AS OP_STK_V,
                   COALESCE(SUM(sal_stk.CL_STK_QTY)/1000,0) AS CL_STK_Q,
                   COALESCE(SUM(sal_stk.CL_STK_VAL)/100000,0) AS CL_STK_V,
                   COALESCE(SUM(sal_stk.SALE_QTY)/1000,0) AS SALE_Q,
                   COALESCE(SUM(sal_stk.SALE_VAL)/100000,0) AS SALE_V,
                   COALESCE(SUM(sal_stk.GM_VAL)/100000,0) AS GM_V
            FROM dbo.COUNT_STOCK_DATA_18M sal_stk WITH (NOLOCK)
            LEFT JOIN (
                SELECT ARTICLE_NUMBER AS MATNR, MAJ_CAT,
                       {grouping_expr} AS {grouping_column}, SEG
                FROM dbo.VW_MASTER_PRODUCT WITH (NOLOCK)
            ) prod ON sal_stk.MATNR = prod.MATNR
            WHERE {where_clause} AND prod.SEG IN ('APP','GM') AND {date_filter}
            GROUP BY sal_stk.WERKS, sal_stk.STOCK_DATE, prod.MAJ_CAT, prod.{grouping_column}
        ) t
        GROUP BY ST_CD, MAJ_CAT, {grouping_column}
    """
    df_data = _read_sql_nolock(data_query, engine)
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


def _apply_mapping_assignments(df, engine):
    """Apply mapping assignments to compute final columns."""
    _ensure_assignment_table(engine)
    _ensure_mapping_table(engine)
    with engine.connect() as c:
        assignments = [dict(r._mapping) for r in c.execute(text(f"SELECT col_name, mapping_name, prefix, target FROM {ASSIGNMENT_TABLE}"))]
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
        df[col_name] = np.where(has_map, result, fb_max)

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

                # Drop + Create
                cursor.execute(f"IF OBJECT_ID('{table_name}','U') IS NOT NULL DROP TABLE [{table_name}]")
                cursor.execute(f"CREATE TABLE [{table_name}] ({', '.join(col_defs)})")
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
                    engine, pname, all_presets[pname], majcats, gc_col, avg_density, apf, df_master_cache)
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
            month_tag = datetime.now().strftime('%Y_%m')
            safe_gc = gc_col.upper().replace(' ','_').replace('-','_')

            def _make_progress_cb(label):
                def cb(inserted, total):
                    _update_job(job_id, progress=f"saving {label}: {inserted:,}/{total:,} rows")
                return cb

            try:
                if not df_store.empty:
                    t = time.time()
                    tbl = f"{TABLE_PREFIX}_{safe_gc}_{month_tag}"
                    logger.info(f"[Job {job_id}] Saving store to DB: {tbl} ({len(df_store):,} rows)")
                    _save_to_db(engine, df_store, tbl, retries=3, progress_cb=_make_progress_cb("store"))
                    dur_s = round(time.time()-t, 2)
                    logger.info(f"[Job {job_id}] Store saved in {dur_s}s")
                    log.append({"action": "saved_store", "table": tbl, "rows": len(df_store),
                                 "duration": dur_s})
                if not df_company.empty:
                    t = time.time()
                    tbl = f"{TABLE_PREFIX}_{safe_gc}_CO_{month_tag}"
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


JOB_AUTO_DELETE_DELAY = 60  # seconds after completion before auto-delete

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
                        s_seg = re.sub(r'[^A-Za-z0-9_-]', '_', str(seg_val))[:30]
                        seg_where = f"{where} AND " if where else ""
                        if str(seg_val).upper() == 'GM':
                            parts = []
                            for chunk in pd.read_sql(f"SELECT * FROM [{safe}] WITH (NOLOCK) WHERE {seg_where}SEG='{seg_val}'", engine, chunksize=100000):
                                parts.append(chunk.to_csv(index=False, header=len(parts)==0))
                                written += len(chunk)
                                with _export_lock:
                                    job["processed_rows"] = written
                            zf.writestr(f"{safe}_SEG_{s_seg}.csv", "".join(parts))
                        else:
                            divs = pd.read_sql(f"SELECT DISTINCT DIV FROM [{safe}] WITH (NOLOCK) WHERE {seg_where}SEG='{seg_val}'", engine)['DIV'].tolist()
                            for div_val in divs:
                                s_div = re.sub(r'[^A-Za-z0-9_-]', '_', str(div_val))[:30]
                                parts = []
                                for chunk in pd.read_sql(f"SELECT * FROM [{safe}] WITH (NOLOCK) WHERE {seg_where}SEG='{seg_val}' AND DIV='{div_val}'", engine, chunksize=100000):
                                    parts.append(chunk.to_csv(index=False, header=len(parts)==0))
                                    written += len(chunk)
                                    with _export_lock:
                                        job["processed_rows"] = written
                                zf.writestr(f"{safe}_{s_seg}_{s_div}.csv", "".join(parts))
                elif has_div:
                    div_query = f"SELECT DISTINCT DIV FROM [{safe}] WITH (NOLOCK){where_sql}"
                    divs = pd.read_sql(div_query, engine)['DIV'].tolist()
                    for div_val in divs:
                        s_div = re.sub(r'[^A-Za-z0-9_-]', '_', str(div_val))[:30]
                        div_where = f"{where} AND " if where else ""
                        parts = []
                        for chunk in pd.read_sql(f"SELECT * FROM [{safe}] WITH (NOLOCK) WHERE {div_where}DIV='{div_val}'", engine, chunksize=100000):
                            parts.append(chunk.to_csv(index=False, header=len(parts)==0))
                            written += len(chunk)
                            with _export_lock:
                                job["processed_rows"] = written
                        zf.writestr(f"{safe}_{s_div}.csv", "".join(parts))
                else:
                    parts = []
                    for chunk in pd.read_sql(base_query, engine, chunksize=100000):
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
