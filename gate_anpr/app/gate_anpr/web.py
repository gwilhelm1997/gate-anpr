"""Ingress UI: approved registrations, access history with images, settings and diagnostics."""

import ipaddress
import logging
import re
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

from .store import Settings

_LOG = logging.getLogger(__name__)
_STATIC = Path(__file__).parent / "static"
_IMAGE_NAME = re.compile(r"\d{7}(_plate)?\.jpg")
SUPERVISOR_IP = ipaddress.ip_address("172.30.32.2")


def create_app(service, allowed_ips: set) -> web.Application:
    @web.middleware
    async def ingress_only(request: web.Request, handler):
        # Supervisor authenticates the user; accept nothing that did not come through it.
        if ipaddress.ip_address(request.remote or "0.0.0.0") not in allowed_ips:
            raise web.HTTPForbidden(text="Gate ANPR is only available through Home Assistant")
        if request.method != "GET" and request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="JSON required")  # blocks cross-site form posts
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def index(_):
        return web.FileResponse(_STATIC / "index.html")

    async def state(_):
        return web.json_response({
            "settings": asdict(service.settings),
            "limits": Settings.LIMITS,
            "status": service.status,
            "diagnostics": service.diagnostics(),
            "engine": service.engine.info(),
            "plates": service.store.plates(),
        })

    async def events(request):
        granted = request.query.get("granted")
        rows = service.store.events(
            limit=int(request.query.get("limit", 50)),
            granted=None if granted in (None, "") else granted == "1",
        )
        return web.json_response(rows)

    async def save_plate(request):
        body = await request.json()
        try:
            plate = service.store.upsert_plate(str(body.get("plate", "")), str(body.get("label", "")),
                                               bool(body.get("enabled", True)))
        except ValueError as err:
            raise web.HTTPBadRequest(text=str(err)) from err
        _LOG.info("Approved registration saved: %s", plate)
        await service.plates_changed()
        return web.json_response({"plate": plate})

    async def delete_plate(request):
        if not service.store.delete_plate(request.match_info["plate"]):
            raise web.HTTPNotFound(text="registration not found")
        _LOG.info("Approved registration removed: %s", request.match_info["plate"])
        await service.plates_changed()
        return web.json_response({"ok": True})

    async def save_settings(request):
        body = await request.json()
        try:
            for key in body:
                Settings.coerce(key, body[key])  # validate everything before changing anything
            for key, value in body.items():
                await service.set_setting(key, value)
        except ValueError as err:
            raise web.HTTPBadRequest(text=str(err)) from err
        return web.json_response(asdict(service.settings))

    async def test(_):
        event = await service.run_session(test=True)
        if event is None:
            raise web.HTTPConflict(text="recognition already running")
        return web.json_response(event)

    async def image(request):
        name = request.match_info["name"]
        if not _IMAGE_NAME.fullmatch(name):
            raise web.HTTPNotFound()
        path = service.store.images / name
        if not path.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": "image/jpeg"})

    async def snapshot(_):
        try:
            jpeg = await service.ha.camera_image(service.options.camera_entity)
            frame, _plate = await service.engine.annotate(jpeg, None, "", service.region(), 1280)
        except Exception as err:  # noqa: BLE001
            raise web.HTTPBadGateway(text=f"snapshot failed: {err}") from err
        return web.Response(body=frame, content_type="image/jpeg")

    app = web.Application(middlewares=[ingress_only], client_max_size=64 * 1024)
    app.add_routes([
        web.get("/", index),
        web.get("/api/state", state),
        web.get("/api/events", events),
        web.post("/api/plates", save_plate),
        web.delete("/api/plates/{plate}", delete_plate),
        web.post("/api/settings", save_settings),
        web.post("/api/test", test),
        web.get("/api/snapshot", snapshot),
        web.get("/images/{name}", image),
    ])
    return app


async def serve(app: web.Application, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    _LOG.info("Ingress UI listening on port %s", port)
    return runner

