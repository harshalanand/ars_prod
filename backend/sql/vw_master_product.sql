USE [Rep_Data]
GO

/****** Object:  View [dbo].[VW_MASTER_PRODUCT]    Script Date: 01-10-2026 09:13:36 AM ******/
SET ANSI_NULLS ON
GO

SET QUOTED_IDENTIFIER ON
GO




CREATE OR ALTER   VIEW [dbo].[VW_MASTER_PRODUCT]
AS
SELECT
    /* ── IDENTIFIERS ──────────────────────────────────────────────────────── */
    -- bigint | NOT NULL
    MAS.PRODUCT_ID,

    -- bigint | NULL → 0
    ISNULL(MAS.ARTICLE_NUMBER,                                    0)         AS ARTICLE_NUMBER,

    -- varchar(100) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.ARTICLE_DESC)),                ''), 'NA')  AS ARTICLE_DESC,

    -- bigint | NULL → 0
    ISNULL(MAS.GEN_ART_NUMBER,                                    0)         AS GEN_ART_NUMBER,

    -- varchar(100) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.GEN_ART_DESC)),                ''), 'NA')  AS GEN_ART_DESC,

    /* ── SEGMENTATION ─────────────────────────────────────────────────────── */
    -- varchar(10) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.SEG)),                         ''), 'NA')  AS SEG,

    -- varchar(50) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.DIV)),                         ''), 'NA')  AS DIV,

    -- varchar(50) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.SUB_DIV)),                     ''), 'NA')  AS SUB_DIV,

    -- varchar(100) | NULL/BLANK → 'NA'
    -- Swapped: MAJ_CAT_FR in Master_ALC_INPUT_SWAP_MAJ_CAT → MAJ_CAT_TO, else original
    ISNULL(NULLIF(LTRIM(RTRIM(MC.MAJ_CAT)),                      ''), 'NA')  AS MAJ_CAT,

    -- varchar(35) | NULL/BLANK/'NA' → 'A'
    ISNULL(NULLIF(NULLIF(LTRIM(RTRIM(MAS.CLR)),                  ''), 'NA'), 'A')  AS CLR,

    -- varchar(50) | Swapped: (MAJ_CAT, SZ) found as (MAJ_CAT, SIZE_INV) in
    -- Master_ALC_INPUT_SWAP_SZ → SIZE_PLN, else original; then NULL/BLANK/'NA' → 'A'
    ISNULL(NULLIF(NULLIF(LTRIM(RTRIM(COALESCE(SZW.SIZE_PLN, MAS.SZ))), ''), 'NA'), 'A')  AS SZ,

    /* ── MERCH CATEGORY ───────────────────────────────────────────────────── */
    -- varchar(20) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.MC_CODE)),                     ''), 'NA')  AS MC_CODE,

    -- varchar(40) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.MC_DESC)),                     ''), 'NA')  AS MC_DESC,

    /* ── PRICING & TYPE ───────────────────────────────────────────────────── */
    -- numeric(9,2) | NULL → 0
    ISNULL(MAS.MRP,                                               0)         AS MRP,

    -- varchar(2) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.REG_SEG)),                     ''), 'NA')  AS REG_SEG,

    -- varchar(1) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.ART_TYPE)),                    ''), 'NA')  AS ART_TYPE,

    -- varchar(2) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.ATT_TYP)),                     ''), 'NA')  AS ATT_TYP,

    /* ── DATES ────────────────────────────────────────────────────────────── */
    -- int | NULL → 0  (SAP numeric date e.g. 20240315, NOT a date type)
    ISNULL(MAS.ERSDA,                                             0)         AS ERSDA,

    -- date | NULL → '0'  | format: YYYY-MM-DD
    ISNULL(CONVERT(VARCHAR(10), MAS.F_GRC_DATE,  120),           '0')       AS F_GRC_DATE,

    -- date | NULL → '0'  | format: YYYY-MM-DD
    ISNULL(CONVERT(VARCHAR(10), MAS.L_GRC_DATE,  120),           '0')       AS L_GRC_DATE,

    -- datetime2 | NOT NULL → always has value, just format
    CONVERT(VARCHAR(19), MAS.CREATE_DATE, 120)                              AS CREATE_DATE,

    -- datetime2 | NULL → '0'  | format: YYYY-MM-DD HH:MI:SS
    ISNULL(CONVERT(VARCHAR(19), MAS.MODIFY_DATE, 120),           '0')       AS MODIFY_DATE,

    /* ── VENDOR ───────────────────────────────────────────────────────────── */
    -- bigint | NULL → 0
    ISNULL(MAS.VENDOR_CODE,                                       0)         AS VENDOR_CODE,

    -- varchar(250) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.VENDOR_NAME)),                 ''), 'NA')  AS VENDOR_NAME,

    -- varchar(150) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.VENDOR_CITY)),                 ''), 'NA')  AS VENDOR_CITY,

    -- bigint | NULL → 0
    ISNULL(MAS.M_VND_CD,                                          0)         AS M_VND_CD,

    -- varchar(250) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.M_VND_NM)),                   ''), 'NA')  AS M_VND_NM,

    -- varchar(150) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.MERGE_VENDOR_CITY)),           ''), 'NA')  AS MERGE_VENDOR_CITY,

    /* ── VENDOR GROUPING ──────────────────────────────────────────────────── */
    -- varchar(50) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.MACRO_MVGR)),                  ''), 'NA')  AS MACRO_MVGR,

    -- varchar(50) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.MICRO_MVGR)),                  ''), 'NA')  AS MICRO_MVGR,

    -- varchar(20) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.M_BUYING_TYPE)),               ''), 'NA')  AS M_BUYING_TYPE,

    /* ── FABRIC / WEAVE ───────────────────────────────────────────────────── */
    -- varchar(40) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.M_FAB_1)),                     ''), 'NA')  AS FAB,

    -- varchar(40) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.FAB)),                         ''), 'NA')  AS FAB_OLD,

    -- varchar(20) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.WEAVE_1)),                     ''), 'NA')  AS WEAVE_1,

    -- varchar(20) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.WEAVE_2)),                     ''), 'NA')  AS WEAVE_2,

    -- varchar(50) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.WEAVE_3)),                     ''), 'NA')  AS WEAVE_3,
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.M_YARN)),                     ''), 'NA')  AS M_YARN,
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.M_YARN_02)),                     ''), 'NA')  AS M_YARN_02,


    /* ── MISC ─────────────────────────────────────────────────────────────── */
    -- varchar(10) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.SSN)),                         ''), 'NA')  AS SSN,

    -- numeric(9,2) | NULL → 0
    ISNULL(MAS.PAK_SZ,                                            0)         AS PAK_SZ,

    -- numeric(9,2) | NULL → 0
    ISNULL(MAS.AVG_DENSITY,                                       0)         AS AVG_DENSITY,

    /* ── FLAGS (bit) ──────────────────────────────────────────────────────── */
    -- bit | NULL → 0
    ISNULL(MAS.IS_GENERIC,                                        0)         AS IS_GENERIC,

    -- bit | NULL → 0
    ISNULL(MAS.IS_PLANNING_USE,                                   0)         AS IS_PLANNING_USE,

    -- bit | NULL → 0
    ISNULL(MAS.IS_ACTIVE,                                         0)         AS IS_ACTIVE,


    /* ── SAP ──────────────────────────────────────────────────────────────── */
    -- varchar(20) | NULL/BLANK → 'NA'
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.MATNR)),                       ''), 'NA')  AS MATNR,

    /* ── JOINED COLUMNS ───────────────────────────────────────────────────── */
    -- varchar(2) derived | NULL/BLANK → 'NA'
    ISNULL(
        NULLIF(LTRIM(RTRIM(COALESCE(RRS.RNG_SEG, MAS.REG_SEG))), ''),
        'NA'
    )                                                                         AS RNG_SEG,

    /* ── C_ART STATUS ─────────────────────────────────────────────────────── */
    CASE
        WHEN C_ART.ARTICLE_CD IS NOT NULL THEN 'C_ART'
        ELSE 'NORMAL'
    END                                                                       AS C_ART,


    /* ── SIZE APPLICABLE ──────────────────────────────────────────────────── */
    CASE
        WHEN SZ.MAJ_CAT       IS NOT NULL
         AND SZ.SZ_APPLICABLE  IS NOT NULL
         AND LTRIM(RTRIM(SZ.SZ_APPLICABLE)) <> ''
        THEN SZ.SZ_APPLICABLE
        ELSE 'N'
    END                                                                       AS SZ_APPLICABLE,
    /* ── SIZE APPLICABLE ──────────────────────────────────────────────────── */
    CASE
        WHEN ISNULL(
        NULLIF(LTRIM(RTRIM(COALESCE(RRS.RNG_SEG, MAS.REG_SEG))), ''),
        'NA'
    )  IN ('E','V')  THEN 'EV'
        WHEN ISNULL(
        NULLIF(LTRIM(RTRIM(COALESCE(RRS.RNG_SEG, MAS.REG_SEG))), ''),
        'NA'
    )  IN ('P','SP') THEN 'PSP'
        ELSE  'NA'  -- safety: don't drop rows if a new tier appears
    END AS MERGE_RNG_SEG,

    BM.M_FIT AS FIT,
    BM.M_PATTERN AS BODY,
    ISNULL(NULLIF(LTRIM(RTRIM(MAS.REF_ART)),                         ''), 'NA')  AS REF_ART

FROM       MASTER_PRODUCT           AS MAS   WITH (NOLOCK)
/* ── MAJ_CAT SWAP ─────────────────────────────────────────────────────────
   MAJ_CAT_FR → MAJ_CAT_TO. GROUP BY MAJ_CAT_FR so a duplicate FR row can never
   multiply product rows; blank MAJ_CAT_TO rows are ignored. Swap table is
   nvarchar(255) — cast to varchar(100) so MAJ_CAT keeps MASTER_PRODUCT's type.
   MC.MAJ_CAT is the effective MAJ_CAT used for output and the joins below. */
LEFT JOIN (
    SELECT CAST(LTRIM(RTRIM(SW.MAJ_CAT_FR))      AS VARCHAR(100)) AS MAJ_CAT_FR,
           CAST(MIN(LTRIM(RTRIM(SW.MAJ_CAT_TO))) AS VARCHAR(100)) AS MAJ_CAT_TO
    FROM   Master_ALC_INPUT_SWAP_MAJ_CAT AS SW WITH (NOLOCK)
    WHERE  NULLIF(LTRIM(RTRIM(SW.MAJ_CAT_TO)), '') IS NOT NULL
    GROUP BY LTRIM(RTRIM(SW.MAJ_CAT_FR))
)                                   AS SWP                 ON  MAS.MAJ_CAT       = SWP.MAJ_CAT_FR
CROSS APPLY (SELECT COALESCE(SWP.MAJ_CAT_TO, MAS.MAJ_CAT) AS MAJ_CAT) AS MC
/* ── SIZE SWAP ────────────────────────────────────────────────────────────
   (effective MAJ_CAT, SZ) = (MAJ_CAT, SIZE_INV) → SIZE_PLN. GROUP BY so a
   duplicate row can never multiply product rows; blank SIZE_PLN ignored.
   Cast to varchar(50) so SZ keeps MASTER_PRODUCT's type. */
LEFT JOIN (
    SELECT CAST(LTRIM(RTRIM(SS.MAJ_CAT))       AS VARCHAR(100)) AS MAJ_CAT,
           CAST(LTRIM(RTRIM(SS.SIZE_INV))      AS VARCHAR(50))  AS SIZE_INV,
           CAST(MIN(LTRIM(RTRIM(SS.SIZE_PLN))) AS VARCHAR(50))  AS SIZE_PLN
    FROM   Master_ALC_INPUT_SWAP_SZ AS SS WITH (NOLOCK)
    WHERE  NULLIF(LTRIM(RTRIM(SS.SIZE_PLN)), '') IS NOT NULL
    GROUP BY LTRIM(RTRIM(SS.MAJ_CAT)), LTRIM(RTRIM(SS.SIZE_INV))
)                                   AS SZW                 ON  SZW.MAJ_CAT       = MC.MAJ_CAT
                                                           AND SZW.SIZE_INV      = LTRIM(RTRIM(MAS.SZ))
LEFT JOIN  MASTER_REV_RNG_SEG       AS RRS   WITH (NOLOCK) ON  MC.MAJ_CAT        = RRS.MAJ_CAT
                                                           AND cast(MAS.MRP AS decimal(18,2)) = CAST(RRS.MRP AS decimal(18,2))
LEFT JOIN  ET_C_ARTICLE_DATA        AS C_ART WITH (NOLOCK) ON  MAS.ARTICLE_NUMBER = C_ART.ARTICLE_CD
LEFT JOIN  ARS_GRID_HIERARCHY        AS SZ    WITH (NOLOCK) ON  MC.MAJ_CAT        = SZ.MAJ_CAT
LEFT JOIN  MASTER_ART_BROADER_MENU  AS BM    WITH (NOLOCK) ON  MAS.GEN_ART_NUMBER = CAST(BM.MATNR AS BIGINT)

;
GO

/* ── VERIFY after deploy ─────────────────────────────────────────────────────
-- 1. Row count must be unchanged (no fan-out):
SELECT (SELECT COUNT(*) FROM MASTER_PRODUCT) AS base_rows,
       (SELECT COUNT(*) FROM VW_MASTER_PRODUCT) AS view_rows;

-- 2. No MAJ_CAT_FR value should survive in the view (identity rows FR = TO excluded):
SELECT V.MAJ_CAT, COUNT(*) AS n
FROM   VW_MASTER_PRODUCT V
JOIN   Master_ALC_INPUT_SWAP_MAJ_CAT S ON S.MAJ_CAT_FR = V.MAJ_CAT AND S.MAJ_CAT_FR <> S.MAJ_CAT_TO
GROUP BY V.MAJ_CAT;

-- 3. Types must stay MAJ_CAT varchar(100), CLR varchar(35), SZ varchar(50):
SELECT c.name, t.name, c.max_length FROM sys.columns c JOIN sys.types t ON c.user_type_id = t.user_type_id
WHERE  c.object_id = OBJECT_ID('dbo.VW_MASTER_PRODUCT') AND c.name IN ('MAJ_CAT','CLR','SZ');

-- 4. No (MAJ_CAT, SIZE_INV) pair should survive, and no 'NA' CLR / SZ:
SELECT COUNT(*) FROM VW_MASTER_PRODUCT V
JOIN   Master_ALC_INPUT_SWAP_SZ S ON S.MAJ_CAT = V.MAJ_CAT AND S.SIZE_INV = V.SZ;
SELECT SUM(CASE WHEN CLR = 'NA' THEN 1 ELSE 0 END), SUM(CASE WHEN SZ = 'NA' THEN 1 ELSE 0 END) FROM VW_MASTER_PRODUCT;

-- After deploy: EXEC sp_refreshview 'dbo.VW_ET_MSA_STK_WITH_MASTER';
*/
