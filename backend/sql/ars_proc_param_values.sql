/* ============================================================================
   ARS_PROC_PARAM_VALUES — generic registry of allowed values for stored-proc
   parameters, so the Report Generation params editor can show a dropdown.

   One row per allowed value.  PROC_NAME is the proc WITHOUT schema; PARAM_NAME
   is the parameter WITHOUT the leading @.  Register any param's dropdown by
   inserting rows here — the /report-gen/proc-params endpoint attaches them.
   ============================================================================ */
IF OBJECT_ID('dbo.ARS_PROC_PARAM_VALUES') IS NULL
BEGIN
    CREATE TABLE dbo.ARS_PROC_PARAM_VALUES (
        PROC_NAME  SYSNAME       NOT NULL,
        PARAM_NAME SYSNAME       NOT NULL,
        VAL        NVARCHAR(200) NOT NULL,
        LABEL      NVARCHAR(300) NULL,
        SORT_ORDER INT           NOT NULL CONSTRAINT DF_APPV_SORT DEFAULT (0),
        -- FANOUT=1: when this param is given a comma list, the Report Generation
        -- engine runs the proc once PER value and writes a SEPARATE output file
        -- suffixed with the value (e.g. MSA_OP_CL_DETAIL, MSA_OP_CL_GEN_CLR).
        FANOUT     BIT           NOT NULL CONSTRAINT DF_APPV_FANOUT DEFAULT (0),
        CONSTRAINT PK_ARS_PROC_PARAM_VALUES PRIMARY KEY (PROC_NAME, PARAM_NAME, VAL)
    );
END
ELSE IF COL_LENGTH('dbo.ARS_PROC_PARAM_VALUES', 'FANOUT') IS NULL
    ALTER TABLE dbo.ARS_PROC_PARAM_VALUES
        ADD FANOUT BIT NOT NULL CONSTRAINT DF_APPV_FANOUT DEFAULT (0);
GO

-- Seed / refresh @Level values for usp_ars_msa_master from its level catalogue.
DELETE FROM dbo.ARS_PROC_PARAM_VALUES
WHERE PROC_NAME = 'usp_ars_msa_master' AND PARAM_NAME = 'Level';

INSERT INTO dbo.ARS_PROC_PARAM_VALUES (PROC_NAME, PARAM_NAME, VAL, LABEL, SORT_ORDER, FANOUT)
SELECT 'usp_ars_msa_master', 'Level', LEVEL_CODE,
       LEVEL_CODE + N' — ' + LEVEL_NAME, SORT_ORDER, 1   -- FANOUT: one file per level
FROM dbo.vw_ars_msa_master_levels;
GO
