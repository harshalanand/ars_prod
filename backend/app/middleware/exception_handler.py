"""
Global Exception Handler Middleware
"""
import traceback
from fastapi import Request, status
from fastapi.responses import JSONResponse
from loguru import logger


async def global_exception_handler(request: Request, exc: Exception):
    """Catch all unhandled exceptions and return structured error."""
    logger.error(f"Unhandled exception: {exc}\n{traceback.format_exc()}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "success": False,
            "message": "Internal server error",
            "errors": [str(exc)] if request.app.state.debug else ["An unexpected error occurred"],
        },
    )


async def request_logging_middleware(request: Request, call_next):
    """Log all incoming requests."""
    logger.info(f"{request.method} {request.url.path}")
    response = await call_next(request)
    logger.info(f"{request.method} {request.url.path} → {response.status_code}")
    return response


async def auto_free_space_middleware(request: Request, call_next):
    """
    Auto-cleanup hook: after a "heavy" endpoint returns 2xx/3xx, schedule a
    non-blocking post-job cleanup (CHECKPOINT + log shrink + tempdb truncate).
    The HTTP response is returned to the client first; the cleanup runs in a
    daemon thread with a cooldown so concurrent calls collapse to one run.

    Configurable via settings:
      - AUTO_FREE_AFTER_JOB     (master switch)
      - AUTO_FREE_PATHS         (substring match against request.url.path)
      - AUTO_FREE_METHODS       (only POST/PUT by default)
      - AUTO_FREE_COOLDOWN_SEC  (skip if a run happened recently)
    """
    response = await call_next(request)

    # Late import to avoid circulars at module load time
    try:
        from app.core.config import get_settings
        from app.services.tempdb_cleanup_service import tempdb_cleaner

        settings = get_settings()
        if not getattr(settings, "AUTO_FREE_AFTER_JOB", False):
            return response

        methods = set(getattr(settings, "AUTO_FREE_METHODS", ["POST", "PUT"]))
        if request.method not in methods:
            return response

        if not (200 <= response.status_code < 400):
            return response

        path = request.url.path
        triggers = getattr(settings, "AUTO_FREE_PATHS", [])
        if not any(t in path for t in triggers):
            return response

        scheduled = tempdb_cleaner.schedule_post_job_cleanup(reason=path)
        if scheduled:
            logger.info(f"auto-free scheduled after {request.method} {path}")
    except Exception as exc:
        # Never let cleanup scheduling break a successful response
        logger.warning(f"auto_free_space_middleware error (non-fatal): {exc}")

    return response
