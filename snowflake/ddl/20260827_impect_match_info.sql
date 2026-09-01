-- Generic match metadata used to derive player participation independently of
-- Impect's retiring pre-aggregated player KPI products.

USE DATABASE CAFC_DB;

CREATE TABLE IF NOT EXISTS CAFC_DB.IMPECT_RAW.MATCH_INFO (
  MATCH_ID                    NUMBER(38,0) NOT NULL,
  ITERATION_ID                NUMBER(38,0),
  MATCH_DATETIME              TIMESTAMP_NTZ,
  SOURCE_LAST_CALCULATION_AT  TIMESTAMP_NTZ,
  HOME_SQUAD_ID               NUMBER(38,0),
  AWAY_SQUAD_ID               NUMBER(38,0),
  HOME_PLAYERS                VARIANT,
  AWAY_PLAYERS                VARIANT,
  HOME_STARTING_POSITIONS     VARIANT,
  AWAY_STARTING_POSITIONS     VARIANT,
  HOME_SUBSTITUTIONS          VARIANT,
  AWAY_SUBSTITUTIONS          VARIANT,
  HOME_FORMATIONS             VARIANT,
  AWAY_FORMATIONS             VARIANT,
  RAW_MATCH_INFO              VARIANT,
  SOURCE_FORMAT               VARCHAR(30) NOT NULL DEFAULT 'MATCH_API',
  LOADED_AT                   TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
  INGESTION_RUN_ID            NUMBER(38,0)
)
CLUSTER BY (ITERATION_ID, MATCH_ID)
COMMENT = 'Generic Impect match metadata from GET /matches/{id}. Starting positions and substitution timelines are the source for dbt player-participation models.';

