"""
MSA Stock Calculation Service
Handles filtering, calculating, and pivoting MSA data
"""
import pandas as pd
import numpy as np
from sqlalchemy import text, MetaData, Table as SQLTable
from typing import Dict, List, Any, Optional, Tuple
from loguru import logger


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
                    "row_counts": {"msa": 0, "msa_gen_clr": 0, "msa_gen_clr_var": 0}
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

            # ============ STEP 6: DEDUCT ARS PENDING (ARS_PEND_ALC) ============
            # Units approved in an ARS run but whose SAP Delivery Orders have
            # not yet been created.  PEND_QTY = ALLOC_QTY − DO_QTY (only what
            # SAP has NOT yet committed). Prevents double-allocation of WH stock
            # before the DO is issued.  IS_CLOSED=1 rows are excluded — those
            # units are already deducted in SAP via DO.
            msa_pivot["PEND_QTY"] = 0.0
            msa_pivot["ARS_PEND"] = 0.0
            # pend_merged_cols tracks which columns were added by the PEND merge
            # so the column-classifier below (Step 9) can aggregate them, not use
            # them as hierarchy keys.
            pend_merged_cols: List[str] = []
            try:
                ars_pend = self._load_ars_pending()
                if not ars_pend.empty and "ARTICLE_NUMBER" in msa_pivot.columns:
                    pend_merged_cols = [
                        c for c in ars_pend.columns
                        if c not in ("RDC", "ARTICLE_NUMBER")
                    ]
                    # Force both join keys to str — pandas merge silently produces
                    # all-NaN matches when dtypes differ (int64 vs object). MSA
                    # pivot may infer ST_CD as int64 if all store codes are numeric;
                    # ARS_PEND_ALC.RDC comes from NVARCHAR → object. Without this
                    # cast, no rows match → fillna(0) → PEND_QTY = 0 → FNL_Q
                    # over-states pool by every store's actual pending qty.
                    msa_pivot["ST_CD"] = msa_pivot["ST_CD"].astype(str).str.strip()
                    msa_pivot["ARTICLE_NUMBER"] = msa_pivot["ARTICLE_NUMBER"].astype(str).str.strip()
                    ars_pend["RDC"] = ars_pend["RDC"].astype(str).str.strip()
                    ars_pend["ARTICLE_NUMBER"] = ars_pend["ARTICLE_NUMBER"].astype(str).str.strip()

                    msa_pivot = msa_pivot.merge(
                        ars_pend,
                        left_on=["ST_CD", "ARTICLE_NUMBER"],
                        right_on=["RDC", "ARTICLE_NUMBER"],
                        how="left",
                    )
                    msa_pivot["ARS_PEND"] = msa_pivot["ARS_PEND"].fillna(0)
                    msa_pivot.drop(columns=["RDC"], inplace=True, errors="ignore")
                    msa_pivot["PEND_QTY"] = msa_pivot["ARS_PEND"]

                    matched_pend = float(msa_pivot["ARS_PEND"].sum())
                    expected_pend = float(ars_pend["ARS_PEND"].sum())
                    logger.info(
                        f"Merged ARS pending: {len(ars_pend)} (RDC,ARTICLE) rows, "
                        f"total ARS_PEND={expected_pend:.0f}, "
                        f"matched into pivot={matched_pend:.0f}"
                    )
                    if expected_pend > 0 and matched_pend < expected_pend * 0.99:
                        logger.warning(
                            f"[msa] PEND merge mismatch — expected {expected_pend:.0f} "
                            f"but only {matched_pend:.0f} landed on pivot rows. "
                            f"Likely ST_CD/RDC value mismatch (whitespace, leading "
                            f"zeros, or unmapped warehouse keys)."
                        )
                else:
                    logger.info("ARS_PEND_ALC: no open pending rows — PEND_QTY = 0")
            except Exception as ars_err:
                logger.warning(f"Could not load ARS pending: {ars_err}")

            # ============ STEP 6.5: DEDUCT OPEN HOLDS (NL/TBL reservations) ====
            # Held units physically sit at the RDC but are reserved for a
            # specific store from a previous TBL/NL allocation. They must not
            # be re-offered to a different store on this run. Same shape as
            # the PEND merge above so downstream classification logic picks
            # HOLD_QTY up automatically.
            try:
                holds_pivot = self._load_open_holds()
                if (
                    not holds_pivot.empty
                    and "ARTICLE_NUMBER" in msa_pivot.columns
                ):
                    # Same dtype-coercion as the PEND merge above. Without this,
                    # holds silently fail to attach → HOLD_QTY = 0 → MSA reports
                    # reserved stock as available.
                    msa_pivot["ST_CD"] = msa_pivot["ST_CD"].astype(str).str.strip()
                    msa_pivot["ARTICLE_NUMBER"] = msa_pivot["ARTICLE_NUMBER"].astype(str).str.strip()
                    holds_pivot["RDC"] = holds_pivot["RDC"].astype(str).str.strip()
                    holds_pivot["ARTICLE_NUMBER"] = holds_pivot["ARTICLE_NUMBER"].astype(str).str.strip()

                    msa_pivot = msa_pivot.merge(
                        holds_pivot,
                        left_on=["ST_CD", "ARTICLE_NUMBER"],
                        right_on=["RDC", "ARTICLE_NUMBER"],
                        how="left",
                    )
                    msa_pivot["HOLD_QTY"] = msa_pivot["HOLD_QTY"].fillna(0)
                    msa_pivot.drop(columns=["RDC"], inplace=True, errors="ignore")

                    matched_hold = float(msa_pivot["HOLD_QTY"].sum())
                    expected_hold = float(holds_pivot["HOLD_QTY"].sum())
                    logger.info(
                        f"Merged open holds: {len(holds_pivot)} (RDC,ARTICLE) rows, "
                        f"total HOLD_QTY={expected_hold:.0f}, "
                        f"matched into pivot={matched_hold:.0f}"
                    )
                    if expected_hold > 0 and matched_hold < expected_hold * 0.99:
                        logger.warning(
                            f"[msa] HOLD merge mismatch — expected {expected_hold:.0f} "
                            f"but only {matched_hold:.0f} landed on pivot rows. "
                            f"Check ST_CD/RDC value consistency."
                        )
                else:
                    msa_pivot["HOLD_QTY"] = 0
                    logger.info("No open holds to merge")
            except Exception as hold_err:
                logger.warning(f"Could not load open holds: {hold_err}")
                msa_pivot["HOLD_QTY"] = 0

            # ============ STEP 7: CALCULATE FINAL QUANTITY ============
            # FNL_Q = max(STK − PEND − HOLD, 0)
            # PEND  = units approved in ARS (ARS_PEND_ALC) but DO not yet issued
            # HOLD  = units reserved for specific stores from prior TBL/NL runs
            #         (ARS_NL_TBL_HOLD_TRACKING). Subtracting both prevents
            #         double-allocation of physically-shared warehouse stock.
            msa_pivot["FNL_Q"] = np.maximum(
                msa_pivot["STK_QTY"] - msa_pivot["PEND_QTY"] - msa_pivot["HOLD_QTY"], 0
            )


            logger.info(f"Calculated FNL_Q (after PEND + HOLD deduction)")

           
            

            # ============ STEP 8: GENERATE COLOR VARIANTS (ROW LEVEL) ============
            grp_cols = ["ST_CD","MAJ_CAT", "GEN_ART_NUMBER", "CLR"]
            grp_cols = [c for c in grp_cols if c in msa_pivot.columns]

            if grp_cols:
                msa_gen_clr_var = msa_pivot[
                    msa_pivot.groupby(grp_cols)["FNL_Q"]
                    .transform("sum") > threshold
                ].copy()
                logger.info(f"Generated color variants: {len(msa_gen_clr_var)} rows (threshold={threshold})")
            else:
                msa_gen_clr_var = msa_pivot.copy()
                logger.warning("Could not determine hierarchy columns, using all rows")

            # ============ STEP 9: GENERATED COLORS (AGGREGATED) ============
            exclude_from_hierarchy = {
                "ARTICLE_NUMBER",
                "ARTICLE_DESC",
                "SZ"
            }

            hierarchy_cols = []
            aggregate_cols = []

            # Identify aggregate columns (SLOC columns + MOA columns + calculated columns)
            sloc_cols_list = [c for c in msa_pivot.columns
                if c not in pivot_keys + ["STK_QTY", "PEND_QTY", "HOLD_QTY", "FNL_Q", "RDC"]
                and pd.api.types.is_numeric_dtype(msa_gen_clr_var[c])]
            moa_cols_list = [c for c in pend_merged_cols
                if c not in ["ARTICLE_NUMBER", "RDC"]
                and c in msa_gen_clr_var.columns
                and pd.api.types.is_numeric_dtype(msa_gen_clr_var[c])]
            calculated_cols = ["STK_QTY", "PEND_QTY", "HOLD_QTY", "FNL_Q"]

            # Classify each column
            for col in msa_gen_clr_var.columns:
                if col in exclude_from_hierarchy:
                    continue
                
                if col in sloc_cols_list or col in moa_cols_list or col in calculated_cols:
                    aggregate_cols.append(col)
                else:
                    hierarchy_cols.append(col)

            logger.info(f"🔹 Hierarchy columns ({len(hierarchy_cols)}): {sorted(hierarchy_cols)}")
            logger.info(f"🔹 Aggregate columns ({len(aggregate_cols)}): {sorted(aggregate_cols)}")

            # Aggregate by hierarchy dimensions
            if hierarchy_cols and aggregate_cols:
                agg_map = {c: "sum" for c in aggregate_cols}
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

            # ============ RENAME ST_CD → RDC IN OUTPUT ============
            rename_map = {"ST_CD": "RDC"}
            msa_pivot.rename(columns=rename_map, inplace=True)
            if not msa_gen_clr.empty:
                msa_gen_clr.rename(columns=rename_map, inplace=True)
            msa_gen_clr_var.rename(columns=rename_map, inplace=True)

            # ============ CONVERT TO DICTS AND RETURN ============
            msa_dict = msa_pivot.where(pd.notna(msa_pivot), None).to_dict("records")

            gen_clr_dict = msa_gen_clr.where(pd.notna(msa_gen_clr), None).to_dict("records") if not msa_gen_clr.empty else []

            var_dict = msa_gen_clr_var.where(pd.notna(msa_gen_clr_var), None).to_dict("records")
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
                }
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

            # Replace NaN with None
            pivot_df = pivot_df.where(pd.notna(pivot_df), None)

            logger.info(f"Pivot generated: {len(pivot_df)} rows x {len(pivot_df.columns)} columns")

            return {
                "columns": pivot_df.columns.tolist(),
                "data": pivot_df.to_dict("records"),
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