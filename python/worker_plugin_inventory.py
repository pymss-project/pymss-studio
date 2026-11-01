"""Application-level plugin intent, independent of runtime-owned files."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any

from worker_runtime_lock import runtime_lock

_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)*\Z")
_BINDING = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_STATES = {"enabled", "disabled", "removed"}
_LIMIT = 4 * 1024 * 1024
_HELD_LOCK: ContextVar[Path | None] = ContextVar("plugin_inventory_lock", default=None)


def valid_binding(value: str) -> bool:
    return isinstance(value, str) and bool(_BINDING.fullmatch(value)) and ".." not in value


@contextmanager
def _transaction(root: Path):
    path = state_path(root)
    if _HELD_LOCK.get() == path:
        yield
        return
    with runtime_lock(root.parent / ".inventory.lock", timeout=30):
        token = _HELD_LOCK.set(path)
        try:
            yield
        finally:
            _HELD_LOCK.reset(token)


@contextmanager
def mutation(root: Path, name: str, revision: int):
    """Exclude newer intent commits while publishing or removing local files."""
    with _transaction(root):
        if _load(root)["plugins"].get(name, {}).get("revision", -1) > revision:
            raise ValueError("A newer plugin action superseded this request; refresh its status")
        yield


def state_path(root: Path) -> Path:
    return root.parent / "inventory.json"


def _validate_entry(name: str, entry: Any) -> None:
    if not _NAME.fullmatch(name) or not isinstance(entry, dict) or entry.get("state") not in _STATES:
        raise ValueError("Invalid application plugin intent")
    if type(entry.get("revision")) is not int or entry["revision"] < 0:
        raise ValueError("Invalid plugin intent revision")
    binding = entry.get("binding")
    if binding is not None and not valid_binding(binding):
        raise ValueError("Invalid plugin runtime binding")
    if entry.get("version") is not None and not isinstance(entry["version"], str):
        raise ValueError("Invalid preferred plugin version")
    if entry.get("content") is not None and (not isinstance(entry["content"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["content"])):
        raise ValueError("Invalid preferred plugin content identity")
    if not isinstance(entry.get("definition", {}), dict):
        raise ValueError("Invalid plugin presentation record")
    if type(entry.get("sourceSelectionRequired", False)) is not bool:
        raise ValueError("Invalid plugin source selection preference")
    pending = entry.get("pending")
    if pending is not None and (not isinstance(pending, dict) or pending.get("action") not in {"disable", "uninstall"}
                                or not valid_binding(pending.get("binding"))):
        raise ValueError("Invalid pending plugin action")


def _load(root: Path) -> dict[str, Any]:
    path = state_path(root)
    if not path.exists():
        return {"schemaVersion": 1, "revision": 0, "defaultPolicy": None, "plugins": {}}
    if path.is_symlink() or path.stat().st_size > _LIMIT:
        raise ValueError("Application plugin inventory is unavailable or too large")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1:
        raise ValueError("Invalid application plugin inventory format")
    if type(data.get("revision")) is not int or data["revision"] < 0 or data.get("defaultPolicy") not in {None, "auto", "review"}:
        raise ValueError("Invalid application plugin inventory revision or defaults policy")
    if not isinstance(data.get("plugins"), dict):
        raise ValueError("Invalid application plugin inventory records")
    for name, entry in data["plugins"].items():
        _validate_entry(name, entry)
    return data


def _write(root: Path, data: dict[str, Any]) -> None:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    for name, entry in data["plugins"].items():
        _validate_entry(name, entry)
    content = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(content) > _LIMIT:
        raise ValueError("Application plugin inventory exceeds the size limit")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".inventory-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + 2
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def merge(root: Path, observations: list[dict[str, Any]], removed: set[str]) -> dict[str, Any]:
    """Read/merge legacy facts and committed binding markers without writing."""
    data = deepcopy(_load(root))
    records = data["plugins"]
    local_names = {item["name"] for item in observations if item["binding"] == root.name}
    ordered = sorted(observations, key=lambda item: (item["binding"] != root.name, item["binding"], item["name"]))
    for item in ordered:
        name = item["name"]
        if not _NAME.fullmatch(name):
            continue
        if name not in records:
            records[name] = {
                "state": "removed" if name in removed and name not in local_names else "enabled" if item["enabled"] else "disabled",
                "revision": 0, "binding": item["binding"], "version": item.get("version"),
                "content": item.get("content"),
                "definition": item.get("definition", {}),
            }
        marker = item.get("marker", {})
        revision = marker.get("revision") if isinstance(marker, dict) else None
        state = marker.get("state") if isinstance(marker, dict) else None
        if type(revision) is int and revision > records[name]["revision"] and state == ("enabled" if item["enabled"] else "disabled"):
            records[name] = {
                "state": state, "revision": revision, "binding": item["binding"],
                "version": item.get("version"), "definition": item.get("definition", {}),
                "content": item.get("content"),
            }
            data["revision"] = max(data["revision"], revision)
    for name in removed:
        if not _NAME.fullmatch(name):
            raise ValueError("Invalid legacy plugin removal preference")
        if name not in records:
            records[name] = {"state": "removed", "revision": 0, "binding": None, "version": None, "definition": {}}
        elif records[name]["revision"] == 0 and name not in local_names:
            records[name]["state"] = "removed"
    return data


def reserve(root: Path, observations: list[dict[str, Any]], removed: set[str]) -> int:
    """Reserve an ordering token; it does not make unverified code available."""
    with _transaction(root):
        data = merge(root, observations, removed)
        # Legacy facts remain local until an explicit action chooses this plugin.
        data["plugins"] = {name: entry for name, entry in data["plugins"].items() if entry["revision"] > 0}
        data["revision"] += 1
        _write(root, data)
        return data["revision"]


def commit(root: Path, name: str, state: str, revision: int, *, definition: dict[str, Any], version: str | None,
           binding: str | None = None, pending: str | None = None, content: str | None = None,
           selection_required: bool = False) -> None:
    entry: dict[str, Any] = {
        "state": state, "revision": revision, "binding": binding or root.name,
        "version": version, "definition": definition,
        "content": content,
        "sourceSelectionRequired": selection_required,
    }
    if pending:
        entry["pending"] = {"action": pending, "binding": root.name}
    _validate_entry(name, entry)
    with _transaction(root):
        data = _load(root)
        if data["plugins"].get(name, {}).get("revision", -1) > revision:
            return
        data["revision"] = max(data["revision"], revision)
        data["plugins"][name] = entry
        _write(root, data)


def complete(root: Path, name: str, revision: int) -> None:
    with _transaction(root):
        data = _load(root)
        entry = data["plugins"].get(name)
        if entry is not None and entry["revision"] == revision:
            entry.pop("pending", None)
            _write(root, data)


def set_default_policy(root: Path, policy: str, observations: list[dict[str, Any]], removed: set[str]) -> None:
    if policy not in {"auto", "review"}:
        raise ValueError("Invalid default plugin policy")
    with _transaction(root):
        data = merge(root, observations, removed)
        data["plugins"] = {name: entry for name, entry in data["plugins"].items() if entry["revision"] > 0}
        data["defaultPolicy"] = policy
        _write(root, data)
