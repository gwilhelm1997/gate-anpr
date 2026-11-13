import ipaddress
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gate_anpr.store import Lane, Store
from gate_anpr.web import create_app


def lane(**kw):
    return Lane.from_dict({"name": "Outside gate", "camera_entity": "camera.gate_cam_snapshots_clear",
                           "trigger_entity": "binary_sensor.gate_cam_vehicle",
                           "gate_entity": "switch.sonoff_100219c178_2", **kw})


def test_lanes_default_to_safe_and_persist(tmp_path):
    store = Store(tmp_path)
    saved = store.save_lane(lane())
    assert saved.id == 1 and saved.auto_open is False and saved.access == "approved"
    saved.auto_open, saved.cooldown = True, 120
    store.save_lane(saved)
    store.close()
    reloaded = Store(tmp_path).lanes()[0]
    assert reloaded.auto_open is True and reloaded.cooldown == 120


def test_single_gate_settings_migrate_to_first_lane(tmp_path):
    store = Store(tmp_path)
    with store._db:  # what 0.1.x left behind
        store._db.execute("INSERT INTO settings VALUES ('auto_open', 'true'), ('min_confidence', '85'),"
                          " ('recognition_enabled', 'false')")
    store.migrate({"camera_entity": "camera.gate_cam_snapshots_clear", "live_camera_entity": "",
                   "trigger_entity": "binary_sensor.gate_cam_vehicle",
                   "gate_entity": "switch.sonoff_100219c178_2", "gate_position_entity": ""})
    (first,) = store.lanes()
    assert first.name == "Outside gate" and first.auto_open is True and first.min_confidence == 85
    assert first.enabled is False and first.camera_entity == "camera.gate_cam_snapshots_clear"
    store.migrate({"camera_entity": "camera.other"})  # only ever once
    assert len(store.lanes()) == 1


@pytest.mark.parametrize("key,value", [("min_confidence", 101), ("min_consistent", 1), ("auto_open", "maybe"),
                                       ("nope", 1), ("gate_entity", "light.porch"), ("access", "everyone"),
                                       ("direction", "sideways"), ("name", "")])
def test_lane_validation(key, value):
    with pytest.raises(ValueError):
        Lane.coerce(key, value)


def test_plates_are_normalised_and_validated(tmp_path):
    store = Store(tmp_path)
    assert store.upsert_plate(" abc-123 ", "Ute") == "ABC123"
    assert store.is_approved("ABC123", 1)["label"] == "Ute"
    assert store.is_approved("ABC12", 1) is None
    store.upsert_plate("ABC123", "Ute", enabled=False)
    assert store.is_approved("ABC123", 1) is None
    with pytest.raises(ValueError):
        store.upsert_plate("TOOLONG1")


def test_prune_removes_old_events_and_images(tmp_path):
    store = Store(tmp_path)
    old = store.add_event({"ts": time.time() - 40 * 86400, "result": "rejected", "reason": "x", "granted": False})
    (store.images / "0000001.jpg").write_bytes(b"x")
    store.set_event_images(old, "0000001.jpg", None)
    store.add_event({"ts": time.time(), "result": "rejected", "reason": "x", "granted": False})
    assert store.prune(30) == 1
    assert len(store.events()) == 1 and not (store.images / "0000001.jpg").exists()


class Svc:
    def __init__(self, store):
        self.store = store
        self.lanes = {}
        self.engine = type("E", (), {"info": lambda self: {}})()
        self.changed = 0

    def diagnostics(self, rt=None):
        return {"home_assistant_connected": True}

    async def plates_changed(self):
        self.changed += 1


async def client(tmp_path, allowed):
    svc = Svc(Store(tmp_path))
    c = TestClient(TestServer(create_app(svc, {ipaddress.ip_address(a) for a in allowed})))
    await c.start_server()
    return c, svc


async def test_requests_not_from_supervisor_are_refused(tmp_path):
    c, _ = await client(tmp_path, ["172.30.32.2"])
    try:
        assert (await c.get("/api/state")).status == 403
    finally:
        await c.close()


async def test_plate_management_api(tmp_path):
    c, svc = await client(tmp_path, ["127.0.0.1"])
    try:
        r = await c.post("/api/plates", json={"plate": "kdx 472", "label": "Car"})
        assert (await r.json())["plate"] == "KDX472" and svc.changed == 1
        assert (await c.post("/api/plates", json={"plate": "BAD!PLATE7"})).status == 400
        assert (await c.post("/api/plates", json={"plate": "ABC1", "lanes": [7]})).status == 400
        assert (await c.post("/api/plates", data="plate=ABC")).status == 415  # no cross-site form posts
        state = await (await c.get("/api/state")).json()
        assert [p["plate"] for p in state["plates"]] == ["KDX472"]
        assert (await c.delete("/api/plates/KDX472", json={})).status == 200
        assert (await c.delete("/api/plates/KDX472", json={})).status == 404
        assert (await c.post("/api/lanes/9/test", json={})).status == 404
        assert (await c.get("/images/..%2Fgate_anpr.db")).status == 404
    finally:
        await c.close()


def test_plate_can_be_limited_to_lanes(tmp_path):
    store = Store(tmp_path)
    store.upsert_plate("ABC123", lanes=[2])
    assert store.is_approved("ABC123", 2) and store.is_approved("ABC123", 1) is None
    store.upsert_plate("ABC123", lanes=[])
    assert store.is_approved("ABC123", 1)


def test_entity_pickers_hide_toggle_inputs():
    from gate_anpr.web import entity_choices
    states = [{"entity_id": "switch.sonoff_100219c178_2", "attributes": {"friendly_name": "Outside gate control OPEN"}},
              {"entity_id": "switch.sonoff_100219c178_4", "attributes": {"friendly_name": "Outside Gate toggle"}},
              {"entity_id": "switch.sonoff_100219c178_3", "attributes": {"friendly_name": "Outside gate control CLOSE"}},
              {"entity_id": "switch.sonoff_100219c178_1", "attributes": {"friendly_name": "Outside gate control STOP"}},
              {"entity_id": "switch.gate_anpr_outside_gate_automatic_opening", "attributes": {}},
              {"entity_id": "binary_sensor.gate_cam_vehicle", "attributes": {}},
              {"entity_id": "binary_sensor.gate_cam_person", "attributes": {}},
              {"entity_id": "camera.gate_cam_snapshots_clear", "attributes": {}}]
    c = entity_choices(states)
    assert [g["id"] for g in c["gates"]] == ["switch.sonoff_100219c178_2"]
    assert [t["id"] for t in c["triggers"]] == ["binary_sensor.gate_cam_vehicle"]


def test_prune_sweeps_orphan_and_expired_images(tmp_path):
    import os
    store = Store(tmp_path)
    orphan_old, orphan_new, stale = (store.images / n for n in ("orphan.jpg", "fresh.jpg", "stale.jpg"))
    for p in (orphan_old, orphan_new, stale):
        p.write_bytes(b"x")
    os.utime(orphan_old, (time.time() - 7200,) * 2)
    os.utime(stale, (time.time() - 40 * 86400,) * 2)
    store.prune(30)
    assert not orphan_old.exists() and not stale.exists() and orphan_new.exists()
