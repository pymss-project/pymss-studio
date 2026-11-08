"""Immutable binary locks shared by CI and the online installer.

Resolver reports are inputs to the release builder, never installation locks.
Clients consume only wheels with exact locations, byte sizes and SHA-256 hashes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from pip._vendor.packaging.tags import compatible_tags, cpython_tags
from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename
from pip._vendor.packaging.version import Version

SCHEMA_VERSION = 1
_MAX_LOCK_BYTES = 4 * 1024 * 1024
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_TARGET = re.compile(r"gfx[0-9a-f]{3,5}\Z")
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9.-]{0,95}\Z")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode("utf-8")).hexdigest()


def artifact_lock_id(lock: dict[str, Any]) -> str:
    return _digest({key: value for key, value in lock.items() if key != "lockId"})


def artifact_lock_path(profile_id: str) -> Path | None:
    if not isinstance(profile_id, str) or not _IDENTIFIER.fullmatch(profile_id):
        raise ValueError("Invalid runtime profile identifier")
    path = Path(__file__).resolve().parent / "runtime-locks" / f"{profile_id}.json"
    return path if path.is_file() else None


def _text(value: Any, label: str, maximum: int = 2048) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(c in value for c in "\r\n\0"):
        raise ValueError(f"Invalid artifact lock {label}")
    return value


def _names(values: Any, label: str, *, empty: bool = False) -> list[str]:
    if not isinstance(values, list) or (not values and not empty) or len(values) > 2048:
        raise ValueError(f"Invalid artifact lock {label}")
    if any(not isinstance(name, str) or canonicalize_name(name) != name or not _IDENTIFIER.fullmatch(name)
           for name in values) or values != sorted(set(values)):
        raise ValueError(f"Artifact lock {label} must contain sorted unique distribution names")
    return values


def _url(value: Any, label: str, allow_file_urls: bool) -> str:
    value = _text(value, label)
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"Artifact lock {label} must not contain credentials, query or fragment")
    if parsed.scheme == "https" and parsed.hostname and parsed.port in (None, 443):
        return value
    if allow_file_urls and parsed.scheme == "file" and not parsed.netloc:
        return value
    raise ValueError(f"Artifact lock {label} requires an immutable HTTPS source")


def _identity(value: Any, label: str, allow_file_urls: bool) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid artifact lock {label}")
    _url(value.get("url"), label + " URL", allow_file_urls)
    if not isinstance(value.get("sha256"), str) or not _SHA256.fullmatch(value["sha256"]):
        raise ValueError(f"Invalid artifact lock {label} SHA-256")
    if type(value.get("bytes")) is not int or not 0 < value["bytes"] < 32 * 1024**3:
        raise ValueError(f"Invalid artifact lock {label} byte size")


def validate_artifact_lock(lock: Any, *, allow_file_urls: bool = False) -> dict[str, Any]:
    if not isinstance(lock, dict) or type(lock.get("schemaVersion")) is not int or lock["schemaVersion"] != SCHEMA_VERSION:
        raise ValueError("Unsupported runtime artifact lock schema")
    if lock.get("backend") != "rocm":
        raise ValueError("Runtime artifact lock is not a ROCm lock")
    if not _IDENTIFIER.fullmatch(_text(lock.get("profileId"), "profile identifier")):
        raise ValueError("Invalid runtime artifact lock profile identifier")
    if type(lock.get("profileRevision")) is not int or lock["profileRevision"] < 1:
        raise ValueError("Invalid artifact lock profile revision")
    for key in ("platform", "machine", "python", "pythonAbi", "platformTag", "channel"):
        _text(lock.get(key), key, 128)
    if lock["platform"] != "win32" or lock["machine"] not in ("amd64", "x86_64"):
        raise ValueError("The artifact lock targets a different operating system or CPU")
    if not re.fullmatch(r"3\.\d{1,2}", lock["python"]) or lock["pythonAbi"] != "cp" + lock["python"].replace(".", ""):
        raise ValueError("The artifact lock has inconsistent Python ABI metadata")
    if lock["platformTag"] != "win_amd64" or lock["channel"] not in ("legacy", "preview", "stable"):
        raise ValueError("Invalid artifact lock platform or distribution channel")
    targets = lock.get("targets")
    if not isinstance(targets, list) or any(not isinstance(t, str) or not _TARGET.fullmatch(t) for t in targets) or targets != sorted(set(targets)):
        raise ValueError("Artifact lock targets must be explicit GPU architectures")
    host = _names(lock.get("hostArtifacts"), "host closure")
    closures = lock.get("targetArtifacts")
    if not isinstance(closures, dict) or sorted(closures) != targets:
        raise ValueError("Artifact lock target closures do not match its coverage")
    for target, names in closures.items():
        _names(names, f"{target} closure", empty=True)
        if set(names) & set(host):
            raise ValueError("An artifact cannot contain both host and device roles")
    artifacts = lock.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts or len(artifacts) > 2048:
        raise ValueError("Invalid artifact lock artifact collection")
    by_name = {}
    for artifact in artifacts:
        _validate_wheel(artifact, lock, host, closures, allow_file_urls)
        name = artifact["name"]
        if name in by_name:
            raise ValueError("Duplicate artifact distribution")
        by_name[name] = artifact
    if set(host) | {name for names in closures.values() for name in names} != set(by_name):
        raise ValueError("Artifact lock contains incomplete or unreferenced artifacts")
    for target in [None, *targets]:
        names = set(host) | set(closures.get(target, []))
        for name in names:
            if not set(by_name[name]["dependencies"]) <= names:
                raise ValueError(f"Artifact lock dependency closure is incomplete: {name}")
    if lock.get("lockId") != artifact_lock_id(lock):
        raise ValueError("Runtime artifact lock content hash does not match lockId")
    return copy.deepcopy(lock)


def _validate_wheel(artifact: Any, lock: dict[str, Any], host: list[str], closures: dict[str, list[str]],
                    allow_file_urls: bool) -> None:
    _identity(artifact, "wheel", allow_file_urls)
    name = _text(artifact.get("name"), "distribution name", 128)
    if canonicalize_name(name) != name:
        raise ValueError("Noncanonical artifact distribution")
    version = _text(artifact.get("version"), "distribution version", 128)
    Version(version)
    filename = _text(artifact.get("filename"), "wheel filename", 255)
    if Path(filename).name != filename or "\\" in filename or "/" in filename:
        raise ValueError("Artifact filename must be a basename")
    wheel_name, wheel_version, _, tags = parse_wheel_filename(filename)
    if wheel_name != name or wheel_version != Version(version):
        raise ValueError("Artifact identity does not match the wheel filename")
    if unquote(urlsplit(artifact["url"]).path.rsplit("/", 1)[-1]) != filename:
        raise ValueError("Artifact download URL does not match its wheel filename")
    if artifact.get("tags") != sorted(str(tag) for tag in tags):
        raise ValueError("Artifact wheel tag metadata does not match its filename")
    python_version = tuple(int(value) for value in lock["python"].split("."))
    supported = {*cpython_tags(python_version, abis=[lock["pythonAbi"]], platforms=[lock["platformTag"]]),
                 *compatible_tags(python_version, interpreter=lock["pythonAbi"], platforms=[lock["platformTag"]])}
    if not tags & supported:
        raise ValueError("Artifact wheel is not compatible with the locked Python ABI")
    role = artifact.get("role")
    expected_targets = sorted(target for target in lock["targets"] if name in closures[target])
    if role == "host":
        if name not in host or expected_targets or artifact.get("targets") != []:
            raise ValueError("Artifact host role conflicts with its closure")
    elif role == "device":
        if name in host or not expected_targets or artifact.get("targets") != expected_targets:
            raise ValueError("Artifact device role conflicts with its closure")
    else:
        raise ValueError("Unknown artifact dependency role")
    _names(artifact.get("dependencies"), "dependency closure", empty=True)
    provenance = artifact.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("kind") not in ("upstream-wheel", "derived-wheel"):
        raise ValueError("Artifact wheel provenance is required")
    _identity(provenance.get("source"), "source", allow_file_urls)
    if provenance["kind"] == "upstream-wheel":
        if provenance["source"]["sha256"] != artifact["sha256"] or provenance["source"]["bytes"] != artifact["bytes"]:
            raise ValueError("Upstream wheel source differs from released wheel")
    else:
        _text(provenance.get("builder"), "source wheel builder", 512)
        inputs = provenance.get("buildInputs")
        if not isinstance(inputs, list) or not inputs or len(inputs) > 256:
            raise ValueError("A derived wheel requires its exact binary build inputs")
        for item in inputs:
            _identity(item, "build input", allow_file_urls)
            _text(item.get("name"), "build input name", 128)
            Version(_text(item.get("version"), "build input version", 128))
            if not str(item.get("filename", "")).endswith(".whl"):
                raise ValueError("Build inputs must be locked binary wheels")


def load_artifact_lock(path: Path | str, *, allow_file_urls: bool = False) -> dict[str, Any]:
    path = Path(path)
    if path.stat().st_size > _MAX_LOCK_BYTES:
        raise ValueError("Runtime artifact lock exceeds its size limit")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Runtime artifact lock contains duplicate JSON fields")
            result[key] = value
        return result
    return validate_artifact_lock(json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object), allow_file_urls=allow_file_urls)


def resolve_artifact_plan(
    lock: dict[str, Any], profile_id: str, targets: Iterable[str], *,
    profile_revision: int | None = None, python_abi: str | None = None,
    platform_tag: str | None = None,
) -> dict[str, Any]:
    lock = validate_artifact_lock(lock)
    if lock["profileId"] != profile_id or (profile_revision is not None and lock["profileRevision"] != profile_revision):
        raise ValueError("Runtime artifact lock does not match the selected profile revision")
    if python_abi is not None and lock["pythonAbi"] != python_abi:
        raise ValueError("Runtime artifact lock does not match the selected Python ABI")
    if platform_tag is not None and lock["platformTag"] != platform_tag:
        raise ValueError("Runtime artifact lock does not match the selected platform")
    if not isinstance(targets, (list, tuple, set)) or any(not isinstance(target, str) for target in targets):
        raise ValueError("Artifact targets must be a list of GPU architectures")
    selected = sorted(set(targets))
    if selected == ["all"]:
        if not lock["targets"]:
            raise ValueError("The legacy artifact lock has no independently selectable GPU targets")
        selected = lock["targets"][:]
    if "all" in selected or any(target not in lock["targets"] for target in selected):
        raise ValueError("The artifact lock does not cover the selected GPU targets")
    names = set(lock["hostArtifacts"]) | {name for target in selected for name in lock["targetArtifacts"][target]}
    artifacts = sorted((a for a in lock["artifacts"] if a["name"] in names), key=lambda a: a["name"])
    host_identities = {a["name"]: {"version": a["version"], "sha256": a["sha256"]}
                       for a in lock["artifacts"] if a["role"] == "host"}
    identities = {a["name"]: {"version": a["version"], "sha256": a["sha256"]} for a in artifacts}
    host_id = _digest({"profileId": lock["profileId"], "profileRevision": lock["profileRevision"],
                       "platform": lock["platform"], "machine": lock["machine"],
                       "pythonAbi": lock["pythonAbi"], "artifacts": host_identities})
    return {"artifactLockId": lock["lockId"], "lockId": lock["lockId"], "hostLockId": host_id,
            "resolvedTargets": selected, "artifacts": artifacts,
            "devicePackageNames": sorted(a["name"] for a in artifacts if a["role"] == "device"),
            "packageRoles": {a["name"]: a["role"] for a in artifacts},
            "resolvedAcceleratorPackages": {a["name"]: a["version"] for a in artifacts},
            "basePackageArtifacts": host_identities, "acceleratorPackageArtifacts": identities}


def build_locked_requirements(plan: dict[str, Any], *, artifacts: list[dict[str, Any]] | None = None,
                              wheelhouse: Path | str | None = None) -> str:
    chosen = plan["artifacts"] if artifacts is None else artifacts
    known = {item["name"]: item for item in plan["artifacts"]}
    lines = []
    for artifact in sorted(chosen, key=lambda item: item["name"]):
        if known.get(artifact["name"]) != artifact:
            raise ValueError("The requested artifact is not part of the locked plan")
        if wheelhouse is None:
            url = artifact["url"]
        else:
            path = Path(wheelhouse).resolve() / artifact["filename"]
            verify_artifact_file(path, artifact)
            url = path.as_uri()
        lines.append(f"{artifact['name']} @ {url} --hash=sha256:{artifact['sha256']}\n")
    return "".join(lines)


def verify_artifact_file(path: Path | str, artifact: dict[str, Any]) -> None:
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size != artifact["bytes"]:
        raise RuntimeError(f"Locked artifact is missing or has an incorrect size: {artifact['filename']}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != artifact["sha256"]:
        raise RuntimeError(f"Locked artifact SHA-256 mismatch: {artifact['filename']}")


def verify_installed_coverage(plan: dict[str, Any], versions: dict[str, str]) -> dict[str, Any]:
    normalized = {canonicalize_name(name): str(version) for name, version in versions.items()}
    missing = [a["name"] for a in plan["artifacts"]
               if a["name"] not in normalized or Version(normalized[a["name"]]) != Version(a["version"])]
    if missing:
        raise RuntimeError("Installed runtime does not match its locked binary closure: " + ", ".join(missing))
    return {"installedTargets": plan["resolvedTargets"][:], "coverageVerified": True,
            "hardwareValidation": "not_run"}
