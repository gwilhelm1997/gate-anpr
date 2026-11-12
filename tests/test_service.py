import asyncio

import pytest

from gate_anpr.decision import PlateRead
from gate_anpr.gate import GateController
from gate_anpr.service import GateAnpr, Options
from gate_anpr.store import Store

RELAY = "switch.sonoff_100219c178_2"


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
        reads = self.table.get(jpeg, [])
        return 3840, 2160, reads, 42

    async def annotate(self, jpeg, box, label, region, max_width=1280):
        return b"\xff\xd8frame", b"\xff\xd8plate" if box else None

    def info(self):
        return {"ready": self.ready}


class FakeMqtt:
    def __init__(self, connected=True):
        self.connected = connected
        self.published = {}

    async def publish(self, key, value, attributes=None):
        self.published[key] = (value, attributes)

    async def publish_image(self, key, jpeg):
        self.published[key] = jpeg


def plate(text, width, conf=0.97):
    return PlateRead(text, text, conf, 0.9, (1800, 1400, 1800 + width, 1400 + width // 4))


TABLE = {
    b"far": [plate("ABC123", 100)],
    b"near": [plate("ABC123", 120)],
    b"stranger": [plate("ZZZ999", 120)],
    b"blur": [plate("ABC123", 120, conf=0.5)],
    b"empty": [],
}


@pytest.fixture
def make(tmp_path, fake_ha):
    def factory(frames, auto_open=False, mqtt=True):
        fake_ha.frames = list(frames)
        fake_ha.set(RELAY, "off", friendly_name="Outside gate control OPEN")
        store = Store(tmp_path)
        store.upsert_plate("ABC 123", "Will's ute")
        gate = GateController(fake_ha, RELAY, confirm_timeout=1, poll_interval=0.01)
        options = Options("camera.gate_cam_snapshots_clear", "binary_sensor.gate_cam_vehicle", RELAY,
                          max_frames=4, capture_window=5, frame_interval=0)
        svc = GateAnpr(options, store, FakeEngine(TABLE), fake_ha, gate, FakeMqtt() if mqtt else None)
        svc.settings.auto_open = auto_open
        return svc
    return factory


async def test_log_only_mode_never_operates_gate(make, fake_ha):
    svc = make([b"far", b"near"])
    event = await svc.run_session(test=False)
    assert event["plate"] == "ABC123" and event["result"] == "approved_monitor"
    assert fake_ha.calls == []
    assert fake_ha.events[0][0] == "gate_anpr_access"
    assert svc.store.events()[0]["image"] == f"{event['id']:07d}.jpg"


async def test_approved_vehicle_opens_gate_once(make, fake_ha):
    svc = make([b"far", b"near", b"near"], auto_open=True)
    event = await svc.run_session(test=False)
    assert event["result"] == "granted" and event["reason"] == "opened"
    assert event["label"] == "Will's ute"
    assert fake_ha.calls == [("switch", "turn_on", RELAY)]
    assert svc.engine.calls == 2  # stopped capturing as soon as two frames agreed
    assert svc.mqtt.published["last_plate"][0] == "ABC123"


async def test_unknown_vehicle_is_rejected(make, fake_ha):
    event = await make([b"stranger", b"stranger"], auto_open=True).run_session(test=False)
    assert event["result"] == "rejected" and event["reason"] == "not_approved" and not fake_ha.calls


async def test_blurred_plate_is_rejected(make, fake_ha):
    event = await make([b"blur"] * 4, auto_open=True).run_session(test=False)
    assert event["reason"] == "low_confidence" and not fake_ha.calls


async def test_departing_vehicle_is_rejected(make, fake_ha):
    event = await make([b"near", b"far"], auto_open=True).run_session(test=False)
    assert event["reason"] == "departing" and not fake_ha.calls


async def test_camera_failure_fails_closed(make, fake_ha):
    svc = make([], auto_open=True)
    fake_ha.fail_camera = True
    event = await svc.run_session(test=False)
    assert event["result"] == "error" and not fake_ha.calls


async def test_engine_not_ready_fails_closed(make, fake_ha):
    svc = make([b"far", b"near"], auto_open=True)
    svc.engine.ready = False
    assert (await svc.run_session(test=False))["result"] == "error" and not fake_ha.calls


async def test_test_mode_never_operates_gate(make, fake_ha):
    event = await make([b"far", b"near"], auto_open=True).run_session(test=True)
    assert event["result"] == "approved_test" and not fake_ha.calls


async def test_no_mqtt_means_no_automatic_opening(make, fake_ha):
    svc = make([b"far", b"near"], auto_open=True)
    svc.mqtt.connected = False
    event = await svc.run_session(test=False)
    assert event["reason"] == "controls_offline" and not fake_ha.calls


async def test_ha_link_down_means_no_opening(make, fake_ha):
    svc = make([b"far", b"near"], auto_open=True)
    fake_ha.connected = False
    assert (await svc.run_session(test=False))["reason"] == "ha_disconnected" and not fake_ha.calls


async def test_overlapping_triggers_run_one_session(make, fake_ha):
    svc = make([b"far", b"near", b"far", b"near"], auto_open=True)
    first, second = await asyncio.gather(svc.run_session(False), svc.run_session(False))
    assert second is None and first["result"] == "granted" and len(fake_ha.calls) == 1


async def test_repeat_arrival_within_cooldown_is_not_reopened(make, fake_ha):
    svc = make([b"far", b"near", b"far", b"near"], auto_open=True)
    assert (await svc.run_session(False))["reason"] == "opened"
    assert (await svc.run_session(False))["reason"] == "cooldown"
    assert len(fake_ha.calls) == 1


async def test_recognition_switch_off_ignores_vehicles(make, fake_ha):
    svc = make([b"far", b"near"], auto_open=True)
    svc.settings.recognition_enabled = False
    await svc.on_vehicle()
    assert svc.engine.calls == 0 and not svc.store.events()


async def test_invalid_mqtt_setting_snaps_back(make):
    svc = make([])
    await svc.on_mqtt_command("min_confidence", "20")
    assert svc.settings.min_confidence == 90 and svc.mqtt.published["min_confidence"][0] == 90
    await svc.on_mqtt_command("auto_open", "ON")
    assert svc.settings.auto_open is True and svc.store.load_settings().auto_open is True
