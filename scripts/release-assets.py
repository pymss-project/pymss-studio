"""Package, inspect and select public release assets without changing runtimes."""
from __future__ import annotations

import argparse
import hashlib
import heapq
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MIB = 1024**2
ASSET_LIMIT = 2048 * MIB
WARNING_SIZE = 1900 * MIB
MACOS_REPAIR_MANIFEST = "prerelease-v1-macos-arm64-update.json"


class HTTPSRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if urlsplit(new_url).scheme != "https":
            raise ValueError("Checksum downloads require HTTPS")
        return super().redirect_request(request, response, code, message, headers, new_url)


def read_checksums(body: bytes) -> dict[str, str]:
    digests = {}
    for line in body.decode("utf-8-sig").splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", line)
        if not match:
            raise ValueError("Invalid published checksum entry")
        digest, name = match[1].lower(), match[2]
        if (name in {".", "..", "SHA256SUMS"} or "/" in name or "\\" in name
                or any(ord(character) < 32 or ord(character) == 127 for character in name)):
            raise ValueError("Invalid published checksum filename")
        if name in digests and digests[name] != digest:
            raise ValueError(f"Conflicting published checksums: {name}")
        digests[name] = digest
    return digests


def fetch_checksums(url: str, provider: str) -> dict[str, str]:
    request = Request(url, headers={"User-Agent": "Pymss-Studio-Release-Checksums", "Accept-Encoding": "identity"})
    try:
        with build_opener(HTTPSRedirectHandler()).open(request, timeout=20) as response:
            content_length = response.headers.get("Content-Length")
            if content_length is not None and not re.fullmatch(r"[0-9]+", content_length):
                raise ValueError(f"Invalid published {provider} checksum content length")
            expected_length = int(content_length) if content_length is not None else None
            if expected_length is not None and expected_length > MIB:
                raise ValueError(f"Published {provider} checksum manifest exceeds 1 MiB")
            body = response.read(MIB + 1)
    except HTTPError as error:
        if error.code == 404:
            return {}
        raise ValueError(f"Cannot read published {provider} checksums (HTTP {error.code})") from None
    except URLError:
        raise ValueError(f"Cannot read published {provider} checksums") from None
    if len(body) > MIB:
        raise ValueError(f"Published {provider} checksum manifest exceeds 1 MiB")
    if expected_length is not None and len(body) != expected_length:
        raise ValueError(f"Incomplete published {provider} checksum download "
                         f"(expected {expected_length} bytes, received {len(body)})")
    try:
        return read_checksums(body)
    except ValueError as error:
        raise ValueError(f"Invalid published {provider} checksums: {error}") from None


def previous_checksums(version: str, repository: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid GitHub release repository")
    folder = quote(version, safe="")
    public = fetch_checksums(f"https://github.com/{repository}/releases/download/v{folder}/SHA256SUMS", "GitHub")
    mirrors = [
        fetch_checksums(f"https://huggingface.co/datasets/HuanLin/Pymss-Studio-Dist/resolve/main/{folder}/SHA256SUMS", "Hugging Face"),
        fetch_checksums(f"https://www.modelscope.cn/datasets/huanlinoto/Pymss-Studio-Dist/resolve/master/{folder}/SHA256SUMS", "ModelScope"),
    ]
    return public, mirrors


def merge_mirror_checksums(current: dict[str, str], previous: list[dict[str, str]]) -> dict[str, str]:
    names = set().union(*(manifest.keys() for manifest in previous))
    merged = {}
    for name in names - current.keys():
        values = {manifest.get(name) for manifest in previous}
        if len(values) != 1 or None in values:
            raise ValueError(f"Published mirrors differ for {name}; rebuild its platform before publishing")
        merged[name] = previous[0][name]
    return {**merged, **current}


def write_report(directory: Path, *, tree: bool = False, title: str = "Release package sizes") -> str:
    files = list(directory.rglob("*") if tree else directory.iterdir())
    sizes = [(path, path.stat().st_size) for path in files if path.is_file() and not path.is_symlink()]
    total = sum(size for _, size in sizes)
    rows = [f"### {title}", "", f"{len(sizes)} files; total {total / MIB:.2f} MiB.", "",
            "| File | MiB |", "| --- | ---: |"]
    for path, size in heapq.nlargest(20, sizes, key=lambda item: item[1]):
        name = path.relative_to(directory).as_posix().replace("|", "\\|").replace("\n", " ")
        rows.append(f"| {name} | {size / MIB:.2f} |")
        if not tree and size >= ASSET_LIMIT:
            print(f"::notice::Package exceeds GitHub's limit and is retained on distribution mirrors: {path.name} ({size} bytes)")
        elif not tree and size >= WARNING_SIZE:
            print(f"::warning::Package near the 2 GiB limit: {path.name} ({size} bytes; {ASSET_LIMIT - size} bytes remaining)")
    if tree:
        groups: dict[str, int] = {}
        for path, size in sizes:
            parts = path.relative_to(directory).parts
            group = "/".join(parts[:min(3, len(parts) - 1)]) or "."
            groups[group] = groups.get(group, 0) + size
        rows += ["", "| Directory | MiB |", "| --- | ---: |"]
        rows += [f"| {name.replace('|', '\\|')} | {size / MIB:.2f} |"
                 for name, size in sorted(groups.items(), key=lambda item: item[1], reverse=True)[:20]]
    report = "\n".join(rows) + "\n"
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write(report + "\n")
    print(f"{title}: {len(sizes)} files, {total / MIB:.2f} MiB")
    return report


def validate_sizes(files: list[Path]) -> None:
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Release asset is not a regular file: {path.name}")
        if path.stat().st_size >= ASSET_LIMIT:
            raise ValueError(f"Release asset must be smaller than 2 GiB: {path.name}")
    if len(files) > 1000:
        raise ValueError("A GitHub Release cannot contain more than 1000 assets")


def portable(stage: Path, seven_zip: str) -> list[Path]:
    stage = stage.resolve(strict=True)
    if not re.fullmatch(r"Pymss-Studio-[0-9A-Za-z.+-]+-windows-x64-(cpu|cuda|rocm|online)", stage.name):
        raise ValueError("Unexpected Windows package directory name")
    archive = stage.with_name(stage.name + ".7z")
    if archive.exists() or list(stage.parent.glob(archive.name + ".*")):
        raise ValueError("Portable archive output already exists")
    (stage / "pymss-studio.portable").write_text("portable\n", encoding="ascii")
    high = stage.name.endswith("-rocm")
    subprocess.run([seven_zip, "a", "-t7z", "-m0=lzma2", "-mx=9" if high else "-mx=5",
                    "-mfb=273" if high else "-mfb=128", "-md=768m", "-ms=on", "-mf=BCJ2", "-mmt=on",
                    archive.name, stage.name], cwd=stage.parent, check=True)
    subprocess.run([seven_zip, "t", str(archive)], check=True)
    write_report(stage, tree=True, title=f"{stage.name}: largest runtime files")
    write_report(stage.parent, title=f"{stage.name}: compressed assets")
    return [archive]


def write_checksums(digests: dict[str, str], target: Path) -> None:
    with target.open("w", encoding="utf-8", newline="\n") as stream:
        for name, digest in sorted(digests.items()):
            stream.write(f"{digest}  {name}\n")


def public_assets(source: Path, output: Path, version: str, *,
                  previous_public: dict[str, str] | None = None,
                  previous_mirrors: list[dict[str, str]] | None = None) -> list[Path]:
    prefix = "Pymss-Studio-" + re.escape(version)
    package = re.compile(prefix + r"-(?:windows-x64-(?:cpu|cuda|online|rocm)(?:-setup\.exe|\.7z)|macos-arm64-mlx\.dmg|linux-x64\.AppImage|windows-x64-update\.zip|macos-arm64-update\.app\.tar\.gz)$")
    candidates = list(source.iterdir())
    if any(path.suffix == ".whl" for path in candidates):
        raise ValueError("Runtime wheels must not enter software release assets")
    chosen = [path for path in candidates if package.fullmatch(path.name)
              or path.name in {"README-Linux.txt", MACOS_REPAIR_MANIFEST}]
    if not any(package.fullmatch(path.name) for path in chosen):
        raise ValueError("No release packages were found")
    if any(path.name in {f"Pymss-Studio-{version}-macos-arm64-mlx.dmg",
                         f"Pymss-Studio-{version}-macos-arm64-update.app.tar.gz"} for path in chosen):
        if MACOS_REPAIR_MANIFEST not in {path.name for path in chosen}:
            raise ValueError("The version-pinned macOS repair manifest is missing")
    all_packages = chosen
    for path in all_packages:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Release package is not a regular file: {path.name}")
    chosen = [path for path in all_packages if path.stat().st_size < ASSET_LIMIT]
    validate_sizes(chosen)
    for path in all_packages:
        if path not in chosen:
            if path.name in (previous_public or {}):
                raise ValueError(f"A previously published GitHub package now exceeds its size limit: {path.name}. Publish a new release version.")
            print(f"Mirror-only package: {path.name} ({path.stat().st_size} bytes)")
    # Leave the complete inputs intact: updater metadata and signatures still
    # belong in the mirror version directory, even though GitHub stays concise.
    output.mkdir(parents=True, exist_ok=False)
    for path in chosen:
        target = output / path.name
        try:
            os.link(path, target)
        except OSError:
            shutil.copy2(path, target)
    # Mirrors retain the complete packages and their own complete checksums.
    mirror_files = [path for path in candidates if path.name != "SHA256SUMS" and path.is_file()]
    if any(path.is_symlink() for path in mirror_files):
        raise ValueError("Mirror release assets cannot be symbolic links")
    digests = {}
    for path in mirror_files:
        with path.open("rb") as data:
            digests[path.name] = hashlib.file_digest(data, "sha256").hexdigest()
    public_digests = {**(previous_public or {}), **{path.name: digests[path.name] for path in output.iterdir()}}
    mirror_digests = merge_mirror_checksums(digests, previous_mirrors or [])
    write_checksums(public_digests, output / "SHA256SUMS")
    write_checksums(mirror_digests, source / "SHA256SUMS")
    validate_sizes(list(output.iterdir()))
    write_report(output, title="Public GitHub release assets")
    return sorted(output.iterdir())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report")
    report.add_argument("directory", type=Path)
    report.add_argument("--tree", action="store_true")
    report.add_argument("--title", default="Release package sizes")
    report.add_argument("--check", action="store_true")
    pack = commands.add_parser("portable")
    pack.add_argument("stage", type=Path)
    pack.add_argument("--seven-zip", default=shutil.which("7z") or r"C:\Program Files\7-Zip\7z.exe")
    stage = commands.add_parser("public")
    stage.add_argument("source", type=Path)
    stage.add_argument("output", type=Path)
    stage.add_argument("--version", required=True)
    stage.add_argument("--merge-published", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "report":
            write_report(args.directory, tree=args.tree, title=args.title)
            if args.check:
                validate_sizes([path for path in args.directory.iterdir() if path.is_file()])
        elif args.command == "portable":
            portable(args.stage, args.seven_zip)
        else:
            public, mirrors = previous_checksums(args.version, os.environ.get("GITHUB_REPOSITORY", "pymss-project/pymss-studio")) if args.merge_published else ({}, [])
            public_assets(args.source, args.output, args.version, previous_public=public, previous_mirrors=mirrors)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
