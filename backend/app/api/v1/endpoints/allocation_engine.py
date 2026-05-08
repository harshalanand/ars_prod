"""
Allocation Engine API Endpoints
POST /allocation-engine/run          — trigger allocation run
GET  /allocation-engine/status/{id}  — check run status
GET  /allocation-engine/runs         — list recent runs
GET  /allocation-engine/config       — get score weights & settings
PUT  /allocation-engine/config       — update score weights & settings
GET  /allocation-engine/results/{id} — get run results
GET  /allocation-engine/results/{id}/scores     — article scores
GET  /allocation-engine/results/{id}/assignments — option assignments
GET  /allocation-engine/results/{id}/variants    — variant allocations
"""
import logging
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.session import get_data_db, get_db
from app.security.dependencies import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/allocation-engine", tags=["Allocation Engine"])


# ── Request/Response Models ──

class RunRequest(BaseModel):
    majcats: Optional[List[str]] = None  # null = all
    rdc_code: str = "DH24"
    current_month: int = 4

class ScoreWeightUpdate(BaseModel):
    attribute_name: str
    score_weight: int

class SettingUpdate(BaseModel):
    setting_key: str
    setting_value: str


# ── Background runner ──

_active_runs = {}

def _run_allocation(run_request: RunRequest, username: str, system_db_url: str, data_db_url: str):
    """Background task for allocation run."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.services.allocation.engine import AllocationEngine

    sys_engine = create_engine(system_db_url, pool_pre_ping=True)
    data_engine = create_engine(data_db_url, pool_pre_ping=True)
    SysSession = sessionmaker(bind=sys_engine)
    DataSession = sessionmaker(bind=data_engine)

    sys_db = SysSession()
    data_db = DataSession()

    try:
        engine = AllocationEngine(sys_db, data_db)
        result = engine.run(
            majcats=run_request.majcats,
            rdc_code=run_request.rdc_code,
            created_by=username,
            current_month=run_request.current_month,
        )
        run_id = result.get('run_id', '')
        _active_runs[run_id] = result
        return result
    finally:
        sys_db.close()
        data_db.close()
        sys_engine.dispose()
        data_engine.dispose()


# ── Endpoints ──

@router.post("/run")
async def run_allocation(
    request: RunRequest,
    background_tasks: BackgroundTasks,
    data_db: Session = Depends(get_data_db),
    system_db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Trigger an allocation run. Processes in background."""
    from app.services.allocation.engine import AllocationEngine

    # For single MAJCAT, run synchronously (fast enough)
    if request.majcats and len(request.majcats) == 1:
        engine = AllocationEngine(system_db, data_db)
        result = engine.run(
            majcats=request.majcats,
            rdc_code=request.rdc_code,
            created_by=current_user.username,
            current_month=request.current_month,
        )
        return {"success": True, "data": result}

    # For multiple MAJCATs, run in background
    from app.database.session import get_system_db_url, get_data_db_url
    background_tasks.add_task(
        _run_allocation, request, current_user.username,
        get_system_db_url(), get_data_db_url()
    )
    return {
        "success": True,
        "message": f"Allocation run started for {len(request.majcats) if request.majcats else 'all'} MAJCATs",
        "data": {"status": "started"}
    }


@router.get("/runs")
async def list_runs(
    limit: int = 20,
    data_db: Session = Depends(get_data_db),
    current_user = Depends(get_current_user),
):
    """List recent allocation runs."""
    rows = data_db.execute(text("""
        SELECT TOP(:lim) run_id, run_date, status, rdc_code,
               total_majcats, processed_majcats,
               total_articles_scored, total_slots_filled,
               total_variants_allocated, duration_ms,
               created_by, created_at, completed_at
        FROM alloc_runs ORDER BY created_at DESC
    """), {'lim': limit}).fetchall()

    return {"success": True, "data": [dict(r._mapping) for r in rows]}


@router.get("/status/{run_id}")
async def get_run_status(
    run_id: str,
    data_db: Session = Depends(get_data_db),
    current_user = Depends(get_current_user),
):
    """Get status of an allocation run."""
    row = data_db.execute(text(
        "SELECT * FROM alloc_runs WHERE run_id = :rid"
    ), {'rid': run_id}).fetchone()

    if not row:
        # Check in-memory active runs
        if run_id in _active_runs:
            return {"success": True, "data": _active_runs[run_id]}
        raise HTTPException(404, "Run not found")

    return {"success": True, "data": dict(row._mapping)}


@router.get("/config")
async def get_config(
    data_db: Session = Depends(get_data_db),
    system_db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Get current score weights and engine settings."""
    # Ensure tables exist by instantiating engine (auto-creates if needed)
    from app.services.allocation.engine import AllocationEngine
    try:
        AllocationEngine(system_db, data_db)
    except Exception:
        pass

    weights = data_db.execute(text(
        "SELECT * FROM alloc_score_config WHERE config_name = 'default' ORDER BY score_weight DESC"
    )).fetchall()

    settings = data_db.execute(text(
        "SELECT * FROM alloc_engine_settings ORDER BY setting_key"
    )).fetchall()

    return {
        "success": True,
        "data": {
            "score_weights": [dict(r._mapping) for r in weights],
            "settings": [dict(r._mapping) for r in settings],
        }
    }


@router.put("/config/weights")
async def update_weight(
    update: ScoreWeightUpdate,
    data_db: Session = Depends(get_data_db),
    current_user = Depends(get_current_user),
):
    """Update a score weight."""
    data_db.execute(text("""
        UPDATE alloc_score_config SET score_weight = :w, updated_at = SYSUTCDATETIME(),
        updated_by = :by WHERE config_name = 'default' AND attribute_name = :attr
    """), {'w': update.score_weight, 'attr': update.attribute_name, 'by': current_user.username})
    data_db.commit()
    return {"success": True, "message": f"Updated {update.attribute_name} to {update.score_weight}"}


@router.put("/config/settings")
async def update_setting(
    update: SettingUpdate,
    data_db: Session = Depends(get_data_db),
    current_user = Depends(get_current_user),
):
    """Update or create an engine setting."""
    data_db.execute(text("""
        MERGE alloc_engine_settings AS t
        USING (SELECT :k AS setting_key) AS s ON t.setting_key = s.setting_key
        WHEN MATCHED THEN UPDATE SET setting_value = :v, updated_at = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN INSERT (setting_key, setting_value) VALUES (:k, :v);
    """), {'v': update.setting_value, 'k': update.setting_key})
    data_db.commit()
    return {"success": True, "message": f"Updated {update.setting_key}"}


@router.get("/results/{run_id}/summary")
async def get_run_summary(
    run_id: str,
    data_db: Session = Depends(get_data_db),
    current_user = Depends(get_current_user),
):
    """Get summary of an allocation run."""
    # Option assignments summary
    assignments = data_db.execute(text("""
        SELECT majcat, seg, art_status,
               COUNT(*) as slot_count,
               AVG(total_score) as avg_score,
               MIN(total_score) as min_score,
               MAX(total_score) as max_score,
               COUNT(DISTINCT st_cd) as store_count,
               COUNT(DISTINCT gen_art_color) as article_count
        FROM alloc_option_assignments WHERE run_id = :rid
        GROUP BY majcat, seg, art_status
        ORDER BY majcat, seg
    """), {'rid': run_id}).fetchall()

    return {
        "success": True,
        "data": [dict(r._mapping) for r in assignments]
    }


@router.get("/results/{run_id}/assignments")
async def get_assignments(
    run_id: str,
    majcat: Optional[str] = None,
    st_cd: Optional[str] = None,
    limit: int = 500,
    data_db: Session = Depends(get_data_db),
    current_user = Depends(get_current_user),
):
    """Get option assignments for a run."""
    query = "SELECT TOP(:lim) * FROM alloc_option_assignments WHERE run_id = :rid"
    params = {'rid': run_id, 'lim': limit}
    if majcat:
        query += " AND majcat = :mc"
        params['mc'] = majcat
    if st_cd:
        query += " AND st_cd = :st"
        params['st'] = st_cd
    query += " ORDER BY st_cd, seg, opt_no"

    rows = data_db.execute(text(query), params).fetchall()
    return {"success": True, "data": [dict(r._mapping) for r in rows], "count": len(rows)}


@router.get("/results/{run_id}/scores")
async def get_scores(
    run_id: str,
    majcat: Optional[str] = None,
    min_score: int = 0,
    limit: int = 200,
    data_db: Session = Depends(get_data_db),
    current_user = Depends(get_current_user),
):
    """Get article scores for a run."""
    query = "SELECT TOP(:lim) * FROM alloc_article_scores WHERE run_id = :rid AND total_score >= :ms"
    params = {'rid': run_id, 'lim': limit, 'ms': min_score}
    if majcat:
        query += " AND majcat = :mc"
        params['mc'] = majcat
    query += " ORDER BY total_score DESC"

    rows = data_db.execute(text(query), params).fetchall()
    return {"success": True, "data": [dict(r._mapping) for r in rows], "count": len(rows)}


@router.get("/results/{run_id}/variants")
async def get_variants(
    run_id: str,
    st_cd: Optional[str] = None,
    gen_art_color: Optional[str] = None,
    limit: int = 500,
    data_db: Session = Depends(get_data_db),
    current_user = Depends(get_current_user),
):
    """Get variant/size allocations for a run."""
    query = "SELECT TOP(:lim) * FROM alloc_variant_assignments WHERE run_id = :rid"
    params = {'rid': run_id, 'lim': limit}
    if st_cd:
        query += " AND st_cd = :st"
        params['st'] = st_cd
    if gen_art_color:
        query += " AND gen_art_color = :gac"
        params['gac'] = gen_art_color
    query += " ORDER BY st_cd, gen_art_color, sz"

    rows = data_db.execute(text(query), params).fetchall()
    return {"success": True, "data": [dict(r._mapping) for r in rows], "count": len(rows)}



@router.get("/results/{run_id}/store-summary")
async def get_store_summary(
    run_id: str,
    majcat: Optional[str] = None,
    data_db: Session = Depends(get_data_db),
    current_user = Depends(get_current_user),
):
    """Comprehensive store-level allocation summary."""
    try:
        params = {'rid': run_id}
        mc_filter = ""
        if majcat:
            mc_filter = " AND majcat = :mc"
            params['mc'] = majcat

        opt_rows = data_db.execute(text("""
            SELECT st_cd, majcat, seg,
                COUNT(*) as total_options,
                COUNT(DISTINCT gen_art_color) as unique_articles,
                COUNT(DISTINCT color) as unique_colors,
                AVG(CAST(total_score AS FLOAT)) as avg_score,
                MIN(total_score) as min_score, MAX(total_score) as max_score,
                MAX(CAST(mbq AS FLOAT)) as mbq,
                MAX(CAST(dc_stock_before AS FLOAT)) as dc_stock_total,
                SUM(CASE WHEN art_status='MIX' THEN 1 ELSE 0 END) as mix_articles,
                SUM(CASE WHEN art_status='NEW' THEN 1 ELSE 0 END) as new_articles,
                SUM(CASE WHEN art_status='HERO' THEN 1 ELSE 0 END) as hero_articles,
                SUM(CASE WHEN art_status='FOCUS' THEN 1 ELSE 0 END) as focus_articles,
                SUM(CASE WHEN art_status='FALLBACK' THEN 1 ELSE 0 END) as fallback_articles,
                MAX(CAST(mrp AS FLOAT)) as max_mrp,
                MIN(CASE WHEN CAST(mrp AS FLOAT)>0 THEN CAST(mrp AS FLOAT) END) as min_mrp,
                AVG(CASE WHEN CAST(mrp AS FLOAT)>0 THEN CAST(mrp AS FLOAT) END) as avg_mrp
            FROM alloc_option_assignments WHERE run_id = :rid """ + mc_filter + """
            GROUP BY st_cd, majcat, seg ORDER BY st_cd
        """), params).fetchall()
        opt_data = {r[0]: dict(r._mapping) for r in opt_rows}

        var_rows = data_db.execute(text("""
            SELECT st_cd,
                COUNT(*) as total_variants,
                COUNT(DISTINCT sz) as unique_sizes,
                SUM(alloc_qty) as total_alloc_qty,
                SUM(CAST(alloc_qty AS FLOAT) * ISNULL(CAST(mrp AS FLOAT),0)) as total_alloc_value,
                SUM(short_qty) as total_short_qty,
                AVG(fill_rate_pct) as avg_fill_rate_pct,
                '' as sizes_list
            FROM alloc_variant_assignments WHERE run_id = :rid """ + mc_filter + """
            GROUP BY st_cd ORDER BY st_cd
        """), params).fetchall()
        var_data = {r[0]: dict(r._mapping) for r in var_rows}

        stores = []
        for st in sorted(set(list(opt_data.keys()) + list(var_data.keys()))):
            o = opt_data.get(st, {})
            v = var_data.get(st, {})
            total_options = o.get('total_options', 0) or 0
            mbq = o.get('mbq', 0) or 0
            total_qty = v.get('total_alloc_qty', 0) or 0
            total_value = v.get('total_alloc_value', 0) or 0
            stores.append({
                'st_cd': st, 'majcat': o.get('majcat',''), 'seg': o.get('seg',''),
                'mbq': int(mbq),
                'total_options': total_options,
                'unique_articles': o.get('unique_articles',0) or 0,
                'unique_colors': o.get('unique_colors',0) or 0,
                'mix_articles': o.get('mix_articles',0) or 0,
                'new_articles': o.get('new_articles',0) or 0,
                'hero_articles': o.get('hero_articles',0) or 0,
                'focus_articles': o.get('focus_articles',0) or 0,
                'fallback_articles': o.get('fallback_articles',0) or 0,
                'avg_score': round(o.get('avg_score',0) or 0, 1),
                'total_variants': v.get('total_variants',0) or 0,
                'unique_sizes': v.get('unique_sizes',0) or 0,
                'sizes': v.get('sizes_list',''),
                'total_alloc_qty': total_qty,
                'total_alloc_value': round(total_value, 0),
                'total_short_qty': v.get('total_short_qty',0) or 0,
                'avg_fill_rate': round(v.get('avg_fill_rate_pct',0) or 0, 1),
                'min_mrp': o.get('min_mrp',0) or 0,
                'max_mrp': o.get('max_mrp',0) or 0,
                'avg_mrp': round(o.get('avg_mrp',0) or 0, 0),
                'fill_rate_pct': round(total_options / max(mbq,1) * 100, 1) if mbq > 0 else 0,
                'avg_qty_per_option': round(total_qty / max(total_options,1), 1),
                'avg_value_per_option': round(total_value / max(total_options,1), 0),
            })
        totals = {
            'total_stores': len(stores),
            'total_options': sum(s['total_options'] for s in stores),
            'total_articles': sum(s['unique_articles'] for s in stores),
            'total_variants': sum(s['total_variants'] for s in stores),
            'total_qty': sum(s['total_alloc_qty'] for s in stores),
            'total_value': sum(s['total_alloc_value'] for s in stores),
            'total_short': sum(s['total_short_qty'] for s in stores),
            'avg_mbq': round(sum(s['mbq'] for s in stores) / max(len(stores),1), 0),
            'avg_fill_rate': round(sum(s['fill_rate_pct'] for s in stores) / max(len(stores),1), 1),
        }
        return {"success": True, "data": stores, "totals": totals, "count": len(stores)}
    except Exception as e:
        return {"success": False, "error": str(e), "data": [], "totals": {}, "count": 0}


@router.get("/majcats")
async def list_majcats(
    data_db: Session = Depends(get_data_db),
    current_user = Depends(get_current_user),
):
    """List available MAJCATs from article data and Supabase budget."""
    majcats = set()
    
    # Source 1: ALLOCATION_MRDC_RAW_DATA (articles in Azure SQL)
    try:
        rows = data_db.execute(text("""
            SELECT DISTINCT MAJ_CAT as majcat FROM ALLOCATION_MRDC_RAW_DATA
            WHERE MAJ_CAT IS NOT NULL
        """)).fetchall()
        for r in rows:
            if r[0]: majcats.add(r[0])
    except:
        pass
    
    # Source 2: ARS_MSA_GEN_ART (MSA data)
    try:
        rows = data_db.execute(text("""
            SELECT DISTINCT MAJ_CAT as majcat FROM ARS_MSA_GEN_ART
            WHERE MAJ_CAT IS NOT NULL
        """)).fetchall()
        for r in rows:
            if r[0]: majcats.add(r[0])
    except:
        pass
    
    # Source 3: Snowflake ARTICLE_SCORES (the 246M scored pairs)
    try:
        from app.services.allocation import snowflake_loader as sf
        sf_majcats = sf.get_available_majcats()
        majcats.update(sf_majcats)
    except:
        pass
    
    # Source 4: Supabase budget (if configured)
    if not majcats:
        try:
            # Get Supabase config from system DB
            from app.database.session import get_system_db
            sys_db = next(get_system_db())
            rows = sys_db.execute(text("SELECT setting_key, setting_value FROM allocation_settings WHERE setting_key IN ('supabase_url','supabase_key')")).fetchall()
            settings = {r[0]: r[1] for r in rows}
            supa_url = settings.get('supabase_url', '')
            supa_key = settings.get('supabase_key', '')
            if supa_url and supa_key:
                import requests as req_lib
                resp = req_lib.get(
                    f'{supa_url}/rest/v1/co_budget_store_major_category?select=major_category&limit=5000',
                    headers={'apikey': supa_key, 'Authorization': f'Bearer {supa_key}'},
                    timeout=15
                )
                if resp.status_code == 200:
                    for r in resp.json():
                        if r.get('major_category'):
                            majcats.add(r['major_category'])
        except:
            pass
    
    return {"success": True, "data": sorted(list(majcats))}


@router.get("/snowflake/test")
async def test_snowflake(current_user = Depends(get_current_user)):
    """Test Snowflake connectivity and data availability."""
    try:
        from app.services.allocation import snowflake_loader as sf
        result = sf.test_connection()
        return {"success": True, "snowflake": result}
    except ImportError as e:
        return {"success": False, "error": f"snowflake_loader not available: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


class BudgetUpload(BaseModel):
    data: list
    majcat: Optional[str] = None


@router.post("/budget/upload")
async def upload_budget(
    payload: BudgetUpload,
    data_db: Session = Depends(get_data_db),
    current_user = Depends(get_current_user),
):
    """Upload derived budget data to budget_majcat table."""
    try:
        # Auto-create table
        data_db.execute(text("""
            IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id=OBJECT_ID(N'budget_majcat') AND type='U')
            CREATE TABLE budget_majcat (
                id INT IDENTITY(1,1) PRIMARY KEY,
                st_cd NVARCHAR(20) NOT NULL,
                majcat NVARCHAR(100) NOT NULL,
                bgt_sales FLOAT DEFAULT 0,
                bgt_display FLOAT DEFAULT 0,
                bgt_sales_val FLOAT DEFAULT 0,
                bgt_disp_val FLOAT DEFAULT 0,
                month INT DEFAULT 4,
                CONSTRAINT UQ_bgt_st_mc UNIQUE(st_cd, majcat, month)
            )
        """))
        data_db.commit()

        # Clear existing data for this majcat if specified
        if payload.majcat:
            data_db.execute(text("DELETE FROM budget_majcat WHERE majcat = :mc"), {"mc": payload.majcat})
            data_db.commit()

        # Insert rows
        inserted = 0
        for row in payload.data:
            try:
                data_db.execute(text("""
                    INSERT INTO budget_majcat (st_cd, majcat, bgt_sales, bgt_display, bgt_sales_val, bgt_disp_val, month)
                    VALUES (:st, :mc, :sq, :dq, :sv, :dv, :m)
                """), {
                    "st": row.get("st_cd", row.get("store_code", "")),
                    "mc": row.get("majcat", row.get("major_category", "")),
                    "sq": row.get("bgt_sales", row.get("sale_q_apr_2026", 0)),
                    "dq": row.get("bgt_display", row.get("disp_q_apr_2026", 0)),
                    "sv": row.get("bgt_sales_val", row.get("sale_v_apr_2026", 0)),
                    "dv": row.get("bgt_disp_val", row.get("disp_v_apr_2026", 0)),
                    "m": row.get("month", 4),
                })
                inserted += 1
            except Exception as e:
                continue
        data_db.commit()
        return {"success": True, "message": f"Uploaded {inserted}/{len(payload.data)} budget rows",
                "data": {"inserted": inserted, "total": len(payload.data)}}
    except Exception as e:
        return {"success": False, "message": str(e)}


@router.get("/budget/data")
async def get_budget_data(
    majcat: Optional[str] = None,
    data_db: Session = Depends(get_data_db),
    current_user = Depends(get_current_user),
):
    """Get budget data from budget_majcat table."""
    try:
        q = "SELECT st_cd, majcat, bgt_sales, bgt_display, bgt_sales_val, bgt_disp_val, month FROM budget_majcat"
        params = {}
        if majcat:
            q += " WHERE majcat = :mc"
            params["mc"] = majcat
        q += " ORDER BY st_cd"
        rows = data_db.execute(text(q), params).fetchall()
        return {"success": True, "data": [dict(r._mapping) for r in rows], "total": len(rows)}
    except Exception as e:
        return {"success": True, "data": [], "total": 0, "message": str(e)}


@router.get("/seed-test-data")
async def seed_test_data(
    majcat: str = "FW_M_SLIPPER",
    data_db: Session = Depends(get_data_db),
    current_user = Depends(get_current_user),
):
    """Seed test MSA articles, DC stock, and store priority for testing the allocation engine."""
    import random
    random.seed(42)

    try:
        # Create tables
        for ddl in [
            """IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id=OBJECT_ID(N'msa_articles') AND type='U')
               CREATE TABLE msa_articles (
                   id INT IDENTITY(1,1) PRIMARY KEY,
                   gen_art_color NVARCHAR(50) NOT NULL,
                   gen_art NVARCHAR(30), color NVARCHAR(20),
                   seg NVARCHAR(10), macro_mvgr NVARCHAR(50), mvgr1 NVARCHAR(50),
                   vendor_code NVARCHAR(20), mrp DECIMAL(10,2), fabric NVARCHAR(50),
                   season NVARCHAR(10), neck NVARCHAR(20), majcat NVARCHAR(100))""",
            """IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id=OBJECT_ID(N'dc_stock') AND type='U')
               CREATE TABLE dc_stock (
                   id INT IDENTITY(1,1) PRIMARY KEY,
                   gen_art_color NVARCHAR(50) NOT NULL,
                   majcat NVARCHAR(100), stock_qty INT DEFAULT 0)""",
            """IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id=OBJECT_ID(N'dc_variant_stock') AND type='U')
               CREATE TABLE dc_variant_stock (
                   id INT IDENTITY(1,1) PRIMARY KEY,
                   gen_art_color NVARCHAR(50), var_art NVARCHAR(30),
                   sz NVARCHAR(20), stock_qty INT DEFAULT 0)""",
            """IF NOT EXISTS (SELECT * FROM sys.objects WHERE object_id=OBJECT_ID(N'store_priority') AND type='U')
               CREATE TABLE store_priority (
                   id INT IDENTITY(1,1) PRIMARY KEY,
                   st_cd NVARCHAR(20) UNIQUE, priority_rank INT DEFAULT 1,
                   opt_density INT DEFAULT 16, is_active BIT DEFAULT 1)""",
        ]:
            data_db.execute(text(ddl))
        data_db.commit()

        # Clear existing test data
        for tbl in ['msa_articles', 'dc_stock', 'dc_variant_stock']:
            data_db.execute(text(f"DELETE FROM {tbl} WHERE majcat = :mc OR gen_art_color LIKE :p"),
                          {"mc": majcat, "p": f"{majcat[:5]}%"})
        data_db.commit()

        # Generate articles
        vendors = ['RELAXO', 'PARAGON', 'VKC', 'WALKAROO', 'SPARX', 'ACTION', 'FLITE', 'BAHAMAS']
        colors = ['BLK', 'BRN', 'NVY', 'GRY', 'RED', 'BLU', 'TAN', 'WHT']
        segs = ['APP', 'GM']
        mrps = [149, 199, 249, 299, 349, 399, 499]
        sizes = ['6', '7', '8', '9', '10', '11']
        fabrics = ['EVA', 'PU', 'PVC', 'RUBBER', 'SYNTHETIC']

        articles = []
        dc_stocks = []
        variants = []
        total_dc = 95192  # From Supabase DC inventory
        
        num_arts = 40
        for i in range(num_arts):
            gen_art = f"SLP{1000+i}"
            vendor = random.choice(vendors)
            mrp = random.choice(mrps)
            fabric = random.choice(fabrics)
            seg = random.choice(segs)
            num_colors = random.randint(2, 4)
            art_colors = random.sample(colors, num_colors)
            
            for clr in art_colors:
                gac = f"{gen_art}_{clr}"
                articles.append({
                    "gen_art_color": gac, "gen_art": gen_art, "color": clr,
                    "seg": seg, "macro_mvgr": f"SLIPPER_{vendor[:3]}",
                    "mvgr1": vendor, "vendor_code": vendor[:3],
                    "mrp": mrp, "fabric": fabric,
                    "season": "AY", "neck": "NA", "majcat": majcat,
                })
                # DC stock for this article-color
                art_stock = random.randint(200, 1500)
                dc_stocks.append({"gen_art_color": gac, "majcat": majcat, "stock_qty": art_stock})
                
                # Variant stock (by size)
                for sz in sizes:
                    sz_stock = random.randint(20, art_stock // 5)
                    variants.append({
                        "gen_art_color": gac, "var_art": f"{gen_art}_{clr}_{sz}",
                        "sz": sz, "stock_qty": sz_stock,
                    })

        # Insert articles
        for a in articles:
            data_db.execute(text("""
                INSERT INTO msa_articles (gen_art_color,gen_art,color,seg,macro_mvgr,mvgr1,vendor_code,mrp,fabric,season,neck,majcat)
                VALUES (:gen_art_color,:gen_art,:color,:seg,:macro_mvgr,:mvgr1,:vendor_code,:mrp,:fabric,:season,:neck,:majcat)
            """), a)
        
        for d in dc_stocks:
            data_db.execute(text("INSERT INTO dc_stock (gen_art_color,majcat,stock_qty) VALUES (:gen_art_color,:majcat,:stock_qty)"), d)
        
        for v in variants:
            data_db.execute(text("INSERT INTO dc_variant_stock (gen_art_color,var_art,sz,stock_qty) VALUES (:gen_art_color,:var_art,:sz,:stock_qty)"), v)

        # Seed store priority from budget data
        try:
            bgt_stores = data_db.execute(text("SELECT DISTINCT st_cd FROM budget_majcat")).fetchall()
            for idx, row in enumerate(bgt_stores):
                try:
                    data_db.execute(text("""
                        IF NOT EXISTS (SELECT 1 FROM store_priority WHERE st_cd=:st)
                        INSERT INTO store_priority (st_cd,priority_rank,opt_density) VALUES (:st,:r,:d)
                    """), {"st": row[0], "r": idx+1, "d": 16})
                except:
                    pass
        except:
            pass

        data_db.commit()

        return {
            "success": True,
            "message": f"Seeded test data for {majcat}",
            "data": {
                "articles": len(articles),
                "dc_stock_entries": len(dc_stocks),
                "variants": len(variants),
                "total_dc_stock": sum(d["stock_qty"] for d in dc_stocks),
            }
        }
    except Exception as e:
        import traceback
        return {"success": False, "message": str(e), "traceback": traceback.format_exc()}


@router.get("/snowflake-test")
async def test_snowflake():
    """Test Snowflake connectivity from Azure."""
    results = {"steps": []}
    try:
        import snowflake.connector
        results["steps"].append({"step": "import", "ok": True, "version": snowflake.connector.__version__})
    except Exception as e:
        results["steps"].append({"step": "import", "ok": False, "error": str(e)})
        return results

    try:
        conn = snowflake.connector.connect(
            account='iafphkw-hh80816', user='akashv2kart', password='SVXqEe5pDdamMb9',
            database='V2_ALLOCATION', login_timeout=30, network_timeout=30,
        )
        results["steps"].append({"step": "connect", "ok": True})
    except Exception as e:
        results["steps"].append({"step": "connect", "ok": False, "error": str(e)})
        return results

    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM RESULTS.ARTICLE_SCORES")
        cnt = cur.fetchone()[0]
        results["steps"].append({"step": "query_scores", "ok": True, "count": cnt})
    except Exception as e:
        results["steps"].append({"step": "query_scores", "ok": False, "error": str(e)})

    try:
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT STORE_CODE) FROM V2RETAIL.GOLD.FACT_STOCK_GENCOLOR WHERE STK_QTY > 0")
        r = cur.fetchone()
        results["steps"].append({"step": "query_stock", "ok": True, "rows": r[0], "stores": r[1]})
    except Exception as e:
        results["steps"].append({"step": "query_stock", "ok": False, "error": str(e)})

    try:
        # Test M_JEANS scored pairs (what engine needs)
        cur.execute("""
            SELECT ST_CD, GEN_ART_COLOR, TOTAL_SCORE, DC_STOCK_QTY, MRP
            FROM RESULTS.ARTICLE_SCORES WHERE MAJCAT = 'M_JEANS'
            ORDER BY TOTAL_SCORE DESC LIMIT 3
        """)
        rows = cur.fetchall()
        results["steps"].append({"step": "query_m_jeans", "ok": True, "sample": [dict(zip(['st_cd','gac','score','dc_stk','mrp'], r)) for r in rows]})
    except Exception as e:
        results["steps"].append({"step": "query_m_jeans", "ok": False, "error": str(e)})

    conn.close()
    return results
