"""
MSA Stock Calculation Service
Handles filtering, calculating, and pivoting MSA data
"""
import json
import pandas as pd
import numpy as np
from sqlalchemy import text, MetaData, Table as SQLTable
from typing import Dict, List, Any, Optional, Tuple
from loguru import logger


def _df_to_native_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Convert a DataFrame to list-of-dicts with **pure Python** scalar types.

    Why this exists: pandas' ``to_dict('records')`` keeps the underlying
    numpy scalars (``numpy.int64``, ``numpy.float64``, ``numpy.bool_``,
    ``pandas.Timestamp``) in the dict values. FastAPI's response_model
    serialization (Pydantic v2 + pydantic_core in Rust) walks the
    ``Optional[Any]`` payload and on certain code paths calls
    ``PyNumber_Index`` on the scalars; Python ``float`` has no
    ``__index__`` so the whole HTTP response 500s with
    ``'float' object cannot be interpreted as an integer`` even though
    the MSA calculation itself succeeded and the rows were already
    persisted to ARS_MSA_*.

    The fastest reliable way to strip numpy/pandas types is a JSON
    round-trip: ``to_json`` knows how to serialize every numpy/pandas
    scalar, and ``json.loads`` deserializes back into pure
    ``int`` / ``float`` / ``str`` / ``bool`` / ``None``. NaN / NaT
    become ``None``. Timestamps become ISO-8601 strings.
    """
    if df is None or df.empty:
        return []
    return json.loads(df.to_json(orient="records", date_format="iso"))


class MSAService:
    """Service for MSA stock calculation operations"""

    def __init__(self, db, rls_categories: list = None):
        """
        Initialize MSAService

        Args:
            db: SQLAlchemy session (Data DB session)
            rls_categories: Optional list of MAJ_CAT values the user can access.
                           If None or empty, no category filter is applied (admin/unrestricted).
        """
        self.db = db
        self.main_table = "VW_ET_MSA_STK_WITH_MASTER"
        self.hold_table = "ARS_NL_TBL_HOLD_TRACKING"
        self.st_master_table = "Master_ALC_INPUT_ST_MASTER"
        self._rls_categories = rls_categories or []
        # Soft-failure surface: any step that can't complete but isn't
        # fatal (e.g. Step 7.5 master-variant backfill skipped because the
        # SQL connection dropped) appends a user-readable string here.
        # calculate() returns this list so the UI can toast each one.
        self.warnings: List[str] = []

    # ------------------------------------------------------------------
    # Open-hold loader (used by Step 6 to deduct reserved units from STK)
    # ------------------------------------------------------------------
    def _load_ars_pending(self) -> pd.DataFrame:
        """Load open pending allocations from ARS_PEND_ALC.

        Returns DataFrame with columns RDC, ARTICLE_NUMBER, ARS_PEND — the
        approved-but-not-yet-DO'd quantities from the ARS system. These are
        added to the legacy MASTER_ALC_PEND deduction in Step 6 to prevent
        double-allocation of warehouse stock before SAP issues a Delivery Order.
        """
        try:
            result = self.db.execute(text("""
                SELECT RDC, ARTICLE_NUMBER,
                       SUM(PEND_QTY) AS ARS_PEND
                FROM ARS_PEND_ALC WITH (NOLOCK)
                WHERE IS_CLOSED = 0 AND PEND_QTY > 0
                GROUP BY RDC, ARTICLE_NUMBER
            """))
            rows = result.fetchall()
            if not rows:
                return pd.DataFrame(columns=["RDC", "ARTICLE_NUMBER", "ARS_PEND"])
            df = pd.DataFrame(rows, columns=["RDC", "ARTICLE_NUMBER", "ARS_PEND"])
            df["ARS_PEND"] = pd.to_numeric(df["ARS_PEND"], errors="coerce").fillna(0)
            return df
        except Exception as e:
            logger.warning(f"[msa] _load_ars_pending failed: {e}")
            return pd.DataFrame(columns=["RDC", "ARTICLE_NUMBER", "ARS_PEND"])

    def _load_open_holds(self) -> pd.DataFrame:
        """Aggregate HOLD_REM for currently-open NL/TBL hold reservations.

        Returns a DataFrame with columns RDC, ARTICLE_NUMBER, HOLD_QTY ready
        to merge into msa_pivot on (ST_CD, ARTICLE_NUMBER). The tracker stores
        WERKS (store) so we map WERKS -> RDC via the store master to bring the
        deduction up to the warehouse grain MSA operates at. Empty DataFrame
        is returned if the tracker or master is absent — MSA still works,
        just without the hold deduction.
        """
        try:
            # Probe the RDC column name on the store master (varies by env)
            cols_result = self.db.execute(text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME = :t"
            ), {"t": self.st_master_table})
            cols_set = {str(r[0]).upper() for r in cols_result.fetchall()}
            rdc_col = next((c for c in ("RDC", "WAREHOUSE", "HUB", "WH_CD")
                            if c in cols_set), None)
            if not rdc_col:
                logger.warning(
                    f"_load_open_holds: no RDC column found on "
                    f"{self.st_master_table}; skipping hold deduction"
                )
                return pd.DataFrame(columns=["RDC", "ARTICLE_NUMBER", "HOLD_QTY"])

            sql = f"""
                SELECT
                    S.[{rdc_col}]   AS RDC,
                    H.[VAR_ART]     AS ARTICLE_NUMBER,
                    SUM(ISNULL(H.[HOLD_REM], 0)) AS HOLD_QTY
                FROM [{self.hold_table}] H
                INNER JOIN [{self.st_master_table}] S
                    ON S.[ST_CD] = H.[WERKS]
                WHERE ISNULL(H.[IS_CLOSED], 0) = 0
                  AND ISNULL(H.[HOLD_REM], 0) > 0
                GROUP BY S.[{rdc_col}], H.[VAR_ART]
            """
            holds_result = self.db.execute(text(sql))
            holds_rows = holds_result.fetchall()
            if not holds_rows:
                return pd.DataFrame(columns=["RDC", "ARTICLE_NUMBER", "HOLD_QTY"])
            holds = pd.DataFrame(holds_rows, columns=["RDC", "ARTICLE_NUMBER", "HOLD_QTY"])
            holds["HOLD_QTY"] = pd.to_numeric(
                holds["HOLD_QTY"], errors="coerce"
            ).fillna(0)
            return holds
        except Exception as e:
            logger.warning(f"_load_open_holds failed (skipping hold deduction): {e}")
            return pd.DataFrame(columns=["RDC", "ARTICLE_NUMBER", "HOLD_QTY"])

    # ------------------------------------------------------------------
    # Universe discovery (Step 0 — drives variant backfill in Step 6)
    # ------------------------------------------------------------------
    def _load_universe(
        self,
        slocs: Optional[List[str]],
        date_filter: Optional[str],
    ) -> pd.DataFrame:
        """Compute the (RDC, GEN_ART_NUMBER) universe that should appear in MSA.

        Three sources, UNION-ed:
          A. Stock universe — products with stock in the SELECTED SLOCs only.
             If `slocs` is empty or None, all SLOCs participate. STK_Q magnitude
             is irrelevant — any positive stock qualifies the GEN_ART.
          B. Pend universe — products with any open ARS_PEND_ALC row (IS_CLOSED=0,
             PEND_QTY > 0). RDC and GEN_ART_NUMBER are read directly from the
             pending row.
          C. Hold universe — products with any open ARS_NL_TBL_HOLD_TRACKING
             row (IS_CLOSED=0, HOLD_REM > 0). WERKS is mapped to RDC via the
             store master; VAR_ART is mapped to GEN_ART_NUMBER via
             vw_master_product.

        The result drives the Step 6 backfill so that every (RDC, GEN_ART) with
        any real signal (stock-in-scope, PEND, or HOLD) has all its master
        variants present in msa_pivot — guaranteeing PEND/HOLD obligations
        always have a row to land on in Steps 7-8.

        Returns a DataFrame with columns [RDC, GEN_ART_NUMBER] as clean strings.
        Empty DataFrame if every source fails or returns nothing.
        """
        parts: List[pd.DataFrame] = []

        # --- Source A: stock in selected SLOCs ----------------------------
        if date_filter:
            try:
                params: Dict[str, Any] = {"d": date_filter}
                sloc_clause = ""
                if slocs:
                    placeholders = ",".join(f":s{i}" for i in range(len(slocs)))
                    sloc_clause = f" AND SLOC IN ({placeholders})"
                    for i, s in enumerate(slocs):
                        params[f"s{i}"] = s
                sql_a = f"""
                    SELECT DISTINCT
                        CAST(ST_CD AS NVARCHAR(50))           AS RDC,
                        CAST(GEN_ART_NUMBER AS NVARCHAR(50))  AS GEN_ART_NUMBER
                    FROM {self.main_table}
                    WHERE CAST([DATE] AS DATE) = :d
                      AND SEG IN ('APP','GM')
                      AND ISNULL(STK_Q, 0) > 0
                      AND GEN_ART_NUMBER IS NOT NULL
                      {sloc_clause}
                """
                df_a = pd.read_sql(text(sql_a), self.db.bind, params=params)
                parts.append(df_a)
                logger.info(
                    f"[universe] A (stock@SLOCs={slocs or 'ALL'}): {len(df_a)} keys"
                )
            except Exception as e:
                logger.warning(f"[universe] source A (stock) failed: {e}")

        # --- Source B: open PEND ------------------------------------------
        try:
            df_b = pd.read_sql(text("""
                SELECT DISTINCT
                    CAST(RDC AS NVARCHAR(50))            AS RDC,
                    CAST(GEN_ART_NUMBER AS NVARCHAR(50)) AS GEN_ART_NUMBER
                FROM ARS_PEND_ALC WITH (NOLOCK)
                WHERE IS_CLOSED = 0
                  AND ISNULL(PEND_QTY, 0) > 0
                  AND GEN_ART_NUMBER IS NOT NULL
            """), self.db.bind)
            parts.append(df_b)
            logger.info(f"[universe] B (open PEND): {len(df_b)} keys")
        except Exception as e:
            logger.warning(f"[universe] source B (pend) failed: {e}")

        # --- Source C: open HOLD (WERKS->RDC, VAR_ART->GEN via master) ---
        try:
            df_c = pd.read_sql(text("""
                SELECT DISTINCT
                    CAST(SM.RDC AS NVARCHAR(50))            AS RDC,
                    CAST(MP.GEN_ART_NUMBER AS NVARCHAR(50)) AS GEN_ART_NUMBER
                FROM ARS_NL_TBL_HOLD_TRACKING H WITH (NOLOCK)
                INNER JOIN Master_ALC_INPUT_ST_MASTER SM
                    ON SM.ST_CD = H.WERKS
                INNER JOIN vw_master_product MP
                    ON CAST(MP.ARTICLE_NUMBER AS NVARCHAR(30))
                     = CAST(H.VAR_ART AS NVARCHAR(30))
                WHERE ISNULL(H.IS_CLOSED, 0) = 0
                  AND ISNULL(H.HOLD_REM, 0) > 0
                  AND MP.GEN_ART_NUMBER IS NOT NULL
            """), self.db.bind)
            parts.append(df_c)
            logger.info(f"[universe] C (open HOLD): {len(df_c)} keys")
        except Exception as e:
            logger.warning(f"[universe] source C (hold) failed: {e}")

        if not parts:
            return pd.DataFrame(columns=["RDC", "GEN_ART_NUMBER"])

        universe = pd.concat(parts, ignore_index=True).drop_duplicates()
        universe["RDC"] = universe["RDC"].astype(str).str.strip()
        universe["GEN_ART_NUMBER"] = (
            universe["GEN_ART_NUMBER"].astype(str).str.strip()
        )
        universe = universe[
            (universe["GEN_ART_NUMBER"].str.len() > 0)
            & (universe["GEN_ART_NUMBER"].str.lower() != "nan")
            & (universe["RDC"].str.len() > 0)
        ].reset_index(drop=True)
        logger.info(
            f"[universe] union total: {len(universe)} distinct "
            f"(RDC, GEN_ART_NUMBER) keys"
        )
        return universe

    # ------------------------------------------------------------------
    # Master variant loader (used by Step 6 to backfill universe variants)
    # ------------------------------------------------------------------
    def _load_master_variants(self, gen_arts: List[str]) -> pd.DataFrame:
        """Load all VAR_ART rows from vw_master_product for the given GEN_ART_NUMBERs.

        ET_MSA_STK only carries rows for variants that had a stock record on
        the run date, so a (MAJ_CAT, GEN_ART_NUMBER, CLR) group with five
        master-defined VAR_ARTs can surface in MSA with just the stocked one.
        Step 7.5 uses this loader to find the missing variants and seed them
        as zero-stock placeholder rows. Returns an empty DataFrame on any
        error so the caller can skip backfill without breaking the run.
        """
        if not gen_arts:
            return pd.DataFrame()
        try:
            cleaned = [
                str(g).strip() for g in gen_arts
                if g is not None
                and str(g).strip()
                and str(g).strip().lower() != "nan"
            ]
            if not cleaned:
                return pd.DataFrame()

            # Probe schema — vw_master_product column set varies by env.
            want = [
                "ARTICLE_NUMBER", "GEN_ART_NUMBER", "MAJ_CAT", "CLR", "SZ",
                "SEG", "M_VND_NM", "M_VND_CD", "MACRO_MVGR", "MICRO_MVGR",
                "FAB", "MVGR_MATRIX", "SSN", "SUB_DIV", "DIV", "MRP", "RSP",
            ]
            cols_result = self.db.execute(text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME = 'vw_master_product'"
            ))
            available = {str(r[0]).upper() for r in cols_result.fetchall()}
            select_cols = [c for c in want if c.upper() in available]
            if (
                "ARTICLE_NUMBER" not in select_cols
                or "GEN_ART_NUMBER" not in select_cols
            ):
                logger.warning(
                    "[msa] _load_master_variants: vw_master_product missing "
                    "ARTICLE_NUMBER or GEN_ART_NUMBER — cannot backfill"
                )
                return pd.DataFrame()
            col_list = ", ".join(f"mp.[{c}]" for c in select_cols)

            # Bulk-load gen_arts into a session-local #tmp and JOIN.
            # vw_master_product is a slow VIEW — hitting it once with all
            # gen_arts already server-side beats N chunked IN(...) round-
            # trips. Also sidesteps the SQL Server 2100-parameter cap that
            # made earlier IN(...) builds fail with pyodbc 07002. Temp
            # table is connection-scoped, so CREATE/INSERT/SELECT/DROP
            # must share one Connection.
            #
            # Hardening for the on-prem SQL Server we run against (one
            # giant 27K-row fast_executemany batch fired pyodbc 10054
            # "connection forcibly closed" under MSA-calc load on
            # 2026-06-11 — local SQL Server killed the session, not
            # Azure):
            #   - INSERT chunked at 5000 rows/batch so each
            #     fast_executemany TDS RPC stays a small write.
            #   - Retry on transient connect-drop errors
            #     (10054 / 10053 / 08S01 / "Communication link") so a
            #     one-shot socket reset doesn't strand the build.
            import uuid as _uuid
            import time as _time
            unique_arts = list({v for v in cleaned})
            INSERT_BATCH = 5000
            MAX_ATTEMPTS = 3

            def _is_transient(err: Exception) -> bool:
                s = str(err)
                return any(code in s for code in
                           ("10054", "10053", "08S01", "Communication link"))

            df = pd.DataFrame()
            last_err: Optional[Exception] = None
            for attempt in range(1, MAX_ATTEMPTS + 1):
                tmp = f"#ga_{_uuid.uuid4().hex[:8]}"
                try:
                    with self.db.bind.connect() as conn:
                        conn.execute(text(
                            f"CREATE TABLE {tmp} ("
                            f"gen_art NVARCHAR(50) COLLATE DATABASE_DEFAULT "
                            f"NOT NULL PRIMARY KEY)"
                        ))
                        try:
                            raw_cur = conn.connection.cursor()
                            try:
                                try:
                                    raw_cur.fast_executemany = True
                                except Exception:
                                    pass
                                for i in range(0, len(unique_arts), INSERT_BATCH):
                                    batch = unique_arts[i:i + INSERT_BATCH]
                                    raw_cur.executemany(
                                        f"INSERT INTO {tmp} (gen_art) "
                                        f"VALUES (?)",
                                        [(v,) for v in batch],
                                    )
                            finally:
                                raw_cur.close()

                            df = pd.read_sql(text(f"""
                                SELECT DISTINCT {col_list}
                                FROM dbo.vw_master_product mp WITH (NOLOCK)
                                INNER JOIN {tmp} t
                                  ON t.gen_art = mp.GEN_ART_NUMBER
                            """), conn)
                        finally:
                            try:
                                conn.execute(text(f"DROP TABLE {tmp}"))
                            except Exception:
                                pass
                    break  # success
                except Exception as e:
                    last_err = e
                    if attempt < MAX_ATTEMPTS and _is_transient(e):
                        sleep_s = 0.5 * (2 ** (attempt - 1))
                        logger.warning(
                            f"[msa] _load_master_variants transient error on "
                            f"attempt {attempt}/{MAX_ATTEMPTS} "
                            f"({type(e).__name__}); retrying in {sleep_s}s"
                        )
                        _time.sleep(sleep_s)
                        continue
                    raise

            # Coerce join keys to clean strings — same defensive pattern as
            # ARS_PEND / HOLD merges, prevents int64/object dtype-mismatch
            # silent misses.
            for k in ("ARTICLE_NUMBER", "GEN_ART_NUMBER", "MAJ_CAT", "CLR"):
                if k in df.columns:
                    df[k] = df[k].astype(str).str.strip()

            logger.info(
                f"[msa] _load_master_variants: pulled {len(df)} variant rows "
                f"for {len(cleaned)} GEN_ART_NUMBERs"
            )
            return df
        except Exception as e:
            logger.warning(f"[msa] _load_master_variants failed: {e}")
            self.warnings.append(
                "Step 7.5: could not load master variants from "
                f"vw_master_product — backfill skipped. "
                f"Missing zero-stock variants will not appear in this run. "
                f"Cause: {type(e).__name__}: {str(e)[:200]}"
            )
            return pd.DataFrame()

    # ========================================================================
    # Data Discovery Methods
    # ========================================================================

    def get_available_columns(self) -> List[str]:
        """
        Get all available columns from the MSA view
        
        Returns:
            List of column names
        """
        try:
            sql = f"SELECT TOP 1 * FROM {self.main_table}"
            df = pd.read_sql(text(sql), self.db.bind)
            columns = df.columns.tolist()
            logger.info(f"Retrieved {len(columns)} columns from {self.main_table}")
            return columns
        except Exception as e:
            logger.error(f"Error getting columns: {str(e)}")
            return []

    def get_available_dates(self) -> List[str]:
        """
        Get distinct dates from the MSA view (sorted DESC)
        
        Returns:
            List of dates as strings (YYYY-MM-DD)
        """
        try:
            # Try common date column names
            sql = f"""
            SELECT DISTINCT 
                CAST([DATE] AS DATE) as date_val
            FROM {self.main_table}
            WHERE [DATE] IS NOT NULL
            ORDER BY date_val DESC
            """
            df = pd.read_sql(text(sql), self.db.bind)
            dates = [str(d.date()) for d in df['date_val']]
            logger.info(f"Retrieved {len(dates)} distinct dates")
            return dates
        except Exception as e:
            logger.warning(f"Error getting dates from {self.main_table}: {str(e)}")
            # Fallback: return last 30 days
            from datetime import datetime, timedelta
            dates = [(datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(30)]
            logger.info(f"Using fallback dates (last 30 days), returned {len(dates)} dates")
            return dates

    def get_source_data_date(self) -> Optional[str]:
        """Return max DATE from the source table ET_MSA_STK."""
        try:
            row = self.db.execute(text(
                "SELECT MAX(CAST([DATE] AS DATE)) FROM ET_MSA_STK WHERE [DATE] IS NOT NULL"
            )).fetchone()
            if row and row[0]:
                return str(row[0]) if isinstance(row[0], str) else row[0].isoformat()
        except Exception as e:
            logger.warning(f"Could not get max date from ET_MSA_STK: {e}")
        return None

    def get_distinct_values(
        self,
        column: str,
        date_filter: Optional[str] = None,
        additional_filters: Optional[Dict[str, List[str]]] = None
    ) -> List[str]:
        """
        Get distinct values for a column with cascading support
        
        Args:
            column: Column name
            date_filter: Optional date filter (YYYY-MM-DD)
            additional_filters: Optional dict for cascading filters
                               Example: {'ST_CD': ['DH24', 'DH25'], 'SLOC': ['V01']}
            
        Returns:
            List of distinct values (as strings, filtered for non-null/non-nan)
        """
        try:
            # Validate column name to prevent SQL injection
            if not self._is_valid_column_name(column):
                raise ValueError(f"Invalid column name: {column}")

            where_conditions = [f"[{column}] IS NOT NULL"]
            params = {}
            param_index = 0
            
            if date_filter:
                where_conditions.append(f"CAST([DATE] AS DATE) = :date_filter")
                params["date_filter"] = date_filter
            
            # Add cascading filters
            if additional_filters:
                logger.debug(f"🔗 Adding cascading filters: {additional_filters}")
                for filter_col, filter_values in additional_filters.items():
                    if filter_col == column:
                        continue  # Skip filtering on the same column
                    
                    if not self._is_valid_column_name(filter_col):
                        logger.warning(f"⚠️ Skipping invalid filter column: {filter_col}")
                        continue
                    
                    if filter_values and isinstance(filter_values, list) and len(filter_values) > 0:
                        placeholders = []
                        for val in filter_values:
                            param_key = f"filter_{param_index}"
                            params[param_key] = val
                            placeholders.append(f":{param_key}")
                            param_index += 1
                        
                        filter_clause = f"[{filter_col}] IN ({','.join(placeholders)})"
                        where_conditions.append(filter_clause)
                        logger.debug(f"✅ Added cascading filter: {filter_col} IN ({','.join(filter_values)})")
            
            where_clause = " AND ".join(where_conditions)

            sql = f"""
            SELECT DISTINCT [{column}]
            FROM {self.main_table}
            WHERE {where_clause}
            ORDER BY [{column}]
            """
            logger.debug(f"🔍 Executing SQL: {sql}")
            logger.debug(f"📋 With params: {params}")
            
            df = pd.read_sql(text(sql), self.db.bind, params=params)
            logger.debug(f"📊 Query returned {len(df)} rows")

            if df.empty:
                # Empty result is a real signal — the view exists but contains
                # no rows matching the filter. Return [] so the UI shows an
                # empty dropdown instead of fake "DH24, DH25" values that hide
                # the real state of the data.
                logger.warning(
                    f"⚠️ No data returned for column {column} from {self.main_table}"
                )
                return []

            values = df[column].astype(str).tolist()
            # Filter empty and nan values
            values = [v for v in values if v and v.lower() != 'nan' and v.strip()]
            logger.info(f"✅ Retrieved {len(values)} distinct values for {column}: {values[:10]}")
            return values
        except Exception as e:
            # Don't return fake fallback values. Previously this returned a
            # hard-coded ["DH24","DH25",...] list, which masked DB outages
            # (rotated credentials, network issues, etc.) — operations team
            # had no signal until users hit a 500 mid-run. Now the exception
            # propagates so the caller can return HTTP 500 with a real error.
            logger.error(
                f"❌ Error getting distinct values for {column}: {str(e)}",
                exc_info=True,
            )
            raise

    # ========================================================================
    # Filtering & Data Loading
    # ========================================================================

    def apply_filters(
        self, 
        date: str, 
        filters: Dict[str, List[str]]
    ) -> Tuple[pd.DataFrame, float]:
        """
        Apply filters to MSA data and load into DataFrame
        Limits to 500k rows to avoid memory issues
        Logs extensive debugging information
        
        Args:
            date: Date filter (YYYY-MM-DD)
            filters: Dict of column names to list of values
                    Example: {'SLOC': ['DC01', 'DC02'], 'CLR': ['RED']}
        
        Returns:
            Tuple of (filtered_dataframe, total_stock_qty)
        """
        try:
            logger.info(f"🔍 apply_filters called with date='{date}', filters={filters}")
            
            # DIAGNOSTIC: Check if date has any data at all
            if date:
                try:
                    diagnostic_sql = f"SELECT COUNT(*) as row_count FROM {self.main_table} WHERE CAST([DATE] AS DATE) = :test_date"
                    diag_df = pd.read_sql(text(diagnostic_sql), self.db.bind, params={"test_date": date})
                    diag_count = diag_df['row_count'].iloc[0]
                    logger.info(f"🔎 DIAGNOSTIC: Found {diag_count} total rows for date '{date}' in {self.main_table}")
                    
                    if diag_count == 0:
                        logger.warning(f"⚠️ DIAGNOSTIC: No data found for date '{date}' - check if date format is correct")
                        logger.info(f"   Date format expected: YYYY-MM-DD (e.g., 2026-03-03)")
                except Exception as diag_err:
                    logger.warning(f"⚠️ DIAGNOSTIC query failed: {str(diag_err)}")
            
            where_clauses = []
            params = {}

            # Add date filter
            if date:
                where_clauses.append("CAST([DATE] AS DATE) = :selected_date")
                params["selected_date"] = date
                logger.info(f"✅ Added date filter: '{date}'")
            else:
                logger.warning("⚠️ No date provided - will return all data")

            # Add column filters
            filter_count = 0
            for col, values in filters.items():
                if values and isinstance(values, list) and len(values) > 0:
                    logger.info(f"📋 Processing filter column '{col}' with {len(values)} values: {values}")
                    placeholders = ",".join([f":{col}_{i}" for i in range(len(values))])
                    where_clauses.append(f"[{col}] IN ({placeholders})")
                    for i, val in enumerate(values):
                        params[f"{col}_{i}"] = val
                    filter_count += 1
                else:
                    logger.debug(f"⏭️  Skipping filter column '{col}' - empty or not a list")

            logger.info(f"📊 Total filter columns to apply: {filter_count}")

            where_sql = ""
            if where_clauses:
                where_sql = " WHERE " + " AND ".join(where_clauses)
                logger.info(f"✅ Built WHERE clause: {where_sql}")
            else:
                logger.warning("⚠️ No where clauses built - will return all data")

            # Load data — select only columns needed for calculation (not SELECT *)
            # This significantly reduces memory usage and network transfer
            needed_cols = [
                "ST_CD", "SLOC", "SEG", "MAJ_CAT", "GEN_ART_NUMBER",
                "CLR", "SZ", "STK_Q", "ARTICLE_NUMBER",
                "M_VND_NM", "M_VND_CD", "MACRO_MVGR", "MICRO_MVGR",
                "FAB", "MVGR_MATRIX", "SSN", "DATE",
                "SUB_DIV", "DIV", "MRP", "RSP",
            ]
            
            # Build column list — only include columns that exist in the view
            try:
                # Quick check for available columns
                sample = pd.read_sql(text(f"SELECT TOP 1 * FROM {self.main_table}"), self.db.bind)
                available = set(sample.columns)
                select_cols = [c for c in needed_cols if c in available]
                # Add any remaining columns that might be needed
                extra_cols = [c for c in available if c not in select_cols]
                select_cols.extend(extra_cols)
                col_list = ", ".join(f"[{c}]" for c in select_cols)
            except Exception:
                col_list = "*"

            sql = f"""
            SELECT  {col_list}
            FROM {self.main_table}
            {where_sql}
            """
            
            logger.info(f"📝 Executing SQL query with params: {params}")
            logger.debug(f"Full SQL:\n{sql}")

            df = pd.read_sql(text(sql), self.db.bind, params=params)
            logger.info(f"✅ Query executed. Loaded {len(df)} rows")

            if len(df) == 0:
                logger.warning(f"⚠️ Query returned 0 rows!")
                logger.warning(f"   This may indicate:")
                logger.warning(f"   - Date '{date}' has no matching data")
                logger.warning(f"   - Filter values don't exist for this date")
                logger.warning(f"   - Date format is incorrect (expected YYYY-MM-DD)")

            # Check DataFrame size
            import sys
            df_memory_mb = df.memory_usage(deep=True).sum() / (1024 * 1024)
            logger.info(f"💾 DataFrame memory usage: {df_memory_mb:.2f}MB for {len(df)} rows")
            
            if df_memory_mb > 500:
                logger.warning(f"⚠️ DataFrame large ({df_memory_mb:.2f}MB) - consider limiting row count")

            # Calculate total stock qty
            total_stock_qty = 0.0
            if "STK_Q" in df.columns:
                try:
                    total_stock_qty = pd.to_numeric(df["STK_Q"], errors="coerce").sum()
                    logger.info(f"💰 Total STK_Q calculated: {total_stock_qty}")
                except Exception as calc_err:
                    logger.warning(f"⚠️ Error calculating STK_Q: {str(calc_err)}")
            else:
                available_cols = df.columns.tolist() if len(df) > 0 else "N/A"
                logger.warning(f"⚠️ STK_Q column not found. Available columns: {available_cols}")

            logger.info(f"✅ apply_filters complete: {len(df)} rows, STK_Q: {total_stock_qty}")
            return df, float(total_stock_qty)

        except Exception as e:
            logger.error(f"❌ Error in apply_filters: {str(e)}", exc_info=True)
            logger.error(f"   Date: '{date}'")
            logger.error(f"   Filters: {filters}")
            logger.error(f"   Params built: {params if 'params' in locals() else 'N/A'}")
            raise

    # ========================================================================
    # MSA Calculation Logic
    # ========================================================================

    def calculate(
        self,
        df: pd.DataFrame,
        slocs: Optional[List[str]] = None,
        threshold: int = 25
    ) -> Dict[str, Any]:
        """
        Calculate MSA allocation from filtered data - matches Streamlit logic exactly
        
        MSA Logic:
        1. Filter by SLOC if provided
        2. Normalize numeric values
        3. Fill missing dimensions with defaults
        4. Filter by SEG = ['APP', 'GM']
        5. Pivot by SLOC to get store-level stock
        6. Load and merge pending allocations
        7. Calculate final quantity = Stock - Pending
        8. Generate color variants based on threshold
        9. Aggregate to generated colors (hierarchy vs metrics)
        
        Args:
            df: Filtered DataFrame from VW_ET_MSA_STK_WITH_MASTER
            slocs: List of SLOC codes to include (None = all)
            threshold: Minimum color total for inclusion (default 25)
        
        Returns:
            Dict with keys: msa, msa_gen_clr, msa_gen_clr_var, row_counts
        """
        try:
            logger.info(f"Starting MSA calculation: {len(df)} rows, threshold={threshold}")
            
            if df.empty:
                logger.warning("DataFrame is empty, returning empty results")
                return {
                    "msa": [],
                    "msa_gen_clr": [],
                    "msa_gen_clr_var": [],
                    "row_counts": {"msa": 0, "msa_gen_clr": 0, "msa_gen_clr_var": 0},
                    "warnings": list(self.warnings),
                }

            msa = df.copy()
            
            
            

            # ============ STEP 1: FILTER SLOCS ============
            if slocs and "SLOC" in msa.columns:
                msa = msa[msa["SLOC"].isin(slocs)]
                logger.info(f"Filtered to {len(msa)} rows for SLOCs: {slocs}")

            if msa.empty:
                logger.warning("No data after SLOC filtering")
                return {
                    "msa": [],
                    "msa_gen_clr": [],
                    "msa_gen_clr_var": [],
                    "row_counts": {"msa": 0, "msa_gen_clr": 0, "msa_gen_clr_var": 0}
                }

            # ============ STEP 2: NUMERIC SAFETY ============
            if "STK_Q" in msa.columns:
                msa["STK_Q"] = pd.to_numeric(msa["STK_Q"], errors="coerce").fillna(0)
            
            # ============ STEP 3: SAFE DEFAULT FILL (BEFORE PIVOT) ============
            fill_defaults = {
                "CLR": "A",
                "M_VND_NM": "NA",
                "MACRO_MVGR": "NA",
                "MICRO_MVGR": "NA",
                "FAB": "NA",
                "MVGR_MATRIX": "NA",
                "SZ": "A",
                "M_VND_CD": 0,
                "SSN": "NA",
            }

            for col, val in fill_defaults.items():
                if col in msa.columns:
                    msa[col] = (
                        msa[col]
                        .replace(["", " ", "0", "nan", "None"], np.nan)
                        .fillna(val)
                    )
            
            # ============ STEP 4: SEG FILTER ============
            if "SEG" in msa.columns:
                seg_filter = ["APP", "GM"]
                msa = msa[msa["SEG"].isin(seg_filter)]
                logger.info(f"After SEG filter {seg_filter}: {len(msa)} rows")
            else:
                # Reaching here means the SEG column is absent — typically when
                # an upstream MAJ_CAT/RLS filter excluded all APP/GM rows. The
                # previous log line referenced msa['SEG'].nunique() inside this
                # else branch, which raised KeyError: 'SEG' and killed the job.
                logger.info("No SEG column present — SEG filter skipped")

            # ============ STEP 4b: CATEGORY RLS FILTER ============
            # If user has category restrictions, filter to only their assigned MAJ_CATs
            # This is set by the caller (msa_stock.py endpoint) via rls_categories param
            if hasattr(self, '_rls_categories') and self._rls_categories:
                if "MAJ_CAT" in msa.columns:
                    before = len(msa)
                    msa = msa[msa["MAJ_CAT"].isin(self._rls_categories)]
                    logger.info(f"Category RLS filter: {before} → {len(msa)} rows (categories: {self._rls_categories})")
                else:
                    logger.warning("MAJ_CAT column not found — category filter skipped")


            # ============ STEP 5: PIVOT MSA BY SLOC ============
            # Normalize DATE to midnight (date-only) before it joins pivot_keys.
            # If DATE arrives as a timestamp, two snapshots of the same SLOC on
            # the same day (e.g. 09:00 and 23:00) become separate pivot keys —
            # producing duplicate rows for one (article, store, day) and
            # corrupting the SEG/CLR aggregates downstream.
            if "DATE" in msa.columns:
                try:
                    msa["DATE"] = pd.to_datetime(
                        msa["DATE"], errors="coerce"
                    ).dt.normalize()
                except Exception as _date_err:
                    logger.warning(
                        f"DATE normalization skipped (pivot may double-row "
                        f"if same-day timestamps exist): {_date_err}"
                    )

            pivot_keys = [c for c in msa.columns if c not in ["SLOC", "STK_Q"]]

            msa_pivot = (
                msa.pivot_table(
                    index=pivot_keys,
                    columns="SLOC",
                    values="STK_Q",
                    aggfunc="sum",
                    fill_value=0
                )
                .reset_index()
            )

            # Calculate total stock across all SLOCs
            sloc_cols = [c for c in msa_pivot.columns if c not in pivot_keys]
            msa_pivot["STK_QTY"] = msa_pivot[sloc_cols].sum(axis=1)
            logger.info(f"Pivoted table: {len(msa_pivot)} rows, {len(sloc_cols)} SLOCs")

            # Rename ST_CD → RDC right after the pivot. All downstream steps
            # (universe backfill, PEND/HOLD merges, threshold filter,
            # aggregation) operate on the canonical warehouse axis name.
            if "ST_CD" in msa_pivot.columns:
                msa_pivot.rename(columns={"ST_CD": "RDC"}, inplace=True)

            # Seed obligation columns to zero. Step 6 adds zero-stock
            # placeholder rows for universe expansion; Steps 7-8 stamp the
            # real PEND/HOLD values onto every (existing + backfilled) row.
            msa_pivot["PEND_QTY"] = 0.0
            # ARS_PEND is brought in by the Step 7 merge — pre-creating it
            # would force pandas to emit ARS_PEND_x / ARS_PEND_y on merge.
            pend_merged_cols: List[str] = []

            # ============ STEP 6: UNIVERSE BACKFILL ============
            # Discover the (RDC, GEN_ART_NUMBER) universe via _load_universe
            # — union of three sources:
            #   A. Stock in the SELECTED SLOCs only (anchors stocked GEN_ARTs)
            #   B. Open ARS_PEND_ALC rows  (anchors pend-only GEN_ARTs)
            #   C. Open ARS_NL_TBL_HOLD_TRACKING rows (anchors hold-only)
            # For every (RDC, GEN_ART) in that union, fetch every master
            # VAR_ART from vw_master_product and insert any variant missing
            # from msa_pivot as a zero-stock placeholder row. Run BEFORE
            # the PEND/HOLD merges so the backfilled rows pick up their
            # real PEND/HOLD values automatically — no special-casing.
            #
            # The universe is NOT "every article anywhere". Stock contribution
            # stays scoped to the user's SLOC selection. It only expands when
            # a real obligation (PEND or HOLD) exists for the (RDC, GEN_ART).
            try:
                date_filter_for_universe: Optional[str] = None
                if "DATE" in msa_pivot.columns:
                    try:
                        _d = pd.to_datetime(
                            msa_pivot["DATE"], errors="coerce"
                        ).max()
                        if pd.notna(_d):
                            date_filter_for_universe = _d.date().isoformat()
                    except Exception:
                        date_filter_for_universe = None

                universe = self._load_universe(slocs, date_filter_for_universe)

                if (
                    not universe.empty
                    and "GEN_ART_NUMBER" in msa_pivot.columns
                    and "ARTICLE_NUMBER" in msa_pivot.columns
                ):
                    # Coerce join keys before any merge
                    msa_pivot["RDC"] = (
                        msa_pivot["RDC"].astype(str).str.strip()
                    )
                    msa_pivot["GEN_ART_NUMBER"] = (
                        msa_pivot["GEN_ART_NUMBER"].astype(str).str.strip()
                    )
                    msa_pivot["ARTICLE_NUMBER"] = (
                        msa_pivot["ARTICLE_NUMBER"].astype(str).str.strip()
                    )

                    gen_arts_in_universe = (
                        universe["GEN_ART_NUMBER"].unique().tolist()
                    )
                    master_df = self._load_master_variants(gen_arts_in_universe)

                    if not master_df.empty:
                        # Clean dtypes on master join keys
                        for k in ("ARTICLE_NUMBER", "GEN_ART_NUMBER",
                                  "MAJ_CAT", "CLR"):
                            if k in master_df.columns:
                                master_df[k] = (
                                    master_df[k].astype(str).str.strip()
                                )

                        # expanded = every (RDC × GEN_ART × master variants)
                        expanded = universe.merge(
                            master_df, on="GEN_ART_NUMBER", how="left"
                        )
                        # Drop universe rows where master returned nothing
                        # (GEN_ART truly absent from vw_master_product)
                        expanded = expanded[
                            expanded["ARTICLE_NUMBER"].notna()
                        ]

                        if not expanded.empty:
                            # Anti-join: keep only rows not already in pivot
                            existing = (
                                msa_pivot[["RDC", "ARTICLE_NUMBER"]]
                                .drop_duplicates()
                            )
                            anti = expanded.merge(
                                existing.assign(_present=1),
                                on=["RDC", "ARTICLE_NUMBER"],
                                how="left",
                            )
                            missing = anti[anti["_present"].isna()].drop(
                                columns=["_present"]
                            )

                            if not missing.empty:
                                # Columns on pivot but not on master need
                                # defaults:
                                #   numeric (SLOC qty + quantity columns)
                                #     → 0
                                #   non-numeric (DATE, SEG, MRP, …)
                                #     → copy from sibling row at the same
                                #       (RDC, GEN_ART_NUMBER); NaN if none.
                                pivot_only_cols = [
                                    c for c in msa_pivot.columns
                                    if c not in missing.columns
                                ]

                                sibling = (
                                    msa_pivot
                                    .drop_duplicates(
                                        subset=["RDC", "GEN_ART_NUMBER"]
                                    )
                                    .set_index(["RDC", "GEN_ART_NUMBER"])
                                )

                                missing_idx = missing.set_index(
                                    ["RDC", "GEN_ART_NUMBER"]
                                )
                                for c in pivot_only_cols:
                                    if pd.api.types.is_numeric_dtype(
                                        msa_pivot[c]
                                    ):
                                        missing_idx[c] = 0
                                    elif c in sibling.columns:
                                        missing_idx[c] = sibling[c]
                                    else:
                                        missing_idx[c] = pd.NA
                                missing = missing_idx.reset_index()

                                # Quantity cols MUST start at zero; the
                                # Step 7-8 merges below will stamp the real
                                # PEND / HOLD on top of these rows.
                                for c in (
                                    "STK_QTY", "PEND_QTY", "HOLD_QTY",
                                    "FNL_Q", "ARS_PEND",
                                ):
                                    if c in msa_pivot.columns:
                                        missing[c] = 0

                                # Align column order to msa_pivot
                                missing = missing[msa_pivot.columns]

                                msa_pivot = pd.concat(
                                    [msa_pivot, missing], ignore_index=True
                                )
                                logger.info(
                                    f"[msa] Step 6 universe-backfill: "
                                    f"+{len(missing)} variant rows from "
                                    f"vw_master_product across "
                                    f"{len(universe)} (RDC, GEN_ART) keys"
                                )
                            else:
                                logger.info(
                                    "[msa] Step 6 universe-backfill: "
                                    "no missing variants — pivot already "
                                    "covers the universe"
                                )
                        else:
                            logger.info(
                                "[msa] Step 6 universe-backfill: master "
                                "returned no rows for universe GEN_ARTs "
                                "— skipping backfill"
                            )
                            if not any(
                                "Step 6" in w for w in self.warnings
                            ):
                                self.warnings.append(
                                    "Step 6: vw_master_product had no rows"
                                    " for the universe GEN_ART_NUMBERs — "
                                    "backfill skipped."
                                )
                    else:
                        logger.info(
                            "[msa] Step 6 universe-backfill: master loader"
                            " returned empty — skipping backfill"
                        )
                else:
                    logger.info(
                        "[msa] Step 6 universe-backfill: empty universe "
                        "(or pivot missing key cols) — skipping"
                    )
            except Exception as e:
                logger.warning(
                    f"[msa] Step 6 universe-backfill failed (skipping): "
                    f"{e}", exc_info=True
                )
                self.warnings.append(
                    f"Step 6 universe-backfill raised "
                    f"{type(e).__name__}: {str(e)[:200]}"
                )

            # ============ STEP 7: MERGE ARS_PEND_ALC → PEND_QTY ============
            # Stamp PEND onto every row (existing + universe-backfilled)
            # by joining ARS_PEND_ALC on (RDC, ARTICLE_NUMBER). Every
            # open pending obligation now has a row to land on, so
            # SUM(TOTAL.PEND_QTY) reconciles to SUM(ARS_PEND_ALC.PEND_QTY).
            ars_pend_loaded = False
            try:
                ars_pend = self._load_ars_pending()
                if (
                    not ars_pend.empty
                    and "ARTICLE_NUMBER" in msa_pivot.columns
                ):
                    pend_merged_cols = [
                        c for c in ars_pend.columns
                        if c not in ("RDC", "ARTICLE_NUMBER")
                    ]
                    # dtype safety — same pattern as the original code
                    msa_pivot["RDC"] = (
                        msa_pivot["RDC"].astype(str).str.strip()
                    )
                    msa_pivot["ARTICLE_NUMBER"] = (
                        msa_pivot["ARTICLE_NUMBER"].astype(str).str.strip()
                    )
                    ars_pend["RDC"] = (
                        ars_pend["RDC"].astype(str).str.strip()
                    )
                    ars_pend["ARTICLE_NUMBER"] = (
                        ars_pend["ARTICLE_NUMBER"].astype(str).str.strip()
                    )

                    msa_pivot = msa_pivot.merge(
                        ars_pend,
                        on=["RDC", "ARTICLE_NUMBER"],
                        how="left",
                    )
                    msa_pivot["ARS_PEND"] = msa_pivot["ARS_PEND"].fillna(0)
                    msa_pivot["PEND_QTY"] = msa_pivot["ARS_PEND"]
                    ars_pend_loaded = True

                    matched_pend = float(msa_pivot["ARS_PEND"].sum())
                    expected_pend = float(ars_pend["ARS_PEND"].sum())
                    logger.info(
                        f"[msa] Step 7 PEND merge: "
                        f"expected={expected_pend:.0f} "
                        f"matched={matched_pend:.0f} across "
                        f"{len(ars_pend)} (RDC,ARTICLE) keys"
                    )
                    if (
                        expected_pend > 0
                        and matched_pend < expected_pend * 0.99
                    ):
                        logger.warning(
                            f"[msa] PEND merge mismatch — expected "
                            f"{expected_pend:.0f} but only "
                            f"{matched_pend:.0f} landed on pivot. Check "
                            f"Step 6 universe coverage."
                        )
                else:
                    logger.info(
                        "[msa] Step 7 PEND merge: no open pending rows"
                    )
            except Exception as ars_err:
                logger.warning(
                    f"[msa] Step 7 PEND merge failed: {ars_err}"
                )

            if not ars_pend_loaded and "ARS_PEND" not in msa_pivot.columns:
                msa_pivot["ARS_PEND"] = 0.0

            # ============ STEP 8: MERGE HOLD_TRACKING → HOLD_QTY ============
            # Held units physically sit at the RDC but are reserved for a
            # specific store from a prior TBL/NL allocation. Subtracting
            # HOLD_QTY in Step 9 prevents double-allocation of physically-
            # shared warehouse stock.
            try:
                holds_pivot = self._load_open_holds()
                if (
                    not holds_pivot.empty
                    and "ARTICLE_NUMBER" in msa_pivot.columns
                ):
                    msa_pivot["RDC"] = (
                        msa_pivot["RDC"].astype(str).str.strip()
                    )
                    msa_pivot["ARTICLE_NUMBER"] = (
                        msa_pivot["ARTICLE_NUMBER"].astype(str).str.strip()
                    )
                    holds_pivot["RDC"] = (
                        holds_pivot["RDC"].astype(str).str.strip()
                    )
                    holds_pivot["ARTICLE_NUMBER"] = (
                        holds_pivot["ARTICLE_NUMBER"].astype(str).str.strip()
                    )

                    msa_pivot = msa_pivot.merge(
                        holds_pivot,
                        on=["RDC", "ARTICLE_NUMBER"],
                        how="left",
                    )
                    msa_pivot["HOLD_QTY"] = msa_pivot["HOLD_QTY"].fillna(0)

                    matched_hold = float(msa_pivot["HOLD_QTY"].sum())
                    expected_hold = float(holds_pivot["HOLD_QTY"].sum())
                    logger.info(
                        f"[msa] Step 8 HOLD merge: "
                        f"expected={expected_hold:.0f} "
                        f"matched={matched_hold:.0f} across "
                        f"{len(holds_pivot)} (RDC,ARTICLE) keys"
                    )
                    if (
                        expected_hold > 0
                        and matched_hold < expected_hold * 0.99
                    ):
                        logger.warning(
                            f"[msa] HOLD merge mismatch — expected "
                            f"{expected_hold:.0f} but only "
                            f"{matched_hold:.0f} landed on pivot."
                        )
                else:
                    msa_pivot["HOLD_QTY"] = 0
                    logger.info(
                        "[msa] Step 8 HOLD merge: no open holds"
                    )
            except Exception as hold_err:
                logger.warning(
                    f"[msa] Step 8 HOLD merge failed: {hold_err}"
                )
                if "HOLD_QTY" not in msa_pivot.columns:
                    msa_pivot["HOLD_QTY"] = 0

            # ============ STEP 9: COMPUTE FNL_Q ============
            # FNL_Q = max(STK − PEND − HOLD, 0)
            msa_pivot["FNL_Q"] = np.maximum(
                msa_pivot["STK_QTY"]
                - msa_pivot["PEND_QTY"]
                - msa_pivot["HOLD_QTY"],
                0,
            )
            logger.info("[msa] Step 9 FNL_Q computed")

            # ============ STEP 10: THRESHOLD FILTER (RELAXED) ============
            # Keep a (RDC, MAJ_CAT, GEN_ART_NUMBER, CLR) group if its
            # total ENGAGEMENT is meaningful, where
            #     engagement = sum(FNL_Q) + sum(PEND_QTY) + sum(HOLD_QTY).
            # Previously sum(FNL_Q) alone determined survival, which
            # dropped groups with zero free stock but real PEND/HOLD
            # obligations from VAR_ART/GEN_ART. With the universe-anchored
            # backfill in Step 6, pend-only and hold-only groups now have
            # rows in TOTAL; the relaxed threshold lets them survive into
            # VAR_ART/GEN_ART too.
            grp_cols = ["RDC", "MAJ_CAT", "GEN_ART_NUMBER", "CLR"]
            grp_cols = [c for c in grp_cols if c in msa_pivot.columns]

            if grp_cols:
                engagement = (
                    msa_pivot.groupby(grp_cols, dropna=False)
                    .agg(
                        _stk=("FNL_Q", "sum"),
                        _pnd=("PEND_QTY", "sum"),
                        _hld=("HOLD_QTY", "sum"),
                    )
                    .reset_index()
                )
                engagement["_eng"] = (
                    engagement["_stk"].astype(float)
                    + engagement["_pnd"].astype(float)
                    + engagement["_hld"].astype(float)
                )
                passing_keys = engagement[
                    engagement["_eng"] > threshold
                ][grp_cols].drop_duplicates()
                msa_gen_clr_var = msa_pivot.merge(
                    passing_keys, on=grp_cols, how="inner"
                )
                logger.info(
                    f"[msa] Step 10 threshold (relaxed, >{threshold}): "
                    f"{len(passing_keys)}/{len(engagement)} groups pass; "
                    f"{len(msa_gen_clr_var)} variant rows kept"
                )
            else:
                msa_gen_clr_var = msa_pivot.copy()
                logger.warning(
                    "[msa] Step 10 threshold: grp_cols empty — passing "
                    "all rows"
                )

            # ============ STEP 11: GENERATED COLORS (AGGREGATED) ============
            # Pin hierarchy to the canonical OPT grain. The previous dynamic
            # classifier inherited pivot_keys which silently admitted SZ-varying
            # numeric master-data columns (AVG_DENSITY, PAK_SZ, MRP, V02_FRESH, …)
            # into hierarchy_cols, splitting each color into 2-3 rows. The
            # downstream bootstrap UPDATE then stamped the full OPT FNL_Q onto
            # every duplicate row, inflating SUM(FNL_Q) per OPT by 2-3×.
            hierarchy_keys = ["RDC", "MAJ_CAT", "GEN_ART_NUMBER", "CLR"]
            hierarchy_cols = [c for c in hierarchy_keys if c in msa_gen_clr_var.columns]

            exclude_from_hierarchy = {"ARTICLE_NUMBER", "ARTICLE_DESC", "SZ"}

            # Stock-like cols sum across SZ; SZ-varying numeric master-data uses
            # max (so zero-stock SZ rows don't override the meaningful value);
            # descriptive strings (invariant within a color) take first.
            sloc_cols_list = [c for c in msa_pivot.columns
                if c not in pivot_keys + ["STK_QTY", "PEND_QTY", "HOLD_QTY", "FNL_Q", "RDC"]
                and pd.api.types.is_numeric_dtype(msa_gen_clr_var[c])]
            moa_cols_list = [c for c in pend_merged_cols
                if c not in ["ARTICLE_NUMBER", "RDC"]
                and c in msa_gen_clr_var.columns
                and pd.api.types.is_numeric_dtype(msa_gen_clr_var[c])]
            sum_cols = set(sloc_cols_list) | set(moa_cols_list) | {
                "STK_QTY", "PEND_QTY", "HOLD_QTY", "FNL_Q"
            }

            agg_map: Dict[str, str] = {}
            for col in msa_gen_clr_var.columns:
                if col in hierarchy_cols or col in exclude_from_hierarchy:
                    continue
                if col in sum_cols:
                    agg_map[col] = "sum"
                elif pd.api.types.is_numeric_dtype(msa_gen_clr_var[col]):
                    agg_map[col] = "max"
                else:
                    agg_map[col] = "first"

            logger.info(f"🔹 Hierarchy columns ({len(hierarchy_cols)}): {hierarchy_cols}")
            logger.info(f"🔹 Aggregate columns ({len(agg_map)}): {sorted(agg_map.keys())}")

            if hierarchy_cols and agg_map:
                msa_gen_clr = (
                    msa_gen_clr_var
                    .groupby(hierarchy_cols, as_index=False, dropna=False)
                    .agg(agg_map)
                    .reset_index(drop=True)
                )
                logger.info(f"Generated colors aggregated: {len(msa_gen_clr)} rows")
            else:
                msa_gen_clr = pd.DataFrame()
                logger.warning("Could not aggregate - using empty DataFrame")

            # ============ CONVERT TO DICTS AND RETURN ============
            # _df_to_native_records strips numpy/pandas scalar types via a
            # JSON round-trip so the FastAPI response can serialize cleanly
            # under Pydantic v2 — see the helper's docstring for details.
            msa_dict = _df_to_native_records(msa_pivot)

            gen_clr_dict = _df_to_native_records(msa_gen_clr)

            var_dict = _df_to_native_records(msa_gen_clr_var)
             # Debug: save color variant dict data

            logger.info(f"✅ MSA calculation complete:")
            logger.info(f"   MSA: {len(msa_dict)} rows")
            logger.info(f"   Generated Colors: {len(gen_clr_dict)} rows")
            logger.info(f"   Color Variants: {len(var_dict)} rows")

            return {
                "msa": msa_dict,
                "msa_gen_clr": gen_clr_dict,
                "msa_gen_clr_var": var_dict,
                "row_counts": {
                    "msa": len(msa_dict),
                    "msa_gen_clr": len(gen_clr_dict),
                    "msa_gen_clr_var": len(var_dict)
                },
                # Soft-failure messages from non-fatal steps (e.g. Step 7.5
                # backfill skipped). The endpoint passes this through and
                # the UI toasts each one so the user knows something didn't
                # complete cleanly even though the run "succeeded".
                "warnings": list(self.warnings),
            }

        except Exception as e:
            logger.error(f"❌ Error in MSA calculation: {str(e)}", exc_info=True)
            raise

    # ========================================================================
    # Pivot Table Generation
    # ========================================================================

    def generate_pivot(
        self,
        df: pd.DataFrame,
        index_cols: List[str],
        pivot_cols: List[str],
        value_cols: List[str],
        agg_funcs: List[str],
        fill_zero: bool = True,
        margin_totals: bool = False
    ) -> Dict[str, Any]:
        """
        Generate pivot table from data
        
        Args:
            df: DataFrame to pivot
            index_cols: Columns for index (rows)
            pivot_cols: Columns for pivot (columns)
            value_cols: Columns for values
            agg_funcs: Aggregation functions
            fill_zero: Fill missing with 0
            margin_totals: Add margin totals
        
        Returns:
            Dict with columns and data
        """
        try:
            if df.empty or not index_cols or not pivot_cols or not value_cols:
                logger.warning("Missing pivot parameters or empty data")
                return {"columns": [], "data": [], "row_count": 0}

            # Convert to string and numeric as needed
            for col in list(set(index_cols + pivot_cols + value_cols)):
                if col in df.columns:
                    if col in value_cols:
                        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
                    else:
                        df[col] = df[col].astype(str)

            # Generate pivot
            agg_param = agg_funcs[0] if len(agg_funcs) == 1 else agg_funcs
            pivot_table = pd.pivot_table(
                df,
                index=index_cols if index_cols else None,
                columns=pivot_cols if pivot_cols else None,
                values=value_cols,
                aggfunc=agg_param,
                fill_value=0 if fill_zero else np.nan,
                margins=margin_totals,
                margins_name="Total"
            )

            # Reset index
            pivot_df = pivot_table.reset_index() if hasattr(pivot_table, 'reset_index') else pivot_table

            # Flatten multi-index columns if needed
            if isinstance(pivot_df.columns, pd.MultiIndex):
                pivot_df.columns = ['_'.join(filter(None, map(str, col))).strip('_')
                                    for col in pivot_df.columns.values]

            logger.info(f"Pivot generated: {len(pivot_df)} rows x {len(pivot_df.columns)} columns")

            return {
                "columns": pivot_df.columns.tolist(),
                # Use _df_to_native_records for the same reason as calculate():
                # avoids numpy scalars in the response that trip pydantic_core.
                "data": _df_to_native_records(pivot_df),
                "row_count": len(pivot_df)
            }

        except Exception as e:
            logger.error(f"Error generating pivot: {str(e)}")
            raise

    # ========================================================================
    # Private Helper Methods
    # ========================================================================

    def _is_valid_column_name(self, column: str) -> bool:
        """Validate column name for SQL injection prevention"""
        import re
        return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", column))

    def _get_pending_allocation(self, msa_df: pd.DataFrame) -> pd.DataFrame:
        """
        Get pending allocations and aggregate by ARTICLE_NUMBER
        
        Args:
            msa_df: The MSA pivot table
        
        Returns:
            DataFrame with ARTICLE_NUMBER and PEND_QTY columns
        """
        try:
            if "ARTICLE_NUMBER" not in msa_df.columns:
                return pd.DataFrame()

            # Check if pending table exists
            sql = f"""SELECT TOP 1 * FROM {self.pending_table}"""
            try:
                pend_df = pd.read_sql(text(sql), self.db.bind)
                if pend_df.empty:
                    logger.info("No pending allocations found")
                    return pd.DataFrame()
            except Exception as e:
                logger.warning(f"Pending table not found or empty: {e}")
                return pd.DataFrame()

            # Load all pending data
            sql = f"""SELECT * FROM {self.pending_table}"""
            pend_df = pd.read_sql(text(sql), self.db.bind)

            if "QTY" in pend_df.columns:
                pend_df["QTY"] = pd.to_numeric(pend_df["QTY"], errors="coerce").fillna(0)

                # Aggregate by ARTICLE_NUMBER
                if "ARTICLE_NUMBER" in pend_df.columns:
                    pend_agg = pend_df.groupby("ARTICLE_NUMBER")["QTY"].sum().reset_index()
                    pend_agg.rename(columns={"QTY": "PEND_QTY"}, inplace=True)
                    logger.info(f"Aggregated {len(pend_agg)} pending allocation records")
                    return pend_agg

            return pd.DataFrame()

        except Exception as e:
            logger.warning(f"Error getting pending allocations: {e}")
            return pd.DataFrame()