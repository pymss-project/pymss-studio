"""Fetch configured plugin packages and generate Studio's offline resources."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from worker_plugin_config import load_plugin_config, repository_url  # noqa: E402

IGNORED_DIRS = {".git", ".studio-deps", "__pycache__", "node_modules", ".venv", ".pytest_cache"}
IGNORED_FILES = {".pymss-install.json", ".DS_Store"}
TEXT_SUFFIXES = {".py", ".toml", ".html", ".css", ".js", ".json", ".txt", ".vue", ".ts", ".svg", ".md"}


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def config_digest(config: dict) -> str:
    return digest(json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def local_path(root: Path, value: str) -> Path:
    if not isinstance(value, str) or "\\" in value or not value or ":" in value:
        raise ValueError("Invalid plugin resource path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Plugin resource paths must stay inside their package")
    path = root.joinpath(*relative.parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Plugin resource escapes its package: {value}")
    current = path
    while current != root.parent:
        if current.is_symlink():
            raise ValueError(f"Plugin resource cannot be a symbolic link: {value}")
        current = current.parent
    return path


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    if result.returncode:
        raise RuntimeError(f"Plugin source acquisition failed: {result.stderr.strip()[-2000:]}")
    return result.stdout.strip()


def source_identity(source: Path, config: dict, local: bool) -> dict:
    commit = git("rev-parse", "HEAD", cwd=source)
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ValueError("Plugin repository did not resolve to a commit")
    return {"repository": repository_url(config["repository"]["url"]), "ref": config["repository"]["ref"], "commit": commit, "local": local}


def collect_packages(source: Path, config: dict) -> tuple[dict[Path, bytes], list[dict]]:
    registry = json.loads((source / "registry.json").read_text(encoding="utf-8"))
    if not isinstance(registry, dict):
        raise ValueError("Plugin registry must be an object")
    files, rows, plugin_ids = {}, [], set()
    for name in config["preinstalled"]:
        entry = registry.get(name)
        if not isinstance(entry, dict):
            raise ValueError(f"Preinstalled plugin is missing from the configured registry: {name}")
        if repository_url(str(entry.get("url", ""))).casefold() != repository_url(config["repository"]["url"]).casefold():
            raise ValueError(f"Preinstalled plugin must come from the configured repository: {name}")
        package = local_path(source, entry.get("subpath", ""))
        if not package.is_dir():
            raise ValueError(f"Preinstalled plugin package is missing: {name}")
        project = tomllib.loads((package / "pyproject.toml").read_text(encoding="utf-8"))
        tool = project.get("tool", {})
        pymss = tool.get("pymss", {}) if isinstance(tool, dict) else None
        protocol = pymss.get("plugin") if isinstance(pymss, dict) else None
        metadata = project.get("project")
        if not isinstance(protocol, dict) or not isinstance(metadata, dict) or protocol.get("api_version") != 2:
            raise ValueError(f"Invalid plugin protocol/project metadata (api_version 2 required): {name}")
        plugin_id = protocol.get("id")
        version = metadata.get("version")
        if not isinstance(plugin_id, str) or not plugin_id.strip() or not isinstance(version, str) or not version.strip():
            raise ValueError(f"Plugin must declare its protocol identity and version: {name}")
        if plugin_id != name:
            raise ValueError(f"Registry entry {name} must select the package with that plugin id, not {plugin_id}")
        if plugin_id in plugin_ids:
            raise ValueError(f"Duplicate preinstalled plugin protocol identity: {plugin_id}")
        plugin_ids.add(plugin_id)
        studio = pymss.get("studio", {})
        if not isinstance(studio, dict) or any(key in studio and not isinstance(studio[key], str) for key in ("category", "icon")):
            raise ValueError(f"Invalid plugin Studio metadata: {name}")
        views = studio.get("views", [])
        if not isinstance(views, list) or not all(isinstance(view, dict) for view in views):
            raise ValueError(f"Invalid plugin Studio views: {name}")
        for view in views:
            page = local_path(package, view.get("entry", ""))
            if not page.is_file() or not page.stat().st_size:
                raise ValueError(f"Plugin UI is not built: {name}/{view.get('entry', '')}. Build and publish the plugin repository first.")
        hashes = {}
        for directory, subdirs, filenames in os.walk(package, followlinks=False):
            directory = Path(directory)
            subdirs[:] = sorted(child for child in subdirs if child not in IGNORED_DIRS)
            if any((directory / child).is_symlink() for child in subdirs):
                raise ValueError(f"Symbolic links are not supported in plugin packages: {name}")
            for filename in sorted(filenames):
                path = directory / filename
                if filename in IGNORED_FILES or filename == ".env" or filename.startswith(".env.") or path.suffix in {".pyc", ".pyo"}:
                    continue
                if path.is_symlink() or not path.is_file():
                    raise ValueError(f"Invalid plugin package file: {name}/{filename}")
                content = path.read_bytes()
                if path.suffix in TEXT_SUFFIXES or filename.startswith(("LICENSE", "NOTICE")):
                    content = content.replace(b"\r\n", b"\n")
                relative = path.relative_to(package).as_posix()
                files[Path(name) / relative] = content
                hashes[relative] = digest(content)
        rows.append({
            "name": name, "version": version, "category": studio.get("category"), "icon": studio.get("icon"),
            "url": repository_url(config["repository"]["url"]), "subpath": entry["subpath"], "files": hashes,
        })
    return files, rows


def verify_generated(output: Path, config: dict, *, release: bool = False) -> bool:
    try:
        if output.is_symlink():
            return False
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("schemaVersion") != 1 or manifest.get("configDigest") != config_digest(config):
            return False
        source = manifest.get("source", {})
        if source.get("repository") != repository_url(config["repository"]["url"]) or source.get("ref") != config["repository"]["ref"]:
            return False
        if config["preinstalled"]:
            if not re.fullmatch(r"[0-9a-f]{40,64}", str(source.get("commit", ""))):
                return False
        elif source.get("commit") is not None or source.get("local") is not False:
            return False
        if release and source.get("local") is not False:
            return False
        if config["preinstalled"] and not source.get("local") and re.fullmatch(r"[0-9a-f]{40,64}", source["ref"]) and source["commit"] != source["ref"]:
            return False
        rows = manifest["plugins"]
        if [row["name"] for row in rows] != config["preinstalled"]:
            return False
        expected_files = {"manifest.json"}
        for row in rows:
            for relative, expected in row["files"].items():
                path = local_path(output / row["name"], relative)
                if not path.is_file() or digest(path.read_bytes()) != expected:
                    return False
                expected_files.add(f"{row['name']}/{relative}")
        nodes = list(output.rglob("*"))
        if any(path.is_symlink() for path in nodes):
            return False
        actual_files = {path.relative_to(output).as_posix() for path in nodes if path.is_file()}
        return actual_files == expected_files
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def rename_directory(source: Path, target: Path) -> None:
    deadline = time.monotonic() + 3
    while True:
        try:
            source.rename(target)
            return
        except OSError as error:
            if getattr(error, "winerror", None) not in {5, 32} or time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def publish(output: Path, files: dict[Path, bytes]) -> None:
    if output.is_symlink() or (output.exists() and not (output / "manifest.json").is_file()):
        raise ValueError("Refusing to replace an unmanaged plugin resource directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Keep staging outside the live source tree: Windows dev-server watchers can
    # hold new directories open and prevent both publication and rollback.
    staging_root = output.parent.parent / ".tmp/plugin-resources"
    staging_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(dir=staging_root, prefix="build-"))
    staged, backup = temporary / "next", temporary / "previous"
    preserve_backup = False
    try:
        staged.mkdir()
        for relative, content in files.items():
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        if output.exists():
            rename_directory(output, backup)
        try:
            rename_directory(staged, output)
        except BaseException:
            if backup.exists():
                try:
                    rename_directory(backup, output)
                except BaseException as restore_error:
                    preserve_backup = True
                    raise RuntimeError(f"Plugin resource rollback failed; previous resources remain at {backup}") from restore_error
            raise
    finally:
        if not preserve_backup:
            shutil.rmtree(temporary, ignore_errors=True)


def prepare(studio: Path, *, source: Path | None = None, release: bool = False, refresh: bool = False, check: bool = False) -> dict:
    config = load_plugin_config(studio / "python/plugin-manifest.json")
    output = studio / "python/preinstalled_plugins"
    if check:
        if not verify_generated(output, config, release=release):
            raise ValueError("Prepared plugins are missing, stale or damaged; run pnpm plugins:prepare")
        return json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    if release and source is not None:
        raise ValueError("Release preparation must fetch the configured repository, without a local source override")
    if source is None and not refresh and verify_generated(output, config, release=release):
        # Reuse the verified snapshot of this ref. --refresh resolves a moving
        # branch again; clean CI checkouts always fetch their own snapshot.
        return json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    if not config["preinstalled"]:
        manifest = {"schemaVersion": 1, "configDigest": config_digest(config),
                    "source": {"repository": repository_url(config["repository"]["url"]),
                               "ref": config["repository"]["ref"], "commit": None, "local": False},
                    "plugins": []}
        publish(output, {Path("manifest.json"): (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")})
        return manifest
    cache = studio / ".cache/preinstalled-plugins"
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache, prefix="source-") as temporary:
        if source is None:
            source = Path(temporary)
            git("init", "--quiet", cwd=source)
            git("fetch", "--quiet", "--depth=1", repository_url(config["repository"]["url"]), config["repository"]["ref"], cwd=source)
            git("checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=source)
            local = False
        else:
            source = source.resolve(strict=True)
            local = True
        identity = source_identity(source, config, local)
        files, rows = collect_packages(source, config)
        manifest = {"schemaVersion": 1, "configDigest": config_digest(config), "source": identity, "plugins": rows}
        files[Path("manifest.json")] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        publish(output, files)
        return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Use an already built local plugin checkout for development")
    parser.add_argument("--release", action="store_true", help="Fetch and verify the configured release source")
    parser.add_argument("--refresh", action="store_true", help="Fetch the source again instead of reusing prepared resources")
    parser.add_argument("--check", action="store_true", help="Validate prepared resources without network access")
    args = parser.parse_args()
    source = args.source
    if source is None and not args.release:
        local_source = os.environ.get("PYMSS_STUDIO_PLUGINS_SOURCE")
        if local_source:
            source = Path(local_source)
            if not source.is_absolute():
                source = ROOT / source
    manifest = prepare(ROOT, source=source, release=args.release, refresh=args.refresh, check=args.check)
    if manifest["plugins"]:
        print(f"Prepared {len(manifest['plugins'])} plugins from {manifest['source']['commit']}" + (" (local checkout)" if manifest["source"]["local"] else ""))
    else:
        print("No preinstalled plugins configured; no repository fetch required")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
