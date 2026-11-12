"""Safe gate opening with an explicit OPEN command, never a toggle.

Two kinds of gate entity are supported:
- cover.*  : cover.open_cover, only from a confirmed 'closed' state.
- switch.* : a relay wired to the gate controller's dedicated OPEN input (momentary/inching).
             OPEN is idempotent on such controllers, so an open gate stays open.
An optional position entity (cover or binary_sensor, on = open) adds a 'must be closed' check.
"""

import asyncio
import logging
import time

from .ha import HAError, HomeAssistant

_LOG = logging.getLogger(__name__)
_COVER_SUPPORTS_OPEN = 1  # CoverEntityFeature.OPEN


class GateController:
    def __init__(self, ha: HomeAssistant, entity_id: str, position_entity: str | None = None,
                 confirm_timeout: float = 20, clock=time.monotonic, poll_interval: float = 0.25):
        self.ha = ha
        self.entity_id = entity_id
        self.position_entity = position_entity or None
        self.confirm_timeout = confirm_timeout
        self._clock = clock
        self._poll = poll_interval
        self._last_command: float | None = None
        self._lock = asyncio.Lock()

    def cooldown_remaining(self, cooldown: float) -> float:
        if self._last_command is None:
            return 0.0
        return max(0.0, cooldown - (self._clock() - self._last_command))

    async def _state(self, entity_id: str) -> dict | None:
        try:
            return await self.ha.get_state(entity_id)
        except HAError:
            return None

    async def check(self) -> tuple[str | None, str]:
        """Pre-flight safety check. Returns (blocking_result, detail); None means safe to command."""
        domain = self.entity_id.split(".")[0]
        if domain not in ("cover", "switch"):
            return "unsupported_gate", f"{self.entity_id}: use a cover or the relay on the gate's OPEN input"
        try:
            state = await self.ha.get_state(self.entity_id)
        except HAError as err:
            return "gate_unavailable", str(err)
        if not state:
            return "gate_unavailable", f"{self.entity_id} not found"
        current = state.get("state")
        attrs = state.get("attributes", {})
        if domain == "switch":
            if "toggle" in str(attrs.get("friendly_name", "")).lower():
                return "unsupported_gate", f"{self.entity_id} looks like a toggle input; use the OPEN relay"
            if current != "off":
                return "gate_unavailable", f"OPEN relay is {current!r}; it must be idle ('off')"
        else:
            if not int(attrs.get("supported_features", 0)) & _COVER_SUPPORTS_OPEN:
                return "unsupported_gate", f"{self.entity_id} has no explicit open command"
            if current in ("open", "opening"):
                return "already_open", f"gate is {current}"
            if current != "closed":
                return "gate_not_closed", f"gate state is {current!r}; only 'closed' is safe to open from"
        if self.position_entity:
            pos = await self._state(self.position_entity)
            pos_state = pos.get("state") if pos else None
            if pos_state in ("on", "open", "opening"):
                return "already_open", f"{self.position_entity} reports {pos_state}"
            if pos_state not in ("off", "closed"):
                return "gate_not_closed", f"{self.position_entity} is {pos_state!r}; gate position uncertain"
        return None, ""

    async def open(self, cooldown: float) -> tuple[str, str]:
        """Returns (result, detail). Only 'opened' means the command was seen to take effect."""
        async with self._lock:
            remaining = self.cooldown_remaining(cooldown)
            if remaining:
                return "cooldown", f"gate was commanded {cooldown - remaining:.0f}s ago"
            blocked, detail = await self.check()
            if blocked:
                return blocked, detail
            before = await self._state(self.entity_id)
            domain = self.entity_id.split(".")[0]

            # Start the cooldown before calling, so a failed or slow call can never be repeated.
            self._last_command = self._clock()
            try:
                if domain == "cover":
                    await self.ha.call_service("cover", "open_cover", {"entity_id": self.entity_id})
                else:
                    await self.ha.call_service("switch", "turn_on", {"entity_id": self.entity_id})
            except HAError as err:
                return "command_failed", str(err)
            _LOG.info("Sent OPEN to %s", self.entity_id)
            return await self._confirm(domain, before or {})

    async def _confirm(self, domain: str, before: dict) -> tuple[str, str]:
        deadline = self._clock() + self.confirm_timeout
        pulsed = False
        current = None
        while self._clock() < deadline:
            await asyncio.sleep(self._poll)
            state = await self._state(self.entity_id) or {}
            current = state.get("state")
            if domain == "cover":
                if current in ("opening", "open"):
                    return "opened", f"gate confirmed {current}"
            else:
                # Inching relays pulse for ~0.5 s: a changed timestamp proves the pulse even if 'on' was missed.
                pulsed = pulsed or current == "on" or state.get("last_changed") != before.get("last_changed")
                if pulsed and current == "off":
                    return "opened", "OPEN relay pulsed and released"
        if domain == "switch" and pulsed:
            return "opened", f"OPEN relay pulsed but is still {current!r}; check its inching setting"
        return "open_unconfirmed", f"no confirmation within {self.confirm_timeout:.0f}s (last {current!r})"
