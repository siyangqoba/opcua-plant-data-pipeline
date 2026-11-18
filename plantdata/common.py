"""Shared pieces: tag definitions, report-by-exception filter."""
from __future__ import annotations

from dataclasses import dataclass

NAMESPACE_URI = "urn:plantdata:riverside:primary-crushing"


@dataclass(frozen=True)
class TagDef:
    name: str            # e.g. "CR101.MotorCurrent"
    unit: str = ""
    deadband: float = 0.0  # absolute change needed before a new value is stored
    description: str = ""

    @property
    def equipment(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def is_discrete(self) -> bool:
        return self.name.endswith((".Running", ".State", ".FaultCode"))


TAGS: list[TagDef] = [
    TagDef("CR101.Running", "", 0, "Crusher running feedback"),
    TagDef("CR101.State", "", 0, "0 Stopped, 1 Starting, 2 Running, 3 Faulted"),
    TagDef("CR101.FaultCode", "", 0, "Active fault code"),
    TagDef("CR101.FeedRate", "t/h", 5.0, "Feed rate to crusher"),
    TagDef("CR101.MotorCurrent", "A", 3.0, "Crusher main motor current"),
    TagDef("CR101.BearingTemp", "degC", 0.5, "Crusher main bearing temperature"),
    TagDef("CR101.Vibration", "mm/s", 0.2, "Crusher bearing vibration (RMS)"),
    TagDef("CV101.Running", "", 0, "Conveyor running feedback"),
    TagDef("CV101.State", "", 0, "0 Stopped, 1 Starting, 2 Running, 3 Faulted"),
    TagDef("CV101.FaultCode", "", 0, "Active fault code"),
    TagDef("CV101.BeltSpeed", "m/s", 0.05, "Belt speed"),
    TagDef("CV101.MotorCurrent", "A", 1.5, "Conveyor drive current"),
    TagDef("CV101.Throughput", "t/h", 5.0, "Belt scale throughput"),
    TagDef("CV101.TotalTonnes", "t", 1.0, "Belt scale totaliser"),
]
TAGS_BY_NAME = {t.name: t for t in TAGS}


class DeadbandFilter:
    """Report-by-exception: only pass values that moved more than the deadband.

    Discrete tags (states, fault codes, run feedback) pass on any change.
    A heartbeat forces a value through every ``max_silence`` seconds so a flat
    line in the database means "unchanged", not "collector was down".
    """

    def __init__(self, deadbands: dict[str, float], max_silence: float = 900.0):
        self.deadbands = deadbands
        self.max_silence = max_silence
        self._last: dict[str, tuple[float, float]] = {}

    def accept(self, tag: str, value: float, ts: float) -> bool:
        value = float(value)
        prev = self._last.get(tag)
        band = self.deadbands.get(tag, 0.0)
        if prev is None or abs(value - prev[0]) > band or (band == 0 and value != prev[0]) \
                or ts - prev[1] >= self.max_silence:
            self._last[tag] = (value, ts)
            return True
        return False
