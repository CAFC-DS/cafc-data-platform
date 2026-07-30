-- 20260730_impect_event_kpis.sql
-- Add EVENT_KPIS to IMPECT_RAW.EVENTS: event-level xG (SHOT_XG, PACKING_XG,
-- POSTSHOT_XG) and the full PXT_* metric family are NOT present on the plain
-- event payload at all -- confirmed live 2026-07-30 they come from a
-- separate endpoint, GET /matches/{id}/event-kpis (long format:
-- eventId/position/playerId/kpiId/value), resolved against GET /kpis/event
-- for the kpiId->name mapping (103 KPIs total). This mirrors exactly how
-- Impect's own official Python SDK (impectPy.getEvents(include_kpis=True))
-- builds its wide event table, and is where the older CAFC_TEST_ANALYSIS
-- ad-hoc table's SHOT_XG column actually came from.
--
-- Stored as an array VARIANT (one object per (eventId, position, playerId)
-- combination), not a flat dict -- confirmed live that one event typically
-- carries ~10 rows here (the primary player plus every other on-pitch
-- player's attribution for that same event, e.g. every outfield player
-- gets a DEF_PXT_SHOT row for one shot). Filter by playerId to get one
-- player's view of an event.
--
-- Re-runnable. Already applied to prod (verified via DESCRIBE TABLE); this
-- migration file documents it for anyone re-creating the table from scratch.
-- python/extract/impect/load_match_events.py --backfill-kpis attaches this
-- to already-loaded matches without re-inserting event rows.

ALTER TABLE CAFC_DB.IMPECT_RAW.EVENTS
  ADD COLUMN IF NOT EXISTS EVENT_KPIS VARIANT
  COMMENT 'Array of {position, playerId, <kpiName>: value, ...} objects from GET /matches/{id}/event-kpis (a SEPARATE endpoint from the plain event payload, resolved against GET /kpis/event for kpiId->name). This is where event-level xG lives (SHOT_XG, PACKING_XG, POSTSHOT_XG) plus the full PXT_* family -- confirmed live 2026-07-30 that none of this exists on the base event JSON at all. One event typically has ~10 rows here (the primary player plus every other on-pitch player''s defensive/positional attribution for that same event), so this is an array, not a flat object -- filter by playerId to get one player''s view.';
