/* ============================================================================
   usp_ars_msa_master — MSA master (opening vs closing) reconciliation,
   rolled up to any @Level, optionally as an ORDERED SEQUENCE OF STEPS.

   Detail grain (finest) = (RDC, SEG, DIV, SUB_DIV, MAJ_CAT, GEN_ART_NUMBER, CLR,
     ARTICLE_NUMBER, PAK_SZ, SZ, FAB-MVGR-1, FAB-MVGR-2, RNG_SEG, ALLOC_TYPE).
   All measures are additive, so higher levels just SUM the detail rows.

   Params
     @Level : report grain. Levels (see dbo.vw_ars_msa_master_levels, or run
              @Level='LIST'):  DETAIL > ARTICLE > GEN_CLR > MAJ_CAT > RDC.
              @Level='LIST' (or '?' / 'HELP') returns the level catalogue.
              *** SEQUENCE / STEPS ***  @Level may also be a COMMA LIST that runs
              each level IN ORDER, returned as ONE combined grid (summary->detail):
                  @Level = N'RDC,MAJ_CAT,GEN_CLR,DETAIL'
              Every row carries leading STEP (1..n) + LEVEL columns; a column that
              a level does not use is NULL on that step's rows. Rows are ordered
              by STEP then by key, so the steps read top-to-bottom in order.
     @SESSION_ID : opening session (default = latest ARS_LISTING_HISTORY)
     @RDC, @MAJ_CAT : optional filters

   Zero rows: line items whose every quantity column sums to 0 are dropped.

   Sources / column map:
     opening  = ARS_MSA_TOTAL_HISTORY (session)   closing = ARS_MSA_TOTAL (live)
     movement = ARS_ALLOC_HISTORY (session, summed over RDC stores)
     FAB-MVGR = vw_master_product (M_YARN_02, WEAVE_2 per ARTICLE_NUMBER)
     V02 BEFORE ALC = V02_FRESH (FRESH rows) / V02_GRT (GRT rows)
     PEND INT / HOLD INT / OP MSA-Q USE IN ALC = opening PEND_QTY / HOLD_QTY / FNL_Q
     ALC-Q / TD-ALC FROM HOLD_Q / TD-HOLD ALC-Q = alloc SUM(ALLOC_QTY/FROM_HOLD_QTY/HOLD_QTY)
     PEND AFT ALC / HOLD AFT ALC / REM MSA-Q = closing PEND_QTY / HOLD_QTY / FNL_Q

   Examples
     EXEC dbo.usp_ars_msa_master @Level=N'LIST';
     EXEC dbo.usp_ars_msa_master @Level=N'GEN_CLR', @RDC=N'DW01', @MAJ_CAT=N'M_JEANS';
     EXEC dbo.usp_ars_msa_master @Level=N'RDC,MAJ_CAT,GEN_CLR,DETAIL', @RDC=N'DW01';
   ============================================================================ */

-- ---------------------------------------------------------------------------
-- Level catalogue — single source of truth for @Level.  Drives both the proc
-- (KEY_COLS) and the discoverable list (@Level='LIST').  Add a level = add a row.
-- ---------------------------------------------------------------------------
CREATE OR ALTER VIEW dbo.vw_ars_msa_master_levels AS
SELECT SORT_ORDER, LEVEL_CODE, LEVEL_NAME, GRAIN, [DESCRIPTION], KEY_COLS
FROM (VALUES
 (1, N'DETAIL',  N'Article + Size',
     N'RDC · SEG · DIV · SUB_DIV · MAJ_CAT · GEN_ART · CLR · ARTICLE_NUMBER · PAK_SZ · SZ · RNG_SEG · ALLOC_TYPE',
     N'Finest grain — one row per variant article, size and pool (FRESH/GRT). Full attributes incl. FAB-MVGR.',
     N'RDC, SEG, DIV, SUB_DIV, MAJ_CAT, GEN_ART_NUMBER, CLR, ARTICLE_NUMBER, PAK_SZ, SZ, FAB_MVGR_1, FAB_MVGR_2, RNG_SEG, ALLOC_TYPE'),
 (2, N'ARTICLE', N'Article (sizes merged)',
     N'RDC · SEG · DIV · SUB_DIV · MAJ_CAT · GEN_ART · CLR · ARTICLE_NUMBER · PAK_SZ · RNG_SEG · ALLOC_TYPE',
     N'One row per variant article and pool; sizes collapsed. Keeps article attributes (FAB-MVGR, RNG_SEG).',
     N'RDC, SEG, DIV, SUB_DIV, MAJ_CAT, GEN_ART_NUMBER, CLR, ARTICLE_NUMBER, PAK_SZ, FAB_MVGR_1, FAB_MVGR_2, RNG_SEG, ALLOC_TYPE'),
 (3, N'GEN_CLR', N'Gen-Article + Colour',
     N'RDC · SEG · DIV · SUB_DIV · MAJ_CAT · GEN_ART · CLR',
     N'One row per RDC + category + generic article + colour (the OPT grain). FRESH/GRT pools combined.',
     N'RDC, SEG, DIV, SUB_DIV, MAJ_CAT, GEN_ART_NUMBER, CLR'),
 (4, N'MAJ_CAT', N'Major Category',
     N'RDC · SEG · DIV · SUB_DIV · MAJ_CAT',
     N'One row per RDC + major category. Category-level opening vs closing summary.',
     N'RDC, SEG, DIV, SUB_DIV, MAJ_CAT'),
 (5, N'RDC',     N'RDC (grand total)',
     N'RDC',
     N'One row per RDC — highest-level rollup.',
     N'RDC')
) v(SORT_ORDER, LEVEL_CODE, LEVEL_NAME, GRAIN, [DESCRIPTION], KEY_COLS);
GO

CREATE OR ALTER PROCEDURE dbo.usp_ars_msa_master
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
                    + @code + N''' AS [LEVEL], ' + @proj + N', ' + @measSel
                    + N' FROM base GROUP BY ' + @keyCols + N' HAVING ' + @having;

        SET @unionAll = @unionAll
                      + CASE WHEN LEN(@unionAll) = 0 THEN N'' ELSE N'
UNION ALL
' END + @branch;

        SET @seq += 1;
    END

    ------------------------------------------------------------------ assemble + run once
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
alloc AS (                           -- movement this run, summed over RDC stores
    SELECT RDC, VAR_ART, SZ, ALLOC_TYPE,
           SUM(ISNULL(ALLOC_QTY,0)) AS ALC_Q, SUM(ISNULL(FROM_HOLD_QTY,0)) AS TD_ALC_FROM_HOLD_Q, SUM(ISNULL(HOLD_QTY,0)) AS TD_HOLD_ALC_Q
    FROM ARS_ALLOC_HISTORY WITH (NOLOCK)
    WHERE SESSION_ID=@pSid AND (@pRDC IS NULL OR RDC=@pRDC) AND (@pMC IS NULL OR MAJ_CAT=@pMC)
    GROUP BY RDC, VAR_ART, SZ, ALLOC_TYPE
),
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
           ISNULL(alloc.ALC_Q,0) AS ALC_Q, ISNULL(alloc.TD_ALC_FROM_HOLD_Q,0) AS TD_ALC_FROM_HOLD_Q, ISNULL(alloc.TD_HOLD_ALC_Q,0) AS TD_HOLD_ALC_Q,
           ISNULL(cl.PEND_AFT_ALC,0) AS PEND_AFT_ALC, ISNULL(cl.HOLD_AFT_ALC,0) AS HOLD_AFT_ALC, ISNULL(cl.REM_MSA_Q,0) AS REM_MSA_Q
    FROM op
    LEFT JOIN pmap  ON pmap.ARTICLE_NUMBER = op.ARTICLE_NUMBER
    LEFT JOIN alloc ON alloc.RDC=op.RDC AND alloc.VAR_ART=op.ARTICLE_NUMBER AND alloc.SZ=op.SZ AND alloc.ALLOC_TYPE=op.ALLOC_TYPE
    LEFT JOIN cl    ON cl.RDC=op.RDC AND cl.ARTICLE_NUMBER=op.ARTICLE_NUMBER AND cl.SZ=op.SZ AND cl.ALLOC_TYPE=op.ALLOC_TYPE
)
' + @unionAll + N'
ORDER BY 1, 3, 7, 8, 9, 10, 12, 16   -- STEP, RDC, MAJ_CAT, GEN_ART, CLR, ARTICLE, SZ, ALLOC_TYPE
OPTION (RECOMPILE);';                 -- RECOMPILE: simplifies (@p IS NULL OR col=@p) so filters seek, not scan

    EXEC sp_executesql @sql,
         N'@pSid NVARCHAR(50), @pRDC NVARCHAR(50), @pMC NVARCHAR(100)',
         @pSid = @SESSION_ID, @pRDC = @RDC, @pMC = @MAJ_CAT;
END
GO
