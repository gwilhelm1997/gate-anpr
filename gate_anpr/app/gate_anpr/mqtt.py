"""Native Home Assistant entities through MQTT discovery (Mosquitto app)."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import aiomqtt

_LOG = logging.getLogger(__name__)
BASE = "gate_anpr"
AVAILABILITY = f"{BASE}/availability"
RECENT_SLOTS = 4

_PCT = {"unit_of_measurement": "%", "min": 0, "max": 100, "step": 1, "mode": "slider"}
ENTITIES: dict[str, tuple[str, dict]] = {
    "recognition_enabled": ("switch", {"name": "Recognition", "icon": "mdi:cctv"}),
    "auto_open": ("switch", {"name": "Automatic opening", "icon": "mdi:gate-open"}),
    "min_confidence": ("number", {"name": "Minimum OCR confidence", "icon": "mdi:target",
                                  **_PCT, "min": 50, "max": 99, "entity_category": "config"}),
    "min_consistent": ("number", {"name": "Matching frames required", "icon": "mdi:image-multiple",
                                  "min": 2, "max": 5, "step": 1, "mode": "box", "entity_category": "config"}),
    "cooldown": ("number", {"name": "Opening cooldown", "icon": "mdi:timer-sand", "unit_of_measurement": "s",
                            "min": 10, "max": 900, "step": 5, "mode": "box", "entity_category": "config"}),
    "min_plate_width": ("number", {"name": "Minimum plate width", "icon": "mdi:arrow-expand-horizontal",
                                   "unit_of_measurement": "px", "min": 20, "max": 800, "step": 5,
                                   "mode": "box", "entity_category": "config"}),
    **{f"region_{side}": ("number", {"name": f"Region {side}", "icon": "mdi:crop", **_PCT,
                                     "entity_category": "config"})
       for side in ("left", "top", "right", "bottom")},
    "last_plate": ("sensor", {"name": "Last registration", "icon": "mdi:card-text-outline"}),
    "last_result": ("sensor", {"name": "Last result", "icon": "mdi:shield-car",
                               "json_attributes_topic": f"{BASE}/last_result/attributes"}),
    "last_confidence": ("sensor", {"name": "Last confidence", "unit_of_measurement": "%",
                                   "state_class": "measurement", "icon": "mdi:gauge"}),
    "status": ("sensor", {"name": "Status", "icon": "mdi:state-machine",
                          "json_attributes_topic": f"{BASE}/status/attributes"}),
    "recognition_time": ("sensor", {"name": "Recognition time", "unit_of_measurement": "ms",
                                    "state_class": "measurement", "icon": "mdi:timer-outline",
                                    "entity_category": "diagnostic"}),
    "approved_count": ("sensor", {"name": "Approved registrations", "icon": "mdi:format-list-checks"}),
    "engine": ("sensor", {"name": "Recognition engine", "icon": "mdi:chip", "entity_category": "diagnostic",
                          "json_attributes_topic": f"{BASE}/engine/attributes"}),
    "test": ("button", {"name": "Test recognition", "icon": "mdi:camera-iris"}),
    "last_plate_image": ("image", {"name": "Last plate image", "content_type": "image/jpeg"}),
    **{f"recent_{i}": ("image", {"name": f"Recent detection {i}", "content_type": "image/jpeg"})
       for i in range(1, RECENT_SLOTS + 1)},
}
SETTINGS_KEYS = [k for k, (c, _) in ENTITIES.items() if c in ("switch", "number")]


def discovery_messages(version: str) -> list[tuple[str, str]]:
    device = {"identifiers": [BASE], "name": "Gate ANPR", "manufacturer": "Gate ANPR",
              "model": "FastALPR Home Assistant app", "sw_version": version}
    out = []
    for key, (component, extra) in ENTITIES.items():
        conf = {"unique_id": f"{BASE}_{key}", "availability_topic": AVAILABILITY, "device": device,
                "origin": {"name": "Gate ANPR", "sw_version": version}, **extra}
        if component == "image":
            conf["image_topic"] = f"{BASE}/{key}/image"
        elif component != "button":
            conf["state_topic"] = f"{BASE}/{key}/state"
        if component in ("switch", "number", "button"):
            conf["command_topic"] = f"{BASE}/{key}/set"
        out.append((f"homeassistant/{component}/{BASE}/{key}/config", json.dumps(conf)))
    return out


class MqttBridge:
    def __init__(self, conf: dict, version: str,
                 on_command: Callable[[str, str], Awaitable[None]],
                 on_connect: Callable[[], Awaitable[None]]):
        self._conf = conf
        self._version = version
        self._on_command = on_command
        self._on_connect = on_connect
        self._client: aiomqtt.Client | None = None
        self.connected = False

    async def publish(self, key: str, value, attributes: dict | None = None) -> None:
        payload = ("ON" if value else "OFF") if isinstance(value, bool) else ("" if value is None else str(value))
        await self._send(f"{BASE}/{key}/state", payload)
        if attributes is not None:
            await self._send(f"{BASE}/{key}/attributes", json.dumps(attributes, default=str))

    async def publish_image(self, key: str, jpeg: bytes) -> None:
        await self._send(f"{BASE}/{key}/image", jpeg)

    async def _send(self, topic: str, payload) -> None:
        client = self._client
        if client is None:
            return  # offline: everything is republished on reconnect
        try:
            await client.publish(topic, payload, qos=1, retain=True)
        except aiomqtt.MqttError as err:
            _LOG.warning("MQTT publish to %s failed: %s", topic, err)

    async def run(self) -> None:
        delay = 2
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self._conf["host"], port=int(self._conf.get("port", 1883)),
                    username=self._conf.get("username") or None, password=self._conf.get("password") or None,
                    identifier="gate_anpr", keepalive=30,
                    will=aiomqtt.Will(AVAILABILITY, "offline", qos=1, retain=True),
                ) as client:
                    await client.subscribe(f"{BASE}/+/set", qos=1)
                    await client.subscribe("homeassistant/status", qos=1)
                    self._client, delay = client, 2
                    await self._announce(client)
                    self.connected = True
                    _LOG.info("Connected to MQTT broker %s", self._conf["host"])
                    async for msg in client.messages:
                        topic = msg.topic.value
                        payload = msg.payload.decode(errors="replace") if isinstance(msg.payload, bytes) else str(msg.payload)
                        if topic == "homeassistant/status":
                            if payload == "online":  # HA restarted: make sure it has everything
                                await self._announce(client)
                            continue
                        key = topic.split("/")[1]
                        try:
                            await self._on_command(key, payload)
                        except Exception:  # noqa: BLE001 - a bad command must not drop the connection
                            _LOG.exception("MQTT command %s=%r failed", key, payload)
            except asyncio.CancelledError:
                raise
            except aiomqtt.MqttError as err:
                _LOG.warning("MQTT connection lost (%s); retrying in %ss", err, delay)
            finally:
                self.connected = False
                self._client = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _announce(self, client: aiomqtt.Client) -> None:
        for topic, payload in discovery_messages(self._version):
            await client.publish(topic, payload, qos=1, retain=True)
        await self._on_connect()
        await client.publish(AVAILABILITY, "online", qos=1, retain=True)

    async def stop(self) -> None:
        if self._client:
            try:
                await self._client.publish(AVAILABILITY, "offline", qos=1, retain=True)
            except aiomqtt.MqttError:
                pass
