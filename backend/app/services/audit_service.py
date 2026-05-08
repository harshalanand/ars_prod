"""
Row-Level Audit Service
========================
Background async audit logging that doesn't slow down main operations.

Features:
- Queue-based design for async processing
- Batch writes for efficiency  
- Thread-safe operations
- Automatic cleanup of old logs

Usage:
    from app.services.audit_service import audit_queue
    
    # For single row changes (UI edits)
    audit_queue.log_change(
        table_name="employees",
        action_type="UPDATE",
        record_key={"id": 123},
        changes={"salary": {"old": 50000, "new": 55000}},
        changed_by="john.doe",
        source="UI"
    )
    
    # For bulk changes (async, doesn't block)
    audit_queue.log_bulk_changes(
        table_name="products",
        batch_id="UPL_abc123",
        row_changes=[...],
        changed_by="admin",
        source="UPLOAD"
    )
"""
import json
import threading
import queue
import time
from datetime import datetime
from typing import Dict, List, Optional, Any
from loguru import logger


class AuditQueue:
    """
    Thread-safe queue for async audit logging.
    Collects changes and writes them in batches to avoid slowing down operations.
    """
    
    def __init__(self, batch_size: int = 100, flush_interval: float = 2.0):
        self._queue = queue.Queue()
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
    
    def start(self):
        """Start the background processing thread."""
        if self._running:
            return
            
        self._running = True
        self._thread = threading.Thread(target=self._process_loop, daemon=True)
        self._thread.start()
        logger.info("Audit queue started")
    
    def stop(self):
        """Stop the background processing thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
        logger.info("Audit queue stopped")
    
    def log_change(
        self,
        table_name: str,
        action_type: str,
        record_key: Dict[str, Any],
        changes: Dict[str, Dict[str, Any]],  # {column: {old: x, new: y}}
        changed_by: str,
        source: str = "UI",
        audit_log_id: Optional[int] = None,
        batch_id: Optional[str] = None,
    ):
        """
        Log a single row change. For UI edits, single-row API updates.
        Changes dict format: {"column_name": {"old": old_value, "new": new_value, "type": data_type}}
        """
        entry = {
            "table_name": table_name,
            "action_type": action_type,
            "record_key": json.dumps(record_key) if isinstance(record_key, dict) else str(record_key),
            "changes": changes,
            "changed_by": changed_by,
            "source": source,
            "audit_log_id": audit_log_id,
            "batch_id": batch_id,
            "timestamp": datetime.utcnow(),
        }
        self._queue.put(entry)
    
    def log_bulk_changes(
        self,
        table_name: str,
        batch_id: str,
        row_changes: List[Dict[str, Any]],
        changed_by: str,
        source: str = "UPLOAD",
        audit_log_id: Optional[int] = None,
    ):
        """
        Log multiple row changes from bulk operations.
        Processes async to avoid slowing down uploads.
        
        Each row_change should have:
        {
            "action_type": "INSERT" | "UPDATE" | "DELETE",
            "record_key": {...},  # Primary key values
            "changes": {"col": {"old": x, "new": y}} | None,
            "row_index": int  # Optional row number
        }
        """
        for i, change in enumerate(row_changes):
            entry = {
                "table_name": table_name,
                "action_type": change.get("action_type", "UPDATE"),
                "record_key": json.dumps(change.get("record_key", {})),
                "changes": change.get("changes", {}),
                "changed_by": changed_by,
                "source": source,
                "audit_log_id": audit_log_id,
                "batch_id": batch_id,
                "row_index": change.get("row_index", i),
                "timestamp": datetime.utcnow(),
            }
            self._queue.put(entry)
    
    def _process_loop(self):
        """Background thread loop for processing queued audit entries."""
        batch = []
        last_flush = time.time()
        
        while self._running or not self._queue.empty():
            try:
                # Get items from queue with timeout
                try:
                    entry = self._queue.get(timeout=0.5)
                    batch.append(entry)
                except queue.Empty:
                    pass
                
                # Flush if batch is full or interval elapsed
                now = time.time()
                should_flush = (
                    len(batch) >= self._batch_size or
                    (batch and now - last_flush >= self._flush_interval)
                )
                
                if should_flush and batch:
                    self._flush_batch(batch)
                    batch = []
                    last_flush = now
                    
            except Exception as e:
                logger.error(f"Audit queue processing error: {e}")
                time.sleep(1)
        
        # Final flush on shutdown
        if batch:
            self._flush_batch(batch)
    
    def _flush_batch(self, batch: List[Dict]):
        """Write a batch of audit entries to the database."""
        if not batch:
            return

        try:
            # Always use the central system engine — it's built from the
            # connection settings saved by the UI (app_settings.json) and
            # forces TCP. Building a one-off pyodbc string here would silently
            # bypass the UI-configured database.
            from app.database.session import system_engine

            conn = system_engine.raw_connection()
            cursor = conn.cursor()
            cursor.fast_executemany = True
            
            # Prepare rows for data_change_log table
            insert_sql = """
                INSERT INTO data_change_log (
                    audit_log_id, table_name, action_type, record_key,
                    column_name, old_value, new_value, data_type,
                    changed_by, changed_at, source, batch_id, row_index
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            
            rows = []
            for entry in batch:
                changes = entry.get("changes", {})
                
                if changes:
                    # One row per changed column
                    for col_name, col_change in changes.items():
                        old_val = col_change.get("old")
                        new_val = col_change.get("new")
                        data_type = col_change.get("type", "")
                        
                        rows.append((
                            entry.get("audit_log_id"),
                            entry["table_name"],
                            entry["action_type"],
                            entry["record_key"],
                            col_name,
                            str(old_val)[:4000] if old_val is not None else None,
                            str(new_val)[:4000] if new_val is not None else None,
                            data_type,
                            entry["changed_by"],
                            entry["timestamp"],
                            entry["source"],
                            entry.get("batch_id"),
                            entry.get("row_index"),
                        ))
                else:
                    # For INSERT/DELETE without column-level changes
                    rows.append((
                        entry.get("audit_log_id"),
                        entry["table_name"],
                        entry["action_type"],
                        entry["record_key"],
                        None,  # column_name
                        None,  # old_value
                        None,  # new_value
                        None,  # data_type
                        entry["changed_by"],
                        entry["timestamp"],
                        entry["source"],
                        entry.get("batch_id"),
                        entry.get("row_index"),
                    ))
            
            if rows:
                cursor.executemany(insert_sql, rows)
                conn.commit()
                logger.debug(f"Flushed {len(rows)} audit entries to data_change_log")
            
            cursor.close()
            conn.close()
            
        except Exception as e:
            logger.error(f"Failed to flush audit batch: {e}")


# Global queue instance - auto-starts on first use
_audit_queue: Optional[AuditQueue] = None
_queue_lock = threading.Lock()


def get_audit_queue() -> AuditQueue:
    """Get the global audit queue, creating and starting it if needed."""
    global _audit_queue
    
    if _audit_queue is None:
        with _queue_lock:
            if _audit_queue is None:
                _audit_queue = AuditQueue(batch_size=100, flush_interval=2.0)
                _audit_queue.start()
    
    return _audit_queue


# Convenience functions
def log_row_change(
    table_name: str,
    action_type: str,
    record_key: Dict[str, Any],
    changes: Dict[str, Dict[str, Any]],
    changed_by: str,
    source: str = "UI",
    audit_log_id: Optional[int] = None,
):
    """Log a single row change asynchronously."""
    get_audit_queue().log_change(
        table_name=table_name,
        action_type=action_type,
        record_key=record_key,
        changes=changes,
        changed_by=changed_by,
        source=source,
        audit_log_id=audit_log_id,
    )


def log_bulk_changes(
    table_name: str,
    batch_id: str,
    row_changes: List[Dict[str, Any]],
    changed_by: str,
    source: str = "UPLOAD",
):
    """Log bulk changes asynchronously."""
    get_audit_queue().log_bulk_changes(
        table_name=table_name,
        batch_id=batch_id,
        row_changes=row_changes,
        changed_by=changed_by,
        source=source,
    )
