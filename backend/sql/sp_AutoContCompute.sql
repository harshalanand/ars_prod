/*
================================================================================
  sp_AutoContCompute — SQL-direct Contribution % pipeline (single preset)
--------------------------------------------------------------------------------
  Replaces the pandas path in backend/app/api/v1/endpoints/contrib.py
  (_process_single_preset + _compute_kpis). Everything runs inside SQL Server
  using window functions — no per-row Python.

  WHY THIS EXISTS
    * Pandas pulls millions of rows over the wire, computes, and pushes them
      back. We're cutting out the round-trip.
    * Float math stays in float64 — fixes the contribution-drift bug where
      INITIAL AUTO CONT% did not sum to 100% across a group.

  INPUTS
    @grouping_column   e.g. 'MACRO_MVGR' / 'M_VND_CD' / 'FAB' / 'RNG_SEG' …
                       Whitelisted in Python before calling — do NOT pass
                       untrusted strings (we string-build the table name).
    @kpi_type          'L30D' | 'L7D' | 'L18M'  (sal_stk.KPI filter)
    @avg_days          int — divisor for daily-sale calculations
    @months_csv        Comma-list of STOCK_DATE values when KPI = 'L18M'.
                       NULL or empty for L30D / L7D (uses KPI flag instead).
    @majcats_csv       Optional comma-list to restrict MAJ_CAT. NULL = all.
    @out_detail        Target table for store-level detail rows.
    @out_company       Target table for company-level aggregated rows.

  OUTPUTS
    Two tables created via SELECT INTO. Each contains hierarchy columns +
    raw aggregates + computed KPIs (0001_STK_Q, FIX, DISP_AREA, GM_%, STR,
    SALES PSF, SALE_PSF_MJ, SALES_PSF_ACH%, GM PSF, GM_PSF_MJ, GM_PSF_ACH%,
    STOCK_CONT%, SALE_CONT%, ALGO, INITIAL AUTO CONT%).

  CAVEATS
    * The grouping column and table names are interpolated via dynamic SQL
      (because @grouping_column changes the column list). Caller MUST
      whitelist these against VALID_GROUPING.
    * Float math is done in FLOAT (8-byte) for correctness. Final SELECT
      INTO uses ROUND(..., 2) to match the existing pandas output.
================================================================================
*/

IF OBJECT_ID('dbo.sp_AutoContCompute', 'P') IS NOT NULL
    DROP PROCEDURE dbo.sp_AutoContCompute;
GO

CREATE PROCEDURE dbo.sp_AutoContCompute
    @grouping_column NVARCHAR(64),
    @kpi_type        NVARCHAR(10),
    @avg_days        INT          = 30,
    @months_csv      NVARCHAR(MAX)= NULL,
    @majcats_csv     NVARCHAR(MAX)= NULL,
    @out_detail      NVARCHAR(200),
    @out_company     NVARCHAR(200)
AS
BEGIN
    SET NOCOUNT ON;
    SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;

    -- ── Guardrails ──────────────────────────────────────────────────────────
    IF @grouping_column NOT IN
        ('CLR','SZ','RNG_SEG','M_VND_CD','MACRO_MVGR','MICRO_MVGR',
         'FAB','WEAVE_2','M_YARN_02')
    BEGIN
        RAISERROR('Invalid @grouping_column: %s', 16, 1, @grouping_column);
        RETURN;
    END;

    IF @kpi_type NOT IN ('L30D','L7D','L18M')
    BEGIN
        RAISERROR('Invalid @kpi_type: %s', 16, 1, @kpi_type);
        RETURN;
    END;

    DECLARE @gc       NVARCHAR(64)  = QUOTENAME(@grouping_column);
    DECLARE @hierTbl  NVARCHAR(128) = QUOTENAME('Master_HIER_' + @grouping_column);
    DECLARE @outD     NVARCHAR(260) = QUOTENAME(@out_detail);
    DECLARE @outC     NVARCHAR(260) = QUOTENAME(@out_company);
    DECLARE @sql      NVARCHAR(MAX);
    DECLARE @Q        FLOAT = 1000.0;     -- qty divisor
    DECLARE @V        FLOAT = 100000.0;   -- value divisor (lakhs)
    DECLARE @gr       FLOAT = CASE WHEN @grouping_column = 'M_VND_CD' THEN 2.0 ELSE 1.0 END;

    -- ── Date filter clause ──────────────────────────────────────────────────
    DECLARE @date_filter NVARCHAR(1000);
    IF @kpi_type = 'L7D'
        SET @date_filter = N'sal_stk.KPI = ''L7D''';
    ELSE IF @kpi_type = 'L30D'
        SET @date_filter = N'sal_stk.KPI = ''L30D''';
    ELSE  -- L18M
    BEGIN
        IF @months_csv IS NULL OR LEN(@months_csv) = 0
        BEGIN
            RAISERROR('L18M kpi_type requires @months_csv', 16, 1);
            RETURN;
        END;
        SET @date_filter = N'sal_stk.STOCK_DATE IN (SELECT value FROM STRING_SPLIT(@p_months,'',''))'
                          + N' AND sal_stk.KPI = ''L18M''';
    END;

    -- ── MAJ_CAT filter clause ───────────────────────────────────────────────
    DECLARE @majcat_filter NVARCHAR(1000) = N'1=1';
    IF @majcats_csv IS NOT NULL AND LEN(@majcats_csv) > 0
        SET @majcat_filter = N'MAJ_CAT IN (SELECT value FROM STRING_SPLIT(@p_majcats, '',''))';

    -- ── Drop previous output tables ─────────────────────────────────────────
    SET @sql = N'IF OBJECT_ID(''' + REPLACE(@out_detail, '''','''''') + ''',''U'') IS NOT NULL DROP TABLE ' + @outD + ';'
             + N'IF OBJECT_ID(''' + REPLACE(@out_company,'''','''''') + ''',''U'') IS NOT NULL DROP TABLE ' + @outC + ';';
    EXEC sp_executesql @sql;

    -- ════════════════════════════════════════════════════════════════════════
    -- Step 1: stock × product aggregate → #stage
    -- ════════════════════════════════════════════════════════════════════════
    SET @sql = N'
    SELECT ST_CD, MAJ_CAT, ' + @gc + N' AS GRP_VAL,
           AVG(NULLIF(OP_STK_Q,0)) AS OP_STK_Q,
           AVG(NULLIF(OP_STK_V,0)) AS OP_STK_V,
           AVG(NULLIF(CL_STK_Q,0)) AS CL_STK_Q,
           AVG(NULLIF(CL_STK_V,0)) AS CL_STK_V,
           AVG(CASE WHEN SALE_Q <> 0 THEN SALE_Q END) AS SALE_Q,
           AVG(CASE WHEN SALE_Q <> 0 THEN SALE_V END) AS SALE_V,
           AVG(CASE WHEN SALE_Q <> 0 THEN GM_V   END) AS GM_V
    INTO #stage
    FROM (
        SELECT sal_stk.STOCK_DATE,
               sal_stk.WERKS AS ST_CD,
               prod.MAJ_CAT,
               prod.' + @gc + N',
               COALESCE(SUM(sal_stk.OP_STK_QTY)/1000.0,0)   AS OP_STK_Q,
               COALESCE(SUM(sal_stk.OP_STK_VAL)/100000.0,0) AS OP_STK_V,
               COALESCE(SUM(sal_stk.CL_STK_QTY)/1000.0,0)   AS CL_STK_Q,
               COALESCE(SUM(sal_stk.CL_STK_VAL)/100000.0,0) AS CL_STK_V,
               COALESCE(SUM(sal_stk.SALE_QTY)/1000.0,0)     AS SALE_Q,
               COALESCE(SUM(sal_stk.SALE_VAL)/100000.0,0)   AS SALE_V,
               COALESCE(SUM(sal_stk.GM_VAL)/100000.0,0)     AS GM_V
        FROM dbo.COUNT_STOCK_DATA_18M sal_stk WITH (NOLOCK)
        LEFT JOIN (
            SELECT ARTICLE_NUMBER AS MATNR, MAJ_CAT,
                   COALESCE(NULLIF(' + @gc + N','''') , ''NA'') AS ' + @gc + N',
                   SEG
            FROM dbo.VW_MASTER_PRODUCT WITH (NOLOCK)
        ) prod ON sal_stk.MATNR = prod.MATNR
        WHERE ' + @majcat_filter + N'
          AND prod.SEG IN (''APP'',''GM'')
          AND ' + @date_filter + N'
        GROUP BY sal_stk.WERKS, sal_stk.STOCK_DATE, prod.MAJ_CAT, prod.' + @gc + N'
    ) t
    GROUP BY ST_CD, MAJ_CAT, ' + @gc + N';';

    EXEC sp_executesql @sql,
        N'@p_months NVARCHAR(MAX), @p_majcats NVARCHAR(MAX)',
        @p_months  = @months_csv,
        @p_majcats = @majcats_csv;

    -- ════════════════════════════════════════════════════════════════════════
    -- Step 2: master hierarchy × store plan → #master
    -- ════════════════════════════════════════════════════════════════════════
    SET @sql = N'
    SELECT B.ST_CD, B.ST_NM, B.APF, B.[STATUS], B.REF_ST_CD, B.REF_ST_NM,
           B.REF_GRP_NEW, B.REF_GRP_OLD,
           A.*
    INTO #master
    FROM ' + @hierTbl + N' A WITH (NOLOCK)
    CROSS JOIN dbo.Master_STORE_PLAN B WITH (NOLOCK)
    WHERE ' + @majcat_filter + N';';

    EXEC sp_executesql @sql,
        N'@p_majcats NVARCHAR(MAX)',
        @p_majcats = @majcats_csv;

    -- Drop the upload-datetime column if present (mirrors pandas exclude)
    IF COL_LENGTH('tempdb..#master', 'UPLOAD_DATETIME') IS NOT NULL
        ALTER TABLE #master DROP COLUMN [UPLOAD_DATETIME];

    -- ════════════════════════════════════════════════════════════════════════
    -- Step 3: detail merge (master ⟕ stage ⟕ avg_density)
    -- ════════════════════════════════════════════════════════════════════════
    SET @sql = N'
    SELECT m.*,
           COALESCE(s.OP_STK_Q,0) AS OP_STK_Q,
           COALESCE(s.OP_STK_V,0) AS OP_STK_V,
           COALESCE(s.CL_STK_Q,0) AS CL_STK_Q,
           COALESCE(s.CL_STK_V,0) AS CL_STK_V,
           COALESCE(s.SALE_Q,  0) AS SALE_Q,
           COALESCE(s.SALE_V,  0) AS SALE_V,
           COALESCE(s.GM_V,    0) AS GM_V,
           COALESCE(d.AVG_DNSTY, 0) AS AVG_DNSTY
    INTO #merged
    FROM #master m
    LEFT JOIN #stage s
      ON s.ST_CD     = m.ST_CD
     AND s.MAJ_CAT   = m.MAJ_CAT
     AND s.GRP_VAL   = m.' + @gc + N'
    LEFT JOIN dbo.master_avg_density d WITH (NOLOCK)
      ON d.MAJ_CAT = m.MAJ_CAT;';
    EXEC sp_executesql @sql;

    -- ════════════════════════════════════════════════════════════════════════
    -- Step 4: KPI math at STORE level → @out_detail
    -- Window-functions replace pandas groupby().transform("sum")
    -- ════════════════════════════════════════════════════════════════════════
    SET @sql = N'
    WITH base AS (
        SELECT *,
            CASE WHEN OP_STK_Q = 0 AND CL_STK_Q = 0 THEN 0
                 WHEN OP_STK_Q <> 0 AND CL_STK_Q <> 0 THEN (OP_STK_Q+CL_STK_Q)/2.0
                 ELSE (OP_STK_Q+CL_STK_Q) END AS STK_Q,
            CASE WHEN OP_STK_V = 0 AND CL_STK_V = 0 THEN 0
                 WHEN OP_STK_V <> 0 AND CL_STK_V <> 0 THEN (OP_STK_V+CL_STK_V)/2.0
                 ELSE (OP_STK_V+CL_STK_V) END AS STK_V
        FROM #merged
    ),
    kpi1 AS (
        SELECT *,
            (STK_Q * @qq) / NULLIF(AVG_DNSTY, 0) AS FIX_TMP
        FROM base
    ),
    kpi2 AS (
        SELECT *,
            CASE WHEN APF * ISNULL(FIX_TMP,0) > CASE WHEN SALE_V > 0 THEN 1 ELSE 0 END
                 THEN APF * ISNULL(FIX_TMP,0)
                 ELSE CASE WHEN SALE_V > 0 THEN 1 ELSE 0 END
            END AS DISP_AREA_TMP,
            CASE WHEN SALE_Q > 0 THEN (SALE_Q / @ad) * @qq ELSE 0 END AS PDSQ,
            CASE WHEN SALE_V > 0 THEN (SALE_V / @ad) * @vv ELSE 0 END AS PDSV
        FROM kpi1
    ),
    kpi3 AS (
        SELECT *,
            CASE WHEN PDSQ = 0 THEN 0 ELSE STK_Q / PDSQ * @qq END AS STR_TMP,
            CASE WHEN DISP_AREA_TMP = 0 THEN 0 ELSE PDSV / DISP_AREA_TMP END AS SALES_PSF_TMP,
            CASE WHEN SALE_V <> 0 THEN GM_V / SALE_V ELSE 0 END AS GM_PCT_TMP
        FROM kpi2
    ),
    kpi4 AS (
        SELECT *,
            SUM(SALE_V)        OVER (PARTITION BY ST_CD, MAJ_CAT) AS SV_SUM,
            SUM(DISP_AREA_TMP) OVER (PARTITION BY ST_CD, MAJ_CAT) AS DA_SUM,
            SUM(GM_V)          OVER (PARTITION BY ST_CD, MAJ_CAT) AS GV_SUM,
            SUM(CASE WHEN STK_Q > 0 THEN STK_Q ELSE 0 END)
                OVER (PARTITION BY ST_CD, MAJ_CAT) AS STK_POS_SUM,
            SUM(CASE WHEN SALE_V > 0 THEN SALE_V ELSE 0 END)
                OVER (PARTITION BY ST_CD, MAJ_CAT) AS SAL_POS_SUM
        FROM kpi3
    ),
    kpi5 AS (
        SELECT *,
            CASE WHEN DA_SUM = 0 THEN 0
                 ELSE (SV_SUM * @vv / DA_SUM) / @ad END AS SALE_PSF_MJ_TMP,
            CASE WHEN DISP_AREA_TMP = 0 THEN 0
                 ELSE (GM_V * @vv / DISP_AREA_TMP) / @ad END AS GM_PSF_TMP,
            CASE WHEN DA_SUM = 0 THEN 0
                 ELSE (GV_SUM * @vv / DA_SUM) / @ad END AS GM_PSF_MJ_TMP,
            CASE WHEN STK_Q > 0 AND STK_POS_SUM > 0 THEN STK_Q / STK_POS_SUM ELSE 0 END AS STOCK_CONT_TMP,
            CASE WHEN SALE_V > 0 AND SAL_POS_SUM > 0 THEN SALE_V / SAL_POS_SUM ELSE 0 END AS SALE_CONT_TMP
        FROM kpi4
    ),
    kpi6 AS (
        SELECT *,
            CASE WHEN SALE_PSF_MJ_TMP = 0 THEN 0
                 ELSE SALES_PSF_TMP / SALE_PSF_MJ_TMP END AS SALES_PSF_ACH_TMP,
            CASE WHEN GM_PSF_MJ_TMP <= 0 OR GM_PSF_TMP < 0 THEN 0
                 ELSE GM_PSF_TMP / GM_PSF_MJ_TMP END AS GM_PSF_ACH_TMP
        FROM kpi5
    ),
    kpi7 AS (
        SELECT *,
            -- ALGO = min( raw, max( adj_with_gm, 0.5*sale_cont, 0 ) )
            CASE
              WHEN (SALE_CONT_TMP * CASE WHEN SALE_CONT_TMP < 0.05 THEN 5.0 ELSE 3.0 END)
                 < (CASE
                      WHEN (SALE_CONT_TMP * (1 + (GM_PSF_ACH_TMP - 1) * @gr)) > (SALE_CONT_TMP * 0.5)
                        AND (SALE_CONT_TMP * (1 + (GM_PSF_ACH_TMP - 1) * @gr)) > 0
                      THEN SALE_CONT_TMP * (1 + (GM_PSF_ACH_TMP - 1) * @gr)
                      WHEN (SALE_CONT_TMP * 0.5) > 0
                      THEN SALE_CONT_TMP * 0.5
                      ELSE 0
                    END)
              THEN SALE_CONT_TMP * CASE WHEN SALE_CONT_TMP < 0.05 THEN 5.0 ELSE 3.0 END
              ELSE
                   CASE
                      WHEN (SALE_CONT_TMP * (1 + (GM_PSF_ACH_TMP - 1) * @gr)) > (SALE_CONT_TMP * 0.5)
                        AND (SALE_CONT_TMP * (1 + (GM_PSF_ACH_TMP - 1) * @gr)) > 0
                      THEN SALE_CONT_TMP * (1 + (GM_PSF_ACH_TMP - 1) * @gr)
                      WHEN (SALE_CONT_TMP * 0.5) > 0
                      THEN SALE_CONT_TMP * 0.5
                      ELSE 0
                    END
            END AS ALGO_TMP
        FROM kpi6
    ),
    kpi8 AS (
        SELECT *,
            SUM(ALGO_TMP) OVER (PARTITION BY ST_CD, MAJ_CAT) AS ALGO_SUM
        FROM kpi7
    )
    SELECT
        *,
        ROUND(STK_Q,             2) AS [0001_STK_Q],
        ROUND(STK_V,             2) AS [0001_STK_V],
        ROUND(ISNULL(FIX_TMP,0), 2) AS [FIX],
        ROUND(DISP_AREA_TMP,     2) AS [DISP_AREA],
        ROUND(GM_PCT_TMP,        2) AS [GM_%],
        ROUND(STR_TMP,           2) AS [STR],
        ROUND(SALES_PSF_TMP,     2) AS [SALES PSF],
        ROUND(SALE_PSF_MJ_TMP,   2) AS [SALE_PSF_MJ],
        ROUND(SALES_PSF_ACH_TMP, 2) AS [SALES_PSF_ACH%],
        ROUND(GM_PSF_TMP,        2) AS [GM PSF],
        ROUND(GM_PSF_MJ_TMP,     2) AS [GM_PSF_MJ],
        ROUND(GM_PSF_ACH_TMP,    2) AS [GM_PSF_ACH%],
        ROUND(STOCK_CONT_TMP,    2) AS [STOCK_CONT%],
        ROUND(SALE_CONT_TMP,     2) AS [SALE_CONT%],
        ROUND(ALGO_TMP,          2) AS [ALGO],
        ROUND(CASE WHEN ALGO_SUM = 0 THEN 0 ELSE ALGO_TMP / ALGO_SUM END, 2) AS [INITIAL AUTO CONT%]
    INTO ' + @outD + N'
    FROM kpi8;';

    EXEC sp_executesql @sql,
        N'@qq FLOAT, @vv FLOAT, @ad FLOAT, @gr FLOAT',
        @qq = @Q, @vv = @V, @ad = CAST(@avg_days AS FLOAT), @gr = @gr;

    -- ════════════════════════════════════════════════════════════════════════
    -- Step 5: aggregate to company level → @out_company
    -- (no ST_CD partition; groups by hierarchy + grouping_column)
    -- ════════════════════════════════════════════════════════════════════════
    -- The exact hier-column list varies per @grouping_column; we agg over
    -- everything except ST_CD / ST_NM / APF / AVG_DNSTY / numeric raw cols.
    -- Simplest: sum the raw aggregates, then re-apply the KPI block.
    SET @sql = N'
    SELECT MAJ_CAT, ' + @gc + N',
           SUM(OP_STK_Q) AS OP_STK_Q, SUM(OP_STK_V) AS OP_STK_V,
           SUM(CL_STK_Q) AS CL_STK_Q, SUM(CL_STK_V) AS CL_STK_V,
           SUM(SALE_Q)   AS SALE_Q,   SUM(SALE_V)   AS SALE_V,
           SUM(GM_V)     AS GM_V,
           MAX(AVG_DNSTY) AS AVG_DNSTY,
           CAST(25 AS FLOAT) AS APF
    INTO #merged_co
    FROM #merged
    GROUP BY MAJ_CAT, ' + @gc + N';';
    EXEC sp_executesql @sql;

    -- Same KPI math, but partition is MAJ_CAT only (no ST_CD).
    SET @sql = N'
    WITH base AS (
        SELECT *,
            CASE WHEN OP_STK_Q = 0 AND CL_STK_Q = 0 THEN 0
                 WHEN OP_STK_Q <> 0 AND CL_STK_Q <> 0 THEN (OP_STK_Q+CL_STK_Q)/2.0
                 ELSE (OP_STK_Q+CL_STK_Q) END AS STK_Q,
            CASE WHEN OP_STK_V = 0 AND CL_STK_V = 0 THEN 0
                 WHEN OP_STK_V <> 0 AND CL_STK_V <> 0 THEN (OP_STK_V+CL_STK_V)/2.0
                 ELSE (OP_STK_V+CL_STK_V) END AS STK_V
        FROM #merged_co
    ),
    kpi1 AS (SELECT *, (STK_Q*@qq)/NULLIF(AVG_DNSTY,0) AS FIX_TMP FROM base),
    kpi2 AS (
        SELECT *,
            CASE WHEN APF*ISNULL(FIX_TMP,0) > CASE WHEN SALE_V>0 THEN 1 ELSE 0 END
                 THEN APF*ISNULL(FIX_TMP,0)
                 ELSE CASE WHEN SALE_V>0 THEN 1 ELSE 0 END END AS DISP_AREA_TMP,
            CASE WHEN SALE_Q>0 THEN (SALE_Q/@ad)*@qq ELSE 0 END AS PDSQ,
            CASE WHEN SALE_V>0 THEN (SALE_V/@ad)*@vv ELSE 0 END AS PDSV
        FROM kpi1
    ),
    kpi3 AS (
        SELECT *,
            CASE WHEN PDSQ=0 THEN 0 ELSE STK_Q/PDSQ*@qq END AS STR_TMP,
            CASE WHEN DISP_AREA_TMP=0 THEN 0 ELSE PDSV/DISP_AREA_TMP END AS SALES_PSF_TMP,
            CASE WHEN SALE_V<>0 THEN GM_V/SALE_V ELSE 0 END AS GM_PCT_TMP
        FROM kpi2
    ),
    kpi4 AS (
        SELECT *,
            SUM(SALE_V)        OVER (PARTITION BY MAJ_CAT) AS SV_SUM,
            SUM(DISP_AREA_TMP) OVER (PARTITION BY MAJ_CAT) AS DA_SUM,
            SUM(GM_V)          OVER (PARTITION BY MAJ_CAT) AS GV_SUM,
            SUM(CASE WHEN STK_Q>0 THEN STK_Q ELSE 0 END)  OVER (PARTITION BY MAJ_CAT) AS STK_POS_SUM,
            SUM(CASE WHEN SALE_V>0 THEN SALE_V ELSE 0 END) OVER (PARTITION BY MAJ_CAT) AS SAL_POS_SUM
        FROM kpi3
    ),
    kpi5 AS (
        SELECT *,
            CASE WHEN DA_SUM=0 THEN 0 ELSE (SV_SUM*@vv/DA_SUM)/@ad END AS SALE_PSF_MJ_TMP,
            CASE WHEN DISP_AREA_TMP=0 THEN 0 ELSE (GM_V*@vv/DISP_AREA_TMP)/@ad END AS GM_PSF_TMP,
            CASE WHEN DA_SUM=0 THEN 0 ELSE (GV_SUM*@vv/DA_SUM)/@ad END AS GM_PSF_MJ_TMP,
            CASE WHEN STK_Q>0 AND STK_POS_SUM>0 THEN STK_Q/STK_POS_SUM ELSE 0 END AS STOCK_CONT_TMP,
            CASE WHEN SALE_V>0 AND SAL_POS_SUM>0 THEN SALE_V/SAL_POS_SUM ELSE 0 END AS SALE_CONT_TMP
        FROM kpi4
    ),
    kpi6 AS (
        SELECT *,
            CASE WHEN SALE_PSF_MJ_TMP=0 THEN 0 ELSE SALES_PSF_TMP/SALE_PSF_MJ_TMP END AS SALES_PSF_ACH_TMP,
            CASE WHEN GM_PSF_MJ_TMP<=0 OR GM_PSF_TMP<0 THEN 0 ELSE GM_PSF_TMP/GM_PSF_MJ_TMP END AS GM_PSF_ACH_TMP
        FROM kpi5
    ),
    kpi7 AS (
        SELECT *,
            CASE
              WHEN (SALE_CONT_TMP * CASE WHEN SALE_CONT_TMP<0.05 THEN 5.0 ELSE 3.0 END)
                 < (CASE
                      WHEN (SALE_CONT_TMP*(1+(GM_PSF_ACH_TMP-1)*@gr)) > (SALE_CONT_TMP*0.5)
                        AND (SALE_CONT_TMP*(1+(GM_PSF_ACH_TMP-1)*@gr)) > 0
                      THEN SALE_CONT_TMP*(1+(GM_PSF_ACH_TMP-1)*@gr)
                      WHEN (SALE_CONT_TMP*0.5) > 0
                      THEN SALE_CONT_TMP*0.5 ELSE 0 END)
              THEN SALE_CONT_TMP * CASE WHEN SALE_CONT_TMP<0.05 THEN 5.0 ELSE 3.0 END
              ELSE CASE
                      WHEN (SALE_CONT_TMP*(1+(GM_PSF_ACH_TMP-1)*@gr)) > (SALE_CONT_TMP*0.5)
                        AND (SALE_CONT_TMP*(1+(GM_PSF_ACH_TMP-1)*@gr)) > 0
                      THEN SALE_CONT_TMP*(1+(GM_PSF_ACH_TMP-1)*@gr)
                      WHEN (SALE_CONT_TMP*0.5) > 0
                      THEN SALE_CONT_TMP*0.5 ELSE 0 END
            END AS ALGO_TMP
        FROM kpi6
    ),
    kpi8 AS (
        SELECT *, SUM(ALGO_TMP) OVER (PARTITION BY MAJ_CAT) AS ALGO_SUM FROM kpi7
    )
    SELECT *,
        ROUND(STK_Q, 2)            AS [0001_STK_Q],
        ROUND(STK_V, 2)            AS [0001_STK_V],
        ROUND(ISNULL(FIX_TMP,0),2) AS [FIX],
        ROUND(DISP_AREA_TMP,2)     AS [DISP_AREA],
        ROUND(GM_PCT_TMP,   2)     AS [GM_%],
        ROUND(STR_TMP,      2)     AS [STR],
        ROUND(SALES_PSF_TMP,2)     AS [SALES PSF],
        ROUND(SALE_PSF_MJ_TMP,2)   AS [SALE_PSF_MJ],
        ROUND(SALES_PSF_ACH_TMP,2) AS [SALES_PSF_ACH%],
        ROUND(GM_PSF_TMP,    2)    AS [GM PSF],
        ROUND(GM_PSF_MJ_TMP, 2)    AS [GM_PSF_MJ],
        ROUND(GM_PSF_ACH_TMP,2)    AS [GM_PSF_ACH%],
        ROUND(STOCK_CONT_TMP,2)    AS [STOCK_CONT%],
        ROUND(SALE_CONT_TMP, 2)    AS [SALE_CONT%],
        ROUND(ALGO_TMP, 2)         AS [ALGO],
        ROUND(CASE WHEN ALGO_SUM=0 THEN 0 ELSE ALGO_TMP/ALGO_SUM END, 2) AS [INITIAL AUTO CONT%]
    INTO ' + @outC + N'
    FROM kpi8;';

    EXEC sp_executesql @sql,
        N'@qq FLOAT, @vv FLOAT, @ad FLOAT, @gr FLOAT',
        @qq = @Q, @vv = @V, @ad = CAST(@avg_days AS FLOAT), @gr = @gr;

    -- ── cleanup ─────────────────────────────────────────────────────────────
    IF OBJECT_ID('tempdb..#stage')     IS NOT NULL DROP TABLE #stage;
    IF OBJECT_ID('tempdb..#master')    IS NOT NULL DROP TABLE #master;
    IF OBJECT_ID('tempdb..#merged')    IS NOT NULL DROP TABLE #merged;
    IF OBJECT_ID('tempdb..#merged_co') IS NOT NULL DROP TABLE #merged_co;

END;
GO

PRINT 'sp_AutoContCompute created.';
GO
