"""
API v1 Router - Aggregates all endpoint routers
"""
from fastapi import APIRouter

# Phase 1: Auth, RBAC, RLS, Audit
from app.api.v1.endpoints.auth import router as auth_router
from app.api.v1.endpoints.users import router as users_router
from app.api.v1.endpoints.roles import router as roles_router
from app.api.v1.endpoints.rls import router as rls_router
from app.api.v1.endpoints.audit import router as audit_router

# Phase 2: Table Management, Upsert, Upload
from app.api.v1.endpoints.tables import router as tables_router
from app.api.v1.endpoints.data_ops import router as data_ops_router
from app.api.v1.endpoints.upload import router as upload_router

# Phase 4: MSA Stock Calculation
from app.api.v1.endpoints.msa_stock import router as msa_router
from app.api.v1.endpoints.msa import router as msa_legacy_router

# Phase 4b: Contribution Percentage
from app.api.v1.endpoints.contrib import router as contrib_router

# Phase 4b·SQL: Auto Cont % (SQL-direct pipeline via sp_AutoContCompute)
from app.api.v1.endpoints.auto_contrib import router as auto_contrib_router

# Phase 4c: BDC Creation
from app.api.v1.endpoints.bdc import router as bdc_router

# Phase 5: Settings
from app.api.v1.endpoints.settings import router as settings_router

# Phase 6b: SLOC Validation / Data Validation
from app.api.v1.endpoints.sloc_validation import router as store_stock_router
from app.api.v1.endpoints.grid_builder import router as grid_builder_router
from app.api.v1.endpoints.merge_rules import router as merge_rules_router

# Phase 7: Lookup Art Master
from app.api.v1.endpoints.lookup_art_master import router as lookup_art_master_router

# Data Checklist
from app.api.v1.endpoints.checklist import router as checklist_router

# Trends
from app.api.v1.endpoints.trends import router as trends_router

# Reports
from app.api.v1.endpoints.reports import router as reports_router

# Maintenance
from app.api.v1.endpoints.maintenance import router as maintenance_router

# Phase 6: Dashboard
from app.api.v1.endpoints.dashboard import router as dashboard_router

# Hold Dashboard — review HOLD_QTY across multiple angles
from app.api.v1.endpoints.hold_dashboard import router as hold_dashboard_router

# Pending Allocation — ARS_PEND_ALC lifecycle management
from app.api.v1.endpoints.pend_alc import router as pend_alc_router

# ARS Dashboard — unified analytics page (rev 2: Overview charts + Product Drill)
from app.api.v1.endpoints.ars_dashboard import router as ars_dashboard_router

# GAP Report — multi-category algorithm-driven review surface
from app.api.v1.endpoints.gap_report import router as gap_report_router

api_router = APIRouter(prefix="/api/v1")

# Phase 1
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(roles_router)
api_router.include_router(rls_router)
api_router.include_router(audit_router)

# Phase 2
api_router.include_router(tables_router)
api_router.include_router(data_ops_router)
api_router.include_router(upload_router)

# Phase 4: MSA Stock Calculation
api_router.include_router(msa_router)
api_router.include_router(msa_legacy_router)

# Phase 4b: Contribution Percentage
api_router.include_router(contrib_router)

# Phase 4b·SQL: Auto Cont % (SQL-direct pipeline)
api_router.include_router(auto_contrib_router)

# Phase 4c: BDC Creation
api_router.include_router(bdc_router)

# Phase 5
api_router.include_router(settings_router)

# Phase 6b: Store Stock / Data Preparation
api_router.include_router(store_stock_router)
api_router.include_router(grid_builder_router)
api_router.include_router(merge_rules_router)

# Phase 7: Lookup Art Master
api_router.include_router(lookup_art_master_router)

# Phase 6
api_router.include_router(dashboard_router)
api_router.include_router(hold_dashboard_router)
api_router.include_router(pend_alc_router)
api_router.include_router(ars_dashboard_router)
api_router.include_router(gap_report_router)

# Data Checklist
api_router.include_router(checklist_router)

# Trends
api_router.include_router(trends_router)

# Reports
api_router.include_router(reports_router)

# Listing
from app.api.v1.endpoints.listing import router as listing_router
api_router.include_router(listing_router)

# Maintenance (superadmin only)
api_router.include_router(maintenance_router)

# Pipeline (parallel MSA processing — replaces 20 machines)
from app.api.v1.endpoints.pipeline import router as pipeline_router
api_router.include_router(pipeline_router)

# Allocation Engine v2 (score-based, replaces Excel 8-level waterfall)
from app.api.v1.endpoints.allocation_engine import router as alloc_engine_router
api_router.include_router(alloc_engine_router)

# Project Tracker — hierarchical projects, status/priority/phase, dashboard
from app.api.v1.endpoints.project_tracker import router as project_tracker_router
api_router.include_router(project_tracker_router)

# OneSize — post-MSA filter that keeps GEN_ART/CLR combos appearing N times or fewer
from app.api.v1.endpoints.onesize import router as onesize_router
api_router.include_router(onesize_router)
