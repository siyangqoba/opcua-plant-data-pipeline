"""OPC UA server exposing the simulated crushing circuit.

    plant-sim --endpoint opc.tcp://0.0.0.0:4840/plant/ --time-scale 60

Address space (namespace ``urn:plantdata:riverside:primary-crushing``):

    Objects/Plant/CR101/{Running, State, FaultCode, FaultText, FeedRate, MotorCurrent,
                         BearingTemp, Vibration}   methods: Start, Stop, Reset
    Objects/Plant/CV101/{Running, State, FaultCode, FaultText, BeltSpeed, MotorCurrent,
                         Throughput, TotalTonnes}   methods: Start, Stop, Reset

Node ids are strings, e.g. ``ns=2;s=CR101.MotorCurrent``. With ``--time-scale`` above 1
the plant runs faster than real time and values carry *plant* source timestamps, so a
full 8-hour shift can be produced in 8 minutes for demos.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime, timedelta

from asyncua import Server, ua
from asyncua.common.methods import uamethod

from plantdata.common import NAMESPACE_URI, TAGS
from plantdata.sim.model import FAULT_TEXT, Fault, Plant

log = logging.getLogger("plant-sim")

VARIANT_TYPES = {bool: ua.VariantType.Boolean, int: ua.VariantType.Int32, float: ua.VariantType.Double}


class PlantServer:
    def __init__(self, endpoint: str, plant: Plant, time_scale: float = 1.0, step: float = 1.0,
                 start_time: datetime | None = None):
        self.endpoint = endpoint
        self.plant = plant
        self.time_scale = time_scale
        self.step = step
        self.start_time = start_time or datetime.now(UTC)
        self.server = Server()
        self.nodes: dict[str, object] = {}
        self.ns = 0

    def plant_time(self) -> datetime:
        return self.start_time + timedelta(seconds=self.plant.clock)

    async def init(self) -> None:
        await self.server.init()
        self.server.set_endpoint(self.endpoint)
        self.server.set_server_name("PlantData Primary Crushing Simulator")
        self.server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        self.ns = await self.server.register_namespace(NAMESPACE_URI)

        plant_obj = await self.server.nodes.objects.add_object(ua.NodeId("Plant", self.ns), "Plant")
        initial = self.plant.tags()
        for equipment in ("CR101", "CV101"):
            obj = await plant_obj.add_object(ua.NodeId(equipment, self.ns), equipment)
            for tag in (t for t in TAGS if t.equipment == equipment):
                value = initial[tag.name]
                var = await obj.add_variable(
                    ua.NodeId(tag.name, self.ns), tag.name.split(".")[1],
                    ua.Variant(value, VARIANT_TYPES[type(value)]),
                )
                if tag.unit:
                    await var.write_attribute(
                        ua.AttributeIds.Description,
                        ua.DataValue(ua.Variant(ua.LocalizedText(f"{tag.description} [{tag.unit}]"))),
                    )
                self.nodes[tag.name] = var
            text = await obj.add_variable(ua.NodeId(f"{equipment}.FaultText", self.ns), "FaultText",
                                          FAULT_TEXT[Fault.NONE])
            self.nodes[f"{equipment}.FaultText"] = text
            for name, fn in (("Start", self.plant.start), ("Stop", self.plant.stop), ("Reset", self.plant.reset)):
                await obj.add_method(
                    ua.NodeId(f"{equipment}.{name}", self.ns), name,
                    self._make_method(fn, equipment), [], [ua.VariantType.Boolean],
                )

    def _make_method(self, fn, equipment):
        @uamethod
        def method(_parent):
            ok = fn(equipment)
            log.info("method %s on %s -> %s", fn.__name__, equipment, ok)
            return ok
        return method

    async def publish(self) -> None:
        ts = self.plant_time()
        values = self.plant.tags()
        for name, value in values.items():
            dv = ua.DataValue(ua.Variant(value, VARIANT_TYPES[type(value)]),
                              SourceTimestamp=ts, ServerTimestamp=datetime.now(UTC))
            await self.nodes[name].write_value(dv)
        for equipment in ("CR101", "CV101"):
            fault = Fault(values[f"{equipment}.FaultCode"])
            await self.nodes[f"{equipment}.FaultText"].write_value(
                ua.DataValue(ua.Variant(FAULT_TEXT[fault], ua.VariantType.String), SourceTimestamp=ts)
            )

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        async with self.server:
            log.info("serving %s (namespace %s, x%s real time)", self.endpoint, NAMESPACE_URI, self.time_scale)
            await self.publish()
            while not stop.is_set():
                self.plant.step(self.step)
                await self.publish()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.step / self.time_scale)
                except TimeoutError:
                    pass


async def amain(args) -> None:
    plant = Plant(seed=args.seed)
    srv = PlantServer(args.endpoint, plant, time_scale=args.time_scale, step=args.step)
    await srv.init()
    await srv.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(description="OPC UA simulator for a primary crushing circuit")
    p.add_argument("--endpoint", default="opc.tcp://0.0.0.0:4840/plant/")
    p.add_argument("--time-scale", type=float, default=1.0, help="plant seconds per real second")
    p.add_argument("--step", type=float, default=1.0, help="simulation step in plant seconds")
    p.add_argument("--seed", type=int, default=42)
    asyncio.run(amain(p.parse_args()))


if __name__ == "__main__":
    main()
