"""
ARS Parallel Pipeline Service
==============================
Replaces 20 physical machines by running the full allocation pipeline
across all major categories in parallel on a single server.

Pipeline steps (per category batch):
  1. MSA Stock Calculation (9-step)
  2. Grid Builder calculations
  3. Listing generation
  4. BDC preparation

Instead of running sequentially (14 hours), this splits major categories
into batches and runs them concurrently using a thread pool.

Example: 60 major categories ÷ 10 parallel workers = 6 batches
         Each batch ~15 min = total ~90 min instead of 14 hours
"""
import time
import math
from datetime import datetime
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import text, create_engine
from loguru import logger

from app.core.config import get_settings
from app.services.msa_service import MSAService


settings = get_settings()


@dataclass
class BatchResult:
    """Result from processing one category batch."""
    batch_id: int
    categories: List[str]
    status: str = "pending"        # pending | running | completed | failed
    msa_rows: int = 0
    gen_art_rows: int = 0
    variant_rows: int = 0
    duration_sec: float = 0
    error: str = ""


@dataclass
class PipelineRun:
    """Tracks a full pipeline execution."""
    run_id: str = ""
    status: str = "pending"
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    total_categories: int = 0
    total_batches: int = 0
    completed_batches: int = 0
    failed_batches: int = 0
    total_msa_rows: int = 0
    total_gen_art_rows: int = 0
    total_variant_rows: int = 0
    batch_results: List[BatchResult] = field(default_factory=list)
    error: str = ""

    @property
    def duration_sec(self) -> float:
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        elif self.started_at:
            return (datetime.utcnow() - self.started_at).total_seconds()
        return 0

    @property
    def progress_pct(self) -> float:
        if self.total_batches == 0:
            return 0
        return round((self.completed_batches + self.failed_batches) / self.total_batches * 100, 1)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "progress_pct": self.progress_pct,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_sec": round(self.duration_sec, 1),
            "total_categories": self.total_categories,
            "total_batches": self.total_batches,
            "completed_batches": self.completed_batches,
            "failed_batches": self.failed_batches,
            "total_msa_rows": self.total_msa_rows,
            "total_gen_art_rows": self.total_gen_art_rows,
            "total_variant_rows": self.total_variant_rows,
            "batch_results": [
                {
                    "batch_id": b.batch_id,
                    "categories": b.categories,
                    "status": b.status,
                    "msa_rows": b.msa_rows,
                    "gen_art_rows": b.gen_art_rows,
                    "variant_rows": b.variant_rows,
                    "duration_sec": round(b.duration_sec, 1),
                    "error": b.error,
                }
                for b in self.batch_results
            ],
        }


# In-memory tracking of pipeline runs (latest run)
_current_run: Optional[PipelineRun] = None


def get_current_run() -> Optional[PipelineRun]:
    return _current_run


def _create_fresh_engine():
    """Create a new SQLAlchemy engine for worker threads.
    Each thread needs its own engine to avoid connection sharing issues."""
    return create_engine(
        settings.DATA_DATABASE_URL,
        pool_size=2,
        max_overflow=3,
        pool_recycle=300,
        pool_pre_ping=True,
        fast_executemany=True,
    )


def _get_all_major_categories(engine) -> List[str]:
    """Get all distinct MAJ_CAT values from MSA source data."""
    with engine.connect() as conn:
        # Try the MSA view first
        for table in ["VW_ET_MSA_STK_WITH_MASTER", "ET_MSA_STK"]:
            try:
                rows = conn.execute(text(
                    f"SELECT DISTINCT [MAJ_CAT] FROM [{table}] "
                    f"WHERE [MAJ_CAT] IS NOT NULL ORDER BY [MAJ_CAT]"
                )).fetchall()
                if rows:
                    cats = [r[0] for r in rows if r[0]]
                    logger.info(f"Found {len(cats)} major categories from {table}")
                    return cats
            except Exception:
                continue
    return []


def _process_category_batch(
    batch: BatchResult,
    date_filter: str,
    slocs: List[str],
    threshold: int,
    filters: Dict[str, List[str]],
) -> BatchResult:
    """
    Process a single batch of major categories through the MSA pipeline.
    Runs in a worker thread with its own DB connection.
    """
    batch.status = "running"
    start = time.time()

    try:
        # Each worker gets its own engine (thread-safe)
        worker_engine = _create_fresh_engine()

        from sqlalchemy.orm import Session, sessionmaker
        WorkerSession = sessionmaker(bind=worker_engine)
        db = WorkerSession()

        try:
            # Add MAJ_CAT filter to the existing filters
            batch_filters = dict(filters) if filters else {}
            batch_filters["MAJ_CAT"] = batch.categories

            # Run MSA calculation for this batch
            service = MSAService(db)
            df, _ = service.apply_filters(date_filter, batch_filters)

            if df.empty:
                logger.warning(f"Batch {batch.batch_id}: No data for categories {batch.categories}")
                batch.status = "completed"
                batch.duration_sec = time.time() - start
                return batch

            results = service.calculate(df, slocs, threshold)

            batch.msa_rows = results["row_counts"].get("msa", 0)
            batch.gen_art_rows = results["row_counts"].get("msa_gen_clr", 0)
            batch.variant_rows = results["row_counts"].get("msa_gen_clr_var", 0)

            # Store results to DB (append to ARS_MSA_* tables)
            _store_batch_results(db, worker_engine, results, batch)

            batch.status = "completed"
            logger.info(
                f"Batch {batch.batch_id} done: {batch.msa_rows} MSA rows, "
                f"{batch.gen_art_rows} gen-art rows in {time.time()-start:.1f}s "
                f"(categories: {batch.categories})"
            )

        finally:
            db.close()
            worker_engine.dispose()

    except Exception as e:
        batch.status = "failed"
        batch.error = str(e)[:500]
        logger.error(f"Batch {batch.batch_id} failed: {e}")

    batch.duration_sec = time.time() - start
    return batch


def _store_batch_results(db, engine, results: dict, batch: BatchResult):
    """Store MSA calculation results into the ARS_MSA_* tables.
    Appends to existing tables rather than recreating them."""
    try:
        for key, table_name in [
            ("msa", "ARS_MSA_TOTAL"),
            ("msa_gen_clr", "ARS_MSA_GEN_ART"),
            ("msa_gen_clr_var", "ARS_MSA_VAR_ART"),
        ]:
            data = results.get(key, [])
            if not data:
                continue

            df = pd.DataFrame(data)
            if df.empty:
                continue

            # Write to DB using pandas to_sql (append mode)
            df.to_sql(
                table_name,
                engine,
                if_exists="append",
                index=False,
                method="multi",
                chunksize=2000,
            )
            logger.debug(f"Stored {len(df)} rows to {table_name} for batch {batch.batch_id}")

    except Exception as e:
        logger.error(f"Error storing batch {batch.batch_id} results: {e}")
        raise


def run_parallel_pipeline(
    date_filter: str,
    slocs: List[str],
    threshold: int = 25,
    filters: Optional[Dict[str, List[str]]] = None,
    max_workers: int = 6,
    batch_size: int = 5,
    clear_previous: bool = True,
    run_id: str = "",
) -> PipelineRun:
    """
    Run the full MSA pipeline in parallel across all major categories.

    Args:
        date_filter: Date for MSA data (YYYY-MM-DD)
        slocs: List of SLOC codes to include
        threshold: Minimum allocation threshold
        filters: Additional column filters (excluding MAJ_CAT — handled automatically)
        max_workers: Number of parallel threads (default 6 — good for 4-8 CPU cores)
        batch_size: Number of major categories per batch (default 5)
        clear_previous: Drop and recreate result tables before running
        run_id: Optional identifier for this run

    Returns:
        PipelineRun with full status and batch details
    """
    global _current_run

    run = PipelineRun(
        run_id=run_id or f"run_{int(time.time())}",
        status="running",
        started_at=datetime.utcnow(),
    )
    _current_run = run

    try:
        # Get all major categories
        main_engine = _create_fresh_engine()
        all_categories = _get_all_major_categories(main_engine)
        run.total_categories = len(all_categories)

        if not all_categories:
            run.status = "failed"
            run.error = "No major categories found in source data"
            run.completed_at = datetime.utcnow()
            return run

        # Remove MAJ_CAT from filters if present (we handle it per batch)
        clean_filters = {k: v for k, v in (filters or {}).items() if k != "MAJ_CAT"}

        # Clear previous results if requested
        if clear_previous:
            with main_engine.connect() as conn:
                for tbl in ["ARS_MSA_TOTAL", "ARS_MSA_GEN_ART", "ARS_MSA_VAR_ART"]:
                    try:
                        conn.execute(text(f"IF OBJECT_ID('{tbl}','U') IS NOT NULL TRUNCATE TABLE [{tbl}]"))
                        conn.commit()
                        logger.info(f"Cleared {tbl}")
                    except Exception:
                        pass

        main_engine.dispose()

        # Split categories into batches
        batches = []
        for i in range(0, len(all_categories), batch_size):
            chunk = all_categories[i:i + batch_size]
            batches.append(BatchResult(
                batch_id=len(batches) + 1,
                categories=chunk,
            ))

        run.total_batches = len(batches)
        run.batch_results = batches

        logger.info(
            f"Pipeline {run.run_id}: {run.total_categories} categories → "
            f"{run.total_batches} batches × {batch_size} cats, "
            f"{max_workers} parallel workers"
        )

        # Run batches in parallel
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    _process_category_batch,
                    batch,
                    date_filter,
                    slocs,
                    threshold,
                    clean_filters,
                ): batch
                for batch in batches
            }

            for future in as_completed(futures):
                result = future.result()
                if result.status == "completed":
                    run.completed_batches += 1
                    run.total_msa_rows += result.msa_rows
                    run.total_gen_art_rows += result.gen_art_rows
                    run.total_variant_rows += result.variant_rows
                else:
                    run.failed_batches += 1

                logger.info(
                    f"Pipeline progress: {run.completed_batches + run.failed_batches}"
                    f"/{run.total_batches} batches "
                    f"({run.progress_pct}%)"
                )

        # Final status
        if run.failed_batches == 0:
            run.status = "completed"
        elif run.completed_batches > 0:
            run.status = "partial"
        else:
            run.status = "failed"

        run.completed_at = datetime.utcnow()

        logger.info(
            f"Pipeline {run.run_id} {run.status}: "
            f"{run.total_msa_rows} MSA rows, "
            f"{run.total_gen_art_rows} gen-art rows, "
            f"{run.total_variant_rows} variant rows "
            f"in {run.duration_sec:.0f}s"
        )

    except Exception as e:
        run.status = "failed"
        run.error = str(e)[:500]
        run.completed_at = datetime.utcnow()
        logger.error(f"Pipeline {run.run_id} failed: {e}")

    return run
