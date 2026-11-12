"""Recognition sessions per lane: trigger -> frames -> consensus -> access rule -> safe gate command."""

import asyncio
import logging
import time
from dataclasses import asdict, dataclass

from .decision import Frame, Rules, evaluate
from .gate import GateController
from .ha import HAError, HomeAssistant
from .mqtt import LANE_CONTROLS, LEGACY_KEYS, RECENT_SLOTS, MqttBridge, discovery_messages, lane_key
from .recognition import RecognitionEngine, Region
from .store import Lane, Store

_LOG = logging.getLogger(__name__)
EVENT_TYPE = "gate_anpr_access"


@dataclass(frozen=True)
class Options:
    max_frames: int = 6
    capture_window: float = 8.0
    frame_interval: float = 0.6
    confirm_timeout: float = 20.0
    retention_days: int = 30


class LaneRuntime:
    def __init__(self, lane: Lane):
        self.lane = lane
        self.lock = asyncio.Lock()
        self.status = "idle"
        self.last_event: dict | None = None

    def region(self) -> Region:
        s = self.lane
        return Region(s.region_left / 100, s.region_top / 100, s.region_right / 100, s.region_bottom / 100)

    def rules(self) -> Rules:
        s = self.lane
        return Rules(s.min_confidence / 100, s.min_consistent, s.min_plate_width, s.direction)


class GateAnpr:
    def __init__(self, options: Options, store: Store, engine: RecognitionEngine, ha: HomeAssistant,
                 mqtt: MqttBridge | None = None, version: str = "dev", gate_poll: float = 0.25):
        self.options = options
        self.store = store
        self.engine = engine
        self.ha = ha
        self.mqtt = mqtt
        self.version = version
        self.lanes: dict[int, LaneRuntime] = {}
        self._gates: dict[str, GateController] = {}  # shared by lanes that drive the same gate
        self._gate_poll = gate_poll
        self._clock = time.monotonic
        self.on_lanes_changed = None  # set by the app: resubscribe triggers, refresh dashboard
        self.load_lanes()

    # lanes ----------------------------------------------------------------
    def load_lanes(self) -> None:
        fresh = {}
        for lane in self.store.lanes():
            rt = self.lanes.get(lane.id) or LaneRuntime(lane)
            rt.lane = lane
            fresh[lane.id] = rt
        self.lanes = fresh

    def gate(self, lane: Lane) -> GateController:
        ctl = self._gates.get(lane.gate_entity)
        if ctl is None:
            ctl = self._gates[lane.gate_entity] = GateController(
                self.ha, lane.gate_entity, confirm_timeout=self.options.confirm_timeout,
                poll_interval=self._gate_poll)
        ctl.position_entity = lane.gate_position_entity or None
        return ctl

    def triggers(self) -> set[str]:
        return {rt.lane.trigger_entity for rt in self.lanes.values()}

    async def save_lane(self, data: dict, lane_id: int = 0) -> Lane:
        if lane_id in self.lanes and data and set(data) <= set(LANE_CONTROLS):
            for key in data:
                Lane.coerce(key, data[key])  # validate everything before changing anything
            for key, value in data.items():
                await self.set_setting(lane_id, key, value)  # switches/numbers: no re-announce needed
            return self.lanes[lane_id].lane
        base = self.lanes[lane_id].lane.to_dict() if lane_id in self.lanes else {}
        lane = self.store.save_lane(Lane.from_dict({**base, **data}, lane_id))
        _LOG.info("Lane %s saved: %s", lane.id, lane.name)
        await self.lanes_changed()
        return lane

    async def delete_lane(self, lane_id: int) -> bool:
        if not self.store.delete_lane(lane_id):
            return False
        _LOG.info("Lane %s deleted", lane_id)
        await self.lanes_changed()
        return True

    async def lanes_changed(self) -> None:
        self.load_lanes()
        await self.announce()
        if self.on_lanes_changed:
            await self.on_lanes_changed()

    async def set_setting(self, lane_id: int, key: str, value) -> None:
        rt = self.lanes[lane_id]
        value = Lane.coerce(key, value)
        setattr(rt.lane, key, value)
        self.store.save_lane(rt.lane)
        _LOG.info("Lane %s: %s = %s", rt.lane.name, key, value)
        if self.mqtt and key in LANE_CONTROLS:
            await self.mqtt.publish(lane_key(lane_id, key), value)

    async def on_mqtt_command(self, key: str, payload: str) -> None:
        lane_part, _, setting = key.partition("_")
        try:
            lane_id = int(lane_part[1:]) if lane_part.startswith("l") else -1
        except ValueError:
            return
        if lane_id not in self.lanes:
            return
        if setting == "test":
            asyncio.create_task(self.run_session(lane_id, test=True))
        elif setting in LANE_CONTROLS:
            try:
                await self.set_setting(lane_id, setting, payload)
            except ValueError as err:
                _LOG.warning("Rejected %s=%r: %s", key, payload, err)
                await self.mqtt.publish(key, getattr(self.lanes[lane_id].lane, setting))  # snap back

    # MQTT -----------------------------------------------------------------
    async def announce(self) -> None:
        """Discovery for every lane, removal of entities that no longer exist, then all states."""
        if not self.mqtt:
            return
        messages = discovery_messages(self.version, [rt.lane for rt in self.lanes.values()])
        current = {topic for topic, _ in messages}
        stale = set(self.store.meta("discovery_topics", [])) - current
        if not self.store.meta("legacy_cleared"):
            stale |= {f"homeassistant/{c}/gate_anpr/{k}/config" for c, k in LEGACY_KEYS}
        for topic in sorted(stale):
            await self.mqtt.send(topic, "")
        for topic, payload in messages:
            await self.mqtt.send(topic, payload)
        if self.mqtt.can_send:
            self.store.set_meta("discovery_topics", sorted(current))
            self.store.set_meta("legacy_cleared", True)
        await self.publish_all()

    async def publish_all(self) -> None:
        if not self.mqtt:
            return
        await self.mqtt.publish("approved_count", self.store.approved_count())
        await self.mqtt.publish("engine", "ready" if self.engine.ready else "not ready", self.engine.info())
        for lane_id, rt in self.lanes.items():
            for key in LANE_CONTROLS:
                await self.mqtt.publish(lane_key(lane_id, key), getattr(rt.lane, key))
            await self.publish_status(rt)
            if rt.last_event:
                await self._publish_event(rt, rt.last_event)

    async def publish_status(self, rt: LaneRuntime, status: str | None = None) -> None:
        if status:
            rt.status = status
        if self.mqtt:
            blocked, detail = await self.gate(rt.lane).check()
            await self.mqtt.publish(lane_key(rt.lane.id, "status"), rt.status, {
                **self.diagnostics(rt), "gate_ready": blocked is None, "gate_check": detail or "ok"})

    def diagnostics(self, rt: LaneRuntime | None = None) -> dict:
        out = {
            "version": self.version,
            "home_assistant_connected": self.ha.connected,
            "mqtt_connected": bool(self.mqtt and self.mqtt.connected),
            "engine_ready": self.engine.ready,
            "engine_error": self.engine.last_error,
        }
        if rt:
            lane = rt.lane
            out.update(lane=lane.name, camera_entity=lane.camera_entity, trigger_entity=lane.trigger_entity,
                       gate_entity=lane.gate_entity, gate_position_entity=lane.gate_position_entity or None,
                       access=lane.access, direction=lane.direction,
                       cooldown_remaining=round(self.gate(lane).cooldown_remaining(lane.cooldown)))
        return out

    # sessions -------------------------------------------------------------
    async def on_vehicle(self, entity_id: str) -> None:
        for lane_id, rt in list(self.lanes.items()):
            if rt.lane.trigger_entity == entity_id and rt.lane.enabled:
                asyncio.create_task(self.run_session(lane_id, test=False))

    async def run_session(self, lane_id: int, test: bool) -> dict | None:
        rt = self.lanes.get(lane_id)
        if rt is None:
            return None
        if rt.lock.locked():
            _LOG.info("%s: recognition already running; ignoring overlapping trigger", rt.lane.name)
            return None
        async with rt.lock:
            try:
                return await self._session(rt, test)
            finally:
                await self.publish_status(rt, "idle" if self.engine.ready else "engine not ready")

    async def _session(self, rt: LaneRuntime, test: bool) -> dict:
        lane = rt.lane
        started_wall, started = time.time(), self._clock()
        await self.publish_status(rt, "capturing")
        region, rules = rt.region(), rt.rules()
        frames: list[Frame] = []
        jpegs: list[bytes] = []
        timings: list[int] = []
        error = None
        verdict = evaluate(frames, rules)
        try:
            if not self.engine.ready:
                await self.engine.start()  # recover from a crashed or failed worker
            if not self.engine.ready:
                raise RuntimeError(self.engine.last_error or "recognition engine is not ready")
            while len(frames) < self.options.max_frames and self._clock() - started < self.options.capture_window:
                shot_at = self._clock()
                jpeg = await self.ha.camera_image(lane.camera_entity)
                width, height, reads, ms = await self.engine.recognise(jpeg, region)
                frames.append(Frame(len(frames), shot_at - started, width, height, reads))
                jpegs.append(jpeg)
                timings.append(ms)
                verdict = evaluate(frames, rules)
                if verdict.accepted:
                    break
                await asyncio.sleep(max(0.0, self.options.frame_interval - (self._clock() - shot_at)))
        except Exception as err:  # noqa: BLE001 - every failure is recorded and fails closed
            error = f"{type(err).__name__}: {err}"
            _LOG.warning("%s: recognition session failed: %s", lane.name, error)

        approved = self.store.is_approved(verdict.plate, lane.id) if verdict.accepted else None
        allowed = bool(approved) or (verdict.accepted and lane.access == "all")
        if error:
            result, reason, detail = "error", "error", error
        elif not verdict.accepted:
            result, reason, detail = "rejected", verdict.reason, ""
        elif not allowed:
            result, reason, detail = "rejected", "not_approved", ""
        elif test:
            result, reason, detail = "approved_test", "test_only", "test recognition never operates the gate"
        elif not lane.auto_open:
            result, reason, detail = "approved_monitor", "auto_open_disabled", "log-only mode; gate not operated"
        elif not self.ha.connected:
            result, reason, detail = "approved_not_opened", "ha_disconnected", "Home Assistant link uncertain"
        elif not (self.mqtt and self.mqtt.connected):
            result, reason, detail = ("approved_not_opened", "controls_offline",
                                      "dashboard controls unreachable; automatic opening needs MQTT")
        else:
            gate_result, detail = await self.gate(lane).open(lane.cooldown)
            result = "granted" if gate_result in ("opened", "already_open") else "approved_not_opened"
            reason = gate_result
        if allowed and not approved and not error:
            detail = (detail + "; " if detail else "") + "allowed by the lane's allow-all setting"

        event = {
            "ts": started_wall,
            "lane_id": lane.id,
            "lane": lane.name,
            "plate": verdict.plate,
            "result": result,
            "reason": reason,
            "granted": result == "granted",
            "confidence": round(verdict.confidence * 100, 1) if verdict.plate else None,
            "label": approved["label"] if approved else None,
            "test": test,
            "detail": {
                "message": detail,
                "test": test,
                "access": lane.access,
                "verdict": {k: v for k, v in asdict(verdict).items() if k != "best_box"},
                "frames": len(frames),
                "timings_ms": timings,
                "duration_s": round(self._clock() - started, 2),
                "reads": [[{"text": r.text, "ocr": r.ocr_conf, "width": r.width} for r in f.reads] for f in frames],
            },
        }
        if not frames and not error:
            return event
        event["id"] = self.store.add_event(event)
        await self._save_images(event, jpegs, verdict, region)
        rt.last_event = event
        _LOG.info("%s: %s %s (%s) %s", lane.name, result.upper(), verdict.plate or "-", reason, detail)
        await self._publish_event(rt, event)
        try:
            await self.ha.fire_event(EVENT_TYPE, {k: event[k] for k in (
                "lane", "lane_id", "plate", "result", "reason", "granted", "confidence", "label", "test")})
        except HAError as err:
            _LOG.warning("Could not fire %s event: %s", EVENT_TYPE, err)
        return event

    async def _save_images(self, event, jpegs, verdict, region) -> None:
        if not jpegs:
            return
        index = verdict.best_frame if verdict.best_frame is not None else len(jpegs) - 1
        label = f"{verdict.plate} {verdict.confidence * 100:.0f}%" if verdict.plate else ""
        try:
            frame_jpeg, plate_jpeg = await self.engine.annotate(jpegs[index], verdict.best_box, label, region)
        except Exception as err:  # noqa: BLE001 - images are diagnostics; never block the decision
            _LOG.warning("Could not annotate image: %s", err)
            return
        name = f"{event['id']:07d}.jpg"
        (self.store.images / name).write_bytes(frame_jpeg)
        plate_name = None
        if plate_jpeg:
            plate_name = f"{event['id']:07d}_plate.jpg"
            (self.store.images / plate_name).write_bytes(plate_jpeg)
        self.store.set_event_images(event["id"], name, plate_name)
        event["image"], event["plate_image"] = name, plate_name

    async def _publish_event(self, rt: LaneRuntime, event: dict) -> None:
        if not self.mqtt:
            return
        lid = rt.lane.id
        attrs = {k: event.get(k) for k in ("id", "lane", "plate", "label", "result", "reason", "granted",
                                           "confidence", "test")}
        attrs["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event["ts"]))
        attrs.update(event["detail"])
        history = self.store.events(limit=10, lane_id=lid)
        attrs["recent"] = [
            {"time": time.strftime("%d %b %H:%M:%S", time.localtime(e["ts"])), "plate": e["plate"],
             "result": e["result"], "reason": e["reason"], "confidence": e["confidence"]} for e in history]
        await self.mqtt.publish(lane_key(lid, "last_plate"), event.get("plate") or "none")
        await self.mqtt.publish(lane_key(lid, "last_result"), event["result"], attrs)
        await self.mqtt.publish(lane_key(lid, "last_confidence"), event.get("confidence"))
        timings = event["detail"]["timings_ms"]
        if timings:
            await self.mqtt.publish(lane_key(lid, "recognition_time"), round(sum(timings) / len(timings)))
        if event.get("plate_image"):
            path = self.store.images / event["plate_image"]
            if path.exists():
                await self.mqtt.publish_image(lane_key(lid, "last_plate_image"), path.read_bytes())
        recent = [e for e in self.store.events(limit=RECENT_SLOTS * 3, lane_id=lid) if e.get("image")]
        for slot, e in enumerate(recent[:RECENT_SLOTS], start=1):
            path = self.store.images / e["image"]
            if path.exists():
                await self.mqtt.publish_image(lane_key(lid, f"recent_{slot}"), path.read_bytes())

    async def plates_changed(self) -> None:
        if self.mqtt:
            await self.mqtt.publish("approved_count", self.store.approved_count())
