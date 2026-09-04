/*
  app_compat.agent_profiles
  Agent contact profiles.
  App-owned legacy passthrough; bridged until Part 3 cutover (docs/decisions/0001).
*/

select * from {{ source('core_app', 'AGENT_PROFILES') }}
