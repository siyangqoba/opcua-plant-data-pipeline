# OPC UA Plant Data Pipeline

An end-to-end **IT/OT integration** project: plant data from an OPC UA server is collected into PostgreSQL, turned into production KPIs with SQL, and published as a shift report.

The plant is a simulated **primary crushing circuit**: crusher CR101 feeding conveyor CV101. The simulator behaves like real equipment. Motor current follows ore hardness, bearings heat up, and equipment trips on overload, blocked chutes, belt misalignment and pull-wires, with interlocks between the two machines.

![Shift report](docs/shift-report.jpg)

## Architecture

```mermaid
flowchart LR
    subgraph OT["OT: plant floor"]
        SIM["OPC UA server<br/>CR101 crusher · CV101 conveyor<br/>(plant-sim)"]
    end
    subgraph IT["IT: data platform"]
        COL["Collector<br/>subscriptions · deadbands<br/>reconnect · store-and-forward<br/>(plant-collector)"]
        BUF[("SQLite<br/>outbox")]
        DB[("PostgreSQL<br/>plant.readings")]
        SQL["SQL views<br/>state intervals · downtime<br/>shift KPIs · alarms"]
        REP["Shift report<br/>(plant-report)"]
    end
    SIM -- "OPC UA data-change<br/>subscription" --> COL
    COL -- "COPY batches" --> DB
    COL <-. "DB down" .-> BUF
    DB --> SQL --> REP
```

## What it does

| Component | Details |
|---|---|
| **OPC UA server** (`plant-sim`) | `asyncua` server with a proper address space (`Objects/Plant/CR101`, `CV101`), engineering-unit descriptions, string node ids (`ns=2;s=CR101.MotorCurrent`) and **Start / Stop / Reset as OPC UA methods**. Can run faster than real time (`--time-scale 60`) with plant source timestamps, so a whole shift can be generated in minutes. |
| **Process model** | Crusher and conveyor with startup sequences, ore-hardness drift and hard-ore pockets, bearing heating and wear, overload and over-temperature trips, random faults with lognormal repair times, planned stops, a downstream interlock, and an operator that resets and restarts in the right order. |
| **Collector** (`plant-collector`) | Subscribes instead of polling. Report-by-exception per-tag deadbands plus a heartbeat (in a live run it stored 1,818 of 10,687 values, an 83% reduction). Batched `COPY` inserts that are idempotent on retry. **Store-and-forward** to a local SQLite outbox while the database is down, forwarded oldest-first when it returns. **Reconnects** to the OPC UA server with exponential backoff, and shuts down gracefully on SIGTERM. |
| **Storage** | A narrow time-series table `plant.readings(tag_id, ts, value, quality)` with a BRIN index on time, a tag registry, fault-code and alarm-limit tables. The schema is idempotent, so it's applied on every start. |
| **Analytics (SQL, `plantdata/sql/`)** | `v_state_intervals` collapses states with gaps-and-islands, so heartbeats don't split them. `v_downtime_events` carries reason and category. `v_shift_kpis` gives availability, run hours, faulted and stopped minutes and tonnes for shifts A/B/C in SAST. `v_alarm_events` gives high and high-high alarms on one-minute averages. |
| **Shift report** (`plant-report`) | A single self-contained HTML file: KPI tiles, a state timeline, a downtime Pareto chart and table, alarms, and process trends with alarm limits. Printable. Falls back to the current shift, marked *partial*, when no shift is complete yet. |
| **Backfill** (`plant-backfill`) | Generates days of history through the same model and deadband filter as the live path, so there's data immediately (72 hours ≈ 54k readings in under 3 s). |

## Quick start

```bash
docker compose up --build            # db, simulator, 72h backfill, live collector
docker compose run --rm report       # -> reports/shift_<date>_<shift>.html
```

To browse the live OPC UA server, point an OPC UA client such as [UaExpert](https://www.unified-automation.com/products/development-tools/uaexpert.html) at `opc.tcp://localhost:4840/plant/`. You can also call the Start, Stop and Reset methods from there.

**Without Docker**

```bash
pip install -e ".[dev]"
plant-sim --time-scale 60 &                                   # OPC UA server on :4840
plant-backfill --database-url postgresql://user:pass@localhost/plant --hours 72
plant-collector --config config/plant.yaml --database-url postgresql://user:pass@localhost/plant
plant-report --database-url postgresql://user:pass@localhost/plant
```

## Design decisions

- **Subscriptions, not polling.** The server pushes changes at the sampling interval, so there's less network traffic and no missed short spikes between polls.
- **Deadband plus heartbeat.** Deadbands keep storage proportional to *change*. The heartbeat writes a value every 15 minutes even when nothing moves, so a flat line in the data means "unchanged" and not "collector was down". Because heartbeats repeat states, the SQL collapses consecutive equal states before computing intervals.
- **Source timestamps.** Values are stored with the OPC UA *source* timestamp (when the plant measured them), not the time they arrived, so buffered or delayed data still lands at the right time.
- **Buffering by tag name.** The outbox stores tag names, not database ids, so the collector can start and keep collecting even if the database has never been reachable.
- **Idempotent writes.** `COPY` into a temp table, then `INSERT … ON CONFLICT DO NOTHING`. A batch retried after a timeout can't be stored twice.
- **Stopped versus faulted.** When the conveyor trips, the crusher is *stopped by interlock*, not faulted itself. The KPIs keep both, so every minute of a shift is accounted for (run + faulted + stopped = covered time, which is tested).

## Tests

```bash
TEST_DATABASE_URL=postgresql://user:pass@localhost/plant_test pytest
```

There are 22 tests:
- **Process model:** start-up, interlocks, overload trip, reset rules, totaliser balance, plausible availability.
- **Deadband filter:** the deadband, the heartbeat, config overrides.
- **SQL:** downtime events match the simulator's own event log exactly, shift time is fully accounted for, heartbeats don't split intervals, one alarm per excursion, idempotent inserts.
- **End to end:** a live OPC UA server feeding the collector and PostgreSQL. The tests also cover the database going down (buffered, then forwarded with nothing lost) and the OPC UA server restarting (the collector reconnects and carries on).
- **Report:** rendering, including partial shifts.

## Taking it to a real plant

This is a portfolio project. Production use would add:
- **OPC UA security:** `Basic256Sha256` with sign-and-encrypt, certificate trust lists and user authentication. The simulator runs `NoSecurity` for local demos only.
- **Larger-scale storage:** TimescaleDB hypertables with compression and continuous aggregates instead of plain views.
- **Integration with existing systems:** reading from the site historian (e.g. AVEVA PI) or publishing to MQTT/Sparkplug B, and pushing downtime events into the maintenance system (SAP PM / Odoo).
- **Alarm handling to ISA-18.2:** shelving, acknowledgement and alarm-flood metrics.

## Tech

Python 3.11+ · asyncua (OPC UA) · PostgreSQL 16 (window functions, gaps-and-islands, BRIN) · psycopg 3 (COPY) · SQLite · matplotlib · Jinja2 · Docker Compose · pytest-asyncio · GitHub Actions
