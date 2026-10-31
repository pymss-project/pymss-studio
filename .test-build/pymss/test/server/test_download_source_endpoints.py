from __future__ import annotations

from pymss.server import ServerConfig, create_app


def test_download_source_get_update_and_validation(asgi_client_factory, tmp_path):
    response_client = asgi_client_factory(
        create_app(ServerConfig(model_dir=str(tmp_path), source="modelscope", endpoint="https://old.example"))
    )

    initial = response_client.get("/v1/download-source").json()
    assert initial["source"] == "modelscope"
    assert initial["endpoint"] == "https://old.example"
    assert "model_dir" not in initial

    updated = response_client.post("/v1/download-source", json={"source": "huggingface"}).json()
    assert updated["source"] == "huggingface"
    assert updated["endpoint"] == "https://old.example"
    assert "model_dir" not in updated

    cleared = response_client.post("/v1/download-source", json={"source": "hf-mirror", "endpoint": None}).json()
    assert cleared["source"] == "hf-mirror"
    assert cleared["endpoint"] is None
    assert "model_dir" not in cleared

    invalid = response_client.post("/v1/download-source", json={"source": "bad-source"})
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_download_source"

    invalid_endpoint = response_client.post("/v1/download-source", json={"source": "huggingface", "endpoint": 123})
    assert invalid_endpoint.status_code == 400
    assert invalid_endpoint.json()["error"]["code"] == "invalid_download_source"


def test_download_source_rejects_changes_during_operations(asgi_client_factory, tmp_path):
    app = create_app(ServerConfig(model_dir=str(tmp_path)))
    response_client = asgi_client_factory(app)

    response_client.loop.run_until_complete(app.state.pymss_state.download_lock.acquire())
    try:
        response = response_client.post("/v1/download-source", json={"source": "huggingface"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "model_download_in_progress"
    finally:
        app.state.pymss_state.download_lock.release()

    response_client.loop.run_until_complete(app.state.pymss_state.model_lock.acquire())
    try:
        response = response_client.post("/v1/download-source", json={"source": "huggingface"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "model_operation_in_progress"
    finally:
        app.state.pymss_state.model_lock.release()
