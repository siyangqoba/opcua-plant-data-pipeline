"""Generate plant history straight into PostgreSQL (no OPC UA), for demos and testing.

    plant-backfill --database-url postgresql://... --hours 72

Runs the same process model and the same deadband filter as the live collector,
so the stored data looks exactly like collected data.
"""
from __future__ import annotations

import argparse
import logging
from datetime import UTC, datetime, timedelta

import psycopg

from plantdata.collector.collector import load_config
from plantdata.common import DeadbandFilter
from plantdata.db import insert_readings, setup_database, tag_defs
from plantdata.sim.model import Plant

log = logging.getLogger("plant-backfill")


def backfill(database_url: str, hours: float, config: dict, seed: int = 42, step: float = 5.0,
             end: datetime | None = None) -> int:
    tags = tag_defs(config["tags"])
    end = end or datetime.now(UTC).replace(microsecond=0)
    start = end - timedelta(hours=hours)
    plant = Plant(seed=seed)
    flt = DeadbandFilter({t.name: t.deadband for t in tags}, max_silence=config.get("heartbeat_seconds", 900))
    wanted = {t.name for t in tags}

    with psycopg.connect(database_url) as conn:
        ids = setup_database(conn, tags, config.get("alarm_limits"))
        rows, total = [], 0
        steps = int(hours * 3600 / step)
        for i in range(steps + 1):
            if i:
                plant.step(step)
            ts = start + timedelta(seconds=plant.clock)
            for name, value in plant.tags().items():
                if name in wanted and flt.accept(name, float(value), ts.timestamp()):
                    rows.append((ids[name], ts, float(value), 0))
            if len(rows) >= 20_000:
                total += insert_readings(conn, rows)
                rows.clear()
        total += insert_readings(conn, rows)
    log.info("stored %d readings covering %s to %s", total, start.isoformat(), end.isoformat())
    return total


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config/plant.yaml")
    p.add_argument("--database-url")
    p.add_argument("--hours", type=float, default=72)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--step", type=float, default=5.0, help="simulation step in seconds")
    a = p.parse_args()
    cfg = load_config(a.config)
    backfill(a.database_url or cfg["database"]["url"], a.hours, cfg, seed=a.seed, step=a.step)


if __name__ == "__main__":
    main()
