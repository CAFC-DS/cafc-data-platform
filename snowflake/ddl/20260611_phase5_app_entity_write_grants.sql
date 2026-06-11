-- ============================================================================
-- Phase 5: grants for app-minted players and matches (run at cutover, after
-- the Phase 3 clone script, with a role that owns CAFC_DB.CORE — DEV_ROLE
-- today).
--
-- From the full-cutover state the recruitment backend mints manual players
-- and matches straight into the canonical entity tables (mirroring
-- python/identity/mint.py): sequence NEXTVAL + entity row + a MANUAL identity
-- row. INSERT only — the app never UPDATEs or deletes canonical entities;
-- corrections go through merge.py / identity overrides.
-- ============================================================================

GRANT SELECT, INSERT ON CAFC_DB.CORE.PLAYERS            TO ROLE APP_ROLE;
GRANT SELECT, INSERT ON CAFC_DB.CORE.PLAYER_IDENTITIES  TO ROLE APP_ROLE;
GRANT SELECT, INSERT ON CAFC_DB.CORE.FIXTURES           TO ROLE APP_ROLE;
GRANT SELECT, INSERT ON CAFC_DB.CORE.FIXTURE_IDENTITIES TO ROLE APP_ROLE;

GRANT USAGE ON SEQUENCE CAFC_DB.CORE.CAFC_PLAYER_ID_SEQ  TO ROLE APP_ROLE;
GRANT USAGE ON SEQUENCE CAFC_DB.CORE.CAFC_FIXTURE_ID_SEQ TO ROLE APP_ROLE;
