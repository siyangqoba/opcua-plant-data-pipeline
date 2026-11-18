-- Analytics layer. Shift pattern: A 06:00-14:00, B 14:00-22:00, C 22:00-06:00 (plant local time).

CREATE OR REPLACE VIEW plant.v_readings AS
SELECT t.name AS tag, t.equipment, t.unit, r.ts, r.value, r.quality
FROM plant.readings r JOIN plant.tags t USING (tag_id);

-- One-minute averages for analogue tags (charts, alarm detection).
CREATE OR REPLACE VIEW plant.v_minute AS
SELECT t.name AS tag, t.equipment, t.unit,
       date_trunc('minute', r.ts) AS minute,
       avg(r.value) AS avg_value, min(r.value) AS min_value, max(r.value) AS max_value,
       count(*) AS samples
FROM plant.readings r JOIN plant.tags t USING (tag_id)
WHERE r.quality = 0
  AND t.name NOT LIKE '%.State' AND t.name NOT LIKE '%.FaultCode' AND t.name NOT LIKE '%.Running'
GROUP BY t.name, t.equipment, t.unit, date_trunc('minute', r.ts);

-- Equipment state intervals from the State tag (stored on change), with the fault code
-- that was active when the interval started.
CREATE OR REPLACE VIEW plant.v_state_intervals AS
WITH raw AS (
    SELECT t.equipment, r.ts, r.value::int AS state,
           lag(r.value::int) OVER (PARTITION BY t.equipment ORDER BY r.ts) AS prev_state
    FROM plant.readings r JOIN plant.tags t USING (tag_id)
    WHERE t.name LIKE '%.State'
), changes AS (   -- heartbeats repeat the same state: keep only real transitions
    SELECT equipment, ts, state FROM raw
    WHERE prev_state IS DISTINCT FROM state
), states AS (
    SELECT equipment, ts AS start_ts, state,
           lead(ts) OVER (PARTITION BY equipment ORDER BY ts) AS next_ts
    FROM changes
), latest AS (
    SELECT max(ts) AS last_ts FROM plant.readings
)
SELECT s.equipment, s.state,
       CASE s.state WHEN 0 THEN 'Stopped' WHEN 1 THEN 'Starting' WHEN 2 THEN 'Running' ELSE 'Faulted' END AS state_name,
       s.start_ts,
       COALESCE(s.next_ts, l.last_ts) AS end_ts,
       COALESCE(f.fault_code, 0) AS fault_code
FROM states s
CROSS JOIN latest l
LEFT JOIN LATERAL (
    SELECT r.value::int AS fault_code
    FROM plant.readings r JOIN plant.tags t USING (tag_id)
    WHERE t.name = s.equipment || '.FaultCode' AND r.ts <= s.start_ts
    ORDER BY r.ts DESC LIMIT 1
) f ON true
WHERE COALESCE(s.next_ts, l.last_ts) > s.start_ts;

-- Downtime events: faulted periods, plus stops that follow them, tagged with a reason.
CREATE OR REPLACE VIEW plant.v_downtime_events AS
SELECT i.equipment, i.start_ts, i.end_ts,
       round(extract(epoch FROM i.end_ts - i.start_ts) / 60.0, 1) AS minutes,
       i.state_name, i.fault_code,
       COALESCE(fc.description, 'Unknown fault ' || i.fault_code) AS reason,
       COALESCE(fc.category, 'Unknown') AS category
FROM plant.v_state_intervals i
LEFT JOIN plant.fault_codes fc ON fc.code = i.fault_code
WHERE i.state = 3;

-- Shift windows covering the data, in plant local time.
CREATE OR REPLACE FUNCTION plant.shift_windows(tz text DEFAULT 'Africa/Johannesburg')
RETURNS TABLE (shift_date date, shift text, shift_start timestamptz, shift_end timestamptz)
LANGUAGE sql STABLE AS $$
    WITH bounds AS (
        SELECT min(ts) AS t0, max(ts) AS t1 FROM plant.readings
    ), starts AS (
        SELECT g AS local_start
        FROM bounds,
             generate_series(date_trunc('day', (t0 AT TIME ZONE tz)) - interval '2 hours',
                             (t1 AT TIME ZONE tz), interval '8 hours') AS g
    )
    SELECT CASE WHEN extract(hour FROM local_start) = 22 THEN (local_start + interval '1 day')::date
                ELSE local_start::date END,
           CASE extract(hour FROM local_start) WHEN 6 THEN 'A' WHEN 14 THEN 'B' ELSE 'C' END,
           local_start AT TIME ZONE tz,
           (local_start + interval '8 hours') AT TIME ZONE tz
    FROM starts
$$;

-- Availability, tonnes and downtime per equipment per shift.
CREATE OR REPLACE VIEW plant.v_shift_kpis AS
WITH w AS (SELECT * FROM plant.shift_windows()),
covered AS (   -- portion of each shift that actually has data
    SELECT w.*, greatest(w.shift_start, (SELECT min(ts) FROM plant.readings)) AS data_start,
                least(w.shift_end, (SELECT max(ts) FROM plant.readings)) AS data_end
    FROM w
),
overlap AS (
    SELECT c.shift_date, c.shift, c.shift_start, c.shift_end, i.equipment, i.state,
           extract(epoch FROM least(i.end_ts, c.data_end) - greatest(i.start_ts, c.data_start)) AS seconds
    FROM covered c
    JOIN plant.v_state_intervals i ON i.start_ts < c.data_end AND i.end_ts > c.data_start
),
per_state AS (
    SELECT shift_date, shift, shift_start, shift_end, equipment,
           sum(seconds) FILTER (WHERE state = 2) AS run_s,
           sum(seconds) FILTER (WHERE state = 3) AS fault_s,
           sum(seconds) FILTER (WHERE state IN (0, 1)) AS stopped_s,
           sum(seconds) AS covered_s
    FROM overlap GROUP BY shift_date, shift, shift_start, shift_end, equipment
),
tonnes AS (
    SELECT c.shift_date, c.shift,
           max(r.value) - min(r.value) AS tonnes
    FROM covered c
    JOIN plant.readings r ON r.ts >= c.data_start AND r.ts <= c.data_end
    JOIN plant.tags t ON t.tag_id = r.tag_id AND t.name = 'CV101.TotalTonnes'
    GROUP BY c.shift_date, c.shift
)
SELECT p.shift_date, p.shift, p.shift_start, p.shift_end, p.equipment,
       round((p.covered_s / 3600.0)::numeric, 2) AS hours_covered,
       round((COALESCE(p.run_s, 0) / 3600.0)::numeric, 2) AS run_hours,
       round((COALESCE(p.fault_s, 0) / 60.0)::numeric, 1) AS fault_minutes,
       round((COALESCE(p.stopped_s, 0) / 60.0)::numeric, 1) AS stopped_minutes,
       round((COALESCE(p.run_s, 0) / NULLIF(p.covered_s, 0))::numeric, 4) AS availability,
       round(t.tonnes::numeric, 1) AS tonnes
FROM per_state p
LEFT JOIN tonnes t ON t.shift_date = p.shift_date AND t.shift = p.shift;

-- High-limit alarms on one-minute averages (smoothing avoids chatter on noisy signals).
CREATE OR REPLACE VIEW plant.v_alarm_events AS
WITH m AS (
    SELECT v.tag, v.minute, v.avg_value, a.high, a.high_high,
           lag(v.avg_value) OVER (PARTITION BY v.tag ORDER BY v.minute) AS prev_value
    FROM plant.v_minute v JOIN plant.alarm_limits a ON a.tag = v.tag
)
SELECT tag, minute AS raised_at, round(avg_value::numeric, 2) AS value,
       CASE WHEN high_high IS NOT NULL AND avg_value > high_high THEN 'HIGH-HIGH' ELSE 'HIGH' END AS severity,
       high, high_high
FROM m
WHERE avg_value > high AND (prev_value IS NULL OR prev_value <= high);
