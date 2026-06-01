/*
  app_compat.users
  ----------------
  Recruitment-platform application users. App-owned; surfaced verbatim from
  CORE.USERS (where this table already lives). Passthrough.
*/

select * from {{ source('core', 'USERS') }}
