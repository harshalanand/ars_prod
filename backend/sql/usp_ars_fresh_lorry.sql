/* ============================================================================
   usp_ars_fresh_lorry — FRESH LORRY allocation report (stored proc).

   Allocation lines from ARS_ALLOC_HISTORY for the LAST RUN (latest SESSION_ID
   in ARS_ALLOC_HISTORY). One row per store × variant-article × size (alloc
   waves/rounds summed). Only lines with activity (ALLOC_QTY or HOLD_QTY <> 0).

   Params (all optional, NULL = no filter):
     @RDC, @WERKS, @MAJ_CAT

   Sources:
     ARS_ALLOC_HISTORY          — RDC, WERKS(→ST_CD), VAR_ART, GEN_ART_NUMBER,
                                  MAJ_CAT, SZ, CLR, MERGE_RNG_SEG, RNG_SEG, FIT,
                                  M_YARN_02, WEAVE_2, FAB, M_VND_CD, ALLOC_QTY, HOLD_QTY
     MASTER_ALC_INPUT_ST_MASTER — ST_NM, ST_STATUS(→OLD/NEW-ST) (join ST_CD = WERKS)
     vw_master_product          — SEG, DIV, SUB_DIV, SSN (per variant article)

   Examples:
     EXEC dbo.usp_ars_fresh_lorry;
     EXEC dbo.usp_ars_fresh_lorry @WERKS = N'HK15', @MAJ_CAT = N'L_H_CROP_TOP_FS';
   ============================================================================ */
CREATE OR ALTER PROCEDURE dbo.usp_ars_fresh_lorry
    @RDC     NVARCHAR(50)  = NULL,
    @WERKS   NVARCHAR(50)  = NULL,
    @MAJ_CAT NVARCHAR(100) = NULL
AS
BEGIN
    SET NOCOUNT ON;

    SELECT
        a.RDC,
        a.WERKS                    AS ST_CD,
        s.ST_NM,
        a.VAR_ART                  AS VAR_ARTICLE_NUMBER,
        a.GEN_ART_NUMBER,
        pm.SEG,
        pm.DIV,
        pm.SUB_DIV                 AS [SUB-DIV],
        a.MAJ_CAT                  AS [MAJ-CAT],
        pm.SSN,
        a.SZ,
        a.CLR,
        s.ST_STATUS                AS [OLD/NEW-ST],
        a.MERGE_RNG_SEG,
        a.RNG_SEG,
        a.FIT,
        a.M_YARN_02,
        a.WEAVE_2,
        a.FAB,
        a.M_VND_CD,
        SUM(ISNULL(a.ALLOC_QTY, 0)) AS ALLOC_QTY,
        SUM(ISNULL(a.HOLD_QTY,  0)) AS HOLD_QTY
    FROM ARS_ALLOC_HISTORY a WITH (NOLOCK)
    LEFT JOIN MASTER_ALC_INPUT_ST_MASTER s WITH (NOLOCK)
           ON s.ST_CD = a.WERKS
    LEFT JOIN (                                    -- SEG/DIV/SUB_DIV/SSN per variant article
        SELECT CAST(ARTICLE_NUMBER AS NVARCHAR(50)) AS ARTICLE_NUMBER,
               MAX(SEG) AS SEG, MAX(DIV) AS DIV, MAX(SUB_DIV) AS SUB_DIV, MAX(SSN) AS SSN
        FROM vw_master_product WITH (NOLOCK)
        GROUP BY CAST(ARTICLE_NUMBER AS NVARCHAR(50))
    ) pm ON pm.ARTICLE_NUMBER = a.VAR_ART
    WHERE a.SESSION_ID = (SELECT MAX(SESSION_ID) FROM ARS_ALLOC_HISTORY WITH (NOLOCK))
      AND (@RDC     IS NULL OR a.RDC     = @RDC)
      AND (@WERKS   IS NULL OR a.WERKS   = @WERKS)
      AND (@MAJ_CAT IS NULL OR a.MAJ_CAT = @MAJ_CAT)
    GROUP BY
        a.RDC, a.WERKS, s.ST_NM, a.VAR_ART, a.GEN_ART_NUMBER,
        pm.SEG, pm.DIV, pm.SUB_DIV, a.MAJ_CAT, pm.SSN, a.SZ, a.CLR,
        s.ST_STATUS, a.MERGE_RNG_SEG, a.RNG_SEG, a.FIT,
        a.M_YARN_02, a.WEAVE_2, a.FAB, a.M_VND_CD
    HAVING SUM(ISNULL(a.ALLOC_QTY, 0)) <> 0 OR SUM(ISNULL(a.HOLD_QTY, 0)) <> 0
    ORDER BY a.RDC, a.WERKS, a.MAJ_CAT, a.GEN_ART_NUMBER, a.CLR, a.SZ
    OPTION (RECOMPILE);   -- simplifies (@p IS NULL OR col=@p) filters so they seek
END
GO
