"""
mcp_hopc560.py — Minimal stdio MCP server for HOPC560 / rep_data SQL Server.

Exposes one tool:
    sql_query(sql, db?)  — run any SELECT against rep_data (or another DB on
                           the same server) and return the result as text.

Transport: stdio JSON-RPC 2.0  (Claude Desktop / VSCode MCP)

Connection: uses ODBC Driver 17 for SQL Server.
  • By default: Windows Authentication (Trusted_Connection=yes)
  • Override with env vars: HOPC560_USER and HOPC560_PASS for SQL auth

Add to Claude Desktop config  (%APPDATA%\\Claude\\claude_desktop_config.json):
{
  "mcpServers": {
    "hopc560": {
      "command": "E:\\\\ARS\\\\backend\\\\venv\\\\Scripts\\\\python.exe",
      "args":    ["E:\\\\ARS\\\\scripts\\\\mcp_hopc560.py"],
      "env":     {}
    }
  }
}

For SQL auth add to "env":
  "HOPC560_USER": "your_login",
  "HOPC560_PASS": "your_password"
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Dict, List, Optional

import pyodbc

# ── Connection ───────────────────────────────────────────────────────────────
SERVER   = os.getenv("HOPC560_SERVER", "HOPC560")
DEFAULT_DB = os.getenv("HOPC560_DB",  "rep_data")
USER     = os.getenv("HOPC560_USER", "")
PASS     = os.getenv("HOPC560_PASS", "")
DRIVER   = "ODBC Driver 17 for SQL Server"


def _conn(db: str = DEFAULT_DB) -> pyodbc.Connection:
    if USER and PASS:
        cs = (
            f"DRIVER={{{DRIVER}}};SERVER={SERVER};DATABASE={db};"
            f"UID={USER};PWD={PASS};"
            "Connection Timeout=10;"
        )
    else:
        cs = (
            f"DRIVER={{{DRIVER}}};SERVER={SERVER};DATABASE={db};"
            "Trusted_Connection=yes;"
            "Connection Timeout=10;"
        )
    return pyodbc.connect(cs, autocommit=True)


def _run_query(sql: str, db: str = DEFAULT_DB, max_rows: int = 200) -> str:
    """Execute SQL and return a plain-text table. Truncates at max_rows."""
    try:
        with _conn(db) as conn:
            cur = conn.cursor()
            cur.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(max_rows + 1)
    except pyodbc.Error as e:
        return f"SQL ERROR: {e}"

    if not cols:
        return "(no columns returned — possibly a non-SELECT statement)"

    truncated = len(rows) > max_rows
    rows = rows[:max_rows]

    # Format as padded text table
    col_widths = [max(len(str(c)), max((len(str(r[i] if r[i] is not None else 'NULL'))
                  for r in rows), default=0))
                  for i, c in enumerate(cols)]
    sep  = "+-" + "-+-".join("-" * w for w in col_widths) + "-+"
    hdr  = "| " + " | ".join(str(c).ljust(col_widths[i]) for i, c in enumerate(cols)) + " |"
    lines = [sep, hdr, sep]
    for row in rows:
        cell = " | ".join(str(v if v is not None else "NULL").ljust(col_widths[i])
                          for i, v in enumerate(row))
        lines.append("| " + cell + " |")
    lines.append(sep)
    if truncated:
        lines.append(f"(results truncated to {max_rows} rows)")
    lines.append(f"\n{len(rows)} row(s)  server={SERVER}  db={db}")
    return "\n".join(lines)


# ── MCP JSON-RPC stdio ────────────────────────────────────────────────────────
TOOL_DEF = {
    "name": "sql_query",
    "description": (
        "Run a SQL SELECT query against HOPC560 / rep_data (ARS tables: "
        "ARS_LISTING_WORKING, ARS_ALLOC_WORKING, ARS_MSA_TOTAL, etc.). "
        "Returns a text table of results."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "The SQL query to execute (SELECT only recommended)."
            },
            "db": {
                "type": "string",
                "description": f"Database name on HOPC560 (default: {DEFAULT_DB})."
            },
        },
        "required": ["sql"],
    },
}

CAPABILITIES = {
    "protocolVersion": "2024-11-05",
    "capabilities": {"tools": {}},
    "serverInfo": {"name": "hopc560-mcp", "version": "1.0.0"},
}


def _send(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _respond(req_id: Any, result: Any) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id: Any, code: int, msg: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id,
           "error": {"code": code, "message": msg}})


def _handle(msg: Dict[str, Any]) -> None:
    method = msg.get("method", "")
    req_id = msg.get("id")           # None for notifications
    params = msg.get("params") or {}

    if method == "initialize":
        _respond(req_id, CAPABILITIES)

    elif method == "notifications/initialized":
        pass  # notification — no response

    elif method == "tools/list":
        _respond(req_id, {"tools": [TOOL_DEF]})

    elif method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if name != "sql_query":
            _error(req_id, -32601, f"Unknown tool: {name}")
            return
        sql = (args.get("sql") or "").strip()
        db  = (args.get("db")  or DEFAULT_DB).strip()
        if not sql:
            _error(req_id, -32602, "Missing required argument: sql")
            return
        try:
            result_text = _run_query(sql, db)
        except Exception:
            result_text = "INTERNAL ERROR:\n" + traceback.format_exc()
        _respond(req_id, {
            "content": [{"type": "text", "text": result_text}],
            "isError": result_text.startswith("SQL ERROR") or
                       result_text.startswith("INTERNAL ERROR"),
        })

    elif req_id is not None:
        _error(req_id, -32601, f"Method not found: {method}")


def main() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            _send({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": f"Parse error: {e}"}})
            continue
        try:
            _handle(msg)
        except Exception:
            req_id = msg.get("id")
            if req_id is not None:
                _error(req_id, -32603, traceback.format_exc())


if __name__ == "__main__":
    main()
