-- IMPECT set-piece sub-phase classification (real corner/FK/throw-in type,
-- swing direction, first/second touch) from GET /matches/{id}/set-pieces --
-- replaces the geometry-derived approximation used before this table existed.
-- See docs/set-pieces-endpoint-proposal.md for the investigation behind this.

USE DATABASE CAFC_DB;

CREATE TABLE IF NOT EXISTS CAFC_DB.IMPECT_RAW.SET_PIECES (
  MATCH_ID                  NUMBER(38,0) NOT NULL,
  ITERATION_ID              NUMBER(38,0),
  SET_PIECE_ID               NUMBER(38,0) NOT NULL,       -- phase id ("id" above)
  PHASE_INDEX                 NUMBER(38,0),
  SQUAD_ID                     NUMBER(38,0),                -- attacking squad
  SET_PIECE_CATEGORY           VARCHAR(30),                 -- CORNER_LEFT/RIGHT, FREE_KICK, THROW_IN, GOAL_KICK
  ADJ_SET_PIECE_CATEGORY       VARCHAR(30),
  SET_PIECE_EXECUTION_TYPE     VARCHAR(30),                 -- DIRECT, INDIRECT
  START_TIME_IN_SEC            FLOAT,
  END_TIME_IN_SEC               FLOAT,
  SUB_PHASE_ID                  NUMBER(38,0) NOT NULL,
  SUB_PHASE_INDEX                NUMBER(38,0),
  START_ZONE                      VARCHAR(40),
  CORNER_END_ZONE                  VARCHAR(40),
  CORNER_TYPE                       VARCHAR(40),
  FREE_KICK_END_ZONE                 VARCHAR(40),
  FREE_KICK_TYPE                      VARCHAR(40),
  THROW_IN_END_ZONE                    VARCHAR(40),
  THROW_IN_TYPE                         VARCHAR(40),
  GOAL_KICK_END_ZONE                     VARCHAR(40),
  GOAL_KICK_TYPE                          VARCHAR(40),
  BALL_TRAJECTORY                          VARCHAR(20),      -- INSWINGING / OUTSWINGING / STRAIGHT
  MAIN_EVENT_PLAYER_ID                      NUMBER(38,0),
  MAIN_EVENT_OUTCOME                         VARCHAR(20),      -- SUCCESSFUL / UNSUCCESSFUL / UNKNOWN
  PASS_RECEIVER_ID                            NUMBER(38,0),
  FIRST_TOUCH_PLAYER_ID                        NUMBER(38,0),
  FIRST_TOUCH_WON                               BOOLEAN,
  INDIRECT_HEADER                                VARCHAR(10),  -- NONE/KNOCK_DOWN/FLICK_ON/DEFLECTION
  SECOND_TOUCH_PLAYER_ID                          NUMBER(38,0),
  SECOND_TOUCH_WON                                 BOOLEAN,
  SECOND_TOUCH_END_ZONE                             VARCHAR(40),
  AGGREGATES                                         VARIANT,   -- SHOT_XG/PACKING_XG/etc.
  RAW_PHASE                                           VARIANT,   -- full phase JSON, for anything missed above
  SOURCE_FORMAT                                        VARCHAR(30) NOT NULL DEFAULT 'SET_PIECES_API',
  LOADED_AT                                             TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
  INGESTION_RUN_ID                                       NUMBER(38,0)
)
CLUSTER BY (ITERATION_ID, MATCH_ID)
COMMENT = 'IMPECT set-piece sub-phase classification (corner/FK/throw-in type, swing direction, first/second touch) from GET /matches/{id}/set-pieces -- real labels, not the geometry-derived approximation used before this table existed.';
