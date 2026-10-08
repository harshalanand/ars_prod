
CREATE OR ALTER PROCEDURE dbo.ALLRDC_usp_ars_msa_master
    @Level      NVARCHAR(200) = N'DETAIL',
    @SESSION_ID NVARCHAR(50)  = NULL,
    @RDC        NVARCHAR(50)  = NULL,
    @MAJ_CAT    NVARCHAR(100) = NULL
AS
BEGIN
    SET NOCOUNT ON;

    IF @SESSION_ID IS NULL
        SELECT @SESSION_ID = MAX(SESSION_ID) FROM ARS_LISTING_HISTORY WITH (NOLOCK);

    SET @Level = UPPER(LTRIM(RTRIM(@Level)));

    ------------------------------------------------------------------ discover levels
    IF @Level IN (N'LIST', N'?', N'HELP')
    BEGIN
        SELECT SORT_ORDER, LEVEL_CODE, LEVEL_NAME, GRAIN, [DESCRIPTION]
        FROM dbo.vw_ars_msa_master_levels
        ORDER BY SORT_ORDER;
        RETURN;
    END

    ------------------------------------------------------------------ parse ordered steps
    -- @Level = single code OR a comma list run in order (OPENJSON keeps position).
    DECLARE @steps TABLE (seq INT PRIMARY KEY, code NVARCHAR(20));
    INSERT @steps(seq, code)
    SELECT CONVERT(INT,[key]) + 1, UPPER(LTRIM(RTRIM([value])))
    FROM OPENJSON(N'["' + REPLACE(@Level, N',', N'","') + N'"]');

    IF EXISTS (SELECT 1 FROM @steps s
               WHERE NOT EXISTS (SELECT 1 FROM dbo.vw_ars_msa_master_levels v WHERE v.LEVEL_CODE = s.code))
    BEGIN
        DECLARE @bad NVARCHAR(400), @valid NVARCHAR(400);
        SELECT @bad = STRING_AGG(code, N', ') FROM @steps s
               WHERE NOT EXISTS (SELECT 1 FROM dbo.vw_ars_msa_master_levels v WHERE v.LEVEL_CODE = s.code);
        SELECT @valid = STRING_AGG(LEVEL_CODE, N', ') WITHIN GROUP (ORDER BY SORT_ORDER)
        FROM dbo.vw_ars_msa_master_levels;
        RAISERROR('Unknown level(s): %s. Valid: %s.  (EXEC dbo.usp_ars_msa_master @Level=''LIST'' to see all.)',
                  16, 1, @bad, @valid);
        RETURN;
    END

    ------------------------------------------------------------------ superset of key columns (fixed order)
    DECLARE @super TABLE (ord INT, col SYSNAME, alias NVARCHAR(50));
    INSERT @super(ord, col, alias) VALUES
     (1,N'RDC',N'RDC'),(2,N'SEG',N'SEG'),(3,N'DIV',N'DIV'),(4,N'SUB_DIV',N'SUB DIV'),(5,N'MAJ_CAT',N'MAJ CAT'),
     (6,N'GEN_ART_NUMBER',N'GEN_ART_NUMBER'),(7,N'CLR',N'CLR'),(8,N'ARTICLE_NUMBER',N'ARTICLE_NUMBER'),
     (9,N'PAK_SZ',N'PAK_SZ'),(10,N'SZ',N'SZ'),(11,N'FAB_MVGR_1',N'FAB-MVGR-1'),(12,N'FAB_MVGR_2',N'FAB-MVGR-2'),
     (13,N'RNG_SEG',N'RNG_SEG'),(14,N'ALLOC_TYPE',N'ALLOC_TYPE');

    -- Session tracking columns (constant across the report): the session used
    -- and its approved timestamp (NULL until the session is approved).
    DECLARE @apprAt DATETIME = (SELECT MAX(APPROVED_AT) FROM ARS_MSA_TOTAL_HISTORY WITH (NOLOCK)
                                WHERE SESSION_ID = @SESSION_ID);

    DECLARE @measSel NVARCHAR(MAX) = N'SUM(V02_BEFORE_ALC) AS [V02 BEFORE ALC], SUM(PEND_INT) AS [PEND INT], SUM(HOLD_INT) AS [HOLD INT], SUM(OP_MSA_Q) AS [OP MSA-Q USE IN ALC], SUM(ALC_Q) AS [ALC-Q], SUM(TD_ALC_FROM_HOLD_Q) AS [TD-ALC FROM HOLD_Q], SUM(TD_HOLD_ALC_Q) AS [TD-HOLD ALC-Q], SUM(PEND_AFT_ALC) AS [PEND AFT ALC], SUM(HOLD_AFT_ALC) AS [HOLD AFT ALC], SUM(REM_MSA_Q) AS [REM MSA-Q]';
    DECLARE @having  NVARCHAR(MAX) = N'SUM(V02_BEFORE_ALC)<>0 OR SUM(PEND_INT)<>0 OR SUM(HOLD_INT)<>0 OR SUM(OP_MSA_Q)<>0 OR SUM(ALC_Q)<>0 OR SUM(TD_ALC_FROM_HOLD_Q)<>0 OR SUM(TD_HOLD_ALC_Q)<>0 OR SUM(PEND_AFT_ALC)<>0 OR SUM(HOLD_AFT_ALC)<>0 OR SUM(REM_MSA_Q)<>0';

    ------------------------------------------------------------------ build one UNION-ALL branch per step
    DECLARE @seq INT = 1, @maxseq INT = (SELECT MAX(seq) FROM @steps);
    DECLARE @code NVARCHAR(20), @keyCols NVARCHAR(MAX), @proj NVARCHAR(MAX),
            @branch NVARCHAR(MAX), @unionAll NVARCHAR(MAX) = N'';

    WHILE @seq <= @maxseq
    BEGIN
        SELECT @code = code FROM @steps WHERE seq = @seq;
        SELECT @keyCols = KEY_COLS FROM dbo.vw_ars_msa_master_levels WHERE LEVEL_CODE = @code;

        -- project the full superset: real column if the level uses it, else NULL (all CAST NVARCHAR for UNION)
        SELECT @proj = STRING_AGG(
                 CASE WHEN CHARINDEX(N',' + col + N',', N',' + REPLACE(@keyCols,N' ',N'') + N',') > 0
                      THEN N'CAST(' + col + N' AS NVARCHAR(100))'
                      ELSE N'CAST(NULL AS NVARCHAR(100))' END
                 + N' AS ' + QUOTENAME(alias), N', ')
               WITHIN GROUP (ORDER BY ord)
        FROM @super;

        SET @branch = N'SELECT ' + CAST(@seq AS NVARCHAR(10)) + N' AS [STEP], '''
                    + @code + N''' AS [LEVEL], @pSid AS [SESSION_ID], @pApprAt AS [APPROVED_AT], '
                    + @proj + N', ' + @measSel
                    + N' FROM base GROUP BY ' + @keyCols + N' HAVING ' + @having;

        SET @unionAll = @unionAll
                      + CASE WHEN LEN(@unionAll) = 0 THEN N'' ELSE N'
UNION ALL
' END + @branch;

        SET @seq += 1;
    END

    ------------------------------------------------------------------ assemble + run once
    /* ---- allocation movement, attributed to the SOURCING RDC ----------------
       ALC_Q / TD_HOLD_ALC_Q come from ARS_ALLOC_RDC_SPLIT_HISTORY keyed on
       SRC_RDC (the RDC the stock actually shipped FROM) rather than the store's
       connected RDC. Cross-RDC supply is routine in ARS, so store-RDC
       attribution depletes the wrong pool AND loses every allocation whose
       store RDC has no matching MSA grain.

       ARS_ALLOC_RDC_SPLIT_HISTORY is written by rdc_split_service.py on the
       feat/central-rdc-pool branch, which is NOT merged into ars_v2. It exists
       on PROD but not everywhere, so fall back to ARS_ALLOC_HISTORY when it is
       absent -- otherwise this proc dies with "Invalid object name".

       TD_ALC_FROM_HOLD_Q always comes from ARS_ALLOC_HISTORY: the split table
       carries SHIP_QTY and HOLD_QTY but NOT FROM_HOLD_QTY. When that column is
       added, fold alloc_fh into alloc and drop it.                           */
    DECLARE @allocCTE NVARCHAR(MAX) =
      CASE WHEN OBJECT_ID('dbo.ARS_ALLOC_RDC_SPLIT_HISTORY') IS NOT NULL THEN
        N'alloc AS (                       -- SOURCING RDC (split table)
    SELECT SRC_RDC AS RDC, VAR_ART, SZ, ALLOC_TYPE,
           SUM(ISNULL(SHIP_QTY,0)) AS ALC_Q, SUM(ISNULL(HOLD_QTY,0)) AS TD_HOLD_ALC_Q
    FROM ARS_ALLOC_RDC_SPLIT_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID=@pSid AND (@pRDC IS NULL OR SRC_RDC=@pRDC) AND (@pMC IS NULL OR MAJ_CAT=@pMC)
    GROUP BY SRC_RDC, VAR_ART, SZ, ALLOC_TYPE
)'
      ELSE
        N'alloc AS (                       -- FALLBACK: no split table, store-RDC attribution
    SELECT RDC, VAR_ART, SZ, ALLOC_TYPE,
           SUM(ISNULL(ALLOC_QTY,0)) AS ALC_Q, SUM(ISNULL(HOLD_QTY,0)) AS TD_HOLD_ALC_Q
    FROM ARS_ALLOC_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID=@pSid AND (@pRDC IS NULL OR RDC=@pRDC) AND (@pMC IS NULL OR MAJ_CAT=@pMC)
    GROUP BY RDC, VAR_ART, SZ, ALLOC_TYPE
)' END
      + N',
alloc_fh AS (                        -- TD-ALC FROM HOLD_Q (no SRC_RDC available)
    SELECT RDC, VAR_ART, SZ, ALLOC_TYPE,
           SUM(ISNULL(FROM_HOLD_QTY,0)) AS TD_ALC_FROM_HOLD_Q
    FROM ARS_ALLOC_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID=@pSid AND (@pRDC IS NULL OR RDC=@pRDC) AND (@pMC IS NULL OR MAJ_CAT=@pMC)
    GROUP BY RDC, VAR_ART, SZ, ALLOC_TYPE
)';

    DECLARE @sql NVARCHAR(MAX) = N'
;WITH pmap AS (                      -- article -> FAB-MVGR attrs (CAST bigint->nvarchar so join stays a hash join)
    SELECT CAST(ARTICLE_NUMBER AS NVARCHAR(50)) AS ARTICLE_NUMBER,
           MAX(M_YARN_02) AS M_YARN_02, MAX(WEAVE_2) AS WEAVE_2
    FROM vw_master_product WITH (NOLOCK)
    GROUP BY CAST(ARTICLE_NUMBER AS NVARCHAR(50))
),
op AS (                              -- OPENING snapshot (session)
    SELECT RDC, SEG, DIV, SUB_DIV, MAJ_CAT, GEN_ART_NUMBER, CLR, ARTICLE_NUMBER, PAK_SZ, SZ, RNG_SEG, ALLOC_TYPE,
           SUM(ISNULL(V02_FRESH,0)) AS V02_FRESH, SUM(ISNULL(V02_GRT,0)) AS V02_GRT,
           SUM(ISNULL(PEND_QTY,0)) AS PEND_INT, SUM(ISNULL(HOLD_QTY,0)) AS HOLD_INT, SUM(ISNULL(FNL_Q,0)) AS OP_MSA_Q
    FROM ARS_MSA_TOTAL_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID=@pSid AND (@pRDC IS NULL OR RDC=@pRDC) AND (@pMC IS NULL OR MAJ_CAT=@pMC)
    GROUP BY RDC, SEG, DIV, SUB_DIV, MAJ_CAT, GEN_ART_NUMBER, CLR, ARTICLE_NUMBER, PAK_SZ, SZ, RNG_SEG, ALLOC_TYPE
),
' + @allocCTE + N',
cl AS (                              -- CLOSING / live MSA
    SELECT RDC, ARTICLE_NUMBER, SZ, ALLOC_TYPE,
           SUM(ISNULL(PEND_QTY,0)) AS PEND_AFT_ALC, SUM(ISNULL(HOLD_QTY,0)) AS HOLD_AFT_ALC, SUM(ISNULL(FNL_Q,0)) AS REM_MSA_Q
    FROM ARS_MSA_TOTAL WITH (NOLOCK)
    WHERE (@pRDC IS NULL OR RDC=@pRDC) AND (@pMC IS NULL OR MAJ_CAT=@pMC)
    GROUP BY RDC, ARTICLE_NUMBER, SZ, ALLOC_TYPE
),
base AS (                            -- detail grain, all measures resolved
    SELECT op.RDC, op.SEG, op.DIV, op.SUB_DIV, op.MAJ_CAT, op.GEN_ART_NUMBER, op.CLR, op.ARTICLE_NUMBER, op.PAK_SZ, op.SZ,
           pmap.M_YARN_02 AS FAB_MVGR_1, pmap.WEAVE_2 AS FAB_MVGR_2, op.RNG_SEG, op.ALLOC_TYPE,
           CASE WHEN op.ALLOC_TYPE=''GRT'' THEN op.V02_GRT ELSE op.V02_FRESH END AS V02_BEFORE_ALC,
           op.PEND_INT, op.HOLD_INT, op.OP_MSA_Q,
           ISNULL(alloc.ALC_Q,0) AS ALC_Q, ISNULL(alloc_fh.TD_ALC_FROM_HOLD_Q,0) AS TD_ALC_FROM_HOLD_Q, ISNULL(alloc.TD_HOLD_ALC_Q,0) AS TD_HOLD_ALC_Q,
           ISNULL(cl.PEND_AFT_ALC,0) AS PEND_AFT_ALC, ISNULL(cl.HOLD_AFT_ALC,0) AS HOLD_AFT_ALC, ISNULL(cl.REM_MSA_Q,0) AS REM_MSA_Q
    FROM op
    LEFT JOIN pmap  ON pmap.ARTICLE_NUMBER = op.ARTICLE_NUMBER
    LEFT JOIN alloc ON alloc.RDC=op.RDC AND alloc.VAR_ART=op.ARTICLE_NUMBER AND alloc.SZ=op.SZ AND alloc.ALLOC_TYPE=op.ALLOC_TYPE
    LEFT JOIN alloc_fh ON alloc_fh.RDC=op.RDC AND alloc_fh.VAR_ART=op.ARTICLE_NUMBER AND alloc_fh.SZ=op.SZ AND alloc_fh.ALLOC_TYPE=op.ALLOC_TYPE
    LEFT JOIN cl    ON cl.RDC=op.RDC AND cl.ARTICLE_NUMBER=op.ARTICLE_NUMBER AND cl.SZ=op.SZ AND cl.ALLOC_TYPE=op.ALLOC_TYPE
)
' + @unionAll + N'
ORDER BY 1, 5, 9, 10, 11, 12, 14, 18   -- STEP, RDC, MAJ_CAT, GEN_ART, CLR, ARTICLE, SZ, ALLOC_TYPE
                                       -- (+2 vs before: SESSION_ID/APPROVED_AT inserted at cols 3-4)
OPTION (RECOMPILE);';                 -- RECOMPILE: simplifies (@p IS NULL OR col=@p) so filters seek, not scan

    EXEC sp_executesql @sql,
         N'@pSid NVARCHAR(50), @pRDC NVARCHAR(50), @pMC NVARCHAR(100), @pApprAt DATETIME',
         @pSid = @SESSION_ID, @pRDC = @RDC, @pMC = @MAJ_CAT, @pApprAt = @apprAt;
END
GO
