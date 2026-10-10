/* ============================================================================
   ALLRDC_USP_VAR_ART_ALC_RPT - ALL-RDC variant of USP_VAR_ART_ALC_RPT.

   SAME TEMPLATE as the base proc: identical columns, params and filters. The
   only difference is that OP_MSA_QTY and EXTRA_MSA_QTY are computed across
   EVERY RDC rather than the store's own, so a store sees the national pool.

   Why: the base proc groups both the MSA side and the consumption side by RDC
   and joins a.RDC = b.RDC, charging a store's consumption to the store's own
   RDC. Cross-RDC supply is routine here, so that nets against the wrong pool
   and can drive EXTRA_MSA_QTY negative. Summing across all RDCs removes the
   attribution question altogether - keyed by RDC or by SRC_RDC, the total is
   identical once the split is gone.

   RDC_CD still reports the store's connected RDC; it is descriptive now and no
   longer restricts which pool the MSA figures come from.

   The base proc dbo.USP_VAR_ART_ALC_RPT is left exactly as it was.
   ============================================================================ */
CREATE OR ALTER PROC [dbo].[ALLRDC_USP_VAR_ART_ALC_RPT]    
( 
 @SEG   VARCHAR(20) = '',  
 @DIV   VARCHAR(20) = ''  ,
 @SESSION_ID VARCHAR(50) = NULL
)
AS    
BEGIN    
		
		IF @SESSION_ID IS NULL
		BEGIN
			SELECT @SESSION_ID = MAX([SESSION_ID]) FROM  ARS_ALLOC_HISTORY(NOLOCK)
		END
		
		;with cte_msa_stk as (
		SELECT
			a.MAJ_CAT,
			a.ARTICLE_NUMBER,
			SUM(a.FNL_Q) AS OP_MSA_QTY,
			SUM(a.FNL_Q) - COALESCE(MAX(b.consumed), 0) AS EXTRA_MSA_QTY,
			COALESCE(MAX(b.alloc_consumed), 0) AS ALC_Q,
			COALESCE(MAX(b.consumed), 0) AS ALC_HOLD_Q,
			COALESCE(MAX(b.HOLD_QTY), 0) AS HOLD_QTY
		FROM ARS_MSA_TOTAL_HISTORY(NOLOCK) a
		LEFT JOIN (
			SELECT
				MAJ_CAT,
				VAR_ART,
				SUM(HOLD_QTY + ALLOC_QTY - FROM_HOLD_QTY) AS consumed,
				SUM(ALLOC_QTY) AS alloc_consumed,
				SUM(HOLD_QTY) AS HOLD_QTY
			FROM ARS_ALLOC_HISTORY(NOLOCK)
			WHERE SESSION_ID = @SESSION_ID
			GROUP BY
				MAJ_CAT,
				VAR_ART
		) b
			ON a.MAJ_CAT = b.MAJ_CAT
		   AND a.ARTICLE_NUMBER = b.VAR_ART
		WHERE a.SESSION_ID = @SESSION_ID
		GROUP BY
			a.MAJ_CAT,
			a.ARTICLE_NUMBER)

		SELECT 
			A.SESSION_ID,
			A.APPROVED_AT,
			A.WERKS AS ST_CD,
			A.RDC AS RDC_CD,
			E.SEG,
			E.DIV,
			E.SUB_DIV,
			E.SSN	,
			A.MAJ_CAT,
			A.GEN_ART_NUMBER AS [GEN-ART],
			A.GEN_ART_DESC AS [GEN-DESC],
			A.VAR_ART AS [VAR ART],
			A.VAR_DESC AS [VAR DESC],
			A.SZ,
			A.CLR,
			A.OPT_TYPE AS [ART TYP (LISTING)],
			A.PAK_SZ,
			A.I_ROD AS MAX_I_ROD,
			CASE WHEN A.ALLOC_ROUND=0 THEN 1 ELSE A.ALLOC_ROUND END AS ALC_I_ROD,
			A.OPT_MBQ * CASE WHEN A.ALLOC_ROUND=0 THEN 1 ELSE A.ALLOC_ROUND END AS OPT_MBQ_GEN_ART,
			A.SZ_MBQ * CASE WHEN A.ALLOC_ROUND=0 THEN 1 ELSE A.ALLOC_ROUND END AS OPT_MBQ_VAR_ART,
			A.CONT AS [CONT%],
			A.SZ_STK STK_BFR_ALC,
			A.SZ_STK+A.ALLOC_QTY AS STK_AFT_ALC,
			A.MAX_DAILY_SALE AS BGT_PD_SALE_Q,
			A.ALLOC_QTY AS ALC_Q,
			A.HOLD_QTY AS HOLD_Q,
			SUM(A.ALLOC_QTY) OVER (PARTITION BY a.MAJ_CAT,VAR_ART) AS TTL_ALC_Q,
			SUM(A.HOLD_QTY) OVER (PARTITION BY a.MAJ_CAT,VAR_ART) AS TTL_HOLD_Q,
			b.OP_MSA_QTY,
			b.EXTRA_MSA_QTY,
			A.ALLOC_WAVE,
			A.ALLOC_ROUND,
			A.ALLOC_STATUS,
			A.SKIP_REASON,
			A.ALLOC_REMARKS
		FROM 
			ARS_ALLOC_HISTORY(NOLOCK) A
			left join cte_msa_stk b on a.MAJ_CAT=b.MAJ_CAT and a.VAR_ART=b.ARTICLE_NUMBER
			 LEFT JOIN VW_MASTER_PRODUCT(NOLOCK) E ON A.VAR_ART = E.ARTICLE_NUMBER
		
		WHERE SESSION_ID = @SESSION_ID

 AND
 (@SEG = '' OR @SEG IS NULL OR E.SEG = @SEG)  
   AND (@DIV = '' OR @DIV IS NULL OR E.DIV = @DIV)

   OPTION(RECOMPILE)
    
END

