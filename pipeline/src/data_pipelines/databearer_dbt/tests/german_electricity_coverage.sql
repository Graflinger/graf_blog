{{ config(enabled=(target.name == 'dashboard'), tags=['german_electricity']) }}

{% set series = ['biomass', 'hydro', 'wind_offshore', 'wind_onshore', 'solar',
                 'other_renewables', 'lignite', 'hard_coal', 'gas',
                 'other_conventional', 'pumped_storage', 'load', 'price'] %}

with expected as (
    select hour_ms
    from {{ source('smard', 'smard_window') }},
         lateral range(start_ms, end_ms, 3600000) as hours(hour_ms)
), actual as (
    select * from {{ ref('german_electricity_hourly') }}
)
select expected.hour_ms, actual.timestamp
from expected
full outer join actual on expected.hour_ms = actual.timestamp
where expected.hour_ms is null or actual.timestamp is null
{% for column in series %}
    or not isfinite(actual.{{ column }})
    or actual.{{ column }} < {{ -10000 if column == 'price' else 0 }}
    or actual.{{ column }} > {{ 10000 if column == 'price' else 200 }}
{% endfor %}
