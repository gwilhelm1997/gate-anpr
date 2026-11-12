"""Runs the real entry point against a fake Home Assistant (REST + WebSocket) and a real MQTT broker."""

import asyncio
import json
import os

import aiohttp
import aiomqtt
import cv2
import numpy as np
from aiohttp import web

from gate_anpr import __main__ as app_main
from test_mqtt_recognition import free_port

TOKEN = "test-token"
JPEG = cv2.imencode(".jpg", np.full((2160, 3840, 3), 90, np.uint8))[1].tobytes()


class FakeHomeAssistant:
    def __init__(self):
        self.ws_clients = []
        self.service_calls = []
        self.fired = []
        self.dashboards = []
        self.saved_config = None
        self.camera_hits = 0
        self.subscriptions = []
        self.states = {
            "switch.sonoff_100219c178_2": {"entity_id": "switch.sonoff_100219c178_2", "state": "off",
                                           "last_changed": "t0",
                                           "attributes": {"friendly_name": "Outside gate control OPEN"}},
            "switch.sonoff_100219c097_1": {"entity_id": "switch.sonoff_100219c097_1", "state": "off",
                                           "last_changed": "t0",
                                           "attributes": {"friendly_name": "Inside gate Control OPEN"}}}

    def app(self):
        def authed(handler):
            async def wrapper(request):
                if request.headers.get("Authorization") != f"Bearer {TOKEN}":
                    raise web.HTTPUnauthorized()
                return await handler(request)
            return wrapper

        async def state(request):
            s = self.states.get(request.match_info["entity_id"])
            return web.json_response(s) if s else web.Response(status=404)

        async def all_states(_):
            return web.json_response(list(self.states.values()))

        async def service(request):
            self.service_calls.append((request.match_info["domain"], request.match_info["service"]))
            return web.json_response([])

        async def camera(_):
            self.camera_hits += 1
            return web.Response(body=JPEG, content_type="image/jpeg")

        async def event(request):
            self.fired.append((request.match_info["event_type"], await request.json()))
            return web.json_response({"message": "ok"})

        async def websocket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"type": "auth_required"})
            auth = await ws.receive_json()
            await ws.send_json({"type": "auth_ok" if auth.get("access_token") == TOKEN else "auth_invalid"})
            self.ws_clients.append(ws)
            async for msg in ws:
                m = json.loads(msg.data)
                result = None
                if m["type"] == "lovelace/dashboards/list":
                    result = self.dashboards
                elif m["type"] == "lovelace/dashboards/create":
                    self.dashboards.append({"url_path": m["url_path"]})
                elif m["type"] == "lovelace/config":
                    result = self.saved_config
                elif m["type"] == "lovelace/config/save":
                    self.saved_config = m["config"]
                elif m["type"] == "config/entity_registry/list":
                    result = []
                elif m["type"] == "subscribe_trigger":
                    self.trigger = (ws, m["id"], m["trigger"])
                    self.subscriptions.append(m["trigger"]["entity_id"])
                await ws.send_json({"id": m["id"], "type": "result", "success": True, "result": result})
            return ws

        a = web.Application()
        a.add_routes([
            web.get("/api/states", authed(all_states)),
            web.get("/api/states/{entity_id}", authed(state)),
            web.post("/api/services/{domain}/{service}", authed(service)),
            web.get("/api/camera_proxy/{entity_id}", authed(camera)),
            web.post("/api/events/{event_type}", authed(event)),
            web.get("/api/websocket", websocket),
        ])
        return a

    async def fire_vehicle(self, entity_id):
        ws, sub_id, _ = self.trigger
        await ws.send_json({"id": sub_id, "type": "event",
                            "event": {"variables": {"trigger": {"entity_id": entity_id}}}})


async def wait_for(predicate, timeout=60):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.1)


async def test_app_end_to_end(tmp_path, monkeypatch):
    from amqtt.broker import Broker

    fake = FakeHomeAssistant()
    runner = web.AppRunner(fake.app())
    await runner.setup()
    ha_port, mqtt_port, ui_port = free_port(), free_port(), free_port()
    await web.TCPSite(runner, "127.0.0.1", ha_port).start()
    broker = Broker({"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{mqtt_port}"}},
                     "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}})
    await broker.start()

    (tmp_path / "options.json").write_text(json.dumps({
        "camera_entity": "camera.gate_cam_snapshots_clear", "live_camera_entity": "camera.gate_cam_fluent",
        "trigger_entity": "binary_sensor.gate_cam_vehicle", "gate_entity": "switch.sonoff_100219c178_2",
        "gate_position_entity": "", "detector_model": "yolo-v9-t-640-license-plate-end2end",
        "ocr_model": "cct-s-v2-global-model", "ocr_threads": 1, "max_frames": 3, "capture_window": 3,
        "frame_interval": 0.2, "confirm_timeout": 5, "retention_days": 30, "create_dashboard": True,
        "log_level": "info"}))
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    for key, value in {"HA_URL": f"http://127.0.0.1:{ha_port}", "HA_TOKEN": TOKEN, "DATA_DIR": str(tmp_path),
                       "MQTT_HOST": "127.0.0.1", "MQTT_PORT": str(mqtt_port), "INGRESS_PORT": str(ui_port)}.items():
        monkeypatch.setenv(key, value)

    task = asyncio.create_task(app_main.main())
    try:
        async with aiomqtt.Client("127.0.0.1", mqtt_port) as mqtt:
            await mqtt.subscribe("homeassistant/#")
            await mqtt.subscribe("gate_anpr/#")
            await wait_for(lambda: hasattr(fake, "trigger") and fake.saved_config is not None)
            assert fake.trigger[2] == {"platform": "state", "entity_id": ["binary_sensor.gate_cam_vehicle"],
                                       "from": "off", "to": "on"}
            assert fake.dashboards == [{"url_path": "gate-anpr"}]
            assert [v["title"] for v in fake.saved_config["views"]] == ["Outside gate"]

            await fake.fire_vehicle("binary_sensor.gate_cam_vehicle")
            await wait_for(lambda: fake.fired)
            assert fake.camera_hits == 3  # captured the full window: no plate in a blank frame
            assert fake.fired[0][0] == "gate_anpr_access"
            assert fake.fired[0][1]["result"] == "rejected" and fake.fired[0][1]["reason"] == "no_plate"
            assert fake.service_calls == []  # the gate is never touched

            seen = {}
            async with asyncio.timeout(10):
                async for msg in mqtt.messages:
                    seen[msg.topic.value] = msg.payload
                    if "gate_anpr/l1_last_result/state" in seen and "gate_anpr/l1_recent_1/image" in seen:
                        break
            assert seen["gate_anpr/l1_last_result/state"] == b"rejected"
            assert seen["gate_anpr/l1_recent_1/image"][:2] == b"\xff\xd8"
            switch = json.loads(seen["homeassistant/switch/gate_anpr/l1_auto_open/config"])
            assert switch["name"] == "Automatic opening" and switch["device"]["name"] == "Gate ANPR Outside gate"

        async with aiohttp.ClientSession() as http:
            ui = f"http://127.0.0.1:{ui_port}"
            async with http.get(f"{ui}/api/state") as r:
                state = await r.json()
            assert [(lane["id"], lane["name"], lane["auto_open"]) for lane in state["lanes"]] == [
                (1, "Outside gate", False)] and state["engine"]["ready"] is True
            async with http.get(f"{ui}/api/entities") as r:
                gates = [g["id"] for g in (await r.json())["gates"]]
            assert gates == ["switch.sonoff_100219c097_1", "switch.sonoff_100219c178_2"]
            async with http.post(f"{ui}/api/lanes", json={
                    "name": "Inside gate", "access": "all", "direction": "away",
                    "camera_entity": "camera.gate2_cam_snapshots_clear",
                    "trigger_entity": "binary_sensor.gate2_cam_vehicle",
                    "gate_entity": "switch.sonoff_100219c097_1"}) as r:
                assert r.status == 200 and (await r.json())["id"] == 2
            await wait_for(lambda: fake.subscriptions[-1] == ["binary_sensor.gate2_cam_vehicle",
                                                              "binary_sensor.gate_cam_vehicle"])
            await wait_for(lambda: len(fake.saved_config["views"]) == 2)
            await fake.fire_vehicle("binary_sensor.gate2_cam_vehicle")
            await wait_for(lambda: len(fake.fired) == 2)
            assert fake.fired[1][1]["lane"] == "Inside gate" and fake.service_calls == []
            async with http.get(f"{ui}/") as r:
                assert "Approved registrations" in await r.text()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await broker.shutdown()
        await runner.cleanup()
        os.environ.pop("SUPERVISOR_TOKEN", None)
