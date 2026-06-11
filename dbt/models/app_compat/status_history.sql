/*
  app_compat.status_history
  Generic status-change audit.
  App-owned legacy passthrough; bridged until Part 3 cutover (docs/decisions/0001).
*/

select * from {{ source('core_app', 'STATUS_HISTORY') }}
