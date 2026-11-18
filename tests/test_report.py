from datetime import UTC, date, datetime

import psycopg

from plantdata.report.shift_report import build_report
from plantdata.sim.backfill import backfill
from tests.conftest import needs_db


@needs_db
def test_shift_report_renders(clean_db, config):
    backfill(clean_db, 24, config, seed=7, end=datetime(2031, 3, 4, 4, 0, tzinfo=UTC))
    with psycopg.connect(clean_db) as conn:
        html, window = build_report(conn, date(2031, 3, 3), "A")
    assert window["shift"] == "A"
    assert "Crusher availability" in html
    assert "Shift A · Monday 03 March 2031" in html
    assert html.count("data:image/png;base64,") >= 3
    with psycopg.connect(clean_db) as conn:
        latest_html, latest = build_report(conn)  # most recent complete shift
    assert latest["shift_end"] <= datetime(2031, 3, 4, 4, 0, tzinfo=UTC)


@needs_db
def test_partial_shift_when_no_complete_shift(clean_db, config):
    # 3 hours of data ending mid-shift: no complete shift yet
    backfill(clean_db, 3, config, seed=7, end=datetime(2031, 3, 4, 7, 0, tzinfo=UTC))
    with psycopg.connect(clean_db) as conn:
        html, window = build_report(conn)
    assert window["partial"] is True
    assert "Partial" in html
    assert "Crusher availability" in html
