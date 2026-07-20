-- =============================================================================
-- Migration 021: Manual store priority override
-- Purpose: Add MANUAL_ST_PRIORITY to the store master (Master_ALC_INPUT_ST_MASTER)
--          so ops can pin a store's ST_RANK during Listing (Part 6 store ranking).
--
-- Semantics (listing.md FSD — store ranking):
--   NULL / 0 / negative  → no manual pin; store is ranked by W_SCORE as usual.
--   Positive integer P   → the store is pinned to ST_RANK = P inside EVERY MAJ_CAT
--                          it is listed in. Non-manual stores keep their score
--                          order but shift into the smallest rank numbers NOT
--                          occupied by a pinned store (literal + skip-used-slots).
--
-- The Listing run also self-heals this column (ensure-on-run), but this migration
-- lets ops set priorities in the store-master table editor BEFORE the first run.
-- Idempotent: safe to re-run.
-- =============================================================================

IF EXISTS (
    SELECT 1 FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_NAME = 'Master_ALC_INPUT_ST_MASTER'
)
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'Master_ALC_INPUT_ST_MASTER'
          AND COLUMN_NAME = 'MANUAL_ST_PRIORITY'
    )
    BEGIN
        ALTER TABLE Master_ALC_INPUT_ST_MASTER ADD MANUAL_ST_PRIORITY INT NULL;
        PRINT 'Column MANUAL_ST_PRIORITY added to Master_ALC_INPUT_ST_MASTER.';
    END
    ELSE
        PRINT 'MANUAL_ST_PRIORITY already exists on Master_ALC_INPUT_ST_MASTER. Skipping.';
END
ELSE
    PRINT 'Master_ALC_INPUT_ST_MASTER not found; MANUAL_ST_PRIORITY will be self-healed on the next Listing run.';
GO
