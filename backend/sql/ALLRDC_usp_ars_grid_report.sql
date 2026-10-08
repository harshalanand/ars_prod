/* ============================================================================
   ALLRDC_usp_ars_grid_report - ALL-RDC variant of usp_ars_grid_report.

   Two differences from the base proc; every other column is unchanged.

   1. The four RDC-scoped measures are aggregated across EVERY RDC instead of
      being matched to the store's own RDC (base joins msa.RDC = s.RDC):
      MSA_OP_Q, MSA_CL_Q, OP_OPT_CNT_>50_PCS, CL_OPT_CNT_>50_PCS.

   2. ADD-ON COLUMNS, one set per RDC, prefixed with the RDC code. The existing
      columns are kept AS IS and these are added after them:
        <RDC>_MSA_OP_Q, <RDC>_MSA_CL_Q,
        <RDC>_OP_OPT_CNT_>50_PCS, <RDC>_CL_OPT_CNT_>50_PCS
      RDC codes are read from ARS_MSA_TOTAL AT RUN TIME, so the column set
      follows the data rather than being hardcoded.

   The RDC output column is the STORE's own (connected) RDC and is now purely
   descriptive - it no longer restricts which pool the measures come from.

   NOTE on the >50 counts: the all-RDC figure is NOT the sum of the per-RDC
   figures, by design. An option holding 30 pcs in each of two RDCs fails the
   >50 test in each one but passes on the combined pool (60). The all-RDC CTEs
   qualify without RDC; opt_op_r / opt_cl_r qualify WITHIN each RDC.
   ============================================================================ */
CREATE OR ALTER PROCEDURE dbo.ALLRDC_usp_ars_grid_report
    @GridTable SYSNAME       = N'ARS_GRID_MJ',
    @WERKS     NVARCHAR(50)  = NULL,
    @MAJ_CAT   NVARCHAR(100) = NULL
AS
BEGIN
    SET NOCOUNT ON;

    IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = @GridTable)
    BEGIN
        RAISERROR('Unknown grid table: %s', 16, 1, @GridTable);
        RETURN;
    END

    DECLARE @gSel NVARCHAR(MAX), @sql NVARCHAR(MAX), @DimCol SYSNAME, @qd NVARCHAR(258);

    -- Auto-detect the dimension column (extra column vs the base grid); NULL for base.
    SELECT @DimCol = COLUMN_NAME
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = @GridTable
      AND COLUMN_NAME NOT IN (SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
                              WHERE TABLE_NAME = 'ARS_GRID_MJ');
    SET @qd = QUOTENAME(@DimCol);

    -- Does the listing table carry this dim column? (used to split lst by dim)
    DECLARE @lstDim BIT = CASE WHEN @DimCol IS NOT NULL
        AND COL_LENGTH('ARS_LISTING_WORKING_HISTORY', @DimCol) IS NOT NULL THEN 1 ELSE 0 END;

    -- Every grid column, prefixed g., with type-aware ISNULL.
    SELECT @gSel = STRING_AGG(
        CASE
          WHEN DATA_TYPE IN ('int','bigint','smallint','tinyint','bit',
                             'decimal','numeric','float','real','money','smallmoney')
               THEN 'ISNULL(g.' + QUOTENAME(COLUMN_NAME) + ', 0) AS '  + QUOTENAME(COLUMN_NAME)
          WHEN DATA_TYPE IN ('nvarchar','varchar','char','nchar','text','ntext')
               THEN 'ISNULL(g.' + QUOTENAME(COLUMN_NAME) + ', '''') AS ' + QUOTENAME(COLUMN_NAME)
          ELSE 'g.' + QUOTENAME(COLUMN_NAME)
        END, ', ')
        WITHIN GROUP (ORDER BY ORDINAL_POSITION)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_NAME = @GridTable;

    -- ── OPT_STATUS pivot (dynamic CNT_/ALCQ_ per status) ──────────────────────
    -- Sourced from ARS_LISTING_WORKING_HISTORY filtered by the latest SESSION_ID
    -- (all report data comes from history/session). OPT_STATUS is stamped into the
    -- parked listing snapshot in listing.py Part 8.5 so Approve carries it here.
    -- The column set is built at runtime from distinct OPT_STATUS, so it grows/shrinks.
    DECLARE @sid NVARCHAR(50) = (SELECT MAX(SESSION_ID) FROM ARS_LISTING_HISTORY WITH (NOLOCK));
    DECLARE @statDim BIT = CASE WHEN @DimCol IS NOT NULL
        AND COL_LENGTH('ARS_LISTING_WORKING_HISTORY', @DimCol) IS NOT NULL THEN 1 ELSE 0 END;
    DECLARE @statAgg NVARCHAR(MAX), @statOut NVARCHAR(MAX),
            @statFrag NVARCHAR(MAX) = N'', @statOutFrag NVARCHAR(MAX) = N'', @statJoin NVARCHAR(MAX) = N'';

    IF COL_LENGTH('ARS_LISTING_WORKING_HISTORY', 'OPT_STATUS') IS NOT NULL
    BEGIN
        SELECT
          @statAgg = STRING_AGG(CAST(
              'COUNT(DISTINCT CASE WHEN OPT_STATUS=''' + REPLACE(v, '''', '''''')
            + ''' THEN CAST(GEN_ART_NUMBER AS NVARCHAR(50))+''|''+ISNULL(CLR,'''') END) AS ' + QUOTENAME('CNT_' + v)
            + ', SUM(CASE WHEN OPT_STATUS=''' + REPLACE(v, '''', '''''')
            + ''' THEN ISNULL(ALLOC_QTY,0) ELSE 0 END) AS ' + QUOTENAME('ALCQ_' + v)
            AS NVARCHAR(MAX)), ', '),
          @statOut = STRING_AGG(CAST(
              'ISNULL(stat.' + QUOTENAME('CNT_' + v)  + ',0) AS ' + QUOTENAME('CNT_' + v)
            + ', ISNULL(stat.' + QUOTENAME('ALCQ_' + v) + ',0) AS ' + QUOTENAME('ALCQ_' + v)
            AS NVARCHAR(MAX)), ', ')
        FROM (SELECT DISTINCT OPT_STATUS AS v FROM ARS_LISTING_WORKING_HISTORY WITH (NOLOCK)
              WHERE SESSION_ID = @sid AND OPT_STATUS IS NOT NULL) s;
    END

    IF @statAgg IS NOT NULL AND LEN(@statAgg) > 0
    BEGIN
        DECLARE @statBody NVARCHAR(MAX) =
            CASE WHEN @DimCol IS NULL OR @statDim = 0 THEN
                N'stat AS (
    SELECT WERKS, MAJ_CAT, ' + @statAgg + N'
    FROM ARS_LISTING_WORKING_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR MAJ_CAT = @pMC)
      AND (@pW IS NULL OR WERKS = @pW)
    GROUP BY WERKS, MAJ_CAT
)'
            ELSE
                N'stat AS (
    SELECT WERKS, MAJ_CAT, ' + @qd + N' AS DIMV, ' + @statAgg + N'
    FROM ARS_LISTING_WORKING_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR MAJ_CAT = @pMC)
      AND (@pW IS NULL OR WERKS = @pW)
    GROUP BY WERKS, MAJ_CAT, ' + @qd + N'
)' END;
        SET @statFrag    = N',
' + @statBody;
        SET @statOutFrag = N',
    ' + @statOut;
        SET @statJoin    = N'
LEFT JOIN stat ON stat.WERKS = g.WERKS AND stat.MAJ_CAT = g.MAJ_CAT'
            + CASE WHEN @DimCol IS NOT NULL AND @statDim = 1 THEN N' AND stat.DIMV = g.' + @qd ELSE N'' END;
    END

    -- ── dimension-aware CTE fragments (base form when @DimCol IS NULL) ─────────
    -- pdim: one dimension value per variant article, from product master.
    DECLARE @pdimCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN N'' ELSE
        N',
pdim AS (
    SELECT CAST(ARTICLE_NUMBER AS NVARCHAR(50)) AS ARTICLE_NUMBER, MAX(' + @qd + N') AS DIMV
    FROM vw_master_product WITH (NOLOCK)
    WHERE (@pMC IS NULL OR MAJ_CAT = @pMC)
    GROUP BY CAST(ARTICLE_NUMBER AS NVARCHAR(50))
)' END;

    -- lst: listing working result (STORE grain [+ dim from its own column])
    DECLARE @lstCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL OR @lstDim = 0 THEN
        N'lst AS (
    SELECT WERKS, MAJ_CAT,
           SUM(ALLOC_QTY) AS ALC_Q, SUM(HOLD_QTY) AS HOLD_ALC_Q,
           ISNULL(SUM(FROM_HOLD_QTY),0) AS ALC_FROM_HOLD_Q, SUM(ART_EXCESS) AS ART_EXCESS_QTY
    FROM ARS_LISTING_WORKING_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR MAJ_CAT = @pMC)
      AND (@pW IS NULL OR WERKS = @pW)
    GROUP BY WERKS, MAJ_CAT
)'
      ELSE
        N'lst AS (
    SELECT WERKS, MAJ_CAT, ' + @qd + N' AS DIMV,
           SUM(ALLOC_QTY) AS ALC_Q, SUM(HOLD_QTY) AS HOLD_ALC_Q,
           ISNULL(SUM(FROM_HOLD_QTY),0) AS ALC_FROM_HOLD_Q, SUM(ART_EXCESS) AS ART_EXCESS_QTY
    FROM ARS_LISTING_WORKING_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR MAJ_CAT = @pMC)
      AND (@pW IS NULL OR WERKS = @pW)
    GROUP BY WERKS, MAJ_CAT, ' + @qd + N'
)' END;

    /* ---- RDC-split addon columns ------------------------------------------
       RDC codes are DISCOVERED AT RUNTIME, never hardcoded: add or rename an
       RDC and the matching columns appear on the next execution.
       {a} is the table-alias token, substituted per branch like @dDim.       */
    DECLARE @rdcMsa NVARCHAR(MAX), @rdcAlloc NVARCHAR(MAX), @rdcCnt NVARCHAR(MAX),
            @rdcAlc NVARCHAR(MAX), @rdcAlcOut NVARCHAR(MAX), @rdcOut NVARCHAR(MAX),
            @rdcHmov NVARCHAR(MAX), @rdcHold NVARCHAR(MAX), @rdcHoldOut NVARCHAR(MAX);
    ;WITH r AS (SELECT DISTINCT RDC FROM ARS_MSA_TOTAL WITH (NOLOCK)
                WHERE NULLIF(LTRIM(RTRIM(RDC)), '') IS NOT NULL)
    SELECT
      @rdcMsa = STRING_AGG(CAST(
          N', SUM(CASE WHEN {a}RDC = N''' + RDC + N''' THEN {a}FNL_Q ELSE 0 END) AS '
          + QUOTENAME(RDC + N'_OPQ') AS NVARCHAR(MAX)), N'') WITHIN GROUP (ORDER BY RDC),
      -- Consumption is attributed by SRC_RDC (the RDC the stock was allocated
      -- FROM), never by RDC (the store's connected RDC). Cross-RDC supply is
      -- routine here, so keying on the store's RDC over-depletes one pool and
      -- under-depletes the other, producing negative closing MSA.
      @rdcAlloc = STRING_AGG(CAST(
          N', SUM(CASE WHEN {a}SRC_RDC = N''' + RDC + N''' THEN ({a}HOLD_QTY + {a}ALLOC_QTY - {a}FROM_HOLD_QTY) ELSE 0 END) AS '
          + QUOTENAME(RDC + N'_CONS') AS NVARCHAR(MAX)), N'') WITHIN GROUP (ORDER BY RDC),
      @rdcCnt = STRING_AGG(CAST(
          N', SUM(CASE WHEN RDC = N''' + RDC + N''' THEN 1 ELSE 0 END) AS '
          + QUOTENAME(RDC + N'_CNT') AS NVARCHAR(MAX)), N'') WITHIN GROUP (ORDER BY RDC),
      -- ALC_Q split: SRC_RDC (which RDC shipped it), from the RDC SPLIT table.
      -- SRC_RDC is used for ALLOCATION ONLY.
      @rdcAlc = STRING_AGG(CAST(
          N', SUM(CASE WHEN {a}SRC_RDC = N''' + RDC + N''' THEN ISNULL({a}SHIP_QTY, 0) ELSE 0 END) AS '
          + QUOTENAME(RDC + N'_ALCQ') AS NVARCHAR(MAX)), N'') WITHIN GROUP (ORDER BY RDC),
      -- HOLD movement splits on RDC (the holding RDC), NOT SRC_RDC.
      @rdcHmov = STRING_AGG(CAST(
          N', SUM(CASE WHEN {a}RDC = N''' + RDC + N''' THEN ISNULL({a}HOLD_QTY, 0) ELSE 0 END) AS '
          + QUOTENAME(RDC + N'_HADD')
          + N', SUM(CASE WHEN {a}RDC = N''' + RDC + N''' THEN ISNULL({a}FROM_HOLD_QTY, 0) ELSE 0 END) AS '
          + QUOTENAME(RDC + N'_HCONS') AS NVARCHAR(MAX)), N'') WITHIN GROUP (ORDER BY RDC),
      -- HOLD remaining splits on RDC too (ARS_NL_TBL_HOLD_TRACKING.RDC)
      @rdcHold = STRING_AGG(CAST(
          N', SUM(CASE WHEN {a}RDC = N''' + RDC + N''' THEN ISNULL({a}HOLD_REM, 0) ELSE 0 END) AS '
          + QUOTENAME(RDC + N'_HCLOSE') AS NVARCHAR(MAX)), N'') WITHIN GROUP (ORDER BY RDC),
      @rdcAlcOut = STRING_AGG(CAST(
            N',
    ISNULL(alc_r.' + QUOTENAME(RDC + N'_ALCQ') + N', 0) AS ' + QUOTENAME(RDC + N'_ALC_Q')
          AS NVARCHAR(MAX)), N'') WITHIN GROUP (ORDER BY RDC),
      -- same four formulas as the base columns, per RDC
      @rdcHoldOut = STRING_AGG(CAST(
            N',
    ISNULL(hold_r.' + QUOTENAME(RDC + N'_HCLOSE') + N', 0) - ISNULL(hmov_r.' + QUOTENAME(RDC + N'_HADD') + N', 0) + ISNULL(hmov_r.' + QUOTENAME(RDC + N'_HCONS') + N', 0) AS ' + QUOTENAME(RDC + N'_HOLD_OPEN_REM') + N',
    ISNULL(hmov_r.' + QUOTENAME(RDC + N'_HCONS')  + N', 0) AS ' + QUOTENAME(RDC + N'_HOLD_CONSUMED_TODAY') + N',
    ISNULL(hmov_r.' + QUOTENAME(RDC + N'_HADD')   + N', 0) AS ' + QUOTENAME(RDC + N'_HOLD_ADDED_TODAY') + N',
    ISNULL(hold_r.' + QUOTENAME(RDC + N'_HCLOSE') + N', 0) AS ' + QUOTENAME(RDC + N'_HOLD_CLOSE_REM')
          AS NVARCHAR(MAX)), N'') WITHIN GROUP (ORDER BY RDC),
      @rdcOut = STRING_AGG(CAST(
            N',
    ISNULL(msa.' + QUOTENAME(RDC + N'_OPQ') + N', 0) AS ' + QUOTENAME(RDC + N'_MSA_OP_Q') + N',
    ISNULL(msa.' + QUOTENAME(RDC + N'_OPQ') + N', 0) - ISNULL(alloc.' + QUOTENAME(RDC + N'_CONS') + N', 0) AS ' + QUOTENAME(RDC + N'_MSA_CL_Q') + N',
    ISNULL(opt_op_r.' + QUOTENAME(RDC + N'_CNT') + N', 0) AS ' + QUOTENAME(RDC + N'_OP_OPT_CNT_>50_PCS') + N',
    ISNULL(opt_cl_r.' + QUOTENAME(RDC + N'_CNT') + N', 0) AS ' + QUOTENAME(RDC + N'_CL_OPT_CNT_>50_PCS')
          AS NVARCHAR(MAX)), N'') WITHIN GROUP (ORDER BY RDC)
    FROM r;

    -- msa: opening MSA total (ALL-RDC total [+ dim])
    DECLARE @msaCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN
        N'msa AS (
    SELECT MAJ_CAT, SUM(FNL_Q) AS OP_MSA_QTY' + REPLACE(@rdcMsa, N'{a}', N'') + N'
    FROM ARS_MSA_TOTAL_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR MAJ_CAT = @pMC)
    GROUP BY MAJ_CAT
)'
      ELSE
        N'msa AS (
    SELECT m.MAJ_CAT, pd.DIMV, SUM(m.FNL_Q) AS OP_MSA_QTY' + REPLACE(@rdcMsa, N'{a}', N'm.') + N'
    FROM ARS_MSA_TOTAL_HISTORY m WITH (NOLOCK)
    LEFT JOIN pdim pd ON pd.ARTICLE_NUMBER = m.ARTICLE_NUMBER
    WHERE m.SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR m.MAJ_CAT = @pMC)
    GROUP BY m.MAJ_CAT, pd.DIMV
)' END;

    -- alloc: consumption (ALL-RDC total [+ dim])
    DECLARE @allocCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN
        N'alloc AS (
    SELECT MAJ_CAT,
           SUM(HOLD_QTY + ALLOC_QTY - FROM_HOLD_QTY) AS consumed' + REPLACE(@rdcAlloc, N'{a}', N'') + N'
    FROM ARS_ALLOC_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR MAJ_CAT = @pMC)
    GROUP BY MAJ_CAT
)'
      ELSE
        N'alloc AS (
    SELECT a.MAJ_CAT, pd.DIMV,
           SUM(a.HOLD_QTY + a.ALLOC_QTY - a.FROM_HOLD_QTY) AS consumed' + REPLACE(@rdcAlloc, N'{a}', N'a.') + N'
    FROM ARS_ALLOC_HISTORY a WITH (NOLOCK)
    LEFT JOIN pdim pd ON pd.ARTICLE_NUMBER = a.VAR_ART
    WHERE a.SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR a.MAJ_CAT = @pMC)
    GROUP BY a.MAJ_CAT, pd.DIMV
)' END;

    -- opt>50 OPENING (ALL-RDC total [+ dim]) — # of (GEN_ART,CLR) with MSA qty > 50
    DECLARE @optOpCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN
        N'opt_gt50_op AS (
    SELECT MAJ_CAT, COUNT(*) AS OPT_GT50_CNT
    FROM (SELECT MAJ_CAT, GEN_ART_NUMBER, CLR, SUM(FNL_Q) q
          FROM ARS_MSA_TOTAL_HISTORY WITH (NOLOCK)
          WHERE SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR MAJ_CAT = @pMC)
          GROUP BY MAJ_CAT, GEN_ART_NUMBER, CLR HAVING SUM(FNL_Q) > 50) o
    GROUP BY MAJ_CAT
)'
      ELSE
        N'opt_gt50_op AS (
    SELECT MAJ_CAT, DIMV, COUNT(*) AS OPT_GT50_CNT
    FROM (SELECT m.MAJ_CAT, pd.DIMV, m.GEN_ART_NUMBER, m.CLR, SUM(m.FNL_Q) q
          FROM ARS_MSA_TOTAL_HISTORY m WITH (NOLOCK)
          LEFT JOIN pdim pd ON pd.ARTICLE_NUMBER = m.ARTICLE_NUMBER
          WHERE m.SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR m.MAJ_CAT = @pMC)
          GROUP BY m.MAJ_CAT, pd.DIMV, m.GEN_ART_NUMBER, m.CLR HAVING SUM(m.FNL_Q) > 50) o
    GROUP BY MAJ_CAT, DIMV
)' END;

    -- opt>50 CLOSING (live ARS_MSA_TOTAL, no session)
    DECLARE @optClCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN
        N'opt_gt50_cl AS (
    SELECT MAJ_CAT, COUNT(*) AS OPT_GT50_CNT
    FROM (SELECT MAJ_CAT, GEN_ART_NUMBER, CLR, SUM(FNL_Q) q
          FROM ARS_MSA_TOTAL WITH (NOLOCK)
          WHERE (@pMC IS NULL OR MAJ_CAT = @pMC)
          GROUP BY MAJ_CAT, GEN_ART_NUMBER, CLR HAVING SUM(FNL_Q) > 50) o
    GROUP BY MAJ_CAT
)'
      ELSE
        N'opt_gt50_cl AS (
    SELECT MAJ_CAT, DIMV, COUNT(*) AS OPT_GT50_CNT
    FROM (SELECT m.MAJ_CAT, pd.DIMV, m.GEN_ART_NUMBER, m.CLR, SUM(m.FNL_Q) q
          FROM ARS_MSA_TOTAL m WITH (NOLOCK)
          LEFT JOIN pdim pd ON pd.ARTICLE_NUMBER = m.ARTICLE_NUMBER
          WHERE (@pMC IS NULL OR m.MAJ_CAT = @pMC)
          GROUP BY m.MAJ_CAT, pd.DIMV, m.GEN_ART_NUMBER, m.CLR HAVING SUM(m.FNL_Q) > 50) o
    GROUP BY MAJ_CAT, DIMV
)' END;

    DECLARE @hmovCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN
        N'hmov AS (
    SELECT WERKS, MAJ_CAT, SUM(HOLD_QTY) AS HOLD_QTY, SUM(ISNULL(FROM_HOLD_QTY,0)) AS from_hold
    FROM ARS_ALLOC_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR MAJ_CAT = @pMC)
      AND (@pW IS NULL OR WERKS = @pW)
    GROUP BY WERKS, MAJ_CAT
)'
      ELSE
        N'hmov AS (
    SELECT a.WERKS, a.MAJ_CAT, pd.DIMV, SUM(a.HOLD_QTY) AS HOLD_QTY, SUM(ISNULL(a.FROM_HOLD_QTY,0)) AS from_hold
    FROM ARS_ALLOC_HISTORY a WITH (NOLOCK)
    LEFT JOIN pdim pd ON pd.ARTICLE_NUMBER = a.VAR_ART
    WHERE a.SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR a.MAJ_CAT = @pMC)
    GROUP BY a.WERKS, a.MAJ_CAT, pd.DIMV
)' END;

    DECLARE @holdCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN
        N'hold AS (
    SELECT WERKS, MAJ_CAT, SUM(ISNULL(HOLD_REM,0)) AS CLOSE_REM
    FROM ARS_NL_TBL_HOLD_TRACKING WITH (NOLOCK)
    WHERE (@pW IS NULL OR WERKS = @pW) AND (@pMC IS NULL OR MAJ_CAT = @pMC)
    GROUP BY WERKS, MAJ_CAT
)'
      ELSE
        N'hold AS (
    SELECT h.WERKS, h.MAJ_CAT, pd.DIMV, SUM(ISNULL(h.HOLD_REM,0)) AS CLOSE_REM
    FROM ARS_NL_TBL_HOLD_TRACKING h WITH (NOLOCK)
    LEFT JOIN pdim pd ON pd.ARTICLE_NUMBER = h.VAR_ART
    WHERE (@pW IS NULL OR h.WERKS = @pW) AND (@pMC IS NULL OR h.MAJ_CAT = @pMC)
    GROUP BY h.WERKS, h.MAJ_CAT, pd.DIMV
)' END;

    -- Join dim conditions (empty on base grid)
    DECLARE @dLst NVARCHAR(80) = CASE WHEN @DimCol IS NOT NULL AND @lstDim = 1 THEN N' AND lst.DIMV = g.' + @qd ELSE N'' END;
    DECLARE @dDim NVARCHAR(80) = CASE WHEN @DimCol IS NULL THEN N'' ELSE N' AND {a}.DIMV = g.' + @qd END;

    -- optional filters (parameterized — safe from injection)
    DECLARE @where NVARCHAR(400) = N'';
    IF @WERKS   IS NOT NULL SET @where += N' AND g.WERKS = @pW';
    IF @MAJ_CAT IS NOT NULL SET @where += N' AND g.MAJ_CAT = @pMC';
    IF LEN(@where) > 0 SET @where = N'
WHERE 1 = 1' + @where;

    -- ALC_Q per SRC_RDC. STORE-level measure, so keyed on WERKS + MAJ_CAT.
    -- Sourced from ARS_ALLOC_HISTORY, not the lst CTE: ARS_LISTING_WORKING_HISTORY
    -- carries no SRC_RDC. The two agree on ALLOC_QTY, so these columns reconcile
    -- to the existing ALC_Q (verified M_JEANS 22,402 both sides).
    DECLARE @alcRCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN
        N'alc_r AS (
    SELECT WERKS, MAJ_CAT' + REPLACE(@rdcAlc, N'{a}', N'') + N'
    FROM ARS_ALLOC_RDC_SPLIT_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR MAJ_CAT = @pMC)
      AND (@pW IS NULL OR WERKS = @pW)
    GROUP BY WERKS, MAJ_CAT
)'
      ELSE
        N'alc_r AS (
    SELECT a.WERKS, a.MAJ_CAT, pd.DIMV' + REPLACE(@rdcAlc, N'{a}', N'a.') + N'
    FROM ARS_ALLOC_RDC_SPLIT_HISTORY a WITH (NOLOCK)
    LEFT JOIN pdim pd ON pd.ARTICLE_NUMBER = a.VAR_ART
    WHERE a.SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR a.MAJ_CAT = @pMC)
      AND (@pW IS NULL OR a.WERKS = @pW)
    GROUP BY a.WERKS, a.MAJ_CAT, pd.DIMV
)' END;

    -- hold CONSUMED per SRC_RDC (FROM_HOLD_QTY lives only in ARS_ALLOC_HISTORY)
    DECLARE @hmovRCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN
        N'hmov_r AS (
    SELECT WERKS, MAJ_CAT' + REPLACE(@rdcHmov, N'{a}', N'') + N'
    FROM ARS_ALLOC_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR MAJ_CAT = @pMC)
      AND (@pW IS NULL OR WERKS = @pW)
    GROUP BY WERKS, MAJ_CAT
)'
      ELSE
        N'hmov_r AS (
    SELECT a.WERKS, a.MAJ_CAT, pd.DIMV' + REPLACE(@rdcHmov, N'{a}', N'a.') + N'
    FROM ARS_ALLOC_HISTORY a WITH (NOLOCK)
    LEFT JOIN pdim pd ON pd.ARTICLE_NUMBER = a.VAR_ART
    WHERE a.SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR a.MAJ_CAT = @pMC)
      AND (@pW IS NULL OR a.WERKS = @pW)
    GROUP BY a.WERKS, a.MAJ_CAT, pd.DIMV
)' END;

    -- HOLD remaining per RDC (holding RDC, not SRC_RDC). 391 of 421 stores
    -- hold stock at BOTH RDCs, so this split is real, not a copy + a zero.
    DECLARE @holdRCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN
        N'hold_r AS (
    SELECT WERKS, MAJ_CAT' + REPLACE(@rdcHold, N'{a}', N'') + N'
    FROM ARS_NL_TBL_HOLD_TRACKING WITH (NOLOCK)
    WHERE (@pW IS NULL OR WERKS = @pW) AND (@pMC IS NULL OR MAJ_CAT = @pMC)
    GROUP BY WERKS, MAJ_CAT
)'
      ELSE
        N'hold_r AS (
    SELECT h.WERKS, h.MAJ_CAT, pd.DIMV' + REPLACE(@rdcHold, N'{a}', N'h.') + N'
    FROM ARS_NL_TBL_HOLD_TRACKING h WITH (NOLOCK)
    LEFT JOIN pdim pd ON pd.ARTICLE_NUMBER = h.VAR_ART
    WHERE (@pW IS NULL OR h.WERKS = @pW) AND (@pMC IS NULL OR h.MAJ_CAT = @pMC)
    GROUP BY h.WERKS, h.MAJ_CAT, pd.DIMV
)' END;

    -- per-RDC >50 qualification (counted WITHIN each RDC, unlike the all-RDC CTEs)
    DECLARE @optOpRCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN
        N'opt_op_r AS (
    SELECT MAJ_CAT' + @rdcCnt + N'
    FROM (SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR
          FROM ARS_MSA_TOTAL_HISTORY WITH (NOLOCK)
          WHERE SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR MAJ_CAT = @pMC)
          GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR HAVING SUM(FNL_Q) > 50) o
    GROUP BY MAJ_CAT
)'
      ELSE
        N'opt_op_r AS (
    SELECT MAJ_CAT, DIMV' + @rdcCnt + N'
    FROM (SELECT m.RDC, m.MAJ_CAT, pd.DIMV, m.GEN_ART_NUMBER, m.CLR
          FROM ARS_MSA_TOTAL_HISTORY m WITH (NOLOCK)
          LEFT JOIN pdim pd ON pd.ARTICLE_NUMBER = m.ARTICLE_NUMBER
          WHERE m.SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR m.MAJ_CAT = @pMC)
          GROUP BY m.RDC, m.MAJ_CAT, pd.DIMV, m.GEN_ART_NUMBER, m.CLR HAVING SUM(m.FNL_Q) > 50) o
    GROUP BY MAJ_CAT, DIMV
)' END;

    DECLARE @optClRCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN
        N'opt_cl_r AS (
    SELECT MAJ_CAT' + @rdcCnt + N'
    FROM (SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR
          FROM ARS_MSA_TOTAL WITH (NOLOCK)
          WHERE (@pMC IS NULL OR MAJ_CAT = @pMC)
          GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR HAVING SUM(FNL_Q) > 50) o
    GROUP BY MAJ_CAT
)'
      ELSE
        N'opt_cl_r AS (
    SELECT MAJ_CAT, DIMV' + @rdcCnt + N'
    FROM (SELECT m.RDC, m.MAJ_CAT, pd.DIMV, m.GEN_ART_NUMBER, m.CLR
          FROM ARS_MSA_TOTAL m WITH (NOLOCK)
          LEFT JOIN pdim pd ON pd.ARTICLE_NUMBER = m.ARTICLE_NUMBER
          WHERE (@pMC IS NULL OR m.MAJ_CAT = @pMC)
          GROUP BY m.RDC, m.MAJ_CAT, pd.DIMV, m.GEN_ART_NUMBER, m.CLR HAVING SUM(m.FNL_Q) > 50) o
    GROUP BY MAJ_CAT, DIMV
)' END;

    SET @sql = N'
WITH sid AS (SELECT MAX(SESSION_ID) AS SESSION_ID FROM ARS_LISTING_HISTORY WITH (NOLOCK))' + @pdimCTE + N',
' + @lstCTE + N',
' + @msaCTE + N',
' + @optOpCTE + N',
' + @optClCTE + N',
' + @optOpRCTE + N',
' + @optClRCTE + N',
' + @alcRCTE + N',
' + @hmovRCTE + N',
' + @holdRCTE + N',
' + @allocCTE + N',
' + @hmovCTE + N',
' + @holdCTE + N',
prod AS (
    SELECT MAJ_CAT, MAX(SEG) AS SEG, MAX(DIV) AS DIV, MAX(SUB_DIV) AS SUB_DIV, MAX(SSN) AS SSN
    FROM vw_master_product WITH (NOLOCK)
    GROUP BY MAJ_CAT
)' + @statFrag + N'
SELECT
    (SELECT SESSION_ID FROM sid) AS SESSION_ID,
    (SELECT MAX(APPROVED_AT) FROM ARS_LISTING_WORKING_HISTORY WITH (NOLOCK)
     WHERE SESSION_ID = (SELECT SESSION_ID FROM sid)) AS APPROVED_AT,
    ISNULL(s.ST_NM, '''')       AS STORE_NAME,
    ISNULL(s.RDC, '''')         AS RDC,
    ISNULL(s.HUB, '''')         AS HUB,
    ISNULL(s.ST_STATUS, '''')   AS ST_STATUS,
    ISNULL(p.SEG, '''')         AS SEG,
    ISNULL(p.DIV, '''')         AS DIV,
    ISNULL(p.SUB_DIV, '''')     AS SUB_DIV,
    ISNULL(p.SSN, '''')         AS SSN,
    ' + @gSel + N',
    ISNULL(lst.ALC_Q, 0)            AS ALC_Q' + @rdcAlcOut + N',
    ISNULL(lst.HOLD_ALC_Q, 0)       AS HOLD_ALC_Q,
    ISNULL(lst.ALC_FROM_HOLD_Q, 0)  AS ALC_FROM_HOLD_Q,
    ISNULL(lst.ART_EXCESS_QTY, 0)   AS ART_EXCESS_QTY,
    ISNULL(msa.OP_MSA_QTY, 0)                          AS MSA_OP_Q,
    ISNULL(msa.OP_MSA_QTY, 0) - ISNULL(alloc.consumed, 0) AS MSA_CL_Q,
    ISNULL(opt_gt50_op.OPT_GT50_CNT, 0)                AS [OP_OPT_CNT_>50_PCS],
    ISNULL(opt_gt50_cl.OPT_GT50_CNT, 0)                AS [CL_OPT_CNT_>50_PCS]' + @rdcOut + N',
    ISNULL(hold.CLOSE_REM, 0) - ISNULL(hmov.HOLD_QTY, 0) + ISNULL(hmov.from_hold, 0) AS HOLD_OPEN_REM,
    ISNULL(hmov.from_hold, 0)                          AS HOLD_CONSUMED_TODAY,
    ISNULL(hmov.HOLD_QTY, 0)                           AS HOLD_ADDED_TODAY,
    ISNULL(hold.CLOSE_REM, 0)                          AS HOLD_CLOSE_REM' + @rdcHoldOut + N',
    /* Per-OPTION MBQ at this grid grain, mirroring the engine formula
       OPT_MBQ = ACS_D + rate x ALC_D (listing.py Part 4c).
       SAL_PD is the CATEGORY total daily sale, so it MUST be divided by
       OPT_CNT to become a per-option rate: SAL_PD/OPT_CNT tracks the
       engine MAX_DAILY_SALE (validated 0.4393 vs 0.4311 on HJ08/L_KURTI_ST;
       mean abs error across 184,735 rows 14.15 -> 0.67 pcs).
       OPT_CNT = 0 => no options, so the display term ACS_D stands alone. */
    CASE WHEN ISNULL(g.OPT_CNT,0) > 0
         THEN ISNULL(g.ACS_D,0) + (ISNULL(g.SAL_PD,0) / g.OPT_CNT) * ISNULL(g.ALC_D,0)
         ELSE ISNULL(g.ACS_D,0)
    END AS MJ_PER_OPT_MBQ' + @statOutFrag + N'
FROM ' + QUOTENAME(@GridTable) + N' g WITH (NOLOCK)
LEFT JOIN MASTER_ALC_INPUT_ST_MASTER s WITH (NOLOCK) ON s.ST_CD = g.WERKS
LEFT JOIN lst         ON lst.WERKS = g.WERKS AND lst.MAJ_CAT = g.MAJ_CAT' + @dLst + N'
LEFT JOIN msa         ON msa.MAJ_CAT = g.MAJ_CAT' + REPLACE(@dDim, N'{a}', N'msa') + N'
LEFT JOIN alloc       ON alloc.MAJ_CAT = g.MAJ_CAT' + REPLACE(@dDim, N'{a}', N'alloc') + N'
LEFT JOIN opt_gt50_op ON opt_gt50_op.MAJ_CAT = g.MAJ_CAT' + REPLACE(@dDim, N'{a}', N'opt_gt50_op') + N'
LEFT JOIN opt_gt50_cl ON opt_gt50_cl.MAJ_CAT = g.MAJ_CAT' + REPLACE(@dDim, N'{a}', N'opt_gt50_cl') + N'
LEFT JOIN opt_op_r    ON opt_op_r.MAJ_CAT = g.MAJ_CAT' + REPLACE(@dDim, N'{a}', N'opt_op_r') + N'
LEFT JOIN opt_cl_r    ON opt_cl_r.MAJ_CAT = g.MAJ_CAT' + REPLACE(@dDim, N'{a}', N'opt_cl_r') + N'
LEFT JOIN alc_r       ON alc_r.WERKS = g.WERKS AND alc_r.MAJ_CAT = g.MAJ_CAT' + REPLACE(@dDim, N'{a}', N'alc_r') + N'
LEFT JOIN hmov_r      ON hmov_r.WERKS = g.WERKS AND hmov_r.MAJ_CAT = g.MAJ_CAT' + REPLACE(@dDim, N'{a}', N'hmov_r') + N'
LEFT JOIN hold_r      ON hold_r.WERKS = g.WERKS AND hold_r.MAJ_CAT = g.MAJ_CAT' + REPLACE(@dDim, N'{a}', N'hold_r') + N'
LEFT JOIN hmov        ON hmov.WERKS = g.WERKS AND hmov.MAJ_CAT = g.MAJ_CAT' + REPLACE(@dDim, N'{a}', N'hmov') + N'
LEFT JOIN hold        ON hold.WERKS = g.WERKS AND hold.MAJ_CAT = g.MAJ_CAT' + REPLACE(@dDim, N'{a}', N'hold') + N'
LEFT JOIN prod   p ON p.MAJ_CAT = g.MAJ_CAT' + @statJoin + @where + N'
ORDER BY s.RDC, s.HUB, g.WERKS, g.MAJ_CAT
OPTION (RECOMPILE);';

    EXEC sp_executesql @sql,
         N'@pW NVARCHAR(50), @pMC NVARCHAR(100)',
         @pW = @WERKS, @pMC = @MAJ_CAT;
END
GO
