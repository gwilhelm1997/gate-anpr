"""The native Gate ANPR dashboard (standard Lovelace cards only)."""

import asyncio
import logging

from .ha import HAError, HomeAssistant
from .mqtt import BASE, ENTITIES, RECENT_SLOTS

_LOG = logging.getLogger(__name__)
URL_PATH = "gate-anpr"


def predicted_ids() -> dict[str, str]:
    """Entity IDs Home Assistant derives from device + entity names, used when the registry is unavailable."""
    out = {}
    for key, (component, extra) in ENTITIES.items():
        slug = "".join(c if c.isalnum() else "_" for c in extra["name"].lower()).strip("_")
        out[key] = f"{component}.gate_anpr_{slug}"
    return out


def build(ids: dict[str, str], camera: str, gate: str, manage_url: str | None) -> dict:
    e = ids.__getitem__
    history_md = (
        "{% set r = state_attr('" + e("last_result") + "', 'recent') or [] %}"
        "| Time | Registration | Result | Reason | Conf. |\n|---|---|---|---|---|\n"
        "{% for x in r %}| {{ x.time }} | **{{ x.plate or '-' }}** | "
        "{{ '✅' if x.result == 'granted' else ('🟡' if x.result.startswith('approved') else '⛔') }} "
        "{{ x.result }} | {{ x.reason }} | {{ x.confidence or '-' }} |\n{% endfor %}"
    )
    manage = f"[Manage approved registrations and full history]({manage_url})" if manage_url else \
        "Open **Gate ANPR** from the sidebar to manage approved registrations."
    return {
        "title": "Gate ANPR",
        "views": [{
            "title": "Gate",
            "path": "gate",
            "icon": "mdi:gate",
            "type": "masonry",
            "cards": [
                {"type": "picture-entity", "entity": camera, "camera_view": "live", "show_state": False,
                 "name": "Gate camera"},
                {"type": "entities", "title": "Gate control", "show_header_toggle": False, "entities": [
                    {"entity": e("auto_open")},
                    {"entity": e("recognition_enabled")},
                    {"entity": gate, "name": "Gate"},
                    {"entity": e("test")},
                    {"entity": e("status")},
                    {"type": "attribute", "entity": e("status"), "attribute": "gate_check", "name": "Gate check"},
                    {"type": "attribute", "entity": e("status"), "attribute": "cooldown_remaining",
                     "name": "Cooldown remaining", "suffix": "s"},
                ]},
                {"type": "entities", "title": "Last recognition", "entities": [
                    {"entity": e("last_plate")},
                    {"entity": e("last_result")},
                    {"type": "attribute", "entity": e("last_result"), "attribute": "reason", "name": "Reason"},
                    {"type": "attribute", "entity": e("last_result"), "attribute": "label", "name": "Vehicle"},
                    {"entity": e("last_confidence")},
                    {"type": "attribute", "entity": e("last_result"), "attribute": "time", "name": "Time"},
                    {"entity": e("approved_count")},
                ]},
                {"type": "picture-entity", "entity": e("last_plate_image"), "name": "Last plate",
                 "show_state": False},
                {"type": "grid", "columns": 2, "square": False, "title": "Recent detections", "cards": [
                    {"type": "picture-entity", "entity": e(f"recent_{i}"), "show_state": False,
                     "show_name": False} for i in range(1, RECENT_SLOTS + 1)
                ]},
                {"type": "markdown", "title": "Access history", "content": history_md + "\n" + manage},
                {"type": "logbook", "title": "Granted and rejected", "hours_to_show": 168,
                 "target": {"entity_id": [e("last_result")]}},
                {"type": "entities", "title": "Recognition settings", "entities": [
                    {"entity": e(k)} for k in ("min_confidence", "min_consistent", "cooldown", "min_plate_width",
                                               "region_left", "region_top", "region_right", "region_bottom")
                ]},
                {"type": "entities", "title": "Diagnostics", "entities": [
                    {"entity": e("engine")},
                    {"entity": e("recognition_time")},
                    {"type": "attribute", "entity": e("engine"), "attribute": "warmup_ms", "name": "Self-test",
                     "suffix": "ms"},
                    {"type": "attribute", "entity": e("engine"), "attribute": "machine", "name": "CPU"},
                    {"type": "attribute", "entity": e("status"), "attribute": "home_assistant_connected",
                     "name": "HA link"},
                    {"type": "attribute", "entity": e("status"), "attribute": "mqtt_connected", "name": "MQTT link"},
                    {"type": "attribute", "entity": e("last_result"), "attribute": "verdict", "name": "Verdict"},
                    {"type": "attribute", "entity": e("last_result"), "attribute": "timings_ms",
                     "name": "Frame timings (ms)"},
                ]},
            ],
        }],
    }


async def resolve_ids(ha: HomeAssistant, attempts: int = 15) -> dict[str, str]:
    """Look up real entity IDs by unique_id (the user may have renamed them)."""
    predicted = predicted_ids()
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
    return predicted


async def ensure(ha: HomeAssistant, camera: str, gate: str, manage_url: str | None) -> bool:
    """Create the dashboard once. Never overwrites a dashboard the user already has or edited."""
    dashboards = await ha.ws_command({"type": "lovelace/dashboards/list"})
    if any(d.get("url_path") == URL_PATH for d in dashboards):
        return False
    ids = await resolve_ids(ha)
    await ha.ws_command({"type": "lovelace/dashboards/create", "url_path": URL_PATH, "title": "Gate ANPR",
                         "icon": "mdi:gate", "require_admin": True, "show_in_sidebar": True, "mode": "storage"})
    await ha.ws_command({"type": "lovelace/config/save", "url_path": URL_PATH,
                         "config": build(ids, camera, gate, manage_url)})
    _LOG.info("Created the Gate ANPR dashboard at /%s", URL_PATH)
    return True
