/* ============================================================================
   usp_ars_grid_var_art - variant-article grid report (ARS_GRID_MJ_VAR_ART)
   enriched with product hierarchy (SEG / DIV / SUB_DIV / SSN / RNG_SEG +
   descriptions) from vw_master_product, mapped per ARTICLE_NUMBER.

   @WERKS and @MAJ_CAT accept a SINGLE value OR a COMMA LIST (e.g.
   'M_JEANS,L_JEANS') for a multi-select report. Blank/NULL = all.

   The measure columns are DISCOVERED AT RUNTIME from INFORMATION_SCHEMA, so one
   definition serves every environment: DEV and PROD carry different column sets
   (prod has the DH24_PTL_* family, dev has DW01_PTL_V06_Q) and the grid gains
   columns over time. Numeric measures get ISNULL(...,0); text passes through.
   Identity columns are fixed and ordered first.

   Perf: list filters via STRING_SPLIT applied once in gsel; the product map is
   restricted by the slice's ARTICLE_NUMBERs;
   ARTICLE_NUMBER bigint both sides (clean join); NOLOCK + OPTION(RECOMPILE).
   ============================================================================ */
CREATE OR ALTER PROCEDURE dbo.usp_ars_grid_var_art
    @WERKS   NVARCHAR(MAX) = NULL,   -- one code or comma list (e.g. 'HB05,HB06')
    @MAJ_CAT NVARCHAR(MAX) = NULL    -- one category or comma list (e.g. 'M_JEANS,L_JEANS')
AS
BEGIN
    SET NOCOUNT ON;

    /* ---- measure columns for THIS server's copy of the table ---------------- */
    DECLARE @cols NVARCHAR(MAX);
    SELECT @cols = STRING_AGG(CAST(
               CASE WHEN c.DATA_TYPE IN ('int','bigint','smallint','tinyint','bit',
                                         'decimal','numeric','float','real','money','smallmoney')
                    THEN 'ISNULL(g.' + QUOTENAME(c.COLUMN_NAME) + ', 0) AS ' + QUOTENAME(c.COLUMN_NAME)
                    ELSE 'g.' + QUOTENAME(c.COLUMN_NAME)
               END AS NVARCHAR(MAX)), ',' + CHAR(13) + CHAR(10) + N'        ')
             WITHIN GROUP (ORDER BY c.ORDINAL_POSITION)
    FROM INFORMATION_SCHEMA.COLUMNS c
    WHERE c.TABLE_SCHEMA = 'dbo'
      AND c.TABLE_NAME   = 'ARS_GRID_MJ_VAR_ART'
      AND c.COLUMN_NAME NOT IN ('WERKS','MAJ_CAT','GEN_ART_NUMBER','ARTICLE_NUMBER','CLR','SZ');

    IF @cols IS NULL
    BEGIN
        RAISERROR('usp_ars_grid_var_art: dbo.ARS_GRID_MJ_VAR_ART not found or has no measure columns.', 16, 1);
        RETURN;
    END

    DECLARE @sql NVARCHAR(MAX) = N'
    ;WITH gsel AS (   -- the requested grid slice, filtered ONCE
        SELECT * FROM ARS_GRID_MJ_VAR_ART WITH (NOLOCK)
        WHERE (@WERKS   IS NULL OR @WERKS   = ''''
               OR WERKS   IN (SELECT LTRIM(RTRIM(value)) FROM STRING_SPLIT(@WERKS, '','')))
          AND (@MAJ_CAT IS NULL OR @MAJ_CAT = ''''
               OR MAJ_CAT IN (SELECT LTRIM(RTRIM(value)) FROM STRING_SPLIT(@MAJ_CAT, '','')))
    ),
    pmap AS (   -- one hierarchy row per variant article (deduped, no fan-out)
        -- Restricted by the ARTICLE_NUMBERs actually in the slice, NOT by MAJ_CAT:
        -- the master files some articles under a different MAJ_CAT (NA, or the
        -- dash-prefixed variants), and filtering the map by MAJ_CAT would silently
        -- blank their hierarchy. Keying on the join column is correct AND fast.
        SELECT ARTICLE_NUMBER,
               MAX(SEG)          AS SEG,
               MAX(DIV)          AS DIV,
               MAX(SUB_DIV)      AS SUB_DIV,
               MAX(SSN)          AS SSN,
               MAX(RNG_SEG)      AS RNG_SEG,
               MAX(GEN_ART_DESC) AS GEN_ART_DESC,
               MAX(ARTICLE_DESC) AS ARTICLE_DESC
        FROM vw_master_product WITH (NOLOCK)
        WHERE ARTICLE_NUMBER IN (SELECT ARTICLE_NUMBER FROM gsel)
        GROUP BY ARTICLE_NUMBER
    )
    SELECT
        g.[WERKS],
        ISNULL(pm.SEG, '''')          AS SEG,
        ISNULL(pm.DIV, '''')          AS DIV,
        ISNULL(pm.SUB_DIV, '''')      AS SUB_DIV,
        g.[MAJ_CAT],
        ISNULL(pm.SSN, '''')          AS SSN,
        ISNULL(pm.RNG_SEG, '''')      AS RNG_SEG,
        g.[GEN_ART_NUMBER],
        ISNULL(pm.GEN_ART_DESC, '''') AS GEN_ART_DESC,
        g.[ARTICLE_NUMBER],
        ISNULL(pm.ARTICLE_DESC, '''') AS ARTICLE_DESC,
        g.[CLR],
        g.[SZ],
        ' + @cols + N'
    FROM gsel g
    LEFT JOIN pmap pm ON pm.ARTICLE_NUMBER = g.ARTICLE_NUMBER
    ORDER BY g.WERKS, g.MAJ_CAT, g.GEN_ART_NUMBER, g.CLR, g.SZ
    OPTION (RECOMPILE);';

    EXEC sp_executesql @sql,
         N'@WERKS NVARCHAR(MAX), @MAJ_CAT NVARCHAR(MAX)', @WERKS, @MAJ_CAT;
END
GO
