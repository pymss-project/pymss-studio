"""Tests for the plugin system: registration API, folder-scan loading, install/uninstall."""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import Mock

import pytest

from pymss._builtin_node_types import BUILTIN_NODE_TYPES
from pymss.plugins import (
    CapabilityNotFound,
    bootstrap,
    get_plugins_dir,
    get_registry,
    register_capability,
    register_cli,
    register_node,
    require_capability,
    reset,
)
from pymss.plugins.install import InstallError, install, uninstall


# ---------------------------------------------------------------------------
# Registration API
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registry():
    """Isolate each plugin test without invalidating other modules' graph cache."""
    import pymss.operations as operations
    from pymss.graph.core import _load_builtin_nodes
    from pymss.plugins import loader
    from pymss.plugins.builtins import register_builtin_capabilities
    from pymss.plugins.codecs import register_builtin_codecs

    # Python keeps imported graph modules cached. Snapshot their registrations
    # before clearing the test registry, then restore the same entry identities.
    _load_builtin_nodes()
    reg = get_registry()
    snapshots = [
        (reg.capabilities, dict(reg.capabilities)),
        (reg.nodes, dict(reg.nodes)),
        (reg._reserved_node_types, set(reg._reserved_node_types)),
        (reg._builtin_registrations, dict(reg._builtin_registrations)),
    ]
    commands = list(reg.cli_commands)
    depth = reg._builtin_registration_depth
    runtime, report = operations._DEFAULT_RUNTIME, loader._LAST_REPORT
    try:
        for current, _before in snapshots:
            current.clear()
        reg.cli_commands.clear()
        reg._builtin_registration_depth = 0
        reg._reserved_node_types.update(BUILTIN_NODE_TYPES)
        operations._DEFAULT_RUNTIME = None
        reset()
        register_builtin_capabilities()
        register_builtin_codecs()
        yield
    finally:
        for current, before in snapshots:
            current.clear()
            current.update(before)
        reg.cli_commands[:] = commands
        reg._builtin_registration_depth = depth
        operations._DEFAULT_RUNTIME, loader._LAST_REPORT = runtime, report


def test_register_capability_direct():
    def fn(x):
        return x + 1

    register_capability("inc", fn, source="test")
    assert require_capability("inc")(1) == 2


def test_register_capability_decorator():
    @register_capability("double")
    def double(x):
        return x * 2

    assert double(3) == 6  # decorator returns the original func
    assert require_capability("double")(3) == 6


def test_require_capability_missing_raises():
    with pytest.raises(CapabilityNotFound) as exc_info:
        require_capability("nope")
    assert "nope" in str(exc_info.value)


def test_register_node():
    @register_node("MyPluginNode")
    def handler(ctx, inputs):
        return {"out": "ok"}

    reg = get_registry()
    assert reg.get_node("MyPluginNode") is handler


def test_reserved_node_cannot_be_overridden_by_plugin():
    reg = get_registry()
    reg.reserve_node_type("mss_separate")

    with pytest.raises(ValueError, match="reserved"):
        register_node("mss_separate", lambda ctx, inputs: None, source="evil")


def test_reserved_node_allows_builtin_override():
    reg = get_registry()
    reg.reserve_node_type("mss_separate")
    # Only the trusted core initialization boundary can register reserved nodes.
    with reg._builtin_registration():
        reg.register_node(
            "mss_separate", lambda ctx, inputs: None, source="builtin", allow_override_builtin=True
        )
    assert reg.get_node("mss_separate") is not None


def test_register_cli_parses_path():
    register_cli("opus encode", lambda args: 0, help="encode opus")
    reg = get_registry()
    assert len(reg.cli_commands) == 1
    assert reg.cli_commands[0].path == ("opus", "encode")


# ---------------------------------------------------------------------------
# Folder-scan loading
# ---------------------------------------------------------------------------


def _manifest(plugin_id: str, version: str = "0.1.0") -> str:
    return (
        f'[project]\nname = "pymss-plugin-{plugin_id}"\nversion = "{version}"\n\n'
        f'[tool.pymss.plugin]\nid = "{plugin_id}"\napi_version = 2\n'
    )


def _write_plugin(directory: Path, plugin_id: str, body: str = "", version: str = "0.1.0") -> Path:
    """Write a v2 plugin; ``body`` runs at import and must not register operations."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pyproject.toml").write_text(_manifest(plugin_id, version))
    (directory / "__init__.py").write_text(textwrap.dedent(body) + "\ndef setup(plugin):\n    pass\n")
    return directory


def _fake_clone(plugin_id: str, captured: dict | None = None, version: str = "0.1.0"):
    def clone(url, dest, subpath=None, ref=None):
        if captured is not None:
            captured.update(url=url, subpath=subpath, ref=ref)
        _write_plugin(dest, plugin_id, version=version)
        return {"url": url, "ref": ref, "resolved_commit": "a" * 40}
    return clone


def _make_plugin_dir(plugins_dir: Path, name: str, body: str) -> Path:
    return _write_plugin(plugins_dir / name, f"test.{name}", body)


def test_folder_scan_loads_plugin(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    _make_plugin_dir(
        plugins_dir,
        "myplug",
        """
        from pymss.plugins import register_capability
        register_capability("myplug_thing", lambda x: x * 10, source="myplug")
        """,
    )
    monkeypatch.setenv("PYMSS_PLUGINS_DIR", str(plugins_dir))
    reset()

    report = bootstrap()
    assert report.bootstrapped
    assert "myplug" in report.loaded_names
    assert require_capability("myplug_thing")(5) == 50


def test_folder_scan_isolates_failures(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    # good plugin
    _make_plugin_dir(
        plugins_dir,
        "good",
        """
        from pymss.plugins import register_capability
        register_capability("good_cap", lambda: 1, source="good")
        """,
    )
    # bad plugin (ImportError)
    bad = _write_plugin(plugins_dir / "bad", "test.bad")
    (bad / "__init__.py").write_text("raise RuntimeError('boom')\n")
    monkeypatch.setenv("PYMSS_PLUGINS_DIR", str(plugins_dir))
    reset()

    report = bootstrap()
    assert "good" in report.loaded_names
    failed = {r.name: r for r in report.failed}
    assert "bad" in failed
    assert "boom" in failed["bad"].error
    # good plugin's capability still registered despite bad plugin failing
    assert require_capability("good_cap")() == 1


def test_bootstrap_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("PYMSS_PLUGINS_DIR", str(tmp_path / "empty"))
    reset()
    r1 = bootstrap()
    r2 = bootstrap()
    assert r1 is r2
    r3 = bootstrap(force=True)
    assert r3 is not r1


def test_get_plugins_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PYMSS_PLUGINS_DIR", str(tmp_path / "custom"))
    assert get_plugins_dir() == tmp_path / "custom"


def test_get_plugins_dir_default(monkeypatch):
    monkeypatch.delenv("PYMSS_PLUGINS_DIR", raising=False)
    from pymss.plugins.loader import DEFAULT_PLUGINS_DIR

    assert get_plugins_dir() == DEFAULT_PLUGINS_DIR


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------


def test_install_from_local_path(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "installed"
    # source plugin
    src = _write_plugin(tmp_path / "src" / "myplugin", "author.copied", """
        from pymss.plugins import register_capability
        register_capability('copied_cap', lambda: 42, source='myplugin')
    """)
    monkeypatch.setenv("PYMSS_PLUGINS_DIR", str(plugins_dir))

    result = install(str(src))
    assert result.source == "path"
    assert result.name == "author.copied"
    assert (plugins_dir / "author.copied" / "__init__.py").exists()
    assert not list(plugins_dir.glob(".staging-*"))

    # The copied plugin loads correctly.
    reset()
    bootstrap()
    assert require_capability("copied_cap")() == 42


def test_install_duplicate_raises(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "installed"
    src = _write_plugin(tmp_path / "src" / "dup", "author.dup")
    monkeypatch.setenv("PYMSS_PLUGINS_DIR", str(plugins_dir))

    install(str(src))
    with pytest.raises(InstallError, match="already exists"):
        install(str(src))
    assert not list(plugins_dir.glob(".staging-*"))


def test_install_rejects_sources_without_a_v2_manifest(tmp_path):
    src = tmp_path / "src" / "old"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "pyproject.toml").write_text('[tool.pymss.plugin]\nid = "author.old"\napi_version = 1\n')
    plugins_dir = tmp_path / "installed"
    with pytest.raises(InstallError, match="not a pymss v2 plugin"):
        install(str(src), plugins_dir=plugins_dir)
    assert list(plugins_dir.iterdir()) == []


def test_uninstall_removes_dir(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "installed"
    src = _write_plugin(tmp_path / "src" / "gone", "author.gone")
    monkeypatch.setenv("PYMSS_PLUGINS_DIR", str(plugins_dir))

    install(str(src))
    dest = plugins_dir / "author.gone"
    assert dest.exists()
    removed = uninstall("author.gone")
    assert removed == dest
    assert not dest.exists()


def test_uninstall_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMSS_PLUGINS_DIR", str(tmp_path / "empty"))
    with pytest.raises(InstallError, match="no installed plugin"):
        uninstall("ghost")


def test_install_unknown_name_without_registry_raises(tmp_path, monkeypatch):
    # Point registry at a non-existent URL so the fetch fails fast.
    monkeypatch.setenv("PYMSS_PLUGINS_DIR", str(tmp_path / "p"))
    monkeypatch.setenv("PYMSS_PLUGINS_REGISTRY", "http://127.0.0.1:1/nope.json")
    with pytest.raises(InstallError, match="registry|URL|path|fetch"):
        install("nonexistent-plugin-name")


# ---------------------------------------------------------------------------
# CLI smoke (argparse wiring)
# ---------------------------------------------------------------------------


def test_cli_install_and_plugins_list(tmp_path, monkeypatch):
    from pymss.cli import main

    plugins_dir = tmp_path / "cli_plugins"
    monkeypatch.setenv("PYMSS_PLUGINS_DIR", str(plugins_dir))
    reset()

    src = _write_plugin(tmp_path / "src" / "clipplug", "author.clipplug", """
        from pymss.plugins import register_capability
        register_capability('cli_cap', lambda: 7, source='clipplug')
    """)

    # install
    rc = main(["install", str(src)])
    assert rc == 0

    # plugins list (bootstrap loads the freshly installed plugin)
    reset()
    rc = main(["plugins", "list"])
    assert rc == 0

    # plugins dir
    rc = main(["plugins", "dir"])
    assert rc == 0


# ---------------------------------------------------------------------------
# Registry entry parsing (string URL vs {url, subpath} object)
# ---------------------------------------------------------------------------


def test_registry_entry_dict_parsing(monkeypatch, tmp_path):
    """A registry entry may be {url, subpath} for monorepo plugin collections."""
    from pymss.plugins import install as install_mod

    # Fake registry with both shapes.
    fake_registry = {
        "author.plain": "https://example.com/plain.git",
        "author.mono": {"url": "https://example.com/mono.git", "subpath": "plugins/mono"},
    }
    monkeypatch.setattr(install_mod, "_fetch_registry", lambda url=None: fake_registry)
    monkeypatch.setattr(install_mod, "_clone", _fake_clone("author.plain"))

    plugins_dir = tmp_path / "p"
    # plain string entry
    r = install_mod.install("author.plain", plugins_dir=plugins_dir)
    assert r.source == "registry" and r.name == "author.plain"
    # dict entry with subpath — _clone receives the subpath
    captured = {}
    monkeypatch.setattr(install_mod, "_clone", _fake_clone("author.mono", captured))
    install_mod.install("author.mono", plugins_dir=plugins_dir)
    assert captured["url"] == "https://example.com/mono.git"
    assert captured["subpath"] == "plugins/mono"


def test_registry_entry_must_install_its_own_plugin_id(monkeypatch, tmp_path):
    from pymss.plugins import install as install_mod

    monkeypatch.setattr(install_mod, "_fetch_registry", lambda url=None: {"author.a": "https://example.com/a.git"})
    monkeypatch.setattr(install_mod, "_clone", _fake_clone("author.b"))
    with pytest.raises(InstallError, match="installed plugin id 'author.b'"):
        install_mod.install("author.a", plugins_dir=tmp_path / "p")
    assert list((tmp_path / "p").iterdir()) == []


def test_registry_url_argument_overrides_the_environment(monkeypatch, tmp_path):
    from pymss.plugins import install as install_mod

    monkeypatch.setenv("PYMSS_PLUGINS_REGISTRY", "http://127.0.0.1:1/env.json")
    requested = []
    monkeypatch.setattr(install_mod, "_fetch_registry", lambda url=None: requested.append(url) or {"author.x": "u"})
    assert install_mod.list_available("file:///explicit.json", plugins_dir=tmp_path)[0]["name"] == "author.x"
    assert requested == ["file:///explicit.json"]
    assert install_mod._registry_url("file:///explicit.json") == "file:///explicit.json"
    assert install_mod._registry_url() == "http://127.0.0.1:1/env.json"


def test_registry_entry_missing_url_raises(monkeypatch, tmp_path):
    from pymss.plugins import install as install_mod
    from pymss.plugins.install import InstallError

    fake_registry = {"bad": {"subpath": "x"}}  # no url
    monkeypatch.setattr(install_mod, "_fetch_registry", lambda url=None: fake_registry)
    with pytest.raises(InstallError, match="no 'url'"):
        install_mod.install("bad", plugins_dir=tmp_path / "p")


# ---------------------------------------------------------------------------
# Subdirectory install (URL #fragment and --subpath)
# ---------------------------------------------------------------------------


def test_url_subpath_via_fragment(monkeypatch, tmp_path):
    """A trailing #path on a URL selects that subdirectory of the repo."""
    from pymss.plugins import install as install_mod

    captured = {}
    monkeypatch.setattr(install_mod, "_clone", _fake_clone("author.myplugin", captured))
    r = install_mod.install(
        "https://github.com/xxx/repo#plugins/myplugin",
        plugins_dir=tmp_path / "p",
    )
    assert captured["url"] == "https://github.com/xxx/repo"
    assert captured["subpath"] == "plugins/myplugin"
    assert r.name == "author.myplugin"  # the manifest id, not the folder name
    assert r.source == "url"


def test_url_subpath_via_argument(monkeypatch, tmp_path):
    """The subpath argument selects a subdir without a #fragment in the URL."""
    from pymss.plugins import install as install_mod

    captured = {}
    monkeypatch.setattr(install_mod, "_clone", _fake_clone("author.myplugin", captured))
    r = install_mod.install(
        "https://github.com/xxx/repo",
        plugins_dir=tmp_path / "p",
        subpath="plugins/myplugin",
    )
    assert captured["subpath"] == "plugins/myplugin"
    assert r.name == "author.myplugin"


def test_url_fragment_overrides_subpath_arg(monkeypatch, tmp_path):
    """When both #fragment and subpath arg are given, the fragment wins."""
    from pymss.plugins import install as install_mod

    captured = {}
    monkeypatch.setattr(install_mod, "_clone", _fake_clone("author.frag", captured))
    install_mod.install(
        "https://github.com/xxx/repo#frag/path",
        plugins_dir=tmp_path / "p",
        subpath="arg/path",
    )
    assert captured["subpath"] == "frag/path"


def test_local_path_subpath(tmp_path):
    """Installing a local path with subpath copies only that subdir."""
    from pymss.plugins.install import install as plugin_install

    repo = tmp_path / "repo"
    _write_plugin(repo / "plugins" / "myplugin", "author.myplugin")
    (repo / "other").mkdir()

    dest_root = tmp_path / "p"
    r = plugin_install(str(repo), plugins_dir=dest_root, subpath="plugins/myplugin")
    assert r.name == "author.myplugin"
    assert (dest_root / "author.myplugin" / "__init__.py").exists()
    assert not (dest_root / "other").exists()  # only the subdir was copied


def test_local_path_subpath_missing(tmp_path):
    """A non-existent subpath under a local path raises a clear error."""
    from pymss.plugins.install import install as plugin_install, InstallError

    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(InstallError, match="subpath.*not found"):
        plugin_install(str(repo), plugins_dir=tmp_path / "p", subpath="nope")


# ---------------------------------------------------------------------------
# Dependency detection and package-manager probing
# ---------------------------------------------------------------------------


def test_read_plugin_meta(tmp_path):
    """_read_plugin_meta parses name/version/dependencies from pyproject.toml."""
    from pymss.plugins.install import _read_plugin_meta

    d = tmp_path / "p"
    d.mkdir()
    (d / "pyproject.toml").write_text(
        '[project]\nname = "pymss-plugin-x"\nversion = "1.2.3"\n'
        'dependencies = ["pymss", "numpy", "scipy>=1.10"]\n'
    )
    meta = _read_plugin_meta(d)
    assert meta["name"] == "pymss-plugin-x"
    assert meta["version"] == "1.2.3"
    # pymss itself is filtered out of deps.
    assert meta["dependencies"] == ["numpy", "scipy>=1.10"]


def test_read_plugin_meta_no_pyproject(tmp_path):
    """Missing pyproject.toml returns {} (not an error)."""
    from pymss.plugins.install import _read_plugin_meta

    d = tmp_path / "p"
    d.mkdir()
    assert _read_plugin_meta(d) == {}


@pytest.fixture
def marked_dependencies():
    return [
        "numpy",
        "funasr-mlx>=0.2; sys_platform == 'darwin' and platform_machine == 'arm64'",
        "funasr>=1.2; sys_platform != 'darwin' or platform_machine != 'arm64'",
        "torch>=2.1; sys_platform != 'darwin' or platform_machine != 'arm64'",
        "torchaudio>=2.1; sys_platform != 'darwin' or platform_machine != 'arm64'",
        'requests[socks]>=2.32; python_version >= "3.11"',
    ]


def test_parse_plugin_meta_preserves_markers_and_extras(marked_dependencies):
    from pymss.plugins.install import _parse_plugin_meta

    text = '[project]\nname = "pymss-plugin-asr"\nversion = "0.3.0"\n'
    text += 'dependencies = ' + json.dumps(marked_dependencies) + '\n'
    assert _parse_plugin_meta(text) == {
        "name": "pymss-plugin-asr", "version": "0.3.0", "dependencies": marked_dependencies,
    }


def test_parse_plugin_meta_uses_only_project_fields():
    from pymss.plugins.install import _parse_plugin_meta

    text = '''
[tool.build]
name = "unrelated"
version = "9.0.0"
dependencies = ["tool-only"]
[project]
name = "pymss-plugin-audio"
version = "1.2.3"
dependencies = [
    "numpy", # "comment-only"
]
[project.optional-dependencies]
server = ["extra-only"]
[dependency-groups]
dev = ["dev-only"]
'''
    assert _parse_plugin_meta(text) == {
        "name": "pymss-plugin-audio", "version": "1.2.3", "dependencies": ["numpy"],
    }


def test_parse_plugin_meta_keeps_missing_fields_absent():
    from pymss.plugins.install import _parse_plugin_meta

    assert _parse_plugin_meta('[tool.build]\ndependencies = ["tool-only"]\n') == {}
    assert _parse_plugin_meta('[project]\nname = "audio"\n[project.optional-dependencies]\nserver = ["extra-only"]\n') == {
        "name": "audio",
    }
    assert _parse_plugin_meta('[project]\nname = 12\nversion = true\n') == {}


def test_parse_plugin_meta_filters_only_bare_pymss():
    from pymss.plugins.install import _parse_plugin_meta

    retained = ["pymss>=2.2", "pymss[server]>=2.2", " pymss "]
    text = '[project]\ndependencies = ' + json.dumps(["pymss", "PyMSS", *retained])
    assert _parse_plugin_meta(text)["dependencies"] == retained


@pytest.mark.parametrize(("text", "message"), [
    ("[project", "invalid pyproject.toml"),
    ("project = []", "project must be a TOML table"),
    ('[project]\ndependencies = "numpy"', "project.dependencies must be a list of strings"),
    ('[project]\ndependencies = ["numpy", 1]', "project.dependencies must be a list of strings"),
    ('[project]\ndependencies = {numpy = "*"}', "project.dependencies must be a list of strings"),
])
def test_parse_plugin_meta_rejects_invalid_metadata(text, message):
    from pymss.plugins.install import _parse_plugin_meta

    with pytest.raises(InstallError, match=message):
        _parse_plugin_meta(text)


def test_read_plugin_meta_reports_invalid_utf8(tmp_path):
    from pymss.plugins.install import _read_plugin_meta

    (tmp_path / "pyproject.toml").write_bytes(b"\xff\xfe")
    with pytest.raises(InstallError, match="cannot read pyproject.toml"):
        _read_plugin_meta(tmp_path)


@pytest.mark.parametrize("no_deps", [False, True])
def test_install_preserves_marked_dependencies(tmp_path, monkeypatch, marked_dependencies, no_deps):
    from pymss.plugins import install as install_mod

    source = _write_plugin(tmp_path / "source", "author.marked")
    text = _manifest("author.marked").replace('[project]\n', '[project]\ndependencies = ' + json.dumps(marked_dependencies) + '\n')
    (source / "pyproject.toml").write_text(text, encoding="utf-8")
    dependencies = Mock()
    monkeypatch.setattr(install_mod, "_install_dependencies", dependencies)
    installed = tmp_path / "installed"
    result = install_mod.install(str(source), plugins_dir=installed, no_deps=no_deps)
    if no_deps:
        dependencies.assert_not_called()
        assert result.dependencies_installed == []
    else:
        dependencies.assert_called_once_with(marked_dependencies)
        assert result.dependencies_installed == marked_dependencies
    assert result.path == installed / "author.marked"
    assert list(installed.iterdir()) == [result.path]


def test_install_rejects_invalid_dependencies_before_installing(tmp_path, monkeypatch):
    from pymss.plugins import install as install_mod

    source = _write_plugin(tmp_path / "source", "author.invalid")
    text = _manifest("author.invalid").replace('[project]\n', '[project]\ndependencies = ["numpy", 1]\n')
    (source / "pyproject.toml").write_text(text, encoding="utf-8")
    dependencies = Mock()
    monkeypatch.setattr(install_mod, "_install_dependencies", dependencies)
    installed = tmp_path / "installed"
    with pytest.raises(InstallError, match="project.dependencies must be a list of strings"):
        install_mod.install(str(source), plugins_dir=installed)
    dependencies.assert_not_called()
    assert list(installed.iterdir()) == []


def test_detect_package_manager_returns_string():
    """_detect_package_manager returns 'uv', 'pip', or None."""
    from pymss.plugins.install import _detect_package_manager

    pm = _detect_package_manager()
    assert pm in ("uv", "pip", None)


def test_install_dependencies_empty_is_noop(monkeypatch):
    """An empty deps list calls nothing."""
    from pymss.plugins import install as install_mod

    called = []
    monkeypatch.setattr(install_mod, "_detect_package_manager", lambda: "uv")
    monkeypatch.setattr(install_mod.subprocess, "run", lambda *a, **k: called.append(a))
    install_mod._install_dependencies([])
    assert called == []


# ---------------------------------------------------------------------------
# Version pinning (@ref syntax)
# ---------------------------------------------------------------------------


def test_ref_split_from_name(monkeypatch, tmp_path):
    """`name@ref` splits into arg=name, ref=ref before cloning."""
    from pymss.plugins import install as install_mod

    fake_registry = {"author.plain": {"url": "https://example.com/r.git"}}
    monkeypatch.setattr(install_mod, "_fetch_registry", lambda url=None: fake_registry)
    captured = {}
    monkeypatch.setattr(install_mod, "_clone", _fake_clone("author.plain", captured))
    monkeypatch.setattr(install_mod, "_install_dependencies", lambda deps: None)
    install_mod.install("author.plain@v1.0.0", plugins_dir=tmp_path / "p")
    assert captured["ref"] == "v1.0.0"


def test_ref_not_split_for_local_path(tmp_path):
    """Local paths containing @ (e.g. user dirs) are not split into ref."""
    from pymss.plugins.install import install as plugin_install

    # Create a dir whose absolute path could contain @ (we just test the no-split logic).
    repo = _write_plugin(tmp_path / "repo", "author.repo")
    r = plugin_install(str(repo), plugins_dir=tmp_path / "p")
    assert r.source == "path"


# ---------------------------------------------------------------------------
# Search and available listing
# ---------------------------------------------------------------------------


def test_search_available_matches(monkeypatch):
    import pymss.plugins.install as install_mod
    from pymss.plugins.install import search_available

    fake = {
        "_comment": "skip",
        "loudnorm": {"url": "u", "description": "LUFS loudness", "tags": ["loudness"]},
        "timing": {"url": "u", "description": "pitch shift"},
    }
    monkeypatch.setattr(install_mod, "_fetch_registry", lambda url=None: fake)
    results = search_available("loud")
    assert len(results) == 1
    assert results[0]["name"] == "loudnorm"


def test_list_available_marks_installed(monkeypatch, tmp_path):
    from pymss.plugins.install import list_available
    import pymss.plugins.install as im

    fake = {"x": {"url": "u"}}
    monkeypatch.setattr(im, "_fetch_registry", lambda url=None: fake)
    monkeypatch.setattr(im, "get_plugins_dir", lambda: tmp_path)
    # Nothing installed yet.
    entries = list_available()
    assert entries[0]["installed"] is False
    # Now create it.
    (tmp_path / "x").mkdir()
    entries = list_available()
    assert entries[0]["installed"] is True


# ---------------------------------------------------------------------------
# Install manifest (for updates)
# ---------------------------------------------------------------------------


def test_install_writes_manifest(monkeypatch, tmp_path):
    """install() writes .pymss-install.json with source/url/version."""
    from pymss.plugins import install as install_mod
    import json

    fake = {"author.x": {"url": "https://example.com/r.git", "subpath": "p/x"}}
    monkeypatch.setattr(install_mod, "_fetch_registry", lambda url=None: fake)
    monkeypatch.setattr(
        install_mod, "_clone",
        _fake_clone("author.x", version="0.3"),
    )
    monkeypatch.setattr(install_mod, "_install_dependencies", lambda deps: None)
    install_mod.install("author.x", plugins_dir=tmp_path)
    manifest = json.loads((tmp_path / "author.x" / ".pymss-install.json").read_text())
    assert manifest["name"] == "author.x"
    assert manifest["version"] == "0.3"
    assert manifest["source"] == "registry"
    assert manifest["url"] == "https://example.com/r.git"
    assert manifest["subpath"] == "p/x"


def _plugin_files(directory):
    return {path.relative_to(directory): path.read_bytes() for path in directory.rglob("*") if path.is_file()}


@pytest.fixture
def installed_update(tmp_path):
    source = _write_plugin(tmp_path / "source", "author.updated", "VALUE = 1\n")
    result = install(str(source), plugins_dir=tmp_path / "installed", no_deps=True)
    return source, result.path


@pytest.mark.parametrize("source_kind", ["path", "url"])
def test_update_rejects_changed_identity_before_installing_dependencies(installed_update, monkeypatch, source_kind):
    from pymss.plugins import install as install_mod

    source, destination = installed_update
    if source_kind == "url":
        install_record = destination / ".pymss-install.json"
        record = json.loads(install_record.read_text())
        record.update(source="url", url="https://github.com/owner/plugin")
        install_record.write_text(json.dumps(record))

        def clone(url, dest, subpath=None, ref=None):
            _write_plugin(dest, "author.other")
            (dest / "pyproject.toml").write_text(
                _manifest("author.other").replace('[project]\n', '[project]\ndependencies = ["unwanted>=1"]\n')
            )
            return {"resolved_commit": "a" * 40}

        monkeypatch.setattr(install_mod, "_clone", clone)
    else:
        (source / "pyproject.toml").write_text(
            _manifest("author.other").replace('[project]\n', '[project]\ndependencies = ["unwanted>=1"]\n')
        )
    previous = _plugin_files(destination)
    dependencies = Mock()
    monkeypatch.setattr(install_mod, "_install_dependencies", dependencies)
    with pytest.raises(InstallError, match="update for 'author.updated' contains plugin 'author.other'"):
        install_mod.update("author.updated", plugins_dir=destination.parent)
    dependencies.assert_not_called()
    assert _plugin_files(destination) == previous
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize("failure", ["manifest", "metadata", "dependencies"])
def test_update_preparation_failure_preserves_original(installed_update, monkeypatch, failure):
    from pymss.plugins import install as install_mod

    source, destination = installed_update
    previous = _plugin_files(destination)
    dependencies = Mock()
    monkeypatch.setattr(install_mod, "_install_dependencies", dependencies)
    if failure == "manifest":
        (source / "pyproject.toml").write_text("tool = []\n")
        message = "not a pymss v2 plugin"
    elif failure == "metadata":
        (source / "pyproject.toml").write_text(
            _manifest("author.updated", "0.2.0").replace('[project]\n', '[project]\ndependencies = ["numpy", 1]\n')
        )
        message = "project.dependencies must be a list of strings"
    else:
        (source / "pyproject.toml").write_text(
            _manifest("author.updated", "0.2.0").replace('[project]\n', '[project]\ndependencies = ["dependency>=2"]\n')
        )
        message = "dependency preparation failed"
        dependencies.side_effect = InstallError(message)
    with pytest.raises(InstallError, match=message):
        install_mod.update("author.updated", plugins_dir=destination.parent)
    if failure in {"manifest", "metadata"}:
        dependencies.assert_not_called()
    else:
        dependencies.assert_called_once_with(["dependency>=2"])
    assert _plugin_files(destination) == previous
    assert list(destination.parent.iterdir()) == [destination]


def test_update_restores_original_when_candidate_rename_fails(installed_update, monkeypatch):
    from pymss.plugins import install as install_mod

    source, destination = installed_update
    previous = _plugin_files(destination)
    _write_plugin(source, "author.updated", "VALUE = 2\n", version="0.2.0")
    monkeypatch.setattr(install_mod, "_install_dependencies", Mock())
    rename = Path.rename
    failed_candidates = []

    def fail_candidate(path, target):
        if path.name == "author.updated" and path.parent.name.startswith(".update-"):
            assert not destination.exists()
            assert len(list(destination.parent.glob(".previous-author.updated-*"))) == 1
            failed_candidates.append(path)
            raise OSError("candidate rename failed")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_candidate)
    with pytest.raises(OSError, match="candidate rename failed"):
        install_mod.update("author.updated", plugins_dir=destination.parent)
    assert len(failed_candidates) == 1
    assert _plugin_files(destination) == previous
    assert list(destination.parent.iterdir()) == [destination]


def test_update_replaces_plugin_and_cleans_staging(installed_update, monkeypatch):
    from pymss.plugins import install as install_mod

    source, destination = installed_update
    _write_plugin(source, "author.updated", "VALUE = 2\n", version="0.2.0")
    dependencies = Mock()
    monkeypatch.setattr(install_mod, "_install_dependencies", dependencies)
    result = install_mod.update("author.updated", plugins_dir=destination.parent)
    assert result.path == destination
    assert result.name == "author.updated"
    assert result.version == "0.2.0"
    assert (destination / "__init__.py").read_bytes() == (source / "__init__.py").read_bytes()
    assert json.loads((destination / ".pymss-install.json").read_text())["version"] == "0.2.0"
    dependencies.assert_called_once_with([])
    assert list(destination.parent.iterdir()) == [destination]


def test_update_preserves_marked_dependencies(installed_update, monkeypatch, marked_dependencies):
    from pymss.plugins import install as install_mod

    source, destination = installed_update
    text = _manifest("author.updated", "0.2.0").replace(
        '[project]\n', '[project]\ndependencies = ' + json.dumps(marked_dependencies) + '\n',
    )
    (source / "pyproject.toml").write_text(text, encoding="utf-8")
    dependencies = Mock()
    monkeypatch.setattr(install_mod, "_install_dependencies", dependencies)
    result = install_mod.update("author.updated", plugins_dir=destination.parent)
    dependencies.assert_called_once_with(marked_dependencies)
    assert result.dependencies_installed == marked_dependencies
    assert result.version == "0.2.0"
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize("content", [b"[project", b"project = []", b"\xff\xfe"])
def test_check_update_reports_malformed_remote_metadata(installed_update, monkeypatch, content):
    from pymss.plugins import install as install_mod

    _, destination = installed_update
    record_path = destination / ".pymss-install.json"
    record = json.loads(record_path.read_text())
    record.update(source="url", url="https://github.com/owner/plugin", subpath="plugins/audio")
    record_path.write_text(json.dumps(record))
    previous = _plugin_files(destination)
    commit = "a" * 40
    resolve = Mock(return_value=commit)
    read = Mock(return_value=content)
    monkeypatch.setattr(install_mod, "resolve_github_commit", resolve)
    monkeypatch.setattr(install_mod, "read_github_file", read)
    with pytest.raises(InstallError):
        install_mod.check_update("author.updated", plugins_dir=destination.parent)
    resolve.assert_called_once_with("https://github.com/owner/plugin", None)
    read.assert_called_once_with("https://github.com/owner/plugin", "plugins/audio/pyproject.toml", commit=commit)
    assert _plugin_files(destination) == previous
