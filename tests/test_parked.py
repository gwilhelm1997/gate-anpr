import pytest

from gate_anpr.decision import Frame, PlateRead
from gate_anpr.parked import ParkedFilter
from gate_anpr.store import Lane
from test_service import lane_data, make  # noqa: F401 - pytest fixture reuse


def plate(text, x, width=120, y=1400):
    return PlateRead(text, text, 0.97, 0.9, (x, y, x + width, y + width // 4))


def test_exclusion_zone_reads_are_ignored():
    f = ParkedFilter()
    kept, ignored = f.keep([plate("PARK01", 200), plate("ABC123", 1800)], 3840, 2160,
                           [(0.0, 0.0, 0.2, 1.0)], 2, 1000.0)
    assert [r.text for r in kept] == ["ABC123"] and ignored == ["PARK01: exclusion zone"]


def test_plate_at_the_same_spot_minutes_ago_is_parked():
    f = ParkedFilter()
    f.remember([Frame(0, 0, 3840, 2160, [plate("PARK01", 600)])], at=1000.0)
    # Two minutes later, same spot: parked. A moved or different plate is kept.
    kept, ignored = f.keep([plate("PARK01", 605), plate("ABC123", 1800)], 3840, 2160, [], 2, 1000.0 + 121)
    assert [r.text for r in kept] == ["ABC123"] and ignored == ["PARK01: parked"]
    kept, _ = f.keep([plate("PARK01", 2400)], 3840, 2160, [], 2, 1000.0 + 121)
    assert [r.text for r in kept] == ["PARK01"]  # same car, moved: it is driving again


def test_recently_seen_plate_is_not_yet_parked():
    f = ParkedFilter()
    f.remember([Frame(0, 0, 3840, 2160, [plate("ABC123", 600)])], at=1000.0)
    kept, _ = f.keep([plate("ABC123", 600)], 3840, 2160, [], 2, 1000.0 + 30)  # car waiting at the gate
    assert [r.text for r in kept] == ["ABC123"]


def test_parked_filter_off():
    f = ParkedFilter()
    f.remember([Frame(0, 0, 3840, 2160, [plate("PARK01", 600)])], at=0.0)
    assert len(f.keep([plate("PARK01", 600)], 3840, 2160, [], 0, 10_000.0)[0]) == 1


@pytest.mark.parametrize("value", ["0,0,30", "0,0,30,140", "a,b,c,d", "0,0,10,10;"])
def test_bad_exclusions_rejected(value):
    with pytest.raises(ValueError):
        Lane.coerce("exclusions", value)


def test_exclusions_parse():
    assert Lane.coerce("exclusions", "0,0,30,40; 70, 0,100,30") == "0,0,30,40;70,0,100,30"
    lane = Lane(exclusions="30,40,0,0")
    assert lane.exclusion_boxes() == [(0.0, 0.0, 0.3, 0.4)]


async def test_parked_approved_car_never_opens_for_an_arriving_stranger(make, fake_ha):  # noqa: F811
    """An approved car parked (closer to the camera) must not open the gate when an unknown car arrives."""
    parked = PlateRead("ABC123", "ABC123", 0.97, 0.9, (600, 1500, 900, 1575))  # big: nearest to the camera
    stranger_far, stranger_near = (PlateRead("ZZZ999", "ZZZ999", 0.97, 0.9, (2000, 1300, 2000 + w, 1300 + w // 4))
                                   for w in (100, 120))
    svc = make([], auto_open=True)
    svc.engine.table.update({b"p": [parked], b"p+far": [parked, stranger_far], b"p+near": [parked, stranger_near]})
    rt = svc.lanes[1]
    rt.parked.remember([Frame(0, 0, 3840, 2160, [parked])], at=0.0)  # seen at that spot long ago
    fake_ha.frames = [b"p+far", b"p+near", b"p+near", b"p+near"]
    event = await svc.run_session(1, test=False)
    assert event["plate"] == "ZZZ999" and event["reason"] == "not_approved" and not fake_ha.calls
    assert "ABC123: parked" in event["detail"]["ignored"]
