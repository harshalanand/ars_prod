r"""
Data Export Service — export stored-procedure results to a session folder.

Given a session code and a list of stored procedures, this service:
  1. Creates a `data` folder inside the desired base folder (default:
     backend/exports).
  2. Creates a sub-folder named after the session code:
         <base_dir>/data/<session_code>/
  3. Executes each stored procedure against the Data DB (Rep_data) and
     writes every result set it returns to a CSV file inside that folder:
         <proc_name>.csv            (single result set)
         <proc_name>_rs2.csv, ...   (additional result sets)

Usage:
    from app.services.data_export_service import export_session_data

    result = export_session_data(
        session_code="20260709_141530_123",
        procedures=[
            "dbo.sp_AutoContCompute",                               # no params
            {"name": "dbo.usp_ars_allocate_majcat",                 # with params
             "params": {"WERKS": "V201", "MAJ_CAT": "SHIRTS"}},
        ],
        base_dir=r"D:\exports",       # optional — the "desired folder"
    )
"""
import csv
import os
import re
import shutil
import tempfile
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Union

import pandas as pd
from loguru import logger
from sqlalchemy import text

from app.database.session import get_data_engine

# Default base folder mirrors export_job_service: backend/exports
DEFAULT_BASE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "exports"
)

# Stored-proc identifiers: optional schema prefix, then a normal SQL identifier.
_PROC_NAME_RE = re.compile(r"^(?:\[?[A-Za-z0-9_]+\]?\.)?\[?[A-Za-z0-9_]+\]?$")
_PARAM_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _safe_folder_name(value: str, max_length: int = 80) -> str:
    """Make a session code safe to use as a folder name."""
    cleaned = re.sub(r'[<>:"/\\|?*]', "-", str(value).strip())
    cleaned = "_".join(cleaned.split()) or "UNNAMED"
    return cleaned[:max_length]


def _proc_base_name(proc_name: str) -> str:
    """dbo.usp_ars_allocate_majcat -> usp_ars_allocate_majcat"""
    return proc_name.split(".")[-1].strip("[]")


def _normalize_procedures(
    procedures: List[Union[str, Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """Accept 'dbo.proc' strings or {'name':..., 'params': {...}} dicts."""
    normalized = []
    for entry in procedures:
        if isinstance(entry, str):
            entry = {"name": entry, "params": {}}
        name = str(entry.get("name", "")).strip()
        params = entry.get("params") or {}
        if not _PROC_NAME_RE.match(name):
            raise ValueError(f"Invalid stored procedure name: {name!r}")
        for pname in params:
            if not _PARAM_NAME_RE.match(str(pname)):
                raise ValueError(f"Invalid parameter name: {pname!r} for {name}")
        normalized.append({"name": name, "params": params})
    return normalized


def _run_sql_all_resultsets(
    sql: str, values=None, cancel_token=None
) -> List[pd.DataFrame]:
    """Execute arbitrary SQL and collect EVERY result set it returns.

    Uses a raw DBAPI connection + pyodbc nextset() so it works for multi-
    statement batches (DECLARE / SET / assignment-SELECT / EXEC sp_executesql)
    where the real SELECT is not the first thing the batch does. Result sets
    with no columns (DML counts, variable assignments) are skipped. When a
    cancel_token is given, its cursor is bound so a cancel interrupts the query.
    """
    engine = get_data_engine()
    raw = engine.raw_connection()
    frames: List[pd.DataFrame] = []
    try:
        cursor = raw.cursor()
        if cancel_token is not None:
            cancel_token.bind_cursor(cursor)
        try:
            if values:
                cursor.execute(sql, values)
            else:
                cursor.execute(sql)
            while True:
                if cursor.description:  # None for DML / assignment statements
                    columns = [col[0] for col in cursor.description]
                    rows = cursor.fetchall()
                    frames.append(
                        pd.DataFrame([tuple(r) for r in rows], columns=columns)
                    )
                if not cursor.nextset():
                    break
        finally:
            if cancel_token is not None:
                cancel_token.unbind_cursor()
            cursor.close()
        raw.commit()
    finally:
        raw.close()
    return frames


def _run_proc_all_resultsets(
    proc_name: str, params: Dict[str, Any], cancel_token=None
) -> List[pd.DataFrame]:
    """EXEC a stored procedure and collect every result set it returns."""
    if params:
        placeholders = ", ".join(f"@{p} = ?" for p in params)
        sql = f"EXEC {proc_name} {placeholders}"
        values = list(params.values())
    else:
        sql = f"EXEC {proc_name}"
        values = None
    return _run_sql_all_resultsets(sql, values, cancel_token)


def make_session_dir(session_code: str, base_dir: Optional[str] = None,
                     per_run: bool = True) -> str:
    """Return the export folder, creating it (and its parents) if missing.

    Always nests under a date folder (YYYYMMDD, today's run date):
      per_run=True  -> <base_dir>/data/<YYYYMMDD>/<session_code>/  (fresh each run)
      per_run=False -> <base_dir>/data/<YYYYMMDD>/                 (one folder per
                       day, reused; files land straight in it)
    """
    date_str = datetime.now().strftime("%Y%m%d")
    date_dir = os.path.join(base_dir or DEFAULT_BASE_DIR, "data", date_str)
    if per_run:
        if not session_code or not str(session_code).strip():
            raise ValueError("session_code is required")
        export_dir = os.path.join(date_dir, _safe_folder_name(session_code))
    else:
        export_dir = date_dir
    os.makedirs(export_dir, exist_ok=True)
    return export_dir


# ── Storage guardrails (retention cleanup + disk pre-flight) ──────────────────
_DATE_DIR_RE = re.compile(r"^\d{8}$")   # YYYYMMDD run-date folders


def free_space_mb(path: str) -> Optional[float]:
    """Free space (MB) on the volume/share holding `path`, or None if unknown."""
    try:
        return shutil.disk_usage(path).free / (1024 * 1024)
    except Exception:
        return None


def assert_free_space(path: str, min_free_mb: Optional[float]) -> None:
    """Raise if the volume holding `path` has less than `min_free_mb` free.
    A None/0 threshold, or an unreadable volume, skips the check."""
    if not min_free_mb or min_free_mb <= 0:
        return
    free = free_space_mb(path)
    if free is not None and free < min_free_mb:
        raise RuntimeError(
            f"insufficient disk space at {path}: {free:,.0f} MB free, need ≥ "
            f"{min_free_mb:,.0f} MB — free space on the output drive/share, or "
            f"lower the retention window so old runs are pruned")


def cleanup_old_run_folders(base_dir: Optional[str], retention_days: Optional[int],
                            keep_today: bool = True) -> Dict[str, Any]:
    """Delete `<base_dir>/data/<YYYYMMDD>` run-date folders older than
    `retention_days`, so the output location doesn't fill up over time. Only
    touches 8-digit date folders under the report's own `data` directory; never
    raises. Returns {"deleted": [...], "freed_mb": float, "errors": [...]}."""
    result: Dict[str, Any] = {"deleted": [], "kept": 0, "errors": []}
    if not base_dir or not retention_days or retention_days <= 0:
        return result
    data_root = os.path.join(base_dir, "data")
    if not os.path.isdir(data_root):
        return result
    cutoff = (datetime.now() - timedelta(days=int(retention_days))).strftime("%Y%m%d")
    today = datetime.now().strftime("%Y%m%d")
    try:
        entries = os.listdir(data_root)
    except Exception as e:
        result["errors"].append(str(e))
        return result
    for name in entries:
        if not _DATE_DIR_RE.match(name):
            continue                      # ignore anything not a YYYYMMDD folder
        if (keep_today and name == today) or name >= cutoff:
            result["kept"] += 1           # within the retention window — keep
            continue                      # (lexicographic == chronological for YYYYMMDD)
        p = os.path.join(data_root, name)
        if not os.path.isdir(p):
            continue
        try:
            shutil.rmtree(p)
            result["deleted"].append(name)
        except Exception as e:
            result["errors"].append(f"{name}: {e}")
    return result


# Split defaults mirror the existing Export page (export_job_service).
_PRODUCT_HIERARCHY = ["SEG", "DIV", "SUB_DIV", "MAJ_CAT"]
_STORE_HIERARCHY = ["ZONE", "REG", "STORE"]


def _resolve_split_columns(method: str, product_hier, store_hier,
                           df_cols: List[str]) -> List[str]:
    """Which columns to group by, keeping only those present in the data."""
    if str(method).lower() == "store":
        preferred = store_hier or _STORE_HIERARCHY
    else:
        preferred = product_hier or _PRODUCT_HIERARCHY
    available = set(df_cols)
    return [c for c in preferred if c in available]


def _write_frame(df, export_dir: str, base_name: str, file_format: str,
                 procedure: str) -> Dict[str, Any]:
    file_path = os.path.join(export_dir, f"{base_name}.{file_format}")
    if file_format == "xlsx":
        df.to_excel(file_path, index=False)
    else:
        df.to_csv(file_path, index=False)
    logger.info(f"[data_export] {procedure} -> {file_path} ({len(df)} rows)")
    return {"procedure": procedure, "file": file_path, "rows": len(df)}


def write_frame_split(df, export_dir: str, base_name: str, file_format: str,
                      procedure: str,
                      split_config: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Write a DataFrame to one or many files per split_config.

    split_config = {enabled, method('product'|'store'), max_rows,
                    product_hierarchy[], store_hierarchy[]}. Same behavior as the
    Export page: splitting only kicks in when the data EXCEEDS max_rows. At or
    under the limit it's always a single file. Above it, group by the hierarchy
    columns (one file per group), and add _partN when a group still exceeds
    max_rows. Method 'none' (or disabled) splits by size only.
    """
    sc = split_config or {}
    enabled = bool(sc.get("enabled"))
    method = str(sc.get("method", "product")).lower()
    try:
        max_rows = int(sc.get("max_rows") or 1000000)
    except (TypeError, ValueError):
        max_rows = 1000000
    max_rows = max(1, max_rows)

    written: List[Dict[str, Any]] = []

    def size_split(frame, label_base):
        rows = len(frame)
        chunks = max(1, (rows + max_rows - 1) // max_rows)
        frame = frame.reset_index(drop=True)
        for ci in range(chunks):
            part = frame.iloc[ci * max_rows: min((ci + 1) * max_rows, rows)]
            name = label_base if chunks == 1 else f"{label_base}_part{ci + 1}"
            written.append(_write_frame(part, export_dir,
                                        _safe_folder_name(name, 180),
                                        file_format, procedure))

    # Split OFF → one file, whatever the size (unchecking "Split output into
    # multiple files" means don't split at all).
    if not enabled:
        return [_write_frame(df, export_dir, _safe_folder_name(base_name, 180),
                             file_format, procedure)]
    # Split ON but the data is WITHIN max_rows, or method is size-only ('none') →
    # size_split writes a single file when len <= max_rows, else row-count parts.
    if len(df) <= max_rows or method not in ("product", "store"):
        size_split(df, base_name)
        return written

    hierarchy = (sc.get("product_hierarchy") if method == "product"
                 else sc.get("store_hierarchy")) or (
                 _PRODUCT_HIERARCHY if method == "product" else _STORE_HIERARCHY)

    # Progressive (adaptive) hierarchy split: use the COARSEST level that keeps
    # each file within max_rows. The whole frame is already over the limit here,
    # so split by the first level; any group STILL over the limit is split by
    # the next level down, and so on. A group that fits becomes one file at that
    # level. If the deepest level is still too big, size-chunk it into _partN.
    def _rec(frame, levels, label):
        if len(frame) <= max_rows or not levels:
            size_split(frame, f"{base_name}_{label}" if label else base_name)
            return
        col = levels[0]
        if col not in frame.columns:
            _rec(frame, levels[1:], label)          # level absent — try the next
            return
        for val, grp in frame.groupby(col, dropna=False, sort=False):
            piece = f"{col}-{_safe_folder_name(str(val), 40)}"
            _rec(grp, levels[1:], f"{label}_{piece}" if label else piece)

    _rec(df, list(hierarchy), "")
    return written


# ── Memory-safe streaming export ─────────────────────────────────────────────
# A stored proc / query can return millions of rows (report 70's USP_PARK_TEMP
# returned ~4.5M × 100 cols). Building one pandas object-dtype DataFrame of that
# needs a single contiguous 3+ GiB block → "Unable to allocate X GiB" MemoryError
# — the crash we're fixing. So we PEEK the first _INMEM_ROW_CAP rows: if the whole
# result set fits, we keep the original in-memory path (full hierarchy split);
# if it overflows, we STREAM to disk in batches (constant memory, split into
# _partN files by max_rows) instead of ever materialising the whole thing.
_INMEM_ROW_CAP = 250_000          # ~250k wide rows ≈ 190 MB — safe to hold in RAM
_STREAM_CHUNK = 50_000            # fetchmany batch size while streaming
_RS_TOKEN = "--RSSLOT{}--"        # per-result-set placeholder, resolved at the end
_UNLIMITED = 10 ** 15             # effectively-infinite part size (split OFF → 1 file)


def _resolve_max_rows(split_config: Optional[Dict[str, Any]]) -> int:
    try:
        mr = int((split_config or {}).get("max_rows") or 1000000)
    except (TypeError, ValueError):
        mr = 1000000
    return max(1, mr)


def _stream_resultset_to_csv(cursor, columns, export_dir, base, max_rows,
                             procedure, prebuffered) -> List[Dict[str, Any]]:
    """Stream a forward-only cursor result set straight to CSV part files —
    never holding more than one fetch batch in memory. Rolls to `_partN` every
    `max_rows` rows. `prebuffered` are rows already read during the size peek."""
    import csv
    written: List[Dict[str, Any]] = []
    state = {"fh": None, "w": None, "rows": 0, "part": 0}

    def _open_part():
        state["part"] += 1
        path = os.path.join(
            export_dir, _safe_folder_name(f"{base}_part{state['part']}", 180) + ".csv")
        fh = open(path, "w", newline="", encoding="utf-8")
        w = csv.writer(fh)
        w.writerow(columns)
        state.update(fh=fh, w=w, rows=0)
        written.append({"procedure": procedure, "file": path, "rows": 0})

    def _write(rows):
        i, n = 0, len(rows)
        while i < n:
            if state["w"] is None or state["rows"] >= max_rows:
                if state["fh"]:
                    state["fh"].close()
                _open_part()
            take = rows[i:i + (max_rows - state["rows"])]
            state["w"].writerows(take)        # pyodbc.Row is a sequence — no tuple() copy
            state["rows"] += len(take)
            written[-1]["rows"] += len(take)
            i += len(take)

    try:
        if prebuffered:
            _write(prebuffered)
        while True:
            batch = cursor.fetchmany(_STREAM_CHUNK)
            if not batch:
                break
            _write(batch)
    finally:
        if state["fh"]:
            state["fh"].close()

    # One part only → drop the `_part1` suffix to match single-file naming.
    if state["part"] == 1 and written:
        old = written[0]["file"]
        new = os.path.join(export_dir, _safe_folder_name(base, 180) + ".csv")
        if old != new:
            os.replace(old, new)
            written[0]["file"] = new
    logger.info(f"[data_export] {procedure} streamed {sum(w['rows'] for w in written)} "
                f"rows -> {len(written)} CSV file(s)")
    return written


def _stream_resultset_to_xlsx(cursor, columns, export_dir, base, max_rows,
                              procedure, prebuffered) -> List[Dict[str, Any]]:
    """xlsx variant — accumulates at most one part (capped at the 1,048,576-row
    sheet limit) before writing it, so memory stays bounded to a single part."""
    part_cap = min(max_rows, 1_000_000)
    written: List[Dict[str, Any]] = []
    buf: List[Any] = list(prebuffered or [])
    part = {"n": 0}

    def _flush(frame_rows):
        part["n"] += 1
        df = pd.DataFrame([tuple(r) for r in frame_rows], columns=columns)
        path = os.path.join(
            export_dir, _safe_folder_name(f"{base}_part{part['n']}", 180) + ".xlsx")
        df.to_excel(path, index=False)
        written.append({"procedure": procedure, "file": path, "rows": len(frame_rows)})

    while len(buf) >= part_cap:
        _flush(buf[:part_cap]); buf = buf[part_cap:]
    while True:
        batch = cursor.fetchmany(_STREAM_CHUNK)
        if not batch:
            break
        buf.extend(batch)
        while len(buf) >= part_cap:
            _flush(buf[:part_cap]); buf = buf[part_cap:]
    if buf or not written:
        _flush(buf)

    if part["n"] == 1 and written:
        old = written[0]["file"]
        new = os.path.join(export_dir, _safe_folder_name(base, 180) + ".xlsx")
        if old != new:
            os.replace(old, new)
            written[0]["file"] = new
    return written


class _GroupedCsvWriter:
    """Routes streamed rows into per-GROUP CSV files (one file per hierarchy
    group, e.g. `<base>_SEG-APP_DIV-MENS.csv`), so a large export is split by the
    product/store LOGIC, not by arbitrary row-count. Open file handles are capped
    (LRU, reopened in append mode) so group count never exhausts OS handles.
    A group flagged `oversized` (a leaf that alone exceeds max_rows) rolls to
    `_partN` within itself."""

    def __init__(self, export_dir, base, columns, levels, max_rows, procedure,
                 oversized, max_open=200):
        self.export_dir, self.base, self.columns = export_dir, base, columns
        self.levels, self.max_rows, self.procedure = levels, max_rows, procedure
        self.oversized = oversized
        self.max_open = max_open
        self._open: "OrderedDict[tuple, tuple]" = OrderedDict()  # label -> (fh, writer)
        self._state: Dict[tuple, Dict[str, Any]] = {}            # label -> part/rows/cur_path
        self.written: List[Dict[str, Any]] = []
        self._idx_by_path: Dict[str, int] = {}

    def _label_base(self, label):
        parts = "_".join(f"{self.levels[i]}-{_safe_folder_name(str(label[i]), 40)}"
                         for i in range(len(label)))
        return f"{self.base}_{parts}" if parts else self.base

    def _writer(self, label):
        st = self._state.get(label)
        if st is None:
            st = {"part": 1, "rows": 0, "cur_path": None}
            self._state[label] = st
        # Oversized leaf that filled a part → roll to the next part file.
        if label in self.oversized and st["rows"] >= self.max_rows:
            if label in self._open:
                self._open.pop(label)[0].close()
            st["part"] += 1
            st["rows"] = 0
            st["cur_path"] = None
        if label in self._open:
            self._open.move_to_end(label)
            return st, self._open[label][1]
        # Evict least-recently-used handle if at the cap (file stays, reopened later).
        while len(self._open) >= self.max_open:
            _, (ofh, _) = self._open.popitem(last=False)
            ofh.close()
        if st["cur_path"] is None:
            namebase = self._label_base(label)
            if label in self.oversized:
                namebase = f"{namebase}_part{st['part']}"
            st["cur_path"] = os.path.join(
                self.export_dir, _safe_folder_name(namebase, 190) + ".csv")
        path = st["cur_path"]
        first_time = path not in self._idx_by_path
        fh = open(path, "w" if first_time else "a", newline="", encoding="utf-8")
        w = csv.writer(fh)
        if first_time:
            w.writerow(self.columns)
            self._idx_by_path[path] = len(self.written)
            self.written.append({"procedure": self.procedure, "file": path, "rows": 0})
        self._open[label] = (fh, w)
        return st, w

    def write_row(self, row, label):
        st, w = self._writer(label)
        w.writerow(row)
        st["rows"] += 1
        self.written[self._idx_by_path[st["cur_path"]]]["rows"] += 1

    def close(self):
        for fh, _ in self._open.values():
            try:
                fh.close()
            except Exception:
                pass
        self._open.clear()


def _stream_grouped_split(cursor, columns, export_dir, base, split_config,
                          max_rows, procedure, prebuffered) -> List[Dict[str, Any]]:
    """Split a large result set by the product/store hierarchy (adaptive: the
    coarsest level that keeps each file ≤ max_rows), memory-bounded via a local
    temp spill. This restores the Export-page split LOGIC for data too big to
    hold in memory (the streaming path previously fell back to row-count parts).
    Falls back to row-count parts if the data has none of the hierarchy columns."""
    sc = split_config or {}
    method = str(sc.get("method", "product")).lower()
    levels = _resolve_split_columns(
        method, sc.get("product_hierarchy"), sc.get("store_hierarchy"), columns)
    if not levels:
        return _stream_resultset_to_csv(cursor, columns, export_dir, base,
                                        max_rows, procedure, prebuffered)
    idxs = [columns.index(c) for c in levels]

    def _cell(v):
        return "" if v is None else str(v)   # match csv.writer's stringification

    # ── Pass 1: spill every row to a LOCAL temp CSV + count full-hierarchy groups
    spill_fd, spill_path = tempfile.mkstemp(prefix="arsspill_", suffix=".csv")
    leaf_counts: Dict[tuple, int] = {}
    try:
        with os.fdopen(spill_fd, "w", newline="", encoding="utf-8") as sf:
            sw = csv.writer(sf)

            def _pass1(rows):
                for r in rows:
                    k = tuple(_cell(r[i]) for i in idxs)
                    leaf_counts[k] = leaf_counts.get(k, 0) + 1
                sw.writerows(rows)

            if prebuffered:
                _pass1(prebuffered)
            while True:
                batch = cursor.fetchmany(_STREAM_CHUNK)
                if not batch:
                    break
                _pass1(batch)

        total = sum(leaf_counts.values())
        # ── Split ONLY when the total EXCEEDS "Max rows per file". A result set
        # within the limit is a SINGLE file, no hierarchy split — even though it
        # was big enough to stream. (The 250k stream threshold is a memory limit,
        # NOT the split threshold; only max_rows decides splitting.)
        if total <= max_rows:
            single = os.path.join(export_dir, _safe_folder_name(base, 190) + ".csv")
            with open(spill_path, "r", newline="", encoding="utf-8") as sf, \
                    open(single, "w", newline="", encoding="utf-8") as out:
                ow = csv.writer(out)
                ow.writerow(columns)
                for row in csv.reader(sf):
                    ow.writerow(row)
            logger.info(f"[data_export] {procedure} {total:,} rows ≤ max_rows "
                        f"({max_rows:,}) → single file (no split)")
            return [{"procedure": procedure, "file": single, "rows": total}]

        # ── Total exceeds max_rows → adaptive hierarchy split: coarsest prefix
        #    whose subtree ≤ max_rows.
        subtree: Dict[tuple, int] = {}
        for k, cnt in leaf_counts.items():
            for d in range(1, len(k) + 1):
                subtree[k[:d]] = subtree.get(k[:d], 0) + cnt

        def _label(k):
            for d in range(1, len(k) + 1):
                if subtree[k[:d]] <= max_rows:
                    return k[:d]
            return k  # even the leaf exceeds max_rows → its own group, rolls _partN

        label_of = {k: _label(k) for k in leaf_counts}
        oversized = {lab for lab in set(label_of.values())
                     if subtree.get(lab, 0) > max_rows}

        # ── Pass 2: re-read the spill and route each row to its group's file
        gw = _GroupedCsvWriter(export_dir, base, columns, levels, max_rows,
                               procedure, oversized)
        try:
            with open(spill_path, "r", newline="", encoding="utf-8") as sf:
                for row in csv.reader(sf):
                    k = tuple(row[i] for i in idxs)
                    gw.write_row(row, label_of.get(k, k))
        finally:
            gw.close()
    finally:
        try:
            os.remove(spill_path)
        except OSError:
            pass

    logger.info(f"[data_export] {procedure} split by {levels} -> {len(gw.written)} "
                f"file(s) across {len(set(label_of.values()))} group(s)")
    return gw.written


def _export_one_resultset(cursor, columns, export_dir, rs_base, file_format,
                          split_config, max_rows, procedure) -> List[Dict[str, Any]]:
    """Peek up to _INMEM_ROW_CAP rows. If the whole result set fits, use the
    in-memory writer (full hierarchy split). Otherwise stream to disk — honouring
    the product/store split LOGIC for CSV, or row-count parts as a fallback."""
    buf: List[Any] = []
    while len(buf) < _INMEM_ROW_CAP:
        batch = cursor.fetchmany(_STREAM_CHUNK)
        if not batch:
            # Whole result set fits in memory → original full-featured path.
            df = pd.DataFrame([tuple(r) for r in buf], columns=columns)
            return write_frame_split(df, export_dir, rs_base, file_format,
                                     procedure, split_config)
        buf.extend(batch)

    # The 250k peek is a MEMORY threshold (stream vs hold in RAM) — it is NOT the
    # split threshold. Splitting is governed solely by the split config + max_rows:
    #   split OFF                 → one file, no split (stream all to a single file)
    #   split ON, product/store   → adaptive hierarchy split (single file if the
    #                               total is within max_rows — decided inside)
    #   split ON, 'none'          → size split by max_rows (single file if within)
    sc = split_config or {}
    enabled = bool(sc.get("enabled"))
    method = str(sc.get("method", "")).lower()

    if file_format == "xlsx":
        # xlsx is always bounded by the 1,048,576-row sheet limit; when split is
        # off we still cap at that limit only.
        cap = max_rows if enabled else 1_000_000
        logger.warning(f"[data_export] {procedure}: >{_INMEM_ROW_CAP:,} rows — streaming xlsx")
        return _stream_resultset_to_xlsx(cursor, columns, export_dir, rs_base,
                                         cap, procedure, buf)

    if enabled and method in ("product", "store"):
        logger.warning(f"[data_export] {procedure}: >{_INMEM_ROW_CAP:,} rows — "
                       f"streaming (split by hierarchy when > max_rows={max_rows:,})")
        return _stream_grouped_split(cursor, columns, export_dir, rs_base,
                                     split_config, max_rows, procedure, buf)
    if enabled:
        logger.warning(f"[data_export] {procedure}: >{_INMEM_ROW_CAP:,} rows — "
                       f"streaming (size split at max_rows={max_rows:,})")
        return _stream_resultset_to_csv(cursor, columns, export_dir, rs_base,
                                        max_rows, procedure, buf)
    # Split OFF → single file, no chunking (an unlimited part size never rolls).
    logger.warning(f"[data_export] {procedure}: >{_INMEM_ROW_CAP:,} rows — "
                   f"streaming to a single file (split off)")
    return _stream_resultset_to_csv(cursor, columns, export_dir, rs_base,
                                    _UNLIMITED, procedure, buf)


def _execute_sql_to_files(sql, values, cancel_token, export_dir, base,
                          file_format, split_config, procedure) -> List[Dict[str, Any]]:
    """Execute SQL and write every result set to files, streaming large ones.

    Result-set naming matches the historical behaviour: a single result set is
    `<base>.<ext>`; multiple become `<base>_rs1`, `<base>_rs2`, …. Because the
    count isn't known until the cursor is exhausted, each set is written with a
    placeholder token that's resolved (dropped, or → `_rsN`) at the end."""
    max_rows = _resolve_max_rows(split_config)
    engine = get_data_engine()
    raw = engine.raw_connection()
    per_rs: List[List[Dict[str, Any]]] = []
    try:
        cursor = raw.cursor()
        if cancel_token is not None:
            cancel_token.bind_cursor(cursor)
        try:
            if values:
                cursor.execute(sql, values)
            else:
                cursor.execute(sql)
            while True:
                if cursor.description:  # None for DML / assignment statements
                    columns = [col[0] for col in cursor.description]
                    rs_base = f"{base}{_RS_TOKEN.format(len(per_rs) + 1)}"
                    per_rs.append(_export_one_resultset(
                        cursor, columns, export_dir, rs_base, file_format,
                        split_config, max_rows, procedure))
                if not cursor.nextset():
                    break
        finally:
            if cancel_token is not None:
                cancel_token.unbind_cursor()
            cursor.close()
        raw.commit()
    except OSError as e:
        if getattr(e, "errno", None) == 28:
            raise RuntimeError(
                f"disk full while writing '{procedure}' to {export_dir} — free "
                f"space or point the report's output folder to a larger drive") from e
        raise
    finally:
        raw.close()

    if not per_rs:
        return []
    # Resolve the per-result-set placeholder in every filename.
    single = len(per_rs) == 1
    resolved: List[Dict[str, Any]] = []
    for idx, written in enumerate(per_rs, start=1):
        token, repl = _RS_TOKEN.format(idx), ("" if single else f"_rs{idx}")
        for w in written:
            fname = os.path.basename(w["file"])
            if token in fname:
                new = os.path.join(os.path.dirname(w["file"]), fname.replace(token, repl))
                if new != w["file"]:
                    os.replace(w["file"], new)
                    w["file"] = new
            resolved.append(w)
    return resolved


def fanout_param_runs(
    proc_name: str, params: Dict[str, Any]
) -> List[tuple]:
    """Expand a proc's params for FANOUT parameters.

    If the proc has a parameter registered with FANOUT=1 in ARS_PROC_PARAM_VALUES
    and its value is a comma list, return one (params, suffix) run per value so
    the caller writes a SEPARATE output file per value (suffixed with the value).
    Otherwise returns a single [(params, None)]. Only the first matching fan-out
    param is expanded (no cross-product). Registry is optional — any error or a
    single value falls back to one run.
    """
    params = dict(params or {})
    pname = str(proc_name or "").strip().strip("[]").split(".")[-1].strip("[]")
    try:
        eng = get_data_engine()
        with eng.connect() as conn:
            if not conn.execute(text("SELECT OBJECT_ID('dbo.ARS_PROC_PARAM_VALUES')")).scalar():
                return [(params, None)]
            if not conn.execute(text("SELECT COL_LENGTH('dbo.ARS_PROC_PARAM_VALUES','FANOUT')")).scalar():
                return [(params, None)]
            fo = [r[0] for r in conn.execute(text("""
                SELECT DISTINCT PARAM_NAME FROM dbo.ARS_PROC_PARAM_VALUES
                WHERE LOWER(PROC_NAME) = LOWER(:p) AND ISNULL(FANOUT, 0) = 1
            """), {"p": pname})]
    except Exception:
        return [(params, None)]

    for fp in fo:
        key = next((k for k in params if k.lower() == str(fp).lower()), None)
        if not key or params.get(key) in (None, ""):
            continue
        vals = [v.strip() for v in str(params[key]).split(",") if v.strip()]
        if len(vals) <= 1:
            continue
        runs = []
        for v in vals:
            p2 = dict(params)
            p2[key] = v
            runs.append((p2, v))
        return runs
    return [(params, None)]


def run_procedure_to_files(
    proc: Union[str, Dict[str, Any]],
    export_dir: str,
    file_format: str = "csv",
    split_config: Optional[Dict[str, Any]] = None,
    cancel_token=None,
    output_name: Optional[str] = None,
    name_suffix: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Run one stored procedure and write every result set into export_dir.

    output_name overrides the file base name (defaults to the procedure name);
    name_suffix (e.g. the triggering session id) is appended to it.
    If split_config is given, each result set is split into multiple files per
    that config (same logic as the Export page). Returns a list of file dicts
    ({"procedure", "file", "rows"}). Raises if the proc name/params are invalid
    or the proc returns no result set. A cancel_token lets a caller kill the
    running query.
    """
    if file_format not in ("csv", "xlsx"):
        raise ValueError(f"Unsupported file_format: {file_format!r}")
    normalized = _normalize_procedures([proc])[0]
    name, params = normalized["name"], normalized["params"]

    if params:
        placeholders = ", ".join(f"@{p} = ?" for p in params)
        sql, values = f"EXEC {name} {placeholders}", list(params.values())
    else:
        sql, values = f"EXEC {name}", None

    base = _safe_folder_name(output_name, 120) if output_name else _proc_base_name(name)
    if name_suffix:
        base = f"{base}_{_safe_folder_name(str(name_suffix), 80)}"

    # Streaming executor: large result sets go straight to disk in batches so a
    # multi-million-row proc can never blow the process memory (the OOM we fixed).
    written = _execute_sql_to_files(
        sql, values, cancel_token, export_dir, base, file_format, split_config, name)
    if not written:
        raise RuntimeError(f"{name} returned no result set")
    return written


# A report "query" step runs read-only SQL. Block data-modifying / DDL keywords
# so a pasted query can never mutate the database by accident. EXEC is allowed
# ONLY for sp_executesql (the read-only dynamic-SQL pattern many report scripts
# use); any other EXEC belongs in a stored-procedure step.
_DESTRUCTIVE_RE = re.compile(
    r"\b(INSERT\s+INTO|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|MERGE\s+INTO|"
    r"GRANT|REVOKE|BACKUP|RESTORE)\b", re.IGNORECASE)
_BAD_EXEC_RE = re.compile(r"\bEXEC(UTE)?\b\s+(?!sp_executesql)", re.IGNORECASE)


def run_query_to_files(
    sql: str,
    base_name: str,
    export_dir: str,
    file_format: str = "csv",
    split_config: Optional[Dict[str, Any]] = None,
    cancel_token=None,
    name_suffix: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Run a read-only SQL query and write its result into export_dir.

    Only SELECT-style queries are allowed — any data-modifying / DDL keyword
    raises. Honors split_config exactly like a stored-procedure step. A raw
    cursor is used (not pd.read_sql) so a cancel_token can interrupt it.
    """
    if file_format not in ("csv", "xlsx"):
        raise ValueError(f"Unsupported file_format: {file_format!r}")
    if not sql or not str(sql).strip():
        raise ValueError("query step has empty SQL")
    if _DESTRUCTIVE_RE.search(sql):
        raise ValueError(
            "query step must be read-only — remove data-modifying/DDL keywords "
            "(INSERT INTO / UPDATE / DELETE / DROP / ALTER / CREATE / MERGE INTO). "
            "Use a stored-procedure step for anything that writes.")
    if _BAD_EXEC_RE.search(sql):
        raise ValueError(
            "query step may only EXEC sp_executesql (read-only dynamic SQL). "
            "To call another stored procedure, use a stored-procedure step.")

    label = _safe_folder_name(base_name or "query", 120) or "query"
    if name_suffix:
        label = f"{label}_{_safe_folder_name(str(name_suffix), 80)}"

    # Walk every result set (multi-statement batches return their SELECT after
    # the non-result statements), streaming large ones straight to disk.
    written = _execute_sql_to_files(
        sql, None, cancel_token, export_dir, label, file_format, split_config,
        base_name or "query")
    if not written:
        raise RuntimeError("query returned no result set (nothing to export)")
    return written


def export_session_data(
    session_code: str,
    procedures: List[Union[str, Dict[str, Any]]],
    base_dir: Optional[str] = None,
    file_format: str = "csv",
) -> Dict[str, Any]:
    """Export stored-procedure results into <base_dir>/data/<session_code>/.

    Args:
        session_code: session id used as the folder name
                      (e.g. from listing_sessions.make_session_id()).
        procedures:   list of proc names or {"name", "params"} dicts.
        base_dir:     desired folder; defaults to backend/exports.
        file_format:  "csv" (default) or "xlsx".

    Returns a manifest dict:
        {"session_code", "export_dir", "files": [...], "errors": [...]}
    """
    procs = _normalize_procedures(procedures)
    export_dir = make_session_dir(session_code, base_dir)

    manifest: Dict[str, Any] = {
        "session_code": session_code,
        "export_dir": export_dir,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "files": [],
        "errors": [],
    }

    for proc in procs:
        try:
            manifest["files"].extend(
                run_procedure_to_files(proc, export_dir, file_format)
            )
        except Exception as e:
            logger.error(f"[data_export] {proc} failed: {e}")
            manifest["errors"].append(
                {"procedure": proc if isinstance(proc, str) else proc.get("name"),
                 "error": str(e)}
            )

    manifest["completed_at"] = datetime.now().isoformat(timespec="seconds")
    return manifest
