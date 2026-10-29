"""The shared plugin source and preinstallation configuration."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import quote


PLUGIN_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}\Z")
REPOSITORY = re.compile(r"https://github\.com/([A-Za-z0-9_-][A-Za-z0-9_.-]*)/([A-Za-z0-9_-][A-Za-z0-9_.-]*?)(?:\.git)?\Z")


def repository_url(value: str) -> str:
    match = REPOSITORY.fullmatch(value)
    if not match:
        raise ValueError("Plugin repository must be an HTTPS GitHub repository URL")
    return f"https://github.com/{match[1]}/{match[2]}"


def _validate_ref(ref: object) -> None:
    if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./-]*", ref) or ".." in ref or "//" in ref or ref.endswith(("/", ".")):
        raise ValueError("Invalid plugin repository ref")


def load_plugin_config(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1:
        raise ValueError("Invalid plugin configuration schema")
    repository = data.get("repository")
    if not isinstance(repository, dict) or not isinstance(repository.get("url"), str):
        raise ValueError("Invalid plugin repository configuration")
    repository_url(repository["url"])
    for key in ("ref", "registryRef"):
        _validate_ref(repository.get(key, repository.get("ref")))
    names = data.get("preinstalled")
    if not isinstance(names, list) or not all(isinstance(name, str) and PLUGIN_NAME.fullmatch(name) for name in names) or len(set(names)) != len(names):
        raise ValueError("Preinstalled plugins must be a list of unique plugin ids")
    return data


def plugin_registry_url(config: dict) -> str:
    repository = config["repository"]
    base = repository_url(repository["url"]).replace("https://github.com/", "https://raw.githubusercontent.com/", 1)
    return f"{base}/{quote(repository.get('registryRef', repository['ref']), safe='/')}/registry.json"


def plugin_source(config_path: Path, draft: object | None = None) -> dict:
    config = load_plugin_config(config_path)
    repository = config["repository"]
    value = draft
    if value is None:
        raw = os.environ.get("PYMSS_STUDIO_PLUGIN_SOURCE_OVERRIDE")
        value = json.loads(raw) if raw else None
    if value is None:
        local = os.environ.get("PYMSS_STUDIO_PLUGINS_SOURCE")
        if local:
            value = {"mode": "local", "localPath": local}
        else:
            value = {"mode": "repository", "repositoryUrl": repository["url"], "ref": repository["ref"], "registryRef": repository.get("registryRef", repository["ref"])}
    if not isinstance(value, dict) or value.get("mode") not in {"local", "repository"}:
        raise ValueError("Invalid plugin source override")
    if value["mode"] == "local":
        path = Path(str(value.get("localPath") or "")).expanduser()
        if not path.is_absolute():
            raise ValueError("Plugin localPath must be absolute")
        return {"mode": "local", "localPath": str(path.resolve())}
    url, ref, registry_ref = value.get("repositoryUrl"), value.get("ref"), value.get("registryRef")
    repository_url(str(url))
    for candidate in (ref, registry_ref):
        _validate_ref(candidate)
    return {"mode": "repository", "repositoryUrl": repository_url(url), "ref": ref, "registryRef": registry_ref}
