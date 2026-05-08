"""
Pipeline API — Run Full Allocation Pipeline in Parallel
========================================================
Replaces 20 physical machines. Splits major categories into batches
and runs MSA calculation concurrently using a thread pool.

Endpoints:
  POST /pipeline/run          — Start parallel pipeline
  GET  /pipeline/status       — Check current run status
  GET  /pipeline/categories   — List available major categories
"""
import threading
from typing import Optional, List, Dict

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from loguru import logger

from app.security.dependencies import get_current_user
from app.models.rbac import User
from app.services.parallel_pipeline import (
    run_parallel_pipeline,
    get_current_run,
    _get_all_major_categories,
    _create_fresh_engine,
)

router = APIRouter(prefix="/pipeline", tags=["Pipeline"])

# Track if a pipeline is currently running
_pipeline_lock = threading.Lock()
_is_running = False


class PipelineRequest(BaseModel):
    date: str = Field(..., description="Date filter (YYYY-MM-DD)")
    slocs: List[str] = Field(..., description="SLOC codes to include")
    threshold: int = Field(25, description="Minimum allocation threshold")
    filters: Dict[str, List[str]] = Field(default_factory=dict, description="Additional column filters")
    max_workers: int = Field(6, ge=1, le=20, description="Parallel workers (default 6)")
    batch_size: int = Field(5, ge=1, le=20, description="Categories per batch (default 5)")
    clear_previous: bool = Field(True, description="Clear previous MSA results before running")


# ── Run Pipeline ─────────────────────────────────────────────────────────

def _run_in_background(req: PipelineRequest, username: str):
    """Background thread that runs the full pipeline."""
    global _is_running
    try:
        logger.info(f"Pipeline started by {username}: {req.dict()}")
        run_parallel_pipeline(
            date_filter=req.date,
            slocs=req.slocs,
            threshold=req.threshold,
            filters=req.filters,
            max_workers=req.max_workers,
            batch_size=req.batch_size,
            clear_previous=req.clear_previous,
            run_id=f"pipeline_{username}",
        )
    except Exception as e:
        logger.error(f"Pipeline background run failed: {e}")
    finally:
        _is_running = False


@router.post("/run")
def start_pipeline(
    req: PipelineRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Start the full MSA pipeline in parallel.
    
    This replaces running on 20 separate machines. The system:
    1. Gets all distinct major categories from the source data
    2. Splits them into batches (e.g., 5 categories per batch)
    3. Runs MSA calculation for each batch in parallel (e.g., 6 workers)
    4. Stores all results to ARS_MSA_TOTAL, ARS_MSA_GEN_ART, ARS_MSA_VAR_ART
    
    Monitor progress with GET /pipeline/status
    
    With 60 categories, 6 workers, batch_size=5: ~12 batches, 2 at a time = ~6 rounds
    If each batch takes ~10 min, total = ~60 min instead of 14 hours.
    """
    global _is_running

    if _is_running:
        current = get_current_run()
        return {
            "success": False,
            "message": "Pipeline is already running",
            "data": current.to_dict() if current else None,
        }

    _is_running = True

    # Run in background thread so the API responds immediately
    thread = threading.Thread(
        target=_run_in_background,
        args=(req, current_user.username),
        daemon=True,
    )
    thread.start()

    return {
        "success": True,
        "message": (
            f"Pipeline started. {req.max_workers} parallel workers, "
            f"batch_size={req.batch_size}. Monitor with GET /pipeline/status"
        ),
    }


# ── Check Status ─────────────────────────────────────────────────────────

@router.get("/status")
def pipeline_status(current_user: User = Depends(get_current_user)):
    """Get current pipeline run status with per-batch details."""
    current = get_current_run()
    if not current:
        return {
            "success": True,
            "data": None,
            "message": "No pipeline run found. Start one with POST /pipeline/run",
        }

    return {
        "success": True,
        "data": current.to_dict(),
        "is_running": _is_running,
    }


# ── List Categories ──────────────────────────────────────────────────────

@router.get("/categories")
def list_categories(current_user: User = Depends(get_current_user)):
    """List all available major categories from the source data."""
    try:
        engine = _create_fresh_engine()
        cats = _get_all_major_categories(engine)
        engine.dispose()

        return {
            "success": True,
            "data": {
                "categories": cats,
                "count": len(cats),
            },
        }
    except Exception as e:
        raise HTTPException(500, f"Failed to get categories: {str(e)[:200]}")
