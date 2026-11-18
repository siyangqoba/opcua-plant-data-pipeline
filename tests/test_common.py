from plantdata.common import TAGS, DeadbandFilter
from plantdata.db import tag_defs


def test_deadband_passes_only_real_changes():
    f = DeadbandFilter({"A": 2.0}, max_silence=600)
    assert f.accept("A", 100.0, 0)
    assert not f.accept("A", 101.5, 1)
    assert f.accept("A", 102.5, 2)      # moved 2.5 from last stored value
    assert not f.accept("A", 101.0, 3)  # within band of 102.5


def test_discrete_tags_pass_any_change():
    f = DeadbandFilter({"S": 0.0})
    assert f.accept("S", 2, 0)
    assert not f.accept("S", 2, 1)
    assert f.accept("S", 3, 2)


def test_heartbeat_forces_a_value_through():
    f = DeadbandFilter({"A": 5.0}, max_silence=900)
    assert f.accept("A", 10, 0)
    assert not f.accept("A", 10, 899)
    assert f.accept("A", 10, 900)


def test_config_overrides_deadband():
    tags = tag_defs(["CR101.State", {"name": "CV101.TotalTonnes", "deadband": 2.0}])
    assert tags[1].deadband == 2.0
    assert tags[0].is_discrete
    assert len({t.name for t in TAGS}) == len(TAGS)
