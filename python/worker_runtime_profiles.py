"""Runtime profiles and immutable accelerator package identities.

This module only reads configuration and package metadata. It never installs packages
or imports Torch, so installation plans also work on machines without an AMD GPU.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.machinery
import json
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version

FINGERPRINT_SCHEMA_VERSION = 2
RELEASE_STATES = {"legacy", "disabled", "preview", "stable", "revoked"}


def profile_release_state(profile: dict[str, Any]) -> str:
    enabled = profile.get("installationEnabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("Runtime installationEnabled must be a boolean")
    state = profile.get("releaseState", "legacy" if enabled else "disabled")
    if not isinstance(state, str) or state not in RELEASE_STATES:
        raise ValueError("Unrecognized runtime release state")
    if enabled != (state in {"legacy", "preview", "stable"}):
        raise ValueError("Runtime release state conflicts with its installation gate")
    return state


def assert_profile_installable(plan: dict[str, Any], *, accept_preview: bool = False) -> None:
    state = profile_release_state(plan)
    if state in {"disabled", "revoked"}:
        raise ValueError("This runtime profile is not available for installation")
    if state == "preview" and accept_preview is not True:
        raise ValueError("Explicit acceptance is required to install a preview runtime")


BASE_ACCELERATOR_PACKAGES = ("torch", "torchaudio", "torchvision", "triton", "mlx", "rocm")
BASE_ACCELERATOR_PREFIXES = (
    "nvidia-", "rocm-", "mlx-", "amd-torch-device-", "amd-torchvision-device-",
)


def backend_spec(manifest: dict[str, Any], backend: str, platform_name: str | None = None) -> dict[str, Any]:
    spec = copy.deepcopy(manifest.get("backends", {}).get(backend) or {})
    platform_name = platform_name or sys.platform
    spec.update(copy.deepcopy(spec.get("platformOverrides", {}).get(platform_name) or {}))
    return spec


def resolve_profile(
    manifest: dict[str, Any], backend: str, profile_id: str | None = None,
    *, platform_name: str | None = None,
) -> dict[str, Any]:
    platform_name = platform_name or sys.platform
    spec = backend_spec(manifest, backend, platform_name)
    if not spec or platform_name not in spec.get("platforms", []):
        raise ValueError(f"Backend {backend} is not supported on {platform_name}")
    profiles = spec.get("profiles") or {}
    selected = profile_id if profile_id is not None else spec.get("defaultProfile")
    if selected is not None:
        if not isinstance(selected, str) or selected not in profiles:
            raise ValueError(f"Unknown runtime profile: {selected}")
        profile = copy.deepcopy(profiles[selected])
        if profile.get("id") != selected or platform_name not in profile.get("platforms", []):
            raise ValueError(f"Runtime profile {selected} is not valid on {platform_name}")
        return profile
    return {
        "id": None, "revision": 1, "platforms": spec["platforms"],
        "python": spec.get("python") or manifest.get("python"),
        "torch": spec.get("torch") or {}, "packages": {},
        "installationEnabled": True, "installMode": "legacy",
    }


def resolve_profile_for_runtime(
    manifest: dict[str, Any], backend: str, torch_version: str,
    *, profile_id: str | None = None, platform_name: str | None = None,
) -> dict[str, Any] | None:
    """Identify the installed build; never assume the default describes old state."""
    platform_name = platform_name or sys.platform
    spec = backend_spec(manifest, backend, platform_name)
    profiles = spec.get("profiles") or {}
    if profile_id is not None:
        candidates = [resolve_profile(manifest, backend, profile_id, platform_name=platform_name)]
    else:
        candidates = list(profiles.values())
    try:
        actual = Version(torch_version)
    except Exception:
        actual = None
    matches = [copy.deepcopy(profile) for profile in candidates if actual is not None
               and platform_name in profile.get("platforms", [])
               and Version(profile["packages"]["torch"]) == actual]
    if len(matches) == 1:
        return matches[0]
    if profile_id is not None:
        raise ValueError(f"Installed Torch does not match runtime profile {profile_id}")
    return None


def is_base_accelerator_package(name: str, additional: Iterable[str] = ()) -> bool:
    name = canonicalize_name(name)
    return (
        name in BASE_ACCELERATOR_PACKAGES
        or name.startswith(BASE_ACCELERATOR_PREFIXES)
        or name in {canonicalize_name(value) for value in additional}
    )


def accelerator_versions(versions: dict[str, Any], additional: Iterable[str] = ()) -> dict[str, str]:
    result = {}
    for name, value in versions.items():
        if value is not None and is_base_accelerator_package(name, additional):
            # Validate before writing a value to a pip constraint file.
            normalized = canonicalize_name(name)
            requirement = Requirement(f"{normalized}=={value}")
            if requirement.url or requirement.marker or requirement.extras:
                raise ValueError(f"Invalid accelerator package identity: {name}")
            result[normalized] = str(Version(str(value)))
    return dict(sorted(result.items()))


def accelerator_constraints(versions: dict[str, str]) -> str:
    fixed = accelerator_versions(versions, versions)
    return "".join(f"{name}=={version}\n" for name, version in fixed.items())


def accelerator_source_records(report: dict[str, Any]) -> dict[str, Any]:
    if report.get("version") != "1" or not isinstance(report.get("install"), list):
        raise RuntimeError("The package installer returned an invalid installation report")
    result = {}
    for item in report["install"]:
        package = item.get("metadata") or {}
        name = canonicalize_name(str(package.get("name") or ""))
        if not is_base_accelerator_package(name):
            continue
        download = item.get("download_info") or {}
        source = urlsplit(str(download.get("url") or ""))
        # Signed queries and URL credentials must not enter persistent runtime state.
        url = urlunsplit((source.scheme, source.hostname or "", source.path, "", ""))
        hashes = (download.get("archive_info") or {}).get("hashes") or {}
        result[name] = {"version": package.get("version"), "url": url, "hashes": hashes}
    return result


def verify_profile_accelerator_sources(plan: dict[str, Any], records: dict[str, Any]) -> None:
    torch = plan["torch"]
    hosts = {urlsplit(value).hostname for value in [
        *(torch.get("rocmRequirements") or []), *(torch.get("requirements") or []), torch.get("indexUrl") or "",
    ] if urlsplit(value).hostname}
    for name, version in plan["packages"].items():
        source = records.get(name) or {}
        url = urlsplit(source.get("url", ""))
        if (source.get("version") != version or url.hostname not in hosts or url.scheme != "https"
                or not re.fullmatch(r"[0-9a-fA-F]{64}", str(source.get("hashes", {}).get("sha256", "")))):
            raise RuntimeError(f"The installed ROCm package has no verified official source and SHA-256: {name}")


def verify_accelerator_versions(expected: dict[str, str], actual: dict[str, str]) -> None:
    expected = accelerator_versions(expected, expected)
    actual = accelerator_versions(actual, expected)
    if actual != expected:
        changed = sorted(name for name in set(expected) | set(actual) if expected.get(name) != actual.get(name))
        raise RuntimeError("Base accelerator packages changed: " + ", ".join(changed))


def assert_no_accelerator_overlay(directory: Path, frozen: Iterable[str] = ()) -> None:
    """Reject sidecars that could shadow a frozen package, even without metadata."""
    frozen = tuple(frozen)
    for distribution in metadata.distributions(path=[str(directory)]):
        name = distribution.metadata.get("Name") or ""
        if is_base_accelerator_package(name, frozen):
            raise RuntimeError(f"Runtime overlay contains a base accelerator package: {name}")
    for path in directory.iterdir():
        module_name = path.name
        for suffix in (*importlib.machinery.EXTENSION_SUFFIXES, ".py", ".pyc"):
            if module_name.endswith(suffix):
                module_name = module_name.removesuffix(suffix)
                break
        if is_base_accelerator_package(module_name, frozen) or module_name.startswith("_rocm_sdk"):
            raise RuntimeError(f"Runtime overlay shadows a base accelerator module: {module_name}")


def runtime_fingerprints(
    profile: dict[str, Any], python_version: str, versions: dict[str, str],
    targets: Iterable[str] = (), *, device_packages: Iterable[str] = (),
    package_artifacts: dict[str, Any] | None = None, host_lock_id: str | None = None,
) -> dict[str, str]:
    """Device package roles must come from the resolved plan, not name heuristics."""
    devices = {canonicalize_name(name) for name in device_packages}
    fixed = accelerator_versions(versions, versions)
    host = {name: version for name, version in fixed.items() if name not in devices}
    device = {name: version for name, version in fixed.items() if name in devices}
    artifacts = package_artifacts or {}
    host_artifacts = {name: identity for name, identity in artifacts.items() if canonicalize_name(name) not in devices}
    device_artifacts = {name: identity for name, identity in artifacts.items() if canonicalize_name(name) in devices}
    def digest(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "baseFingerprint": digest({"profile": profile.get("id"), "revision": profile.get("revision"),
                                   "platforms": profile.get("platforms"), "machine": profile.get("machine"),
                                   "python": ".".join(python_version.split(".")[:2]), "packages": host,
                                   "artifacts": host_artifacts, "hostLockId": host_lock_id,
                                   "schemaVersion": FINGERPRINT_SCHEMA_VERSION}),
        "targetFingerprint": digest({"targets": sorted(set(targets)), "packages": device,
                                     "artifacts": device_artifacts, "schemaVersion": FINGERPRINT_SCHEMA_VERSION}),
    }


def runtime_install_plan(
    manifest: dict[str, Any], backend: str, profile_id: str | None = None,
    targets: list[str] | None = None, *, platform_name: str | None = None,
    machine_name: str | None = None, python_version: str | None = None,
) -> dict[str, Any]:
    profile = resolve_profile(manifest, backend, profile_id, platform_name=platform_name)
    release_state = profile_release_state(profile)
    if profile.get("machine") and machine_name and machine_name.lower() not in profile["machine"]:
        raise ValueError("The runtime profile requires a different CPU architecture")
    if python_version and profile.get("python") and ".".join(python_version.split(".")[:2]) != profile["python"]:
        raise ValueError(f"The runtime profile requires Python {profile['python']}")
    if targets is None:
        targets = []
    if not isinstance(targets, list) or any(not isinstance(target, str) for target in targets):
        raise ValueError("Runtime targets must be a list of GPU architectures")
    targets = sorted(set(targets))
    if any(target not in profile.get("availableTargets", []) for target in targets):
        raise ValueError("The runtime profile does not provide the selected GPU target")
    if "all" in targets and len(targets) != 1:
        raise ValueError("The all target cannot be combined with individual GPU targets")
    torch = copy.deepcopy(profile["torch"])
    if profile.get("installMode") == "multiarch":
        if not targets:
            raise ValueError("Select at least one GPU target for the multi-architecture runtime")
        extras = ",".join(f"device-{target}" for target in targets)
        packages = profile["packages"]
        torch["requirements"] = [
            f"torch[{extras}]=={packages['torch']}",
            f"torchvision[{extras}]=={packages['torchvision']}",
            f"torchaudio=={packages['torchaudio']}", f"rocm=={packages['rocm']}",
        ]
    return {
        "backend": backend, "profileId": profile["id"], "profileRevision": profile["revision"],
        "profileIdentity": {key: profile.get(key) for key in ("id", "revision", "platforms", "machine")},
        "profileSnapshot": copy.deepcopy(profile),
        "python": profile.get("python"), "targets": targets, "requestedTargets": targets,
        "resolvedTargets": [target for target in profile.get("availableTargets", []) if target != "all"] if targets == ["all"] else targets,
        "releaseState": release_state, "requiresPreviewAcceptance": release_state == "preview",
        "fingerprintSchemaVersion": FINGERPRINT_SCHEMA_VERSION,
        "installMode": profile.get("installMode"), "upstreamChannel": profile.get("upstreamChannel"),
        "availableTargets": profile.get("availableTargets", []),
        "validatedTargets": profile.get("validatedTargets", []),
        "installationEnabled": profile.get("installationEnabled", False),
        "validationStatus": profile.get("validationStatus", "legacy"),
        "torch": torch, "packages": profile["packages"],
        "constraints": accelerator_constraints(profile["packages"]),
        # A configuration plan is not a resolved dependency lock with wheel hashes.
        "resolutionStatus": "unresolved",
    }


def runtime_state_from_artifact_plan(
    plan: dict[str, Any], python_version: str, versions: dict[str, str],
) -> dict[str, Any]:
    """One state projection for the online client and the GPU-less release builder."""
    devices = plan.get("devicePackageNames", [])
    fixed = accelerator_versions(versions, devices)
    if plan.get("resolutionStatus") == "locked":
        expected = accelerator_versions(plan["resolvedAcceleratorPackages"], devices)
        verify_accelerator_versions(expected, fixed)
    resolved = plan.get("resolvedTargets", [])
    return {
        "profileId": plan["profileId"], "profileRevision": plan["profileRevision"],
        "profileSnapshot": copy.deepcopy(plan["profileSnapshot"]),
        "releaseState": plan["releaseState"], "requestedTargets": plan["requestedTargets"],
        "resolvedTargets": resolved, "installedTargets": resolved,
        "devicePackageNames": devices, "packageRoles": plan.get("packageRoles", {}),
        "fingerprintSchemaVersion": FINGERPRINT_SCHEMA_VERSION,
        **{key: plan[key] for key in ("artifactLockId", "hostLockId", "basePackageArtifacts", "acceleratorPackageArtifacts") if key in plan},
        **runtime_fingerprints(plan["profileIdentity"], python_version, fixed, resolved, device_packages=devices,
                               package_artifacts=plan.get("acceleratorPackageArtifacts"),
                               host_lock_id=plan.get("hostLockId")),
    }
