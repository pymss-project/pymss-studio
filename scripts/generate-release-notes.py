"""Combine reviewed release summaries with actual packages and update metadata."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_REPOSITORY = "pymss-project/pymss-studio"
GITHUB_ASSET_LIMIT = 2 * 1024**3
PACKAGE_TYPES = (
    ("windows-x64-online", "Windows x64", "推荐：首次启动安装环境 / Recommended: install runtime on first launch", (("-setup.exe", "安装版 / Installer"), (".7z", "便携版 / Portable"))),
    ("windows-x64-cuda", "Windows x64", "NVIDIA CUDA", (("-setup.exe", "安装版 / Installer"), (".7z", "便携版 / Portable"))),
    ("windows-x64-cpu", "Windows x64", "CPU", (("-setup.exe", "安装版 / Installer"), (".7z", "便携版 / Portable"))),
    ("windows-x64-rocm", "Windows x64", "受支持的 AMD GPU / Supported AMD GPUs", (("-setup.exe", "安装版 / Installer"), (".7z", "便携版 / Portable"))),
    ("macos-arm64-mlx", "macOS", "Apple Silicon · MLX", ((".dmg", "DMG"),)),
    ("linux-x64", "Linux x64", "AppImage", ((".AppImage", "AppImage"),)),
)
UPDATE_PLATFORMS = (
    ("windows-x64", "Windows"),
    ("macos-arm64", "macOS"),
    ("linux-x64", "Linux"),
)


def mirror_download_roots(version: str):
    folder = quote(version, safe="")
    return (
        ("Hugging Face", f"https://huggingface.co/datasets/HuanLin/Pymss-Studio-Dist/resolve/main/{folder}"),
        ("魔搭 / ModelScope", f"https://www.modelscope.cn/datasets/huanlinoto/Pymss-Studio-Dist/resolve/master/{folder}"),
    )


class GitHubRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if urlsplit(new_url).scheme != "https":
            raise ValueError("Release asset downloads require HTTPS")
        redirected = super().redirect_request(request, response, code, message, headers, new_url)
        if redirected is not None and urlsplit(new_url).netloc != urlsplit(request.full_url).netloc:
            redirected.remove_header("Authorization")
        return redirected


def github_json(path: str, token: str, accept: str = "application/vnd.github+json"):
    headers = {
        "Accept": accept,
        "User-Agent": "Pymss-Studio-Release-Notes",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(f"https://api.github.com{path}", headers=headers)
    with build_opener(GitHubRedirectHandler()).open(request, timeout=20) as response:
        content = response.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024:
        raise ValueError("Release JSON exceeds the supported size")
    return json.loads(content)


def release_version(tag: str) -> str:
    version = tag.removeprefix("v")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?(?:\+[0-9A-Za-z][0-9A-Za-z.-]*)?", version):
        raise ValueError("Release tag must contain a semantic version")
    return version


def fetch_published_release(
    artifacts: Path, tag: str, repository: str, token: str,
) -> tuple[set[str], dict[str, dict]]:
    release_version(tag)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Repository must be an owner/repository pair")
    root = f"/repos/{repository}/releases"
    try:
        release = github_json(f"{root}/tags/{quote(tag, safe='')}", token)
    except HTTPError as error:
        if error.code == 404:
            return set(), {}
        raise
    if release.get("tag_name") != tag or type(release.get("id")) is not int:
        raise ValueError("Published release does not match the requested tag")

    assets = {}
    # Bound pagination while covering substantially more than the package matrix.
    for page in range(1, 11):
        batch = github_json(f"{root}/{release['id']}/assets?per_page=100&page={page}", token)
        if not isinstance(batch, list):
            raise ValueError("Invalid published release asset list")
        for asset in batch:
            if asset.get("state") == "uploaded":
                assets[asset["name"]] = asset["id"]
        if len(batch) < 100:
            break
    else:
        raise ValueError("Published release has too many assets to merge safely")

    metadata = {}
    # A later platform-only publication must retain the mirror-only packages
    # explicitly linked by our previous notes: GitHub's asset API omits them.
    version = release_version(tag)
    body = release.get("body") or ""
    mirror_only = []
    for slug, _platform, _purpose, formats in PACKAGE_TYPES:
        for suffix, _label in formats:
            name = f"Pymss-Studio-{version}-{slug}{suffix}"
            if all(f"]({root}/{quote(name, safe='')})" in body for _provider, root in mirror_download_roots(version)):
                mirror_only.append(name)
    if mirror_only:
        metadata["mirror-packages"] = {"names": mirror_only}
    update_section = body.partition("## 在线更新 / In-app updates")[2].split("\n## ", 1)[0]
    for slug, platform in UPDATE_PLATFORMS:
        name = f"prerelease-v1-{slug}-update.json"
        if (artifacts / name).is_file():
            continue
        if name in assets:
            metadata[name] = github_json(f"{root}/assets/{assets[name]}", token, "application/octet-stream")
        elif match := re.search(rf"^\| {re.escape(platform)} \| (已启用 / Enabled|请手动安装 / Manual installation) \|\r?$", update_section, re.MULTILINE):
            # Windows/Linux metadata stays on mirrors; retain the previously
            # published status when supplementing a different platform.
            metadata.setdefault("update-status", {})[slug] = match[1]
    return set(assets), metadata


def render_release_notes(
    artifacts: Path, notes_dir: Path, tag: str, *,
    repository: str = DEFAULT_REPOSITORY,
    published_assets: set[str] | None = None,
    published_metadata: dict[str, dict] | None = None,
    allow_missing_summary: bool = False,
) -> str:
    version = release_version(tag)
    summary_path = notes_dir / f"{version}.md"
    summary = (re.sub(r"<!--.*?-->", "", summary_path.read_text(encoding="utf-8-sig"), flags=re.DOTALL).strip()
               if summary_path.exists() or not allow_missing_summary else "")
    if summary_path.exists() and not any(line.strip() and not line.lstrip().startswith("#") for line in summary.splitlines()):
        raise ValueError(f"Release summary is empty or contains only headings: {summary_path}")

    mirror_only = set((published_metadata or {}).get("mirror-packages", {}).get("names", []))
    assets = {path.name for path in artifacts.iterdir() if path.is_file()} | (published_assets or set()) | mirror_only
    repository_url = f"https://github.com/{repository}"
    download_root = f"{repository_url}/releases/download/{quote(tag, safe='')}"
    mirror_roots = mirror_download_roots(version)
    rows = []
    platforms = set()
    for slug, platform, purpose, formats in PACKAGE_TYPES:
        links = []
        for suffix, label in formats:
            name = f"Pymss-Studio-{version}-{slug}{suffix}"
            if name in assets:
                path = artifacts / name
                mirrored = (path.stat().st_size >= GITHUB_ASSET_LIMIT if path.is_file()
                            else name in mirror_only and name not in (published_assets or set()))
                if mirrored:
                    links.append(label + ": " + " · ".join(
                        f"[{provider}]({root}/{quote(name, safe='')})" for provider, root in mirror_roots))
                else:
                    links.append(f"[{label}]({download_root}/{quote(name, safe='')})")
        if links:
            rows.append(f"| {platform} | {purpose} | {' · '.join(links)} |")
            platforms.add(slug.split("-", 1)[0])
    if not rows:
        raise ValueError(f"No installation packages were found for {tag}")

    update_rows = []
    for slug, platform in UPDATE_PLATFORMS:
        if slug.split("-", 1)[0] not in platforms:
            continue
        # This channel is generated for both stable releases and prereleases.
        metadata_name = f"prerelease-v1-{slug}-update.json"
        metadata_path = artifacts / metadata_name
        metadata = (published_metadata or {}).get(metadata_name)
        status = (published_metadata or {}).get("update-status", {}).get(slug, "请在应用内检查 / Check in the application")
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata is not None:
            if metadata.get("version") != version:
                raise ValueError(f"Update metadata version does not match {tag}: {metadata_name}")
            enabled = metadata.get("pymss_update_supported") is True and metadata.get("pymss_requires_manual_install") is False
            status = "已启用 / Enabled" if enabled else "请手动安装 / Manual installation"
        update_rows.append(f"| {platform} | {status} |")

    sections = [
        summary,
        "\n".join([
            "## 安装包选择 / Package selection", "",
            "| 平台 / Platform | 设备或用途 / Hardware or use | 下载 / Download |",
            "| --- | --- | --- |", *rows, "",
            "Windows 新用户推荐在线版，首次启动后安装所需运行环境；离线版包含对应运行环境。",
            "New Windows users can choose Online and install a runtime on first launch. Offline packages include their runtime.", "",
            "达到 2 GiB 的完整包仅在 Hugging Face 和魔搭提供，GitHub 附件不包含这些包，无需下载分卷。",
            "Complete packages of 2 GiB or larger are available on Hugging Face and ModelScope instead of GitHub assets. No split downloads are required.", "",
            f"完整下载 / All downloads: [Hugging Face](https://huggingface.co/datasets/HuanLin/Pymss-Studio-Dist/tree/main/{quote(version, safe='')}) · [魔搭 / ModelScope](https://www.modelscope.cn/datasets/huanlinoto/Pymss-Studio-Dist/tree/master/{quote(version, safe='')})", "",
            "首次安装请选择上表中的完整安装包；更新归档和签名文件不用于首次安装。",
            "For a first installation, use a complete package listed above. Update archives and signature files are not installers.",
        ]),
        "\n".join([
            "## 在线更新 / In-app updates", "",
            "| 平台 / Platform | 本次发行 / This release |", "| --- | --- |", *update_rows, "",
            "更新前请保存当前工程。发布端允许在线更新时，实际可用性仍以“设置 → 关于”的检查结果为准。",
            "Save your projects before updating. Availability remains subject to the checks in Settings → About.",
        ]),
        "\n".join([
            "## 文档与反馈 / Documentation and support", "",
            f"- [中文使用说明]({repository_url}/blob/main/README.zh-CN.md) · [English documentation]({repository_url}/blob/main/README.md)",
            f"- [故障反馈 / Report a bug]({repository_url}/issues/new/choose)",
            f"- [使用问答与功能建议 / Questions and ideas]({repository_url}/discussions)",
            f"- [新用户指南 / Start here]({repository_url}/discussions/88)",
        ]),
    ]
    return "\n\n".join(section for section in sections if section) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    parser.add_argument("--notes-dir", type=Path, default=Path(".github/release-notes"))
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPOSITORY))
    parser.add_argument("--include-published", action="store_true", help="Merge existing assets and update metadata for this tag")
    parser.add_argument("--allow-missing-summary", action="store_true", help="Generate download instructions when reviewed notes are not available")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    tag = os.environ["RELEASE_TAG"]
    published_assets, published_metadata = set(), {}
    if arguments.include_published:
        published_assets, published_metadata = fetch_published_release(
            arguments.artifacts, tag, arguments.repository, os.environ.get("GH_TOKEN", ""),
        )
    notes = render_release_notes(
        arguments.artifacts, arguments.notes_dir, tag, repository=arguments.repository,
        published_assets=published_assets, published_metadata=published_metadata,
        allow_missing_summary=arguments.allow_missing_summary,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(notes, encoding="utf-8")
    print(f"Release notes written to {arguments.output}")
