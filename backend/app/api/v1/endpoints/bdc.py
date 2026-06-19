"""
BDC Creation API Endpoints
- Upload allocation quantity data (CSV/Excel)
- Process: join with VW_MASTER_PRODUCT, filter out hold/division/majcat exclusions
- Return BDC-format output ready for download
- Status upload: update ARS_ALLOCATION_MASTER with DO_QTY column
"""
import io
from typing import List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from loguru import logger

from app.database.session import get_data_db, get_data_engine
from app.security.dependencies import get_current_user
from app.models.rbac import User

router = APIRouter(prefix="/bdc", tags=["BDC Creation"])


def _read_file_to_df(content: bytes, filename: str, sheet_name: Optional[str] = None) -> pd.DataFrame:
    """Read CSV or Excel file bytes into a DataFrame."""
    lower = filename.lower()
    if lower.endswith(".csv"):
        df = pd.read_csv(io.BytesIO(content))
    elif lower.endswith((".xlsx", ".xls")):
        kwargs = {}
        if sheet_name:
            kwargs["sheet_name"] = sheet_name
        df = pd.read_excel(io.BytesIO(content), **kwargs)
    else:
        raise ValueError("Unsupported file format. Please upload CSV or Excel (.xlsx/.xls) files.")
    return df


def _process_bdc(df: pd.DataFrame, engine, allocation_no="", alloc_batch: str = "",
                 alloc_by_rdc: Optional[dict] = None) -> dict:
    """
    BDC Processing Pipeline:
    1. Aggregate PEND/NEW status rows: sum qty by VAR-ART + ST-CD + RDC, track pending qty
    2. Join uploaded data with VW_MASTER_PRODUCT on VAR-ART = ARTICLE_NUMBER
    3. Remove rows matching ARS_HOLD_ARTICLE_BDC (GEN_ART_NUMBER + CLR)
    4. Remove rows where store is in ARS_DIVISION_DELETE_BDC and DIV = 'KIDS'
    5. Remove rows where store + MAJ_CAT matches ARS_DIVISION_DELETE_ON_MAJ_CAT_BDC
    6. Build final BDC output format
    7. Build WITHOUT_PENDING data (total qty - pending qty)
    """
    stats = {
        "input_rows": len(df),
        "input_qty": 0,
        "after_master_join": 0,
        "after_master_join_qty": 0,
        "hold_article_removed": 0,
        "hold_article_removed_qty": 0,
        "division_delete_removed": 0,
        "division_delete_removed_qty": 0,
        "majcat_delete_removed": 0,
        "majcat_delete_removed_qty": 0,
        "final_rows": 0,
        "final_qty": 0,
        "alloc_batch": alloc_batch,
    }

    # Clean input - drop fully empty rows
    df = df.dropna(subset=["VAR-ART"]).copy()
    df["VAR-ART"] = df["VAR-ART"].astype("int64")

    # Validate STATUS column (PEND / NEW)
    if "STATUS" in df.columns:
        df["STATUS"] = df["STATUS"].astype(str).str.strip().str.upper()
        invalid = df[~df["STATUS"].isin(["PEND", "NEW"])]
        if len(invalid) > 0:
            raise ValueError(f"STATUS must be PEND or NEW. Found: {invalid['STATUS'].unique().tolist()}")
    else:
        df["STATUS"] = "NEW"

    # Aggregate PEND and NEW rows: group by VAR-ART + ST-CD + RDC
    # Sum total qty and track pending qty separately
    group_cols = ["ALLOC-DATE", "RDC", "VAR-ART", "ST-CD", "PICKING_DATE"]

    # Calculate pending qty per group
    pend_agg = df[df["STATUS"] == "PEND"].groupby(group_cols, as_index=False)["ALLOC-QTY"].sum().rename(columns={"ALLOC-QTY": "PEND-QTY"})

    # Aggregate total qty (PEND + NEW combined) per group
    total_agg = df.groupby(group_cols, as_index=False)["ALLOC-QTY"].sum()

    # Left join total with pending to get PEND-QTY per group
    merged = total_agg.merge(pend_agg, on=group_cols, how="left")
    merged["PEND-QTY"] = merged["PEND-QTY"].fillna(0).astype(int)
    merged["VAR-ART"] = merged["VAR-ART"].astype("int64")

    df = merged.copy()

    stats["input_rows"] = len(df)
    stats["input_qty"] = int(df["ALLOC-QTY"].sum())

    logger.info(f"BDC after aggregation: {len(df)} rows, {int(df['ALLOC-QTY'].sum())} qty, PEND total: {int(df['PEND-QTY'].sum())}")

    # Step 1: Join with VW_MASTER_PRODUCT to get ARTICLE_NUMBER, GEN_ART_NUMBER, DIV, MAJ_CAT, CLR
    article_numbers = df["VAR-ART"].unique().tolist()

    # Query in chunks to avoid SQL parameter limits
    chunk_size = 500
    master_parts = []
    with engine.connect() as conn:
        for i in range(0, len(article_numbers), chunk_size):
            chunk = article_numbers[i:i + chunk_size]
            placeholders = ",".join(str(int(a)) for a in chunk)
            query = text(f"""
                SELECT DISTINCT ARTICLE_NUMBER, GEN_ART_NUMBER, DIV, MAJ_CAT, CLR, MATNR
                FROM VW_MASTER_PRODUCT WITH (NOLOCK)
                WHERE ARTICLE_NUMBER IN ({placeholders})
            """)
            result = conn.execute(query)
            rows = result.fetchall()
            if rows:
                master_parts.append(pd.DataFrame(rows, columns=["ARTICLE_NUMBER", "GEN_ART_NUMBER", "DIV", "MAJ_CAT", "CLR", "MATNR"]))

    if not master_parts:
        raise ValueError("No matching articles found in VW_MASTER_PRODUCT for the uploaded data.")

    master_df = pd.concat(master_parts, ignore_index=True)
    master_df["ARTICLE_NUMBER"] = master_df["ARTICLE_NUMBER"].astype("int64")

    logger.info(f"BDC master lookup: {len(article_numbers)} unique articles, {len(master_df)} master matches")

    # Merge: input + master product
    combined = df.merge(
        master_df,
        left_on="VAR-ART",
        right_on="ARTICLE_NUMBER",
        how="inner",
    )
    stats["after_master_join"] = len(combined)
    stats["after_master_join_qty"] = int(combined["ALLOC-QTY"].sum())

    if combined.empty:
        raise ValueError("No matching articles found after joining with master product data.")

    # Step 2: Remove hold articles (ARS_HOLD_ARTICLE_BDC) by GEN_ART_NUMBER + CLR
    with engine.connect() as conn:
        result = conn.execute(text("SELECT GEN_ART_CLR, CLR FROM ARS_HOLD_ARTICLE_BDC WITH (NOLOCK)"))
        hold_rows = result.fetchall()

    if hold_rows:
        hold_df = pd.DataFrame(hold_rows, columns=["GEN_ART_CLR", "CLR_HOLD"])
        hold_df["GEN_ART_CLR"] = hold_df["GEN_ART_CLR"].astype(str).str.strip()
        hold_df["CLR_HOLD"] = hold_df["CLR_HOLD"].astype(str).str.strip()

        combined["_GEN_ART_STR"] = combined["GEN_ART_NUMBER"].astype(str).str.strip()
        combined["_CLR_STR"] = combined["CLR"].astype(str).str.strip()

        before = len(combined)
        before_qty = int(combined["ALLOC-QTY"].sum())
        combined = combined.merge(
            hold_df,
            left_on=["_GEN_ART_STR", "_CLR_STR"],
            right_on=["GEN_ART_CLR", "CLR_HOLD"],
            how="left",
            indicator=True,
        )
        combined = combined[combined["_merge"] == "left_only"].drop(columns=["GEN_ART_CLR", "CLR_HOLD", "_merge"])
        stats["hold_article_removed"] = before - len(combined)
        stats["hold_article_removed_qty"] = before_qty - int(combined["ALLOC-QTY"].sum())

    # Step 3: Remove KIDS division for stores in ARS_DIVISION_DELETE_BDC
    with engine.connect() as conn:
        result = conn.execute(text("SELECT STORE FROM ARS_DIVISION_DELETE_BDC WITH (NOLOCK)"))
        div_delete_rows = result.fetchall()

    if div_delete_rows:
        div_delete_stores = set(r[0].strip() for r in div_delete_rows)
        before = len(combined)
        before_qty = int(combined["ALLOC-QTY"].sum())
        mask = (combined["ST-CD"].str.strip().isin(div_delete_stores)) & (combined["DIV"].str.strip().str.upper() == "KIDS")
        combined = combined[~mask]
        stats["division_delete_removed"] = before - len(combined)
        stats["division_delete_removed_qty"] = before_qty - int(combined["ALLOC-QTY"].sum())

    # Step 4: Remove store + MAJ_CAT matches from ARS_DIVISION_DELETE_ON_MAJ_CAT_BDC
    with engine.connect() as conn:
        result = conn.execute(text("SELECT STORE, MAJCAT FROM ARS_DIVISION_DELETE_ON_MAJ_CAT_BDC WITH (NOLOCK)"))
        majcat_rows = result.fetchall()

    if majcat_rows:
        majcat_df = pd.DataFrame(majcat_rows, columns=["STORE", "MAJCAT"])
        majcat_df["STORE"] = majcat_df["STORE"].astype(str).str.strip()
        majcat_df["MAJCAT"] = majcat_df["MAJCAT"].astype(str).str.strip()

        before = len(combined)
        before_qty = int(combined["ALLOC-QTY"].sum())
        combined["_ST_CD_STR"] = combined["ST-CD"].astype(str).str.strip()
        combined["_MAJ_CAT_STR"] = combined["MAJ_CAT"].astype(str).str.strip()

        combined = combined.merge(
            majcat_df,
            left_on=["_ST_CD_STR", "_MAJ_CAT_STR"],
            right_on=["STORE", "MAJCAT"],
            how="left",
            indicator=True,
        )
        combined = combined[combined["_merge"] == "left_only"].drop(columns=["STORE", "MAJCAT", "_merge"])
        stats["majcat_delete_removed"] = before - len(combined)
        stats["majcat_delete_removed_qty"] = before_qty - int(combined["ALLOC-QTY"].sum())

    # Step 5: Build BDC output format (total qty = PEND + NEW)
    combined = combined.reset_index(drop=True)
    combined["Serial No"] = range(1, len(combined) + 1)
    combined["Allocation Date"] = pd.to_datetime(combined["ALLOC-DATE"]).dt.strftime("%Y-%m-%d")
    combined["VENDOR"] = combined["RDC"].astype(str).str.strip()
    # Per-RDC alloc# when provided (one allocation_number per warehouse); fall
    # back to the single `allocation_no` for legacy callers (e.g. /bdc/download
    # where the user supplies a number directly).
    if alloc_by_rdc:
        combined["Allocation Number"] = (
            combined["VENDOR"].map(alloc_by_rdc).fillna(allocation_no or "")
        )
    else:
        combined["Allocation Number"] = allocation_no
    combined["MATERIAL NO"] = combined["MATNR"].astype(str).str.strip().str.lstrip("0")
    combined["BDC-QTY"] = combined["ALLOC-QTY"].astype(int)
    combined["RECEIVING STORE"] = combined["ST-CD"].astype(str).str.strip()
    combined["Picking Date"] = pd.to_datetime(combined["PICKING_DATE"]).dt.strftime("%Y-%m-%d")
    combined["Remark"] = ""
    combined["ALLOC_BATCH"] = stats.get("alloc_batch", "")

    output = combined[["Serial No", "Allocation Date", "Allocation Number", "ALLOC_BATCH", "VENDOR", "MATERIAL NO", "BDC-QTY", "RECEIVING STORE", "Picking Date", "Remark"]].copy()

    stats["final_rows"] = len(output)
    stats["final_qty"] = int(output["BDC-QTY"].sum())

    # Step 6: Build WITHOUT_PENDING data (total qty - pending qty)
    wp = combined.copy()
    wp["PEND-QTY"] = wp["PEND-QTY"].fillna(0).astype(int)
    wp["BDC-QTY-WP"] = (wp["ALLOC-QTY"].astype(int) - wp["PEND-QTY"]).clip(lower=0)

    wp_output = wp[["Serial No", "Allocation Date", "Allocation Number", "ALLOC_BATCH", "VENDOR", "MATERIAL NO", "RECEIVING STORE", "Picking Date", "Remark"]].copy()
    wp_output["BDC-QTY"] = wp["BDC-QTY-WP"]
    wp_output = wp_output[["Serial No", "Allocation Date", "Allocation Number", "ALLOC_BATCH", "VENDOR", "MATERIAL NO", "BDC-QTY", "RECEIVING STORE", "Picking Date", "Remark"]]
    # Remove rows where qty became 0 after subtracting pending
    wp_output = wp_output[wp_output["BDC-QTY"] > 0].copy()
    wp_output = wp_output.reset_index(drop=True)
    wp_output["Serial No"] = range(1, len(wp_output) + 1)

    # Preview excludes ALLOC_BATCH (internal reference, not for SAP export)
    export_cols = [c for c in output.columns if c != 'ALLOC_BATCH']
    preview = output[export_cols].head(100).to_dict(orient="records")

    return {
        "success": True,
        "stats": stats,
        "total_rows": len(output),
        "columns": export_cols,
        "preview": preview,
        "_full_data": output,              # includes ALLOC_BATCH for DB save
        "_full_data_without_pending": wp_output,  # includes ALLOC_BATCH for DB save
    }


def _get_next_batch_no(engine) -> str:
    """Central batch sequence: FY-{3-digit serial}. e.g., 2526-001. One per upload, shared across stores."""
    fy = _get_fy_tag()
    prefix = f"{fy}-"
    table_name = "ARS_ALLOCATION_MASTER"
    with engine.connect() as conn:
        tbl_exists = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME=:t"
        ), {"t": table_name}).scalar()
        if tbl_exists:
            # Check if ALLOC_BATCH column exists
            has_col = conn.execute(text(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME=:t AND COLUMN_NAME='ALLOC_BATCH'"
            ), {"t": table_name}).scalar()
            if has_col:
                row = conn.execute(text(f"""
                    SELECT MAX([ALLOC_BATCH]) FROM dbo.{table_name} WITH (NOLOCK)
                    WHERE [ALLOC_BATCH] LIKE :p
                """), {"p": prefix + "%"}).scalar()
                if row:
                    try:
                        last = int(str(row).split("-")[-1])
                        return f"{prefix}{last + 1:03d}"
                    except (ValueError, IndexError):
                        pass
    return f"{prefix}001"


def _get_fy_tag():
    """Get current financial year tag. FY runs April-March. e.g., Apr 2025 – Mar 2026 → '2526'."""
    from datetime import date
    today = date.today()
    fy_start = today.year if today.month >= 4 else today.year - 1
    fy_end = fy_start + 1
    return f"{fy_start % 100:02d}{fy_end % 100:02d}"


def _get_next_allocation_no(engine, rdc: Optional[str] = None) -> str:
    """Generate next allocation number.

    rdc=None  →  legacy `FY-NNN`   (e.g. `2526-001`).  Kept for callers
                 that issue one number for an entire multi-RDC upload.
    rdc='X'   →  per-RDC `FY-X-NNN` (e.g. `2526-DH24-001`).  Serial is
                 scoped per (FY, RDC) so each warehouse has its own
                 sequence — required for /bdc-generate splitting BDC files
                 by RDC + date.

    Both forms can coexist in `ARS_ALLOCATION_MASTER`. The pattern uses
    SQL Server LIKE without a trailing `%`, so `'2526-[0-9][0-9][0-9]'`
    matches exactly `2526-` + 3 digits and ignores any `2526-DH24-001`
    rows — and vice-versa.
    """
    table_name = "ARS_ALLOCATION_MASTER"
    fy = _get_fy_tag()
    if rdc:
        rdc_clean = "".join(c for c in str(rdc).strip().upper()
                            if c.isalnum())  # strip stray chars; keep ASCII
        if not rdc_clean:
            rdc_clean = "X"
        prefix = f"{fy}-{rdc_clean}-"
    else:
        prefix = f"{fy}-"

    # The counter must advance even if ARS_ALLOCATION_MASTER hasn't been
    # populated (it's only written by _save_to_db, which doesn't run for
    # every BDC code path). Without a fallback the function used to return
    # `{prefix}001` forever, and that collision broke insert_bdc_history's
    # readback (it queried by ALLOCATION_NUMBER, picking up prior ops'
    # history_ids). Now we take MAX across every place a BDC alloc-no can
    # be recorded: ARS_ALLOCATION_MASTER, ARS_BDC_HISTORY, and the
    # operations log. Whichever source has the highest serial wins.
    pat = prefix + "[0-9][0-9][0-9]"
    candidates: List[str] = []

    with engine.connect() as conn:
        for src_sql in (
            "SELECT MAX([Allocation Number]) FROM dbo.ARS_ALLOCATION_MASTER WITH (NOLOCK) "
            "WHERE [Allocation Number] LIKE :pat",
            "SELECT MAX(ALLOCATION_NUMBER) FROM dbo.ARS_BDC_HISTORY WITH (NOLOCK) "
            "WHERE ALLOCATION_NUMBER LIKE :pat",
            "SELECT MAX(OP_KEY) FROM dbo.ARS_PEND_ALC_OPERATIONS WITH (NOLOCK) "
            "WHERE OP_TYPE = 'BDC' AND OP_KEY LIKE :pat",
        ):
            try:
                row = conn.execute(text(src_sql), {"pat": pat}).scalar()
                if row:
                    candidates.append(str(row).strip())
            except Exception:
                # Missing table is fine — we union across whatever exists.
                pass

    if candidates:
        try:
            last_serial = max(int(c[-3:]) for c in candidates if c[-3:].isdigit())
            return f"{prefix}{last_serial + 1:03d}"
        except (ValueError, IndexError):
            pass

    return f"{prefix}001"


def _save_to_db(output_df: pd.DataFrame, engine):
    """Save BDC output to ARS_ALLOCATION_MASTER table."""
    table_name = "ARS_ALLOCATION_MASTER"

    with engine.connect() as conn:
        result = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :tbl"
        ), {"tbl": table_name})
        table_exists = result.scalar() > 0

        if not table_exists:
            conn.execute(text(f"""
                CREATE TABLE dbo.{table_name} (
                    [Serial No]          INT,
                    [Allocation Date]    VARCHAR(20),
                    [Allocation Number]  VARCHAR(50),
                    [ALLOC_BATCH]        VARCHAR(20),
                    [VENDOR]             VARCHAR(50),
                    [MATERIAL NO]        BIGINT,
                    [BDC-QTY]            INT,
                    [RECEIVING STORE]    VARCHAR(20),
                    [Picking Date]       VARCHAR(20),
                    [Remark]             VARCHAR(200),
                    [CREATED_AT]         DATETIME2 DEFAULT GETDATE()
                )
            """))
            conn.commit()
        else:
            # Add ALLOC_BATCH if missing
            has = conn.execute(text("SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME=:t AND COLUMN_NAME='ALLOC_BATCH'"), {"t": table_name}).scalar()
            if not has:
                conn.execute(text(f"ALTER TABLE dbo.{table_name} ADD [ALLOC_BATCH] VARCHAR(20) NULL"))
                conn.commit()
            # Migrate MATERIAL NO from VARCHAR to BIGINT if needed
            dtype = conn.execute(text(
                "SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME=:t AND COLUMN_NAME='MATERIAL NO'"
            ), {"t": table_name}).scalar()
            if dtype and dtype.lower() in ('varchar', 'nvarchar', 'char', 'nchar'):
                conn.execute(text(f"ALTER TABLE dbo.{table_name} ALTER COLUMN [MATERIAL NO] BIGINT"))
                conn.commit()

    save_df = output_df.copy()
    # Ensure MATERIAL NO is numeric before saving
    save_df["MATERIAL NO"] = pd.to_numeric(save_df["MATERIAL NO"], errors="coerce").astype("Int64")
    save_df.to_sql(table_name, engine, if_exists="append", index=False, schema="dbo")
    return True


def _save_to_db_without_pending(output_df: pd.DataFrame, engine):
    """Save BDC output (total qty minus pending qty) to ARS_ALLOCATION_MASTER_WITHOUT_PENDING."""
    table_name = "ARS_ALLOCATION_MASTER_WITHOUT_PENDING"

    with engine.connect() as conn:
        result = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :tbl"
        ), {"tbl": table_name})
        table_exists = result.scalar() > 0

        if not table_exists:
            conn.execute(text(f"""
                CREATE TABLE dbo.{table_name} (
                    [Serial No]          INT,
                    [Allocation Date]    VARCHAR(20),
                    [Allocation Number]  VARCHAR(50),
                    [ALLOC_BATCH]        VARCHAR(20),
                    [VENDOR]             VARCHAR(50),
                    [MATERIAL NO]        BIGINT,
                    [BDC-QTY]            INT,
                    [RECEIVING STORE]    VARCHAR(20),
                    [Picking Date]       VARCHAR(20),
                    [Remark]             VARCHAR(200),
                    [CREATED_AT]         DATETIME2 DEFAULT GETDATE()
                )
            """))
            conn.commit()
        else:
            has = conn.execute(text("SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME=:t AND COLUMN_NAME='ALLOC_BATCH'"), {"t": table_name}).scalar()
            if not has:
                conn.execute(text(f"ALTER TABLE dbo.{table_name} ADD [ALLOC_BATCH] VARCHAR(20) NULL"))
                conn.commit()
            # Migrate MATERIAL NO to BIGINT
            dtype = conn.execute(text(
                "SELECT DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME=:t AND COLUMN_NAME='MATERIAL NO'"
            ), {"t": table_name}).scalar()
            if dtype and dtype.lower() in ('varchar', 'nvarchar', 'char', 'nchar'):
                conn.execute(text(f"ALTER TABLE dbo.{table_name} ALTER COLUMN [MATERIAL NO] BIGINT"))
                conn.commit()

    save_df = output_df.copy()
    save_df["MATERIAL NO"] = pd.to_numeric(save_df["MATERIAL NO"], errors="coerce").astype("Int64")
    save_df.to_sql(table_name, engine, if_exists="append", index=False, schema="dbo")
    return True


def _rebuild_pend_alc(engine):
    """Rebuild ARS_pend_alc = BDC total qty minus DO total qty, aggregated by RDC+ST_CD+MATNR."""
    with engine.connect() as conn:
        # Check if DO_QTY column exists
        has_do_qty = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='ARS_ALLOCATION_MASTER' AND COLUMN_NAME='DO_QTY'"
        )).scalar()

        if has_do_qty:
            conn.execute(text("""
                IF OBJECT_ID('ARS_pend_alc','U') IS NOT NULL DROP TABLE ARS_pend_alc;
                SELECT
                    a.[VENDOR] AS RDC,
                    a.[RECEIVING STORE] AS ST_CD,
                    CAST(a.[MATERIAL NO] AS BIGINT) AS MATNR,
                    SUM(a.[BDC-QTY]) - ISNULL(SUM(a.[DO_QTY]), 0) AS QTY
                INTO ARS_pend_alc
                FROM dbo.ARS_ALLOCATION_MASTER a WITH (NOLOCK)
                GROUP BY a.[VENDOR], a.[RECEIVING STORE], a.[MATERIAL NO]
                HAVING SUM(a.[BDC-QTY]) - ISNULL(SUM(a.[DO_QTY]), 0) > 0
            """))
        else:
            # No DO_QTY yet — all BDC qty is pending
            conn.execute(text("""
                IF OBJECT_ID('ARS_pend_alc','U') IS NOT NULL DROP TABLE ARS_pend_alc;
                SELECT
                    [VENDOR] AS RDC,
                    [RECEIVING STORE] AS ST_CD,
                    CAST([MATERIAL NO] AS BIGINT) AS MATNR,
                    SUM([BDC-QTY]) AS QTY
                INTO ARS_pend_alc
                FROM dbo.ARS_ALLOCATION_MASTER WITH (NOLOCK)
                GROUP BY [VENDOR], [RECEIVING STORE], [MATERIAL NO]
                HAVING SUM([BDC-QTY]) > 0
            """))
        conn.commit()


def _update_do_qty(output_df: pd.DataFrame, engine):
    """Bulk update DO_QTY in ARS_ALLOCATION_MASTER using temp table + single UPDATE."""
    table_name = "ARS_ALLOCATION_MASTER"
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME=:tbl AND COLUMN_NAME='DO_QTY'"
        ), {"tbl": table_name}).scalar()
        if not exists:
            conn.execute(text(f"ALTER TABLE dbo.{table_name} ADD [DO_QTY] INT NULL DEFAULT 0"))
            conn.commit()

    # Aggregate by keys
    key_cols = ['Allocation Number', 'VENDOR', 'MATERIAL NO', 'RECEIVING STORE']
    qty_col = 'BDC-QTY' if 'BDC-QTY' in output_df.columns else 'DO_QTY'
    agg = output_df.groupby(key_cols)[qty_col].sum().reset_index()
    agg.columns = ['alloc_no', 'vendor', 'material', 'store', 'qty']

    # Bulk update via temp table
    import uuid as _u2
    tmp = f"#do_qty_tmp_{_u2.uuid4().hex[:10]}"
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        # Create local temp table
        cols_sql = ", ".join(
            f"[{c}] NVARCHAR(200) COLLATE DATABASE_DEFAULT"
            for c in agg.columns[:-1]
        )
        cursor.execute(f"CREATE TABLE [{tmp}] ({cols_sql}, [qty] FLOAT)")
        rows = [tuple(r) for r in agg.itertuples(index=False)]
        cursor.executemany(f"INSERT INTO [{tmp}] VALUES({','.join(['?']*len(agg.columns))})", rows)
        cursor.execute(f"""
            UPDATE a SET a.[DO_QTY] = ISNULL(a.[DO_QTY], 0) + t.[qty]
            FROM dbo.{table_name} a
            INNER JOIN [{tmp}] t ON a.[Allocation Number] = t.[alloc_no]
                AND a.[VENDOR] = t.[vendor]
                AND a.[MATERIAL NO] = t.[material]
                AND a.[RECEIVING STORE] = t.[store]
        """)
        raw.commit()
    finally:
        try:
            raw.cursor().execute(f"IF OBJECT_ID('tempdb..[{tmp}]') IS NOT NULL DROP TABLE [{tmp}]")
            raw.commit()
        except Exception:
            pass
        raw.close()


def _ensure_do_qty_column(conn, table_name: str):
    """Add DO_QTY column to table if it doesn't exist."""
    result = conn.execute(text("""
        SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = :tbl AND COLUMN_NAME = 'DO_QTY'
    """), {"tbl": table_name})
    if result.scalar() == 0:
        conn.execute(text(f"ALTER TABLE dbo.{table_name} ADD [DO_QTY] INT NULL DEFAULT 0"))
        conn.commit()


# ===================== BDC Upload Endpoints =====================

@router.post("/upload")
async def upload_and_process_bdc(
    file: UploadFile = File(..., description="CSV or Excel file with allocation quantity data"),
    sheet_name: Optional[str] = Form(None, description="Excel sheet name (optional)"),
    auto_save: str = Form("false", description="Auto-save to database"),
    current_user: User = Depends(get_current_user),
    db=Depends(get_data_db),
):
    """Upload allocation quantity data, process through BDC pipeline, and return results."""
    try:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="File is empty")

        df = _read_file_to_df(content, file.filename, sheet_name)

        if df.empty:
            raise HTTPException(status_code=400, detail="File contains no data rows")

        required = {"ALLOC-DATE", "RDC", "VAR-ART", "ST-CD", "ALLOC-QTY", "PICKING_DATE", "STATUS"}
        missing = required - set(df.columns)
        if missing:
            raise HTTPException(status_code=400, detail=f"Missing required columns: {', '.join(missing)}")

        engine = get_data_engine()
        is_auto_save = auto_save.lower() == "true"

        alloc_by_rdc: dict = {}
        if is_auto_save:
            # One allocation# per (FY, RDC). The output rows are stamped with
            # the alloc# matching their VENDOR (= RDC).
            distinct_rdcs = sorted({
                str(v).strip() for v in df["RDC"].dropna().astype(str)
                if str(v).strip()
            })
            alloc_by_rdc = {
                rdc: _get_next_allocation_no(engine, rdc=rdc)
                for rdc in distinct_rdcs
            }
            allocation_no = ",".join(alloc_by_rdc[k] for k in distinct_rdcs)
            batch_no = _get_next_batch_no(engine)
        else:
            allocation_no = ""
            batch_no = ""
        result = _process_bdc(df, engine, allocation_no=allocation_no,
                              alloc_batch=batch_no, alloc_by_rdc=alloc_by_rdc)

        saved = False
        if is_auto_save and result["total_rows"] > 0:
            full_data = result["_full_data"]
            _save_to_db(full_data, engine)
            wp_data = result["_full_data_without_pending"]
            if len(wp_data) > 0:
                _save_to_db_without_pending(wp_data, engine)
            # Track in ARS_pend_alc (BDC done, DO pending)
            _rebuild_pend_alc(engine)
            saved = True

        result.pop("_full_data", None)
        result.pop("_full_data_without_pending", None)
        result["saved"] = saved
        result["allocation_no"] = allocation_no

        return result

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"BDC processing error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process BDC: {str(e)}")


@router.post("/save")
async def save_bdc_to_db(
    file: UploadFile = File(..., description="CSV or Excel file with allocation quantity data"),
    sheet_name: Optional[str] = Form(None, description="Excel sheet name (optional)"),
    current_user: User = Depends(get_current_user),
    db=Depends(get_data_db),
):
    """Re-process and save BDC results to ARS_ALLOCATION_MASTER table."""
    try:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="File is empty")

        df = _read_file_to_df(content, file.filename, sheet_name)

        if df.empty:
            raise HTTPException(status_code=400, detail="File contains no data rows")

        required = {"ALLOC-DATE", "RDC", "VAR-ART", "ST-CD", "ALLOC-QTY", "PICKING_DATE", "STATUS"}
        missing = required - set(df.columns)
        if missing:
            raise HTTPException(status_code=400, detail=f"Missing required columns: {', '.join(missing)}")

        engine = get_data_engine()
        distinct_rdcs = sorted({
            str(v).strip() for v in df["RDC"].dropna().astype(str)
            if str(v).strip()
        })
        alloc_by_rdc = {
            rdc: _get_next_allocation_no(engine, rdc=rdc)
            for rdc in distinct_rdcs
        }
        allocation_no = ",".join(alloc_by_rdc[k] for k in distinct_rdcs)
        batch_no = _get_next_batch_no(engine)
        result = _process_bdc(df, engine, allocation_no=allocation_no,
                              alloc_batch=batch_no, alloc_by_rdc=alloc_by_rdc)

        if result["total_rows"] == 0:
            raise HTTPException(status_code=400, detail="No rows to save after processing")

        full_data = result["_full_data"]
        _save_to_db(full_data, engine)
        wp_data = result["_full_data_without_pending"]
        if len(wp_data) > 0:
            _save_to_db_without_pending(wp_data, engine)

        # Update ARS_pend_alc with BDC records (BDC done, DO pending)
        _rebuild_pend_alc(engine)

        return {"success": True, "saved_rows": result["total_rows"],
                "allocation_no": str(allocation_no)}

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"BDC save error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save BDC: {str(e)}")


@router.post("/download")
async def download_bdc(
    file: UploadFile = File(..., description="CSV or Excel file with allocation quantity data"),
    sheet_name: Optional[str] = Form(None, description="Excel sheet name (optional)"),
    allocation_no: str = Form(..., description="Allocation number to use for download"),
    current_user: User = Depends(get_current_user),
    db=Depends(get_data_db),
):
    """Process BDC and return as downloadable CSV file."""
    try:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="File is empty")

        df = _read_file_to_df(content, file.filename, sheet_name)

        if df.empty:
            raise HTTPException(status_code=400, detail="File contains no data rows")

        required = {"ALLOC-DATE", "RDC", "VAR-ART", "ST-CD", "ALLOC-QTY", "PICKING_DATE", "STATUS"}
        missing = required - set(df.columns)
        if missing:
            raise HTTPException(status_code=400, detail=f"Missing required columns: {', '.join(missing)}")

        engine = get_data_engine()
        result = _process_bdc(df, engine, allocation_no=allocation_no.strip())
        output_df = result["_full_data"]
        # Exclude ALLOC_BATCH from export (internal reference only, not for SAP)
        export_cols = [c for c in output_df.columns if c != 'ALLOC_BATCH']
        export_df = output_df[export_cols]

        buffer = io.StringIO()
        export_df.to_csv(buffer, index=False)
        buffer.seek(0)

        return StreamingResponse(
            io.BytesIO(buffer.getvalue().encode("utf-8")),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=BDC_Output.csv"},
        )

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"BDC download error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate BDC file: {str(e)}")


# ===================== Delivery Order Upload =====================

@router.post("/delivery-order-upload")
async def upload_delivery_order(
    file: UploadFile = File(..., description="CSV or Excel file with DO_QTY"),
    sheet_name: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db=Depends(get_data_db),
):
    """
    Upload file to update DO_QTY in ARS_ALLOCATION_MASTER.
    Match on: VENDOR, RECEIVING STORE, MATERIAL NO, Allocation Number.
    Set: DO_QTY = uploaded value.
    Then rebuild ARS_pend_alc.
    """
    try:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="File is empty")

        df = _read_file_to_df(content, file.filename, sheet_name)
        if df.empty:
            raise HTTPException(status_code=400, detail="File contains no data rows")

        required = {"VENDOR", "RECEIVING STORE", "MATERIAL NO", "Allocation Number", "DO_QTY"}
        missing = required - set(df.columns)
        if missing:
            raise HTTPException(status_code=400, detail=f"Missing required columns: {', '.join(missing)}")

        engine = get_data_engine()
        table_name = "ARS_ALLOCATION_MASTER"

        with engine.connect() as conn:
            if conn.execute(text("SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME=:t"), {"t": table_name}).scalar() == 0:
                raise HTTPException(status_code=404, detail="ARS_ALLOCATION_MASTER does not exist")
            _ensure_do_qty_column(conn, table_name)

        # Clean data — all as strings for safe matching
        do_df = df[["VENDOR", "RECEIVING STORE", "MATERIAL NO", "Allocation Number", "DO_QTY"]].copy()
        do_df["VENDOR"] = do_df["VENDOR"].astype(str).str.strip()
        do_df["RECEIVING STORE"] = do_df["RECEIVING STORE"].astype(str).str.strip()
        do_df["MATERIAL NO"] = do_df["MATERIAL NO"].astype(str).str.strip().str.split('.').str[0]  # remove .0
        do_df["Allocation Number"] = do_df["Allocation Number"].astype(str).str.strip()
        do_df["DO_QTY"] = pd.to_numeric(do_df["DO_QTY"], errors="coerce").fillna(0).clip(-2147483647, 2147483647).astype(int)

        # Use raw pyodbc: create temp table → bulk insert → single UPDATE JOIN
        import uuid as _uuid
        _do_suffix = _uuid.uuid4().hex[:10]
        raw_conn = engine.raw_connection()
        try:
            cursor = raw_conn.cursor()
            cursor.fast_executemany = True

            # 1. Create local temp table — all NVARCHAR for safe comparison.
            # COLLATE DATABASE_DEFAULT forces the temp columns to take the
            # user DB's collation instead of tempdb's, otherwise the JOIN
            # below raises SQL 468 (collation conflict) when the deployment's
            # tempdb (Latin1_General_CI_AS) differs from the app DB
            # (SQL_Latin1_General_CP1_CI_AS).
            cursor.execute(f"""
                CREATE TABLE #do_update_{_do_suffix} (
                    v NVARCHAR(50) COLLATE DATABASE_DEFAULT,
                    s NVARCHAR(50) COLLATE DATABASE_DEFAULT,
                    m NVARCHAR(50) COLLATE DATABASE_DEFAULT,
                    a NVARCHAR(50) COLLATE DATABASE_DEFAULT,
                    q INT
                )
            """)

            # 2. Bulk insert
            rows = list(zip(
                do_df["VENDOR"].tolist(),
                do_df["RECEIVING STORE"].tolist(),
                do_df["MATERIAL NO"].tolist(),
                do_df["Allocation Number"].tolist(),
                do_df["DO_QTY"].tolist(),
            ))
            cursor.executemany(f"INSERT INTO #do_update_{_do_suffix}(v,s,m,a,q) VALUES(?,?,?,?,?)", rows)

            # 3. Check sample for debug
            cursor.execute(f"SELECT TOP 2 [VENDOR],[RECEIVING STORE],CAST([MATERIAL NO] AS NVARCHAR(50)),[Allocation Number] FROM dbo.{table_name}")
            logger.info(f"DO DB sample: {cursor.fetchall()}")
            cursor.execute(f"SELECT TOP 2 v,s,m,a FROM #do_update_{_do_suffix}")
            logger.info(f"DO File sample: {cursor.fetchall()}")

            # 4. UPDATE JOIN — all comparisons as NVARCHAR
            cursor.execute(f"""
                UPDATE t
                SET t.[DO_QTY] = CAST(u.q AS INT)
                FROM dbo.{table_name} t
                INNER JOIN #do_update_{_do_suffix} u
                    ON LTRIM(RTRIM(CAST(t.[VENDOR] AS NVARCHAR(50)))) = LTRIM(RTRIM(u.v))
                    AND LTRIM(RTRIM(CAST(t.[RECEIVING STORE] AS NVARCHAR(50)))) = LTRIM(RTRIM(u.s))
                    AND LTRIM(RTRIM(CAST(t.[MATERIAL NO] AS NVARCHAR(50)))) = LTRIM(RTRIM(u.m))
                    AND LTRIM(RTRIM(CAST(t.[Allocation Number] AS NVARCHAR(50)))) = LTRIM(RTRIM(u.a))
            """)
            updated_count = cursor.rowcount
            logger.info(f"DO updated: {updated_count}")

            raw_conn.commit()
        finally:
            try:
                _cc = raw_conn.cursor()
                _cc.execute(
                    f"IF OBJECT_ID('tempdb..#do_update_{_do_suffix}') IS NOT NULL "
                    f"DROP TABLE #do_update_{_do_suffix}"
                )
                raw_conn.commit()
            except Exception:
                pass
            raw_conn.close()

        not_found_count = len(do_df) - updated_count if updated_count < len(do_df) else 0

        # Rebuild legacy ARS_pend_alc (BDC-based; kept for backward compat)
        _rebuild_pend_alc(engine)

        # Also deduct from ARS_PEND_ALC (ARS-sourced pending table) and update
        # ARS_BDC_HISTORY so the audit trail shows DO_RECEIVED + STATUS.
        # VENDOR=source warehouse (RDC), RECEIVING STORE=destination (ST_CD).
        try:
            import uuid as _uuid
            from app.services.pend_alc_service import (
                apply_do_deductions, update_bdc_history_with_do, log_operation,
            )
            do_batch_id = _uuid.uuid4().hex[:12]
            do_rows = [
                {"rdc":               str(r["VENDOR"]).strip(),
                 "st_cd":             str(r["RECEIVING STORE"]).strip(),
                 "article_number":    str(r["MATERIAL NO"]).strip(),
                 "do_qty":            float(r["DO_QTY"]),
                 "allocation_number": str(r["Allocation Number"]).strip()}
                for _, r in do_df.iterrows()
                if float(r.get("DO_QTY") or 0) > 0
            ]
            if do_rows:
                with engine.connect() as _pc:
                    _do_res   = apply_do_deductions(_pc, do_rows)
                    _hist_res = update_bdc_history_with_do(_pc, do_rows)
                    total_qty = sum(float(r.get("do_qty") or 0) for r in do_rows)
                    log_operation(
                        _pc,
                        op_type="DO",
                        op_key=do_batch_id,
                        payload={
                            "input_rows":      do_rows,
                            "pend_updates":    _do_res["pend_updates"],
                            "history_updates": _hist_res["history_updates"],
                        },
                        summary=f"DO file upload {do_batch_id}: {len(do_rows)} lines, "
                                f"{int(total_qty)} units",
                        rows_affected=_do_res["touched"],
                        qty_total=total_qty,
                        created_by=getattr(current_user, "username", None),
                    )
                    logger.info(
                        f"[pend_alc] DO upload: {_do_res['touched']} ARS_PEND_ALC updated, "
                        f"{_hist_res['touched']} ARS_BDC_HISTORY rows updated, "
                        f"batch={do_batch_id}"
                    )
        except Exception as _pe:
            logger.warning(f"[pend_alc] apply_do_deductions skipped: {_pe}")

        return {
            "success": True,
            "total_file_rows": len(df),
            "updated_rows": updated_count,
            "not_found_rows": not_found_count,
        }

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"DO_QTY upload error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update DO_QTY: {str(e)}")


# ===================== Sequences =====================

@router.get("/sequences")
async def get_bdc_sequences(
    current_user: User = Depends(get_current_user),
    db=Depends(get_data_db),
):
    """Get all saved allocation sequences from ARS_ALLOCATION_MASTER."""
    try:
        engine = get_data_engine()
        table_name = "ARS_ALLOCATION_MASTER"

        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :tbl"
            ), {"tbl": table_name})
            if result.scalar() == 0:
                return {"sequences": []}

            # Check if ALLOC_BATCH column exists
            has_batch = conn.execute(text(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME=:t AND COLUMN_NAME='ALLOC_BATCH'"
            ), {"t": table_name}).scalar()

            if has_batch:
                result = conn.execute(text(f"""
                    SELECT
                        ISNULL([ALLOC_BATCH], [Allocation Number]) AS batch_key,
                        MIN([Allocation Date]) AS alloc_date,
                        COUNT(DISTINCT [RECEIVING STORE]) AS store_count,
                        COUNT(*) AS total_rows,
                        SUM([BDC-QTY]) AS total_qty,
                        MIN([CREATED_AT]) AS created_at
                    FROM dbo.{table_name} WITH (NOLOCK)
                    GROUP BY ISNULL([ALLOC_BATCH], [Allocation Number])
                    ORDER BY MIN([CREATED_AT]) DESC
                """))
            else:
                result = conn.execute(text(f"""
                    SELECT
                        [Allocation Number] AS batch_key,
                        MIN([Allocation Date]) AS alloc_date,
                        COUNT(DISTINCT [RECEIVING STORE]) AS store_count,
                        COUNT(*) AS total_rows,
                        SUM([BDC-QTY]) AS total_qty,
                        MIN([CREATED_AT]) AS created_at
                    FROM dbo.{table_name} WITH (NOLOCK)
                    GROUP BY [Allocation Number]
                    ORDER BY MIN([CREATED_AT]) DESC
                """))
            rows = result.fetchall()

            sequences = []
            for r in rows:
                sequences.append({
                    "allocation_no": r[0],
                    "alloc_date": str(r[1]) if r[1] else "",
                    "stores": r[2] or 0,
                    "total_rows": r[3],
                    "total_qty": r[4],
                    "created_at": str(r[5]) if r[5] else "",
                })

            return {"sequences": sequences}

    except Exception as e:
        logger.error(f"BDC sequences error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get sequences: {str(e)}")


@router.delete("/sequences/{allocation_no}")
async def delete_bdc_sequence(
    allocation_no: str,
    current_user: User = Depends(get_current_user),
    db=Depends(get_data_db),
):
    """Delete all rows for a given allocation number from both ARS_ALLOCATION_MASTER and ARS_ALLOCATION_MASTER_WITHOUT_PENDING."""
    try:
        engine = get_data_engine()
        table_name = "ARS_ALLOCATION_MASTER"
        wp_table = "ARS_ALLOCATION_MASTER_WITHOUT_PENDING"

        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :tbl"
            ), {"tbl": table_name})
            if result.scalar() == 0:
                raise HTTPException(status_code=404, detail="Table does not exist")

            # Delete by ALLOC_BATCH or Allocation Number
            has_batch = conn.execute(text(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME=:t AND COLUMN_NAME='ALLOC_BATCH'"
            ), {"t": table_name}).scalar()

            if has_batch:
                result = conn.execute(
                    text(f"DELETE FROM dbo.{table_name} WHERE [ALLOC_BATCH] = :key OR [Allocation Number] = :key"),
                    {"key": allocation_no})
            else:
                result = conn.execute(
                    text(f"DELETE FROM dbo.{table_name} WHERE [Allocation Number] = :key"),
                    {"key": allocation_no})
            deleted = result.rowcount

            # Also delete from WITHOUT_PENDING
            wp_exists = conn.execute(text(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = :tbl"
            ), {"tbl": wp_table}).scalar()
            wp_deleted = 0
            if wp_exists > 0:
                if has_batch:
                    wp_result = conn.execute(
                        text(f"DELETE FROM dbo.{wp_table} WHERE [ALLOC_BATCH] = :key OR [Allocation Number] = :key"),
                        {"key": allocation_no})
                else:
                    wp_result = conn.execute(
                        text(f"DELETE FROM dbo.{wp_table} WHERE [Allocation Number] = :key"),
                        {"key": allocation_no})
                wp_deleted = wp_result.rowcount

            conn.commit()

            # Rebuild pend_alc after delete
            try:
                _rebuild_pend_alc(engine)
            except Exception:
                pass

        logger.info(f"Deleted allocation #{allocation_no}: {deleted} from MASTER, {wp_deleted} from WITHOUT_PENDING")

        return {"success": True, "deleted_rows": deleted, "deleted_rows_wp": wp_deleted, "allocation_no": allocation_no}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"BDC delete error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete sequence: {str(e)}")


@router.post("/sheets")
async def get_excel_sheets(
    file: UploadFile = File(..., description="Excel file to extract sheet names"),
    current_user: User = Depends(get_current_user),
):
    """Return list of sheet names from an Excel file."""
    try:
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="File is empty")

        lower = file.filename.lower()
        if not lower.endswith((".xlsx", ".xls")):
            return {"sheets": []}

        xls = pd.ExcelFile(io.BytesIO(content))
        return {"sheets": xls.sheet_names}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"BDC sheets error: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to read sheets: {str(e)}")
