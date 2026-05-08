/* ===========================================================================
   usp_ars_allocate_majcat
   ---------------------------------------------------------------------------
   Allocates ONE MAJ_CAT (RL -> TBC -> TBL waterfall, all rounds, all ranks)
   entirely server-side. Mirrors the Python rule_engine_new logic exactly so
   results are bit-identical to sequential / python_parallel modes.

   Caller: rule_engine_parallel_sql.py (Python orchestrator). Several callers
   run concurrently on different MAJ_CATs, each from its own connection
   (=> own session => own #nre_pool tempdb table). They never collide because
   MAJ_CATs are independent in the rule engine.

   Inputs:
     @maj_cat            single MAJ_CAT to allocate
     @working_table      ARS_LISTING_WORKING (has _REM shadow cols)
     @alloc_table        ARS_ALLOC_WORKING   (target of SHIP_QTY/HOLD_QTY)
     @msa_var_table      ARS_MSA_VAR_ART     (source of FNL_Q for pool)
     @grids_json         JSON array of grid metadata, see below
     @pri_ct_check_rl    1=apply PRI_CT% gate to RL  (mirror of Python flag)
     @pri_ct_check_tbc   1=apply PRI_CT% gate to TBC (mirror of Python flag)
     @acs_skip_factor    H_REM threshold; default 0.5

   @grids_json shape (Python builds this from _discover_primary_grids):
     [
       { "req_col":"MJ_REQ",        "req_rem":"MJ_REQ_REM",
         "gh_col":"GH_MJ",          "h_col":"H_MJ", "h_rem":"H_MJ_REM",
         "extras":[] },
       { "req_col":"RNG_SEG_REQ",   "req_rem":"RNG_SEG_REQ_REM",
         "gh_col":"GH_RNG_SEG",     "h_col":"H_RNG_SEG", "h_rem":"H_RNG_SEG_REM",
         "extras":["RNG_SEG"] },
       ...
     ]

   Outputs:
     @ship_out, @hold_out  — totals for this MAJ_CAT (post-allocation)
     @rows_out             — row count touched for this MAJ_CAT
   =========================================================================== */
IF OBJECT_ID('dbo.usp_ars_allocate_majcat','P') IS NOT NULL
    DROP PROCEDURE dbo.usp_ars_allocate_majcat;
GO

CREATE PROCEDURE dbo.usp_ars_allocate_majcat
    @maj_cat            NVARCHAR(50),
    @working_table      SYSNAME       = N'ARS_LISTING_WORKING',
    @alloc_table        SYSNAME       = N'ARS_ALLOC_WORKING',
    @msa_var_table      SYSNAME       = N'ARS_MSA_VAR_ART',
    @grids_json         NVARCHAR(MAX) = NULL,
    @pri_ct_check_rl    BIT           = 1,
    @pri_ct_check_tbc   BIT           = 1,
    @acs_skip_factor    FLOAT         = 0.5,
    @ship_out           FLOAT         OUTPUT,
    @hold_out           FLOAT         OUTPUT,
    @rows_out           INT           OUTPUT
AS
BEGIN
    SET NOCOUNT ON;

    BEGIN TRY

    -- ------------------------------------------------------------------
    -- 0. Per-session pool (#nre_pool) — filtered to this MAJ_CAT only.
    --
    -- IMPORTANT: #nre_pool MUST be created in this proc's static scope,
    -- not inside sp_executesql.  Temp tables created inside dynamic SQL
    -- are scoped to that nested execution and disappear the moment
    -- sp_executesql returns — which is what previously made the child
    -- procs fail with "Invalid object name #nre_pool" and dragged the
    -- transaction into the "cannot be committed" (msg 3930) state.
    --
    -- We hardcode dbo.ARS_ALLOC_WORKING here because the orchestrator
    -- only ever passes that name; the @alloc_table param is still
    -- honoured by every other UPDATE in this proc and the helpers.
    -- ------------------------------------------------------------------
    IF OBJECT_ID('tempdb..#nre_pool') IS NOT NULL DROP TABLE #nre_pool;
    SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ,
           MAX(ISNULL(FNL_Q,0)) AS FNL_Q_ORIG,
           MAX(ISNULL(FNL_Q,0)) AS FNL_Q_REM
      INTO #nre_pool
      FROM dbo.ARS_ALLOC_WORKING
     WHERE MAJ_CAT = @maj_cat
     GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ;

    BEGIN TRY
        CREATE UNIQUE CLUSTERED INDEX IX_pool_key ON #nre_pool
            (RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ);
    END TRY BEGIN CATCH END CATCH;

    DECLARE @sql NVARCHAR(MAX);

    -- ------------------------------------------------------------------
    -- 1. Decode grids JSON (one row per primary grid for this run)
    -- ------------------------------------------------------------------
    DECLARE @grids TABLE (
        ord       INT IDENTITY(1,1),
        req_col   NVARCHAR(100),
        req_rem   NVARCHAR(100),
        gh_col    NVARCHAR(100),
        h_col     NVARCHAR(100),
        h_rem     NVARCHAR(100),
        extras    NVARCHAR(MAX)  -- JSON array
    );
    IF @grids_json IS NOT NULL AND LEN(@grids_json) > 2
        INSERT INTO @grids (req_col, req_rem, gh_col, h_col, h_rem, extras)
        SELECT req_col, req_rem, gh_col, h_col, h_rem, extras
        FROM OPENJSON(@grids_json)
        WITH (
            req_col  NVARCHAR(100) '$.req_col',
            req_rem  NVARCHAR(100) '$.req_rem',
            gh_col   NVARCHAR(100) '$.gh_col',
            h_col    NVARCHAR(100) '$.h_col',
            h_rem    NVARCHAR(100) '$.h_rem',
            extras   NVARCHAR(MAX) AS JSON
        );

    -- Build the H_REM SET fragment + pri_ct numerator/denominator once.
    DECLARE @h_rem_sets NVARCHAR(MAX) = N'';
    DECLARE @h_sum      NVARCHAR(MAX) = N'';
    DECLARE @gh_sum     NVARCHAR(MAX) = N'';

    DECLARE g_cur CURSOR LOCAL FAST_FORWARD FOR
        SELECT req_rem, gh_col, h_rem FROM @grids ORDER BY ord;

    DECLARE @gr_req_rem NVARCHAR(100), @gr_gh NVARCHAR(100), @gr_h_rem NVARCHAR(100);
    OPEN g_cur;
    FETCH NEXT FROM g_cur INTO @gr_req_rem, @gr_gh, @gr_h_rem;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        IF LEN(@h_rem_sets) > 0 SET @h_rem_sets = @h_rem_sets + N', ';
        SET @h_rem_sets = @h_rem_sets +
            N'[' + @gr_h_rem + N'] = CASE WHEN ISNULL([' + @gr_req_rem + N'],0) > '
            + CAST(@acs_skip_factor AS NVARCHAR(50)) + N' * ISNULL(ACS_D,0) '
            + N'AND ISNULL([' + @gr_gh + N'],0) = 1 THEN 1 ELSE 0 END';
        IF LEN(@h_sum) > 0 SET @h_sum = @h_sum + N' + ';
        SET @h_sum  = @h_sum  + N'ISNULL([' + @gr_h_rem + N'],0)';
        IF LEN(@gh_sum) > 0 SET @gh_sum = @gh_sum + N' + ';
        SET @gh_sum = @gh_sum + N'ISNULL([' + @gr_gh + N'],0)';
        FETCH NEXT FROM g_cur INTO @gr_req_rem, @gr_gh, @gr_h_rem;
    END
    CLOSE g_cur; DEALLOCATE g_cur;

    -- The PRI_CT% gate enforces TBL always; RL/TBC honour caller flags.
    DECLARE @pri_opts NVARCHAR(100) = N'''TBL''';
    IF @pri_ct_check_rl  = 1 SET @pri_opts = @pri_opts + N',''RL''';
    IF @pri_ct_check_tbc = 1 SET @pri_opts = @pri_opts + N',''TBC''';

    -- ------------------------------------------------------------------
    -- 2. Outer waterfall: OPT_TYPE -> round -> rank
    -- ------------------------------------------------------------------
    DECLARE @ot         NVARCHAR(10);
    DECLARE @r          INT;
    DECLARE @max_round  INT;
    DECLARE @rank       INT;

    DECLARE ot_cur CURSOR LOCAL FAST_FORWARD FOR
        SELECT 'RL' UNION ALL SELECT 'TBC' UNION ALL SELECT 'TBL';
    OPEN ot_cur;
    FETCH NEXT FROM ot_cur INTO @ot;

    WHILE @@FETCH_STATUS = 0
    BEGIN
        SET @sql = N'
            SELECT @mr = ISNULL(MAX(I_ROD),0)
              FROM [' + @alloc_table + N']
             WHERE OPT_TYPE = @ot AND MAJ_CAT = @mc';
        EXEC sp_executesql @sql,
            N'@mr INT OUTPUT, @ot NVARCHAR(10), @mc NVARCHAR(50)',
            @mr = @max_round OUTPUT, @ot = @ot, @mc = @maj_cat;

        SET @r = 1;
        WHILE @r <= @max_round
        BEGIN
            -- Reset round deltas for this opt_type/maj_cat
            SET @sql = N'
                UPDATE [' + @alloc_table + N']
                   SET ROUND_SHIP = 0, ROUND_HOLD = 0
                 WHERE OPT_TYPE = @ot AND MAJ_CAT = @mc';
            EXEC sp_executesql @sql,
                N'@ot NVARCHAR(10), @mc NVARCHAR(50)',
                @ot = @ot, @mc = @maj_cat;

            -- Build the iterable list of ranks for this round (sparse:
            -- a MAJ_CAT only owns a subset of the global rank space).
            -- Use a #temp table because table variables can't cross dynamic
            -- SQL boundaries cleanly.
            IF OBJECT_ID('tempdb..#nre_ranks') IS NOT NULL DROP TABLE #nre_ranks;
            CREATE TABLE #nre_ranks (rk INT PRIMARY KEY);
            SET @sql = N'
                INSERT INTO #nre_ranks (rk)
                SELECT DISTINCT OPT_PRIORITY_RANK
                  FROM [' + @alloc_table + N']
                 WHERE OPT_TYPE = @ot AND MAJ_CAT = @mc
                   AND ISNULL(I_ROD,1) >= @r
                   AND OPT_PRIORITY_RANK IS NOT NULL';
            EXEC sp_executesql @sql,
                N'@ot NVARCHAR(10), @mc NVARCHAR(50), @r INT',
                @ot = @ot, @mc = @maj_cat, @r = @r;

            -- Iterate ranks ascending — BAND_SIZE=1 (one rank per band)
            DECLARE rk_cur CURSOR LOCAL FAST_FORWARD FOR
                SELECT rk FROM #nre_ranks ORDER BY rk;
            OPEN rk_cur;
            FETCH NEXT FROM rk_cur INTO @rank;
            WHILE @@FETCH_STATUS = 0
            BEGIN
                EXEC dbo._usp_ars_alloc_band_one
                     @maj_cat   = @maj_cat,
                     @opt_type  = @ot,
                     @round_r   = @r,
                     @rank      = @rank,
                     @alloc_table = @alloc_table;

                EXEC dbo._usp_ars_revalidate_band_one
                     @maj_cat        = @maj_cat,
                     @opt_type       = @ot,
                     @rank           = @rank,
                     @working_table  = @working_table,
                     @alloc_table    = @alloc_table,
                     @h_rem_sets     = @h_rem_sets,
                     @h_sum          = @h_sum,
                     @gh_sum         = @gh_sum,
                     @pri_opts       = @pri_opts,
                     @acs_skip_factor= @acs_skip_factor,
                     @grids_json     = @grids_json;

                FETCH NEXT FROM rk_cur INTO @rank;
            END
            CLOSE rk_cur; DEALLOCATE rk_cur;

            SET @r = @r + 1;
        END

        FETCH NEXT FROM ot_cur INTO @ot;
    END
    CLOSE ot_cur; DEALLOCATE ot_cur;

    IF OBJECT_ID('tempdb..#nre_pool')  IS NOT NULL DROP TABLE #nre_pool;
    IF OBJECT_ID('tempdb..#nre_ranks') IS NOT NULL DROP TABLE #nre_ranks;

    -- ------------------------------------------------------------------
    -- 3. Output totals
    -- ------------------------------------------------------------------
    SET @sql = N'
        SELECT @sh = ISNULL(SUM(SHIP_QTY),0),
               @ho = ISNULL(SUM(HOLD_QTY),0),
               @rw = COUNT(*)
          FROM [' + @alloc_table + N']
         WHERE MAJ_CAT = @mc';
    EXEC sp_executesql @sql,
        N'@sh FLOAT OUTPUT, @ho FLOAT OUTPUT, @rw INT OUTPUT, @mc NVARCHAR(50)',
        @sh = @ship_out OUTPUT, @ho = @hold_out OUTPUT, @rw = @rows_out OUTPUT,
        @mc = @maj_cat;

    END TRY
    BEGIN CATCH
        -- Re-raise the *real* error to the caller. Without this, the
        -- ambient SQLAlchemy transaction reports only the generic msg
        -- 3930 ("transaction cannot be committed") and the actual cause
        -- (e.g. "Invalid object name #nre_pool") stays hidden.
        DECLARE @err_msg  NVARCHAR(4000) = ERROR_MESSAGE();
        DECLARE @err_num  INT            = ERROR_NUMBER();
        DECLARE @err_line INT            = ERROR_LINE();
        DECLARE @err_proc NVARCHAR(200)  = ISNULL(ERROR_PROCEDURE(), N'(adhoc)');
        IF OBJECT_ID('tempdb..#nre_pool')  IS NOT NULL DROP TABLE #nre_pool;
        IF OBJECT_ID('tempdb..#nre_ranks') IS NOT NULL DROP TABLE #nre_ranks;
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;
        DECLARE @combined NVARCHAR(4000) =
            N'usp_ars_allocate_majcat failed in ' + @err_proc
            + N' (msg ' + CAST(@err_num AS NVARCHAR(20))
            + N', line ' + CAST(@err_line AS NVARCHAR(20))
            + N'): ' + @err_msg;
        RAISERROR(@combined, 16, 1);
    END CATCH
END
GO


/* ===========================================================================
   _usp_ars_alloc_band_one
     One band (rank) UPDATE — verbatim port of rule_engine_new._stage_c_run_band
     scoped to one (maj_cat, opt_type, round, rank).
   =========================================================================== */
IF OBJECT_ID('dbo._usp_ars_alloc_band_one','P') IS NOT NULL
    DROP PROCEDURE dbo._usp_ars_alloc_band_one;
GO

CREATE PROCEDURE dbo._usp_ars_alloc_band_one
    @maj_cat      NVARCHAR(50),
    @opt_type     NVARCHAR(10),
    @round_r      INT,
    @rank         INT,
    @alloc_table  SYSNAME = N'ARS_ALLOC_WORKING'  -- legacy param, ignored;
                                                   -- table is hardcoded to
                                                   -- dbo.ARS_ALLOC_WORKING
AS
BEGIN
    SET NOCOUNT ON;

    -- Static T-SQL (no sp_executesql).  Earlier dynamic-SQL versions of
    -- this body raised msg 4145 inside Azure SQL's dynamic-SQL parser.
    -- The exact same statements run cleanly as static SQL — same way the
    -- Python sequential / python_parallel paths use them via SQLAlchemy.

    -- Step 1 — cumulative-window pool take
    ;WITH Target AS (
        SELECT A.WERKS, A.RDC, A.MAJ_CAT, A.GEN_ART_NUMBER, A.CLR,
               A.VAR_ART, A.SZ, A.OPT_PRIORITY_RANK, A.ST_RANK, A.IS_NEW,
               ISNULL(A.POOL_CONSUMED, 0) AS prev_pool,
               ISNULL(A.SHIP_QTY,      0) AS prev_ship,
               ISNULL(A.HOLD_QTY,      0) AS prev_hold,
               CASE WHEN @round_r * ISNULL(A.SZ_MBQ_WH,0) - ISNULL(A.SZ_STK,0)
                       > ISNULL(A.POOL_CONSUMED,0)
                    THEN @round_r * ISNULL(A.SZ_MBQ_WH,0) - ISNULL(A.SZ_STK,0)
                       - ISNULL(A.POOL_CONSUMED,0)
                    ELSE 0 END AS need_pool,
               CASE WHEN @round_r * ISNULL(A.SZ_MBQ,0) - ISNULL(A.SZ_STK,0)
                       > ISNULL(A.SHIP_QTY,0)
                    THEN @round_r * ISNULL(A.SZ_MBQ,0) - ISNULL(A.SZ_STK,0)
                       - ISNULL(A.SHIP_QTY,0)
                    ELSE 0 END AS need_ship,
               CASE WHEN ISNULL(A.I_ROD,1) * ISNULL(A.SZ_MBQ_WH,0)
                       - ISNULL(A.SZ_STK,0) > 0
                    THEN ISNULL(A.I_ROD,1) * ISNULL(A.SZ_MBQ_WH,0)
                       - ISNULL(A.SZ_STK,0)
                    ELSE 0 END AS lifetime_target
        FROM dbo.ARS_ALLOC_WORKING A
        WHERE A.OPT_TYPE = @opt_type
          AND A.OPT_PRIORITY_RANK = @rank
          AND ISNULL(A.ALLOC_STATUS,'PENDING') NOT IN ('SKIPPED','INELIGIBLE')
          AND ISNULL(A.I_ROD,1) >= @round_r
          AND A.MAJ_CAT = @maj_cat
    ),
    Ranked AS (
        SELECT T.*, P.FNL_Q_REM,
               ROW_NUMBER() OVER (
                 PARTITION BY T.RDC, T.MAJ_CAT, T.GEN_ART_NUMBER, T.CLR, T.VAR_ART, T.SZ
                 ORDER BY
                   T.OPT_PRIORITY_RANK ASC,
                   ISNULL(T.ST_RANK, 999999) ASC
               ) AS ord
        FROM Target T
        INNER JOIN #nre_pool P
            ON P.RDC = T.RDC AND P.MAJ_CAT = T.MAJ_CAT
           AND P.GEN_ART_NUMBER = T.GEN_ART_NUMBER
           AND ISNULL(P.CLR,'') = ISNULL(T.CLR,'')
           AND P.VAR_ART = T.VAR_ART AND P.SZ = T.SZ
        WHERE T.need_pool > 0 AND P.FNL_Q_REM > 0
    ),
    Cum AS (
        SELECT *,
               SUM(need_pool) OVER (
                 PARTITION BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ
                 ORDER BY ord ROWS UNBOUNDED PRECEDING
               ) AS cum_demand
        FROM Ranked
    ),
    Take AS (
        SELECT *,
               CASE
                 WHEN FNL_Q_REM - (cum_demand - need_pool) <= 0 THEN 0
                 WHEN FNL_Q_REM - (cum_demand - need_pool) >= need_pool THEN need_pool
                 ELSE FNL_Q_REM - (cum_demand - need_pool)
               END AS take_pool
        FROM Cum
    )
    UPDATE A SET
        A.POOL_CONSUMED = ISNULL(A.POOL_CONSUMED,0) + X.take_pool,
        A.ROUND_SHIP    = CASE WHEN A.IS_NEW = 1
                               THEN CASE WHEN X.take_pool < X.need_ship
                                         THEN X.take_pool ELSE X.need_ship END
                               ELSE X.take_pool END,
        A.ROUND_HOLD    = CASE WHEN A.IS_NEW = 1
                               THEN X.take_pool - CASE WHEN X.take_pool < X.need_ship
                                                       THEN X.take_pool ELSE X.need_ship END
                               ELSE 0 END,
        A.SHIP_QTY      = ISNULL(A.SHIP_QTY,0) +
                          CASE WHEN A.IS_NEW = 1
                               THEN CASE WHEN X.take_pool < X.need_ship
                                         THEN X.take_pool ELSE X.need_ship END
                               ELSE X.take_pool END,
        A.HOLD_QTY      = ISNULL(A.HOLD_QTY,0) +
                          CASE WHEN A.IS_NEW = 1
                               THEN X.take_pool - CASE WHEN X.take_pool < X.need_ship
                                                       THEN X.take_pool ELSE X.need_ship END
                               ELSE 0 END,
        A.ALLOC_WAVE    = CONCAT(@opt_type, '_R', @round_r),
        A.ALLOC_ROUND   = @round_r,
        A.ALLOC_STATUS  = CASE
            WHEN ISNULL(A.POOL_CONSUMED,0) + X.take_pool >= X.lifetime_target
            THEN 'ALLOCATED'
            ELSE 'PARTIAL'
        END
    FROM dbo.ARS_ALLOC_WORKING A
    INNER JOIN Take X
        ON A.WERKS = X.WERKS AND A.RDC = X.RDC
       AND A.MAJ_CAT = X.MAJ_CAT AND A.GEN_ART_NUMBER = X.GEN_ART_NUMBER
       AND ISNULL(A.CLR,'') = ISNULL(X.CLR,'')
       AND A.VAR_ART = X.VAR_ART AND A.SZ = X.SZ
    WHERE X.take_pool > 0;

    -- Step 2 — decrement pool
    ;WITH S AS (
        SELECT RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ,
               SUM(ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0)) AS taken
        FROM dbo.ARS_ALLOC_WORKING
        WHERE OPT_TYPE = @opt_type
          AND OPT_PRIORITY_RANK = @rank
          AND ALLOC_ROUND = @round_r
          AND MAJ_CAT = @maj_cat
        GROUP BY RDC, MAJ_CAT, GEN_ART_NUMBER, CLR, VAR_ART, SZ
        HAVING SUM(ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0)) > 0
    )
    UPDATE P SET P.FNL_Q_REM = P.FNL_Q_REM - S.taken
    FROM #nre_pool P
    INNER JOIN S
        ON P.RDC = S.RDC AND P.MAJ_CAT = S.MAJ_CAT
       AND P.GEN_ART_NUMBER = S.GEN_ART_NUMBER
       AND ISNULL(P.CLR,'') = ISNULL(S.CLR,'')
       AND P.VAR_ART = S.VAR_ART AND P.SZ = S.SZ;
END
GO


/* ===========================================================================
   _usp_ars_revalidate_band_one
     Revalidation after one band — verbatim port of
     rule_engine_new._revalidate_after_band, scoped to one (maj_cat, rank).
   =========================================================================== */
IF OBJECT_ID('dbo._usp_ars_revalidate_band_one','P') IS NOT NULL
    DROP PROCEDURE dbo._usp_ars_revalidate_band_one;
GO

CREATE PROCEDURE dbo._usp_ars_revalidate_band_one
    @maj_cat        NVARCHAR(50),
    @opt_type       NVARCHAR(10),
    @rank           INT,
    @working_table  SYSNAME       = N'ARS_LISTING_WORKING',
    @alloc_table    SYSNAME       = N'ARS_ALLOC_WORKING',
    @h_rem_sets     NVARCHAR(MAX) = N'',
    @h_sum          NVARCHAR(MAX) = N'',
    @gh_sum         NVARCHAR(MAX) = N'',
    @pri_opts       NVARCHAR(100) = N'''TBL''',
    @acs_skip_factor FLOAT        = 0.5,
    @grids_json     NVARCHAR(MAX) = NULL
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @sql NVARCHAR(MAX);

    -- Early-exit: if this band took nothing, no _REM changed.
    DECLARE @band_take FLOAT = 0;
    SET @sql = N'
        SELECT @bt = ISNULL(SUM(ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0)), 0)
          FROM [' + @alloc_table + N']
         WHERE OPT_TYPE = @ot AND OPT_PRIORITY_RANK = @rk AND MAJ_CAT = @mc';
    EXEC sp_executesql @sql,
        N'@bt FLOAT OUTPUT, @ot NVARCHAR(10), @rk INT, @mc NVARCHAR(50)',
        @bt = @band_take OUTPUT, @ot = @opt_type, @rk = @rank, @mc = @maj_cat;
    IF ISNULL(@band_take,0) <= 0 RETURN;

    -- (1) Reduce MSA_FNL_Q_REM per OPT
    SET @sql = N'
        ;WITH OptTake AS (
            SELECT WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR,
                   SUM(ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0)) AS take_total
              FROM [' + @alloc_table + N']
             WHERE OPT_TYPE = @ot AND OPT_PRIORITY_RANK = @rk AND MAJ_CAT = @mc
             GROUP BY WERKS, MAJ_CAT, GEN_ART_NUMBER, CLR
            HAVING SUM(ISNULL(ROUND_SHIP,0) + ISNULL(ROUND_HOLD,0)) > 0
        )
        UPDATE W SET
            W.MSA_FNL_Q_REM = CASE
                WHEN ISNULL(W.MSA_FNL_Q_REM,0) - O.take_total < 0 THEN 0
                ELSE ISNULL(W.MSA_FNL_Q_REM,0) - O.take_total END
          FROM [' + @working_table + N'] W
          INNER JOIN OptTake O
             ON W.WERKS=O.WERKS AND W.MAJ_CAT=O.MAJ_CAT
            AND W.GEN_ART_NUMBER=O.GEN_ART_NUMBER
            AND ISNULL(W.CLR,'''') = ISNULL(O.CLR,'''')';
    EXEC sp_executesql @sql,
        N'@ot NVARCHAR(10), @rk INT, @mc NVARCHAR(50)',
        @ot = @opt_type, @rk = @rank, @mc = @maj_cat;

    -- (2) Reduce each grid's <REQ>_REM at its grain. Generated per grid.
    -- T-SQL variables are BATCH-scoped, not block-scoped. To avoid stale
    -- values leaking across iterations of the WHILE loop, every working
    -- string is reset with SET at the top of each iteration.
    IF @grids_json IS NOT NULL AND LEN(@grids_json) > 2
    BEGIN
        DECLARE @gj_req_rem  NVARCHAR(100);
        DECLARE @gj_extras   NVARCHAR(MAX);
        DECLARE @key_cols    NVARCHAR(MAX);
        DECLARE @key_select  NVARCHAR(MAX);
        DECLARE @join_cond   NVARCHAR(MAX);
        DECLARE @group_by    NVARCHAR(MAX);
        DECLARE @ek          NVARCHAR(100);
        DECLARE @kk          NVARCHAR(100);
        DECLARE @first       BIT;

        DECLARE gj_cur CURSOR LOCAL FAST_FORWARD FOR
            SELECT req_rem, extras FROM OPENJSON(@grids_json)
            WITH (
                req_rem NVARCHAR(100) '$.req_rem',
                extras  NVARCHAR(MAX) AS JSON
            );
        OPEN gj_cur;
        FETCH NEXT FROM gj_cur INTO @gj_req_rem, @gj_extras;
        WHILE @@FETCH_STATUS = 0
        BEGIN
            -- Reset every per-iteration string explicitly.
            SET @key_cols   = N'WERKS, MAJ_CAT';
            SET @key_select = N'';
            SET @join_cond  = N'';
            SET @group_by   = N'';
            SET @first      = 1;

            -- Append extras (e.g. RNG_SEG, MACRO_MVGR) to the grain
            IF @gj_extras IS NOT NULL AND LEN(@gj_extras) > 2
            BEGIN
                DECLARE ek_cur CURSOR LOCAL FAST_FORWARD FOR
                    SELECT [value] FROM OPENJSON(@gj_extras);
                OPEN ek_cur;
                FETCH NEXT FROM ek_cur INTO @ek;
                WHILE @@FETCH_STATUS = 0
                BEGIN
                    SET @key_cols = @key_cols + N', ' + @ek;
                    FETCH NEXT FROM ek_cur INTO @ek;
                END
                CLOSE ek_cur; DEALLOCATE ek_cur;
            END

            -- Build SELECT/GROUP-BY/JOIN strings from the comma-CSV.
            -- STRING_SPLIT order is unspecified, but the resulting SQL is
            -- semantically order-insensitive (same set of grain keys).
            DECLARE kk_cur CURSOR LOCAL FAST_FORWARD FOR
                SELECT LTRIM(RTRIM(value))
                FROM STRING_SPLIT(@key_cols, ',');
            OPEN kk_cur;
            FETCH NEXT FROM kk_cur INTO @kk;
            WHILE @@FETCH_STATUS = 0
            BEGIN
                IF @first = 0
                BEGIN
                    SET @key_select = @key_select + N', ';
                    SET @join_cond  = @join_cond  + N' AND ';
                    SET @group_by   = @group_by   + N', ';
                END
                SET @key_select = @key_select + N'W2.[' + @kk + N']';
                SET @group_by   = @group_by   + N'W2.[' + @kk + N']';
                -- NULL-safe NVARCHAR-cast match. Same as sequential, so
                -- results stay bit-identical.
                SET @join_cond  = @join_cond
                    + N'ISNULL(CAST(W.[' + @kk + N'] AS NVARCHAR(200)),'''') = '
                    + N'ISNULL(CAST(G.[' + @kk + N'] AS NVARCHAR(200)),'''')';
                SET @first = 0;
                FETCH NEXT FROM kk_cur INTO @kk;
            END
            CLOSE kk_cur; DEALLOCATE kk_cur;

            SET @sql = N'
                IF COL_LENGTH(''' + @working_table + N''',''' + @gj_req_rem + N''') IS NOT NULL
                BEGIN
                    ;WITH GridTake AS (
                        SELECT ' + @key_select + N',
                               SUM(ISNULL(A.ROUND_SHIP,0)) AS grid_take
                          FROM [' + @alloc_table + N'] A
                          INNER JOIN [' + @working_table + N'] W2
                             ON A.WERKS=W2.WERKS AND A.MAJ_CAT=W2.MAJ_CAT
                            AND A.GEN_ART_NUMBER=W2.GEN_ART_NUMBER
                            AND ISNULL(A.CLR,'''') = ISNULL(W2.CLR,'''')
                         WHERE A.OPT_TYPE = @ot
                           AND A.OPT_PRIORITY_RANK = @rk
                           AND A.MAJ_CAT = @mc
                         GROUP BY ' + @group_by + N'
                        HAVING SUM(ISNULL(A.ROUND_SHIP,0)) > 0
                    )
                    UPDATE W SET W.[' + @gj_req_rem + N'] = CASE
                        WHEN ISNULL(W.[' + @gj_req_rem + N'],0) - G.grid_take < 0 THEN 0
                        ELSE ISNULL(W.[' + @gj_req_rem + N'],0) - G.grid_take END
                      FROM [' + @working_table + N'] W
                      INNER JOIN GridTake G ON ' + @join_cond + N'
                END';
            EXEC sp_executesql @sql,
                N'@ot NVARCHAR(10), @rk INT, @mc NVARCHAR(50)',
                @ot = @opt_type, @rk = @rank, @mc = @maj_cat;

            FETCH NEXT FROM gj_cur INTO @gj_req_rem, @gj_extras;
        END
        CLOSE gj_cur; DEALLOCATE gj_cur;
    END

    -- (3) H_<grid>_REM recompute
    IF LEN(@h_rem_sets) > 0
    BEGIN
        SET @sql = N'
            UPDATE [' + @working_table + N'] SET ' + @h_rem_sets + N'
             WHERE MAJ_CAT = @mc';
        EXEC sp_executesql @sql,
            N'@mc NVARCHAR(50)', @mc = @maj_cat;
    END

    -- (4) PRI_CT_REM recompute
    IF LEN(@h_sum) > 0 AND LEN(@gh_sum) > 0
    BEGIN
        SET @sql = N'
            UPDATE [' + @working_table + N'] SET
                PRI_CT_REM = CASE
                    WHEN (' + @gh_sum + N') = 0 THEN 0
                    ELSE ROUND(CAST((' + @h_sum + N') AS FLOAT) / (' + @gh_sum + N') * 100, 1)
                END
             WHERE MAJ_CAT = @mc';
        EXEC sp_executesql @sql, N'@mc NVARCHAR(50)', @mc = @maj_cat;
    END

    -- (5) Skip rules on remaining OPTs (rank > current band)
    SET @sql = N'
        UPDATE [' + @working_table + N'] SET
            ALLOC_STATUS = CASE
                WHEN ISNULL(MSA_FNL_Q_REM,0) <= 0 THEN ''SKIPPED''
                WHEN ISNULL(PRI_CT_REM,0) < 100
                     AND ISNULL(OPT_TYPE,'''') IN (' + @pri_opts + N') THEN ''SKIPPED''
                ELSE ALLOC_STATUS END,
            ALLOC_REMARKS = CASE
                WHEN ISNULL(MSA_FNL_Q_REM,0) <= 0
                    THEN ISNULL(ALLOC_REMARKS,'''') + '' SKIP_MSA_EXHAUSTED;''
                WHEN ISNULL(PRI_CT_REM,0) < 100
                     AND ISNULL(OPT_TYPE,'''') IN (' + @pri_opts + N')
                    THEN ISNULL(ALLOC_REMARKS,'''') + '' SKIP_PRI_BROKEN;''
                ELSE ALLOC_REMARKS END
         WHERE LISTED_FLAG = 1
           AND MAJ_CAT = @mc
           AND ISNULL(ALLOC_STATUS,''PENDING'') NOT IN (''SKIPPED'',''ALLOCATED'')
           AND OPT_PRIORITY_RANK > @rk';
    EXEC sp_executesql @sql,
        N'@mc NVARCHAR(50), @rk INT', @mc = @maj_cat, @rk = @rank;

    -- Store-broken: per opt_type within the maj_cat
    IF COL_LENGTH(@working_table, 'MJ_REQ_REM') IS NOT NULL
    BEGIN
        SET @sql = N'
            UPDATE [' + @working_table + N'] SET
                ALLOC_STATUS = ''SKIPPED'',
                ALLOC_REMARKS = ISNULL(ALLOC_REMARKS,'''') + '' SKIP_STORE_BROKEN;''
             WHERE LISTED_FLAG = 1
               AND MAJ_CAT = @mc
               AND ISNULL(ALLOC_STATUS,''PENDING'') NOT IN (''SKIPPED'',''ALLOCATED'')
               AND OPT_TYPE = @ot
               AND OPT_PRIORITY_RANK > @rk
               AND ISNULL(MJ_REQ_REM,0) < ' + CAST(@acs_skip_factor AS NVARCHAR(50)) + N' * ISNULL(ACS_D,0)';
        EXEC sp_executesql @sql,
            N'@mc NVARCHAR(50), @ot NVARCHAR(10), @rk INT',
            @mc = @maj_cat, @ot = @opt_type, @rk = @rank;
    END

    -- (6) Propagate SKIP to alloc_table
    SET @sql = N'
        UPDATE A SET
            A.ALLOC_STATUS = ''SKIPPED'',
            A.SKIP_REASON  = CASE
                WHEN A.SKIP_REASON IS NULL OR A.SKIP_REASON = ''''
                    THEN ''REVALIDATION_SKIP''
                ELSE A.SKIP_REASON END
          FROM [' + @alloc_table + N'] A
          INNER JOIN [' + @working_table + N'] W
             ON A.WERKS=W.WERKS AND A.MAJ_CAT=W.MAJ_CAT
            AND A.GEN_ART_NUMBER=W.GEN_ART_NUMBER
            AND ISNULL(A.CLR,'''') = ISNULL(W.CLR,'''')
         WHERE W.ALLOC_STATUS = ''SKIPPED''
           AND W.MAJ_CAT = @mc AND A.MAJ_CAT = @mc
           AND ISNULL(A.ALLOC_STATUS,''PENDING'') NOT IN (''SKIPPED'',''ALLOCATED'',''PARTIAL'')';
    EXEC sp_executesql @sql,
        N'@mc NVARCHAR(50)', @mc = @maj_cat;
END
GO
