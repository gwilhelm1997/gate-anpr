"""Ingress UI: gates (lanes), approved registrations, access history with images, diagnostics."""

import ipaddress
import logging
import re
from pathlib import Path

from aiohttp import web

from .store import CHOICES, LIMITS

_LOG = logging.getLogger(__name__)
_STATIC = Path(__file__).parent / "static"
_IMAGE_NAME = re.compile(r"\d{7}(_plate)?\.jpg")
SUPERVISOR_IP = ipaddress.ip_address("172.30.32.2")


def entity_choices(states: list[dict]) -> dict:
    """Entities offered in the lane editor's pickers."""
    def item(s):
        return {"id": s["entity_id"], "name": s.get("attributes", {}).get("friendly_name") or s["entity_id"],
                "state": s.get("state")}

    by_domain: dict[str, list] = {}
    for s in states:
        by_domain.setdefault(s["entity_id"].split(".")[0], []).append(s)
    sort = lambda xs: sorted((item(s) for s in xs), key=lambda x: x["id"])  # noqa: E731
    binary = by_domain.get("binary_sensor", [])
    # Never offer toggle, close or stop inputs, or Gate ANPR's own switches, as a gate OPEN control.
    blocked = re.compile(r"toggle|close|stop", re.I)
    gates = [s for s in by_domain.get("switch", []) + by_domain.get("cover", [])
             if not s["entity_id"].startswith("switch.gate_anpr_")
             and not blocked.search(f'{s["entity_id"]} {s.get("attributes", {}).get("friendly_name", "")}')]
    return {
        "cameras": sort(by_domain.get("camera", [])),
        "triggers": sort([s for s in binary if "vehicle" in s["entity_id"]]) or sort(binary),
        "motions": sort([s for s in binary if "motion" in s["entity_id"]]) or sort(binary),
        "gates": sort(gates),
        "positions": sort(binary + by_domain.get("cover", [])),
    }


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

    def lane_id(request) -> int:
        try:
            lid = int(request.match_info["lane"])
        except ValueError:
            raise web.HTTPNotFound() from None
        if lid not in service.lanes:
            raise web.HTTPNotFound(text="gate not found")
        return lid

    async def index(_):
        return web.FileResponse(_STATIC / "index.html")

    async def state(_):
        return web.json_response({
            "lanes": [{**rt.lane.public(), "status": rt.status, "stream": service.stream_info(rt.lane.id)}
                      for rt in service.lanes.values()],
            "limits": LIMITS,
            "choices": CHOICES,
            "diagnostics": service.diagnostics(),
            "engine": service.engine.info(),
            "plates": service.store.plates(),
        })

    async def entities(_):
        try:
            return web.json_response(entity_choices(await service.ha.states()))
        except Exception as err:  # noqa: BLE001
            raise web.HTTPBadGateway(text=f"Home Assistant entities unavailable: {err}") from err

    async def events(request):
        granted = request.query.get("granted")
        lane = request.query.get("lane")
        rows = service.store.events(
            limit=int(request.query.get("limit", 50)),
            granted=None if granted in (None, "") else granted == "1",
            lane_id=int(lane) if lane else None,
        )
        return web.json_response(rows)

    async def save_lane(request):
        body = await request.json()
        lid = int(body.pop("id", 0) or 0)
        if lid and lid not in service.lanes:
            raise web.HTTPNotFound(text="gate not found")
        try:
            lane = await service.save_lane(body, lid)
        except ValueError as err:
            raise web.HTTPBadRequest(text=str(err)) from err
        return web.json_response(lane.public())

    async def delete_lane(request):
        await service.delete_lane(lane_id(request))
        return web.json_response({"ok": True})

    async def save_plate(request):
        body = await request.json()
        lanes = body.get("lanes") or []
        if not isinstance(lanes, list) or any(int(x) not in service.lanes for x in lanes):
            raise web.HTTPBadRequest(text="unknown gate in lanes")
        try:
            plate = service.store.upsert_plate(str(body.get("plate", "")), str(body.get("label", "")),
                                               bool(body.get("enabled", True)), lanes)
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

    async def test(request):
        lid = lane_id(request)
        event = await service.run_session(lid, test=True)
        if event is None:
            raise web.HTTPConflict(text="recognition already running")
        return web.json_response(event)

    async def snapshot(request):
        rt = service.lanes[lane_id(request)]
        try:
            jpeg = await service.ha.camera_image(rt.lane.camera_entity)
            frame, _plate = await service.engine.annotate(jpeg, None, "", rt.region(), 1280,
                                                          rt.lane.exclusion_boxes())
        except Exception as err:  # noqa: BLE001
            raise web.HTTPBadGateway(text=f"snapshot failed: {err}") from err
        return web.Response(body=frame, content_type="image/jpeg")

    async def image(request):
        name = request.match_info["name"]
        if not _IMAGE_NAME.fullmatch(name):
            raise web.HTTPNotFound()
        path = service.store.images / name
        if not path.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": "image/jpeg"})

    app = web.Application(middlewares=[ingress_only], client_max_size=64 * 1024)
    app.add_routes([
        web.get("/", index),
        web.get("/api/state", state),
        web.get("/api/entities", entities),
        web.get("/api/events", events),
        web.post("/api/lanes", save_lane),
        web.delete("/api/lanes/{lane}", delete_lane),
        web.post("/api/lanes/{lane}/test", test),
        web.get("/api/lanes/{lane}/snapshot", snapshot),
        web.post("/api/plates", save_plate),
        web.delete("/api/plates/{plate}", delete_plate),
        web.get("/images/{name}", image),
    ])
    return app


async def serve(app: web.Application, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    _LOG.info("Ingress UI listening on port %s", port)
    return runner
