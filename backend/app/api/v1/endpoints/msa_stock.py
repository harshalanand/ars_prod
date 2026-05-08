"""
MSA Stock Calculation API Endpoints
RESTful API for MSA filtering, calculation, and analysis
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Path
from sqlalchemy.orm import Session
from typing import Dict, Any
import json
from loguru import logger
from sqlalchemy import text
import pandas as pd

from app.database.session import get_db, get_data_db
from app.schemas.msa import (
    MSAFilterRequest,
    MSACalculateRequest,
    PivotTableRequest,
    MSARunRequest,
    DistinctValuesResponse,
    InitialDataResponse,
    MSAFilterResponse,
    MSACalculateResponse,
)
from app.schemas.common import APIResponse
from app.services.msa_service import MSAService
from app.services.msa_result_storage import MSAResultStorageService
from app.services.msa_job_service import create_msa_storage_job, get_job_status, list_jobs
from app.security.dependencies import get_current_user, get_rls_context, RLSContext
from app.models.rbac import User

router = APIRouter(prefix="/msa", tags=["MSA Stock Calculation"])


# ============================================================================
# Initialize & Configuration Endpoints
# ============================================================================

@router.get(
    "/columns",
    response_model=APIResponse,
    summary="Get MSA columns and dates"
)
def get_msa_columns(
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get all available columns and dates from MSA view
    Used for initializing filter dropdowns
    
    Returns:
        - columns: List of all available columns
        - dates: List of distinct dates (sorted DESC)
        - filter_configs: List of saved filter configurations from database
    """
    try:
        service = MSAService(db)
        
        # Get columns, dates, and source data date
        columns = service.get_available_columns()
        dates = service.get_available_dates()
        data_date = service.get_source_data_date()

        logger.info(f"✅ Retrieved {len(columns)} columns and {len(dates)} dates, data_date={data_date}")
        logger.debug(f"Date samples: {dates[:5] if dates else 'None'}")
        
        # Get filter configs from database
        filter_configs = []
        try:
            # Query MSA_Filter_Config table
            sql = """
            SELECT 
                id,
                config_name, 
                created_at,
                is_last_used
            FROM dbo.MSA_Filter_Config
            ORDER BY created_at DESC
            """
            configs_df = pd.read_sql(text(sql), db.bind)
            
            if configs_df is not None and len(configs_df) > 0:
                filter_configs = [
                    {
                        'id': int(row['id']),
                        'name': row['config_name'],
                        'created_at': str(row['created_at']) if row['created_at'] else None,
                        'is_last_used': bool(row['is_last_used']) if 'is_last_used' in row else False
                    }
                    for _, row in configs_df.iterrows()
                ]
                logger.info(f"✅ Loaded {len(filter_configs)} filter configs from database: {[c['name'] for c in filter_configs]}")
        except Exception as e:
            logger.warning(f"⚠️ Could not fetch filter configs from database: {str(e)}")
            filter_configs = []
        
        # Always return response - dates should never be empty (uses fallback)
        return APIResponse(
            data={
                "columns": columns or [],
                "dates": dates or [],
                "filter_configs": filter_configs,
                "data_date": data_date,
            },
            message=f"Retrieved {len(columns)} columns, {len(dates)} dates, {len(filter_configs)} presets"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error getting MSA columns: {str(e)}", exc_info=True)
        # Return empty data instead of throwing - frontend handles gracefully
        return APIResponse(
            data={
                "columns": [],
                "dates": [],
                "filter_configs": []
            },
            message=f"Error: {str(e)}"
        )


@router.get(
    "/distinct",
    response_model=APIResponse,
    summary="Get distinct values for a column"
)
def get_distinct_values(
    column: str = Query(..., description="Column name"),
    date: str = Query(None, description="Optional date filter (YYYY-MM-DD)"),
    filters: str = Query(None, description="Optional JSON-encoded cascading filters (e.g., {\"ST_CD\": [\"DH24\", \"DH25\"]})"),
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get distinct values for filtering a specific column
    Supports cascading filters for dependent columns
    
    Query Parameters:
        - column: Column name (required)
        - date: Optional date filter
        - filters: Optional JSON-encoded cascading filters dict
    
    Returns:
        - values: List of distinct values for the column
        - total_count: Number of distinct values
    """
    try:
        if not column:
            raise HTTPException(status_code=400, detail="Column name required")
        
        logger.info(f"📍 Getting distinct values for column: {column}")
        if date:
            logger.info(f"   📅 Date filter: {date}")
        if filters:
            logger.info(f"   🔗 Cascading filters raw param: {filters}")
        
        # Parse cascading filters if provided
        additional_filters = None
        if filters:
            try:
                additional_filters = json.loads(filters)
                logger.info(f"✅ Parsed cascading filters: {additional_filters}")
            except json.JSONDecodeError as e:
                logger.warning(f"⚠️ Could not parse filters JSON: {str(e)}")
                logger.warning(f"   Raw value: {filters}")
                additional_filters = None
        
        service = MSAService(db)
        values = service.get_distinct_values(column, date, additional_filters)
        
        logger.info(f"✅ Query returned {len(values)} distinct values for {column}")
        if values:
            logger.debug(f"   Sample values: {values[:5]}")
        
        return APIResponse(
            data={
                "column": column,
                "values": values or [],
                "total_count": len(values)
            },
            message=f"Retrieved {len(values)} distinct values for {column}"
        )
    except HTTPException:
        raise
    except Exception as e:
        # Don't swallow into a 200-OK with empty values + buried error message —
        # the frontend treats that as "no data" and the user sees a blank dropdown
        # while the DB is silently down. Log the full stack for ops, then fail
        # loudly with HTTP 500 so the UI shows an explicit error toast.
        logger.error(
            f"❌ Error getting distinct values for {column}: {str(e)}",
            exc_info=True,
        )
        logger.error(f"   Column: {column}, Date: {date}, Filters: {filters}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load distinct values for {column}",
        )


@router.get(
    "/load/{config_name}",
    response_model=APIResponse,
    summary="Load filter configuration by name"
)
def load_filter_config(
    config_name: str,
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user)
):
    """
    Load a saved filter configuration by name
    
    Path Parameters:
        - config_name: Name of the filter configuration to load
    
    Returns:
        - config_name: Configuration name
        - filter_columns: List of selected filter columns
        - filters: Dictionary of filter values {column: [values]}
        - sql_agg: Threshold percentage
        - created_at: When configuration was created
    """
    try:
        if not config_name:
            raise HTTPException(status_code=400, detail="Config name required")
        
        logger.info(f"📂 Loading filter config: {config_name}")
        
        # Query the config from database
        sql = """
        SELECT 
            id,
            config_name,
            filter_columns,
            filter_values,
            sql_agg,
            created_at
        FROM dbo.MSA_Filter_Config
        WHERE config_name = :name
        """
        
        import json
        configs_df = pd.read_sql(text(sql), db.bind, params={"name": config_name})
        
        if configs_df is None or len(configs_df) == 0:
            logger.warning(f"⚠️ Config not found: {config_name}")
            return APIResponse(
                data={},
                message=f"Configuration '{config_name}' not found"
            )
        
        row = configs_df.iloc[0]
        
        # Parse JSON fields
        try:
            filter_columns = json.loads(row['filter_columns']) if row['filter_columns'] else []
            filters = json.loads(row['filter_values']) if row['filter_values'] else {}
        except:
            filter_columns = []
            filters = {}
        
        logger.info(f"✅ Loaded config '{config_name}': {len(filter_columns)} columns, {len(filters)} filters")
        
        return APIResponse(
            data={
                "config_name": row['config_name'],
                "filter_columns": filter_columns,
                "filters": filters,
                "sql_agg": int(row['sql_agg']) if row['sql_agg'] else 25,
                "created_at": str(row['created_at']) if row['created_at'] else None
            },
            message=f"Loaded configuration '{config_name}'"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error loading config '{config_name}': {str(e)}", exc_info=True)
        return APIResponse(
            data={},
            message=f"Error loading configuration: {str(e)}"
        )


@router.post(
    "/config",
    response_model=APIResponse,
    summary="Save or update filter configuration"
)
def save_filter_config(
    body: Dict[str, Any],
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user)
):
    """
    Save or update a filter configuration
    
    Request Body:
        - name: Configuration name (required)
        - filter_columns: List of selected filter columns
        - filters: Dictionary of filter values {column: [values]}
        - sql_agg: Threshold percentage (default 25)
    
    Returns:
        - config_name: Saved configuration name
        - message: Success or error message
    """
    try:
        config_name = body.get('name', '').strip()
        filter_columns = body.get('filter_columns', [])
        filters = body.get('filters', {})
        sql_agg = body.get('sql_agg', 25)
        
        if not config_name:
            raise HTTPException(status_code=400, detail="Configuration name is required")
        
        logger.info(f"💾 Saving filter config: {config_name}")
        
        import json
        
        # Convert to JSON strings
        filter_cols_json = json.dumps(filter_columns)
        filters_json = json.dumps(filters)
        
        # Check if config exists
        check_sql = "SELECT id FROM dbo.MSA_Filter_Config WHERE config_name = :name"
        exists_df = pd.read_sql(text(check_sql), db.bind, params={"name": config_name})
        
        with db.begin():
            if exists_df is not None and len(exists_df) > 0:
                # Update existing
                update_sql = """
                UPDATE dbo.MSA_Filter_Config
                SET filter_columns = :fc,
                    filter_values = :fv,
                    sql_agg = :sa,
                    is_last_used = 1,
                    updated_at = SYSUTCDATETIME()
                WHERE config_name = :n
                """
                db.execute(text(update_sql), {
                    "n": config_name,
                    "fc": filter_cols_json,
                    "fv": filters_json,
                    "sa": int(sql_agg)
                })
                logger.info(f"✅ Updated config: {config_name}")
            else:
                # Insert new
                insert_sql = """
                INSERT INTO dbo.MSA_Filter_Config
                (config_name, filter_columns, filter_values, sql_agg, is_last_used)
                VALUES (:n, :fc, :fv, :sa, 1)
                """
                db.execute(text(insert_sql), {
                    "n": config_name,
                    "fc": filter_cols_json,
                    "fv": filters_json,
                    "sa": int(sql_agg)
                })
                logger.info(f"✅ Created new config: {config_name}")
        
        return APIResponse(
            data={
                "config_name": config_name,
                "filter_columns": len(filter_columns),
                "filters": len(filters),
                "sql_agg": sql_agg
            },
            message=f"Configuration '{config_name}' saved successfully"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error saving config: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error saving configuration: {str(e)}")


# ============================================================================
# Filtering & Data Loading
# ============================================================================

@router.get(
    "/debug/test-date",
    response_model=APIResponse,
    summary="Debug: Test if date has data"
)
def debug_test_date(
    date: str = Query(..., description="Date to test (YYYY-MM-DD)"),
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user)
):
    """
    Debug endpoint to check if a date has data in the view
    """
    try:
        logger.info(f"🔍 DEBUG: Testing date '{date}'")
        
        # Test 1: Check if date has ANY data
        sql1 = f"""
        SELECT COUNT(*) as row_count, 
               COUNT(DISTINCT CAST([DATE] AS DATE)) as date_count
        FROM {MSAService(db).main_table}
        WHERE CAST([DATE] AS DATE) = :test_date
        """
        test_df = pd.read_sql(text(sql1), db.bind, params={"test_date": date})
        row_count = int(test_df['row_count'].iloc[0])
        date_count = int(test_df['date_count'].iloc[0])
        
        logger.info(f"✅ Date '{date}' has {row_count} rows")
        
        # Test 2: Get sample ST_CD values for this date
        # SQL Server uses TOP n, not LIMIT n. The previous LIMIT 10 made this
        # endpoint 500 with "Incorrect syntax near 'LIMIT'" since it was written.
        sql2 = f"""
        SELECT DISTINCT TOP 10 ST_CD
        FROM {MSAService(db).main_table}
        WHERE CAST([DATE] AS DATE) = :test_date
        """
        sample_df = pd.read_sql(text(sql2), db.bind, params={"test_date": date})
        st_cd_samples = sample_df['ST_CD'].tolist() if len(sample_df) > 0 else []

        logger.info(f"✅ Sample ST_CD values for date: {st_cd_samples}")

        # Test 3: Get recent available dates in the view
        sql3 = f"""
        SELECT DISTINCT TOP 20 CAST([DATE] AS DATE) as date_val
        FROM {MSAService(db).main_table}
        ORDER BY date_val DESC
        """
        dates_df = pd.read_sql(text(sql3), db.bind)
        available_dates = [str(d) for d in dates_df['date_val'].tolist()]
        
        logger.info(f"✅ Available dates: {available_dates}")
        
        return APIResponse(
            data={
                "date_tested": date,
                "row_count": row_count,
                "has_data": row_count > 0,
                "sample_st_cd_values": st_cd_samples,
                "available_dates": available_dates
            },
            message=f"Date '{date}' has {row_count} rows"
        )
    except Exception as e:
        logger.error(f"❌ Debug error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/filter",
    response_model=APIResponse,
    summary="Apply filters and load MSA data"
)
def apply_filters(
    body: MSAFilterRequest,
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user),
    request=None  # For session storage if needed
):
    """
    Apply filters to MSA data and load results
    Data is stored in session/cache for subsequent operations
    
    Request Body:
        - date: Date filter (YYYY-MM-DD)
        - filters: Dict of column names to list of filter values
    
    Returns:
        - row_count: Number of rows loaded
        - columns: Available columns in result
        - total_stock_qty: Sum of STK_Q column
        - message: Status message
    """
    try:
        if not body.date:
            logger.warning(f"❌ /filter endpoint called without date")
            raise HTTPException(status_code=400, detail="Date is required")
        
        logger.info(f"📥 /filter endpoint called")

        logger.info(f"   Date: {body.date}")
        logger.info(f"   Filters received: {body.filters}")
        logger.info(f"   Number of filter columns: {len(body.filters)}")
        
        # Check if filters are empty
        if not body.filters or all(not v for v in body.filters.values()):
            logger.warning(f"⚠️ No filters provided! Filters dict: {body.filters}")
        
        service = MSAService(db)
        
        # Call service to apply filters and get dataframe
        df, total_stock_qty = service.apply_filters(body.date, body.filters)
        
        logger.info(f"✅ Filters applied successfully:")
        logger.info(f"   Loaded {len(df)} rows")
        logger.info(f"   Total STK_Q: {total_stock_qty}")
        logger.info(f"   Columns: {len(df.columns)} = {df.columns.tolist() if len(df) > 0 else 'N/A'}")
        
        # Check data size
        import sys
        df_memory_mb = df.memory_usage(deep=True).sum() / (1024 * 1024) if len(df) > 0 else 0.0
        logger.info(f"   Memory usage: {df_memory_mb:.2f}MB")
        
        if len(df) == 0:
            logger.warning(f"⚠️ WARNING: Query returned 0 rows!")
            logger.warning(f"   Check if:")
            logger.warning(f"   1. Date '{body.date}' has data in the view")
            logger.warning(f"   2. Filter values actually exist for this date")
            logger.warning(f"   3. Filters are not empty: {body.filters}")
        
        if df_memory_mb > 500:
            logger.warning(f"⚠️ Large result set detected ({df_memory_mb:.2f}MB)")
        
        return APIResponse(
            data={
                "row_count": len(df),
                "columns": df.columns.tolist() if len(df) > 0 else [],
                "total_stock_qty": float(total_stock_qty),
                "memory_mb": round(df_memory_mb, 2)
            },
            message=f"Loaded {len(df)} rows successfully"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error in /filter endpoint: {str(e)}", exc_info=True)
        logger.error(f"   Request body: date={body.date if 'body' in locals() else 'N/A'}, filters={body.filters if 'body' in locals() else 'N/A'}")
        raise HTTPException(status_code=500, detail=f"Error applying filters: {str(e)}")


# ============================================================================
# MSA Calculation
# ============================================================================

@router.post(
    "/calculate",
    response_model=APIResponse,
    summary="Calculate MSA allocation and store results"
)
def calculate_msa(
    body: MSACalculateRequest,
    db: Session = Depends(get_data_db),
    main_db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    rls_context: RLSContext = Depends(get_rls_context),
    request=None  # For session retrieval if needed
):
    """
    Calculate MSA allocation from filtered data
    Returns 3 result sets: base analysis, generated colors, color variants
    ALSO: Stores all results to database with sequence tracking and returns sequence_id
    
    Category RLS: Results are automatically filtered to the user's assigned
    major categories. Admins/SuperAdmins see all categories.
    
    Request Body:
        - slocs: List of SLOC codes to include
        - threshold: Minimum allocation percentage (0-100)
        - date: Optional date filter
        - filters: Optional filter dict
    
    Returns:
        - msa: Base MSA analysis table
        - msa_gen_clr: Generated colors analysis
        - msa_gen_clr_var: Color variants analysis
        - row_counts: Row counts for each result set
        - sequence_id: Database sequence ID for this calculation
        - storage_info: Info about stored results
    """
    try:
        if not body.slocs:
            raise HTTPException(status_code=400, detail="At least one SLOC is required")
        
        # Get category restrictions from RLS context
        rls_categories = rls_context.get_category_values() if rls_context.has_category_restrictions else None
        if rls_categories:
            logger.info(f"📊 MSA for user {current_user.username} — categories: {rls_categories}")
        
        logger.info(f"📊 Calculating MSA for SLOCs: {body.slocs}, threshold: {body.threshold}, date: {body.date}")
        
        data_service = MSAService(db, rls_categories=rls_categories)
        
        # Load data with filters to improve performance
        # Use provided date and filters, or load all data if not provided
        date_filter = body.date if body.date else ""
        filters = body.filters if body.filters else {}

        # ST_CD is used ONLY as a cascading parent to narrow the SLOC dropdown
        # in the frontend.  It must NOT be passed as a SQL WHERE clause here —
        # doing so restricts data to a single RDC warehouse even when the user
        # selects SLOCs across multiple RDCs.  The SLOC filter already constrains
        # the data to the correct warehouses.
        filters_for_sql = {k: v for k, v in filters.items() if k != "ST_CD"}
        if "ST_CD" in filters:
            logger.info(
                f"[msa] ST_CD filter ({filters['ST_CD']}) stripped from SQL — "
                f"SLOC selection already constrains data to correct RDC(s)"
            )

        logger.info(f"Loading data with filters - date: '{date_filter}', filters: {filters_for_sql}")
        df, _ = data_service.apply_filters(date_filter, filters_for_sql)

        # Detect which requested RDCs actually have data for the chosen SLOCs
        requested_rdcs = filters.get("ST_CD", [])
        covered_rdcs = []
        missing_rdcs = []
        if requested_rdcs and "ST_CD" in df.columns:
            covered_rdcs = sorted(df["ST_CD"].dropna().unique().tolist())
            missing_rdcs = [r for r in requested_rdcs if r not in covered_rdcs]
            if missing_rdcs:
                logger.warning(
                    f"[msa] RDC(s) {missing_rdcs} have no data for SLOC(s) {body.slocs} "
                    f"on date '{date_filter}' — they will be absent from results"
                )

        # Calculate MSA
        results = data_service.calculate(df, body.slocs, body.threshold)

        logger.info(f"✅ MSA calculation complete: {results['row_counts']}")
        results["covered_rdcs"] = covered_rdcs
        results["missing_rdcs"] = missing_rdcs
        
        # ================================================================
        # CREATE SEQUENCE RECORD AND QUEUE BACKGROUND STORAGE JOB (IF ENABLED)
        # ================================================================
        storage_service = MSAResultStorageService(db)
        
        # Check if auto_store_results is enabled (default: True for backward compatibility)
        auto_store = getattr(body, 'auto_store_results', True)
        logger.info(f"🔍 Auto-store results: {auto_store}")
        
        if not auto_store:
            # Auto-store disabled: Return calculation results WITHOUT creating storage job
            logger.info("⏭️  Skipping storage job creation (auto_store_results=False)")
            return APIResponse(
                data={
                    **results,
                    'sequence_id': None,
                    'storage_job': None
                },
                message="MSA calculation completed. Not storing (auto-save disabled). Click 'Save to Database' to store manually."
            )
        
        # Auto-store enabled: Create sequence and queue storage job
        try:
            # Get filter columns from request (or detect from provided filters)
            filter_columns = body.filter_columns if hasattr(body, 'filter_columns') and body.filter_columns else list(filters.keys())
            
            # Create sequence record first (synchronously)
            sequence_id = storage_service.create_sequence_record(
                date_filter=date_filter,
                filter_columns=filter_columns,
                filters=filters,  # Pass dict directly, not JSON
                threshold=int(body.threshold),
                slocs=body.slocs,  # Pass list directly
                msa_row_count=results['row_counts'].get('msa', 0),
                gen_color_row_count=results['row_counts'].get('msa_gen_clr', 0),
                color_variant_row_count=results['row_counts'].get('msa_gen_clr_var', 0),
                created_by=getattr(current_user, 'username', 'system')
            )
            
            logger.info(f"✅ Created sequence record: {sequence_id}")
            
            # Queue the data storage as background job
            job_info = create_msa_storage_job(
                db=main_db,
                sequence_id=sequence_id,
                calculation_results={
                    'msa': results.get('msa', []),
                    'msa_gen_clr': results.get('msa_gen_clr', []),
                    'msa_gen_clr_var': results.get('msa_gen_clr_var', []),
                },
                created_by=getattr(current_user, 'username', 'system')
            )
            
            logger.info(f"📋 Queued storage job: {job_info['job_id']}")
            
            # Return calculation results with job info
            response_data = {
                **results,  # Include all calculation results
                'sequence_id': sequence_id,
                'storage_job': {
                    'job_id': job_info['job_id'],
                    'status': job_info['status'],
                    'position_in_queue': job_info['position_in_queue'],
                    'total_rows': job_info['total_rows'],
                }
            }
            
            return APIResponse(
                data=response_data,
                message=f"MSA calculation completed. Storage queued as job {job_info['job_id']} (position {job_info['position_in_queue']})"
            )
        except Exception as storage_err:
            logger.error(f"⚠️ Error creating storage job (but calculation succeeded): {storage_err}")
            # Return calculation results even if job creation failed
            return APIResponse(
                data={
                    **results,
                    'sequence_id': 0,
                    'storage_error': str(storage_err)
                },
                message=f"MSA calculation completed. Storage job error: {str(storage_err)}"
            )
    except Exception as e:
        logger.error(f"❌ Error calculating MSA: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Pivot Table Generation
# ============================================================================

@router.post(
    "/pivot",
    response_model=APIResponse,
    summary="Generate pivot table"
)
def generate_pivot_table(
    body: PivotTableRequest,
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user),
    request=None
):
    """
    Generate pivot table from MSA data
    
    Request Body:
        - index_cols: Column(s) for index (rows)
        - pivot_cols: Column(s) for pivot (columns)
        - value_cols: Column(s) for values
        - agg_funcs: Aggregation functions
        - fill_zero: Fill missing with 0
        - margin_totals: Add margin totals
    
    Returns:
        - columns: Pivot table column names
        - data: Pivot table data rows
        - row_count: Number of rows in pivot
    """
    try:
        logger.info(f"Generating pivot table: index={body.index_cols}, pivot={body.pivot_cols}, values={body.value_cols}")
        
        service = MSAService(db)
        
        # Load data - in production would retrieve from cache
        df, _ = service.apply_filters("", {})
        
        # Generate pivot
        pivot_result = service.generate_pivot(
            df,
            body.index_cols,
            body.pivot_cols,
            body.value_cols,
            body.agg_funcs,
            body.fill_zero,
            body.margin_totals
        )
        
        logger.info(f"Pivot table generated: {pivot_result['row_count']} rows")
        
        return APIResponse(
            data=pivot_result,
            message="Pivot table generated successfully"
        )
    except Exception as e:
        logger.error(f"Error generating pivot: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Legacy Endpoints (for backward compatibility)
# ============================================================================

@router.post(
    "/run",
    response_model=APIResponse,
    summary="Run MSA calculation (legacy)"
)
def run_msa_legacy(
    body: MSARunRequest,
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user)
):
    """
    Legacy MSA run endpoint for backward compatibility
    Combines filtering and calculation in one call
    """
    try:
        service = MSAService(db)

        # Strip ST_CD — only a UI cascading parent, must not restrict SQL to one RDC
        legacy_filters = {k: v for k, v in (body.filters or {}).items() if k != "ST_CD"}

        # Apply filters
        df, _ = service.apply_filters("", legacy_filters)

        # Calculate
        results = service.calculate(df, body.slocs, body.threshold)
        
        return APIResponse(
            data=results,
            message="MSA calculation completed"
        )
    except Exception as e:
        logger.error(f"Error in legacy MSA run: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/save",
    response_model=APIResponse,
    summary="Save MSA results to database tables"
)
def save_msa_results(
    body: Dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Save MSA calculation results to database tables (ARS_MSA_TOTAL, ARS_MSA_GEN_ART, ARS_MSA_VAR_ART)
    
    Request Body:
        - msa: List of MSA analysis results
        - msa_gen_clr: List of generated colors results
        - msa_gen_clr_var: List of color variants results
        - row_counts: Dict with row counts
        - date_filter: Date filter applied
        - filter_columns: List of filter columns
        - filters: Dict of filter values
        - threshold: Threshold percentage
        - slocs: List of SLOC codes
    
    Returns:
        - sequence_id: Database sequence ID for these results
        - storage_info: Info about stored results
        - message: Success or error message
    """
    try:
        logger.info(f"💾 Save Results button clicked - storing to database")
        
        # Extract data from request body
        calculation_results = {
            'msa': body.get('msa', []),
            'msa_gen_clr': body.get('msa_gen_clr', []),
            'msa_gen_clr_var': body.get('msa_gen_clr_var', []),
            'row_counts': body.get('row_counts', {})
        }
        
        date_filter = body.get('date_filter', '')
        filter_columns = body.get('filter_columns', [])
        filters = body.get('filters', {})
        threshold = body.get('threshold', 25)
        slocs = body.get('slocs', [])
        
        logger.info(f"   📊 MSA: {len(calculation_results['msa'])} rows")
        logger.info(f"   🎨 Generated Colors: {len(calculation_results['msa_gen_clr'])} rows")
        logger.info(f"   🔸 Color Variants: {len(calculation_results['msa_gen_clr_var'])} rows")
        logger.info(f"   📅 Date filter: {date_filter}")
        logger.info(f"   ⚙️ Threshold: {threshold}")
        
        # Store results using storage service
        storage_service = MSAResultStorageService(db)
        
        storage_info = storage_service.store_results(
            calculation_results=calculation_results,
            date_filter=date_filter,
            filter_columns=filter_columns,
            filters=filters,
            threshold=threshold,
            slocs=slocs,
            created_by=getattr(current_user, 'username', 'system')
        )
        
        sequence_id = storage_info.get('sequence_id', 0)
        
        logger.info(f"✅ Results stored successfully with sequence ID: {sequence_id}")
        
        return APIResponse(
            data={
                "sequence_id": sequence_id,
                "storage_info": storage_info,
                "msa_rows": len(calculation_results['msa']),
                "gen_color_rows": len(calculation_results['msa_gen_clr']),
                "color_variant_rows": len(calculation_results['msa_gen_clr_var'])
            },
            message=f"✅ Results saved to database with Sequence ID: {sequence_id}"
        )
    except Exception as e:
        logger.error(f"❌ Error saving MSA results: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error saving results: {str(e)}")


# ============================================================================
# Stored Results Management
# ============================================================================

@router.get(
    "/results/sequences",
    response_model=APIResponse,
    summary="Get list of stored calculation sequences"
)
def get_stored_sequences(
    limit: int = Query(10, ge=1, le=100, description="Number of sequences to return"),
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get list of recent stored MSA calculation sequences
    Shows sequence ID, calculation date, row counts, and user info
    
    Query Parameters:
        - limit: Number of sequences to retrieve (default 10, max 100)
    
    Returns:
        - List of sequence records with metadata
    """
    try:
        logger.info(f"📋 Retrieving {limit} stored sequences")
        
        storage_service = MSAResultStorageService(db)
        sequences = storage_service.get_latest_sequences(limit=limit)
        
        logger.info(f"✅ Retrieved {len(sequences)} sequences")
        
        return APIResponse(
            data={
                "sequences": sequences,
                "total_retrieved": len(sequences)
            },
            message=f"Retrieved {len(sequences)} stored sequences"
        )
    except Exception as e:
        logger.error(f"Error retrieving sequences: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/results/{sequence_id}",
    response_model=APIResponse,
    summary="Get stored MSA calculation results by sequence ID"
)
def get_stored_results(
    sequence_id: int = Path(..., ge=1, description="Calculation sequence ID"),
    table: str = Query("msa", pattern="^(msa|msa_gen_clr|msa_gen_clr_var)$", description="Which result table to retrieve"),
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user)
):
    """
    Retrieve stored MSA calculation results by sequence ID
    
    Path Parameters:
        - sequence_id: Calculation sequence ID (required)
    
    Query Parameters:
        - table: Result table to retrieve - msa, msa_gen_clr, or msa_gen_clr_var
    
    Returns:
        - data: List of result rows
        - metadata: Calculation metadata (date, filters, threshold, etc.)
        - row_count: Number of rows in this result set
    """
    try:
        logger.info(f"📂 Retrieving results for sequence {sequence_id}, table: {table}")
        
        storage_service = MSAResultStorageService(db)
        data, metadata = storage_service.get_sequence_data(sequence_id, table)
        
        if not data:
            logger.warning(f"No data found for sequence {sequence_id}")
            return APIResponse(
                data={
                    "sequence_id": sequence_id,
                    "table": table,
                    "data": [],
                    "metadata": {},
                    "row_count": 0
                },
                message=f"No data found for sequence {sequence_id}"
            )
        
        logger.info(f"✅ Retrieved {len(data)} rows from {table}")
        
        return APIResponse(
            data={
                "sequence_id": sequence_id,
                "table": table,
                "data": data,
                "metadata": metadata,
                "row_count": len(data)
            },
            message=f"Retrieved {len(data)} rows from sequence {sequence_id}"
        )
    except Exception as e:
        logger.error(f"Error retrieving stored results: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/results/{sequence_id}/summary",
    response_model=APIResponse,
    summary="Get summary of a stored calculation"
)
def get_sequence_summary(
    sequence_id: int = Path(..., ge=1, description="Calculation sequence ID"),
    db: Session = Depends(get_data_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get summary/metadata of a stored calculation sequence
    Shows calculation parameters, row counts, and timestamp
    
    Path Parameters:
        - sequence_id: Calculation sequence ID (required)
    
    Returns:
        - calculation_date: When calculation was performed
        - date_filter: Date filter that was applied
        - filter_columns: Columns that were used for filtering
        - filters: Filter values that were applied
        - threshold: Threshold percentage used
        - slocs: SLOC codes that were included
        - row_counts: Number of rows in each result table
        - created_by: User who performed calculation
        - status: Status of the calculation (COMPLETED, ERROR, etc.)
    """
    try:
        logger.info(f"📊 Getting summary for sequence {sequence_id}")
        
        storage_service = MSAResultStorageService(db)
        data, metadata = storage_service.get_sequence_data(sequence_id, 'msa')
        
        if not metadata:
            logger.warning(f"No metadata found for sequence {sequence_id}")
            raise HTTPException(status_code=404, detail=f"Sequence {sequence_id} not found")
        
        # Build summary response
        summary = {
            "sequence_id": sequence_id,
            "calculation_date": metadata.get('calculation_date'),
            "date_filter": metadata.get('date_filter'),
            "filter_columns": metadata.get('filter_columns', []),
            "filters": metadata.get('filters', {}),
            "threshold": metadata.get('threshold'),
            "slocs": metadata.get('slocs', []),
            "row_counts": {
                "msa": metadata.get('msa_row_count', 0),
                "msa_gen_clr": metadata.get('gen_color_row_count', 0),
                "msa_gen_clr_var": metadata.get('color_variant_row_count', 0)
            },
            "created_by": metadata.get('created_by'),
            "status": metadata.get('status')
        }
        
        logger.info(f"✅ Summary retrieved for sequence {sequence_id}")
        
        return APIResponse(
            data=summary,
            message=f"Summary retrieved for sequence {sequence_id}"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving sequence summary: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Background Job Management
# ============================================================================

@router.get(
    "/jobs/{job_id}",
    response_model=APIResponse,
    summary="Get MSA storage job status"
)
def get_storage_job_status(
    job_id: str = Path(..., description="Job ID"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get the status of an MSA storage job
    
    Path Parameters:
        - job_id: MSA storage job ID
    
    Returns:
        - job_id: Job ID
        - sequence_id: Sequence ID for this calculation
        - status: Current status (queued, running, completed, failed)
        - total_rows: Total rows to process
        - processed_rows: Rows processed so far
        - inserted_msa: Rows inserted into ARS_MSA_TOTAL
        - inserted_colors: Rows inserted into ARS_MSA_GEN_ART
        - inserted_variants: Rows inserted into ARS_MSA_VAR_ART
        - error_message: Error message if failed
        - created_at: When job was created
        - started_at: When job started processing
        - completed_at: When job completed
        - duration_ms: Processing duration in milliseconds
    """
    try:
        logger.info(f"📋 Getting job status: {job_id}")
        
        job_status = get_job_status(db, job_id)
        
        if not job_status:
            logger.warning(f"⚠️ Job not found: {job_id}")
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        
        return APIResponse(
            data=job_status,
            message=f"Job {job_id} status: {job_status['status']}"
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error getting job status: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/jobs",
    response_model=APIResponse,
    summary="List MSA storage jobs"
)
def list_storage_jobs(
    status: str = Query(None, description="Filter by status (pending, running, completed, failed)"),
    limit: int = Query(20, ge=1, le=100, description="Maximum number of jobs to return"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    List MSA storage jobs with optional status filter
    
    Query Parameters:
        - status: Optional status filter (pending, running, completed, failed)
        - limit: Maximum jobs to return (default 20, max 100)
    
    Returns:
        - jobs: List of job records
    """
    try:
        logger.info(f"📋 Listing MSA storage jobs (status={status}, limit={limit})")
        
        jobs = list_jobs(db, status, limit)
        
        return APIResponse(
            data={
                'jobs': jobs,
                'count': len(jobs),
                'filtered_by_status': status or 'all'
            },
            message=f"Retrieved {len(jobs)} jobs"
        )
    except Exception as e:
        logger.error(f"❌ Error listing jobs: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=APIResponse,
    summary="Cancel an MSA storage job"
)
def cancel_storage_job(
    job_id: str = Path(..., description="Job ID to cancel"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Cancel a queued MSA storage job
    Can only cancel jobs that haven't started processing yet
    
    Path Parameters:
        - job_id: MSA storage job ID to cancel
    
    Returns:
        - job_id: Job ID
        - status: New status (cancelled)
        - message: Success or failure message
    """
    try:
        logger.info(f"🛑 Cancelling job: {job_id}")
        
        from app.services.msa_job_service import cancel_job
        success = cancel_job(db, job_id)
        
        if success:
            logger.info(f"✅ Job {job_id} cancelled")
            return APIResponse(
                data={'job_id': job_id, 'status': 'cancelled'},
                message=f"Job {job_id} cancelled successfully"
            )
        else:
            logger.warning(f"⚠️ Could not cancel job {job_id} (may already be running)")
            raise HTTPException(
                status_code=400,
                detail=f"Job {job_id} cannot be cancelled (already running or completed)"
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error cancelling job: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/jobs/cancel/all",
    response_model=APIResponse,
    summary="Cancel all pending MSA storage jobs"
)
def cancel_all_storage_jobs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Cancel all pending/queued MSA storage jobs
    
    Returns:
        - cancelled_count: Number of jobs cancelled
        - message: Success message
    """
    try:
        logger.info(f"🛑 Cancelling all pending jobs")
        
        from app.services.msa_job_service import cancel_all_pending_jobs
        count = cancel_all_pending_jobs(db)
        
        logger.info(f"✅ Cancelled {count} pending jobs")
        return APIResponse(
            data={'cancelled_count': count},
            message=f"Cancelled {count} pending MSA storage jobs"
        )
    except Exception as e:
        logger.error(f"❌ Error cancelling all jobs: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


