"""
SAP module Snowflake door — thin adapter over the shared Snowflake config.

The actual connection + query logic and the single source-of-truth credentials
now live in snowflake_config_service (Settings → Snowflake), so the SAP door and
the Report Generation engine share one configuration. This module stays as the
stable import point for sap_pull_service and the /sap endpoints, and re-raises
Snowflake errors as SapError so existing handlers work unchanged.
"""
from typing import Any, Dict, List, Optional

from app.services import snowflake_config_service as sfc
from app.services.snowflake_config_service import SnowflakeError
from app.services.sap_client import SapError


def test_connection() -> Dict[str, Any]:
    return sfc.test_connection()


def run_query(sql: str, row_limit: Optional[int] = None,
              database: Optional[str] = None, schema: Optional[str] = None) -> Dict[str, List]:
    try:
        return sfc.run_query(sql, row_limit=row_limit, database=database, schema=schema)
    except SnowflakeError as e:
        raise SapError(str(e))


def list_tables(database: Optional[str] = None, schema: Optional[str] = None,
                like: Optional[str] = None, limit: int = 500) -> Dict[str, List]:
    try:
        return sfc.list_tables(database=database, schema=schema, like=like, limit=limit)
    except SnowflakeError as e:
        raise SapError(str(e))
