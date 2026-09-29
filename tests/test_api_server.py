"""REST + WebSocket поверх эмулятора (aiohttp TestServer, без сети наружу)."""
from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from plc_api.controller import ControllerConfig, PlcController
from plc_api.server import create_app
from plc_api.sim import SimulatedPlc
from plc_panel.config import AXIS_SAFETY, GridLimits

TOKEN = "s3cret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _controller(plc: SimulatedPlc) -> PlcController:
    return PlcController(plc, ControllerConfig(
        poll_interval_s=0.02, reconnect_interval_s=0.05, command_timeout_s=3.0,
        arrival_tolerance_mm=0.05, active_axes=(1, 2, 3),
        limits=GridLimits(x_min=-10, x_max=500, y_min=-10, y_max=500,
                          z_min=-10, z_max=200)))


def run(test, token=None):
    """Поднять приложение на TestServer и выполнить async-тест."""
    async def main():
        plc = SimulatedPlc()
        app = create_app(_controller(plc), token=token)
        async with TestClient(TestServer(app)) as client:
            # дождаться первого кадра телеметрии
            for _ in range(200):
                r = await client.get("/api/v1/telemetry", headers=AUTH)
                if r.status == 200:
                    break
                await asyncio.sleep(0.02)
            await test(client, plc)
    asyncio.run(main())


async def _ok(resp, status=200):
    body = await resp.json()
    assert resp.status == status, body
    return body


def test_health_and_capabilities():
    async def t(client, _plc):
        h = await _ok(await client.get("/api/v1/health"))
        assert h["connected"] is True
        caps = await _ok(await client.get("/api/v1/capabilities"))
        assert caps["axes"]["X"]["pulse_on_arrival"] is True
        assert caps["axes"]["Z"]["pulse_on_arrival"] is False
        assert caps["axes"]["Y"]["coils"]["move_absolute"] == 23
        assert caps["soft_limits"]["Z"] == [-10, 200]
    run(t)


def test_auth_required_when_token_set():
    async def t(client, _plc):
        r = await client.get("/api/v1/health")
        assert r.status == 401
        assert (await r.json())["error"]["code"] == "unauthorized"
        r = await client.get("/api/v1/health",
                             headers={"Authorization": "Bearer wrong"})
        assert r.status == 401
        await _ok(await client.get("/api/v1/health", headers=AUTH))
    run(t, token=TOKEN)


def test_move_absolute_with_wait():
    async def t(client, plc):
        await _ok(await client.post("/api/v1/axes/x/power", json={"on": True}), 202)
        body = await _ok(await client.post(
            "/api/v1/axes/x/move-absolute",
            json={"position": 42.5, "speed": 1000, "wait": True, "timeout_s": 5}))
        assert body["status"] == "arrived"
        assert body["state"]["position"] == pytest.approx(42.5, abs=0.05)
        assert plc.position(1) == pytest.approx(42.5, abs=0.05)
    run(t)


def test_stop_aborts_waiting_move():
    async def t(client, _plc):
        await _ok(await client.post("/api/v1/axes/x/power", json={"on": True}), 202)
        waiting = asyncio.ensure_future(client.post(
            "/api/v1/axes/x/move-absolute",
            json={"position": 400, "speed": 10, "wait": True, "timeout_s": 60}))
        await asyncio.sleep(0.3)
        await _ok(await client.post("/api/v1/axes/x/stop"), 202)
        r = await asyncio.wait_for(waiting, timeout=3)
        assert r.status == 409
        assert (await r.json())["error"]["code"] == "aborted"
    run(t)


def test_command_without_wait_is_accepted():
    async def t(client, _plc):
        body = await _ok(await client.post(
            "/api/v1/axes/2/move-absolute", json={"position": 1, "speed": 1}), 202)
        assert body == {"command": "move_absolute", "axis": "Y", "status": "sent",
                        "params": {"position": 1, "speed": 1, "pulse": False}}
    run(t)


@pytest.mark.parametrize("path,payload,status,code", [
    ("/api/v1/axes/x/move-absolute", {"position": 1}, 400, "bad_request"),
    ("/api/v1/axes/x/move-absolute", {"position": "1", "speed": 1}, 400, "bad_request"),
    ("/api/v1/axes/x/move-absolute", {"postion": 1, "speed": 1}, 400, "bad_request"),
    ("/api/v1/axes/x/power", {"on": 1}, 400, "bad_request"),
    ("/api/v1/axes/w/stop", {}, 404, "not_found"),
    ("/api/v1/axes/y/move-absolute", {"position": 1, "speed": 1, "pulse": True},
     422, "unsupported_by_ladder"),
    ("/api/v1/axes/z/move-absolute", {"position": 999, "speed": 1}, 422, "out_of_limits"),
    ("/api/v1/nope", {}, 404, "not_found"),
])
def test_errors_are_json(path, payload, status, code):
    async def t(client, _plc):
        r = await client.post(path, json=payload)
        assert r.status == status
        assert (await r.json())["error"]["code"] == code
    run(t)


def test_invalid_json_body():
    async def t(client, _plc):
        r = await client.post("/api/v1/stop-all", data="{oops",
                              headers={"Content-Type": "application/json"})
        assert r.status == 400
    run(t)


def test_blocked_axis_conflict_and_unblock():
    async def t(client, plc):
        plc.set_input(AXIS_SAFETY[2].limit_left, True)
        for _ in range(100):
            snap = await _ok(await client.get("/api/v1/axes/y"))
            if snap["blocked"]:
                break
            await asyncio.sleep(0.02)
        r = await client.post("/api/v1/axes/y/move-velocity",
                              json={"speed": 5, "acceleration": 1, "deceleration": 1})
        assert r.status == 409
        await _ok(await client.post("/api/v1/axes/y/unblock"), 202)
    run(t)


def test_y0_and_stop_all():
    async def t(client, plc):
        await _ok(await client.put("/api/v1/io/y0", json={"high": True}), 202)
        assert plc.y0_level is True
        await _ok(await client.put("/api/v1/io/y0", json={"high": False}), 202)
        assert plc.y0_level is False
        await _ok(await client.post("/api/v1/io/y0/pulse"), 202)
        body = await _ok(await client.post("/api/v1/stop-all", json={"power_off": True}), 202)
        assert body["params"] == {"power_off": True}
    run(t)


def test_websocket_streams_telemetry_and_events():
    async def t(client, _plc):
        async with client.ws_connect("/api/v1/ws") as ws:
            hello = await ws.receive_json(timeout=2)
            assert hello["type"] == "hello" and hello["status"]["connected"]
            tel = await ws.receive_json(timeout=2)
            assert tel["type"] == "telemetry" and "X" in tel["data"]["axes"]

            await ws.send_json({"type": "subscribe", "events": ["command", "arrived"]})
            seen = {}
            await _ok(await client.post("/api/v1/axes/x/power", json={"on": True}), 202)
            await _ok(await client.post("/api/v1/axes/x/move-absolute",
                                        json={"position": 7, "speed": 1000}), 202)
            for _ in range(50):
                msg = await ws.receive_json(timeout=2)
                seen.setdefault(msg["type"], msg)
                if "arrived" in seen:
                    break
            assert seen["subscribed"]["events"] == ["arrived", "command"]
            assert seen["command"]["command"] in ("power", "move_absolute")
            assert seen["arrived"]["axis"] == "X"

            await ws.send_json({"type": "ping"})
            assert (await ws.receive_json(timeout=2)) == {"type": "pong"}
            await ws.send_str("not json")
            assert (await ws.receive_json(timeout=2))["type"] == "error"
    run(t)


def test_websocket_token_via_query_and_event_filter():
    async def t(client, _plc):
        async with client.ws_connect(
                f"/api/v1/ws?token={TOKEN}&events=connection") as ws:
            hello = await ws.receive_json(timeout=2)
            assert hello["events"] == ["connection"]
            with pytest.raises(asyncio.TimeoutError):
                await ws.receive_json(timeout=0.3)   # телеметрия отфильтрована
        r = await client.get("/api/v1/ws")
        assert r.status == 401
    run(t, token=TOKEN)


def test_websocket_telemetry_throttle():
    async def t(client, _plc):
        async with client.ws_connect("/api/v1/ws?events=telemetry"
                                     "&telemetry_interval_ms=200") as ws:
            await ws.receive_json(timeout=2)          # hello
            loop = asyncio.get_running_loop()
            start, count = loop.time(), 0
            while loop.time() - start < 0.65:
                try:
                    await ws.receive_json(timeout=0.7)
                    count += 1
                except asyncio.TimeoutError:
                    break
            assert 2 <= count <= 5      # при опросе 20 мс было бы ~30
    run(t)


def test_plc_offline_returns_503():
    async def main():
        plc = SimulatedPlc()
        plc.connect_failures = 10**6
        app = create_app(_controller(plc))
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/api/v1/axes/x/stop")
            assert r.status == 503
            assert (await r.json())["error"]["code"] == "plc_not_connected"
            h = await (await client.get("/api/v1/health")).json()
            assert h["connected"] is False
    asyncio.run(main())
