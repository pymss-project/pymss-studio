"""Offline coverage for GitHub plugin acquisition and archive validation."""

from __future__ import annotations

import io
import json
from pathlib import Path
import stat
from unittest.mock import Mock
import zipfile

import pytest

from pymss.plugins import install as install_mod
from pymss.plugins import sources


REPOSITORY = "https://github.com/owner/plugin"
COMMIT = "a" * 40
MANIFEST = b'''[project]
name = "pymss-plugin-demo"
version = "1.2.3"
[tool.pymss.plugin]
id = "author.demo"
api_version = 2
'''


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    monkeypatch.setattr(sources.urllib.request, "urlopen", Mock(side_effect=AssertionError("Unexpected network request")))


def _archive(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def _mock_download(monkeypatch, archive, *, fail_archive=False):
    requests, temporary_archives = [], []

    def download(url, target, *, limit, accept="application/vnd.github+json"):
        requests.append((url, limit))
        if url.startswith("https://api.github.com/repos/owner/plugin/commits"):
            metadata = [{"sha": COMMIT}] if "/commits?" in url else {"sha": COMMIT}
            target.write(json.dumps(metadata).encode())
        else:
            assert url == f"{REPOSITORY}/archive/{COMMIT}.zip"
            temporary_archives.append(Path(target.name))
            if fail_archive:
                target.write(b"partial download")
                raise sources.SourceError("download interrupted")
            target.write(archive)

    monkeypatch.setattr(sources, "_download", download)
    return requests, temporary_archives


@pytest.mark.parametrize("ref", [None, "release/v1.2.3", COMMIT])
def test_download_resolves_commit_and_extracts_only_selected_subpath(tmp_path, monkeypatch, ref):
    archive = _archive([
        ("repository/plugins/demo/pyproject.toml", MANIFEST),
        ("repository/plugins/demo/backend/operation.py", b"VALUE = 1\n"),
        ("repository/plugins/other/ignored.py", b"IGNORED = True\n"),
    ])
    requests, temporary_archives = _mock_download(monkeypatch, archive)
    destination = tmp_path / "plugin"
    result = sources.download_github_source(REPOSITORY, destination, subpath="plugins/demo", ref=ref)
    assert result == {"url": REPOSITORY, "ref": ref, "resolved_commit": COMMIT}
    endpoint = "commits?per_page=1" if ref is None else f"commits/{sources.urllib.parse.quote(ref, safe='')}?per_page=1"
    assert requests == [
        (f"https://api.github.com/repos/owner/plugin/{endpoint}", sources._MAX_METADATA_BYTES),
        (f"{REPOSITORY}/archive/{COMMIT}.zip", sources._MAX_ARCHIVE_BYTES),
    ]
    assert (destination / "pyproject.toml").read_bytes() == MANIFEST
    assert (destination / "backend/operation.py").read_bytes() == b"VALUE = 1\n"
    assert sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()) == [
        "backend/operation.py", "pyproject.toml",
    ]
    assert len(temporary_archives) == 1
    assert not temporary_archives[0].parent.exists()


def test_install_github_archive_without_git_records_commit(tmp_path, monkeypatch):
    archive = _archive([
        ("repository/pyproject.toml", MANIFEST),
        ("repository/__init__.py", b"def setup(plugin):\n    pass\n"),
    ])
    _, temporary_archives = _mock_download(monkeypatch, archive)
    monkeypatch.setattr(install_mod, "_git_available", lambda: False)
    installed = tmp_path / "installed"
    result = install_mod.install(REPOSITORY, plugins_dir=installed, no_deps=True)
    assert result.name == "author.demo"
    assert result.path == installed / "author.demo"
    record = json.loads((result.path / ".pymss-install.json").read_text())
    assert record["resolved_commit"] == COMMIT
    assert record["url"] == REPOSITORY
    assert list(installed.iterdir()) == [result.path]
    assert not temporary_archives[0].parent.exists()


@pytest.mark.parametrize("filename", [
    "repository/../escape.py", "/absolute.py", "repository/C:/file.py", "repository/dir\\file.py",
    "repository/file\x00.py",
])
def test_archive_rejects_unsafe_paths_before_writing(tmp_path, filename):
    archive = tmp_path / "repository.zip"
    entry = zipfile.ZipInfo(filename)
    # Preserve malformed names instead of normalizing them while building the ZIP.
    entry.filename = filename
    archive.write_bytes(_archive([(entry, b"unsafe")]))
    with zipfile.ZipFile(archive) as source:
        assert source.infolist()[0].orig_filename == filename
    destination = tmp_path / "plugin"
    with pytest.raises(sources.SourceError, match="Unsafe repository path"):
        sources._extract_archive(archive, destination, None)
    assert not destination.exists()
    assert not (tmp_path / "escape.py").exists()


def test_archive_rejects_symlinks(tmp_path):
    link = zipfile.ZipInfo("repository/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive = tmp_path / "repository.zip"
    archive.write_bytes(_archive([(link, b"../../outside")]))
    destination = tmp_path / "plugin"
    with pytest.raises(sources.SourceError, match="link or special file"):
        sources._extract_archive(archive, destination, None)
    assert not destination.exists()


def test_archive_rejects_case_insensitive_duplicate_paths(tmp_path):
    archive = tmp_path / "repository.zip"
    archive.write_bytes(_archive([
        ("repository/Plugin.py", b"first"), ("repository/plugin.py", b"second"),
    ]))
    destination = tmp_path / "plugin"
    with pytest.raises(sources.SourceError, match="duplicate paths"):
        sources._extract_archive(archive, destination, None)
    assert not destination.exists()


@pytest.mark.parametrize(("setting", "limit", "message"), [
    ("_MAX_ARCHIVE_ENTRIES", 1, "too many entries"),
    ("_MAX_EXTRACTED_BYTES", 3, "extracted size limit"),
])
def test_archive_enforces_entry_and_uncompressed_size_limits(tmp_path, monkeypatch, setting, limit, message):
    archive = tmp_path / "repository.zip"
    archive.write_bytes(_archive([("repository/a.py", b"aa"), ("repository/b.py", b"bb")]))
    monkeypatch.setattr(sources, setting, limit)
    destination = tmp_path / "plugin"
    with pytest.raises(sources.SourceError, match=message):
        sources._extract_archive(archive, destination, None)
    assert not destination.exists()


def test_absent_subpath_cleans_download_archive(tmp_path, monkeypatch):
    _, temporary_archives = _mock_download(monkeypatch, _archive([("repository/pyproject.toml", MANIFEST)]))
    destination = tmp_path / "plugin"
    with pytest.raises(sources.SourceError, match="empty or absent"):
        sources.download_github_source(REPOSITORY, destination, subpath="plugins/missing")
    assert not destination.exists()
    assert not temporary_archives[0].parent.exists()


@pytest.mark.parametrize(("content_length", "payload", "too_large"), [
    (None, b"123", False), ("3", b"123", False),
    (None, b"1234", True), ("2", b"1234", True), ("4", b"1234", True),
])
def test_download_enforces_size_limit_and_closes_response(monkeypatch, content_length, payload, too_large):
    class Response(io.BytesIO):
        headers = {} if content_length is None else {"Content-Length": content_length}

        def geturl(self):
            return f"https://codeload.github.com/owner/plugin/zip/{COMMIT}"

    response = Response(payload)
    monkeypatch.setattr(sources.urllib.request, "urlopen", lambda request, timeout: response)
    output = io.BytesIO()
    if too_large:
        with pytest.raises(sources.SourceError, match="size limit"):
            sources._download(f"{REPOSITORY}/archive/{COMMIT}.zip", output, limit=3)
        assert output.getvalue() == b""
    else:
        sources._download(f"{REPOSITORY}/archive/{COMMIT}.zip", output, limit=3)
        assert output.getvalue() == payload
    assert response.closed


@pytest.mark.parametrize("failure", ["download", "invalid-zip", "extract"])
def test_install_failure_cleans_archive_and_partial_staging(tmp_path, monkeypatch, failure):
    archive = _archive([
        ("repository/pyproject.toml", MANIFEST),
        ("repository/__init__.py", b"def setup(plugin):\n    pass\n"),
    ])
    _, temporary_archives = _mock_download(
        monkeypatch, b"invalid zip" if failure == "invalid-zip" else archive,
        fail_archive=failure == "download",
    )
    installed = tmp_path / "installed"
    partial_files = []
    if failure == "extract":
        open_entry = zipfile.ZipFile.open

        def fail_second_file(archive_file, name, mode="r", *args, **kwargs):
            if mode == "r" and isinstance(name, zipfile.ZipInfo) and name.filename.endswith("/__init__.py"):
                partial_files.extend(installed.glob(".staging-*/plugin/pyproject.toml"))
                raise OSError("archive read failed")
            return open_entry(archive_file, name, mode, *args, **kwargs)

        monkeypatch.setattr(zipfile.ZipFile, "open", fail_second_file)
    with pytest.raises(install_mod.InstallError):
        install_mod.install(REPOSITORY, plugins_dir=installed, no_deps=True)
    if failure == "extract":
        assert len(partial_files) == 1
    assert list(installed.iterdir()) == []
    assert len(temporary_archives) == 1
    assert not temporary_archives[0].parent.exists()
