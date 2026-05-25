/*
  stg_impect__championship_match_info
  -----------------------------------
  Per-match metadata for championship matches. Several columns are JSON
  serialized as VARCHAR (SQUADHOMEPLAYERSJSON etc.); we parse them to
  VARIANT in staging so downstream models can use LATERAL FLATTEN.
*/

with source as (
    select * from {{ source('impect', 'CHAMPIONSHIP_MATCH_INFO') }}
),

renamed as (
    select
        competitionid                                       as impect_competition_id,
        competitionname                                     as competition_name,
        season,
        iterationid                                         as iteration_id,
        matchid                                             as impect_match_id,
        try_to_timestamp_ntz(scheduleddate)                 as scheduled_at,
        try_to_timestamp_ntz(datetime)                      as match_datetime,
        try_to_timestamp_ntz(lastcalculationdate)           as last_calculation_at,

        stadiumid                                           as impect_stadium_id,
        squadhomeid                                         as home_squad_id,
        squadawayid                                         as away_squad_id,
        squadhomecoachid                                    as home_coach_id,
        squadawaycoachid                                    as away_coach_id,
        squadhomestartingformation                          as home_starting_formation,
        squadawaystartingformation                          as away_starting_formation,

        -- VARCHAR-encoded JSON -> VARIANT for downstream flattening.
        try_parse_json(squadhomeplayersjson)                as home_players,
        try_parse_json(squadawayplayersjson)                as away_players,
        try_parse_json(squadhomestartingpositionsjson)      as home_starting_positions,
        try_parse_json(squadawaystartingpositionsjson)      as away_starting_positions,
        try_parse_json(squadhomesubstitutionsjson)          as home_substitutions,
        try_parse_json(squadawaysubstitutionsjson)          as away_substitutions,
        try_parse_json(squadhomeformationsjson)             as home_formations,
        try_parse_json(squadawayformationsjson)             as away_formations
    from source
)

select * from renamed
