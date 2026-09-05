/*
  app_compat.password_reset_tokens
  Single-use password reset tokens.
  App-owned legacy passthrough; bridged until Part 3 cutover (docs/decisions/0001).
*/

select * from {{ source('core_app', 'PASSWORD_RESET_TOKENS') }}
