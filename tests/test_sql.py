from collections import Counter
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row

from plantdata.sim.backfill import backfill
from plantdata.sim.model import Plant, State
from tests.conftest import needs_db

END = datetime(2031, 3, 4, 4, 0, tzinfo=UTC)  # 06:00 SAST, a shift boundary


@needs_db
def test_downtime_events_match_the_simulator(clean_db, config):
    backfill(clean_db, 48, config, seed=11, end=END)
    plant = Plant(seed=11)
    for _ in range(int(48 * 3600 / 5)):
        plant.step(5)
    expected = Counter((e[1], e[3]) for e in plant.events if e[2] == State.FAULTED)
    with psycopg.connect(clean_db) as conn:
        rows = conn.execute(
            "SELECT equipment, fault_code, count(*) FROM plant.v_downtime_events GROUP BY 1, 2"
        ).fetchall()
    got = Counter({(eq, code): n for eq, code, n in rows})
    # zero-length interlock trips are filtered out by design
    expected = Counter({k: v for k, v in expected.items() if k[1] not in (104, 203)})
    assert got == expected


@needs_db
def test_shift_time_is_fully_accounted_for(clean_db, config):
    backfill(clean_db, 24, config, seed=5, end=END)
    with psycopg.connect(clean_db, row_factory=dict_row) as conn:
        kpis = conn.execute("SELECT * FROM plant.v_shift_kpis WHERE equipment = 'CR101'").fetchall()
    assert {(k["shift"]) for k in kpis} == {"A", "B", "C"}
    for k in kpis:
        total = float(k["run_hours"]) * 60 + float(k["fault_minutes"]) + float(k["stopped_minutes"])
        assert abs(total - float(k["hours_covered"]) * 60) < 1.0
        assert 0 <= float(k["availability"]) <= 1
        assert float(k["tonnes"]) > 0


@needs_db
def test_heartbeats_do_not_split_state_intervals(clean_db, config):
    backfill(clean_db, 12, config, seed=5, end=END)
    with psycopg.connect(clean_db) as conn:
        repeated = conn.execute(
            """SELECT count(*) FROM (
                   SELECT state, lag(state) OVER (PARTITION BY equipment ORDER BY start_ts) AS prev
                   FROM plant.v_state_intervals) x
               WHERE state = prev"""
        ).fetchone()[0]
    assert repeated == 0


@needs_db
def test_alarm_raised_once_per_excursion(clean_db, config):
    from plantdata.db import insert_readings, setup_database, tag_defs
    with psycopg.connect(clean_db) as conn:
        ids = setup_database(conn, tag_defs(config["tags"]), {"CR101.BearingTemp": {"high": 75, "high_high": 85}})
        t0 = datetime(2031, 1, 1, 8, 0, tzinfo=UTC).timestamp()
        values = [70] * 5 + [78] * 5 + [88] * 3 + [70] * 5 + [79] * 2
        rows = [(ids["CR101.BearingTemp"], datetime.fromtimestamp(t0 + i * 60, UTC), float(v), 0)
                for i, v in enumerate(values)]
        insert_readings(conn, rows)
        alarms = conn.execute("SELECT severity FROM plant.v_alarm_events ORDER BY raised_at").fetchall()
    assert [a[0] for a in alarms] == ["HIGH", "HIGH"]


@needs_db
def test_insert_is_idempotent(clean_db, config):
    from plantdata.db import insert_readings, setup_database, tag_defs
    with psycopg.connect(clean_db) as conn:
        ids = setup_database(conn, tag_defs(config["tags"]))
        row = (ids["CV101.Throughput"], datetime(2031, 1, 1, tzinfo=UTC), 450.0, 0)
        assert insert_readings(conn, [row]) == 1
        assert insert_readings(conn, [row]) == 0
        setup_database(conn, tag_defs(config["tags"]))  # re-running setup keeps data
        assert conn.execute("SELECT count(*) FROM plant.readings").fetchone()[0] == 1
