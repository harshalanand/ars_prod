"""
GRT ALC — Step 1: load the workbook's three sheets, check first.

Two steps, each a background job whose progress lives in ARS_B2B_UPLOAD so a
refresh or a closed tab loses nothing:

    CHECK   read the workbook, clean every column exactly as the Streamlit
            loader does, run every check, and stage the cleaned sheets as
            parquet. Writes nothing to the data tables.
    LOAD    write the staged sheets. All selected sheets go in ONE
            transaction: either every table is replaced, or none is.

Differences from the tool's load_data.py, each on purpose:

  * It stops at the first problem; this collects every problem into one
    report, with counts and examples, before anything is written.
  * It reads the workbook once per attempt; this reads it once for the check
    and loads from the staged copy, so a 1.2M-row sheet is not parsed twice.
  * It commits each table on its own, so a failure half-way leaves one sheet
    new and the others old; this is all-or-nothing.
  * It infers nothing about warehouses; this stores BIN_RDC once, from the
    bin code, and checks it against the RDCs Store Master knows.

Cleaning itself (strip, blank tokens → NULL, width limits, integer rounding,
6-dp decimals, explicit parameter sizes for fast_executemany) is ported as is.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import traceback
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
from loguru import logger
from sqlalchemy import text

from app.database.session import data_engine
from app.services import b2b_schema as S

# ── sheet → table mapping (source header, target column, kind, width, required)
SHEETS: Dict[str, Tuple[str, List[Tuple[str, str, str, int, bool]]]] = {
    "Bin Master": (S.BIN_MASTER, [
        ("SEG", "SEG", "str", 10, False),
        ("DIV", "DIV", "str", 20, False),
        ("SUB_DIV", "SUB_DIV", "str", 30, False),
        ("MAJ_CAT", "MAJ_CAT", "str", 60, True),
        ("SIZE", "SIZE", "str", 20, False),
        ("SEASON", "SEASON", "str", 20, False),
        ("ART", "ART", "str", 20, True),
        ("BIN", "BIN", "str", 20, True),
        ("qty", "QTY", "int", 0, True),
    ]),
    "STORE MASTER": (S.STORE_MASTER, [
        ("Store_Code", "STORE_CODE", "str", 10, True),
        ("ST_NM", "ST_NM", "str", 100, False),
        ("OLD\\NEW", "OLD_NEW", "str", 10, False),
        ("RDC", "RDC", "str", 10, False),
    ]),
    "REQ": (S.REQ, [
        ("Store_Code", "STORE_CODE", "str", 10, True),
        ("ST_NM", "ST_NM", "str", 100, False),
        ("SEG", "SEG", "str", 10, False),
        ("DIV", "DIV", "str", 20, False),
        ("SUB_DIV", "SUB_DIV", "str", 30, False),
        ("MAJ_CAT", "MAJ_CAT", "str", 60, True),
        ("SIZE", "SIZE", "str", 20, False),
        ("ACC_D", "ACC_D", "int", 0, False),
        ("SEASON", "SEASON", "str", 20, False),
        ("REQ", "REQ", "dec", 0, True),
    ]),
}
# Other headers seen in real workbooks for the same column. Each use is
# reported in the check, never applied silently. Seen: the DW01 workbook has
# 'article' for ART and 'ACS_D' for ACC_D; the Streamlit loader rejects it.
ALIASES: Dict[str, List[str]] = {
    "ART": ["ARTICLE", "ARTICLE_NUMBER"],
    "ACC_D": ["ACS_D"],
    "Store_Code": ["STORE_CODE", "ST_CD"],
}
ALIAS_NOTE = {
    "ACS_D": "The DH24 workbooks call this ACC_D. Confirm it is the same display figure — "
             "it drives every target in the demand build.",
}
SHEET_KEYS = {"Bin Master": "bin", "STORE MASTER": "store", "REQ": "req"}
KEY_SHEETS = {v: k for k, v in SHEET_KEYS.items()}

DEFAULT_PATH = (r"\\file\0-V2\04-DEPARTMENT\07-REPLENISHMENT\14-MY-FOLDER"
                r"\100-VISHNU\DHYANU\REQ-BIN TO BIN TRANSFER-DH24.xlsx")
EXCEL_EXT = (".xlsx", ".xlsm", ".xls")
BATCH = 10_000
STAGE_TTL = timedelta(hours=24)
SAMPLE = 25                              # examples kept per check
_STAGE_ROOT = Path(__file__).resolve().parents[2] / "uploads" / "b2b_stage"
_BLANK = ("", "nan", "None", "NaT", "NaN", "none", "null", "NULL")

# One upload job at a time: a load replaces shared tables. `_admission` makes
# "is anything running? → start" one step, so two requests cannot both pass.
# Like the listing job manager, this assumes one server process.
_jobs: Dict[int, threading.Thread] = {}
_cancel: Dict[int, threading.Event] = {}
_jobs_lock = threading.Lock()
_admission = threading.Lock()


class Cancelled(Exception):
    pass


# ═══════════════════════════════════════════════════════════════════════════
#  job bookkeeping
# ═══════════════════════════════════════════════════════════════════════════
def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s).strip()).upper()


def _update(upload_id: int, **cols) -> None:
    """Write status/progress on its own short transaction, so the page sees it
    even while a load holds a long one open."""
    if not cols:
        return
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    with data_engine.begin() as conn:
        conn.execute(text(f"UPDATE dbo.{S.UPLOAD} SET {sets} WHERE UPLOAD_ID = :id"),
                     {**cols, "id": upload_id})


def _progress(upload_id: int, msg: str, pct: Optional[int] = None) -> None:
    logger.info(f"[b2b upload {upload_id}] {msg}")
    _update(upload_id, PROGRESS=msg[:300], **({"PROGRESS_PCT": int(pct)} if pct is not None else {}))


def running_job() -> Optional[int]:
    with _jobs_lock:
        for uid, t in list(_jobs.items()):
            if t.is_alive():
                return uid
            _jobs.pop(uid, None)
            _cancel.pop(uid, None)
    return None


def _start(upload_id: int, target: Callable[[], None]) -> None:
    with _jobs_lock:
        ev = threading.Event()
        _cancel[upload_id] = ev
        t = threading.Thread(target=target, name=f"b2b-upload-{upload_id}", daemon=True)
        _jobs[upload_id] = t
        t.start()


def _check_cancel(upload_id: int) -> None:
    ev = _cancel.get(upload_id)
    if ev is not None and ev.is_set():
        raise Cancelled()


def cancel(upload_id: int) -> bool:
    ev = _cancel.get(upload_id)
    if ev is None:
        return False
    ev.set()
    return True


def _row_to_dict(r) -> Dict[str, Any]:
    d = dict(r._mapping)
    for k, v in list(d.items()):
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        elif isinstance(v, Decimal):
            d[k] = float(v)
    if d.get("CHECKS_JSON"):
        try:
            d["checks"] = json.loads(d["CHECKS_JSON"])
        except ValueError:
            d["checks"] = None
    d.pop("CHECKS_JSON", None)
    d.pop("STAGE_DIR", None)
    return d


def get(upload_id: int) -> Optional[Dict[str, Any]]:
    S.ensure_tables()
    with data_engine.connect() as conn:
        r = conn.execute(text(f"SELECT * FROM dbo.{S.UPLOAD} WHERE UPLOAD_ID = :id"),
                         {"id": upload_id}).fetchone()
    if r is None:
        return None
    d = _row_to_dict(r)
    # A job the server no longer runs (restart, crash) must not look alive.
    # QUEUED is excluded: it covers the moment before the worker thread starts.
    if d["STATUS"] in ("CHECKING", "LOADING") and running_job() != upload_id:
        was = d["STATUS"]
        _update(upload_id, STATUS="FAILED",
                PROGRESS=f"Interrupted while {was.lower()}",
                ERROR=f"The server stopped while this was {was.lower()}. "
                      + ("The load was never committed, so the tables are unchanged."
                         if was == "LOADING" else "Run the check again."))
        return get(upload_id)
    return d


def recent(limit: int = 15) -> List[Dict[str, Any]]:
    S.ensure_tables()
    with data_engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT TOP (:n) UPLOAD_ID, STATUS, SOURCE_KIND, SOURCE_NAME, MODE, SHEETS,
                   ROWS_BIN, ROWS_STORE, ROWS_REQ, BLOCKING, WARNINGS, CREATED_BY,
                   CREATED_AT, CHECKED_AT, LOADED_AT, LOADED_BY, READ_SEC, LOAD_SEC, ERROR
              FROM dbo.{S.UPLOAD} ORDER BY UPLOAD_ID DESC"""), {"n": limit}).fetchall()
    return [_row_to_dict(r) for r in rows]


def last_loaded() -> Optional[Dict[str, Any]]:
    with data_engine.connect() as conn:
        r = conn.execute(text(f"""
            SELECT TOP 1 * FROM dbo.{S.UPLOAD}
             WHERE STATUS = 'LOADED' ORDER BY LOADED_AT DESC, UPLOAD_ID DESC""")).fetchone()
    return _row_to_dict(r) if r else None


# ═══════════════════════════════════════════════════════════════════════════
#  reading and cleaning
# ═══════════════════════════════════════════════════════════════════════════
def _excel_engine() -> Optional[str]:
    try:
        import python_calamine  # noqa: F401  — Rust reader, several times faster
        return "calamine"
    except ImportError:
        return None                      # pandas picks openpyxl for .xlsx


def validate_path(path: str) -> Path:
    p = Path(str(path).strip().strip('"'))
    if p.suffix.lower() not in EXCEL_EXT:
        raise ValueError(f"Not an Excel workbook: {p.name}. Expected {', '.join(EXCEL_EXT)}.")
    if not p.exists():
        raise ValueError(f"Workbook not found, or the server cannot reach it: {p}")
    return p


def list_folder(folder: Optional[str] = None) -> Dict[str, Any]:
    """Excel workbooks in a folder, newest first. Names, sizes and dates only —
    nothing is opened. The tool's configured default is the oldest file in its
    folder (31 Aug, 1,867 bin rows), so picking by date matters."""
    d = Path(str(folder).strip().strip('"')) if folder else Path(DEFAULT_PATH).parent
    if not d.exists() or not d.is_dir():
        raise ValueError(f"Folder not found, or the server cannot reach it: {d}")
    files = []
    for p in d.iterdir():
        if p.is_file() and p.suffix.lower() in EXCEL_EXT and not p.name.startswith("~$"):
            st = p.stat()
            files.append({"name": p.name, "path": str(p), "bytes": st.st_size,
                          "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="minutes"),
                          "warehouse": detect_warehouse(p.name),
                          "is_default": str(p).lower() == DEFAULT_PATH.lower()})
    files.sort(key=lambda f: f["modified"], reverse=True)
    return {"folder": str(d), "files": files}


def _match_sheets(names: List[str], wanted: List[str]) -> Tuple[Dict[str, str], List[str]]:
    """Map wanted sheet → actual sheet, ignoring case and spacing."""
    by_norm = {_norm(n): n for n in names}
    found, missing = {}, []
    for w in wanted:
        hit = by_norm.get(_norm(w))
        if hit is None:
            missing.append(w)
        else:
            found[w] = hit
    return found, missing


def prepare(raw: pd.DataFrame, mapping, sheet: str) -> Tuple[Optional[pd.DataFrame], List[Dict]]:
    """Clean one sheet into table shape. Returns (frame, issues). The frame is
    None when a column is missing — nothing further can be judged."""
    issues: List[Dict] = []
    by_norm = {_norm(c): c for c in raw.columns}
    # Resolve each expected header to a real column, trying known aliases.
    for src, *_ in mapping:
        if _norm(src) in by_norm:
            continue
        for alt in ALIASES.get(src, []):
            if _norm(alt) in by_norm:
                actual = by_norm[_norm(alt)]
                by_norm[_norm(src)] = actual
                issues.append(_issue("HEADER_ALIAS", "info", sheet,
                                     f"Column '{actual}' read as {src}",
                                     ALIAS_NOTE.get(_norm(alt), "Same column under another name.")))
                break
    missing = [src for src, *_ in mapping if _norm(src) not in by_norm]
    if missing:
        issues.append(_issue("COLS", "block", sheet,
                             f"{len(missing)} column(s) missing from '{sheet}'",
                             f"Expected: {', '.join(missing)}. Found: {', '.join(map(str, raw.columns))}",
                             count=len(missing), sample=missing))
        return None, issues

    out = pd.DataFrame(index=raw.index)
    for src, tgt, kind, width, required in mapping:
        col = raw[by_norm[_norm(src)]]
        if kind == "str":
            col = col.astype(str).str.strip()
            col = col.mask(col.isin(_BLANK), None)
            lens = col.dropna().str.len()
            over = lens[lens > width]
            if len(over):
                ex = col.loc[over.index].head(5).tolist()
                issues.append(_issue("WIDTH", "block", sheet,
                                     f"'{src}' has {len(over):,} value(s) longer than {width} characters",
                                     f"Longest is {int(over.max())}. The column holds {width}. "
                                     f"Nothing is cut short silently — shorten the values or widen the table.",
                                     count=int(len(over)), sample=ex))
        else:
            txt = col.astype(str).str.strip()
            present = ~txt.isin(_BLANK)
            num = pd.to_numeric(col, errors="coerce")
            bad = present & num.isna()
            if bad.any():
                ex = col[bad].head(5).tolist()
                issues.append(_issue("NUMBER", "block" if required else "warn", sheet,
                                     f"'{src}' has {int(bad.sum()):,} value(s) that are not numbers",
                                     "They would load as blank." + (" The column is required." if required else ""),
                                     count=int(bad.sum()), sample=ex))
            neg = num < 0
            if neg.any():
                issues.append(_issue("NEGATIVE", "warn", sheet,
                                     f"'{src}' has {int(neg.sum()):,} negative value(s)",
                                     "Loaded as they are. Check they are intended.",
                                     count=int(neg.sum()), sample=num[neg].head(5).tolist()))
            col = num.round().astype("Int64") if kind == "int" else num.round(6)
        if required:
            blank = col.isna()
            if blank.any():
                issues.append(_issue("BLANK", "block", sheet,
                                     f"'{src}' is blank on {int(blank.sum()):,} row(s)",
                                     "Every row needs a value in this column.",
                                     count=int(blank.sum()),
                                     sample=[int(i) + 2 for i in blank[blank].index[:SAMPLE]],
                                     sample_label="sheet row"))
        out[tgt] = col
    return out, issues


def _issue(code, level, sheet, title, detail="", *, count=None, units=None,
           sample=None, sample_label=None) -> Dict[str, Any]:
    return {"code": code, "level": level, "sheet": sheet, "title": title,
            "detail": detail, "count": count, "units": units,
            "sample": [str(x) for x in (sample or [])][:SAMPLE],
            "sample_label": sample_label}


# ── warehouses ──────────────────────────────────────────────────────────────
# A warehouse code looks like DH24 / DW01. A bin's own zone prefix looks like
# A2 / D4 / M0. The shape is what tells them apart.
_WH_SHAPE = re.compile(r"^[A-Z]{2}\d{2}$")
_WH_IN_NAME = re.compile(r"(?<![A-Z0-9])([A-Z]{2}\d{2})(?![A-Z0-9])")
STORE_STATUS = {"OLD", "NEW", "UPC"}


def detect_warehouse(file_name: str) -> Optional[str]:
    """'REQ-BIN TO BIN TRANSFER-DH24-(01-09).xlsx' → 'DH24'."""
    m = _WH_IN_NAME.search(Path(str(file_name)).stem.upper())
    return m.group(1) if m else None


def _store_cols_swapped(sm: pd.DataFrame) -> bool:
    """RDC holding OLD/NEW/UPC while OLD\\NEW holds something else means the
    two data columns sit under each other's headers. Seen in every current
    DH24 workbook (31 Aug, 1 Sep, 8 Sep)."""
    rdc = {str(v).upper() for v in sm["RDC"].dropna()}
    status = {str(v).upper() for v in sm["OLD_NEW"].dropna()}
    return bool(rdc) and rdc <= STORE_STATUS and bool(status) and not status <= STORE_STATUS


def _resolve_store_swap(frames, options, issues) -> None:
    sm = frames.get("STORE MASTER")
    if sm is None or not _store_cols_swapped(sm):
        return
    sample = [f"{a}: OLD\\NEW = {b}, RDC = {c}"
              for a, b, c in sm[["STORE_CODE", "OLD_NEW", "RDC"]].head(5).itertuples(index=False)]
    if options.get("swap_store_cols"):
        sm["RDC"], sm["OLD_NEW"] = sm["OLD_NEW"].copy(), sm["RDC"].copy()
        issues.append(_issue("STORE_SWAP", "info", "STORE MASTER",
                             "RDC and OLD\\NEW swapped back for this load",
                             "You chose to swap them. The workbook itself is unchanged.", sample=sample))
    else:
        issues.append(_issue("STORE_SWAP", "block", "STORE MASTER",
                             "The RDC and OLD\\NEW columns look swapped",
                             "RDC holds OLD / NEW / UPC and OLD\\NEW holds warehouse codes. Loaded like "
                             "this, every store's warehouse would be 'OLD'. Swap the two header cells in "
                             "the workbook, or swap them for this load only.",
                             sample=sample))


def _resolve_bins(frames, mode, options, issues) -> None:
    """Set BIN_RDC on every bin, and give a bin with no warehouse the prefix
    the live B2B_BIN_MASTER already uses (A2-0101-B1 → DH24-A2-0101-B1)."""
    bm = frames.get("Bin Master")
    if bm is None:
        return
    declared = (options.get("bin_rdc") or "").strip().upper() or None
    bins = bm["BIN"].astype(object)
    has_dash = bins.map(lambda b: isinstance(b, str) and "-" in b)
    pre = bins.map(lambda b: b.split("-", 1)[0].upper() if isinstance(b, str) and "-" in b else None)
    carries = has_dash & pre.map(lambda p: bool(p) and bool(_WH_SHAPE.match(p)))
    need = ~carries & bins.notna()
    rdc = pre.where(carries, None)

    if need.any():
        n = int(need.sum())
        eg = bins[need].drop_duplicates().head(3).tolist()      # several articles share one bin
        if not declared:
            issues.append(_issue("BIN_WAREHOUSE", "block", "Bin Master",
                                 f"{n:,} bin code(s) do not say which warehouse they are in",
                                 f"Codes like {', '.join(eg)} have no warehouse prefix, and none could be "
                                 f"read from the file name. Choose the warehouse these bins belong to.",
                                 count=n, sample=eg))
        else:
            new = declared + "-" + bins[need].astype(str)
            long_ = new[new.str.len() > 20]
            if len(long_):
                issues.append(_issue("WIDTH", "block", "Bin Master",
                                     f"{len(long_):,} bin code(s) would be longer than 20 characters "
                                     f"with the {declared} prefix", count=len(long_),
                                     sample=long_.head(5).tolist()))
            else:
                bm.loc[need, "BIN"] = new
                rdc = rdc.where(~need, declared)
                issues.append(_issue("BIN_PREFIX", "info", "Bin Master",
                                     f"{n:,} bin code(s) will be stored with the {declared} prefix",
                                     f"For example {eg[0]} → {declared}-{eg[0]}. This is the form the live "
                                     f"B2B_BIN_MASTER already uses. It keeps a bin unique when the same zone "
                                     f"code exists in both warehouses, and it is how the warehouse rule knows "
                                     f"where a pick comes from.", count=n,
                                     sample=[f"{b} → {declared}-{b}" for b in eg]))
    bm["BIN_RDC"] = rdc.astype(object)


# ═══════════════════════════════════════════════════════════════════════════
#  CHECK
# ═══════════════════════════════════════════════════════════════════════════
def start_check(source_kind: str, source: str, mode: str, sheets: List[str],
                user: Optional[str], file_obj=None, file_name: Optional[str] = None,
                options: Optional[Dict[str, Any]] = None) -> int:
    """options: bin_rdc — warehouse of the bins (default: read from the file
    name); swap_store_cols — swap RDC and OLD\\NEW back for this load."""
    S.ensure_tables()
    with _admission:
        busy = running_job()
        if busy:
            raise RuntimeError(f"Upload {busy} is still running. Wait for it to finish or cancel it.")
        return _admit_check(source_kind, source, mode, sheets, user, file_obj, file_name,
                            dict(options or {}))


def _admit_check(source_kind, source, mode, sheets, user, file_obj, file_name, options) -> int:
    mode = mode.upper()
    if mode not in ("OVERWRITE", "APPEND"):
        raise ValueError("Mode must be OVERWRITE or APPEND")
    sheets = [KEY_SHEETS.get(s, s) for s in (sheets or list(SHEETS))]
    bad = [s for s in sheets if s not in SHEETS]
    if bad or not sheets:
        raise ValueError(f"Unknown sheet(s): {bad or '(none chosen)'}")

    if source_kind == "PATH":
        path = validate_path(source)
        name, size = str(path), path.stat().st_size
    elif source_kind == "FILE":
        if file_obj is None:
            raise ValueError("No file received")
        if Path(file_name or "").suffix.lower() not in EXCEL_EXT:
            raise ValueError(f"Not an Excel workbook: {file_name}")
        name, size = file_name, None
    else:
        raise ValueError("Source must be PATH or FILE")

    if options.get("bin_rdc"):
        options["bin_rdc"] = str(options["bin_rdc"]).strip().upper()
        options["bin_rdc_from"] = "chosen"
    else:
        found = detect_warehouse(name)
        options["bin_rdc"] = found
        options["bin_rdc_from"] = "file name" if found else None
    if options["bin_rdc"] and not _WH_SHAPE.match(options["bin_rdc"]):
        raise ValueError(f"'{options['bin_rdc']}' is not a warehouse code like DH24 or DW01")

    with data_engine.begin() as conn:
        upload_id = int(conn.execute(text(f"""
            INSERT INTO dbo.{S.UPLOAD} (STATUS, SOURCE_KIND, SOURCE_NAME, FILE_BYTES, MODE, SHEETS,
                                        PROGRESS, PROGRESS_PCT, CREATED_BY, OPTIONS_JSON)
            OUTPUT INSERTED.UPLOAD_ID
            VALUES ('QUEUED', :k, :n, :b, :m, :s, 'Queued', 0, :u, :o)"""),
            {"k": source_kind, "n": name[:500], "b": size, "m": mode,
             "s": ",".join(SHEET_KEYS[x] for x in sheets), "u": user,
             "o": json.dumps(options)}).scalar())

    stage = _STAGE_ROOT / str(upload_id)
    stage.mkdir(parents=True, exist_ok=True)
    if source_kind == "FILE":
        dest = stage / ("source" + Path(file_name).suffix.lower())
        with open(dest, "wb") as fh:
            shutil.copyfileobj(file_obj, fh, length=4 * 1024 * 1024)
        path = dest
        _update(upload_id, FILE_BYTES=dest.stat().st_size)
    _update(upload_id, STAGE_DIR=str(stage))

    _start(upload_id, lambda: _run_check(upload_id, path, mode, sheets, stage, options))
    return upload_id


def _run_check(upload_id: int, path: Path, mode: str, sheets: List[str], stage: Path,
               options: Dict[str, Any]) -> None:
    t0 = time.time()
    try:
        _update(upload_id, STATUS="CHECKING")
        _progress(upload_id, "Opening workbook", 2)
        engine = _excel_engine()
        issues: List[Dict] = []
        frames: Dict[str, pd.DataFrame] = {}
        # `with` closes the file handle; on Windows an open handle would stop
        # the staged copy of an uploaded workbook from being deleted later.
        with (pd.ExcelFile(path, engine=engine) if engine else pd.ExcelFile(path)) as xl:
            found, missing = _match_sheets(xl.sheet_names, sheets)
            for m in missing:
                issues.append(_issue("SHEET", "block", m, f"Sheet '{m}' not found",
                                     f"The workbook has: {', '.join(xl.sheet_names)}"))
            for i, sheet in enumerate(s for s in sheets if s in found):
                _check_cancel(upload_id)
                _progress(upload_id, f"Reading '{found[sheet]}'", 5 + i * 25)
                raw = xl.parse(found[sheet], dtype=str)
                _progress(upload_id, f"Cleaning '{sheet}' — {len(raw):,} rows", 15 + i * 25)
                frame, sheet_issues = prepare(raw, SHEETS[sheet][1], sheet)
                del raw
                issues.extend(sheet_issues)
                if frame is not None:
                    frames[sheet] = frame
        read_sec = round(time.time() - t0, 1)

        _check_cancel(upload_id)
        # Store Master first: its RDCs are the warehouses the bins are judged against.
        _resolve_store_swap(frames, options, issues)
        _resolve_bins(frames, mode, options, issues)
        _progress(upload_id, "Checking the sheets against each other", 82)
        issues.extend(_cross_checks(frames, mode, sheets))
        summary = _summary(frames)

        _progress(upload_id, "Staging the cleaned sheets", 92)
        for sheet, frame in frames.items():
            frame.to_parquet(stage / f"{SHEET_KEYS[sheet]}.parquet", index=False)
        for src in stage.glob("source.*"):          # the uploaded workbook is no longer needed
            src.unlink(missing_ok=True)

        blocking = sum(1 for x in issues if x["level"] == "block")
        warnings = sum(1 for x in issues if x["level"] == "warn")
        order = {"block": 0, "warn": 1, "ok": 2, "info": 3}
        issues.sort(key=lambda x: order.get(x["level"], 9))
        _update(upload_id, STATUS="CHECKED", CHECKED_AT=datetime.now(),
                ROWS_BIN=len(frames["Bin Master"]) if "Bin Master" in frames else None,
                ROWS_STORE=len(frames["STORE MASTER"]) if "STORE MASTER" in frames else None,
                ROWS_REQ=len(frames["REQ"]) if "REQ" in frames else None,
                BLOCKING=blocking, WARNINGS=warnings, READ_SEC=read_sec,
                CHECKS_JSON=json.dumps({"issues": issues, "summary": summary,
                                        "engine": engine or "openpyxl"}, default=str),
                PROGRESS=("Ready to load" if not blocking
                          else f"{blocking} problem(s) must be fixed before loading"),
                PROGRESS_PCT=100)
    except Cancelled:
        _update(upload_id, STATUS="CANCELLED", PROGRESS="Cancelled", ERROR="Cancelled by user")
        shutil.rmtree(stage, ignore_errors=True)
    except Exception as e:
        logger.exception(f"[b2b upload {upload_id}] check failed")
        _update(upload_id, STATUS="FAILED", ERROR=f"{e}\n\n{traceback.format_exc()[-1500:]}",
                PROGRESS="Check failed")
        shutil.rmtree(stage, ignore_errors=True)


def _db_distinct(sql: str) -> set:
    with data_engine.connect() as conn:
        return {tuple(r) if len(r) > 1 else r[0] for r in conn.execute(text(sql))}


def _after(sheet: str, frames, mode, cols: List[str], table: str) -> set:
    """The set of `cols` values the table will hold AFTER this upload loads."""
    db = None
    if sheet not in frames or mode == "APPEND":
        sel = ", ".join(f"[{c}]" for c in cols)
        db = _db_distinct(f"SELECT DISTINCT {sel} FROM dbo.{table}")
    if sheet not in frames:
        return db or set()
    f = frames[sheet][cols].dropna(subset=[cols[0]])
    new = set(f.itertuples(index=False, name=None)) if len(cols) > 1 else set(f[cols[0]])
    if len(cols) > 1:
        new = {tuple(None if pd.isna(v) else v for v in t) for t in new}
    return new | (db or set()) if mode == "APPEND" else new


def _cross_checks(frames: Dict[str, pd.DataFrame], mode: str, sheets: List[str]) -> List[Dict]:
    out: List[Dict] = []
    bm, sm, rq = frames.get("Bin Master"), frames.get("STORE MASTER"), frames.get("REQ")

    # G5 — duplicate store codes (the key would fail the load)
    if sm is not None:
        dup = sm["STORE_CODE"].dropna()
        dup = dup[dup.duplicated(keep=False)]
        if len(dup):
            out.append(_issue("G5", "block", "STORE MASTER",
                              f"{dup.nunique():,} store code(s) appear more than once",
                              "Store code is the key of Store Master. Keep one row per store.",
                              count=int(dup.nunique()), sample=sorted(dup.unique())))
        if mode == "APPEND":
            have = _db_distinct(f"SELECT STORE_CODE FROM dbo.{S.STORE_MASTER}")
            clash = sorted(set(sm["STORE_CODE"].dropna()) & have)
            if clash:
                out.append(_issue("APPEND_KEY", "block", "STORE MASTER",
                                  f"{len(clash):,} store(s) already in Store Master",
                                  "Append cannot add a store that is already there. Use Overwrite.",
                                  count=len(clash), sample=clash))

    # G4 — an article must have one category and one size (the build takes MAX)
    if bm is not None:
        g = bm.groupby("ART", dropna=True).agg(
            cats=("MAJ_CAT", "nunique"),
            sizes=("SIZE", lambda s: s.fillna("").nunique()))
        multi = g[(g.cats > 1) | (g.sizes > 1)]
        if len(multi):
            out.append(_issue("G4", "block", "Bin Master",
                              f"{len(multi):,} article(s) sit under more than one category or size",
                              "The demand build keeps one category and one size per article. "
                              "It would quietly pick one and lose the rest.",
                              count=len(multi), sample=multi.index.tolist()))
        else:
            out.append(_issue("G4", "ok", "Bin Master",
                              "Every article has one category and one size"))

        # duplicate (ART, BIN) rows — allowed by the tool, but they double stock
        d = bm.duplicated(subset=["ART", "BIN"], keep=False)
        if d.any():
            keys = bm.loc[d, ["ART", "BIN"]].drop_duplicates()
            out.append(_issue("DUP_BIN", "warn", "Bin Master",
                              f"{len(keys):,} article-bin pair(s) appear on more than one row",
                              "Their quantities add up when stock is counted.",
                              count=len(keys), units=int(bm.loc[d, "QTY"].sum()),
                              sample=[f"{a} @ {b}" for a, b in keys.head(SAMPLE).itertuples(index=False)]))

        # G8 — append would double what is already loaded
        if mode == "APPEND":
            have = _db_distinct(f"SELECT DISTINCT ART, BIN FROM dbo.{S.BIN_MASTER}")
            pairs = set(bm[["ART", "BIN"]].dropna().itertuples(index=False, name=None))
            again = pairs & have
            if again:
                out.append(_issue("G8", "warn", "Bin Master",
                                  f"{len(again):,} article-bin pair(s) are already loaded",
                                  "Append adds rows on top and never removes duplicates, so this "
                                  "stock would be counted twice. Use Overwrite unless this is a "
                                  "second warehouse's extract.",
                                  count=len(again),
                                  sample=[f"{a} @ {b}" for a, b in list(again)[:SAMPLE]]))

        zero = int((bm["QTY"] == 0).sum())
        if zero:
            out.append(_issue("ZERO_QTY", "info", "Bin Master",
                              f"{zero:,} bin row(s) hold zero",
                              "Loaded as they are and ignored later. This is normal."))

    if rq is not None:
        d = rq.duplicated(subset=["STORE_CODE", "MAJ_CAT", "SIZE"], keep=False)
        if d.any():
            keys = rq.loc[d, ["STORE_CODE", "MAJ_CAT", "SIZE"]].drop_duplicates()
            out.append(_issue("DUP_REQ", "warn", "REQ",
                              f"{len(keys):,} store × category × size line(s) appear more than once",
                              "The allocator adds them up, so that store's requirement is counted twice.",
                              count=len(keys), units=float(rq.loc[d, "REQ"].sum()),
                              sample=[f"{a} / {b} / {c or '(blank)'}"
                                      for a, b, c in keys.head(SAMPLE).itertuples(index=False)]))
        nacc = int(rq["ACC_D"].isna().sum())
        if nacc:
            out.append(_issue("ACC_D", "warn", "REQ",
                              f"ACC_D is blank on {nacc:,} REQ line(s)",
                              "A store and category with no ACC_D gets a target of zero.",
                              count=nacc))

    # G1 — REQ stores the build cannot see
    if "REQ" in frames or "STORE MASTER" in frames:
        master = _after("STORE MASTER", frames, mode, ["STORE_CODE"], S.STORE_MASTER)
        if rq is not None:
            per = rq.groupby("STORE_CODE")["REQ"].sum()
        else:
            with data_engine.connect() as conn:
                per = pd.Series({r[0]: float(r[1] or 0) for r in conn.execute(text(
                    f"SELECT STORE_CODE, SUM(REQ) FROM dbo.{S.REQ} GROUP BY STORE_CODE"))})
        lost = per[~per.index.isin(master)]
        if len(lost):
            out.append(_issue("G1", "warn", "REQ",
                              f"{len(lost):,} REQ store(s) are missing from Store Master",
                              f"{lost.sum():,.0f} units they asked for can never be sent. The demand "
                              f"build skips any store it cannot find in Store Master, with no error.",
                              count=len(lost), units=float(lost.sum()),
                              sample=[f"{k} ({v:,.0f})" for k, v in lost.sort_values(ascending=False).items()]))
        else:
            out.append(_issue("G1", "ok", "REQ", "Every REQ store is in Store Master"))

    # G2 — categories in one sheet only, and likely spelling drift
    if "Bin Master" in frames or "REQ" in frames:
        bin_cats = _after("Bin Master", frames, mode, ["MAJ_CAT"], S.BIN_MASTER)
        req_cats = _after("REQ", frames, mode, ["MAJ_CAT"], S.REQ)
        only_bin, only_req = bin_cats - req_cats, req_cats - bin_cats
        key = lambda c: re.sub(r"[^A-Z0-9]", "", str(c).upper())
        rk = {key(c): c for c in only_req}
        drift = sorted((b, rk[key(b)]) for b in only_bin if key(b) in rk)
        if drift:
            out.append(_issue("G2_DRIFT", "warn", "Both",
                              f"{len(drift):,} category name(s) differ only in spelling between the sheets",
                              "Stock and requirement for these will never meet.",
                              count=len(drift), sample=[f"{a}  ≠  {b}" for a, b in drift]))
        if only_bin or only_req:
            out.append(_issue("G2", "warn", "Both",
                              f"{len(only_bin) + len(only_req):,} categories appear in only one sheet",
                              f"{len(only_bin):,} have stock and no REQ, usually non-merchandise such as "
                              f"carry bags. {len(only_req):,} have REQ and no stock.",
                              count=len(only_bin) + len(only_req),
                              sample=[f"stock only: {c}" for c in sorted(only_bin)][:12]
                                     + [f"REQ only: {c}" for c in sorted(only_req)][:13]))
        else:
            out.append(_issue("G2", "ok", "Both", "Every category appears in both sheets"))

        # sizes a category's REQ never mentions: that stock has a cap of zero
        if bm is not None:
            req_pairs = _after("REQ", frames, mode, ["MAJ_CAT", "SIZE"], S.REQ)
            shared = bin_cats & req_cats
            b = bm[bm["MAJ_CAT"].isin(shared)]
            pairs = b.groupby(["MAJ_CAT", b["SIZE"].fillna("")])["QTY"].sum()
            req_norm = {(c, s if s is not None else "") for c, s in req_pairs}
            orphan = pairs[[p not in req_norm for p in pairs.index]]
            if len(orphan):
                out.append(_issue("G2_SIZE", "warn", "Both",
                                  f"{len(orphan):,} category + size pair(s) have stock the REQ never asks for",
                                  "No store can receive them, because the REQ cap for that size is zero.",
                                  count=len(orphan), units=int(orphan.sum()),
                                  sample=[f"{c} / {s or '(blank)'} ({q:,})"
                                          for (c, s), q in orphan.sort_values(ascending=False).items()]))

    # G3 — each bin warehouse should have stores to serve. BIN_RDC is already
    # resolved by _resolve_bins; a bin still without one was blocked there.
    if bm is not None and bm["BIN_RDC"].notna().any():
        rdcs = {str(r).upper() for r in _after("STORE MASTER", frames, mode, ["RDC"], S.STORE_MASTER) if r}
        by = bm.groupby(bm["BIN_RDC"].fillna("(none)"))["QTY"].agg(["size", "sum"])
        orphan = [k for k in by.index if k != "(none)" and k not in rdcs]
        if orphan:
            out.append(_issue("G3", "warn", "Bin Master",
                              f"Bins in {', '.join(orphan)}, but Store Master has no store there",
                              f"Store Master's warehouses: {', '.join(sorted(rdcs)) or '(none)'}. These bins "
                              f"can only ship to another warehouse's stores, which the warehouse rule "
                              f"Home only will never allow.",
                              count=int(by.loc[orphan, "size"].sum()), units=int(by.loc[orphan, "sum"].sum()),
                              sample=[f"{k}: {int(by.loc[k, 'size']):,} rows" for k in orphan]))
        else:
            out.append(_issue("G3", "ok", "Bin Master", "Every bin's warehouse has stores in Store Master",
                              " · ".join(f"{k} {int(r['size']):,} rows, {int(r['sum']):,} pcs"
                                         for k, r in by.iterrows())))
    return out


def _summary(frames: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
    s: Dict[str, Any] = {}
    if "Bin Master" in frames:
        b = frames["Bin Master"]
        s["bin"] = {"rows": len(b), "articles": int(b["ART"].nunique()),
                    "bins": int(b["BIN"].nunique()), "categories": int(b["MAJ_CAT"].nunique()),
                    "qty": int(b["QTY"].sum()),
                    "by_rdc": {str(k): {"rows": int(v["size"]), "qty": int(v["sum"])}
                               for k, v in b.groupby(b["BIN_RDC"].fillna("(none)"))["QTY"]
                                            .agg(["size", "sum"]).iterrows()}}
    if "STORE MASTER" in frames:
        m = frames["STORE MASTER"]
        s["store"] = {"rows": len(m), "stores": int(m["STORE_CODE"].nunique()),
                      "by_rdc": {str(k): int(v) for k, v in m["RDC"].fillna("(none)").value_counts().items()}}
    if "REQ" in frames:
        r = frames["REQ"]
        s["req"] = {"rows": len(r), "stores": int(r["STORE_CODE"].nunique()),
                    "categories": int(r["MAJ_CAT"].nunique()), "units": float(r["REQ"].sum())}
    return s


# ═══════════════════════════════════════════════════════════════════════════
#  LOAD
# ═══════════════════════════════════════════════════════════════════════════
def start_load(upload_id: int, user: Optional[str]) -> int:
    S.ensure_tables()
    from app.services import b2b_mbq_service as M      # lazy: M imports this module
    with _admission:
        busy = running_job()
        if busy:
            raise RuntimeError(f"Upload {busy} is still running.")
        building = M.running_build()
        if building:
            raise RuntimeError(f"MBQ build {building} is reading these tables. Load when it finishes.")
        from app.services import b2b_alloc_service as A
        if A.running_run():
            raise RuntimeError(f"Allocation session {A.running_run()} is reading these tables. "
                               f"Load when it finishes.")
        return _admit_load(upload_id, user)


def _admit_load(upload_id: int, user: Optional[str]) -> int:
    with data_engine.connect() as conn:
        r = conn.execute(text(f"SELECT * FROM dbo.{S.UPLOAD} WHERE UPLOAD_ID = :id"),
                         {"id": upload_id}).fetchone()
        if r is None:
            raise ValueError(f"Upload {upload_id} not found")
        u = dict(r._mapping)
        if u["STATUS"] != "CHECKED":
            raise ValueError(f"Upload {upload_id} is {u['STATUS']}; only a checked upload can be loaded")
        if (u["BLOCKING"] or 0) > 0:
            raise ValueError(f"Upload {upload_id} has {u['BLOCKING']} problem(s) that must be fixed first")
        # The check compared against the tables as they were. If anything has
        # been loaded since, those comparisons no longer hold.
        newer = conn.execute(text(f"""
            SELECT TOP 1 UPLOAD_ID FROM dbo.{S.UPLOAD}
             WHERE STATUS = 'LOADED' AND LOADED_AT > :t"""), {"t": u["CHECKED_AT"]}).scalar()
    if newer:
        _expire(upload_id, u, f"Upload {newer} was loaded after this check. Check the file again.")
        raise ValueError(f"Upload {newer} was loaded after this check was run. Check the file again.")
    if u["CHECKED_AT"] and datetime.now() - u["CHECKED_AT"] > STAGE_TTL:
        _expire(upload_id, u, "Check is older than 24 hours. Check again.")
        raise ValueError("This check is older than 24 hours. Check the file again.")
    stage = Path(u["STAGE_DIR"] or "")
    if not stage.exists():
        _update(upload_id, STATUS="EXPIRED", PROGRESS="Staged data is gone. Check again.")
        raise ValueError("The staged data for this check is gone. Check the file again.")

    sheets = [KEY_SHEETS[k] for k in (u["SHEETS"] or "").split(",") if k in KEY_SHEETS]
    _update(upload_id, STATUS="LOADING", LOADED_BY=user, PROGRESS="Starting load", PROGRESS_PCT=0,
            ERROR=None)
    _start(upload_id, lambda: _run_load(upload_id, stage, sheets, u["MODE"], user))
    return upload_id


def _expire(upload_id: int, u: Dict[str, Any], why: str) -> None:
    _update(upload_id, STATUS="EXPIRED", PROGRESS=why)
    if u.get("STAGE_DIR"):
        shutil.rmtree(u["STAGE_DIR"], ignore_errors=True)   # can never be loaded now


def _input_sizes(mapping, extra: List[Tuple[str, int]]):
    import pyodbc
    sizes = []
    for _, _, kind, width, _ in mapping:
        if kind == "str":
            sizes.append((pyodbc.SQL_WVARCHAR, width, 0))
        elif kind == "int":
            sizes.append((pyodbc.SQL_INTEGER, 0, 0))
        else:
            sizes.append((pyodbc.SQL_DECIMAL, 18, 6))
    for kind, width in extra:
        sizes.append((pyodbc.SQL_WVARCHAR, width, 0) if kind == "str" else (pyodbc.SQL_INTEGER, 0, 0))
    return sizes


def _batch_rows(frame: pd.DataFrame, mapping, cols: List[str], upload_id: int):
    """Yield insert tuples BATCH rows at a time. Converting the whole sheet at
    once would hold ~1.9M Python tuples — well over a GB — for one load."""
    ints = [tgt for _, tgt, kind, _, _ in mapping if kind == "int"]
    decs = [tgt for _, tgt, kind, _, _ in mapping if kind == "dec"]
    data_cols = [c for c in cols if c != "UPLOAD_ID"]
    for i in range(0, len(frame), BATCH):
        part = frame.iloc[i:i + BATCH][data_cols].astype(object)
        part = part.where(pd.notna(part), None)
        for c in ints:
            part[c] = [None if v is None else int(v) for v in part[c]]
        for c in decs:
            part[c] = [None if v is None else Decimal(f"{float(v):.6f}") for v in part[c]]
        if "UPLOAD_ID" in cols:
            part["UPLOAD_ID"] = upload_id
        yield list(part[cols].itertuples(index=False, name=None))


def _run_load(upload_id: int, stage: Path, sheets: List[str], mode: str, user: Optional[str]) -> None:
    t0 = time.time()
    raw = data_engine.raw_connection()
    try:
        frames = {s: pd.read_parquet(stage / f"{SHEET_KEYS[s]}.parquet") for s in sheets}
        total = sum(len(f) for f in frames.values()) or 1
        done = 0

        cur = raw.cursor()
        cur.fast_executemany = True
        # Everything below is one transaction. TRUNCATE is transactional in
        # SQL Server, so a failure at any point rolls every table back.
        for sheet in sheets:
            table, mapping = SHEETS[sheet]
            frame = frames[sheet]
            extra = ["BIN_RDC", "UPLOAD_ID"] if sheet == "Bin Master" else ["UPLOAD_ID"]
            cols = [tgt for _, tgt, *_ in mapping] + extra
            sizes = _input_sizes(mapping, ([("str", 10)] if sheet == "Bin Master" else []) + [("int", 0)])
            _check_cancel(upload_id)
            if mode == "OVERWRITE":
                cur.execute(f"TRUNCATE TABLE dbo.[{table}]")
            sql = (f"INSERT INTO dbo.[{table}] ({', '.join(f'[{c}]' for c in cols)}) "
                   f"VALUES ({', '.join('?' for _ in cols)})")
            cur.setinputsizes(sizes)
            sent = 0
            for rows in _batch_rows(frame, mapping, cols, upload_id):
                _check_cancel(upload_id)
                cur.executemany(sql, rows)
                sent += len(rows)
                done += len(rows)
                _progress(upload_id, f"Loading {sheet}: {sent:,} of {len(frame):,}",
                          int(done / total * 95))
        _progress(upload_id, "Committing", 97)
        raw.commit()
        took = round(time.time() - t0, 1)
        _update(upload_id, STATUS="LOADED", LOADED_AT=datetime.now(), LOAD_SEC=took,
                PROGRESS=f"Loaded {done:,} rows in {took:,.0f}s", PROGRESS_PCT=100)
        shutil.rmtree(stage, ignore_errors=True)
        logger.info(f"[b2b upload {upload_id}] loaded {done:,} rows ({mode}) by {user} in {took}s")
    except Cancelled:
        raw.rollback()
        # Nothing changed, so the check still holds: back to CHECKED, stage
        # kept, and the same upload can simply be loaded again.
        _update(upload_id, STATUS="CHECKED", PROGRESS_PCT=100,
                PROGRESS="Load cancelled. Nothing was written — you can load again.",
                ERROR=None)
    except Exception as e:
        try:
            raw.rollback()
        except Exception:
            pass
        logger.exception(f"[b2b upload {upload_id}] load failed")
        _update(upload_id, STATUS="FAILED", PROGRESS="Load failed. Nothing was written.",
                ERROR=f"{e}\n\n{traceback.format_exc()[-1500:]}")
    finally:
        try:
            raw.close()
        except Exception:
            pass


def purge_stale_stages() -> int:
    """Remove staged sheets older than the TTL. Called opportunistically."""
    if not _STAGE_ROOT.exists():
        return 0
    n = 0
    cutoff = time.time() - STAGE_TTL.total_seconds()
    for d in _STAGE_ROOT.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff and (not d.name.isdigit() or running_job() != int(d.name)):
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n
