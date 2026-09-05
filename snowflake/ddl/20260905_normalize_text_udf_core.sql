-- ============================================================================
--  20260905_normalize_text_udf_core.sql
--  Create NORMALIZE_TEXT_UDF in CAFC_DB.CORE.
--
--  Found by the 2026-09-05 full-cutover rehearsal: the app calls
--  NORMALIZE_TEXT_UDF UNQUALIFIED (~40 call sites) so it resolves against
--  the connection's default schema. Post-cutover that's CAFC_DB.CORE, where
--  the UDF has never existed -- only RECRUITMENT_TEST.PUBLIC (legacy) and
--  CAFC_DB.APP (the earlier lift-and-shift clone) have it. Every ILIKE /
--  fuzzy-search endpoint failed with "Unknown function NORMALIZE_TEXT_UDF"
--  until this ran. Re-run at the real cutover (idempotent — CREATE OR
--  REPLACE) in case CORE was rebuilt since.
-- ============================================================================

CREATE OR REPLACE FUNCTION CAFC_DB.CORE.NORMALIZE_TEXT_UDF("TEXT" VARCHAR)
RETURNS VARCHAR
LANGUAGE JAVASCRIPT
AS '
  if (!TEXT) return \'\';
  var normalized = TEXT.normalize(\'NFD\');
  var result = normalized.replace(/[\\u0300-\\u036f]/g, \'\');
  return result.toLowerCase();
';

GRANT USAGE ON FUNCTION CAFC_DB.CORE.NORMALIZE_TEXT_UDF(VARCHAR) TO ROLE APP_ROLE;

-- Verify:
--   SELECT CAFC_DB.CORE.NORMALIZE_TEXT_UDF('Müller');  -- expect 'muller'
