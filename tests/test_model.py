from collections import Counter

from plantdata.sim.model import CR_OVERLOAD_AMPS, Fault, Plant, State


def run(plant, seconds, step=1.0):
    for _ in range(int(seconds / step)):
        plant.step(step)


def test_starts_up_and_runs():
    plant = Plant(seed=1, planned_stops_per_day=0)
    run(plant, 300)
    assert plant.conveyor.state == State.RUNNING
    assert plant.crusher.state == State.RUNNING
    assert 400 < plant.crusher.feed_rate < 460
    assert abs(plant.conveyor.belt_speed - 2.5) < 0.05


def test_is_deterministic():
    a, b = Plant(seed=9), Plant(seed=9)
    run(a, 7200, 5)
    run(b, 7200, 5)
    assert a.tags() == b.tags()
    assert a.events == b.events


def test_crusher_needs_conveyor_running():
    plant = Plant(seed=1, auto_restart=False)
    assert plant.start("CR101") is False        # conveyor still stopped
    assert plant.start("CV101") is True


def test_conveyor_stop_trips_crusher_interlock():
    plant = Plant(seed=1, planned_stops_per_day=0, auto_restart=False)
    plant.start("CV101")
    run(plant, 30)
    plant.start("CR101")
    run(plant, 60)
    plant.stop("CV101")
    assert plant.crusher.state == State.FAULTED
    assert plant.crusher.fault == Fault.CR_DOWNSTREAM_INTERLOCK


def test_overload_trip_after_sustained_high_current():
    plant = Plant(seed=1, planned_stops_per_day=0, auto_restart=False)
    plant.start("CV101")
    run(plant, 30)
    plant.start("CR101")
    run(plant, 200)
    plant.crusher.feed_setpoint = 650           # far beyond what the motor can take
    run(plant, 300)
    assert plant.crusher.state == State.FAULTED
    assert plant.crusher.fault == Fault.CR_OVERLOAD
    assert max(t for t in [plant.crusher.motor_current]) < CR_OVERLOAD_AMPS + 100


def test_reset_only_after_repair():
    plant = Plant(seed=1, auto_restart=False)
    plant._trip(plant.conveyor, Fault.CV_BELT_MISALIGNMENT, repair_s=120)
    assert plant.reset("CV101") is False
    run(plant, 121)
    assert plant.reset("CV101") is True
    assert plant.conveyor.state == State.STOPPED and plant.conveyor.fault == Fault.NONE


def test_totaliser_matches_integrated_throughput():
    plant = Plant(seed=3)
    integrated = 0.0
    for _ in range(4 * 3600):
        plant.step(1.0)
        integrated += plant.conveyor.throughput / 3600
    assert abs(plant.conveyor.total_tonnes - integrated) < 1e-6


def test_three_days_look_like_a_real_plant():
    plant = Plant(seed=7)
    seconds = Counter()
    for _ in range(3 * 86400 // 5):
        plant.step(5)
        seconds[plant.crusher.state] += 5
    availability = seconds[State.RUNNING] / sum(seconds.values())
    assert 0.75 < availability < 0.97
    faults = Counter(e[3] for e in plant.events if e[2] == State.FAULTED)
    assert faults[Fault.PLANNED_STOP] >= 4
    assert sum(faults.values()) >= 10
