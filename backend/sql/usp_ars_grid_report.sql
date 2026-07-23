/* ============================================================================
   usp_ars_grid_report — COMPLETE GRID REPORT as a stored procedure.

   Works for the MJ grid and every secondary grid (MERGE_RNG_SEG / RNG_SEG /
   M_YARN_02 / FAB / CLR / ...). @DimCol is auto-detected = the column
   @GridTable has that ARS_GRID_MJ does NOT.

     • @GridTable : grid table to report on (default ARS_GRID_MJ)
     • @WERKS     : optional single-store filter (NULL = all stores)
     • @MAJ_CAT   : optional single-category filter (NULL = all categories)

   Grain = (WERKS, MAJ_CAT [, dim]).  On a secondary grid EVERY measure is split
   by the grid dimension so each dim row shows its own value and the dim rows sum
   back to the MAJ_CAT total (no repeated/inflated values):
     - lst (ALC_Q / HOLD_ALC_Q / ALC_FROM_HOLD_Q / ART_EXCESS_QTY): STORE + dim
       (from the listing table's own dim column).
     - msa / alloc / opt>50 (MSA_OP_Q / MSA_CL_Q / OP&CL_OPT_CNT): RDC + dim
       (dim mapped from vw_master_product on the article number).
     - HOLD_* : STORE + dim (mapped on VAR_ART).
   On the base MJ grid there is no dim, so all measures are at their natural grain.
   Hold movement: opening = closing - added_today + consumed_today.

   Examples:
     EXEC dbo.usp_ars_grid_report;
     EXEC dbo.usp_ars_grid_report @GridTable = N'ARS_GRID_MJ_RNG_SEG';
     EXEC dbo.usp_ars_grid_report @GridTable = N'ARS_GRID_MJ_RNG_SEG',
                                  @WERKS = N'HB05', @MAJ_CAT = N'M_JEANS';
   ============================================================================ */
CREATE OR ALTER PROCEDURE dbo.usp_ars_grid_report
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
    GROUP BY WERKS, MAJ_CAT
)'
      ELSE
        N'lst AS (
    SELECT WERKS, MAJ_CAT, ' + @qd + N' AS DIMV,
           SUM(ALLOC_QTY) AS ALC_Q, SUM(HOLD_QTY) AS HOLD_ALC_Q,
           ISNULL(SUM(FROM_HOLD_QTY),0) AS ALC_FROM_HOLD_Q, SUM(ART_EXCESS) AS ART_EXCESS_QTY
    FROM ARS_LISTING_WORKING_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR MAJ_CAT = @pMC)
    GROUP BY WERKS, MAJ_CAT, ' + @qd + N'
)' END;

    -- msa: opening MSA total (RDC grain [+ dim via pdim on ARTICLE_NUMBER])
    DECLARE @msaCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN
        N'msa AS (
    SELECT RDC, MAJ_CAT, SUM(FNL_Q) AS OP_MSA_QTY
    FROM ARS_MSA_TOTAL_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR MAJ_CAT = @pMC)
    GROUP BY RDC, MAJ_CAT
)'
      ELSE
        N'msa AS (
    SELECT m.RDC, m.MAJ_CAT, pd.DIMV, SUM(m.FNL_Q) AS OP_MSA_QTY
    FROM ARS_MSA_TOTAL_HISTORY m WITH (NOLOCK)
    LEFT JOIN pdim pd ON pd.ARTICLE_NUMBER = m.ARTICLE_NUMBER
    WHERE m.SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR m.MAJ_CAT = @pMC)
    GROUP BY m.RDC, m.MAJ_CAT, pd.DIMV
)' END;

    -- alloc: consumption (RDC grain [+ dim via pdim on VAR_ART])
    DECLARE @allocCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN
        N'alloc AS (
    SELECT RDC, MAJ_CAT,
           SUM(HOLD_QTY + ALLOC_QTY - FROM_HOLD_QTY) AS consumed
    FROM ARS_ALLOC_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR MAJ_CAT = @pMC)
    GROUP BY RDC, MAJ_CAT
)'
      ELSE
        N'alloc AS (
    SELECT a.RDC, a.MAJ_CAT, pd.DIMV,
           SUM(a.HOLD_QTY + a.ALLOC_QTY - a.FROM_HOLD_QTY) AS consumed
    FROM ARS_ALLOC_HISTORY a WITH (NOLOCK)
    LEFT JOIN pdim pd ON pd.ARTICLE_NUMBER = a.VAR_ART
    WHERE a.SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR a.MAJ_CAT = @pMC)
    GROUP BY a.RDC, a.MAJ_CAT, pd.DIMV
)' END;

    -- opt>50 OPENING (RDC grain [+ dim]) — # of (GEN_ART,CLR) with MSA qty > 50
    DECLARE @optOpCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN
        N'opt_gt50_op AS (
    SELECT RDC, MAJ_CAT, COUNT(*) AS OPT_GT50_CNT
    FROM (SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, SUM(FNL_Q) q
          FROM ARS_MSA_TOTAL_HISTORY WITH (NOLOCK)
          WHERE SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR MAJ_CAT = @pMC)
          GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR HAVING SUM(FNL_Q) > 50) o
    GROUP BY RDC, MAJ_CAT
)'
      ELSE
        N'opt_gt50_op AS (
    SELECT RDC, MAJ_CAT, DIMV, COUNT(*) AS OPT_GT50_CNT
    FROM (SELECT m.RDC, m.MAJ_CAT, pd.DIMV, m.GEN_ART_NUMBER, m.CLR, SUM(m.FNL_Q) q
          FROM ARS_MSA_TOTAL_HISTORY m WITH (NOLOCK)
          LEFT JOIN pdim pd ON pd.ARTICLE_NUMBER = m.ARTICLE_NUMBER
          WHERE m.SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR m.MAJ_CAT = @pMC)
          GROUP BY m.RDC, m.MAJ_CAT, pd.DIMV, m.GEN_ART_NUMBER, m.CLR HAVING SUM(m.FNL_Q) > 50) o
    GROUP BY RDC, MAJ_CAT, DIMV
)' END;

    -- opt>50 CLOSING (live ARS_MSA_TOTAL, no session)
    DECLARE @optClCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN
        N'opt_gt50_cl AS (
    SELECT RDC, MAJ_CAT, COUNT(*) AS OPT_GT50_CNT
    FROM (SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, SUM(FNL_Q) q
          FROM ARS_MSA_TOTAL WITH (NOLOCK)
          WHERE (@pMC IS NULL OR MAJ_CAT = @pMC)
          GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR HAVING SUM(FNL_Q) > 50) o
    GROUP BY RDC, MAJ_CAT
)'
      ELSE
        N'opt_gt50_cl AS (
    SELECT RDC, MAJ_CAT, DIMV, COUNT(*) AS OPT_GT50_CNT
    FROM (SELECT m.RDC, m.MAJ_CAT, pd.DIMV, m.GEN_ART_NUMBER, m.CLR, SUM(m.FNL_Q) q
          FROM ARS_MSA_TOTAL m WITH (NOLOCK)
          LEFT JOIN pdim pd ON pd.ARTICLE_NUMBER = m.ARTICLE_NUMBER
          WHERE (@pMC IS NULL OR m.MAJ_CAT = @pMC)
          GROUP BY m.RDC, m.MAJ_CAT, pd.DIMV, m.GEN_ART_NUMBER, m.CLR HAVING SUM(m.FNL_Q) > 50) o
    GROUP BY RDC, MAJ_CAT, DIMV
)' END;

    DECLARE @hmovCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN
        N'hmov AS (
    SELECT WERKS, MAJ_CAT, SUM(HOLD_QTY) AS HOLD_QTY, SUM(ISNULL(FROM_HOLD_QTY,0)) AS from_hold
    FROM ARS_ALLOC_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID = (SELECT SESSION_ID FROM sid) AND (@pMC IS NULL OR MAJ_CAT = @pMC)
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

    SET @sql = N'
WITH sid AS (SELECT MAX(SESSION_ID) AS SESSION_ID FROM ARS_LISTING_HISTORY WITH (NOLOCK))' + @pdimCTE + N',
' + @lstCTE + N',
' + @msaCTE + N',
' + @optOpCTE + N',
' + @optClCTE + N',
' + @allocCTE + N',
' + @hmovCTE + N',
' + @holdCTE + N',
prod AS (
    SELECT MAJ_CAT, MAX(SEG) AS SEG, MAX(DIV) AS DIV, MAX(SUB_DIV) AS SUB_DIV, MAX(SSN) AS SSN
    FROM vw_master_product WITH (NOLOCK)
    GROUP BY MAJ_CAT
)
SELECT
    ISNULL(s.ST_NM, '''')       AS STORE_NAME,
    ISNULL(s.RDC, '''')         AS RDC,
    ISNULL(s.HUB, '''')         AS HUB,
    ISNULL(s.ST_STATUS, '''')   AS ST_STATUS,
    ISNULL(p.SEG, '''')         AS SEG,
    ISNULL(p.DIV, '''')         AS DIV,
    ISNULL(p.SUB_DIV, '''')     AS SUB_DIV,
    ISNULL(p.SSN, '''')         AS SSN,
    ' + @gSel + N',
    ISNULL(lst.ALC_Q, 0)            AS ALC_Q,
    ISNULL(lst.HOLD_ALC_Q, 0)       AS HOLD_ALC_Q,
    ISNULL(lst.ALC_FROM_HOLD_Q, 0)  AS ALC_FROM_HOLD_Q,
    ISNULL(lst.ART_EXCESS_QTY, 0)   AS ART_EXCESS_QTY,
    ISNULL(msa.OP_MSA_QTY, 0)                          AS MSA_OP_Q,
    ISNULL(msa.OP_MSA_QTY, 0) - ISNULL(alloc.consumed, 0) AS MSA_CL_Q,
    ISNULL(opt_gt50_op.OPT_GT50_CNT, 0)                AS [OP_OPT_CNT_>50_PCS],
    ISNULL(opt_gt50_cl.OPT_GT50_CNT, 0)                AS [CL_OPT_CNT_>50_PCS],
    ISNULL(hold.CLOSE_REM, 0) - ISNULL(hmov.HOLD_QTY, 0) + ISNULL(hmov.from_hold, 0) AS HOLD_OPEN_REM,
    ISNULL(hmov.from_hold, 0)                          AS HOLD_CONSUMED_TODAY,
    ISNULL(hmov.HOLD_QTY, 0)                           AS HOLD_ADDED_TODAY,
    ISNULL(hold.CLOSE_REM, 0)                          AS HOLD_CLOSE_REM
FROM ' + QUOTENAME(@GridTable) + N' g WITH (NOLOCK)
LEFT JOIN MASTER_ALC_INPUT_ST_MASTER s WITH (NOLOCK) ON s.ST_CD = g.WERKS
LEFT JOIN lst         ON lst.WERKS = g.WERKS AND lst.MAJ_CAT = g.MAJ_CAT' + @dLst + N'
LEFT JOIN msa         ON msa.RDC = s.RDC AND msa.MAJ_CAT = g.MAJ_CAT' + REPLACE(@dDim, N'{a}', N'msa') + N'
LEFT JOIN alloc       ON alloc.RDC = s.RDC AND alloc.MAJ_CAT = g.MAJ_CAT' + REPLACE(@dDim, N'{a}', N'alloc') + N'
LEFT JOIN opt_gt50_op ON opt_gt50_op.RDC = s.RDC AND opt_gt50_op.MAJ_CAT = g.MAJ_CAT' + REPLACE(@dDim, N'{a}', N'opt_gt50_op') + N'
LEFT JOIN opt_gt50_cl ON opt_gt50_cl.RDC = s.RDC AND opt_gt50_cl.MAJ_CAT = g.MAJ_CAT' + REPLACE(@dDim, N'{a}', N'opt_gt50_cl') + N'
LEFT JOIN hmov        ON hmov.WERKS = g.WERKS AND hmov.MAJ_CAT = g.MAJ_CAT' + REPLACE(@dDim, N'{a}', N'hmov') + N'
LEFT JOIN hold        ON hold.WERKS = g.WERKS AND hold.MAJ_CAT = g.MAJ_CAT' + REPLACE(@dDim, N'{a}', N'hold') + N'
LEFT JOIN prod   p ON p.MAJ_CAT = g.MAJ_CAT' + @where + N'
ORDER BY s.RDC, s.HUB, g.WERKS, g.MAJ_CAT
OPTION (RECOMPILE);';

    EXEC sp_executesql @sql,
         N'@pW NVARCHAR(50), @pMC NVARCHAR(100)',
         @pW = @WERKS, @pMC = @MAJ_CAT;
END
GO
