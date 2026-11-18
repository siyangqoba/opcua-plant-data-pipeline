"""PostgreSQL storage for tag readings (psycopg 3)."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import psycopg

from plantdata.common import TAGS_BY_NAME, TagDef

SQL_DIR = Path(__file__).resolve().parent / "sql"
Row = tuple[int, datetime, float, int]   # tag_id, ts, value, quality

UPSERT_TAG = """
INSERT INTO plant.tags (name, equipment, unit, description, deadband)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (name) DO UPDATE SET equipment = EXCLUDED.equipment, unit = EXCLUDED.unit,
    description = EXCLUDED.description, deadband = EXCLUDED.deadband
RETURNING tag_id
"""
UPSERT_LIMIT = """
INSERT INTO plant.alarm_limits (tag, high, high_high) VALUES (%s, %s, %s)
ON CONFLICT (tag) DO UPDATE SET high = EXCLUDED.high, high_high = EXCLUDED.high_high
"""


def setup_database(
    conn: psycopg.Connection, tags: Iterable[TagDef], alarm_limits: dict | None = None
) -> dict[str, int]:
    """Create schema and views (idempotent), register tags, return {tag name: tag_id}."""
    conn.execute((SQL_DIR / "schema.sql").read_text())
    ids = {}
    for tag in tags:
        ids[tag.name] = conn.execute(
            UPSERT_TAG, (tag.name, tag.equipment, tag.unit, tag.description, tag.deadband)
        ).fetchone()[0]
    for name, lim in (alarm_limits or {}).items():
        conn.execute(UPSERT_LIMIT, (name, lim["high"], lim.get("high_high")))
    conn.execute((SQL_DIR / "views.sql").read_text())
    conn.commit()
    return ids


def insert_readings(conn: psycopg.Connection, rows: list[Row]) -> int:
    """Bulk insert with COPY into a temp table, then merge. Duplicates are ignored, so a
    batch that is retried after a timeout can never be stored twice."""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE IF NOT EXISTS _incoming (LIKE plant.readings) ON COMMIT DELETE ROWS")
        with cur.copy("COPY _incoming (tag_id, ts, value, quality) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)
        cur.execute("INSERT INTO plant.readings SELECT * FROM _incoming ON CONFLICT DO NOTHING")
        inserted = cur.rowcount
    conn.commit()
    return inserted


def tag_defs(config_tags: list) -> list[TagDef]:
    """Turn the YAML tag list (names or {name, deadband}) into TagDefs."""
    out = []
    for item in config_tags:
        name = item if isinstance(item, str) else item["name"]
        base = TAGS_BY_NAME.get(name)
        if base is None:
            raise ValueError(f"unknown tag {name!r}")
        if isinstance(item, dict) and "deadband" in item:
            base = TagDef(base.name, base.unit, float(item["deadband"]), base.description)
        out.append(base)
    return out
