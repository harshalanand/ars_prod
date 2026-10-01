"""
Get Data — bulk loader (the fast path).

Snowflake result → Arrow batches → delimited chunk files → `bcp` into the stage
table, several chunks at once. Measured on the store stock view on 2026-09-30:
bcp ~96,000 rows/s against ~7,800–13,000 rows/s for parameterised inserts.
bcp runs as its own process, so Python's single CPU core is no longer the limit.

Lossless by construction, and checked on every run:
  • fields are separated by 0x1F (unit separator) and rows by 0x1E (record
    separator), so text with line breaks — descriptions, VARIANT JSON — loads
    unchanged. Only a text value containing 0x1E, 0x1F or NUL itself sends the
    job to the classic loader (the profile pass counts them).
  • NULL is an empty field; an empty string is a single NUL byte (bcp's
    character-mode convention), so '' and NULL stay distinct.
  • text is written as UTF-8 and loaded with -C 65001, so NVARCHAR columns keep
    every character.
  • timestamps → UTC microseconds, booleans → 0/1, NaN/±Inf → NULL — the same
    values the classic loader writes.
  • each chunk is ONE bcp transaction (no -b), so a chunk that fails commits
    nothing and is retried once without duplicating rows.
After the load the engine compares the row count and per-column counts/sums
with Snowflake's own figures (get_data_sync_service._verify_stage).

Login: Windows authentication (-T, the account running the backend) when SQL
Server accepts it, checked every 10 minutes. Otherwise the app's SQL login, with
the password passed on bcp's standard input — never on its command line, where
any local user could read it from the process list.

Switches (environment): GET_DATA_FAST_LOADER=0 turns the fast path off,
GET_DATA_BCP_AUTH=windows|sql (default windows), GET_DATA_BCP_PARALLEL
(default 3), GET_DATA_CHUNK_ROWS (default 1,000,000).
Spec: frontend/public/docs/manual/get_data.md.
"""
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from app.core.config import get_settings

FAST_KINDS = {"int", "bigint", "dec", "float", "str", "date", "datetime", "bit", "time", "json"}
FIELD_SEP = "\x1f"          # bcp -t 0x1f
ROW_SEP = "\x1e"            # bcp -r 0x1e
PARALLEL = max(1, int(os.getenv("GET_DATA_BCP_PARALLEL", "3")))
CHUNK_ROWS = max(10_000, int(os.getenv("GET_DATA_CHUNK_ROWS", "1000000")))
BCP_TIMEOUT_S = 3600
AUTH_PREF = os.getenv("GET_DATA_BCP_AUTH", "windows").strip().lower()   # windows | sql
AUTH_RECHECK_S = 600

_BCP_CANDIDATES = (
    r"C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\180\Tools\Binn\bcp.exe",
    r"C:\Program Files\Microsoft SQL Server\Client SDK\ODBC\170\Tools\Binn\bcp.exe",
    "/opt/mssql-tools18/bin/bcp",
    "/opt/mssql-tools/bin/bcp",
)


class FastPathUnsupported(Exception):
    """The data can't go through bcp losslessly — use the classic loader."""


def find_bcp() -> Optional[str]:
    p = shutil.which("bcp")
    if p:
        return p
    return next((c for c in _BCP_CANDIDATES if os.path.exists(c)), None)


def fast_path_reason(specs: List[Dict[str, Any]], profiles: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """None when the bulk path can load these columns losslessly; otherwise
    the reason the classic loader is used (recorded on the run)."""
    if os.getenv("GET_DATA_FAST_LOADER", "1").strip().lower() in ("0", "false", "no", "off"):
        return "fast loader switched off (GET_DATA_FAST_LOADER=0)"
    if not find_bcp():
        return "bcp is not installed on this server"
    if bcp_auth()[0] is None:
        return "no login bcp can use: " + (bcp_auth()[1] or "unknown")
    odd = [f"{s['sf_name']} ({s['kind']})" for s in specs if s["kind"] not in FAST_KINDS]
    if odd:
        return "column types bcp can't take as text: " + ", ".join(odd[:5])
    unsafe = [s["sf_name"] for s in specs
              if s["kind"] == "str" and (profiles.get(s["sf_name"]) or {}).get("unsafe")]
    if unsafe:
        return ("text contains the bulk-file separator characters (0x1E / 0x1F / NUL) in: "
                + ", ".join(unsafe[:5]))
    return None


# ── Connection and login (mirror the app's own ODBC settings) ────────────────
def _target_args() -> List[str]:
    """Server, database and encryption — everything except the login."""
    c = get_settings()._db()
    host = str(c["server"]).strip()
    if ":" not in host.split("\\")[0]:
        host = f"tcp:{host}"
    port = str(c.get("port") or "").strip()
    if port and "," not in host and "\\" not in host:
        host = f"{host},{port}"
    args = ["-S", host, "-d", c["data_database"], "-l", "30"]
    args += ["-Ym" if str(c.get("encrypt")).lower() == "yes" else "-Yo"]
    if str(c.get("trust_cert")).lower() == "yes":
        args += ["-u"]
    return args


def _login(mode: str) -> Tuple[List[str], Optional[str]]:
    """(command-line args, stdin) for a login mode. The SQL password goes on
    stdin — bcp prompts for it when -U is given without -P."""
    if mode == "windows":
        return ["-T"], None
    c = get_settings()._db()
    return ["-U", c["username"]], (c.get("password") or "") + "\n"


def _windows_account() -> str:
    return f"{os.getenv('USERDOMAIN', '')}\\{os.getenv('USERNAME', '')}".strip("\\")


def _probe(mode: str) -> Tuple[bool, str]:
    """Can bcp log in with this mode? A one-row queryout, nothing written to SQL."""
    out = os.path.join(tempfile.gettempdir(), f"gd_probe_{os.getpid()}.txt")
    args, stdin = _login(mode)
    try:
        p = subprocess.run([find_bcp(), "SELECT 1", "queryout", out, *_target_args(), *args, "-c"],
                           input=stdin, capture_output=True, text=True, timeout=60)
        text_out = _mask(p.stdout + p.stderr)
        ok = p.returncode == 0 and "1 rows copied" in text_out
        return ok, "" if ok else text_out.strip()[-300:]
    except Exception as e:
        return False, str(e)
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


_auth_lock = threading.Lock()
_auth_state: Dict[str, Any] = {"at": 0.0, "mode": None, "note": None}


def bcp_auth() -> Tuple[Optional[str], Optional[str]]:
    """(mode, note) bcp will use: 'windows' when GET_DATA_BCP_AUTH=windows and
    SQL Server accepts the account running the backend; else 'sql' (password
    on stdin) with a note saying why; (None, reason) when neither works.
    Re-checked every 10 minutes, so a Windows login added by the DBA is picked
    up without a restart."""
    with _auth_lock:
        if _auth_state["mode"] and time.time() - _auth_state["at"] < AUTH_RECHECK_S:
            return _auth_state["mode"], _auth_state["note"]
        mode, note = None, None
        if AUTH_PREF == "windows":
            ok, err = _probe("windows")
            if ok:
                mode = "windows"
            else:
                note = f"Windows login {_windows_account()} refused by SQL Server"
                logger.warning(f"[get-data] bcp {note}: {err[:200]}")
        if mode is None and get_settings()._db().get("username"):
            ok, err = _probe("sql")
            if ok:
                mode = "sql"
            else:
                note = ((note + "; ") if note else "") + f"SQL login refused: {err[:150]}"
        _auth_state.update(at=time.time(), mode=mode, note=note)
        return mode, note


def auth_label() -> str:
    mode, note = bcp_auth()
    if mode == "windows":
        return f"Windows login {_windows_account()}"
    if mode == "sql":
        return "SQL login" + (f" — {note}" if note else "")
    return "no login"


def _mask(s: str) -> str:
    pwd = get_settings()._db().get("password") or ""
    return s.replace(pwd, "***") if pwd else s


# ── Arrow → file bytes ───────────────────────────────────────────────────────
def _encode(tbl, specs: List[Dict[str, Any]], run_id: int, loaded_at: str):
    """One Arrow table → a large_string array, one element per row, each
    already ending in ROW_SEP. Columns are taken by position (= SELECT order)."""
    import pyarrow as pa
    import pyarrow.compute as pc

    LS = pa.large_string()
    if tbl.num_columns != len(specs):
        raise FastPathUnsupported(f"Arrow batch has {tbl.num_columns} columns, expected {len(specs)}")
    cols = []
    for i, s in enumerate(specs):
        a = tbl.column(i).combine_chunks()
        k, t = s["kind"], tbl.column(i).type
        if k in ("str", "json"):     # json = VARIANT / OBJECT / ARRAY, delivered as JSON text
            if not (pa.types.is_string(t) or pa.types.is_large_string(t)):
                raise FastPathUnsupported(f"{s['sf_name']} arrived as {t}, not text")
            a = pc.cast(a, LS)
            a = pc.if_else(pc.equal(a, pa.scalar("", LS)), pa.scalar("\x00", LS), a)
        elif k == "time":            # → 'HH:MM:SS.ffffff', as the classic loader writes it
            if not pa.types.is_time(t):
                raise FastPathUnsupported(f"{s['sf_name']} arrived as {t}, not a time")
            a = pc.cast(a, pa.time64("us"))
        elif k == "datetime":
            if not pa.types.is_timestamp(t):
                raise FastPathUnsupported(f"{s['sf_name']} arrived as {t}, not a timestamp")
            if t.tz is not None:      # instant → UTC wall clock, like the classic loader
                a = pc.cast(pc.cast(pc.cast(a, pa.timestamp("us", tz="UTC")), pa.int64()), pa.timestamp("us"))
            else:
                a = pc.cast(a, pa.timestamp("us"))
        elif k == "bit":
            a = pc.cast(a, pa.int8())
        elif k == "float":
            a = pc.cast(a, pa.float64())
            a = pc.if_else(pc.or_(pc.is_nan(a), pc.is_inf(a)), pa.scalar(None, pa.float64()), a)
        elif k == "date":
            if pa.types.is_timestamp(t):
                a = pc.cast(a, pa.date32())
        elif k in ("int", "bigint", "dec"):
            if not (pa.types.is_integer(t) or pa.types.is_decimal(t)):
                raise FastPathUnsupported(f"{s['sf_name']} arrived as {t}, not a number")
        cols.append(pc.cast(a, LS))
    n = tbl.num_rows
    cols.append(pa.array([str(run_id)] * n, LS) if n else pa.array([], LS))
    cols.append(pa.array([loaded_at] * n, LS) if n else pa.array([], LS))
    rows = pc.binary_join_element_wise(*cols, pa.scalar(FIELD_SEP, LS),
                                       null_handling="replace", null_replacement="")
    return pc.binary_join_element_wise(rows, pa.scalar("", LS), pa.scalar(ROW_SEP, LS))


def _write(path: str, parts) -> None:
    import numpy as np
    with open(path, "wb") as fh:
        for arr in parts:
            if not len(arr):
                continue
            offs = np.frombuffer(arr.buffers()[1], dtype=np.int64)[arr.offset: arr.offset + len(arr) + 1]
            fh.write(memoryview(arr.buffers()[2])[int(offs[0]):int(offs[-1])])


# ── bcp ──────────────────────────────────────────────────────────────────────
_COPIED = re.compile(r"(\d+) rows copied")


def _bcp_chunk(path: str, stage: str, expected: int) -> int:
    """Load one chunk as a single transaction; retry once if nothing committed."""
    err = path + ".err"
    mode, _ = bcp_auth()
    login, stdin = _login(mode or "sql")
    # CHECK_CONSTRAINTS: the stage has no CHECK constraints, and with the hint
    # bcp needs only INSERT + SELECT — not ALTER TABLE — from its login.
    cmd = [find_bcp(), f"dbo.{stage}", "in", path, *_target_args(), *login,
           "-c", "-C", "65001", "-t", "0x1f", "-r", "0x1e", "-h", "TABLOCK,CHECK_CONSTRAINTS",
           "-a", "32768", "-m", "1", "-e", err]
    last = ""
    for attempt in (1, 2):
        try:
            p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=BCP_TIMEOUT_S)
            out = p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            out = f"bcp timed out after {BCP_TIMEOUT_S}s"
        m = _COPIED.search(out)
        copied = int(m.group(1)) if m else 0
        if copied == expected:
            return copied
        detail = ""
        if os.path.exists(err):
            with open(err, encoding="utf-8", errors="replace") as fh:
                detail = fh.read(600)
        last = _mask(f"{out.strip()[-500:]} {detail}".strip())
        if copied:           # partial commit can't happen with one batch — refuse to guess
            raise RuntimeError(f"bcp copied {copied:,} of {expected:,} rows: {last}")
        logger.warning(f"[get-data] bcp chunk {os.path.basename(path)} attempt {attempt} failed: {last[:300]}")
        time.sleep(5)
    raise RuntimeError(f"bcp failed twice for a chunk of {expected:,} rows: {last}")


def load_stage_bulk(sf_cur, stage: str, specs: List[Dict[str, Any]], run_id: int,
                    loaded_at: str, hb) -> int:
    """Stream the open Snowflake result into the (already created, empty) stage
    table. Returns rows loaded. Raises on any failure; the caller drops the stage."""
    tmpdir = tempfile.mkdtemp(prefix=f"gd_run{run_id}_")
    lock = threading.Lock()
    loaded = [0]
    pool = ThreadPoolExecutor(max_workers=PARALLEL, thread_name_prefix=f"GdBcp-{run_id}")
    running = set()
    fetched = 0

    def submit(parts, n, idx):
        path = os.path.join(tmpdir, f"chunk_{idx:05d}.dat")
        _write(path, parts)

        def job():
            try:
                c = _bcp_chunk(path, stage, n)
            finally:
                for f in (path, path + ".err"):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
            with lock:
                loaded[0] += c
                hb.rows_loaded = loaded[0]
            return c
        running.add(pool.submit(job))

    def drain(limit):
        while len(running) > limit:
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for f in done:
                running.discard(f)
                f.result()          # re-raise a chunk failure at once

    try:
        parts, n, idx, last_flush = [], 0, 0, time.time()
        for tbl in sf_cur.fetch_arrow_batches():
            parts.append(_encode(tbl, specs, run_id, loaded_at))
            n += tbl.num_rows
            fetched += tbl.num_rows
            if n >= CHUNK_ROWS:
                drain(PARALLEL)      # back-pressure: at most PARALLEL+1 chunk files on disk
                submit(parts, n, idx)
                parts, n, idx = [], 0, idx + 1
            if time.time() - last_flush > 5:
                hb.flush()
                last_flush = time.time()
        if n:
            drain(PARALLEL)
            submit(parts, n, idx)
        drain(0)
        if loaded[0] != fetched:
            raise RuntimeError(f"bcp loaded {loaded[0]:,} rows but {fetched:,} were fetched")
        return loaded[0]
    finally:
        for f in running:
            f.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
        shutil.rmtree(tmpdir, ignore_errors=True)
