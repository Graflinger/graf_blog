{{ config(enabled=(target.name == 'dashboard'), tags=['german_electricity']) }}

-- Enforce uniqueness before the curated pivot could hide duplicate observations.
select series, timestamp_ms
from {{ ref('smard_hourly_cleaned') }}
group by series, timestamp_ms
having count(*) <> 1
    or min(timestamp_ms) % 3600000 <> 0
    or not bool_and(value is null or isfinite(value))
    or min(value) < case when series = 'price' then -10000 else 0 end
    or max(value) > case when series = 'price' then 10000 else 200 end

union all

-- Every series/hour slot is required, even when its value is explicitly null.
select series, timestamp_ms
from (
    select unnest(['biomass', 'hydro', 'wind_offshore', 'wind_onshore', 'solar',
                   'other_renewables', 'lignite', 'hard_coal', 'gas',
                   'other_conventional', 'pumped_storage', 'load', 'price']) as series
) series
cross join {{ source('smard', 'smard_window') }}
cross join lateral range(start_ms, end_ms, 3600000) as hours(timestamp_ms)
anti join {{ ref('smard_hourly_cleaned') }} actual using (series, timestamp_ms)
