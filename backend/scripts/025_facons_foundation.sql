/* ============================================================================
   025_facons_foundation.sql
   FA & CONS data foundation — per-stream SLOC selection, MBQ master, and the
   persisted FA/CONS MSA + store-stock calc result tables.

   FA  = Project-Store Allocation (new / UPC project stores)
   CONS = Consumables Allocation (pan-India)

   Design notes (see frontend/public/docs/manual/fa_cons.md):
   • Both the "store-stock" and "MSA / DC-pool" totals are sourced from the SAME
     stock view VW_ET_MSA_STK_WITH_MASTER (the SOP's "MRST_FA & CO" plant stock),
     split purely by the per-stream, per-scope SLOC selection below. ALL segments
     are considered (no SEG='APP'/'GM' filter) — an FA/CONS revised requirement.
   • The SOP's "0001,0002,INT,PRD,STO" are conceptual labels, not literal SLOC
     codes. Real SLOCs (0014, 0099, HUB_INTRA, HUB_PRD, ST_PRD, V01, V02_FRESH,
     V02_GRT, V06, V07, …) are DISCOVERED live via the Sync action. Only the
     certain default (V02_FRESH → MSA scope, active) is seeded; the planner
     activates the store-scope SLOCs and labels them (INT=HUB_INTRA, etc.).

   Runs on the DATA DB (Rep_data). The services also create these lazily
   (services/facons_*_service.py :: ensure_tables), so this script is for
   documentation / explicit provisioning. Idempotent — safe to re-run.
   ============================================================================ */

-- ── Per-stream, per-scope SLOC selection ────────────────────────────────────
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='ARS_FACONS_SLOC_SETTINGS')
BEGIN
    CREATE TABLE ARS_FACONS_SLOC_SETTINGS (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        stream      NVARCHAR(10)  NOT NULL,   -- 'FA' | 'CONS'
        scope       NVARCHAR(10)  NOT NULL,   -- 'STORE' (store-stock) | 'MSA' (DC pool)
        sloc        NVARCHAR(50)  NOT NULL,
        kpi         NVARCHAR(200) NULL,       -- free-text label (e.g. INT / PRD / STO)
        is_active   BIT           NOT NULL DEFAULT 0,
        created_at  DATETIME      NOT NULL DEFAULT GETDATE(),
        updated_at  DATETIME      NOT NULL DEFAULT GETDATE(),
        updated_by  NVARCHAR(100) NULL,
        CONSTRAINT CK_FACONS_SLOC_stream CHECK (stream IN ('FA','CONS')),
        CONSTRAINT CK_FACONS_SLOC_scope  CHECK (scope  IN ('STORE','MSA')),
        CONSTRAINT UQ_FACONS_SLOC UNIQUE (stream, scope, sloc)
    );
END

-- ── MBQ master (upload only ST_CD + REF_ART + MBQ_Q; rest resolved/blank) ────
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='ARS_FACONS_MBQ')
BEGIN
    CREATE TABLE ARS_FACONS_MBQ (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        stream      NVARCHAR(10)  NOT NULL,          -- 'FA' | 'CONS'
        st_cd       NVARCHAR(20)  NOT NULL,          -- store
        ref_art     NVARCHAR(30)  NOT NULL,          -- reference article (GEN_ART_NUMBER)
        clr         NVARCHAR(20)  NOT NULL DEFAULT '',-- '' = all colours (kept non-null for clean uniqueness)
        maj_cat     NVARCHAR(50)  NULL,
        opt_type    NVARCHAR(10)  NULL,
        mbq_q       FLOAT         NOT NULL DEFAULT 0,
        priority    INT           NULL,              -- FA priority-wise dispatch rank
        cover_days  INT           NULL,              -- CONS cover days
        reason      NVARCHAR(500) NULL,              -- mandatory reason on change (revised SOP)
        source      NVARCHAR(20)  NOT NULL DEFAULT 'manual',
        eff_from    DATE          NULL,
        eff_to      DATE          NULL,
        created_at  DATETIME      NOT NULL DEFAULT GETDATE(),
        updated_at  DATETIME      NOT NULL DEFAULT GETDATE(),
        updated_by  NVARCHAR(100) NULL,
        CONSTRAINT CK_FACONS_MBQ_stream CHECK (stream IN ('FA','CONS')),
        CONSTRAINT UQ_FACONS_MBQ UNIQUE (stream, st_cd, ref_art, clr)
    );
END

-- ── Run record for the FA/CONS stock calc (like MSA_Calculation_Sequence) ────
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='ARS_FACONS_STOCK_SEQUENCE')
BEGIN
    CREATE TABLE ARS_FACONS_STOCK_SEQUENCE (
        sequence_id  INT IDENTITY(1,1) PRIMARY KEY,
        stream       NVARCHAR(10)  NOT NULL,
        calc_date    DATE          NULL,           -- stock date used (NULL = latest)
        slocs_store  NVARCHAR(MAX) NULL,           -- JSON: active STORE-scope SLOCs
        slocs_msa    NVARCHAR(MAX) NULL,           -- JSON: active MSA-scope SLOCs
        store_rows   INT           NOT NULL DEFAULT 0,
        msa_rows     INT           NOT NULL DEFAULT 0,
        status       NVARCHAR(20)  NOT NULL DEFAULT 'COMPLETED',
        created_by   NVARCHAR(100) NULL,
        created_at   DATETIME      NOT NULL DEFAULT GETDATE()
    );
END

-- ── Store-stock result (scope=STORE): total per store × ref-art × colour ─────
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='ARS_FACONS_STOCK')
BEGIN
    CREATE TABLE ARS_FACONS_STOCK (
        id            INT IDENTITY(1,1) PRIMARY KEY,
        sequence_id   INT           NOT NULL,
        stream        NVARCHAR(10)  NOT NULL,
        st_cd         NVARCHAR(20)  NULL,       -- store holding the stock
        maj_cat       NVARCHAR(50)  NULL,
        ref_art       NVARCHAR(30)  NULL,       -- GEN_ART_NUMBER
        clr           NVARCHAR(20)  NULL,
        store_stk_ttl FLOAT         NOT NULL DEFAULT 0,
        created_at    DATETIME      NOT NULL DEFAULT GETDATE()
    );
    CREATE INDEX IX_FACONS_STOCK_seq ON ARS_FACONS_STOCK (stream, sequence_id);
    CREATE INDEX IX_FACONS_STOCK_key ON ARS_FACONS_STOCK (stream, st_cd, ref_art);
END

-- ── MSA / DC-pool result (scope=MSA): available per RDC × ref-art × colour ───
IF NOT EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME='ARS_FACONS_MSA')
BEGIN
    CREATE TABLE ARS_FACONS_MSA (
        id          INT IDENTITY(1,1) PRIMARY KEY,
        sequence_id INT           NOT NULL,
        stream      NVARCHAR(10)  NOT NULL,
        rdc         NVARCHAR(20)  NULL,        -- DC / warehouse holding the pool
        maj_cat     NVARCHAR(50)  NULL,
        ref_art     NVARCHAR(30)  NULL,        -- GEN_ART_NUMBER
        clr         NVARCHAR(20)  NULL,
        dc_stk_ttl  FLOAT         NOT NULL DEFAULT 0,
        created_at  DATETIME      NOT NULL DEFAULT GETDATE()
    );
    CREATE INDEX IX_FACONS_MSA_seq ON ARS_FACONS_MSA (stream, sequence_id);
    CREATE INDEX IX_FACONS_MSA_key ON ARS_FACONS_MSA (stream, rdc, ref_art);
END

-- ── Seed the one certain default: V02_FRESH → MSA scope, active, both streams ─
MERGE ARS_FACONS_SLOC_SETTINGS AS tgt
USING (VALUES
        ('FA','MSA','V02_FRESH','STK',1),
        ('CONS','MSA','V02_FRESH','STK',1)
      ) AS src(stream, scope, sloc, kpi, is_active)
ON  tgt.stream = src.stream AND tgt.scope = src.scope AND tgt.sloc = src.sloc
WHEN NOT MATCHED THEN
    INSERT (stream, scope, sloc, kpi, is_active, updated_by)
    VALUES (src.stream, src.scope, src.sloc, src.kpi, src.is_active, 'seed');
