"""The native Gate ANPR dashboard (standard Lovelace cards only), one view per lane."""

import asyncio
import hashlib
import json
import logging
import re

from .ha import HAError, HomeAssistant
from .mqtt import BASE, GLOBAL_ENTITIES, LANE_ENTITIES, RECENT_SLOTS, lane_key

_LOG = logging.getLogger(__name__)
URL_PATH = "gate-anpr"


def _slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]", "_", text.lower())).strip("_")


def predicted_ids(lanes) -> dict[str, str]:
    """Entity IDs Home Assistant derives from device + entity names, used when the registry lags."""
    out = {k: f"{c}.gate_anpr_{_slug(e['name'])}" for k, (c, e) in GLOBAL_ENTITIES.items()}
    for lane in lanes:
        device = _slug(f"Gate ANPR {lane.name}")
        for k, (c, e) in LANE_ENTITIES.items():
            out[lane_key(lane.id, k)] = f"{c}.{device}_{_slug(e['name'])}"
    return out


def _lane_view(lane, ids: dict, manage_url: str | None) -> dict:
    def e(key):
        return ids[lane_key(lane.id, key)]

    history_md = (
        "{% set r = state_attr('" + e("last_result") + "', 'recent') or [] %}"
        "| Time | Registration | Result | Reason | Conf. |\n|---|---|---|---|---|\n"
        "{% for x in r %}| {{ x.time }} | **{{ x.plate or '-' }}** | "
        "{{ '✅' if x.result == 'granted' else ('🟡' if x.result.startswith('approved') else '⛔') }} "
        "{{ x.result }} | {{ x.reason }} | {{ x.confidence or '-' }} |\n{% endfor %}"
    )
    manage = f"[Manage gates, approved registrations and full history]({manage_url})" if manage_url else \
        "Open **Gate ANPR** from the sidebar to manage gates and approved registrations."
    access = "Allow all vehicles" if lane.access == "all" else "Approved vehicles only"
    return {
        "title": lane.name,
        "path": f"lane-{lane.id}",
        "icon": "mdi:gate",
        "type": "masonry",
        "cards": [
            {"type": "picture-entity", "entity": lane.live_camera_entity or lane.camera_entity,
             "camera_view": "live", "show_state": False, "name": lane.name},
            {"type": "entities", "title": f"{lane.name} control", "show_header_toggle": False, "entities": [
                {"entity": e("auto_open")},
                {"entity": e("enabled")},
                {"entity": lane.gate_entity, "name": "Gate OPEN control"},
                {"entity": e("test")},
                {"entity": e("status")},
                {"type": "attribute", "entity": e("status"), "attribute": "gate_check", "name": "Gate check"},
                {"type": "attribute", "entity": e("status"), "attribute": "cooldown_remaining",
                 "name": "Cooldown remaining", "suffix": "s"},
                {"type": "section", "label": f"Access: {access} · direction: {lane.direction}"},
            ]},
            {"type": "entities", "title": "Last recognition", "entities": [
                {"entity": e("last_plate")},
                {"entity": e("last_result")},
                {"type": "attribute", "entity": e("last_result"), "attribute": "reason", "name": "Reason"},
                {"type": "attribute", "entity": e("last_result"), "attribute": "label", "name": "Vehicle"},
                {"entity": e("last_confidence")},
                {"type": "attribute", "entity": e("last_result"), "attribute": "time", "name": "Time"},
                {"entity": ids["approved_count"]},
            ]},
            {"type": "picture-entity", "entity": e("last_plate_image"), "name": "Last plate", "show_state": False},
            {"type": "grid", "columns": 2, "square": False, "title": "Recent detections", "cards": [
                {"type": "picture-entity", "entity": e(f"recent_{i}"), "show_state": False, "show_name": False}
                for i in range(1, RECENT_SLOTS + 1)
            ]},
            {"type": "markdown", "title": "Access history", "content": history_md + "\n" + manage},
            {"type": "logbook", "title": "Granted and rejected", "hours_to_show": 168,
             "target": {"entity_id": [e("last_result")]}},
            {"type": "entities", "title": "Recognition settings", "entities": [
                {"entity": e(k)} for k in ("min_confidence", "min_consistent", "cooldown", "min_plate_width",
                                           "region_left", "region_top", "region_right", "region_bottom")
            ]},
            {"type": "entities", "title": "Diagnostics", "entities": [
                {"entity": ids["engine"]},
                {"entity": e("recognition_time")},
                {"type": "attribute", "entity": ids["engine"], "attribute": "warmup_ms", "name": "Self-test",
                 "suffix": "ms"},
                {"type": "attribute", "entity": e("status"), "attribute": "home_assistant_connected",
                 "name": "HA link"},
                {"type": "attribute", "entity": e("status"), "attribute": "mqtt_connected", "name": "MQTT link"},
                {"type": "attribute", "entity": e("last_result"), "attribute": "verdict", "name": "Verdict"},
                {"type": "attribute", "entity": e("last_result"), "attribute": "timings_ms",
                 "name": "Frame timings (ms)"},
            ]},
        ],
    }


def build(lanes, ids: dict[str, str], manage_url: str | None) -> dict:
    views = [_lane_view(lane, ids, manage_url) for lane in lanes]
    if not views:
        views = [{"title": "Gate ANPR", "path": "setup", "cards": [{"type": "markdown", "content":
                  "No gates configured yet. Open **Gate ANPR** in the sidebar and add a gate."}]}]
    return {"title": "Gate ANPR", "views": views}


def fingerprint(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


async def resolve_ids(ha: HomeAssistant, lanes, attempts: int = 15) -> dict[str, str]:
    """Look up real entity IDs by unique_id (the user may have renamed them)."""
    predicted = predicted_ids(lanes)
    found: dict[str, str] = {}
    for _ in range(attempts):
        try:
            entries = await ha.ws_command({"type": "config/entity_registry/list"})
            found = {en["unique_id"][len(BASE) + 1:]: en["entity_id"] for en in entries
                     if en.get("platform") == "mqtt" and str(en.get("unique_id", "")).startswith(f"{BASE}_")}
            if set(predicted) <= set(found):
                return found
        except HAError as err:
            _LOG.debug("Entity registry not ready: %s", err)
        await asyncio.sleep(2)
    return {**predicted, **{k: v for k, v in found.items() if k in predicted}}


def _is_legacy_generated(config: dict) -> bool:
    """The 0.1.x app dashboard: a single 'gate' view."""
    return config.get("title") == "Gate ANPR" and [v.get("path") for v in config.get("views", [])] == ["gate"]


async def ensure(ha: HomeAssistant, store, lanes, manage_url: str | None) -> bool:
    """Create or refresh the dashboard. A dashboard the user has edited is never overwritten."""
    dashboards = await ha.ws_command({"type": "lovelace/dashboards/list"})
    exists = any(d.get("url_path") == URL_PATH for d in dashboards)
    if exists:
        try:
            current = await ha.ws_command({"type": "lovelace/config", "url_path": URL_PATH}) or {}
        except HAError:
            current = {}
        if fingerprint(current) != store.meta("dashboard_hash") and not _is_legacy_generated(current):
            _LOG.info("Gate ANPR dashboard has been edited by hand; leaving it unchanged")
            return False
    ids = await resolve_ids(ha, lanes)
    config = build(lanes, ids, manage_url)
    if exists and fingerprint(config) == store.meta("dashboard_hash"):
        return False
    if not exists:
        await ha.ws_command({"type": "lovelace/dashboards/create", "url_path": URL_PATH, "title": "Gate ANPR",
                             "icon": "mdi:gate", "require_admin": True, "show_in_sidebar": True,
                             "mode": "storage"})
    await ha.ws_command({"type": "lovelace/config/save", "url_path": URL_PATH, "config": config})
    store.set_meta("dashboard_hash", fingerprint(config))
    _LOG.info("%s the Gate ANPR dashboard at /%s", "Updated" if exists else "Created", URL_PATH)
    return True
