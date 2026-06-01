/*
  app_compat.notifications
  In-app user notifications.
  App-owned legacy passthrough; bridged until Part 3 cutover (docs/decisions/0001).
*/

select * from {{ source('recruitment_legacy', 'NOTIFICATIONS') }}
