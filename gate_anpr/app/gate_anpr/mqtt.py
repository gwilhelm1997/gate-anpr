"""Native Home Assistant entities through MQTT discovery (Mosquitto app): one device per lane."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import aiomqtt

_LOG = logging.getLogger(__name__)
BASE = "gate_anpr"
AVAILABILITY = f"{BASE}/availability"
RECENT_SLOTS = 4

_PCT = {"unit_of_measurement": "%", "min": 0, "max": 100, "step": 1, "mode": "slider", "entity_category": "config"}
LANE_ENTITIES: dict[str, tuple[str, dict]] = {
    "enabled": ("switch", {"name": "Recognition", "icon": "mdi:cctv"}),
    "auto_open": ("switch", {"name": "Automatic opening", "icon": "mdi:gate-open"}),
    "min_confidence": ("number", {"name": "Minimum OCR confidence", "icon": "mdi:target",
                                  **_PCT, "min": 50, "max": 99}),
    "min_consistent": ("number", {"name": "Matching frames required", "icon": "mdi:image-multiple",
                                  "min": 2, "max": 5, "step": 1, "mode": "box", "entity_category": "config"}),
    "cooldown": ("number", {"name": "Opening cooldown", "icon": "mdi:timer-sand", "unit_of_measurement": "s",
                            "min": 10, "max": 900, "step": 5, "mode": "box", "entity_category": "config"}),
    "min_plate_width": ("number", {"name": "Minimum plate width", "icon": "mdi:arrow-expand-horizontal",
                                   "unit_of_measurement": "px", "min": 20, "max": 800, "step": 5,
                                   "mode": "box", "entity_category": "config"}),
    **{f"region_{side}": ("number", {"name": f"Region {side}", "icon": "mdi:crop", **_PCT})
       for side in ("left", "top", "right", "bottom")},
    "last_plate": ("sensor", {"name": "Last registration", "icon": "mdi:card-text-outline"}),
    "last_result": ("sensor", {"name": "Last result", "icon": "mdi:shield-car", "attributes": True}),
    "last_confidence": ("sensor", {"name": "Last confidence", "unit_of_measurement": "%",
                                   "state_class": "measurement", "icon": "mdi:gauge"}),
    "status": ("sensor", {"name": "Status", "icon": "mdi:state-machine", "attributes": True}),
    "recognition_time": ("sensor", {"name": "Recognition time", "unit_of_measurement": "ms",
                                    "state_class": "measurement", "icon": "mdi:timer-outline",
                                    "entity_category": "diagnostic"}),
    "test": ("button", {"name": "Test recognition", "icon": "mdi:camera-iris"}),
    "last_plate_image": ("image", {"name": "Last plate image", "content_type": "image/jpeg"}),
    **{f"recent_{i}": ("image", {"name": f"Recent detection {i}", "content_type": "image/jpeg"})
       for i in range(1, RECENT_SLOTS + 1)},
}
GLOBAL_ENTITIES: dict[str, tuple[str, dict]] = {
    "approved_count": ("sensor", {"name": "Approved registrations", "icon": "mdi:format-list-checks"}),
    "engine": ("sensor", {"name": "Recognition engine", "icon": "mdi:chip", "entity_category": "diagnostic",
                          "attributes": True}),
}
LANE_CONTROLS = [k for k, (c, _) in LANE_ENTITIES.items() if c in ("switch", "number")]
# 0.1.x single-gate entities, removed on first 2.0 start.
LEGACY_KEYS = [("switch", "recognition_enabled"), ("switch", "auto_open"), ("button", "test"),
               *[(c, k) for k, (c, _) in LANE_ENTITIES.items() if k not in ("enabled", "auto_open", "test")]]


def lane_key(lane_id: int, key: str) -> str:
    return f"l{lane_id}_{key}"


def _config(key: str, component: str, extra: dict, device: dict, version: str) -> tuple[str, str]:
    extra = dict(extra)
    conf = {"unique_id": f"{BASE}_{key}", "availability_topic": AVAILABILITY, "device": device,
            "origin": {"name": "Gate ANPR", "sw_version": version}}
    if extra.pop("attributes", False):
        conf["json_attributes_topic"] = f"{BASE}/{key}/attributes"
    conf.update(extra)
    if component == "image":
        conf["image_topic"] = f"{BASE}/{key}/image"
    elif component != "button":
        conf["state_topic"] = f"{BASE}/{key}/state"
    if component in ("switch", "number", "button"):
        conf["command_topic"] = f"{BASE}/{key}/set"
    return f"homeassistant/{component}/{BASE}/{key}/config", json.dumps(conf)


def discovery_messages(version: str, lanes: list) -> list[tuple[str, str]]:
    hub = {"identifiers": [BASE], "name": "Gate ANPR", "manufacturer": "Gate ANPR",
           "model": "FastALPR Home Assistant app", "sw_version": version}
    out = [_config(k, c, e, hub, version) for k, (c, e) in GLOBAL_ENTITIES.items()]
    for lane in lanes:
        device = {"identifiers": [f"{BASE}_l{lane.id}"], "name": f"Gate ANPR {lane.name}",
                  "manufacturer": "Gate ANPR", "model": "Gate lane", "via_device": BASE, "sw_version": version}
        out += [_config(lane_key(lane.id, k), c, e, device, version) for k, (c, e) in LANE_ENTITIES.items()]
    return out


class MqttBridge:
    def __init__(self, conf: dict, on_command: Callable[[str, str], Awaitable[None]],
                 on_connect: Callable[[], Awaitable[None]]):
        self._conf = conf
        self._on_command = on_command
        self._on_connect = on_connect
        self._client: aiomqtt.Client | None = None
        self.connected = False

    @property
    def can_send(self) -> bool:
        return self._client is not None

    async def publish(self, key: str, value, attributes: dict | None = None) -> None:
        payload = ("ON" if value else "OFF") if isinstance(value, bool) else ("" if value is None else str(value))
        await self.send(f"{BASE}/{key}/state", payload)
        if attributes is not None:
            await self.send(f"{BASE}/{key}/attributes", json.dumps(attributes, default=str))

    async def publish_image(self, key: str, jpeg: bytes) -> None:
        await self.send(f"{BASE}/{key}/image", jpeg)

    async def send(self, topic: str, payload) -> None:
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
                        payload = msg.payload.decode(errors="replace") if isinstance(msg.payload, bytes) \
                            else str(msg.payload)
                        if topic == "homeassistant/status":
                            if payload == "online":  # HA restarted: make sure it has everything
                                await self._announce(client)
                            continue
                        try:
                            await self._on_command(topic.split("/")[1], payload)
                        except Exception:  # noqa: BLE001 - a bad command must not drop the connection
                            _LOG.exception("MQTT command %s=%r failed", topic, payload)
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
        await self._on_connect()  # publishes discovery and states through send()
        await client.publish(AVAILABILITY, "online", qos=1, retain=True)

    async def stop(self) -> None:
        if self._client:
            try:
                await self._client.publish(AVAILABILITY, "offline", qos=1, retain=True)
            except aiomqtt.MqttError:
                pass
