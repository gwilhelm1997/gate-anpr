"""The recognition session: trigger -> frames -> consensus -> whitelist -> safe gate command."""

import asyncio
import logging
import time
from dataclasses import asdict, dataclass

from .decision import Frame, Rules, evaluate
from .gate import GateController
from .ha import HAError, HomeAssistant
from .mqtt import RECENT_SLOTS, SETTINGS_KEYS, MqttBridge
from .recognition import RecognitionEngine, Region
from .store import Settings, Store

_LOG = logging.getLogger(__name__)
EVENT_TYPE = "gate_anpr_access"


@dataclass(frozen=True)
class Options:
    camera_entity: str
    trigger_entity: str
    gate_entity: str
    max_frames: int = 6
    capture_window: float = 8.0
    frame_interval: float = 0.6
    confirm_timeout: float = 20.0
    retention_days: int = 30


class GateAnpr:
    def __init__(self, options: Options, store: Store, engine: RecognitionEngine, ha: HomeAssistant,
                 gate: GateController, mqtt: MqttBridge | None = None, version: str = "dev"):
        self.options = options
        self.store = store
        self.engine = engine
        self.ha = ha
        self.gate = gate
        self.mqtt = mqtt
        self.version = version
        self.settings = store.load_settings()
        self.status = "starting"
        self.last_event: dict | None = None
        self._busy = asyncio.Lock()
        self._clock = time.monotonic

    # settings -------------------------------------------------------------
    async def set_setting(self, key: str, value) -> None:
        value = Settings.coerce(key, value)
        setattr(self.settings, key, value)
        self.store.save_setting(key, value)
        _LOG.info("Setting %s = %s", key, value)
        if self.mqtt:
            await self.mqtt.publish(key, value)

    async def on_mqtt_command(self, key: str, payload: str) -> None:
        if key == "test":
            asyncio.create_task(self.run_session(test=True))
        elif key in SETTINGS_KEYS:
            try:
                await self.set_setting(key, payload)
            except ValueError as err:
                _LOG.warning("Rejected %s=%r: %s", key, payload, err)
                await self.mqtt.publish(key, getattr(self.settings, key))  # snap the control back

    async def publish_all(self) -> None:
        if not self.mqtt:
            return
        for key in SETTINGS_KEYS:
            await self.mqtt.publish(key, getattr(self.settings, key))
        await self.mqtt.publish("approved_count", self.store.approved_count())
        await self.mqtt.publish("engine", "ready" if self.engine.ready else "not ready", self.engine.info())
        await self.publish_status()
        if self.last_event:
            await self._publish_event(self.last_event)

    async def publish_status(self, status: str | None = None) -> None:
        if status:
            self.status = status
        if self.mqtt:
            blocked, detail = await self.gate.check()
            await self.mqtt.publish("status", self.status, {
                **self.diagnostics(), "gate_ready": blocked is None, "gate_check": detail or "ok"})

    def diagnostics(self) -> dict:
        return {
            "version": self.version,
            "home_assistant_connected": self.ha.connected,
            "mqtt_connected": bool(self.mqtt and self.mqtt.connected),
            "engine_ready": self.engine.ready,
            "engine_error": self.engine.last_error,
            "camera_entity": self.options.camera_entity,
            "trigger_entity": self.options.trigger_entity,
            "gate_entity": self.options.gate_entity,
            "gate_position_entity": self.gate.position_entity,
            "cooldown_remaining": round(self.gate.cooldown_remaining(self.settings.cooldown)),
        }

    def region(self) -> Region:
        s = self.settings
        return Region(s.region_left / 100, s.region_top / 100, s.region_right / 100, s.region_bottom / 100)

    def rules(self) -> Rules:
        s = self.settings
        return Rules(s.min_confidence / 100, s.min_consistent, s.min_plate_width)

    # session --------------------------------------------------------------
    async def on_vehicle(self) -> None:
        if not self.settings.recognition_enabled:
            _LOG.debug("Vehicle detected but recognition is switched off")
            return
        await self.run_session(test=False)

    async def run_session(self, test: bool) -> dict | None:
        if self._busy.locked():
            _LOG.info("Recognition already running; ignoring overlapping trigger")
            return None
        async with self._busy:
            try:
                return await self._session(test)
            finally:
                await self.publish_status("idle" if self.engine.ready else "engine not ready")

    async def _session(self, test: bool) -> dict:
        started_wall, started = time.time(), self._clock()
        await self.publish_status("capturing")
        region, rules = self.region(), self.rules()
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
                jpeg = await self.ha.camera_image(self.options.camera_entity)
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
            _LOG.warning("Recognition session failed: %s", error)

        approved = self.store.is_approved(verdict.plate) if verdict.accepted else None
        if error:
            result, reason, detail = "error", "error", error
        elif not verdict.accepted:
            result, reason, detail = "rejected", verdict.reason, ""
        elif not approved:
            result, reason, detail = "rejected", "not_approved", ""
        elif test:
            result, reason, detail = "approved_test", "test_only", "test recognition never operates the gate"
        elif not self.settings.auto_open:
            result, reason, detail = "approved_monitor", "auto_open_disabled", "log-only mode; gate not operated"
        elif not self.ha.connected:
            result, reason, detail = "approved_not_opened", "ha_disconnected", "Home Assistant link uncertain"
        elif not (self.mqtt and self.mqtt.connected):
            result, reason, detail = ("approved_not_opened", "controls_offline",
                                      "dashboard controls unreachable; automatic opening needs MQTT")
        else:
            gate_result, detail = await self.gate.open(self.settings.cooldown)
            result = "granted" if gate_result in ("opened", "already_open") else "approved_not_opened"
            reason = gate_result

        event = {
            "ts": started_wall,
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
                "verdict": {k: v for k, v in asdict(verdict).items() if k != "best_box"},
                "frames": len(frames),
                "timings_ms": timings,
                "duration_s": round(self._clock() - started, 2),
                "reads": [[{"text": r.text, "ocr": r.ocr_conf, "width": r.width} for r in f.reads] for f in frames],
            },
        }
        if not frames and not error:
            return event  # nothing captured (should not happen), nothing worth logging
        event["id"] = self.store.add_event(event)
        await self._save_images(event, frames, jpegs, verdict, region)
        self.last_event = event
        _LOG.info("%s %s (%s) %s", result.upper(), verdict.plate or "-", reason, detail)
        await self._publish_event(event)
        try:
            await self.ha.fire_event(EVENT_TYPE, {k: event[k] for k in
                                                  ("plate", "result", "reason", "granted", "confidence", "label", "test")})
        except HAError as err:
            _LOG.warning("Could not fire %s event: %s", EVENT_TYPE, err)
        return event

    async def _save_images(self, event, frames, jpegs, verdict, region) -> None:
        if not jpegs:
            return
        index = verdict.best_frame if verdict.best_frame is not None else len(jpegs) - 1
        label = f"{verdict.plate or '?'} {verdict.confidence * 100:.0f}%" if verdict.plate else ""
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

    async def _publish_event(self, event: dict) -> None:
        if not self.mqtt:
            return
        attrs = {k: event.get(k) for k in ("id", "plate", "label", "result", "reason", "granted",
                                           "confidence", "test")}
        attrs["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(event["ts"]))
        attrs.update(event["detail"])
        attrs["recent"] = [
            {"time": time.strftime("%d %b %H:%M:%S", time.localtime(e["ts"])), "plate": e["plate"],
             "result": e["result"], "reason": e["reason"], "confidence": e["confidence"]}
            for e in self.store.events(limit=10)
        ]
        await self.mqtt.publish("last_plate", event.get("plate") or "none")
        await self.mqtt.publish("last_result", event["result"], attrs)
        await self.mqtt.publish("last_confidence", event.get("confidence"))
        timings = event["detail"]["timings_ms"]
        if timings:
            await self.mqtt.publish("recognition_time", round(sum(timings) / len(timings)))
        if event.get("plate_image"):
            await self.mqtt.publish_image("last_plate_image", (self.store.images / event["plate_image"]).read_bytes())
        recent = [e for e in self.store.events(limit=RECENT_SLOTS * 3) if e.get("image")][:RECENT_SLOTS]
        for slot, e in enumerate(recent, start=1):
            path = self.store.images / e["image"]
            if path.exists():
                await self.mqtt.publish_image(f"recent_{slot}", path.read_bytes())

    async def plates_changed(self) -> None:
        if self.mqtt:
            await self.mqtt.publish("approved_count", self.store.approved_count())
