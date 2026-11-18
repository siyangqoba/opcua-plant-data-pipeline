"""OPC UA → PostgreSQL collector.

    plant-collector --config config/plant.yaml

* Subscribes to the configured tags (OPC UA data-change subscription), so the server
  pushes changes instead of the collector polling.
* Filters with per-tag deadbands (report by exception) plus a heartbeat.
* Writes in batches with COPY. If PostgreSQL is down, batches go to a local SQLite
  outbox and are forwarded, in order, when it comes back.
* Reconnects to the OPC UA server with exponential backoff and re-subscribes.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import psycopg
import yaml
from asyncua import Client, ua

from plantdata.collector.buffer import NamedRow, Outbox
from plantdata.common import DeadbandFilter
from plantdata.db import insert_readings, setup_database, tag_defs

log = logging.getLogger("plant-collector")


def quality_of(status: ua.StatusCode | None) -> int:
    if status is None or status.is_good():
        return 0
    return 1 if status.is_uncertain() else 2


@dataclass
class Stats:
    received: int = 0
    accepted: int = 0
    written: int = 0
    buffered: int = 0
    reconnects: int = 0
    started: float = field(default_factory=time.monotonic)


class Collector:
    def __init__(self, config: dict):
        self.cfg = config
        self.tags = tag_defs(config["tags"])
        self.filter = DeadbandFilter({t.name: t.deadband for t in self.tags},
                                     max_silence=config.get("heartbeat_seconds", 900))
        self.queue: asyncio.Queue[NamedRow] = asyncio.Queue(maxsize=100_000)
        self.outbox = Outbox(config["buffer"]["path"])
        self.tag_ids: dict[str, int] = {}
        self.node_to_tag: dict[str, str] = {}
        self.db: psycopg.Connection | None = None
        self.stats = Stats()
        self.stop = asyncio.Event()
        self.connected = asyncio.Event()

    # ------------------------------------------------------------------ database
    def _db_connect(self) -> None:
        self.db = psycopg.connect(self.cfg["database"]["url"], connect_timeout=5)
        self.tag_ids = setup_database(self.db, self.tags, self.cfg.get("alarm_limits"))

    def _with_ids(self, rows: list[NamedRow]):
        return [(self.tag_ids[tag], ts, value, q) for tag, ts, value, q in rows if tag in self.tag_ids]

    def _write(self, rows: list[NamedRow]) -> None:
        """Runs in a worker thread. Raises on database failure."""
        if self.db is None or self.db.closed:
            self._db_connect()
        # oldest first: drain the outbox before new data so ordering is preserved
        while len(self.outbox):
            ids, backlog = self.outbox.peek()
            self.stats.written += insert_readings(self.db, self._with_ids(backlog))
            self.outbox.ack(ids)
            log.info("forwarded %d buffered readings (%d left)", len(backlog), len(self.outbox))
        self.stats.written += insert_readings(self.db, self._with_ids(rows))

    async def writer(self) -> None:
        batch_size = self.cfg["database"].get("batch_size", 500)
        flush_s = self.cfg["database"].get("flush_seconds", 2.0)
        while not (self.stop.is_set() and self.queue.empty()):
            batch: list[NamedRow] = []
            deadline = time.monotonic() + flush_s
            while len(batch) < batch_size and time.monotonic() < deadline:
                try:
                    remaining = max(0.05, deadline - time.monotonic())
                    batch.append(await asyncio.wait_for(self.queue.get(), timeout=remaining))
                except TimeoutError:
                    break
            if not batch and not len(self.outbox):
                continue
            try:
                await asyncio.to_thread(self._write, batch)
            except (psycopg.Error, OSError) as exc:
                log.warning("database unavailable (%s); buffering %d readings", exc.__class__.__name__, len(batch))
                if self.db is not None:
                    try:
                        self.db.close()
                    except Exception:  # noqa: BLE001
                        pass
                self.db = None
                self.outbox.put(batch)
                self.stats.buffered += len(batch)
                await self._sleep(2.0)

    # ------------------------------------------------------------------ OPC UA
    def datachange_notification(self, node, val, data) -> None:  # asyncua callback
        tag = self.node_to_tag.get(node.nodeid.to_string())
        if tag is None:
            return
        self.stats.received += 1
        dv = data.monitored_item.Value
        ts = dv.SourceTimestamp or dv.ServerTimestamp or datetime.now(UTC)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        try:
            value = float(val)
        except (TypeError, ValueError):
            return
        if not self.filter.accept(tag, value, ts.timestamp()):
            return
        self.stats.accepted += 1
        self.queue.put_nowait((tag, ts, value, quality_of(dv.StatusCode)))

    def status_change_notification(self, status) -> None:
        log.warning("subscription status change: %s", status)

    async def opcua_loop(self) -> None:
        opc = self.cfg["opcua"]
        backoff = 1.0
        while not self.stop.is_set():
            try:
                async with Client(opc["endpoint"], timeout=10) as client:
                    ns = await client.get_namespace_index(opc["namespace_uri"])
                    nodes = []
                    for tag in self.tags:
                        node = client.get_node(ua.NodeId(tag.name, ns))
                        self.node_to_tag[node.nodeid.to_string()] = tag.name
                        nodes.append(node)
                    sub = await client.create_subscription(opc.get("publishing_interval_ms", 1000), self)
                    await sub.subscribe_data_change(nodes, sampling_interval=opc.get("sampling_interval_ms", 500))
                    log.info("subscribed to %d tags on %s", len(nodes), opc["endpoint"])
                    self.connected.set()
                    backoff = 1.0
                    while not self.stop.is_set():
                        await client.check_connection()
                        await self._sleep(2.0)
                    await sub.delete()
            except (OSError, TimeoutError, ua.UaError, ConnectionError) as exc:
                self.connected.clear()
                if self.stop.is_set():
                    break
                self.stats.reconnects += 1
                log.warning("OPC UA connection lost (%s); retrying in %.0fs", exc.__class__.__name__, backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, opc.get("reconnect_max_seconds", 60))

    async def stats_loop(self) -> None:
        while not self.stop.is_set():
            await self._sleep(30)
            s = self.stats
            log.info("received=%d stored=%d buffered_now=%d reconnects=%d queue=%d",
                     s.received, s.written, len(self.outbox), s.reconnects, self.queue.qsize())

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def run(self) -> None:
        try:
            await asyncio.to_thread(self._db_connect)
        except psycopg.Error as exc:
            # keep collecting: readings are buffered by tag name and written once the database is back
            log.warning("database not reachable at start (%s); readings will be buffered", exc.__class__.__name__)
        await asyncio.gather(self.opcua_loop(), self.writer(), self.stats_loop())
        if self.db is not None:
            self.db.close()
        self.outbox.close()


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config/plant.yaml")
    p.add_argument("--endpoint", help="override opcua.endpoint")
    p.add_argument("--database-url", help="override database.url")
    a = p.parse_args()
    cfg = load_config(a.config)
    if a.endpoint:
        cfg["opcua"]["endpoint"] = a.endpoint
    if a.database_url:
        cfg["database"]["url"] = a.database_url

    async def run():
        collector = Collector(cfg)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, collector.stop.set)
        await collector.run()

    asyncio.run(run())


if __name__ == "__main__":
    main()
