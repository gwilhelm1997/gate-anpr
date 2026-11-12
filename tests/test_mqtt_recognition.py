import asyncio
import json
import socket

import aiomqtt
import cv2
import numpy as np
import pytest

from gate_anpr import dashboard, recognition
from gate_anpr.mqtt import ENTITIES, MqttBridge, discovery_messages
from gate_anpr.recognition import RecognitionEngine, Region


def test_discovery_payloads_are_complete():
    msgs = dict(discovery_messages("0.1.0"))
    assert len(msgs) == len(ENTITIES)
    switch = json.loads(msgs["homeassistant/switch/gate_anpr/auto_open/config"])
    assert switch["command_topic"] == "gate_anpr/auto_open/set" and switch["availability_topic"]
    image = json.loads(msgs["homeassistant/image/gate_anpr/recent_1/config"])
    assert image["image_topic"] == "gate_anpr/recent_1/image" and "state_topic" not in image


def test_dashboard_uses_only_known_entities():
    ids = dashboard.predicted_ids()
    assert ids["auto_open"] == "switch.gate_anpr_automatic_opening"
    config = json.dumps(dashboard.build(ids, "camera.gate_cam_fluent", "switch.relay", "/app/local_gate_anpr"))
    assert "/app/local_gate_anpr" in config and "switch.gate_anpr_automatic_opening" in config


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_mqtt_round_trip_with_real_broker():
    from amqtt.broker import Broker

    port = free_port()
    broker = Broker({"listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
                     "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}}})
    await broker.start()
    commands, connected = [], asyncio.Event()

    async def on_command(key, payload):
        commands.append((key, payload))

    async def on_connect():
        connected.set()

    bridge = MqttBridge({"host": "127.0.0.1", "port": port}, "test", on_command, on_connect)
    task = asyncio.create_task(bridge.run())
    try:
        await asyncio.wait_for(connected.wait(), 10)
        async with aiomqtt.Client("127.0.0.1", port) as ha:
            await ha.subscribe("homeassistant/switch/gate_anpr/auto_open/config")
            await ha.subscribe("gate_anpr/availability")
            seen = {}
            async with asyncio.timeout(10):
                async for msg in ha.messages:
                    seen[msg.topic.value] = msg.payload
                    if len(seen) == 2:
                        break
            assert seen["gate_anpr/availability"] == b"online"
            await ha.publish("gate_anpr/auto_open/set", "ON")
            async with asyncio.timeout(5):
                while not commands:
                    await asyncio.sleep(0.05)
        assert commands == [("auto_open", "ON")]
    finally:
        task.cancel()
        await broker.shutdown()


def test_region_crop_offsets_boxes_back_to_full_frame(monkeypatch):
    class Box:
        x1, y1, x2, y2 = 10, 20, 110, 45

    class Result:
        detection = type("D", (), {"bounding_box": Box(), "confidence": 0.9})()
        ocr = type("O", (), {"text": "abc 123", "confidence": [0.99, 0.95, 0.97]})()

    seen = {}

    class Fake:
        def predict(self, img):
            seen["shape"] = img.shape
            return [Result()]

    monkeypatch.setattr(recognition, "_ALPR", Fake())
    jpeg = cv2.imencode(".jpg", np.zeros((2160, 3840, 3), np.uint8))[1].tobytes()
    out = recognition._recognise(jpeg, Region(0.5, 0.5, 1.0, 1.0))
    assert seen["shape"][:2] == (1080, 1920)
    r = out["reads"][0]
    assert r["text"] == "ABC123" and r["ocr_conf"] == 0.95 and r["box"] == (1930, 1100, 2030, 1125)


def test_region_too_small_is_rejected():
    with pytest.raises(ValueError):
        Region(0.5, 0.5, 0.505, 0.6).pixels(3840, 2160)


async def test_real_engine_starts_in_isolated_process_and_reads_a_plate():
    """Loads the real FastALPR models (cached after the first run) in the worker process."""
    engine = RecognitionEngine("yolo-v9-t-640-license-plate-end2end", "cct-s-v2-global-model", 2)
    try:
        await engine.start()
        assert engine.ready and engine.warmup_ms is not None, engine.last_error
        frame, plate = await engine.annotate(
            cv2.imencode(".jpg", np.zeros((720, 1280, 3), np.uint8))[1].tobytes(),
            (100, 100, 300, 160), "ABC123 99%", Region())
        assert frame[:2] == b"\xff\xd8" and plate[:2] == b"\xff\xd8"
    finally:
        engine.close()
