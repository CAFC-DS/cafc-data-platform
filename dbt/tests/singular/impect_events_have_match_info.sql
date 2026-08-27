-- Every event-backed match needs a lineup timeline before participation can
-- be complete. The backfill is expected to make this return zero rows.
select distinct e.impect_match_id
from {{ ref('stg_impect__events') }} e
left join {{ ref('stg_impect__match_info') }} m using (impect_match_id)
where m.impect_match_id is null

