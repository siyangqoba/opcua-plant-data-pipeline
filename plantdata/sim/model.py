"""Process model for a primary crushing circuit: crusher CR101 feeding conveyor CV101.

Not a physics-grade simulator, but it behaves like the real thing in the ways
that matter for data work: motor current follows load and ore hardness,
bearings heat up and cool down, vibration drifts with wear, and equipment
trips on overload, over-temperature, blocked chutes and belt faults, then
sits faulted until the fault is cleared.

Interlocks follow normal practice: the crusher can't run unless the conveyor
it discharges onto is running, so a conveyor trip stops the crusher too.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from enum import IntEnum


class State(IntEnum):
    STOPPED = 0
    STARTING = 1
    RUNNING = 2
    FAULTED = 3


class Fault(IntEnum):
    NONE = 0
    CR_OVERLOAD = 101
    CR_BEARING_OVERTEMP = 102
    CR_BLOCKED_CHUTE = 103
    CV_BELT_MISALIGNMENT = 201
    CV_PULL_WIRE = 202
    CV_DOWNSTREAM_INTERLOCK = 203
    CR_DOWNSTREAM_INTERLOCK = 104
    PLANNED_STOP = 900


FAULT_TEXT = {
    Fault.NONE: "No fault",
    Fault.CR_OVERLOAD: "Crusher motor overload trip",
    Fault.CR_BEARING_OVERTEMP: "Crusher bearing over-temperature",
    Fault.CR_BLOCKED_CHUTE: "Crusher discharge chute blocked",
    Fault.CR_DOWNSTREAM_INTERLOCK: "Crusher stopped by downstream interlock",
    Fault.CV_BELT_MISALIGNMENT: "Conveyor belt misalignment trip",
    Fault.CV_PULL_WIRE: "Conveyor emergency pull-wire",
    Fault.CV_DOWNSTREAM_INTERLOCK: "Conveyor stopped by downstream interlock",
    Fault.PLANNED_STOP: "Planned stop",
}

# Mean time between faults (running hours) and mean time to repair (minutes)
FAULT_RATES = {
    Fault.CR_BLOCKED_CHUTE: (14.0, 25.0),
    Fault.CV_BELT_MISALIGNMENT: (30.0, 35.0),
    Fault.CV_PULL_WIRE: (60.0, 12.0),
}
REPAIR_MINUTES = {
    Fault.CR_OVERLOAD: 15.0,
    Fault.CR_BEARING_OVERTEMP: 60.0,
    Fault.CR_BLOCKED_CHUTE: 25.0,
    Fault.CV_BELT_MISALIGNMENT: 35.0,
    Fault.CV_PULL_WIRE: 12.0,
    Fault.PLANNED_STOP: 30.0,
}

AMBIENT_C = 24.0
CR_OVERLOAD_AMPS = 265.0
CR_OVERLOAD_SECONDS = 8.0
CR_BEARING_TRIP_C = 92.0
STARTUP_SECONDS = 20.0


@dataclass
class Equipment:
    tag: str
    state: State = State.STOPPED
    fault: Fault = Fault.NONE
    state_timer: float = 0.0          # seconds in current state
    repair_left: float = 0.0          # seconds until a fault clears itself (operator fixes it)
    motor_current: float = 0.0

    @property
    def running(self) -> bool:
        return self.state == State.RUNNING


@dataclass
class Crusher(Equipment):
    feed_setpoint: float = 450.0      # t/h
    feed_rate: float = 0.0
    hardness: float = 1.0             # relative ore hardness, random walk around 1
    bearing_temp: float = AMBIENT_C
    vibration: float = 1.2            # mm/s RMS
    wear: float = 0.35                # 0..1, raises vibration and bearing heat
    overload_timer: float = 0.0
    hard_ore_left: float = 0.0        # seconds left in a hard-ore pocket


@dataclass
class Conveyor(Equipment):
    belt_speed: float = 0.0           # m/s
    throughput: float = 0.0           # t/h
    total_tonnes: float = 0.0


@dataclass
class Plant:
    seed: int = 42
    auto_restart: bool = True
    planned_stops_per_day: int = 2
    crusher: Crusher = field(default_factory=lambda: Crusher("CR101"))
    conveyor: Conveyor = field(default_factory=lambda: Conveyor("CV101"))
    clock: float = 0.0                # plant seconds since start
    events: list = field(default_factory=list)

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        self._next_planned = self._schedule_planned()

    # ------------------------------------------------------------------ commands
    def start(self, tag: str) -> bool:
        eq = self._eq(tag)
        if eq.state == State.FAULTED or eq.state in (State.RUNNING, State.STARTING):
            return False
        if eq is self.crusher and not self.conveyor.running:
            return False  # interlock: start downstream first
        self._set_state(eq, State.STARTING)
        return True

    def stop(self, tag: str) -> bool:
        eq = self._eq(tag)
        if eq.state in (State.STOPPED, State.FAULTED):
            return False
        self._set_state(eq, State.STOPPED)
        if eq is self.conveyor and self.crusher.state in (State.RUNNING, State.STARTING):
            self._trip(self.crusher, Fault.CR_DOWNSTREAM_INTERLOCK, repair_s=0)
        return True

    def reset(self, tag: str) -> bool:
        eq = self._eq(tag)
        if eq.state != State.FAULTED or eq.repair_left > 0:
            return False
        eq.fault = Fault.NONE
        self._set_state(eq, State.STOPPED)
        return True

    # ------------------------------------------------------------------ simulation
    def step(self, dt: float = 1.0) -> None:
        """Advance the plant by ``dt`` seconds."""
        self.clock += dt
        for eq in (self.conveyor, self.crusher):
            eq.state_timer += dt
            if eq.state == State.FAULTED and eq.repair_left > 0:
                eq.repair_left = max(0.0, eq.repair_left - dt)
            if eq.state == State.STARTING and eq.state_timer >= STARTUP_SECONDS:
                self._set_state(eq, State.RUNNING)

        self._maybe_planned_stop()
        self._random_faults(dt)
        self._update_conveyor(dt)
        self._update_crusher(dt)
        if self.auto_restart:
            self._operator(dt)

    def _update_conveyor(self, dt: float) -> None:
        cv = self.conveyor
        target_speed = 2.5 if cv.state in (State.RUNNING, State.STARTING) else 0.0
        if cv.state == State.STARTING:
            target_speed *= min(1.0, cv.state_timer / STARTUP_SECONDS)
        cv.belt_speed += (target_speed - cv.belt_speed) * min(1.0, dt / 4.0)
        cv.throughput = self.crusher.feed_rate if cv.running else 0.0
        cv.total_tonnes += cv.throughput * dt / 3600.0
        idle = 18.0 if cv.belt_speed > 0.1 else 0.0
        cv.motor_current = idle + 0.075 * cv.throughput + self.rng.gauss(0, 0.8) * (idle > 0)
        cv.motor_current = max(0.0, cv.motor_current)

    def _update_crusher(self, dt: float) -> None:
        cr = self.crusher
        # ore hardness wanders slowly (mean-reverting), with occasional hard-ore pockets
        if cr.running and cr.hard_ore_left <= 0 and self.rng.random() < dt / (8 * 3600):
            cr.hard_ore_left = self.rng.uniform(10, 30) * 60
        cr.hard_ore_left = max(0.0, cr.hard_ore_left - dt)
        target_hardness = 1.38 if cr.hard_ore_left > 0 else 1.0
        cr.hardness += (target_hardness - cr.hardness) * 0.004 * dt + self.rng.gauss(0, 0.004) * math.sqrt(dt)
        cr.hardness = min(1.5, max(0.7, cr.hardness))
        cr.wear = min(1.0, cr.wear + dt / (3600 * 24 * 30))  # ~a month to fully worn

        target_feed = cr.feed_setpoint if cr.running else 0.0
        cr.feed_rate += (target_feed - cr.feed_rate) * min(1.0, dt / 30.0)
        if cr.feed_rate < 1:
            cr.feed_rate = 0.0 if not cr.running else cr.feed_rate

        spinning = cr.state in (State.RUNNING, State.STARTING)
        idle = 42.0 if spinning else 0.0
        cr.motor_current = idle + 0.36 * cr.feed_rate * cr.hardness + self.rng.gauss(0, 2.0) * spinning
        cr.motor_current = max(0.0, cr.motor_current)

        heat_target = AMBIENT_C + (0.17 * cr.motor_current) * (1 + 0.4 * cr.wear)
        cr.bearing_temp += (heat_target - cr.bearing_temp) * min(1.0, dt / 900.0)
        base_vib = (1.6 + 2.2 * cr.wear) if spinning else 0.1
        cr.vibration = max(0.05, base_vib * (0.6 + 0.4 * cr.hardness) + self.rng.gauss(0, 0.12))

        if cr.running:
            if cr.motor_current > CR_OVERLOAD_AMPS:
                cr.overload_timer += dt
                if cr.overload_timer >= CR_OVERLOAD_SECONDS:
                    self._trip(cr, Fault.CR_OVERLOAD)
            else:
                cr.overload_timer = 0.0
            if cr.bearing_temp >= CR_BEARING_TRIP_C:
                self._trip(cr, Fault.CR_BEARING_OVERTEMP)

    def _random_faults(self, dt: float) -> None:
        for fault, (mtbf_h, _mttr) in FAULT_RATES.items():
            eq = self.crusher if fault < 200 else self.conveyor
            if eq.running and self.rng.random() < dt / (mtbf_h * 3600):
                self._trip(eq, fault)
                if eq is self.conveyor and self.crusher.state in (State.RUNNING, State.STARTING):
                    self._trip(self.crusher, Fault.CR_DOWNSTREAM_INTERLOCK, repair_s=0)

    def _maybe_planned_stop(self) -> None:
        if self.clock >= self._next_planned:
            for eq in (self.crusher, self.conveyor):
                if eq.state != State.FAULTED:
                    self._trip(eq, Fault.PLANNED_STOP)
            self._next_planned = self._schedule_planned()

    def _schedule_planned(self) -> float:
        if not self.planned_stops_per_day:
            return math.inf
        gap = 86400 / self.planned_stops_per_day
        return self.clock + gap * self.rng.uniform(0.8, 1.2)

    def _operator(self, dt: float) -> None:
        """A sensible control-room operator: reset cleared faults, start downstream first."""
        for eq in (self.conveyor, self.crusher):
            if eq.state == State.FAULTED and eq.repair_left == 0:
                self.reset(eq.tag)
        cv, cr = self.conveyor, self.crusher
        if cv.state == State.STOPPED and cv.state_timer > 30:
            self.start(cv.tag)
        if cr.state == State.STOPPED and cv.running and cr.state_timer > 30:
            # after a bearing trip, let it cool before restarting
            if cr.bearing_temp < CR_BEARING_TRIP_C - 15:
                self.start(cr.tag)

    # ------------------------------------------------------------------ helpers
    def _trip(self, eq: Equipment, fault: Fault, repair_s: float | None = None) -> None:
        if repair_s is None:
            mean = REPAIR_MINUTES.get(fault, 10.0) * 60
            repair_s = self.rng.lognormvariate(math.log(mean), 0.45)
        eq.fault = fault
        eq.repair_left = repair_s
        self._set_state(eq, State.FAULTED)
        if isinstance(eq, Crusher):
            eq.overload_timer = 0.0
        if fault == Fault.CR_BEARING_OVERTEMP and isinstance(eq, Crusher):
            eq.wear = 0.15  # bearing replaced during the repair

    def _set_state(self, eq: Equipment, state: State) -> None:
        if eq.state != state:
            self.events.append((self.clock, eq.tag, int(state), int(eq.fault)))
        eq.state = state
        eq.state_timer = 0.0

    def _eq(self, tag: str) -> Equipment:
        if tag == self.crusher.tag:
            return self.crusher
        if tag == self.conveyor.tag:
            return self.conveyor
        raise KeyError(tag)

    # ------------------------------------------------------------------ tag snapshot
    def tags(self) -> dict[str, float | int | bool]:
        cr, cv = self.crusher, self.conveyor
        return {
            "CR101.Running": cr.running,
            "CR101.State": int(cr.state),
            "CR101.FaultCode": int(cr.fault),
            "CR101.FeedRate": round(cr.feed_rate, 2),
            "CR101.MotorCurrent": round(cr.motor_current, 2),
            "CR101.BearingTemp": round(cr.bearing_temp, 2),
            "CR101.Vibration": round(cr.vibration, 3),
            "CV101.Running": cv.running,
            "CV101.State": int(cv.state),
            "CV101.FaultCode": int(cv.fault),
            "CV101.BeltSpeed": round(cv.belt_speed, 3),
            "CV101.MotorCurrent": round(cv.motor_current, 2),
            "CV101.Throughput": round(cv.throughput, 2),
            "CV101.TotalTonnes": round(cv.total_tonnes, 3),
        }
