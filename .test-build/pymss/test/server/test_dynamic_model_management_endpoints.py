from __future__ import annotations

import base64

import numpy as np

from pymss.server import ServerConfig, create_app


def test_unloaded_server_health_and_models(asgi_client_factory):
    response_client = asgi_client_factory(create_app(ServerConfig()))

    health = response_client.get("/health").json()
    assert health == {
        "status": "ok",
        "model_loaded": False,
        "model_loading": False,
        "model": None,
        "device": None,
    }
    assert response_client.get("/v1/models").json() == {"object": "list", "data": []}


def test_unloaded_separation_returns_model_not_loaded(asgi_client_factory):
    response = asgi_client_factory(create_app(ServerConfig())).post(
        "/v1/audio/separations",
        content=b"abc",
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_not_loaded"


def test_streamed_request_without_content_length_is_limited(asgi_client_factory, fake_loader):
    fake_loader()
    response_client = asgi_client_factory(create_app(ServerConfig()))
    response_client.post("/v1/models/load", json={"model": "model-a"})
    response_client.app.state.pymss_state.config.max_request_bytes = 8

    response = response_client.post(
        "/v1/audio/separations?model=model-a.ckpt&format=pcm_f32le&sample_rate=44100&channels=1",
        body_chunks=[b"1234", b"56789"],
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_load_model_uses_catalog_name(asgi_client_factory, fake_loader):
    loaded = fake_loader()
    response_client = asgi_client_factory(create_app(ServerConfig()))

    response = response_client.post("/v1/models/load", json={"model": "alias-a"})

    assert response.status_code == 200
    body = response.json()
    assert body["previous_model_loaded"] is False
    assert body["model"]["id"] == "model-a.ckpt"
    assert response_client.get("/health").json()["model"] == "model-a.ckpt"
    models = response_client.get("/v1/models").json()["data"]
    assert [model["id"] for model in models] == ["model-a.ckpt"]
    assert len(loaded) == 1


def test_switch_closes_old_model_and_failure_leaves_unloaded(asgi_client_factory, fake_loader):
    loaded = fake_loader(fail_model="missing")
    response_client = asgi_client_factory(create_app(ServerConfig()))

    assert response_client.post("/v1/models/load", json={"model": "model-a"}).status_code == 200
    first = loaded[0]
    assert response_client.post("/v1/models/load", json={"model": "model-b"}).status_code == 200
    assert first.closed is True
    assert response_client.get("/health").json()["model"] == "model-b.ckpt"

    failed = response_client.post("/v1/models/load", json={"model": "missing"})
    assert failed.status_code == 404
    assert loaded[-1].closed is True
    assert response_client.get("/health").json()["model_loaded"] is False
    assert response_client.get("/v1/models").json()["data"] == []


def test_separation_requires_canonical_model_id(asgi_client_factory, fake_loader):
    fake_loader()
    response_client = asgi_client_factory(create_app(ServerConfig()))
    response_client.post("/v1/models/load", json={"model": "alias-a"})

    raw = np.zeros((4, 2), dtype="<f4").tobytes()
    wrong = response_client.post(
        "/v1/audio/separations",
        json={
            "model": "alias-a",
            "input": {
                "format": "pcm_f32le",
                "sample_rate": 44100,
                "channels": 2,
                "data": base64.b64encode(raw).decode("ascii"),
            },
        },
    )
    assert wrong.status_code == 404
    assert wrong.json()["error"]["code"] == "model_not_found"

    ok = response_client.post(
        "/v1/audio/separations",
        json={
            "model": "model-a.ckpt",
            "input": {
                "format": "pcm_f32le",
                "sample_rate": 44100,
                "channels": 2,
                "data": base64.b64encode(raw).decode("ascii"),
            },
            "stems": ["vocals"],
        },
    )
    assert ok.status_code == 200
    assert ok.json()["model"] == "model-a.ckpt"


def test_loading_state_returns_conflict_for_separation(asgi_client_factory):
    app = create_app(ServerConfig())
    app.state.pymss_state.model_loading = True
    response = asgi_client_factory(app).post(
        "/v1/audio/separations",
        content=b"abc",
        headers={"Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "model_operation_in_progress"


def test_load_rejects_unsupported_inference_parameter(asgi_client_factory, fake_loader):
    loaded = fake_loader()
    response = asgi_client_factory(create_app(ServerConfig())).post(
        "/v1/models/load",
        json={"model": "model-a", "inference_params": {"window_size": 512}},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_inference_parameter"
    assert loaded == []


def test_load_rejects_invalid_inference_parameter_value(asgi_client_factory, fake_loader):
    loaded = fake_loader()
    response = asgi_client_factory(create_app(ServerConfig())).post(
        "/v1/models/load",
        json={"model": "model-a", "inference_params": {"batch_size": "abc"}},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_inference_parameter"
    assert loaded == []


