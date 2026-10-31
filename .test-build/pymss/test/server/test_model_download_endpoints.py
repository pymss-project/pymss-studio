from __future__ import annotations

from pymss.server import ServerConfig, create_app
from pymss.server.state import LoadedModel


def test_download_model_uses_default_source_and_returns_local_status(
    asgi_client_factory,
    catalog_entry_factory,
    install_model_catalog,
    install_fake_model_download,
    tmp_path,
):
    entry = catalog_entry_factory("model-a.ckpt")
    lookup = install_model_catalog([entry])
    calls = install_fake_model_download(lookup, tmp_path)
    response_client = asgi_client_factory(
        create_app(ServerConfig(model_dir=str(tmp_path), source="huggingface", endpoint="https://default.example"))
    )

    response = response_client.post(
        "/v1/models/download",
        json={"model": "model-a-alias", "force": "false", "verify": False, "timeout_seconds": 12},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "model.download"
    assert body["model"]["id"] == "model-a.ckpt"
    assert body["model"]["pymss"]["local"]["complete"] is True
    assert "model_dir" not in body["model"]["pymss"]["local"]
    assert body["source"] == "huggingface"
    assert body["endpoint"] == "https://default.example"
    assert body["downloaded"] == ["vocal/test/model-a.ckpt", "vocal/test/model-a.yaml"]
    assert body["skipped"] == []
    assert calls == [
        {
            "model": "model-a-alias",
            "model_dir": str(tmp_path),
            "source": "huggingface",
            "endpoint": "https://default.example",
            "verify": False,
            "force": False,
            "timeout": 12,
        }
    ]


def test_download_model_skips_complete_local_model_without_force(
    asgi_client_factory,
    catalog_entry_factory,
    install_model_catalog,
    install_fake_model_download,
    write_catalog_entry_files,
    tmp_path,
):
    entry = catalog_entry_factory("model-a.ckpt")
    lookup = install_model_catalog([entry])
    install_fake_model_download(lookup, tmp_path)
    write_catalog_entry_files(tmp_path, entry)
    response_client = asgi_client_factory(create_app(ServerConfig(model_dir=str(tmp_path))))

    response = response_client.post("/v1/models/download", json={"model": "model-a"})

    assert response.status_code == 200
    body = response.json()
    assert body["model"]["pymss"]["local"]["complete"] is True
    assert body["downloaded"] == []
    assert body["skipped"] == ["vocal/test/model-a.ckpt", "vocal/test/model-a.yaml"]


def test_download_model_default_timeout_is_integer(
    asgi_client_factory,
    catalog_entry_factory,
    install_model_catalog,
    install_fake_model_download,
    tmp_path,
):
    entry = catalog_entry_factory("model-a.ckpt")
    lookup = install_model_catalog([entry])
    calls = install_fake_model_download(lookup, tmp_path)
    response_client = asgi_client_factory(create_app(ServerConfig(model_dir=str(tmp_path))))

    response = response_client.post("/v1/models/download", json={"model": "model-a"})

    assert response.status_code == 200
    assert calls[-1]["timeout"] == 30
    assert isinstance(calls[-1]["timeout"], int)


def test_download_result_does_not_expose_paths_outside_model_dir(
    asgi_client_factory,
    monkeypatch,
    catalog_entry_factory,
    tmp_path,
):
    entry = catalog_entry_factory("model-a.ckpt")
    outside_path = tmp_path.parent / "outside.ckpt"

    def fake_download_model(*_args, **_kwargs):
        return {"entry": entry, "downloaded": [outside_path], "skipped": []}

    monkeypatch.setattr("pymss.server.app.download_model", fake_download_model)
    response_client = asgi_client_factory(create_app(ServerConfig(model_dir=str(tmp_path))))

    response = response_client.post("/v1/models/download", json={"model": "model-a"})

    assert response.status_code == 200
    assert response.json()["downloaded"] == ["<path_resolve_error>"]


def test_download_model_errors_and_conflicts(
    asgi_client_factory,
    catalog_entry_factory,
    install_model_catalog,
    install_fake_model_download,
    tmp_path,
):
    entry = catalog_entry_factory("model-a.ckpt")
    lookup = install_model_catalog([entry])
    install_fake_model_download(lookup, tmp_path)
    app = create_app(ServerConfig(model_dir=str(tmp_path)))
    response_client = asgi_client_factory(app)

    missing = response_client.post("/v1/models/download", json={"model": "missing"})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "model_not_found"

    failed = response_client.post("/v1/models/download", json={"model": "broken"})
    assert failed.status_code == 500
    assert failed.json()["error"]["code"] == "model_download_failed"

    response_client.loop.run_until_complete(app.state.pymss_state.download_lock.acquire())
    try:
        conflict = response_client.post("/v1/models/download", json={"model": "model-a"})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "model_download_in_progress"
    finally:
        app.state.pymss_state.download_lock.release()

    response_client.loop.run_until_complete(app.state.pymss_state.model_lock.acquire())
    try:
        conflict = response_client.post("/v1/models/download", json={"model": "model-a"})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "model_operation_in_progress"
    finally:
        app.state.pymss_state.model_lock.release()


def test_load_rejects_while_download_is_in_progress(asgi_client_factory, tmp_path):
    app = create_app(ServerConfig(model_dir=str(tmp_path)))
    response_client = asgi_client_factory(app)

    response_client.loop.run_until_complete(app.state.pymss_state.download_lock.acquire())
    try:
        response = response_client.post("/v1/models/load", json={"model": "model-a"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "model_download_in_progress"
    finally:
        app.state.pymss_state.download_lock.release()


def test_load_uses_hot_switched_download_source(asgi_client_factory, monkeypatch, catalog_entry_factory, tmp_path):
    entry = catalog_entry_factory("model-a.ckpt")
    calls = []

    def fake_load_model(config, model, source=None, endpoint=None, inference_params=None):
        calls.append({"model": model, "source": source, "endpoint": endpoint})
        return LoadedModel(
            separator=object(),
            entry=entry,
            resolved={},
            requested_model=model,
            model_id=entry.name,
            sample_rate=44100,
            instruments=("vocals", "instrument"),
            device="cpu",
            inference_params={},
            supported_parameters={},
        )

    monkeypatch.setattr("pymss.server.app.load_model", fake_load_model)
    response_client = asgi_client_factory(
        create_app(ServerConfig(model_dir=str(tmp_path), source="modelscope", endpoint="https://old.example"))
    )

    response_client.post("/v1/download-source", json={"source": "huggingface", "endpoint": None})
    response = response_client.post("/v1/models/load", json={"model": "model-a"})

    assert response.status_code == 200
    assert calls == [{"model": "model-a", "source": "huggingface", "endpoint": None}]
