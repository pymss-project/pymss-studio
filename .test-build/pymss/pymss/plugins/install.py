"""Plugin install / uninstall.

Three install sources:
  pymss install <id>       -> resolve <id> via the plugin registry
  pymss install <url>      -> download the repository source
  pymss install <path>     -> copy a local directory

Every source is staged, validated as a v2 manifest, and published under
``<plugins_dir>/<plugin_id>``.

Version pinning: `pymss install <id>@<ref>` checks out a git ref (tag/branch/commit).
Dependencies: read from the plugin's pyproject.toml [project].dependencies and
installed via uv (preferred for uv-managed venvs) or pip.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .loader import get_plugins_dir
from .manifest import ManifestError, read_manifest
from .sources import (
    SourceError,
    download_github_source,
    github_repository,
    read_github_file,
    resolve_github_commit,
)

logger = logging.getLogger(__name__)

# Hosts pass ``registry_url`` explicitly; CLI users can set PYMSS_PLUGINS_REGISTRY.
DEFAULT_REGISTRY_URL = (
    "https://raw.githubusercontent.com/pymss-project/pymss-plugins/main/registry.json"
)


def _registry_url(explicit: str | None = None) -> str:
    return explicit or os.environ.get("PYMSS_PLUGINS_REGISTRY", DEFAULT_REGISTRY_URL)


class InstallError(Exception):
    """Raised when a plugin install fails."""


@dataclass
class InstallResult:
    name: str
    path: Path
    source: str  # "registry" | "url" | "path"
    version: str | None = None
    dependencies_installed: list[str] = field(default_factory=list)


def _is_url(arg: str) -> bool:
    return arg.startswith("http://") or arg.startswith("https://") or arg.startswith("git@") or arg.startswith("ssh://")


def _git_available() -> bool:
    return shutil.which("git") is not None


def _fetch_registry(registry_url: str | None = None) -> dict[str, str]:
    """Fetch and parse the registry JSON. Raises InstallError on failure."""
    url = _registry_url(registry_url)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pymss"})
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise InstallError(
            f"could not fetch official plugin registry from {url}: {exc}. "
            f"Install by URL or path instead, e.g. `pymss install https://github.com/xxx/pymss-opus`."
        ) from exc
    if not isinstance(data, dict):
        raise InstallError(f"registry at {url} is not a JSON object mapping name -> url")
    return data


def _clone(url: str, dest: Path, subpath: str | None = None, ref: str | None = None) -> dict:
    """Acquire one plugin directory and record the actual source commit.

    Public HTTPS GitHub repositories use commit-pinned archives. Other hosts
    and SSH sources use Git, with the same default-branch or explicit-ref policy.
    """
    try:
        if github_repository(url):
            return download_github_source(url, dest, subpath=subpath, ref=ref)
    except SourceError as exc:
        raise InstallError(str(exc)) from exc
    if not _git_available():
        raise InstallError("git is required for non-GitHub or SSH plugin sources; install git or use a public HTTPS GitHub URL.")
    if dest.exists():
        raise InstallError(f"destination already exists: {dest} (uninstall it first)")
    dest.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="pymss-git-") as temporary:
        repo = Path(temporary) / "repo"
        try:
            def git(*args: str) -> str:
                return subprocess.run(  # noqa: S603, S607
                    ["git", *args], check=True, capture_output=True, text=True, timeout=180,
                ).stdout.strip()

            if ref:
                git("init", str(repo))
                git("-C", str(repo), "remote", "add", "origin", url)
                git("-C", str(repo), "fetch", "--depth", "1", "origin", ref)
                git("-C", str(repo), "checkout", "--detach", "FETCH_HEAD")
            else:
                git("clone", "--depth", "1", url, str(repo))
            commit = git("-C", str(repo), "rev-parse", "HEAD")
        except (subprocess.SubprocessError, OSError) as exc:
            detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
            raise InstallError(f"git source acquisition failed: {detail}") from exc
        selected = (repo / subpath).resolve() if subpath else repo.resolve()
        if not selected.is_relative_to(repo.resolve()) or not selected.is_dir():
            raise InstallError(f"plugin subpath {subpath!r} is not a directory within the source repository")
        shutil.copytree(selected, dest, ignore=shutil.ignore_patterns(".git"))
        return {"url": url, "ref": ref, "resolved_commit": commit}


# ---------------------------------------------------------------------------
# Plugin metadata (read from pyproject.toml) and dependency installation
# ---------------------------------------------------------------------------

def _read_plugin_meta(plugin_dir: Path) -> dict:
    """Read name/version/dependencies from a plugin's pyproject.toml.

    Returns {} if no pyproject.toml is present.
    """
    pp = plugin_dir / "pyproject.toml"
    if not pp.exists():
        return {}
    try:
        text = pp.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InstallError(f"cannot read pyproject.toml: {exc}") from exc
    return _parse_plugin_meta(text)


def _parse_plugin_meta(txt: str) -> dict:
    try:
        data = tomllib.loads(txt)
    except tomllib.TOMLDecodeError as exc:
        raise InstallError(f"invalid pyproject.toml: {exc}") from exc
    project = data.get("project", {})
    if not isinstance(project, dict):
        raise InstallError("project must be a TOML table")
    meta = {key: project[key] for key in ("name", "version") if isinstance(project.get(key), str)}
    if "dependencies" in project:
        dependencies = project["dependencies"]
        if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
            raise InstallError("project.dependencies must be a list of strings")
        # Preserve requirement strings, including markers and extras, for pip/uv.
        meta["dependencies"] = [item for item in dependencies if item.lower() != "pymss"]
    return meta


def _detect_package_manager() -> str | None:
    """Detect how to install packages into the current environment.

    Returns "uv" if this is a uv-managed venv (or uv is on PATH), "pip" if the
    pip module is importable, or None if neither is available.
    """
    # Check pyvenv.cfg for a uv-managed venv first — uv venvs intentionally
    # ship without pip, so we must use uv there.
    try:
        import sysconfig
        cfg = Path(sysconfig.get_config_var("prefix") or "") / "pyvenv.cfg"
        if cfg.exists() and "uv =" in cfg.read_text(encoding="utf-8"):
            if shutil.which("uv"):
                return "uv"
    except Exception:
        pass
    # Fall back to uv if it's on PATH (covers uv-managed setups without cfg).
    if shutil.which("uv"):
        return "uv"
    # Finally, pip module.
    try:
        import importlib.util
        if importlib.util.find_spec("pip") is not None:
            return "pip"
    except Exception:
        pass
    return None


def _install_dependencies(deps: list[str]) -> None:
    """Install Python dependencies into the current environment.

    Prefers uv (for uv-managed venvs), falls back to pip. Raises InstallError
    if neither is available or the install fails.
    """
    if not deps:
        return
    pm = _detect_package_manager()
    if pm is None:
        raise InstallError(
            "cannot install plugin dependencies: neither uv nor pip is available. "
            "Install them manually, e.g. `uv pip install " + " ".join(deps) + "`, "
            "or reinstall with --no-deps."
        )
    if pm == "uv":
        cmd = ["uv", "pip", "install", "--python", sys.executable, *deps]
    else:
        cmd = [sys.executable, "-m", "pip", "install", *deps]
    try:
        subprocess.run(  # noqa: S603
            cmd, check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise InstallError(
            f"dependency install failed: {exc.stderr.strip() or exc}. "
            f"You can skip this with `pymss install ... --no-deps` and install manually: "
            f"`{' '.join(deps)}`"
        ) from exc


def _write_install_manifest(
    plugin_dir: Path, *, name: str, source: str, url: str | None,
    subpath: str | None, ref: str | None, meta: dict, registry_url: str | None,
    resolved_commit: str | None,
) -> None:
    """Write .pymss-install.json so update/list can identify the install."""
    manifest = {
        "name": name,
        "version": meta.get("version"),
        "source": source,
        "url": url,
        "subpath": subpath,
        "ref": ref,
        "resolved_commit": resolved_commit,
        "registry_url": registry_url,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (plugin_dir / ".pymss-install.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def read_install_manifest(plugin_dir: Path) -> dict:
    """Read the .pymss-install.json written at install time. Returns {} if none."""
    mf = plugin_dir / ".pymss-install.json"
    if not mf.exists():
        return {}
    try:
        return json.loads(mf.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _copy_dir(src: Path, dest: Path) -> None:
    if dest.exists():
        raise InstallError(f"destination already exists: {dest} (uninstall it first)")
    shutil.copytree(src, dest)


def install(
    arg: str,
    plugins_dir: Path | None = None,
    subpath: str | None = None,
    ref: str | None = None,
    no_deps: bool = False,
    registry_url: str | None = None,
    *,
    expected_plugin_id: str | None = None,
) -> InstallResult:
    """Install a plugin by registry id, URL, or local path.

    The plugin lands in ``plugins_dir/<plugin_id>`` (the manifest id, not the
    source folder name). subpath overrides the auto-detected subdirectory. For
    URLs, a trailing ``#path/to/subdir`` also selects a subdirectory (takes
    precedence over the subpath argument). ref checks out a git
    tag/branch/commit (e.g. from ``id@0.2.0``). Dependencies from
    pyproject.toml are installed unless no_deps is True. When expected_plugin_id
    is provided, the manifest must match before any dependencies are installed.
    """
    plugins_dir = plugins_dir or get_plugins_dir()
    plugins_dir.mkdir(parents=True, exist_ok=True)

    # `id@ref` syntax applies to registry ids, not SSH credentials.
    if ref is None and not _is_url(arg) and "@" in arg and not arg.startswith(("/", ".", "~")):
        arg, ref = arg.rsplit("@", 1)

    with tempfile.TemporaryDirectory(prefix=".staging-", dir=plugins_dir) as temporary:
        staged = Path(temporary) / "plugin"
        resolved_registry_url = None
        source_meta = {}
        local = Path(arg).expanduser()
        if _is_url(arg):
            # A trailing #path selects a subdirectory of the repo.
            if "#" in arg:
                url_part, frag = arg.rsplit("#", 1)
                subpath = frag or subpath
                arg = url_part
            # Ref suffixes belong to the repository path, never to user@host.
            parsed = urllib.parse.urlsplit(arg) if "://" in arg else None
            if parsed is None:
                remote, _, path = arg.partition(":")
            else:
                path = parsed.path
            if "@" in path:
                path, url_ref = path.rsplit("@", 1)
                if not url_ref:
                    raise InstallError("repository ref must not be empty")
                if ref is None:
                    ref = url_ref
                arg = urllib.parse.urlunsplit(parsed._replace(path=path)) if parsed else f"{remote}:{path}"
            source_meta = _clone(arg, staged, subpath=subpath or None, ref=ref)
            source, url = "url", arg
        elif local.exists() and local.is_dir():
            if subpath:
                local = local / subpath
                if not local.is_dir():
                    raise InstallError(
                        f"plugin subpath {subpath!r} not found under {arg} (checked {local})"
                    )
            _copy_dir(local, staged)
            source, url = "path", str(Path(arg).resolve())
        else:
            # Otherwise treat arg as an id in the plugin registry.
            resolved_registry_url = _registry_url(registry_url)
            registry = _fetch_registry(resolved_registry_url)
            if arg not in registry:
                available = ", ".join(sorted(key for key in registry if not key.startswith("_"))) or "(empty)"
                raise InstallError(
                    f"'{arg}' is not in the plugin registry. "
                    f"Available: {available}. Or install by URL/path."
                )
            entry = registry[arg]
            # Registry entries may be either a plain URL string, or an object
            # {"url": ..., "subpath": ...} for monorepo plugin collections.
            if isinstance(entry, dict):
                url = entry.get("url", "")
                subpath = entry.get("subpath") or subpath
                if not url:
                    raise InstallError(f"registry entry for '{arg}' has no 'url' field")
            else:
                url = str(entry)
            source_meta = _clone(url, staged, subpath=subpath, ref=ref)
            source = "registry"

        try:
            manifest = read_manifest(staged)
        except ManifestError as exc:
            raise InstallError(f"not a pymss v2 plugin: {exc}") from exc
        name = manifest.plugin_id
        if source == "registry" and name != arg:
            raise InstallError(f"registry entry '{arg}' installed plugin id '{name}'")
        if expected_plugin_id is not None and name != expected_plugin_id:
            raise InstallError(f"update for '{expected_plugin_id}' contains plugin '{name}'")
        dest = plugins_dir / name
        if dest.exists():
            raise InstallError(f"destination already exists: {dest} (uninstall it first)")
        meta = _read_plugin_meta(staged)
        installed_deps: list[str] = []
        if not no_deps:
            _install_dependencies(meta.get("dependencies", []))
            installed_deps = meta.get("dependencies", [])
        _write_install_manifest(staged, name=name, source=source, url=url,
                                subpath=subpath, ref=ref, meta=meta, registry_url=resolved_registry_url,
                                resolved_commit=source_meta.get("resolved_commit"))
        staged.rename(dest)
        return InstallResult(name=name, path=dest, source=source,
                             version=meta.get("version"), dependencies_installed=installed_deps)


def uninstall(name: str, plugins_dir: Path | None = None) -> Path:
    """Remove an installed plugin directory. Returns the removed path."""
    plugins_dir = plugins_dir or get_plugins_dir()
    dest = plugins_dir / name
    if not dest.exists():
        raise InstallError(f"no installed plugin named '{name}' in {plugins_dir}")
    if not dest.is_dir():
        raise InstallError(f"'{name}' is not a plugin directory: {dest}")
    shutil.rmtree(dest)
    return dest


# ---------------------------------------------------------------------------
# Browse / search the official registry
# ---------------------------------------------------------------------------

def _registry_entries(registry_url: str | None = None) -> list[dict]:
    """Return registry entries as normalized dicts (drop _comment etc.)."""
    raw = _fetch_registry(registry_url)
    out = []
    for name, entry in raw.items():
        if name.startswith("_"):
            continue
        if isinstance(entry, dict):
            e = {"name": name, **entry}
        else:
            e = {"name": name, "url": str(entry)}
        out.append(e)
    return out


def list_available(registry_url: str | None = None, plugins_dir: Path | None = None) -> list[dict]:
    """List all plugins in the registry with their metadata.

    Each entry: {name, url, subpath?, description?, tags?, installed?}; ``name``
    is the plugin id. `installed` is True if that plugin id exists locally.
    """
    plugins_dir = plugins_dir or get_plugins_dir()
    entries = _registry_entries(registry_url)
    for e in entries:
        e["installed"] = (plugins_dir / e["name"]).exists()
    return entries


def search_available(query: str, registry_url: str | None = None) -> list[dict]:
    """Search the registry by name/description/tags (case-insensitive substring)."""
    q = query.lower()
    results = []
    for e in list_available(registry_url):
        haystack = " ".join([
            e.get("name", ""),
            e.get("description", ""),
            " ".join(e.get("tags", []) or []),
        ]).lower()
        if q in haystack:
            results.append(e)
    return results


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------

def _fetch_remote_meta(url: str, subpath: str | None, ref: str | None) -> dict:
    """Read remote plugin metadata and the commit it belongs to."""
    try:
        if github_repository(url):
            commit = resolve_github_commit(url, ref)
            path = f"{subpath.rstrip('/')}/pyproject.toml" if subpath else "pyproject.toml"
            content = read_github_file(url, path, commit=commit)
            return {**_parse_plugin_meta(content.decode("utf-8")), "resolved_commit": commit}
    except (SourceError, UnicodeDecodeError) as exc:
        raise InstallError(str(exc)) from exc
    if not _is_url(url):
        local = Path(url)
        return _read_plugin_meta(local / subpath if subpath else local)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_repo = Path(tmp) / "repo"
        source_meta = _clone(url, tmp_repo, subpath=subpath, ref=ref)
        return {**_read_plugin_meta(tmp_repo), **source_meta}


def list_installed(plugins_dir: Path | None = None) -> list[dict]:
    """List installed plugins with their install manifest (version/source/url)."""
    plugins_dir = plugins_dir or get_plugins_dir()
    out = []
    if not plugins_dir.exists():
        return out
    for d in sorted(plugins_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        manifest = read_install_manifest(d)
        out.append({
            "name": d.name,
            "version": manifest.get("version"),
            "source": manifest.get("source", "unknown"),
            "url": manifest.get("url"),
            "subpath": manifest.get("subpath"),
            "ref": manifest.get("ref"),
            "resolved_commit": manifest.get("resolved_commit"),
            "registry_url": manifest.get("registry_url"),
            "installed_at": manifest.get("installed_at"),
        })
    return out


def check_update(name: str, plugins_dir: Path | None = None) -> dict:
    """Check if an installed plugin has a newer version available.

    Returns versions, commits, content_changed, and up_to_date. A changed
    commit is an update even when the plugin version is unchanged.
    Works for both registry and url/path-installed plugins: reads the local
    .pymss-install.json for the source url/subpath, fetches remote meta.
    """
    plugins_dir = plugins_dir or get_plugins_dir()
    dest = plugins_dir / name
    if not dest.exists():
        raise InstallError(f"no installed plugin named '{name}' in {plugins_dir}")
    manifest = read_install_manifest(dest)
    url = manifest.get("url")
    subpath = manifest.get("subpath")
    local_version = manifest.get("version")
    local_commit = manifest.get("resolved_commit")
    if not url:
        raise InstallError(
            f"plugin '{name}' has no url in its install manifest; cannot check for updates"
        )
    # Fetch remote meta from the default branch (no ref) so we detect new
    # releases even when the plugin was installed at a pinned ref.
    remote_meta = _fetch_remote_meta(url, subpath, ref=None)
    remote_version = remote_meta.get("version")
    remote_commit = remote_meta.get("resolved_commit")
    content_changed = remote_commit is not None and local_commit != remote_commit
    return {
        "name": name,
        "local_version": local_version,
        "remote_version": remote_version,
        "local_commit": local_commit,
        "remote_commit": remote_commit,
        "content_changed": content_changed,
        "up_to_date": remote_version is not None and local_version == remote_version and not content_changed,
    }


def update(name: str, plugins_dir: Path | None = None) -> InstallResult:
    """Reinstall a plugin at its latest version. Returns the new InstallResult.

    Uses the recorded source and prepares the new plugin before replacing
    the installed directory. Ignores any pinned ref.
    """
    plugins_dir = plugins_dir or get_plugins_dir()
    dest = plugins_dir / name
    if not dest.exists():
        raise InstallError(f"no installed plugin named '{name}' in {plugins_dir}")
    manifest = read_install_manifest(dest)
    url = manifest.get("url")
    subpath = manifest.get("subpath")
    source = manifest.get("source", "url")
    if not url:
        raise InstallError(
            f"plugin '{name}' has no url in its install manifest; cannot update"
        )
    registry_url = manifest.get("registry_url")
    if source == "registry" and not registry_url:
        raise InstallError(f"plugin '{name}' has no registry URL in its install manifest")
    with tempfile.TemporaryDirectory(prefix=".update-", dir=plugins_dir) as temporary:
        candidate = install(
            name if source == "registry" else url,
            plugins_dir=Path(temporary), subpath=subpath, registry_url=registry_url,
            expected_plugin_id=name,
        )
        backup = plugins_dir / f".previous-{name}-{uuid4().hex}"
        dest.rename(backup)
        try:
            candidate.path.rename(dest)
        except BaseException:
            backup.rename(dest)
            raise
        candidate.path = dest
        try:
            shutil.rmtree(backup)
        except OSError:
            logger.warning("Updated plugin %s; could not remove previous directory %s", name, backup, exc_info=True)
        return candidate


__all__ = [
    "InstallError",
    "InstallResult",
    "install",
    "uninstall",
    "list_available",
    "search_available",
    "list_installed",
    "check_update",
    "update",
    "read_install_manifest",
    "DEFAULT_REGISTRY_URL",
]
