"""
SAP gateway client — the ONLY thing in ARS that reaches SAP.

It speaks JSON-RPC over HTTPS to the V2 "universal MCP" worker (configured in
sap_config_service), which relays to SAP via RFC_READ_TABLE / OData and returns
JSON rows. No SAP SDK is involved on this server.

Read-only by design (GET data from SAP). Writing back to SAP is a future phase.

Tools used on the worker:
  * sap_read_table   — generic RFC_READ_TABLE reader (table, fields[], where, limit, offset, env)
  * sap_odata_pull   — OData v2 pull (service, entity, top, skip, filter, select, env)
  * sap_odata_services — catalog of known Z_SB_* services
  * v2_rfc_status    — health of the SAP RFC relay (used by Test Connection)
"""
import json
from typing import Any, Dict, List, Optional

import requests
from loguru import logger

from app.services import sap_config_service as cfg_svc

_HTTP_TIMEOUT = 120  # SAP reads can be slow


class SapError(Exception):
    """A gateway or SAP-side failure surfaced to the caller as a clean message."""


# ── Low-level JSON-RPC ───────────────────────────────────────────────────────
def _resolve_conn() -> Dict[str, Any]:
    cfg = cfg_svc.get_config(reveal=True)
    url = (cfg.get("worker_url") or "").strip()
    key = (cfg.get("api_key") or "").strip()
    if not url:
        raise SapError("SAP gateway URL is not configured (SAP → Connection).")
    if not key:
        raise SapError("SAP gateway API key is not configured (SAP → Connection).")
    return {"url": url, "key": key, "default_env": cfg.get("default_env") or "prod"}


def _rpc(method: str, params: Optional[Dict[str, Any]] = None,
         timeout: int = _HTTP_TIMEOUT) -> Dict[str, Any]:
    conn = _resolve_conn()
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    try:
        r = requests.post(
            conn["url"],
            headers={"Content-Type": "application/json", "X-API-Key": conn["key"]},
            json=body, timeout=timeout,
        )
    except requests.RequestException as e:
        raise SapError(f"Could not reach the SAP gateway: {e}")
    if r.status_code == 401 or r.status_code == 403:
        raise SapError("SAP gateway rejected the API key (401/403).")
    try:
        return r.json()
    except ValueError:
        raise SapError(f"SAP gateway returned a non-JSON response (HTTP {r.status_code}).")


def _extract(rpc_json: Dict[str, Any]) -> Any:
    """Unwrap an MCP tools/call response to the parsed tool payload, raising
    SapError on any transport-, gateway-, or SAP-side error."""
    if isinstance(rpc_json, dict) and rpc_json.get("error"):
        err = rpc_json["error"]
        raise SapError(err.get("message") if isinstance(err, dict) else str(err))
    result = (rpc_json or {}).get("result", {})
    content = result.get("content") if isinstance(result, dict) else None
    if content and isinstance(content, list):
        raw = content[0].get("text", "")
    else:
        raw = json.dumps(result)
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        obj = {"text": raw}
    # SAP RFC surfaces its own errors inside the payload.
    if isinstance(obj, dict):
        ex = obj.get("EX_RETURN")
        if isinstance(ex, dict) and str(ex.get("TYPE", "")).upper() == "E":
            raise SapError(f"SAP error: {ex.get('MESSAGE', 'unknown')}")
        if result.get("isError"):
            raise SapError(str(obj.get("text") or obj))
    return obj


def _call_tool(name: str, arguments: Dict[str, Any],
               timeout: int = _HTTP_TIMEOUT) -> Any:
    return _extract(_rpc("tools/call",
                         {"name": name, "arguments": arguments}, timeout))


def _normalize(obj: Any) -> Dict[str, List]:
    """Coerce a tool payload into {'columns': [...], 'rows': [ {col: val} ]}."""
    rows: List[Dict[str, Any]] = []
    columns: List[str] = []
    if isinstance(obj, list):
        rows = [r for r in obj if isinstance(r, dict)]
    elif isinstance(obj, dict):
        rows = (obj.get("rows") or obj.get("value")
                or (obj.get("d", {}) or {}).get("results") or [])
        rows = [r for r in rows if isinstance(r, dict)]
        columns = obj.get("columns") or []
    if not columns and rows:
        # Preserve first-row key order as the column order.
        columns = list(rows[0].keys())
    return {"columns": columns, "rows": rows}


# ── Public API ───────────────────────────────────────────────────────────────
def test_connection() -> Dict[str, Any]:
    """Ping the SAP RFC relay via the gateway. Returns {success, message}."""
    try:
        payload = _call_tool("v2_rfc_status", {}, timeout=30)
    except SapError as e:
        return {"success": False, "message": str(e)}
    # v2_rfc_status returns a health blob; any non-error payload means the
    # gateway is reachable and authenticated.
    msg = "SAP gateway reachable."
    if isinstance(payload, dict):
        msg = payload.get("status") or payload.get("message") or msg
    return {"success": True, "message": str(msg)[:900], "detail": payload}


def read_table(table: str, fields: Optional[List[str]] = None,
               where: Optional[str] = None, limit: int = 1000,
               offset: int = 0, env: Optional[str] = None) -> Dict[str, List]:
    """One page of an SAP table via RFC_READ_TABLE."""
    args: Dict[str, Any] = {"table": table, "limit": limit, "offset": offset,
                            "env": (env or _resolve_conn()["default_env"])}
    if fields:
        args["fields"] = fields
    if where:
        args["where"] = where
    return _normalize(_call_tool("sap_read_table", args))


def odata_pull(service: str, entity: str, top: int = 1000, skip: int = 0,
               filter: Optional[str] = None, select: Optional[str] = None,
               env: Optional[str] = None) -> Dict[str, List]:
    """One page of an SAP OData entity."""
    args: Dict[str, Any] = {"service": service, "entity": entity,
                            "top": top, "skip": skip,
                            "env": (env or _resolve_conn()["default_env"])}
    if filter:
        args["filter"] = filter
    if select:
        args["select"] = select
    return _normalize(_call_tool("sap_odata_pull", args))


def odata_services(env: Optional[str] = None) -> Any:
    """Catalog of discovered Z_SB_* OData services (for the Explorer)."""
    return _call_tool("sap_odata_services",
                      {"env": (env or _resolve_conn()["default_env"])}, timeout=30)
