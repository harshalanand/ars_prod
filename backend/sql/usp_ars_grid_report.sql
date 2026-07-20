/* ============================================================================
   usp_ars_grid_report — COMPLETE GRID REPORT as a stored procedure.

   Works for the MJ grid and every secondary grid (MERGE_RNG_SEG / RNG_SEG /
   M_YARN_02 / FAB / CLR / ...). @DimCol is auto-detected = the column
   @GridTable has that ARS_GRID_MJ does NOT.

     • @GridTable : grid table to report on (default ARS_GRID_MJ)
     • @WERKS     : optional single-store filter (NULL = all stores)
     • @MAJ_CAT   : optional single-category filter (NULL = all categories)

   Grain = (WERKS, MAJ_CAT [, dim]).
     - MSA_* / OPT_CNT : RDC+MAJ_CAT totals, repeated per store/dim row — do NOT SUM.
     - HOLD_*          : STORE-level; on a secondary grid SPLIT by the grid dimension
       (mapped from vw_master_product on VAR_ART), so dim rows sum to the MAJ_CAT total.
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

    -- Validate the grid table exists (guards the dynamic FROM).
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

    -- Every grid column, prefixed g., with type-aware ISNULL:
    --   numeric -> 0,  text -> '',  anything else left as-is.  Original name kept via AS.
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

    -- ── dimension-aware fragments (base form when @DimCol IS NULL) ────────────
    DECLARE @pdimCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN N'' ELSE
        N',
pdim AS (                           -- one dimension value per variant article
    SELECT ARTICLE_NUMBER, MAX(' + @qd + N') AS DIMV
    FROM vw_master_product WITH (NOLOCK)
    GROUP BY ARTICLE_NUMBER
)' END;

    DECLARE @hmovCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN
        N'hmov AS (                          -- hold movement this run per (WERKS, MAJ_CAT)
    SELECT WERKS, MAJ_CAT,
           SUM(HOLD_QTY)                 AS HOLD_QTY,
           SUM(ISNULL(FROM_HOLD_QTY, 0)) AS from_hold
    FROM ARS_ALLOC_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID = (SELECT SESSION_ID FROM sid)
    GROUP BY WERKS, MAJ_CAT
)'
      ELSE
        N'hmov AS (                          -- hold movement this run per (WERKS, MAJ_CAT, dim)
    SELECT a.WERKS, a.MAJ_CAT, pd.DIMV,
           SUM(a.HOLD_QTY)                 AS HOLD_QTY,
           SUM(ISNULL(a.FROM_HOLD_QTY, 0)) AS from_hold
    FROM ARS_ALLOC_HISTORY a WITH (NOLOCK)
    LEFT JOIN pdim pd ON pd.ARTICLE_NUMBER = a.VAR_ART
    WHERE a.SESSION_ID = (SELECT SESSION_ID FROM sid)
    GROUP BY a.WERKS, a.MAJ_CAT, pd.DIMV
)' END;

    DECLARE @holdCTE NVARCHAR(MAX) = CASE WHEN @DimCol IS NULL THEN
        N'hold AS (                          -- current hold snapshot per (WERKS, MAJ_CAT)
    SELECT WERKS, MAJ_CAT,
           SUM(ISNULL(HOLD_REM, 0)) AS CLOSE_REM
    FROM ARS_NL_TBL_HOLD_TRACKING WITH (NOLOCK)
    GROUP BY WERKS, MAJ_CAT
)'
      ELSE
        N'hold AS (                          -- current hold snapshot per (WERKS, MAJ_CAT, dim)
    SELECT h.WERKS, h.MAJ_CAT, pd.DIMV,
           SUM(ISNULL(h.HOLD_REM, 0)) AS CLOSE_REM
    FROM ARS_NL_TBL_HOLD_TRACKING h WITH (NOLOCK)
    LEFT JOIN pdim pd ON pd.ARTICLE_NUMBER = h.VAR_ART
    GROUP BY h.WERKS, h.MAJ_CAT, pd.DIMV
)' END;

    DECLARE @hmovJoin NVARCHAR(500) =
        N'LEFT JOIN hmov ON hmov.WERKS = g.WERKS AND hmov.MAJ_CAT = g.MAJ_CAT'
        + CASE WHEN @DimCol IS NULL THEN N'' ELSE N' AND hmov.DIMV = g.' + @qd END;
    DECLARE @holdJoin NVARCHAR(500) =
        N'LEFT JOIN hold ON hold.WERKS = g.WERKS AND hold.MAJ_CAT = g.MAJ_CAT'
        + CASE WHEN @DimCol IS NULL THEN N'' ELSE N' AND hold.DIMV = g.' + @qd END;

    -- optional filters (parameterized — safe from injection)
    DECLARE @where NVARCHAR(400) = N'';
    IF @WERKS   IS NOT NULL SET @where += N' AND g.WERKS = @pW';
    IF @MAJ_CAT IS NOT NULL SET @where += N' AND g.MAJ_CAT = @pMC';
    IF LEN(@where) > 0 SET @where = N'
WHERE 1 = 1' + @where;

    SET @sql = N'
WITH sid AS (                       -- latest listing session
    SELECT MAX(SESSION_ID) AS SESSION_ID FROM ARS_LISTING_HISTORY
)' + @pdimCTE + N',
lst AS (                            -- listing working result per (WERKS, MAJ_CAT)
    SELECT WERKS, MAJ_CAT,
           SUM(ALLOC_QTY)                 AS ALC_Q,
           SUM(HOLD_QTY)                  AS HOLD_ALC_Q,
           ISNULL(SUM(FROM_HOLD_QTY), 0)  AS ALC_FROM_HOLD_Q,
           SUM(ART_EXCESS)                AS ART_EXCESS_QTY
    FROM ARS_LISTING_WORKING_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID = (SELECT SESSION_ID FROM sid)
    GROUP BY WERKS, MAJ_CAT
),
msa AS (                            -- MSA total per (RDC, MAJ_CAT)
    SELECT RDC, MAJ_CAT, SUM(FNL_Q) AS OP_MSA_QTY
    FROM ARS_MSA_TOTAL_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID = (SELECT SESSION_ID FROM sid)
    GROUP BY RDC, MAJ_CAT
),
opt_gt50 AS (                       -- # of OPTs (GEN_ART+CLR) with MSA qty > 50, per (RDC, MAJ_CAT)
    SELECT RDC, MAJ_CAT, COUNT(*) AS OPT_GT50_CNT
    FROM (
        SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, SUM(FNL_Q) AS OPT_MSA_QTY
        FROM ARS_MSA_TOTAL_HISTORY WITH (NOLOCK)
        WHERE SESSION_ID = (SELECT SESSION_ID FROM sid)
        GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR
        HAVING SUM(FNL_Q) > 50
    ) o
    GROUP BY RDC, MAJ_CAT
),
alloc AS (                          -- allocation consumption per (RDC, MAJ_CAT)
    SELECT RDC, MAJ_CAT,
           SUM(HOLD_QTY + ALLOC_QTY - FROM_HOLD_QTY) AS consumed,
           SUM(ALLOC_QTY)                            AS alloc_consumed
    FROM ARS_ALLOC_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID = (SELECT SESSION_ID FROM sid)
    GROUP BY RDC, MAJ_CAT
),
' + @hmovCTE + N',
' + @holdCTE + N',
prod AS (                           -- product attributes collapsed to one row per MAJ_CAT
    SELECT MAJ_CAT,
           MAX(SEG)     AS SEG,
           MAX(DIV)     AS DIV,
           MAX(SUB_DIV) AS SUB_DIV,
           MAX(SSN)     AS SSN
    FROM vw_master_product WITH (NOLOCK)
    GROUP BY MAJ_CAT
)
SELECT
    -- store details (selected columns)
    ISNULL(s.ST_NM, '''')       AS STORE_NAME,
    ISNULL(s.RDC, '''')         AS RDC,
    ISNULL(s.HUB, '''')         AS HUB,
    ISNULL(s.ST_STATUS, '''')   AS ST_STATUS,
    -- product attributes (MAJ_CAT grain, from vw_master_product)
    ISNULL(p.SEG, '''')         AS SEG,
    ISNULL(p.DIV, '''')         AS DIV,
    ISNULL(p.SUB_DIV, '''')     AS SUB_DIV,
    ISNULL(p.SSN, '''')         AS SSN,
    -- grid table : ALL columns (type-aware ISNULL)
    ' + @gSel + N',
    -- listing working result (store grain, selected columns)
    ISNULL(lst.ALC_Q, 0)            AS ALC_Q,
    ISNULL(lst.HOLD_ALC_Q, 0)       AS HOLD_ALC_Q,
    ISNULL(lst.ALC_FROM_HOLD_Q, 0)  AS ALC_FROM_HOLD_Q,
    ISNULL(lst.ART_EXCESS_QTY, 0)   AS ART_EXCESS_QTY,
    -- RDC-level MSA vs ALLOC (repeated per store/dim row in the RDC)
    ISNULL(msa.OP_MSA_QTY, 0)                         AS MSA_OP_Q,
    ISNULL(msa.OP_MSA_QTY, 0) - ISNULL(alloc.consumed, 0) AS MSA_CL_Q,
    ISNULL(opt_gt50.OPT_GT50_CNT, 0)                 AS [OPT_CNT_>50_PCS],
    -- HOLD movement (store, split by grid dim on secondary grids)
    ISNULL(hold.CLOSE_REM, 0)
      - ISNULL(hmov.HOLD_QTY, 0)
      + ISNULL(hmov.from_hold, 0)                     AS HOLD_OPEN_REM,
    ISNULL(hmov.from_hold, 0)                         AS HOLD_CONSUMED_TODAY,
    ISNULL(hmov.HOLD_QTY, 0)                          AS HOLD_ADDED_TODAY,
    ISNULL(hold.CLOSE_REM, 0)                         AS HOLD_CLOSE_REM
FROM ' + QUOTENAME(@GridTable) + N' g WITH (NOLOCK)
LEFT JOIN MASTER_ALC_INPUT_ST_MASTER s WITH (NOLOCK)
       ON s.ST_CD = g.WERKS
LEFT JOIN lst      ON lst.WERKS    = g.WERKS AND lst.MAJ_CAT     = g.MAJ_CAT
LEFT JOIN msa      ON msa.RDC      = s.RDC   AND msa.MAJ_CAT     = g.MAJ_CAT
LEFT JOIN alloc    ON alloc.RDC    = s.RDC   AND alloc.MAJ_CAT   = g.MAJ_CAT
LEFT JOIN opt_gt50 ON opt_gt50.RDC = s.RDC   AND opt_gt50.MAJ_CAT = g.MAJ_CAT
' + @hmovJoin + N'
' + @holdJoin + N'
LEFT JOIN prod   p ON p.MAJ_CAT    = g.MAJ_CAT' + @where + N'
ORDER BY s.RDC, s.HUB, g.WERKS, g.MAJ_CAT';

    EXEC sp_executesql @sql,
         N'@pW NVARCHAR(50), @pMC NVARCHAR(100)',
         @pW = @WERKS, @pMC = @MAJ_CAT;
END
GO
