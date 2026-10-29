"""Studio plugin requirements, checked without importing plugins or ML backends."""
from __future__ import annotations

import importlib.metadata
import json
import platform
import re
import sys
from pathlib import Path
from typing import Any

_SDK = json.loads(Path(__file__).with_name("plugin-sdk.json").read_text(encoding="utf-8"))
SDK_VERSION: int = _SDK["version"]
SDK_CAPABILITIES: frozenset[str] = frozenset(_SDK["capabilities"])
_FIELDS = (
    ("required_capabilities", "requiredCapabilities", None),
    ("platforms", "platforms", {"win32", "linux", "darwin"}),
    ("architectures", "architectures", {"x86_64", "arm64"}),
    ("backends", "backends", {"torch", "mlx"}),
)


class PluginCompatibilityError(ValueError):
    code = "PLUGIN_INCOMPATIBLE"


def read_requirements(studio: dict[str, Any]) -> dict[str, Any]:
    sdk_version = studio.get("sdk_version")
    if sdk_version is not None or studio.get("views"):
        if type(sdk_version) is not int or sdk_version < 1:
            raise ValueError("Studio sdk_version must be a positive integer")
    result = {"sdkVersion": sdk_version}
    for field, output, allowed in _FIELDS:
        values = studio.get(field, [])
        if not isinstance(values, list) or not all(
            isinstance(value, str)
            and (value in allowed if allowed else re.fullmatch(r"[a-z][A-Za-z0-9]*(?:\.[a-z][A-Za-z0-9]*)+", value))
            for value in values
        ):
            raise ValueError(f"Invalid Studio {field}")
        if len(values) != len(set(values)):
            raise ValueError(f"Duplicate Studio {field} entries")
        result[output] = values
    return result


def catalog_requirements(value: object) -> dict[str, Any]:
    if value is None:
        return read_requirements({})
    if not isinstance(value, dict):
        raise ValueError("Plugin catalog requirements must be an object")
    return read_requirements({
        "sdk_version": value.get("sdkVersion"),
        **{field: value.get(output, []) for field, output, _allowed in _FIELDS},
    })


def runtime_profile() -> dict[str, Any]:
    machine = platform.machine().lower()
    architecture = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    backends = []
    for name in ("torch", "mlx"):
        try:
            importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        backends.append(name)
    return {"platform": sys.platform, "architecture": architecture, "backends": backends}


def compatibility(requirements: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    reasons = []
    sdk = requirements["sdkVersion"]
    if sdk is not None and sdk != SDK_VERSION:
        reasons.append(f"Requires Studio SDK {sdk}; this Studio provides SDK {SDK_VERSION}.")
    missing = sorted(set(requirements["requiredCapabilities"]) - SDK_CAPABILITIES)
    if missing:
        reasons.append(f"Studio capabilities unavailable: {', '.join(missing)}.")
    for field, runtime_key, label in (
        ("platforms", "platform", "operating system"),
        ("architectures", "architecture", "architecture"),
    ):
        requested = requirements[field]
        if requested and runtime[runtime_key] not in requested:
            reasons.append(f"Requires {label}: {', '.join(requested)}; current: {runtime[runtime_key]}.")
    requested = requirements["backends"]
    if requested and not set(requested).intersection(runtime["backends"]):
        available = ', '.join(runtime["backends"]) or "none"
        reasons.append(f"Requires a runtime with one of these compute backends: {', '.join(requested)}; available: {available}.")
    return {"compatible": not reasons, "reasons": reasons}


def require_compatible(studio: dict[str, Any], runtime: dict[str, Any] | None = None) -> dict[str, Any]:
    requirements = read_requirements(studio)
    status = compatibility(requirements, runtime if runtime is not None else runtime_profile())
    if not status["compatible"]:
        raise PluginCompatibilityError(" ".join(status["reasons"]))
    return requirements
