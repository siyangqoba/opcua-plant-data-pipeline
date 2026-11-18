-- Idempotent: safe to run on every collector start.
CREATE SCHEMA IF NOT EXISTS plant;

CREATE TABLE IF NOT EXISTS plant.tags (
    tag_id      serial PRIMARY KEY,
    name        text NOT NULL UNIQUE,          -- e.g. CR101.MotorCurrent
    equipment   text NOT NULL,
    unit        text NOT NULL DEFAULT '',
    description text NOT NULL DEFAULT '',
    deadband    double precision NOT NULL DEFAULT 0
);

-- Narrow, append-only time-series table (one row per stored value).
CREATE TABLE IF NOT EXISTS plant.readings (
    tag_id  integer NOT NULL REFERENCES plant.tags,
    ts      timestamptz NOT NULL,              -- OPC UA source timestamp
    value   double precision NOT NULL,
    quality smallint NOT NULL DEFAULT 0,       -- 0 good, 1 uncertain, 2 bad
    PRIMARY KEY (tag_id, ts)
);
CREATE INDEX IF NOT EXISTS readings_ts_brin ON plant.readings USING brin (ts);

CREATE TABLE IF NOT EXISTS plant.fault_codes (
    code        integer PRIMARY KEY,
    description text NOT NULL,
    category    text NOT NULL CHECK (category IN ('None', 'Process', 'Mechanical', 'Electrical', 'Safety', 'Interlock', 'Planned'))
);

INSERT INTO plant.fault_codes (code, description, category) VALUES
    (0,   'No fault',                                  'None'),
    (101, 'Crusher motor overload trip',               'Electrical'),
    (102, 'Crusher bearing over-temperature',          'Mechanical'),
    (103, 'Crusher discharge chute blocked',           'Process'),
    (104, 'Crusher stopped by downstream interlock',   'Interlock'),
    (201, 'Conveyor belt misalignment trip',           'Mechanical'),
    (202, 'Conveyor emergency pull-wire',              'Safety'),
    (203, 'Conveyor stopped by downstream interlock',  'Interlock'),
    (900, 'Planned stop',                              'Planned')
ON CONFLICT (code) DO UPDATE SET description = EXCLUDED.description, category = EXCLUDED.category;

CREATE TABLE IF NOT EXISTS plant.alarm_limits (
    tag       text PRIMARY KEY REFERENCES plant.tags (name),
    high      double precision NOT NULL,
    high_high double precision
);
