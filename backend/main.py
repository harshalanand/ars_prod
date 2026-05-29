"""
ARS - Auto Replenishment System - FastAPI Application
======================================================
Enterprise-grade backend for multi-store retail management.
"""
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.core.config import get_settings
from app.database.session import check_db_connection, check_data_db_connection, SessionLocal, enable_rcsi, Base, system_engine, reconcile_columns
from app.services.tempdb_cleanup_service import tempdb_cleaner
from app.api.v1.router import api_router
from app.middleware.exception_handler import global_exception_handler, request_logging_middleware, auto_free_space_middleware

settings = get_settings()

# ============================================================================
# Logging Configuration
# ============================================================================
logger.remove()
logger.add(sys.stderr, level=settings.LOG_LEVEL, format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}")
os.makedirs("logs", exist_ok=True)
logger.add(
    settings.LOG_FILE,
    rotation="10 MB",
    retention="30 days",
    level=settings.LOG_LEVEL,
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{function}:{line} | {message}",
)


# ============================================================================
# Application Lifespan
# ============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    logger.info(f"Starting {settings.APP_NAME} v{settings.APP_VERSION}")

    # Show the resolved DB target (JSON overrides → env defaults) so it is
    # obvious from logs which server the app actually connected to.
    db_cfg = settings._db()
    logger.info(
        f"Database target → server={db_cfg['server']} "
        f"port={db_cfg.get('port') or '(default)'} "
        f"system_db={db_cfg['system_database']} "
        f"data_db={db_cfg['data_database']} "
        f"user={db_cfg['username']}"
    )

    # Check database connection
    if check_db_connection():
        logger.info("✅ Database connection successful")
    else:
        logger.error("❌ Database connection failed!")

    # Enable RCSI so readers never block during uploads
    enable_rcsi()

    # Ensure all model tables exist (auto-create new ones)
    try:
        import app.models.rbac  # noqa - register models with Base
        import app.models.rls   # noqa
        import app.models.audit # noqa
        Base.metadata.create_all(bind=system_engine, checkfirst=True)
        logger.info("System DB tables verified")
        # create_all only handles MISSING TABLES — never adds new columns
        # to existing ones. Run reconcile_columns to ALTER TABLE ADD any
        # model columns that exist in code but not in the live DB
        # (e.g. upload_jobs.validation_errors after a model update).
        added = reconcile_columns(system_engine)
        if added:
            logger.info(f"Schema reconcile added columns: {added}")
    except Exception as e:
        logger.warning(f"Table auto-create: {e}")

    # Create super admin + seed permissions if needed
    try:
        from app.services.auth_service import create_super_admin_if_needed, seed_permissions_if_needed
        db = SessionLocal()
        try:
            create_super_admin_if_needed(db)
            seed_permissions_if_needed(db)
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Bootstrap skipped: {e}")

    # Clean up any hanging jobs from previous runs
    try:
        from app.models.audit import MSAStorageJob
        db = SessionLocal()
        try:
            hanging_jobs = db.query(MSAStorageJob).filter(
                MSAStorageJob.status == 'running'
            ).all()
            if hanging_jobs:
                logger.warning(f"Found {len(hanging_jobs)} hanging jobs from previous server run, marking as failed")
                for job in hanging_jobs:
                    job.status = 'failed'
                    job.error_message = 'Job interrupted - server was stopped while job was running'
                    job.completed_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Could not clean up hanging jobs: {e}")

    logger.info(f"✅ {settings.APP_NAME} started on {settings.HOST}:{settings.PORT}")

    # Start TempDB background cleanup service
    try:
        tempdb_cleaner.start()
        logger.info("✅ TempDB cleanup service started")
    except Exception as e:
        logger.warning(f"TempDB cleanup service failed to start: {e}")

    yield
    logger.warning(f"Shutting down {settings.APP_NAME}...")

    # Stop TempDB cleanup service
    try:
        tempdb_cleaner.stop()
    except Exception:
        pass

    # Mark any currently running job as interrupted
    try:
        from app.models.audit import MSAStorageJob
        db = SessionLocal()
        try:
            running_jobs = db.query(MSAStorageJob).filter(
                MSAStorageJob.status == 'running'
            ).all()
            if running_jobs:
                logger.warning(f"Found {len(running_jobs)} running jobs at shutdown, marking as failed")
                for job in running_jobs:
                    job.status = 'failed'
                    job.error_message = 'Job interrupted - server shutdown'
                    job.completed_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Could not mark running jobs as failed: {e}")
    
    logger.info(f"✅ {settings.APP_NAME} stopped")


# ============================================================================
# Create FastAPI App
# ============================================================================
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="ARS - Auto Replenishment System",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
    redirect_slashes=False,
)

# Store debug flag for exception handler
app.state.debug = settings.DEBUG

# ============================================================================
# Middleware
# ============================================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.middleware("http")(request_logging_middleware)
app.middleware("http")(auto_free_space_middleware)
app.add_exception_handler(Exception, global_exception_handler)

# Log 422 validation errors to terminal so we can debug
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    logger.error(f"422 Validation Error on {request.method} {request.url.path}: {exc.errors()}")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

# ============================================================================
# Routes
# ============================================================================
app.include_router(api_router)


@app.get("/", tags=["Health"])
async def root():
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health", tags=["Health"])
async def health_check():
    db_ok = check_db_connection()
    data_db_ok = check_data_db_connection()
    all_ok = db_ok and data_db_ok
    return {
        "status": "healthy" if all_ok else "degraded",
        "system_db": "connected" if db_ok else "disconnected",
        "data_db": "connected" if data_db_ok else "disconnected",
        "version": settings.APP_VERSION,
        "environment": settings.APP_ENV,
    }


# ============================================================================
# Entry Point
# ============================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        workers=1 if settings.DEBUG else 4,
    )

# ============================================================================
# Static Frontend Serving (production)
# ============================================================================
import os
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir) and os.path.exists(os.path.join(static_dir, "index.html")):
    # Serve static assets (JS, CSS, images)
    app.mount("/assets", StaticFiles(directory=os.path.join(static_dir, "assets")), name="static-assets")
    
    # Catch-all: serve index.html for SPA routing (must be LAST)
    @app.get("/{full_path:path}", tags=["Frontend"], include_in_schema=False)
    async def serve_frontend(full_path: str):
        # Don't intercept API routes or docs
        if full_path.startswith(("api/", "docs", "redoc", "openapi", "health")):
            return
        file_path = os.path.join(static_dir, full_path)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            return FileResponse(file_path)
        return FileResponse(os.path.join(static_dir, "index.html"))
