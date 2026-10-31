from __future__ import annotations

from pymss.server import ServerConfig, create_app


def test_catalog_download_and_source_over_live_http(
    catalog_entry_factory,
    install_model_catalog,
    install_fake_model_download,
    tmp_path,
    live_client_factory,
):
    entry = catalog_entry_factory("model-a.ckpt")
    lookup = install_model_catalog([entry])
    calls = install_fake_model_download(lookup, tmp_path)

    with live_client_factory(create_app(ServerConfig(model_dir=str(tmp_path)))) as response_client:
        catalog = response_client.get("/v1/catalog/models")
        assert catalog.status_code == 200
        assert [item["id"] for item in catalog.json()["data"]] == ["model-a.ckpt"]

        source = response_client.post("/v1/download-source", json_body={"source": "huggingface", "endpoint": None})
        assert source.status_code == 200
        assert source.json()["source"] == "huggingface"

        download = response_client.post("/v1/models/download", json_body={"model": "model-a"})
        assert download.status_code == 200
        assert download.json()["downloaded"] == ["vocal/test/model-a.ckpt", "vocal/test/model-a.yaml"]
        assert download.json()["model"]["pymss"]["local"]["complete"] is True

    assert calls[-1]["source"] == "huggingface"
