import os

import psycopg
import pytest

DB_URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture()
def clean_db():
    with psycopg.connect(DB_URL) as conn:
        conn.execute("DROP SCHEMA IF EXISTS plant CASCADE")
        conn.commit()
    return DB_URL


@pytest.fixture()
def config(tmp_path):
    from plantdata.collector.collector import load_config
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config", "plant.yaml"))
    cfg["database"]["url"] = DB_URL or "postgresql://invalid"
    cfg["database"]["flush_seconds"] = 0.3
    cfg["buffer"]["path"] = str(tmp_path / "buffer.sqlite")
    return cfg
