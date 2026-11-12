import ipaddress
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gate_anpr.store import Settings, Store
from gate_anpr.web import create_app


def test_settings_default_to_safe_and_persist(tmp_path):
    store = Store(tmp_path)
    assert store.load_settings().auto_open is False
    store.save_setting("auto_open", True)
    store.save_setting("cooldown", 120)
    store.close()
    reloaded = Store(tmp_path).load_settings()
    assert reloaded.auto_open is True and reloaded.cooldown == 120


def test_corrupt_stored_value_falls_back_to_default(tmp_path):
    store = Store(tmp_path)
    store.save_setting("min_confidence", 5)  # out of range
    assert store.load_settings().min_confidence == 90


@pytest.mark.parametrize("key,value", [("min_confidence", 101), ("min_consistent", 1), ("auto_open", "maybe"),
                                       ("nope", 1)])
def test_setting_validation(key, value):
    with pytest.raises(ValueError):
        Settings.coerce(key, value)


def test_plates_are_normalised_and_validated(tmp_path):
    store = Store(tmp_path)
    assert store.upsert_plate(" abc-123 ", "Ute") == "ABC123"
    assert store.is_approved("ABC123")["label"] == "Ute"
    assert store.is_approved("ABC12") is None
    store.upsert_plate("ABC123", "Ute", enabled=False)
    assert store.is_approved("ABC123") is None
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
        self.settings = store.load_settings()
        self.status = "idle"
        self.engine = type("E", (), {"info": lambda self: {}})()
        self.changed = 0

    def diagnostics(self):
        return {"home_assistant_connected": True}

    async def plates_changed(self):
        self.changed += 1

    async def set_setting(self, key, value):
        setattr(self.settings, key, Settings.coerce(key, value))


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
        assert (await c.post("/api/plates", data="plate=ABC")).status == 415  # no cross-site form posts
        state = await (await c.get("/api/state")).json()
        assert [p["plate"] for p in state["plates"]] == ["KDX472"]
        assert (await c.delete("/api/plates/KDX472", json={})).status == 200
        assert (await c.delete("/api/plates/KDX472", json={})).status == 404
        assert (await c.post("/api/settings", json={"cooldown": 5})).status == 400
        assert (await c.get("/images/..%2Fgate_anpr.db")).status == 404
    finally:
        await c.close()
