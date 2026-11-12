"""Home Assistant Core REST + WebSocket client (through the Supervisor proxy inside the app)."""

import asyncio
import itertools
import json
import logging
from collections.abc import Awaitable, Callable

import aiohttp

_LOG = logging.getLogger(__name__)


class HAError(Exception):
    """Home Assistant could not be reached or refused the request."""


class HomeAssistant:
    def __init__(self, session: aiohttp.ClientSession, rest_url: str, ws_url: str, token: str):
        self._session = session
        self._rest = rest_url.rstrip("/")
        self._ws_url = ws_url
        self._auth = {"Authorization": f"Bearer {token}"}
        self._token = token
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self.connected = False

    async def _request(self, method: str, path: str, timeout: float, **kw):
        try:
            async with self._session.request(
                method, f"{self._rest}{path}", headers=self._auth,
                timeout=aiohttp.ClientTimeout(total=timeout), **kw,
            ) as resp:
                if resp.status == 404 and method == "GET":
                    return None
                if resp.status >= 400:
                    raise HAError(f"{method} {path} -> HTTP {resp.status}: {(await resp.text())[:200]}")
                if resp.content_type == "application/json":
                    return await resp.json()
                return await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise HAError(f"{method} {path} failed: {err!r}") from err

    async def get_state(self, entity_id: str) -> dict | None:
        return await self._request("GET", f"/states/{entity_id}", 10)

    async def call_service(self, domain: str, service: str, data: dict) -> None:
        await self._request("POST", f"/services/{domain}/{service}", 15, json=data)

    async def camera_image(self, entity_id: str, timeout: float = 10) -> bytes:
        body = await self._request("GET", f"/camera_proxy/{entity_id}", timeout)
        if not body or not isinstance(body, bytes):
            raise HAError(f"camera {entity_id} returned no image")
        return body

    async def fire_event(self, event_type: str, data: dict) -> None:
        await self._request("POST", f"/events/{event_type}", 10, json=data)

    async def ws_command(self, msg: dict, timeout: float = 15):
        if not self._ws or self._ws.closed:
            raise HAError("websocket not connected")
        msg_id = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send_json({**msg, "id": msg_id})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(msg_id, None)

    async def run(self, trigger_entity: str, on_trigger: Callable[[], Awaitable[None]],
                  on_connected: Callable[[], Awaitable[None]] | None = None) -> None:
        """Stay subscribed to the vehicle-detection edge; reconnect forever (HA restarts, updates)."""
        delay = 2
        while True:
            try:
                async with self._session.ws_connect(self._ws_url, heartbeat=30, max_msg_size=0) as ws:
                    await self._handshake(ws)
                    self._ws, delay = ws, 2
                    sub_id = next(self._ids)
                    await ws.send_json({
                        "id": sub_id, "type": "subscribe_trigger",
                        "trigger": {"platform": "state", "entity_id": trigger_entity, "from": "off", "to": "on"},
                    })
                    self.connected = True
                    _LOG.info("Connected to Home Assistant; watching %s", trigger_entity)
                    if on_connected:
                        asyncio.create_task(on_connected())
                    async for raw in ws:
                        if raw.type != aiohttp.WSMsgType.TEXT:
                            break
                        msg = json.loads(raw.data)
                        if msg.get("type") == "event" and msg.get("id") == sub_id:
                            asyncio.create_task(on_trigger())
                        elif msg.get("type") == "result":
                            fut = self._pending.get(msg.get("id"))
                            if fut and not fut.done():
                                if msg.get("success"):
                                    fut.set_result(msg.get("result"))
                                else:
                                    fut.set_exception(HAError(str(msg.get("error"))))
                            elif msg.get("id") == sub_id and not msg.get("success"):
                                raise HAError(f"subscribe_trigger rejected: {msg.get('error')}")
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - any failure means reconnect
                _LOG.warning("Home Assistant connection lost (%s); retrying in %ss", err, delay)
            finally:
                self.connected = False
                self._ws = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(HAError("websocket closed"))
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _handshake(self, ws) -> None:
        first = await ws.receive_json(timeout=10)
        if first.get("type") != "auth_required":
            raise HAError(f"unexpected websocket greeting: {first}")
        await ws.send_json({"type": "auth", "access_token": self._token})
        reply = await ws.receive_json(timeout=10)
        if reply.get("type") != "auth_ok":
            raise HAError(f"websocket auth failed: {reply.get('message', reply)}")
