"""HTTP REST + WebSocket поверх :class:`plc_api.controller.PlcController`.

REST — команды (запрос → ответ с кодом ошибки), WebSocket ``/api/v1/ws`` —
поток телеметрии и событий (сервер сам шлёт каждый кадр опроса).

Блокирующие вызовы контроллера выполняются в пуле потоков, поэтому цикл
asyncio не ждёт Modbus. Описание эндпоинтов — python_panel/REST_API.md.
"""
from __future__ import annotations

import asyncio
import functools
import hmac
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set

from aiohttp import WSMsgType, web

from . import __version__
from .controller import ApiError, NotConnected, NotFound, PlcController
from .ladder import AXIS_NAMES, capabilities, parse_axis
from .openapi import build_spec

API = "/api/v1"
#: Спецификация открыта без токена: Swagger UI загружает её до «Authorize».
OPENAPI_PATH = f"{API}/openapi.json"
STATIC_DIR = Path(__file__).resolve().parent / "static"
EVENT_TYPES = frozenset({"telemetry", "connection", "command", "arrived", "blocked"})
_WS_QUEUE_SIZE = 200

_dumps = functools.partial(json.dumps, ensure_ascii=False)


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=_dumps)


def _error(status: int, code: str, message: str) -> web.Response:
    return _json({"error": {"code": code, "message": message}}, status=status)


# ---------------------------------------------------------------------- #
# Разбор тела запроса                                                    #
# ---------------------------------------------------------------------- #
_MISSING = object()


class _Number:
    pass


class _Bool:
    pass


NUMBER, BOOL = _Number(), _Bool()


async def _body(request: web.Request, spec: Dict[str, tuple]) -> dict:
    """Строгий разбор JSON: неизвестные поля и неверные типы -> 400.

    spec: имя -> (тип, значение по умолчанию | _MISSING для обязательного).
    """
    raw = await request.read()
    if raw.strip():
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ApiError(f"тело запроса — не JSON: {exc}") from None
    else:
        data = {}
    if not isinstance(data, dict):
        raise ApiError("тело запроса должно быть JSON-объектом")
    unknown = set(data) - set(spec)
    if unknown:
        raise ApiError(f"неизвестные поля: {', '.join(sorted(unknown))} "
                       f"(допустимо: {', '.join(spec) or 'нет'})")
    out = {}
    for name, (kind, default) in spec.items():
        if name not in data:
            if default is _MISSING:
                raise ApiError(f"не указано обязательное поле {name}")
            out[name] = default
            continue
        value = data[name]
        if kind is BOOL and not isinstance(value, bool):
            raise ApiError(f"{name} должно быть true/false")
        if kind is NUMBER and (isinstance(value, bool)
                               or not isinstance(value, (int, float))):
            raise ApiError(f"{name} должно быть числом")
        out[name] = value
    return out


def _axis(request: web.Request) -> int:
    token = request.match_info["axis"]
    n = parse_axis(token)
    if n is None:
        raise NotFound(f"неизвестная ось: {token!r} (допустимо: "
                       f"{', '.join(a.lower() for a in AXIS_NAMES.values())})")
    return n


async def _run(fn: Callable, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


_INTERNAL = frozenset({"issued_at_mono", "motion_gen"})


def _public(result: dict) -> dict:
    return {k: v for k, v in result.items() if k not in _INTERNAL}


def _sent(result: dict) -> web.Response:
    return _json({**_public(result), "status": "sent"}, status=202)


# ---------------------------------------------------------------------- #
# WebSocket-хаб: события из потока plc-io -> очереди клиентов            #
# ---------------------------------------------------------------------- #
@dataclass(eq=False)
class _Client:
    queue: "asyncio.Queue[dict]"
    events: Set[str] = field(default_factory=lambda: set(EVENT_TYPES))
    telemetry_interval_s: float = 0.0
    last_telemetry: float = 0.0


class _Hub:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._clients: Set[_Client] = set()

    def add(self, client: _Client) -> None:
        self._clients.add(client)

    def remove(self, client: _Client) -> None:
        self._clients.discard(client)

    def dispatch(self, event: dict) -> None:
        """Вызывается из потока plc-io."""
        try:
            self._loop.call_soon_threadsafe(self._fanout, event)
        except RuntimeError:
            pass    # цикл уже закрыт (остановка сервера)

    def _fanout(self, event: dict) -> None:
        now = time.monotonic()
        for c in list(self._clients):
            kind = event["type"]
            if kind not in c.events:
                continue
            if kind == "telemetry":
                if now - c.last_telemetry < c.telemetry_interval_s:
                    continue
                if c.queue.full():
                    continue        # медленный клиент: старые кадры не копим
                c.last_telemetry = now
            elif c.queue.full():
                c.queue.get_nowait()   # важное событие вытесняет самое старое
            c.queue.put_nowait(event)


# ---------------------------------------------------------------------- #
# Middleware                                                             #
# ---------------------------------------------------------------------- #
@web.middleware
async def _errors(request: web.Request, handler):
    try:
        return await handler(request)
    except ApiError as exc:
        return _error(exc.status, exc.code, exc.message)
    except web.HTTPException as exc:
        if exc.status >= 400:
            return _error(exc.status, exc.reason.lower().replace(" ", "_"), exc.reason)
        raise


def _auth(token: Optional[str]):
    @web.middleware
    async def middleware(request: web.Request, handler):
        if (token is None or not request.path.startswith(API)
                or request.path == OPENAPI_PATH):
            return await handler(request)
        supplied = ""
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            supplied = header[len("Bearer "):]
        elif request.path == f"{API}/ws":
            # Браузерный WebSocket не умеет ставить заголовки.
            supplied = request.query.get("token", "")
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            resp = _error(401, "unauthorized", "нужен заголовок Authorization: Bearer <token>")
            resp.headers["WWW-Authenticate"] = "Bearer"
            return resp
        return await handler(request)
    return middleware


# ---------------------------------------------------------------------- #
# Приложение                                                             #
# ---------------------------------------------------------------------- #
def create_app(controller: PlcController, token: Optional[str] = None,
               manage_controller: bool = True) -> web.Application:
    """Собрать aiohttp-приложение.

    manage_controller: запускать/останавливать контроллер вместе с приложением.
    """
    app = web.Application(middlewares=[_errors, _auth(token)])
    c = controller
    state: dict = {}
    routes = web.RouteTableDef()

    async def on_startup(_app: web.Application) -> None:
        hub = _Hub(asyncio.get_running_loop())
        state["hub"] = hub
        state["unsubscribe"] = c.subscribe(hub.dispatch)
        if manage_controller:
            c.start()

    async def on_cleanup(_app: web.Application) -> None:
        state.pop("unsubscribe", lambda: None)()
        if manage_controller:
            await _run(c.shutdown)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # --- документация ------------------------------------------------- #
    spec = build_spec(token_required=token is not None)

    @routes.get("/")
    async def root(_r: web.Request) -> web.Response:
        raise web.HTTPFound("/docs")

    @routes.get("/docs")
    async def docs(_r: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "docs.html")

    @routes.get(OPENAPI_PATH)
    async def openapi(_r: web.Request) -> web.Response:
        return _json(spec)

    routes.static("/docs/static", STATIC_DIR)

    # --- состояние ---------------------------------------------------- #
    @routes.get(f"{API}/health")
    async def health(_r: web.Request) -> web.Response:
        return _json({"version": __version__, **c.status()})

    @routes.get(f"{API}/capabilities")
    async def caps(_r: web.Request) -> web.Response:
        cfg = c._cfg  # noqa: SLF001 - конфигурация только для чтения
        lim = cfg.limits
        return _json({
            **capabilities(),
            "active_axes": [AXIS_NAMES[n] for n in cfg.active_axes],
            "arrival_tolerance_mm": cfg.arrival_tolerance_mm,
            "soft_limits": None if lim is None else {
                a: [getattr(lim, f"{a.lower()}_min"), getattr(lim, f"{a.lower()}_max")]
                for a in AXIS_NAMES.values()},
        })

    async def _snapshot() -> dict:
        snap = c.telemetry()
        if snap is None and c.connected:
            snap = await _run(c.wait_for_telemetry, 2.0)
        if snap is None:
            raise NotConnected(f"нет телеметрии: {c.status()['detail']}")
        return snap

    @routes.get(f"{API}/telemetry")
    async def telemetry(_r: web.Request) -> web.Response:
        return _json(await _snapshot())

    @routes.get(API + "/axes/{axis}")
    async def axis_state(r: web.Request) -> web.Response:
        n = _axis(r)
        return _json((await _snapshot())["axes"][AXIS_NAMES[n]])

    # --- команды осей ------------------------------------------------- #
    @routes.post(API + "/axes/{axis}/power")
    async def power(r: web.Request) -> web.Response:
        n = _axis(r)
        b = await _body(r, {"on": (BOOL, _MISSING)})
        return _sent(await _run(c.power, n, b["on"]))

    @routes.post(API + "/axes/{axis}/reset")
    async def reset(r: web.Request) -> web.Response:
        n = _axis(r)
        await _body(r, {})
        return _sent(await _run(c.reset, n))

    @routes.post(API + "/axes/{axis}/move-absolute")
    async def move_absolute(r: web.Request) -> web.Response:
        n = _axis(r)
        b = await _body(r, {"position": (NUMBER, _MISSING), "speed": (NUMBER, _MISSING),
                            "pulse": (BOOL, False), "wait": (BOOL, False),
                            "timeout_s": (NUMBER, 60.0)})
        if b["timeout_s"] <= 0:
            raise ApiError("timeout_s должно быть больше 0")
        result = await _run(c.move_absolute, n, b["position"], b["speed"], b["pulse"])
        if not b["wait"]:
            return _sent(result)
        axis = await _run(c.wait_arrival, n, b["position"],
                          result["issued_at_mono"], b["timeout_s"],
                          result["motion_gen"])
        return _json({**_public(result), "status": "arrived", "state": axis})

    @routes.post(API + "/axes/{axis}/move-relative")
    async def move_relative(r: web.Request) -> web.Response:
        n = _axis(r)
        b = await _body(r, {"distance": (NUMBER, _MISSING), "speed": (NUMBER, _MISSING)})
        return _sent(await _run(c.move_relative, n, b["distance"], b["speed"]))

    @routes.post(API + "/axes/{axis}/move-velocity")
    async def move_velocity(r: web.Request) -> web.Response:
        n = _axis(r)
        b = await _body(r, {"speed": (NUMBER, _MISSING),
                            "acceleration": (NUMBER, _MISSING),
                            "deceleration": (NUMBER, _MISSING)})
        return _sent(await _run(c.move_velocity, n, b["speed"],
                                b["acceleration"], b["deceleration"]))

    @routes.post(API + "/axes/{axis}/stop")
    async def stop(r: web.Request) -> web.Response:
        n = _axis(r)
        await _body(r, {})
        return _sent(await _run(c.stop, n))

    @routes.post(API + "/axes/{axis}/set-position")
    async def set_position(r: web.Request) -> web.Response:
        n = _axis(r)
        b = await _body(r, {"position": (NUMBER, _MISSING)})
        return _sent(await _run(c.set_position, n, b["position"]))

    @routes.post(API + "/axes/{axis}/unblock")
    async def unblock(r: web.Request) -> web.Response:
        n = _axis(r)
        await _body(r, {})
        return _sent(await _run(c.unblock, n))

    # --- общие команды ------------------------------------------------ #
    @routes.post(f"{API}/stop-all")
    async def stop_all(r: web.Request) -> web.Response:
        b = await _body(r, {"power_off": (BOOL, False)})
        return _sent(await _run(c.stop_all, b["power_off"]))

    @routes.post(f"{API}/io/y0/pulse")
    async def y0_pulse(r: web.Request) -> web.Response:
        await _body(r, {})
        return _sent(await _run(c.y0_pulse))

    @routes.put(f"{API}/io/y0")
    async def y0_set(r: web.Request) -> web.Response:
        b = await _body(r, {"high": (BOOL, _MISSING)})
        return _sent(await _run(c.y0_set, b["high"]))

    # --- WebSocket ---------------------------------------------------- #
    @routes.get(f"{API}/ws")
    async def ws_handler(r: web.Request) -> web.WebSocketResponse:
        client = _Client(asyncio.Queue(maxsize=_WS_QUEUE_SIZE))
        _configure_client(client, r.query.get("events"),
                          r.query.get("telemetry_interval_ms"))
        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(r)
        hub: _Hub = state["hub"]
        hub.add(client)
        await ws.send_str(_dumps({
            "type": "hello", "version": __version__, "status": c.status(),
            "events": sorted(client.events), "telemetry": c.telemetry(),
        }))
        sender = asyncio.create_task(_pump(ws, client))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    reply = _ws_request(client, msg.data)
                    if reply is not None:
                        await ws.send_str(_dumps(reply))
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            hub.remove(client)
            sender.cancel()
        return ws

    app.add_routes(routes)
    return app


def _configure_client(client: _Client, events: Optional[str],
                      interval_ms: Optional[str]) -> None:
    if events:
        wanted = {e.strip() for e in events.split(",") if e.strip()}
        unknown = wanted - EVENT_TYPES
        if unknown:
            raise ApiError(f"неизвестные события: {', '.join(sorted(unknown))}")
        client.events = wanted
    if interval_ms:
        try:
            client.telemetry_interval_s = max(0.0, float(interval_ms) / 1000.0)
        except ValueError:
            raise ApiError("telemetry_interval_ms должно быть числом") from None


def _ws_request(client: _Client, text: str) -> Optional[dict]:
    """Сообщения клиента: ping и смена подписки. Команды — только через REST."""
    try:
        msg = json.loads(text)
        kind = msg["type"]
    except (ValueError, TypeError, KeyError):
        return {"type": "error", "message": "ожидается JSON-объект с полем type"}
    if kind == "ping":
        return {"type": "pong"}
    if kind == "subscribe":
        try:
            _configure_client(client, ",".join(msg.get("events", [])) or None,
                              msg.get("telemetry_interval_ms") and str(msg["telemetry_interval_ms"]))
        except ApiError as exc:
            return {"type": "error", "message": exc.message}
        return {"type": "subscribed", "events": sorted(client.events),
                "telemetry_interval_ms": round(client.telemetry_interval_s * 1000)}
    return {"type": "error", "message": f"неизвестный тип сообщения: {kind!r} "
                                        f"(команды отправляются через REST)"}


async def _pump(ws: web.WebSocketResponse, client: _Client) -> None:
    while not ws.closed:
        event = await client.queue.get()
        try:
            await ws.send_str(_dumps(event))
        except ConnectionResetError:
            return
