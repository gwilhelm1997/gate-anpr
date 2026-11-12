"""Gate ANPR app entry point.

Inside Home Assistant everything comes from the Supervisor. For local development set
HA_URL + HA_TOKEN (long-lived token), DATA_DIR, and optionally MQTT_HOST/MQTT_PORT/MQTT_USER/MQTT_PASSWORD.
"""

import asyncio
import ipaddress
import json
import logging
import os
import signal
import socket
import sys
from pathlib import Path

import aiohttp

from . import dashboard
from .ha import HomeAssistant
from .mqtt import MqttBridge
from .recognition import RecognitionEngine
from .service import GateAnpr, Options
from .store import Store
from .web import SUPERVISOR_IP, create_app, serve

_LOG = logging.getLogger("gate_anpr")
VERSION = os.environ.get("GATE_ANPR_VERSION", "dev")
SUPERVISOR = "http://supervisor"


async def supervisor_get(session: aiohttp.ClientSession, token: str, path: str) -> dict | None:
    try:
        async with session.get(f"{SUPERVISOR}{path}", headers={"Authorization": f"Bearer {token}"},
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                _LOG.info("Supervisor %s -> HTTP %s", path, resp.status)
                return None
            return (await resp.json()).get("data")
    except aiohttp.ClientError as err:
        _LOG.warning("Supervisor %s failed: %s", path, err)
        return None


async def main() -> None:
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    opts = json.loads((data_dir / "options.json").read_text())
    logging.basicConfig(level=opts.get("log_level", "info").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    for noisy in ("aiohttp.access", "open_image_models", "fast_plate_ocr"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    token = os.environ.get("SUPERVISOR_TOKEN")
    dev = not token
    if dev:
        token = os.environ["HA_TOKEN"]
        ha_url = os.environ["HA_URL"].rstrip("/")
        rest_url, ws_url = f"{ha_url}/api", ha_url.replace("http", "ws", 1) + "/api/websocket"
    else:
        rest_url, ws_url = f"{SUPERVISOR}/core/api", "ws://supervisor/core/websocket"

    options = Options(
        max_frames=opts.get("max_frames", 6), capture_window=opts.get("capture_window", 8.0),
        frame_interval=opts.get("frame_interval", 0.6), confirm_timeout=opts.get("confirm_timeout", 20),
        retention_days=opts.get("retention_days", 30),
    )
    store = Store(data_dir)
    store.migrate(opts)
    engine = RecognitionEngine(opts.get("detector_model", "yolo-v9-t-640-license-plate-end2end"),
                               opts.get("ocr_model", "cct-s-v2-global-model"), opts.get("ocr_threads", 2))

    async with aiohttp.ClientSession() as session:
        ha = HomeAssistant(session, rest_url, ws_url, token)

        if dev:
            mqtt_conf = {"host": os.environ["MQTT_HOST"], "port": os.environ.get("MQTT_PORT", 1883),
                         "username": os.environ.get("MQTT_USER"), "password": os.environ.get("MQTT_PASSWORD")}                 if os.environ.get("MQTT_HOST") else None
        else:
            mqtt_conf = await supervisor_get(session, token, "/services/mqtt")
        if not mqtt_conf:
            _LOG.warning("No MQTT broker found: dashboard entities unavailable and automatic opening blocked. "
                         "Install the Mosquitto broker app and the MQTT integration.")

        service = GateAnpr(options, store, engine, ha, version=VERSION)
        if mqtt_conf:
            service.mqtt = MqttBridge(mqtt_conf, service.on_mqtt_command, service.announce)
        # An app's container hostname is its slug with dashes, e.g. local-gate-anpr.
        manage_url = None if dev else f"/app/{socket.gethostname().replace('-', '_')}"
        dashboard_lock = asyncio.Lock()

        async def refresh_dashboard():
            if not opts.get("create_dashboard", True):
                return
            async with dashboard_lock:
                try:
                    await dashboard.ensure(ha, store, [rt.lane for rt in service.lanes.values()], manage_url)
                except Exception as err:  # noqa: BLE001 - dashboard is a convenience, never fatal
                    _LOG.warning("Could not update the dashboard: %s", err)

        async def on_lanes_changed():
            await ha.resubscribe()
            asyncio.create_task(refresh_dashboard())

        async def on_ha_connected():
            for rt in service.lanes.values():
                await service.publish_status(rt)
            await refresh_dashboard()

        service.on_lanes_changed = on_lanes_changed

        async def housekeeping():
            while True:
                removed = await asyncio.to_thread(store.prune, options.retention_days)
                if removed:
                    _LOG.info("Removed %s events older than %s days", removed, options.retention_days)
                await asyncio.sleep(6 * 3600)

        allowed = {SUPERVISOR_IP} | ({ipaddress.ip_address("127.0.0.1")} if dev else set())
        runner = await serve(create_app(service, allowed), int(os.environ.get("INGRESS_PORT", 8099)))
        await engine.start()

        tasks = [asyncio.create_task(ha.run(service.triggers, service.on_vehicle, on_ha_connected)),
                 asyncio.create_task(housekeeping())]
        if service.mqtt:
            tasks.append(asyncio.create_task(service.mqtt.run()))

        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows dev runs
                pass
        try:
            await stop.wait()
        finally:
            _LOG.info("Stopping")
            if service.mqtt:
                await service.mqtt.stop()
            for task in tasks:
                task.cancel()
            await runner.cleanup()
            engine.close()
            store.close()


if __name__ == "__main__":
    if sys.platform == "win32":  # aiomqtt needs a selector loop on Windows (local development only)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
