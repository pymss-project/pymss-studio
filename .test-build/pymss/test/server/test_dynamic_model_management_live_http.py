from __future__ import annotations

import base64

import numpy as np

from pymss.server import ServerConfig, create_app


def test_unloaded_server_endpoints_over_live_http(live_client_factory):
    with live_client_factory(create_app(ServerConfig())) as response_client:
        health = response_client.get("/health")
        assert health.status_code == 200
        assert health.json() == {
            "status": "ok",
            "model_loaded": False,
            "model_loading": False,
            "model": None,
            "device": None,
        }

        models = response_client.get("/v1/models")
        assert models.status_code == 200
        assert models.json() == {"object": "list", "data": []}

        missing = response_client.get("/v1/models/model-a.ckpt")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "model_not_found"

        separation = response_client.post(
            "/v1/audio/separations",
            content=b"abc",
            headers={"Content-Type": "application/octet-stream"},
        )
        assert separation.status_code == 503
        assert separation.json()["error"]["code"] == "model_not_loaded"


def test_load_and_separate_fake_model_over_live_http(fake_loader, live_client_factory):
    fake_loader()

    with live_client_factory(create_app(ServerConfig())) as response_client:
        load_response = response_client.post("/v1/models/load", json_body={"model": "alias-a"})
        assert load_response.status_code == 200
        assert load_response.json()["model"]["id"] == "model-a.ckpt"

        health = response_client.get("/health").json()
        assert health["model_loaded"] is True
        assert health["model"] == "model-a.ckpt"

        model = response_client.get("/v1/models/model-a.ckpt")
        assert model.status_code == 200
        assert model.json()["pymss"]["sample_rate"] == 44100

        raw = np.zeros((4, 2), dtype="<f4").tobytes()
        separation = response_client.post(
            "/v1/audio/separations",
            json_body={
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
        assert separation.status_code == 200
        body = separation.json()
        assert body["model"] == "model-a.ckpt"
        assert body["metadata"]["output_stems"] == ["vocals"]
        assert body["outputs"][0]["audio"]["sample_rate"] == 44100
