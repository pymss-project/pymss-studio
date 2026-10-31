from __future__ import annotations

from pymss.server import ServerConfig, create_app


def _write_webui_static(tmp_path):
    static_dir = tmp_path / "webui_static"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<!doctype html><title>pymss WebUI</title>", encoding="utf-8")
    (assets_dir / "app.js").write_text("console.log('pymss webui')", encoding="utf-8")
    return static_dir


def test_webui_routes_are_disabled_by_default(asgi_client_factory):
    response = asgi_client_factory(create_app(ServerConfig())).get("/ui/")

    assert response.status_code == 404


def test_webui_routes_serve_static_assets(asgi_client_factory, monkeypatch, tmp_path):
    static_dir = _write_webui_static(tmp_path)
    monkeypatch.setattr("pymss.server.webui.WEBUI_STATIC_DIR", static_dir)
    response_client = asgi_client_factory(create_app(ServerConfig(webui=True)))

    index = response_client.get("/ui/")
    redirect = response_client.get("/ui")
    asset = response_client.get("/ui/assets/app.js")
    fallback = response_client.get("/ui/catalog")

    assert index.status_code == 200
    assert b"pymss WebUI" in index.content
    assert redirect.status_code == 307
    assert asset.status_code == 200
    assert b"pymss webui" in asset.content
    assert fallback.status_code == 200
    assert b"pymss WebUI" in fallback.content


def test_webui_routes_do_not_bypass_v1_auth(asgi_client_factory, monkeypatch, tmp_path):
    static_dir = _write_webui_static(tmp_path)
    monkeypatch.setattr("pymss.server.webui.WEBUI_STATIC_DIR", static_dir)
    response_client = asgi_client_factory(create_app(ServerConfig(webui=True, api_key="secret")))

    assert response_client.get("/ui/").status_code == 200

    unauthorized = response_client.get("/v1/server/info")
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "invalid_api_key"

    authorized = response_client.get("/v1/server/info", headers={"Authorization": "Bearer secret"})
    assert authorized.status_code == 200
    body = authorized.json()
    assert body["object"] == "server.info"
    assert body["webui"] == {"enabled": True, "path": "/ui/"}
    assert body["auth"] == {"api_key_required": True}
    assert body["limits"]["max_audio_seconds"] == 600.0
    assert body["download_source"]["source"] == "modelscope"
    assert "model_dir" not in body


def test_serve_parser_accepts_webui_flag():
    from pymss.cli import build_parser

    args = build_parser().parse_args(["serve", "--webui"])

    assert args.webui is True


def test_webui_startup_log_includes_browser_url(monkeypatch):
    calls = []

    from pymss.server.app import _log_webui_url

    monkeypatch.setattr("logging.Logger.info", lambda _logger, *args: calls.append(args))

    _log_webui_url(ServerConfig(webui=True, host="0.0.0.0", port=8010))

    assert calls == [("WebUI available at %s", "http://127.0.0.1:8010/ui/")]


def test_webui_startup_log_is_disabled_without_webui(monkeypatch):
    calls = []

    from pymss.server.app import _log_webui_url

    monkeypatch.setattr("logging.Logger.info", lambda _logger, *args: calls.append(args))

    _log_webui_url(ServerConfig(webui=False, host="127.0.0.1", port=8010))

    assert calls == []


def test_webui_url_formats_ipv6_host():
    from pymss.server.app import _server_url

    assert _server_url(ServerConfig(webui=True, host="::1", port=8010), "/ui/") == "http://[::1]:8010/ui/"


def test_uvicorn_started_message_logs_webui_after_server_url(monkeypatch):
    import uvicorn

    from pymss.server.app import _create_uvicorn_server

    calls = []
    server = _create_uvicorn_server(uvicorn, object(), ServerConfig(webui=True, host="127.0.0.1", port=8010))
    monkeypatch.setattr(type(server).__mro__[1], "_log_started_message", lambda _server, _listeners: calls.append("started"))
    monkeypatch.setattr("pymss.server.app._log_webui_url", lambda config: calls.append(("webui", config.webui)))

    server._log_started_message([])

    assert calls == ["started", ("webui", True)]
