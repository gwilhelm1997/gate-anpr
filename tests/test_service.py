import asyncio

import pytest

from gate_anpr.decision import PlateRead
from gate_anpr.service import GateAnpr, Options
from gate_anpr.store import Lane, Store

RELAY = "switch.sonoff_100219c178_2"
RELAY2 = "switch.sonoff_100219c097_1"


class FakeEngine:
    """Each camera 'jpeg' is a key into a table of plate reads."""

    def __init__(self, table):
        self.table = table
        self.ready = True
        self.last_error = None
        self.calls = 0

    async def start(self):
        pass  # stays not-ready when a test makes it so

    async def recognise(self, jpeg, region):
        self.calls += 1
        return 3840, 2160, self.table.get(jpeg, []), 42

    async def annotate(self, jpeg, box, label, region, max_width=1280):
        return b"\xff\xd8frame", b"\xff\xd8plate" if box else None

    def info(self):
        return {"ready": self.ready}


class FakeMqtt:
    def __init__(self, connected=True):
        self.connected = connected
        self.can_send = connected
        self.published = {}
        self.sent = {}

    async def publish(self, key, value, attributes=None):
        self.published[key] = (value, attributes)

    async def publish_image(self, key, jpeg):
        self.published[key] = jpeg

    async def send(self, topic, payload):
        self.sent[topic] = payload


def plate(text, width, conf=0.97):
    return PlateRead(text, text, conf, 0.9, (1800, 1400, 1800 + width, 1400 + width // 4))


TABLE = {
    b"far": [plate("ABC123", 100)],
    b"near": [plate("ABC123", 120)],
    b"stranger": [plate("ZZZ999", 120)],
    b"stranger_far": [plate("ZZZ999", 100)],
    b"blur": [plate("ABC123", 120, conf=0.5)],
}


def lane_data(name="Outside gate", gate=RELAY, trigger="binary_sensor.gate_cam_vehicle", **kw):
    return {"name": name, "camera_entity": "camera.gate_cam_snapshots_clear", "trigger_entity": trigger,
            "gate_entity": gate, **kw}


@pytest.fixture
def make(tmp_path, fake_ha):
    def factory(frames, auto_open=False, mqtt=True, lanes=None):
        fake_ha.frames = list(frames)
        for relay, name in ((RELAY, "Outside gate control OPEN"), (RELAY2, "Inside gate Control OPEN")):
            fake_ha.set(relay, "off", friendly_name=name)
        store = Store(tmp_path)
        for data in lanes or [lane_data()]:
            store.save_lane(Lane.from_dict({"auto_open": auto_open, **data}))
        store.upsert_plate("ABC 123", "Will's ute")
        options = Options(max_frames=4, capture_window=5, frame_interval=0, confirm_timeout=1)
        return GateAnpr(options, store, FakeEngine(TABLE), fake_ha, FakeMqtt() if mqtt else None, gate_poll=0.01)
    return factory


async def test_log_only_mode_never_operates_gate(make, fake_ha):
    svc = make([b"far", b"near"])
    event = await svc.run_session(1, test=False)
    assert event["plate"] == "ABC123" and event["result"] == "approved_monitor" and event["lane"] == "Outside gate"
    assert fake_ha.calls == []
    assert fake_ha.events[0][0] == "gate_anpr_access" and fake_ha.events[0][1]["lane_id"] == 1
    assert svc.store.events()[0]["image"] == f"{event['id']:07d}.jpg"


async def test_approved_vehicle_opens_gate_once(make, fake_ha):
    svc = make([b"far", b"near", b"near"], auto_open=True)
    event = await svc.run_session(1, test=False)
    assert event["result"] == "granted" and event["reason"] == "opened" and event["label"] == "Will's ute"
    assert fake_ha.calls == [("switch", "turn_on", RELAY)]
    assert svc.engine.calls == 2  # stopped capturing as soon as two frames agreed
    assert svc.mqtt.published["l1_last_plate"][0] == "ABC123"


async def test_vehicle_sensor_starts_only_matching_enabled_lanes(make, fake_ha):
    svc = make([b"far", b"near"], auto_open=True, lanes=[
        lane_data(), lane_data("Inside gate", RELAY2, "binary_sensor.gate2_cam_vehicle")])
    await svc.on_vehicle("binary_sensor.gate2_cam_vehicle")
    await asyncio.sleep(0.3)
    assert fake_ha.calls == [("switch", "turn_on", RELAY2)]
    assert svc.store.events()[0]["lane"] == "Inside gate"


async def test_unknown_vehicle_is_rejected(make, fake_ha):
    event = await make([b"stranger_far", b"stranger"], auto_open=True).run_session(1, test=False)
    assert event["result"] == "rejected" and event["reason"] == "not_approved" and not fake_ha.calls


async def test_allow_all_lane_opens_for_any_consistent_plate(make, fake_ha):
    svc = make([b"stranger_far", b"stranger"], auto_open=True, lanes=[lane_data(access="all")])
    event = await svc.run_session(1, test=False)
    assert event["result"] == "granted" and "allow-all" in event["detail"]["message"]
    assert fake_ha.calls == [("switch", "turn_on", RELAY)]


async def test_allow_all_still_needs_a_consistent_read(make, fake_ha):
    svc = make([b"blur"] * 4, auto_open=True, lanes=[lane_data(access="all")])
    assert (await svc.run_session(1, test=False))["reason"] == "low_confidence" and not fake_ha.calls


async def test_plate_limited_to_other_gate_is_rejected(make, fake_ha):
    svc = make([b"far", b"near"], auto_open=True, lanes=[
        lane_data(), lane_data("Inside gate", RELAY2, "binary_sensor.gate2_cam_vehicle")])
    svc.store.upsert_plate("ABC123", "Will's ute", lanes=[2])
    assert (await svc.run_session(1, test=False))["reason"] == "not_approved" and not fake_ha.calls


async def test_lanes_sharing_a_gate_share_its_cooldown(make, fake_ha):
    svc = make([b"far", b"near", b"far", b"near"], auto_open=True, lanes=[
        lane_data(), lane_data("Second camera", RELAY, "binary_sensor.gate2_cam_vehicle")])
    assert (await svc.run_session(1, False))["reason"] == "opened"
    assert (await svc.run_session(2, False))["reason"] == "cooldown"
    assert len(fake_ha.calls) == 1


async def test_blurred_plate_is_rejected(make, fake_ha):
    event = await make([b"blur"] * 4, auto_open=True).run_session(1, test=False)
    assert event["reason"] == "low_confidence" and not fake_ha.calls


async def test_wrong_direction_is_rejected(make, fake_ha):
    event = await make([b"near", b"far"], auto_open=True).run_session(1, test=False)
    assert event["reason"] == "wrong_direction" and not fake_ha.calls


async def test_away_lane_accepts_shrinking_plate(make, fake_ha):
    svc = make([b"near", b"far"], auto_open=True, lanes=[lane_data(direction="away")])
    assert (await svc.run_session(1, test=False))["result"] == "granted"


async def test_camera_failure_fails_closed(make, fake_ha):
    svc = make([], auto_open=True)
    fake_ha.fail_camera = True
    assert (await svc.run_session(1, test=False))["result"] == "error" and not fake_ha.calls


async def test_engine_not_ready_fails_closed(make, fake_ha):
    svc = make([b"far", b"near"], auto_open=True)
    svc.engine.ready = False
    assert (await svc.run_session(1, test=False))["result"] == "error" and not fake_ha.calls


async def test_test_mode_never_operates_gate(make, fake_ha):
    event = await make([b"far", b"near"], auto_open=True).run_session(1, test=True)
    assert event["result"] == "approved_test" and not fake_ha.calls


async def test_no_mqtt_means_no_automatic_opening(make, fake_ha):
    svc = make([b"far", b"near"], auto_open=True)
    svc.mqtt.connected = False
    assert (await svc.run_session(1, test=False))["reason"] == "controls_offline" and not fake_ha.calls


async def test_ha_link_down_means_no_opening(make, fake_ha):
    svc = make([b"far", b"near"], auto_open=True)
    fake_ha.connected = False
    assert (await svc.run_session(1, test=False))["reason"] == "ha_disconnected" and not fake_ha.calls


async def test_overlapping_triggers_run_one_session(make, fake_ha):
    svc = make([b"far", b"near", b"far", b"near"], auto_open=True)
    first, second = await asyncio.gather(svc.run_session(1, False), svc.run_session(1, False))
    assert second is None and first["result"] == "granted" and len(fake_ha.calls) == 1


async def test_recognition_switch_off_ignores_vehicles(make, fake_ha):
    svc = make([b"far", b"near"], auto_open=True, lanes=[lane_data(enabled=False)])
    await svc.on_vehicle("binary_sensor.gate_cam_vehicle")
    await asyncio.sleep(0.1)
    assert svc.engine.calls == 0 and not svc.store.events()


async def test_mqtt_controls_are_per_lane_and_validated(make):
    svc = make([])
    await svc.on_mqtt_command("l1_min_confidence", "20")
    assert svc.lanes[1].lane.min_confidence == 90 and svc.mqtt.published["l1_min_confidence"][0] == 90
    await svc.on_mqtt_command("l1_auto_open", "ON")
    assert svc.lanes[1].lane.auto_open is True and svc.store.lanes()[0].auto_open is True
    await svc.on_mqtt_command("l9_auto_open", "ON")  # unknown lane: ignored


async def test_adding_and_deleting_lanes_updates_entities(make):
    svc = make([])
    changed = []

    async def hook():
        changed.append(True)
    svc.on_lanes_changed = hook
    lane = await svc.save_lane(lane_data("Inside gate", RELAY2, "binary_sensor.gate2_cam_vehicle"))
    assert lane.id == 2 and lane.auto_open is False and svc.triggers() == {
        "binary_sensor.gate_cam_vehicle", "binary_sensor.gate2_cam_vehicle"}
    assert "homeassistant/switch/gate_anpr/l2_auto_open/config" in svc.mqtt.sent
    assert svc.mqtt.sent["homeassistant/switch/gate_anpr/auto_open/config"] == ""  # 0.1 entity removed
    await svc.delete_lane(2)
    assert svc.mqtt.sent["homeassistant/switch/gate_anpr/l2_auto_open/config"] == "" and len(changed) == 2


async def test_invalid_lane_is_refused(make):
    svc = make([])
    with pytest.raises(ValueError):
        await svc.save_lane(lane_data(gate="light.porch"))
    with pytest.raises(ValueError):
        await svc.save_lane({"name": "No camera"})


async def test_parallel_snapshots_halve_time_to_decision(make, fake_ha):
    timings = {}
    for parallel in (1, 2):
        svc = make([])
        frames = [b"far", b"near", b"near", b"near"]

        async def camera(entity_id, frames=frames):
            await asyncio.sleep(0.4)  # a 4K Reolink snapshot takes about this long
            return frames.pop(0)
        fake_ha.camera_image = camera
        svc.options = Options(max_frames=4, capture_window=5, frame_interval=0.4, parallel_snapshots=parallel,
                              confirm_timeout=1)
        event = await svc.run_session(1, test=True)
        assert event["result"] == "approved_test"
        timings[parallel] = event["detail"]["duration_s"]
    # Two 0.4 s snapshots: 0.8 s one after the other, about 0.55 s with two in flight.
    assert timings[1] >= 0.78 and timings[2] <= 0.65, timings
