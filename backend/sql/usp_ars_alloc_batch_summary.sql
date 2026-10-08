/* ============================================================================
   usp_ars_alloc_batch_summary — allocation BATCH summary by DIV / SUB_DIV /
   STATUS / MAJ_CAT, combining the worker queue, MSA availability and the
   leftover store requirement.

   One row per (DIV, SUB_DIV, STATUS, MAJ_CAT) for one allocation batch:
     what the batch shipped  (ARS_ALLOC_MAJCAT_QUEUE)
     what was available      (ARS_MSA_TOTAL)
     what is still unmet     (ARS_LISTING_WORKING_HISTORY.MJ_REQ_REM)

   Params (all optional, NULL = no filter):
     @BATCH_ID  NULL/'' = the LATEST batch by CREATED_AT. Resolved by
                CREATED_AT, not MAX(BATCH_ID): batch ids are not all sortable
                timestamps (e.g. 'FV_B2_allON_145423' sorts above a 2026 id).
     @DIV, @SUB_DIV, @MAJ_CAT, @STATUS

   Sources / grain:
     ARS_ALLOC_MAJCAT_QUEUE      — the batch, 1 row per MAJ_CAT per worker run.
                                   SHIP_QTY / HOLD_QTY / OPT_COUNT are additive.
     vw_master_product           — DIV / SUB_DIV per MAJ_CAT.
     ARS_MSA_TOTAL               — MSA_QTY = SUM(FNL_Q) per MAJ_CAT.
     ARS_LISTING_WORKING_HISTORY — MJ_REQ_REM, keyed by SESSION_ID. The batch's
                                   BATCH_ID IS that SESSION_ID — same run id.

   MJ_REQ_REM semantics: the allocator walks each (WERKS, MAJ_CAT) sequentially
   (RL -> TBC -> TBL), decrementing MJ_REQ_REM as options ship, so the per-store
   MINIMUM is what remained unfilled at the end of the walk. This proc takes
   MIN per store and then SUMs across stores, giving the MAJ_CAT's total unmet
   requirement. STORES_SHORT counts the stores still above zero.

   ⚠ vw_master_product is DEDUPED to one DIV/SUB_DIV per MAJ_CAT with MAX.
     A plain DISTINCT join fans out — 2,025 distinct (MAJ_CAT,DIV,SUB_DIV) rows
     against 1,897 distinct MAJ_CAT, with some (CREPE, JAQUARD, IMP, NET)
     carrying 6 combinations each. Those are fabric-ish values that have not yet
     appeared as an allocation MAJ_CAT — measured inflation on batch
     20261005_124520_655 was 0 rows / 0 pcs — but if one ever does, an
     un-deduped join silently multiplies its SHIP_QTY by 6.

   ⚠ ARS_MSA_TOTAL is scoped to its LATEST sequence_id. The table currently
     holds exactly one (376), so summing everything happens to be right today;
     scoping keeps MSA_QTY correct the moment a second sequence lands.

   Examples:
     EXEC dbo.usp_ars_alloc_batch_summary;
     EXEC dbo.usp_ars_alloc_batch_summary @BATCH_ID = N'20261005_124520_655';
     EXEC dbo.usp_ars_alloc_batch_summary @DIV = N'MENS';
   ============================================================================ */
CREATE OR ALTER PROCEDURE dbo.usp_ars_alloc_batch_summary
    @BATCH_ID NVARCHAR(100) = NULL,
    @DIV      NVARCHAR(100) = NULL,
    @SUB_DIV  NVARCHAR(100) = NULL,
    @MAJ_CAT  NVARCHAR(100) = NULL,
    @STATUS   NVARCHAR(50)  = NULL
AS
BEGIN
    SET NOCOUNT ON;

    -- Resolve the batch once; every CTE scopes to it.
    DECLARE @bid NVARCHAR(100) = NULLIF(LTRIM(RTRIM(ISNULL(@BATCH_ID, ''))), '');
    IF @bid IS NULL
        SELECT TOP 1 @bid = BATCH_ID
        FROM ARS_ALLOC_MAJCAT_QUEUE WITH (NOLOCK)
        GROUP BY BATCH_ID
        ORDER BY MAX(CREATED_AT) DESC;

    ;WITH prod AS (        -- ONE DIV/SUB_DIV per MAJ_CAT (see fan-out note above)
        SELECT MAJ_CAT,
               MAX(DIV)     AS DIV,
               MAX(SUB_DIV) AS SUB_DIV
        FROM vw_master_product WITH (NOLOCK)
        GROUP BY MAJ_CAT
    ),
    msa AS (               -- MSA availability per MAJ_CAT, latest sequence only
        SELECT MAJ_CAT, SUM(CAST(ISNULL(FNL_Q, 0) AS FLOAT)) AS MSA_QTY
        FROM ARS_MSA_TOTAL WITH (NOLOCK)
        WHERE sequence_id = (SELECT MAX(sequence_id) FROM ARS_MSA_TOTAL WITH (NOLOCK))
        GROUP BY MAJ_CAT
    ),
    rem_store AS (         -- per-store leftover requirement = MIN over the walk
        SELECT WERKS, MAJ_CAT,
               MIN(CAST(ISNULL(MJ_REQ_REM, 0) AS FLOAT)) AS REM
        FROM ARS_LISTING_WORKING_HISTORY WITH (NOLOCK)
        WHERE SESSION_ID = @bid
          AND (@MAJ_CAT IS NULL OR MAJ_CAT = @MAJ_CAT)
        GROUP BY WERKS, MAJ_CAT
    ),
    rem AS (               -- rolled to MAJ_CAT
        SELECT MAJ_CAT,
               SUM(REM)                                  AS MJ_REQ_REM,
               COUNT(*)                                  AS STORE_CNT,
               SUM(CASE WHEN REM > 0 THEN 1 ELSE 0 END)  AS STORES_SHORT
        FROM rem_store
        GROUP BY MAJ_CAT
    )
    SELECT
        @bid                                     AS BATCH_ID,
        p.DIV,
        p.SUB_DIV,
        a.STATUS,
        a.MAJ_CAT,
        COUNT(*)                                 AS CNT,
        SUM(ISNULL(a.OPT_COUNT, 0))              AS OPT_COUNT,
        SUM(ISNULL(a.SHIP_QTY,  0))              AS SHIP_QTY,
        SUM(ISNULL(a.HOLD_QTY,  0))              AS HOLD_QTY,
        MAX(ISNULL(msa.MSA_QTY, 0))              AS MSA_QTY,
        ROUND(SUM(ISNULL(a.SHIP_QTY, 0))
              / NULLIF(MAX(msa.MSA_QTY), 0) * 100, 2) AS SHIP_PCT,
        -- MJ_REQ_REM/STORE_CNT are per-MAJ_CAT attributes: MAX (repeated), never SUM
        MAX(ISNULL(r.MJ_REQ_REM,   0))           AS MJ_REQ_REM,
        MAX(ISNULL(r.STORE_CNT,    0))           AS STORE_CNT,
        MAX(ISNULL(r.STORES_SHORT, 0))           AS STORES_SHORT
    FROM ARS_ALLOC_MAJCAT_QUEUE a WITH (NOLOCK)
    LEFT JOIN prod p   ON p.MAJ_CAT   = a.MAJ_CAT
    LEFT JOIN msa      ON msa.MAJ_CAT = a.MAJ_CAT
    LEFT JOIN rem r    ON r.MAJ_CAT   = a.MAJ_CAT
    WHERE a.BATCH_ID = @bid
      AND (@DIV     IS NULL OR p.DIV      = @DIV)
      AND (@SUB_DIV IS NULL OR p.SUB_DIV  = @SUB_DIV)
      AND (@MAJ_CAT IS NULL OR a.MAJ_CAT  = @MAJ_CAT)
      AND (@STATUS  IS NULL OR a.STATUS   = @STATUS)
    GROUP BY p.DIV, p.SUB_DIV, a.STATUS, a.MAJ_CAT
    ORDER BY MAX(ISNULL(msa.MSA_QTY, 0)) DESC
    OPTION (RECOMPILE);   -- simplifies (@p IS NULL OR col=@p) filters so they seek
END
GO
