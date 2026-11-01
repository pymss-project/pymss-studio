"""Advisory v2 operation descriptions without importing plugins or ML backends."""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import sys
import tempfile
from typing import Any

MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_FILES = 5000
_IGNORED_DIRS = {".git", ".studio-deps", "__pycache__", "node_modules", ".venv", "venv", ".tmp", ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_SPEC_FIELDS = {"id", "schemaVersion", "title", "description", "inputs", "params", "outputs"}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def spec_digest(spec: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical({key: value for key, value in spec.items() if key != "specDigest"})).hexdigest()


def with_digest(spec: dict[str, Any]) -> dict[str, Any]:
    return {**spec, "specDigest": spec_digest(spec)}


def _source_files(directory: Path):
    size = count = 0
    if directory.is_symlink():
        raise ValueError("Plugin metadata cannot follow a symbolic link")
    for parent, subdirs, files in os.walk(directory):
        parent = Path(parent)
        subdirs[:] = sorted(name for name in subdirs if name not in _IGNORED_DIRS and (parent != directory or name not in {"ui", "views"}))
        if any((parent / name).is_symlink() for name in subdirs):
            raise ValueError("Plugin metadata cannot follow a symbolic link")
        for name in sorted(files):
            path = parent / name
            if (parent == directory and name == "operations.json") or name in {".pymss-install.json", ".pymss-install-intent.tmp", ".pymss-python.json", ".studio-deps-pending", ".DS_Store", ".env"} or name.startswith(".env.") or path.suffix in {".pyc", ".pyo"}:
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Plugin metadata requires regular source files")
            count += 1
            if count > MAX_SOURCE_FILES or size + info.st_size > MAX_SOURCE_BYTES:
                raise ValueError("Plugin sources exceed metadata limits")
            size += info.st_size
            yield path, info


def source_digest(directory: Path) -> str:
    """Hash backend/package resources; match the plugin build-time exporter."""
    digest = hashlib.sha256()
    size = 0
    for path, _info in _source_files(directory):
        with path.open("rb") as stream:
            content = stream.read(MAX_SOURCE_BYTES - size + 1)
        size += len(content)
        if size > MAX_SOURCE_BYTES:
            raise ValueError("Plugin sources exceed metadata limits")
        if b"\0" not in content:
            content = content.replace(b"\r\n", b"\n")
        digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _source_stamp(directory: Path) -> str:
    return hashlib.sha256(_canonical([
        (path.relative_to(directory).as_posix(), info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        for path, info in _source_files(directory)
    ])).hexdigest()


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate operation metadata field")
        value[key] = item
    return value


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_METADATA_BYTES:
        raise ValueError("Operation metadata is unavailable or too large")
    with path.open("rb") as stream:
        content = stream.read(MAX_METADATA_BYTES + 1)
    if len(content) > MAX_METADATA_BYTES:
        raise ValueError("Operation metadata is too large")
    value = json.loads(content, object_pairs_hook=_object, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("Operation metadata must be an object")
    return value


def _valid_spec(spec: Any, operation: str) -> bool:
    if not isinstance(spec, dict) or set(spec) != _SPEC_FIELDS or spec.get("id") != operation:
        return False
    if type(spec.get("schemaVersion")) is not int or spec["schemaVersion"] < 1:
        return False
    if not isinstance(spec.get("title"), str) or not isinstance(spec.get("description"), str):
        return False
    for field in ("inputs", "outputs"):
        if not isinstance(spec.get(field), dict) or not all(isinstance(key, str) and key and isinstance(port, dict) for key, port in spec[field].items()):
            return False
    params = spec.get("params")
    return isinstance(params, dict) and params.get("type") == "object" and isinstance(params.get("properties", {}), dict)


@dataclass(frozen=True)
class Package:
    directory: Path
    manifest: dict[str, Any]
    stamp: str

    @cached_property
    def digest(self) -> str:
        return source_digest(self.directory)


@dataclass(frozen=True)
class MetadataContext:
    cache_root: Path
    packages: tuple[Package, ...]
    fingerprint: str

    def owner(self, operation: str) -> Package | None:
        candidates = [package for package in self.packages if any(item["id"] == operation for item in package.manifest["operations"])]
        return candidates[0] if len(candidates) == 1 else None


def _profile_fingerprint(packages: tuple[Package, ...]) -> str:
    from worker_optional_packages import _runtime_context

    runtime = _runtime_context()
    paths = set(filter(None, sys.path))
    paths.update(str(package.directory / ".studio-deps") for package in packages)
    if runtime.packages_dir.is_dir():
        paths.update(str(path) for path in runtime.packages_dir.iterdir() if path.is_dir() and not path.name.startswith("."))
    distributions = set()
    for location in sorted(paths):
        directory = Path(location)
        if directory.is_file():
            info = directory.stat()
            distributions.add((os.path.normcase(os.path.abspath(directory)), info.st_size, info.st_mtime_ns))
        elif directory.is_dir():
            for item in sorted(directory.iterdir()):
                if item.name.endswith((".dist-info", ".egg-info")):
                    candidates = [item / name for name in ("METADATA", "PKG-INFO", "RECORD", "direct_url.json")] if item.is_dir() else [item]
                elif item.suffix in {".pth", ".egg-link"}:
                    candidates = [item]
                else:
                    continue
                for candidate in candidates:
                    if candidate.is_file():
                        info = candidate.stat()
                        distributions.add((os.path.normcase(os.path.abspath(candidate)), info.st_size, info.st_mtime_ns))
    return hashlib.sha256(_canonical({
        "python": sys.version, "executable": os.path.normcase(os.path.abspath(sys.executable)),
        "platform": sys.platform, "machine": platform.machine(),
        "backend": runtime.backend, "identity": runtime.identity,
        "sdk": hashlib.sha256(Path(__file__).with_name("plugin-sdk.json").read_bytes()).hexdigest(),
        "packages": [(str(package.directory), package.stamp) for package in packages],
        "distributions": sorted(distributions),
    })).hexdigest()


def capture_context(operation: str | None = None) -> MetadataContext | None:
    """Read installed sources and distribution metadata; never activate code."""
    try:
        import worker_plugins as plugins

        root = plugins._root()
        if root.is_dir():
            plugins._recover_for_read(root)
        intents = plugins._execution_intents(root)
        manual = Path(os.environ.get("PYMSS_PLUGINS_DIR") or Path.home() / ".pymss" / "plugins")
        packages: list[Package] = []
        seen: set[str] = set()
        for source in (root / "installed", manual):
            if not source.is_dir():
                continue
            for directory in sorted(source.iterdir()):
                if source == root / "installed":
                    if intents is None:
                        continue
                    intent = intents.get(directory.name, {})
                    if intent.get("state", "enabled") != "enabled" or not plugins._binding_matches(directory, intent):
                        continue
                identity = os.path.normcase(os.path.abspath(directory))
                if not directory.is_dir() or directory.name.startswith((".", "_")) or identity in seen:
                    continue
                seen.add(identity)
                try:
                    manifest = plugins._read_manifest(directory)
                    ids = [item["id"] for item in manifest["operations"]]
                    if len(ids) != len(set(ids)) or any(not operation.startswith(manifest["id"] + ".") or not plugins._NAME.fullmatch(operation) for operation in ids):
                        continue
                    packages.append(Package(directory, manifest, _source_stamp(directory)))
                except (OSError, ValueError, TypeError, KeyError, RecursionError):
                    continue
        selected = tuple(packages)
        context = MetadataContext(root / "operation-metadata", selected, _profile_fingerprint(selected))
        if operation is not None and (owner := context.owner(operation)) is not None:
            _ = owner.digest
        return context
    except (OSError, ValueError, TypeError, KeyError, ImportError, RecursionError):
        # A missing/invalid cache must preserve the established v2 query path.
        return None


def _header(package: Package) -> dict[str, Any]:
    return {
        "formatVersion": 1, "pluginApiVersion": 2,
        "pluginId": package.manifest["id"], "pluginVersion": package.manifest["version"],
        "sourceDigest": package.digest,
    }


def _matches(value: dict[str, Any], package: Package) -> bool:
    return all(type(value.get(key)) is type(expected) and value[key] == expected for key, expected in _header(package).items())


def _cache_path(context: MetadataContext, operation: str) -> Path:
    return context.cache_root / (hashlib.sha256(operation.encode("utf-8")).hexdigest() + ".json")


def cached_description(context: MetadataContext | None, operation: str) -> dict[str, Any] | None:
    if context is None or (package := context.owner(operation)) is None:
        return None
    if not context.cache_root.is_symlink():
        try:
            value = _read_json(_cache_path(context, operation))
            spec = value.get("operation")
            if _matches(value, package) and value.get("environmentFingerprint") == context.fingerprint and _valid_spec(spec, operation):
                return with_digest(spec)
        except (OSError, ValueError, TypeError, RecursionError):
            pass
    try:
        value = _read_json(package.directory / "operations.json")
        specs = value.get("operations")
        if not _matches(value, package) or not isinstance(specs, list):
            return None
        ids = [item.get("id") for item in specs if isinstance(item, dict)]
        declared = [item["id"] for item in package.manifest["operations"]]
        if len(ids) != len(specs) or len(ids) != len(set(ids)) or set(ids) != set(declared):
            return None
        if not all(_valid_spec(item, item["id"]) for item in specs):
            return None
        return with_digest(next(item for item in specs if item["id"] == operation))
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


def remember_description(context: MetadataContext | None, spec: dict[str, Any]) -> None:
    """Atomically cache one already-described operation, outside execution."""
    operation = spec.get("id")
    if context is None or not isinstance(operation, str) or not _valid_spec(spec, operation):
        return
    current = capture_context()
    if current is None or current.fingerprint != context.fingerprint or current.cache_root.is_symlink():
        return
    package = current.owner(operation)
    original = context.owner(operation)
    if package is None or original is None:
        return
    temporary: str | None = None
    try:
        if package.digest != original.digest:
            return
        content = _canonical({**_header(package), "environmentFingerprint": current.fingerprint, "operation": spec})
        if len(content) > MAX_METADATA_BYTES:
            return
        current.cache_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", dir=current.cache_root, prefix=".operation-", delete=False) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, _cache_path(current, operation))
        temporary = None
    except (OSError, ValueError, TypeError, RecursionError):
        pass
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass
