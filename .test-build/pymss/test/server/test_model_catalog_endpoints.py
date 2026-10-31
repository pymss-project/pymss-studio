from __future__ import annotations

from pymss.server import ServerConfig, create_app


def test_catalog_models_list_filters_local_and_supported(
    asgi_client_factory,
    catalog_entry_factory,
    install_model_catalog,
    write_catalog_entry_files,
    tmp_path,
):
    model_a = catalog_entry_factory("model-a.ckpt")
    model_b = catalog_entry_factory("model-b.ckpt")
    unsupported = catalog_entry_factory("unsupported.ckpt", supported=False)
    install_model_catalog([model_a, model_b, unsupported])
    write_catalog_entry_files(tmp_path, model_a)
    response_client = asgi_client_factory(create_app(ServerConfig(model_dir=str(tmp_path))))

    default = response_client.get("/v1/catalog/models").json()
    assert [item["id"] for item in default["data"]] == ["model-a.ckpt", "model-b.ckpt"]
    assert "model_dir" not in default["pymss"]
    assert all("model_dir" not in item["pymss"]["local"] for item in default["data"])

    all_models = response_client.get("/v1/catalog/models?supported=all").json()
    assert [item["id"] for item in all_models["data"]] == ["model-a.ckpt", "model-b.ckpt", "unsupported.ckpt"]
    assert all_models["data"][2]["pymss"]["unsupported_reason"] == "not supported"

    complete = response_client.get("/v1/catalog/models?local=complete").json()
    assert [item["id"] for item in complete["data"]] == ["model-a.ckpt"]

    missing = response_client.get("/v1/catalog/models?local=missing").json()
    assert [item["id"] for item in missing["data"]] == ["model-b.ckpt"]


def test_catalog_model_detail_resolves_alias_and_omits_local_path(
    asgi_client_factory,
    catalog_entry_factory,
    install_model_catalog,
    write_catalog_entry_files,
    tmp_path,
):
    entry = catalog_entry_factory("model-a.ckpt", aux=("vocal/test/model-a.extra.json",))
    install_model_catalog([entry])
    write_catalog_entry_files(tmp_path, entry)
    response_client = asgi_client_factory(
        create_app(ServerConfig(model_dir=str(tmp_path), endpoint="https://default.example/models"))
    )

    response = response_client.get("/v1/catalog/models/model-a-alias")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "model-a.ckpt"
    assert body["pymss"]["local"]["complete"] is True
    assert "model_dir" not in body["pymss"]["local"]
    assert body["pymss"]["remote"]["endpoint"] == "https://default.example/models"
    files = body["pymss"]["files"]
    assert [item["role"] for item in files] == ["model", "config", "auxiliary"]
    assert "local_path" not in files[0]
    assert "model_dir" not in files[0]
    assert files[0]["remote_url"].startswith("https://default.example/models/")


def test_new_management_endpoints_require_api_key(
    asgi_client_factory,
    catalog_entry_factory,
    install_model_catalog,
    tmp_path,
):
    entry = catalog_entry_factory("model-a.ckpt")
    install_model_catalog([entry])
    response_client = asgi_client_factory(create_app(ServerConfig(model_dir=str(tmp_path), api_key="secret")))

    unauthorized_requests = [
        response_client.get("/v1/catalog/models"),
        response_client.get("/v1/catalog/models/model-a"),
        response_client.post("/v1/models/download", json={"model": "model-a"}),
        response_client.get("/v1/download-source"),
        response_client.post("/v1/download-source", json={"source": "huggingface"}),
    ]
    for response in unauthorized_requests:
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_api_key"

    authorized = response_client.get("/v1/catalog/models", headers={"Authorization": "Bearer secret"})
    assert authorized.status_code == 200
