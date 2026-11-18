"""End-to-end: OPC UA server -> collector -> PostgreSQL, including failure handling."""
import asyncio
import socket

import psycopg

from plantdata.collector.collector import Collector
from plantdata.sim.model import Plant
from plantdata.sim.server import PlantServer
from tests.conftest import needs_db


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def start_server(port: int, seed: int = 1):
    srv = PlantServer(f"opc.tcp://127.0.0.1:{port}/plant/", Plant(seed=seed), time_scale=120, step=1)
    await srv.init()
    stop = asyncio.Event()
    task = asyncio.create_task(srv.run(stop))
    await asyncio.sleep(0.5)
    return stop, task


async def wait_for(predicate, timeout=20.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.2)
    return False


def count(url: str) -> int:
    with psycopg.connect(url) as conn:
        return conn.execute("SELECT count(*) FROM plant.readings").fetchone()[0]


@needs_db
async def test_collects_live_data(clean_db, config):
    port = free_port()
    stop_srv, srv_task = await start_server(port)
    config["opcua"]["endpoint"] = f"opc.tcp://127.0.0.1:{port}/plant/"
    collector = Collector(config)
    task = asyncio.create_task(collector.run())
    assert await wait_for(lambda: collector.stats.written > 50)
    collector.stop.set()
    await task
    stop_srv.set()
    await srv_task
    with psycopg.connect(clean_db) as conn:
        tags = conn.execute(
            "SELECT DISTINCT t.name FROM plant.readings r JOIN plant.tags t USING (tag_id)"
        ).fetchall()
        # source timestamps are plant time (x120), so stored data spans far more than wall time
        span = conn.execute("SELECT extract(epoch FROM max(ts) - min(ts)) FROM plant.readings").fetchone()[0]
    assert {"CR101.MotorCurrent", "CV101.Throughput", "CR101.State"} <= {t[0] for t in tags}
    assert span > 60


@needs_db
async def test_buffers_while_database_is_down_then_forwards(clean_db, config):
    port = free_port()
    stop_srv, srv_task = await start_server(port)
    good_url = config["database"]["url"]
    config["database"]["url"] = "postgresql://nobody:nothing@127.0.0.1:1/none"  # unreachable
    config["opcua"]["endpoint"] = f"opc.tcp://127.0.0.1:{port}/plant/"
    collector = Collector(config)
    task = asyncio.create_task(collector.run())
    assert await wait_for(lambda: len(collector.outbox) > 30)
    buffered = collector.stats.buffered
    assert buffered > 30

    collector.cfg["database"]["url"] = good_url   # database comes back
    assert await wait_for(lambda: len(collector.outbox) == 0 and collector.stats.written >= buffered)
    collector.stop.set()
    await task
    stop_srv.set()
    await srv_task
    assert count(good_url) >= buffered


@needs_db
async def test_reconnects_after_server_restart(clean_db, config):
    port = free_port()
    stop_srv, srv_task = await start_server(port)
    config["opcua"]["endpoint"] = f"opc.tcp://127.0.0.1:{port}/plant/"
    config["opcua"]["reconnect_max_seconds"] = 2
    collector = Collector(config)
    task = asyncio.create_task(collector.run())
    assert await wait_for(lambda: collector.connected.is_set() and collector.stats.written > 10)

    stop_srv.set()                 # server goes away
    await srv_task
    assert await wait_for(lambda: not collector.connected.is_set() and collector.stats.reconnects >= 1)
    before = collector.stats.written

    stop_srv, srv_task = await start_server(port, seed=2)   # and comes back
    assert await wait_for(lambda: collector.connected.is_set(), timeout=30)
    assert await wait_for(lambda: collector.stats.written > before + 10)
    collector.stop.set()
    await task
    stop_srv.set()
    await srv_task
