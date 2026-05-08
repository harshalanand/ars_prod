-- ============================================================================
-- MASTER_GEN_ART_AGE
-- ----------------------------------------------------------------------------
-- Stores "option" age in days at the store level.
-- An option = (ST_CD, MAJ_CAT, GEN_ART_NUMBER, CLR).
--
-- Consumed by the Listing build (Part 3.5c → ARS_LISTING.AGE) and the
-- OPT_MBQ rule (Part 5b):
--   AGE < 15 → rate = MAX(PER_OPT_SALE, L-7/7, AUTO_GEN_ART_SALE)
--   else     → rate = MAX(L-7/7, AUTO_GEN_ART_SALE)
--   OPT_MBQ  = DPN + rate * SAL_D
-- ============================================================================

IF OBJECT_ID('MASTER_GEN_ART_AGE','U') IS NOT NULL DROP TABLE MASTER_GEN_ART_AGE;

CREATE TABLE MASTER_GEN_ART_AGE (
    ST_CD            NVARCHAR(50)  NOT NULL,
    MAJ_CAT          NVARCHAR(100) NOT NULL,
    GEN_ART_NUMBER   BIGINT        NOT NULL,
    CLR              NVARCHAR(50)  NOT NULL,
    AGE              INT           NULL,
    UPLOAD_DATETIME  NVARCHAR(50)  NULL DEFAULT CONVERT(NVARCHAR(50), GETDATE(), 120),
    CONSTRAINT PK_MASTER_GEN_ART_AGE
        PRIMARY KEY (ST_CD, MAJ_CAT, GEN_ART_NUMBER, CLR)
);

-- ----------------------------------------------------------------------------
-- Dummy seed: one row per distinct option in ARS_LISTING.
-- AGE = deterministic pseudo-random 1..180 via CHECKSUM of the composite key
-- (so rerunning gives identical values; ~8% of rows fall under 15 days).
-- Replace this with the real upstream feed once it is wired up.
-- ----------------------------------------------------------------------------
INSERT INTO MASTER_GEN_ART_AGE (ST_CD, MAJ_CAT, GEN_ART_NUMBER, CLR, AGE, UPLOAD_DATETIME)
SELECT
    T.WERKS, T.MAJ_CAT, T.GEN_ART_NUMBER, T.CLR,
    CAST(ABS(CHECKSUM(T.WERKS, T.MAJ_CAT, T.GEN_ART_NUMBER, T.CLR)) % 180 AS INT) + 1  AS AGE,
    CONVERT(NVARCHAR(50), GETDATE(), 120)
FROM (
    SELECT DISTINCT WERKS, MAJ_CAT, GEN_ART_NUMBER, ISNULL(CLR,'') AS CLR
    FROM ARS_LISTING
    WHERE GEN_ART_NUMBER IS NOT NULL AND GEN_ART_NUMBER > 0
      AND WERKS IS NOT NULL AND MAJ_CAT IS NOT NULL
) T;

-- Sanity check
SELECT COUNT(*)                                         AS row_count,
       COUNT(DISTINCT ST_CD)                            AS store_count,
       COUNT(DISTINCT GEN_ART_NUMBER)                   AS article_count,
       MIN(AGE)                                         AS min_age,
       MAX(AGE)                                         AS max_age,
       SUM(CASE WHEN AGE < 15 THEN 1 ELSE 0 END)        AS new_option_count
FROM MASTER_GEN_ART_AGE;
