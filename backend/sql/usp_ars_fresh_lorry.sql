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
                                  M_YARN_02, WEAVE_2, FAB, M_VND_CD, ALLOC_QTY,
                                  HOLD_QTY, OPT_TYPE, CONT
     MASTER_ALC_INPUT_ST_MASTER — ST_NM, ST_STATUS(→OLD/NEW-ST) (join ST_CD = WERKS)
     vw_master_product          — SEG, DIV, SUB_DIV, SSN (per variant article)

   Dispatch verdict (added 2026-10-05):
     CONT_SUM     = SUM over the option (WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR)
                    of the per-size CONT (size contribution %, 0..1).
     HOLD_RELEASE = 'HOLD' when OPT_TYPE = 'TBL' AND CONT_SUM < 0.3
                    (the size set on the lorry covers under 30% of the size
                    curve — a stub, so hold it), otherwise 'RELEASE'.

   OPT_TYPE and CONT are read from ARS_ALLOC_HISTORY **for the reported session**,
   NOT from ARS_LISTING_WORKING / ARS_ALLOC_WORKING. Those working tables are
   overwritten by every allocation run, so they describe whatever ran LAST, not
   the session being reported. Verified 2026-10-05: on PROD they happened to
   agree (100% option overlap, and listing OPT_TYPE matched the history column on
   all 23,291 options); on DEV they did NOT (1.6% overlap → 181,300 of 182,796
   rows came back with NULL OPT_TYPE). Sourcing from history is session-correct
   on any server and for any archived run.

   ⚠ CONT is a per-SIZE attribute REPEATED on every alloc wave row (verified:
     447 size-grains carry 2 rows with identical CONT). It must be de-duplicated
     with MAX per size BEFORE summing across sizes — a raw SUM double-counts and
     reaches 2.0 where the ceiling is 1.0, wrongly flipping HOLD rows to RELEASE.

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

    -- The run being reported. Resolved once so every CTE scopes to it.
    DECLARE @sid NVARCHAR(50) =
        (SELECT MAX(SESSION_ID) FROM ARS_ALLOC_HISTORY WITH (NOLOCK));

    ;WITH cont_sz AS (     -- CONT per SIZE (MAX, not SUM — repeated per alloc wave)
        SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR, SZ,
               MAX(CAST(ISNULL(CONT, 0) AS FLOAT)) AS CONT_SZ
        FROM ARS_ALLOC_HISTORY WITH (NOLOCK)
        WHERE SESSION_ID = @sid
          AND (@WERKS   IS NULL OR WERKS   = @WERKS)
          AND (@MAJ_CAT IS NULL OR MAJ_CAT = @MAJ_CAT)
        GROUP BY WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR, SZ
    ),
    cont_opt AS (          -- CONT_SUM per OPTION = sum of de-duplicated per-size CONT
        SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR,
               SUM(CONT_SZ) AS CONT_SUM
        FROM cont_sz
        GROUP BY WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR
    )
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
        a.OPT_TYPE,
        SUM(ISNULL(a.ALLOC_QTY, 0)) AS ALLOC_QTY,
        SUM(ISNULL(a.HOLD_QTY,  0)) AS HOLD_QTY,
        cs.CONT_SZ                 AS CONT,
        co.CONT_SUM,
        CASE WHEN a.OPT_TYPE = 'TBL' AND ISNULL(co.CONT_SUM, 0) < 0.3
             THEN 'HOLD' ELSE 'RELEASE' END AS HOLD_RELEASE
    FROM ARS_ALLOC_HISTORY a WITH (NOLOCK)
    LEFT JOIN MASTER_ALC_INPUT_ST_MASTER s WITH (NOLOCK)
           ON s.ST_CD = a.WERKS
    LEFT JOIN (                                    -- SEG/DIV/SUB_DIV/SSN per variant article
        SELECT CAST(ARTICLE_NUMBER AS NVARCHAR(50)) AS ARTICLE_NUMBER,
               MAX(SEG) AS SEG, MAX(DIV) AS DIV, MAX(SUB_DIV) AS SUB_DIV, MAX(SSN) AS SSN
        FROM vw_master_product WITH (NOLOCK)
        GROUP BY CAST(ARTICLE_NUMBER AS NVARCHAR(50))
    ) pm ON pm.ARTICLE_NUMBER = a.VAR_ART
    LEFT JOIN cont_sz cs                           -- CONT (size grain)
           ON cs.WERKS          = a.WERKS
          AND cs.MAJ_CAT        = a.MAJ_CAT
          AND cs.GEN_ART_NUMBER = a.GEN_ART_NUMBER
          AND cs.CLR            = a.CLR
          AND cs.SZ             = a.SZ
    LEFT JOIN cont_opt co                          -- CONT_SUM (option grain)
           ON co.WERKS          = a.WERKS
          AND co.MAJ_CAT        = a.MAJ_CAT
          AND co.GEN_ART_NUMBER = a.GEN_ART_NUMBER
          AND co.CLR            = a.CLR
    WHERE a.SESSION_ID = @sid
      AND (@RDC     IS NULL OR a.RDC     = @RDC)
      AND (@WERKS   IS NULL OR a.WERKS   = @WERKS)
      AND (@MAJ_CAT IS NULL OR a.MAJ_CAT = @MAJ_CAT)
    GROUP BY
        a.RDC, a.WERKS, s.ST_NM, a.VAR_ART, a.GEN_ART_NUMBER,
        pm.SEG, pm.DIV, pm.SUB_DIV, a.MAJ_CAT, pm.SSN, a.SZ, a.CLR,
        s.ST_STATUS, a.MERGE_RNG_SEG, a.RNG_SEG, a.FIT,
        a.M_YARN_02, a.WEAVE_2, a.FAB, a.M_VND_CD,
        a.OPT_TYPE, cs.CONT_SZ, co.CONT_SUM
    HAVING SUM(ISNULL(a.ALLOC_QTY, 0)) <> 0 OR SUM(ISNULL(a.HOLD_QTY, 0)) <> 0
    ORDER BY a.RDC, a.WERKS, a.MAJ_CAT, a.GEN_ART_NUMBER, a.CLR, a.SZ
    OPTION (RECOMPILE);   -- simplifies (@p IS NULL OR col=@p) filters so they seek
END
GO
