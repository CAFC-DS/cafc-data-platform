/*
  app_compat.users
  ----------------
  Application users / auth. App-owned passthrough of the LIVE table
  RECRUITMENT_TEST.PUBLIC.USERS (159 rows) — authoritative over CORE.USERS,
  which is a stale 51-row migration copy. The prior view read CORE.USERS and so
  served stale auth data. Bridged until the Part 3 cutover.
*/

select * from {{ source('recruitment_legacy', 'USERS') }}
