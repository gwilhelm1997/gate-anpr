import asyncio
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "gate_anpr" / "app"))

from gate_anpr.ha import HAError  # noqa: E402

if sys.platform == "win32":  # aiomqtt needs a selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class FakeHA:
    """Just enough of Home Assistant: states, service calls, camera frames and events."""

    def __init__(self, states=None, frames=None):
        self.states = states or {}
        self.frames = list(frames or [])
        self.calls = []
        self.events = []
        self.connected = True
        self.fail_states = False
        self.fail_calls = False
        self.fail_camera = False
        self.release_after = 0.05  # inching relay pulse length
        self._tick = 0

    def set(self, entity_id, state, **attrs):
        self._tick += 1
        self.states[entity_id] = {"entity_id": entity_id, "state": state, "attributes": attrs,
                                  "last_changed": f"t{self._tick}"}

    async def get_state(self, entity_id):
        if self.fail_states:
            raise HAError("unreachable")
        return self.states.get(entity_id)

    async def call_service(self, domain, service, data):
        if self.fail_calls:
            raise HAError("service failed")
        self.calls.append((domain, service, data["entity_id"]))
        entity_id = data["entity_id"]
        attrs = self.states[entity_id]["attributes"]
        if (domain, service) == ("switch", "turn_on"):
            self.set(entity_id, "on", **attrs)

            async def release():
                await asyncio.sleep(self.release_after)
                self.set(entity_id, "off", **attrs)
            asyncio.get_running_loop().create_task(release())
        elif (domain, service) == ("cover", "open_cover"):
            self.set(entity_id, "opening", **attrs)

    async def camera_image(self, entity_id):
        if self.fail_camera:
            raise HAError("camera offline")
        return self.frames.pop(0) if self.frames else b"none"

    async def fire_event(self, event_type, data):
        self.events.append((event_type, data))


@pytest.fixture
def fake_ha():
    return FakeHA()
