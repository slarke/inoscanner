"""OpenAPI-спецификация и страница /docs (Swagger UI + вкладка WebSocket)."""
from __future__ import annotations

import re
from pathlib import Path

from plc_api.controller import PlcController
from plc_api.openapi import build_spec
from plc_api.server import OPENAPI_PATH, STATIC_DIR, create_app
from plc_api.sim import SimulatedPlc
from test_api_server import AUTH, TOKEN, run

_DOC_ROUTES = {"/", "/docs", OPENAPI_PATH}


def _app_operations(app) -> set:
    ops = set()
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource else {}
        path = info.get("path") or info.get("formatter")
        if path is None or route.method == "HEAD" or path in _DOC_ROUTES:
            continue
        ops.add((path, route.method.lower()))
    return ops


def _spec_operations(spec) -> set:
    return {(p, m) for p, item in spec["paths"].items()
            for m in item if m in ("get", "post", "put", "delete", "patch")}


def test_spec_documents_exactly_the_app_routes():
    app = create_app(PlcController(SimulatedPlc()), manage_controller=False)
    assert _spec_operations(build_spec(False)) == _app_operations(app)


def test_spec_refs_resolve_and_operations_have_responses():
    spec = build_spec(False)
    text = repr(spec)
    for name in set(re.findall(r"#/components/schemas/(\w+)", text)):
        assert name in spec["components"]["schemas"], name
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method == "parameters":
                continue
            assert op["responses"], (path, method)
            assert op.get("summary"), (path, method)


def test_security_only_when_token_required():
    assert "security" not in build_spec(False)
    assert build_spec(True)["security"] == [{"bearerAuth": []}]


def test_pulse_limitation_is_documented():
    op = build_spec(False)["paths"]["/api/v1/axes/{axis}/move-absolute"]["post"]
    assert "только для оси X" in op["description"]


def test_docs_assets_are_local_only():
    """Страница должна работать в закрытой сети: никаких внешних URL."""
    for name in ("docs.html", "docs.js", "docs.css", "control.js"):
        text = (STATIC_DIR / name).read_text(encoding="utf-8")
        assert not re.search(r"https?://", text), name
    for asset in ("swagger-ui-bundle.js", "swagger-ui.css", "LICENSE", "NOTICE"):
        assert (STATIC_DIR / "swagger-ui" / asset).is_file(), asset


def test_docs_served_without_token_api_still_protected():
    async def t(client, _plc):
        r = await client.get("/", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == "/docs"

        r = await client.get("/docs")
        assert r.status == 200
        html = await r.text()
        assert "swagger-ui-bundle.js" in html and 'data-tab="ws"' in html
        assert 'data-tab="control"' in html and "control.js" in html

        for asset in ("docs.js", "docs.css", "control.js", "swagger-ui/swagger-ui-bundle.js",
                      "swagger-ui/swagger-ui.css"):
            r = await client.get(f"/docs/static/{asset}")
            assert r.status == 200, asset

        spec = await (await client.get(OPENAPI_PATH)).json()
        assert spec["openapi"].startswith("3.")
        assert spec["security"] == [{"bearerAuth": []}]

        assert (await client.get("/api/v1/health")).status == 401
        assert (await client.get("/api/v1/health", headers=AUTH)).status == 200
        assert (await client.get("/docs/static/../server.py")).status in (403, 404)
    run(t, token=TOKEN)


def test_static_dir_is_inside_package():
    assert STATIC_DIR.parent == Path(__file__).resolve().parents[1] / "python_panel" / "plc_api"
