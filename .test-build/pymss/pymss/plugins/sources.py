"""Download public GitHub plugin sources without a local Git installation."""

from __future__ import annotations

import io
import json
import re
import shutil
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import BinaryIO

_HTTP_TIMEOUT = 30
_DOWNLOAD_TIMEOUT = 180
_MAX_METADATA_BYTES = 1024 * 1024
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 50_000
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_GITHUB_HOSTS = {"api.github.com", "codeload.github.com", "github.com"}
_RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class SourceError(RuntimeError):
    """A remote source could not be resolved, downloaded, or extracted."""


def github_repository(url: str) -> tuple[str, str] | None:
    """Return the owner/repository for public HTTPS GitHub repository URLs."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"}:
        return None
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in {None, 443}:
        raise SourceError("Use an HTTPS GitHub repository URL without credentials, query, or fragment.")
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2:
        raise SourceError("Use the repository URL https://github.com/OWNER/REPOSITORY.")
    owner, repo = parts
    repo = repo.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo) or repo in {".", ".."}:
        raise SourceError("Invalid GitHub repository owner or name.")
    return owner, repo


def _download(url: str, target: BinaryIO, *, limit: int, accept: str = "application/vnd.github+json") -> None:
    request = urllib.request.Request(url, headers={
        "User-Agent": "pymss",
        "Accept": accept,
        "X-GitHub-Api-Version": "2026-03-10",
    })
    started = time.monotonic()
    try:
        # Keep the opener installed by the host, including its proxy policy.
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:  # noqa: S310
            parsed = urllib.parse.urlsplit(response.geturl())
            if parsed.scheme != "https" or parsed.hostname not in _GITHUB_HOSTS:
                raise SourceError("GitHub returned an unsupported response URL.")
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > limit:
                raise SourceError(f"GitHub response exceeds the {limit // (1024 * 1024)} MiB size limit.")
            size = 0
            while True:
                if time.monotonic() - started > _DOWNLOAD_TIMEOUT:
                    raise SourceError("GitHub download exceeded the allowed time.")
                chunk = response.read(min(64 * 1024, limit - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise SourceError(f"GitHub response exceeds the {limit // (1024 * 1024)} MiB size limit.")
                target.write(chunk)
    except urllib.error.HTTPError as exc:
        if exc.code == 429 or (exc.code == 403 and (exc.headers.get("X-RateLimit-Remaining") == "0" or exc.headers.get("Retry-After"))):
            message = "GitHub API rate limit reached; wait before retrying."
        elif exc.code in {401, 403}:
            message = "GitHub denied access; HTTPS archive installation requires a public repository."
        elif exc.code == 404:
            message = "GitHub repository, ref, or file was not found; verify the URL and public access."
        else:
            message = f"GitHub request failed with HTTP {exc.code}."
        exc.close()
        raise SourceError(message) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SourceError(f"GitHub download failed: {exc}") from exc


def _repository_api(url: str) -> str:
    repository = github_repository(url)
    if repository is None:
        raise SourceError("This source is not a public HTTPS GitHub repository URL.")
    owner, repo = repository
    return f"https://api.github.com/repos/{owner}/{repo}"


def resolve_github_commit(url: str, ref: str | None = None) -> str:
    """Resolve an explicit ref or the current default branch to a full SHA."""
    api = _repository_api(url)
    endpoint = f"{api}/commits/{urllib.parse.quote(ref, safe='')}?per_page=1" if ref else f"{api}/commits?per_page=1"
    output = io.BytesIO()
    _download(endpoint, output, limit=_MAX_METADATA_BYTES)
    try:
        data = json.loads(output.getvalue())
        commit = data.get("sha") if ref else data[0].get("sha")
    except (ValueError, AttributeError, IndexError, KeyError, TypeError) as exc:
        raise SourceError("GitHub returned an invalid commit response.") from exc
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        raise SourceError("GitHub did not return a full commit SHA.")
    if ref and _COMMIT_RE.fullmatch(ref.lower()) and commit != ref.lower():
        raise SourceError("GitHub resolved a different commit than requested.")
    return commit


def _relative_parts(value: str) -> tuple[str, ...]:
    parts = value.rstrip("/").split("/")
    if not value or "\\" in value or any(
        part in {"", ".", ".."} or part.endswith((".", " "))
        or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
        or part.split(".", 1)[0].lower() in _RESERVED_NAMES
        for part in parts
    ):
        raise SourceError(f"Unsafe repository path: {value!r}.")
    return tuple(parts)


def read_github_file(url: str, path: str, *, commit: str) -> bytes:
    """Read bounded file content at an already resolved GitHub commit."""
    _relative_parts(path)
    if not _COMMIT_RE.fullmatch(commit):
        raise SourceError("A full commit SHA is required to read repository content.")
    endpoint = f"{_repository_api(url)}/contents/{urllib.parse.quote(path, safe='/')}?ref={commit}"
    output = io.BytesIO()
    _download(endpoint, output, limit=_MAX_METADATA_BYTES, accept="application/vnd.github.raw+json")
    return output.getvalue()


def _extract_archive(archive: Path, destination: Path, subpath: str | None) -> None:
    selected = _relative_parts(subpath) if subpath else ()
    try:
        with zipfile.ZipFile(archive) as source:
            entries = source.infolist()
            if not entries or len(entries) > _MAX_ARCHIVE_ENTRIES:
                raise SourceError("GitHub archive is empty or contains too many entries.")
            total = 0
            root = None
            seen: set[str] = set()
            files: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
            for entry in entries:
                # zipfile normalizes separators and truncates NULs in filename.
                # Validate the stored name before accepting that normalization.
                parts = _relative_parts(entry.orig_filename)
                root = root or parts[0]
                if parts[0] != root:
                    raise SourceError("GitHub archive contains multiple root directories.")
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if mode not in {0, stat.S_IFREG, stat.S_IFDIR} or (mode == stat.S_IFDIR and not entry.is_dir()):
                    raise SourceError(f"GitHub archive contains a link or special file: {entry.filename!r}.")
                key = entry.filename.rstrip("/").casefold()
                if key in seen:
                    raise SourceError(f"GitHub archive contains duplicate paths: {entry.filename!r}.")
                seen.add(key)
                total += entry.file_size
                if total > _MAX_EXTRACTED_BYTES:
                    raise SourceError("GitHub archive exceeds the 1 GiB extracted size limit.")
                relative = parts[1:]
                if relative[:len(selected)] == selected and len(relative) > len(selected):
                    files.append((entry, relative[len(selected):]))
            if not files:
                raise SourceError(f"Plugin directory {subpath or '.'!r} is empty or absent from the GitHub archive.")
            destination.mkdir(parents=True, exist_ok=False)
            root_path = destination.resolve()
            for entry, relative in files:
                target = destination.joinpath(*relative)
                if not target.resolve().is_relative_to(root_path):
                    raise SourceError("GitHub archive entry escapes the destination directory.")
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(entry) as content, target.open("xb") as output:
                        shutil.copyfileobj(content, output, length=64 * 1024)
                    if entry.external_attr >> 16 & stat.S_IXUSR:
                        target.chmod(target.stat().st_mode | stat.S_IXUSR)
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        if isinstance(exc, SourceError):
            raise
        raise SourceError(f"Could not extract the GitHub archive: {exc}") from exc


def download_github_source(
    url: str, dest: Path, *, subpath: str | None = None, ref: str | None = None,
) -> dict[str, str | None]:
    """Download a commit-pinned ZIP and return metadata for the install record.

    The caller owns the destination's staging lifetime and removes it if the
    download, extraction, or subsequent plugin validation fails.
    """
    if dest.exists():
        raise SourceError(f"Destination already exists: {dest}.")
    if subpath:
        _relative_parts(subpath)
    commit = resolve_github_commit(url, ref)
    with tempfile.TemporaryDirectory(prefix="pymss-github-") as temporary:
        archive = Path(temporary) / "repository.zip"
        with archive.open("wb") as output:
            owner, repo = github_repository(url)
            _download(f"https://github.com/{owner}/{repo}/archive/{commit}.zip", output, limit=_MAX_ARCHIVE_BYTES)
        _extract_archive(archive, dest, subpath)
    return {"url": url, "ref": ref, "resolved_commit": commit}
