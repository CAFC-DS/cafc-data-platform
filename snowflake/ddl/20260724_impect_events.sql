-- ============================================================================
--  20260724_impect_events.sql
--  Raw landing for Impect's match event feed (GET /v5/customerapi/matches/
--  {matchId}/events), onboarded to replace Impect's pre-aggregated KPI/score
--  data as the analysis foundation with true event-level data.
--
--  Confirmed live against the real API before writing this DDL (per plan
--  §Phase 1 step 1 -- do not guess the shape):
--    - One row per on-ball event, ~2,000-3,800 events per match.
--    - Event-level data ("packing plus data" in Impect's own error message)
--      is NOT available for every match Impect has metadata for. Confirmed
--      empirically for competition 41 (EFL Championship, Charlton's
--      competition): seasons 17/18-20/21 return
--      400 {"message":"Match does not have packing plus data"} for every
--      match tried; seasons 21/22 onward return real event data. Do not
--      assume this depth is uniform across other competitions -- re-verify
--      per competition before promising history for a new one.
--    - Nested groups (start/end/duel/shot/pass/dribble/setPiece/pxT/
--      formation/opponent) are genuinely conditional per actionType (e.g.
--      `shot` is non-null only on shot events, `duel` only on duels) --
--      kept as VARIANT rather than flattened into ~100+ mostly-null columns,
--      per the platform's storage principle of not losing structure to
--      premature flattening.
--    - One table across every competition/season/iteration (existing
--      convention -- see IMPECT_RAW.MATCHES), not one table per competition.
--      MATCH_ID + ITERATION_ID are just columns; cluster on them so 10
--      years x many competitions worth of events still prune efficiently.
--
--  Idempotent: every statement is CREATE ... IF NOT EXISTS. Safe to re-run.
--  No grants to DATA_PLATFORM_ROLE: not provisioned in this account yet
--  (see [[data-platform-role-not-provisioned]]) -- DEV_ROLE owns this by
--  creation, same as every other schema in CAFC_DB today.
-- ============================================================================

USE DATABASE CAFC_DB;

CREATE TABLE IF NOT EXISTS CAFC_DB.IMPECT_RAW.EVENTS (
  MATCH_ID              NUMBER(38,0)  NOT NULL  COMMENT 'Impect match id -- FK to IMPECT_RAW.MATCHES.ID.',
  ITERATION_ID           NUMBER(38,0)           COMMENT 'Impect iteration id (competition-season) the match belongs to -- denormalized here for pruning/filtering without a join, same convention as other IMPECT_RAW tables.',
  EVENT_ID                NUMBER(38,0) NOT NULL  COMMENT 'Impect event id ("id" field) -- unique within a match, stable across re-pulls.',
  EVENT_INDEX               NUMBER(38,0)        COMMENT 'Zero-based position of this event within the match ("index" field).',
  SEQUENCE_INDEX              NUMBER(38,0)      COMMENT 'Position within the current possession sequence.',
  PERIOD_ID                     NUMBER(38,0)    COMMENT 'Half/period number (1 = first half, 2 = second half, etc.).',
  GAME_TIME                      VARCHAR(50)   COMMENT 'Clock display string, e.g. "21:14.5000" or, in stoppage time, "45:00.0000 (+00:02.2580)".',
  GAME_TIME_IN_SEC                 FLOAT       COMMENT 'Game clock in seconds -- the sortable/joinable form of GAME_TIME.',
  SQUAD_ID                          NUMBER(38,0) COMMENT 'Impect squad id of the team performing the event.',
  CURRENT_ATTACKING_SQUAD_ID          NUMBER(38,0) COMMENT 'Impect squad id currently in possession/attacking at this point in play (can differ from SQUAD_ID, e.g. on a defensive action).',
  PLAYER_ID                             NUMBER(38,0) COMMENT 'Impect player id of the event''s primary player -- FK to IMPECT_RAW.PLAYERS.ID.',
  PLAYER_POSITION                         VARCHAR(50) COMMENT 'Player''s position at the time of the event, e.g. "CENTER_FORWARD".',
  PLAYER_POSITION_SIDE                      VARCHAR(20) COMMENT 'Left/right/centre side qualifier for PLAYER_POSITION.',
  ACTION_TYPE                                 VARCHAR(50) COMMENT 'High-level event category, e.g. PASS, SHOT, DUEL.',
  ACTION                                        VARCHAR(100) COMMENT 'Specific action within ACTION_TYPE, e.g. MID_RANGE_SHOT, HEADER.',
  PHASE                                           VARCHAR(50) COMMENT 'Phase of play, e.g. SECOND_BALL, ATTACKING_TRANSITION.',
  BODY_PART                                         VARCHAR(30) COMMENT 'Body part used, e.g. FOOT_LEFT, HEAD.',
  BODY_PART_EXTENDED                                  VARCHAR(30) COMMENT 'More specific body-part qualifier.',
  PREVIOUS_PASS_HEIGHT                                  VARCHAR(30) COMMENT 'Height qualifier of the pass immediately preceding this event, where applicable.',
  DURATION                                                FLOAT   COMMENT 'Event duration in seconds.',
  OPPONENTS                                                 NUMBER(38,0) COMMENT 'Count of opponents Impect considers relevant to this event (used in their packing/pxT model).',
  PRESSURE                                                    FLOAT COMMENT 'Impect''s pressure metric on the event.',
  DISTANCE_TO_GOAL                                              FLOAT COMMENT 'Distance to goal in metres at the event''s start position.',
  DISTANCE_TO_OPPONENT                                            VARCHAR(30) COMMENT 'Bucketed distance-to-nearest-opponent qualifier.',
  RESULT                                                             VARCHAR(20) COMMENT 'Outcome of the event, e.g. SUCCESS, FAIL.',
  PRESSING_PLAYER_ID                                                   NUMBER(38,0) COMMENT 'Player id applying pressure, if any.',
  FOULED_PLAYER_ID                                                       NUMBER(38,0) COMMENT 'Player id fouled, if this event is a foul.',
  INFERRED_SET_PIECE                                                       BOOLEAN COMMENT 'Whether Impect inferred (rather than directly tagged) this as a set piece.',
  START_DETAIL          VARIANT  COMMENT 'Nested "start" object: coordinates, adjCoordinates, packingZone, pitchPosition, lane.',
  END_DETAIL            VARIANT  COMMENT 'Nested "end" object: same shape as START_DETAIL for the event''s end position.',
  DUEL_DETAIL           VARIANT  COMMENT 'Nested "duel" object -- non-null only on duel events.',
  SHOT_DETAIL           VARIANT  COMMENT 'Nested "shot" object (distance, angle, targetPoint, gk, woodwork) -- non-null only on shot events.',
  PASS_DETAIL           VARIANT  COMMENT 'Nested "pass" object (distance, angle, receiver) -- non-null only on pass events.',
  DRIBBLE_DETAIL        VARIANT  COMMENT 'Nested "dribble" object -- non-null only on dribble events.',
  SET_PIECE_DETAIL      VARIANT  COMMENT 'Nested "setPiece" object -- non-null only on set-piece events.',
  PXT_DETAIL            VARIANT  COMMENT 'Nested "pxT" object: {team, opponent} -- Impect''s possession-value-added metric for this event.',
  FORMATION_DETAIL      VARIANT  COMMENT 'Nested "formation" object: {team, opponent} formation strings at the time of the event.',
  OPPONENT_DETAIL       VARIANT  COMMENT 'Nested "opponent" object: nearest-opponent coordinates at the event''s start.',
  EVENT_KPIS            VARIANT  COMMENT 'Array of {position, playerId, <kpiName>: value, ...} objects from GET /matches/{id}/event-kpis (a SEPARATE endpoint from the plain event payload, resolved against GET /kpis/event for kpiId->name). This is where event-level xG lives (SHOT_XG, PACKING_XG, POSTSHOT_XG) plus the full PXT_* family -- confirmed live 2026-07-30 that none of this exists on the base event JSON at all. One event typically has ~10 rows here (the primary player plus every other on-pitch player''s defensive/positional attribution for that same event), so this is an array, not a flat object -- filter by playerId to get one player''s view.',
  RAW_EVENT             VARIANT  NOT NULL  COMMENT 'Full verbatim event JSON as returned by the API -- the replay buffer, in case a future field is needed that wasn''t worth a dedicated column at onboarding time.',
  LOADED_AT             TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()  COMMENT 'When this row was landed.',
  INGESTION_RUN_ID       NUMBER(38,0)  COMMENT 'FK to CORE.INGESTION_RUNS for the extractor run that landed this row.'
)
CLUSTER BY (ITERATION_ID, MATCH_ID)
COMMENT = 'Raw Impect match events, one row per event per match, landed verbatim from GET /v5/customerapi/matches/{matchId}/events. Append-only; MATCH_ID+EVENT_ID is the natural key, deduped by the loader (not by a table constraint) before insert.';
