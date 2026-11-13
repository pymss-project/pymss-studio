from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("release_notes", ROOT / "scripts/generate-release-notes.py")
NOTES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NOTES)


def update_metadata(enabled=True, version="1.2.3"):
    return {"version": version, "pymss_update_supported": enabled, "pymss_requires_manual_install": not enabled}


def asset(name, identity):
    return {"name": name, "id": identity, "state": "uploaded"}


class ReleaseNotesTests(unittest.TestCase):
    def test_large_packages_link_to_both_mirrors_without_a_broken_github_download(self):
        name = "Pymss-Studio-1.2.3-windows-x64-rocm-setup.exe"
        (self.artifacts / name).write_bytes(b"a" * 1024)
        with mock.patch.object(NOTES, "GITHUB_ASSET_LIMIT", 1024):
            rendered = NOTES.render_release_notes(self.artifacts, self.notes, "v1.2.3")
        self.assertIn("https://huggingface.co/datasets/HuanLin/Pymss-Studio-Dist/resolve/main/1.2.3/" + name, rendered)
        self.assertIn("https://www.modelscope.cn/datasets/huanlinoto/Pymss-Studio-Dist/resolve/master/1.2.3/" + name, rendered)
        self.assertNotIn("releases/download/v1.2.3/" + name, rendered)
        self.assertIn("No split downloads", rendered)

    def test_download_instructions_are_generated_even_without_reviewed_notes(self):
        (self.notes / "1.2.3.md").unlink()
        (self.artifacts / "Pymss-Studio-1.2.3-windows-x64-online.7z").touch()
        rendered = NOTES.render_release_notes(self.artifacts, self.notes, "v1.2.3", allow_missing_summary=True)
        self.assertIn("Recommended", rendered)
        self.assertIn("Hugging Face", rendered)
        with self.assertRaises(FileNotFoundError):
            NOTES.render_release_notes(self.artifacts, self.notes, "v1.2.3")

    def test_platform_only_republication_retains_previously_linked_mirror_only_packages(self):
        package = self.artifacts / "Pymss-Studio-1.2.3-windows-x64-rocm.7z"
        package.write_bytes(b"a" * 1024)
        with mock.patch.object(NOTES, "GITHUB_ASSET_LIMIT", 1024):
            previous = NOTES.render_release_notes(self.artifacts, self.notes, "v1.2.3")
        package.unlink()
        (self.artifacts / "Pymss-Studio-1.2.3-macos-arm64-mlx.dmg").touch()
        with mock.patch.object(NOTES, "github_json", side_effect=[{**self.release, "body": previous}, []]):
            assets, metadata = NOTES.fetch_published_release(self.artifacts, "v1.2.3", "owner/repo", "")
        rendered = NOTES.render_release_notes(self.artifacts, self.notes, "v1.2.3", published_assets=assets, published_metadata=metadata)
        self.assertIn("Apple Silicon", rendered)
        self.assertIn("resolve/main/1.2.3/" + package.name, rendered)
        self.assertIn("resolve/master/1.2.3/" + package.name, rendered)
        self.assertNotIn("releases/download/v1.2.3/" + package.name, rendered)

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.artifacts = self.root / "artifacts"
        self.notes = self.root / "notes"
        self.artifacts.mkdir()
        self.notes.mkdir()
        (self.notes / "1.2.3.md").write_text("## Highlights\n\n- Improve startup.\n", encoding="utf-8")
        self.release = {"tag_name": "v1.2.3", "id": 42}

    def test_windows_supplement_preserves_published_macos_links_and_update_state(self):
        mac = "Pymss-Studio-1.2.3-macos-arm64-mlx.dmg"
        windows = "Pymss-Studio-1.2.3-windows-x64-cpu-setup.exe"
        metadata_name = "prerelease-v1-macos-arm64-update.json"
        (self.artifacts / windows).touch()
        (self.artifacts / "prerelease-v1-windows-x64-update.json").write_text(
            json.dumps(update_metadata(False)), encoding="utf-8",
        )
        published = [asset(mac, 1), asset(metadata_name, 2), asset("Pymss-Studio-1.2.2-linux-x64.AppImage", 3)]
        with mock.patch.object(NOTES, "github_json", side_effect=[self.release, published, update_metadata()]) as api:
            assets, metadata = NOTES.fetch_published_release(self.artifacts, "v1.2.3", "owner/repo", "token")
        self.assertEqual(api.call_args_list[-1].args, ("/repos/owner/repo/releases/assets/2", "token", "application/octet-stream"))
        rendered = NOTES.render_release_notes(
            self.artifacts, self.notes, "v1.2.3", repository="owner/repo",
            published_assets=assets, published_metadata=metadata,
        )
        self.assertIn(f"https://github.com/owner/repo/releases/download/v1.2.3/{mac}", rendered)
        self.assertIn(windows, rendered)
        self.assertIn("| macOS | 已启用 / Enabled |", rendered)
        self.assertIn("| Windows | 请手动安装 / Manual installation |", rendered)
        self.assertNotIn("1.2.2", rendered)
        self.assertEqual(rendered.count("Improve startup."), 1)

    def test_macos_supplement_preserves_windows_and_linux_update_status_from_notes(self):
        packages = ["Pymss-Studio-1.2.3-windows-x64-online.7z", "Pymss-Studio-1.2.3-linux-x64.AppImage"]
        for package, slug, enabled in zip(packages, ("windows-x64", "linux-x64"), (True, False)):
            (self.artifacts / package).touch()
            (self.artifacts / f"prerelease-v1-{slug}-update.json").write_text(json.dumps(update_metadata(enabled)), encoding="utf-8")
        previous = NOTES.render_release_notes(self.artifacts, self.notes, "v1.2.3")
        for path in self.artifacts.iterdir():
            path.unlink()
        (self.artifacts / "Pymss-Studio-1.2.3-macos-arm64-mlx.dmg").touch()
        (self.artifacts / "prerelease-v1-macos-arm64-update.json").write_text(json.dumps(update_metadata()), encoding="utf-8")
        published = [asset(package, index) for index, package in enumerate(packages, 1)]
        with mock.patch.object(NOTES, "github_json", side_effect=[{**self.release, "body": previous}, published]):
            assets, metadata = NOTES.fetch_published_release(self.artifacts, "v1.2.3", "owner/repo", "")
        rendered = NOTES.render_release_notes(self.artifacts, self.notes, "v1.2.3", published_assets=assets, published_metadata=metadata)
        self.assertIn("| Windows | 已启用 / Enabled |", rendered)
        self.assertIn("| Linux | 请手动安装 / Manual installation |", rendered)
        self.assertIn("| macOS | 已启用 / Enabled |", rendered)

    def test_current_metadata_takes_precedence_without_downloading_the_old_copy(self):
        package = "Pymss-Studio-1.2.3-windows-x64-cpu-setup.exe"
        metadata_name = "prerelease-v1-windows-x64-update.json"
        (self.artifacts / package).touch()
        (self.artifacts / metadata_name).write_text(json.dumps(update_metadata(False)), encoding="utf-8")
        self.release["body"] = "## 在线更新 / In-app updates\n\n| Windows | 已启用 / Enabled |\n"
        with mock.patch.object(NOTES, "github_json", side_effect=[self.release, [asset(package, 1), asset(metadata_name, 2)]]) as api:
            assets, metadata = NOTES.fetch_published_release(self.artifacts, "v1.2.3", "owner/repo", "token")
        self.assertEqual(api.call_count, 2)
        self.assertEqual(metadata, {})
        rendered = NOTES.render_release_notes(
            self.artifacts, self.notes, "v1.2.3", published_assets=assets,
            published_metadata={metadata_name: update_metadata(True, "1.2.2")},
        )
        self.assertIn("| Windows | 请手动安装 / Manual installation |", rendered)

    def test_only_a_missing_release_is_treated_as_a_first_publication(self):
        for status in (404, 403, 500):
            with self.subTest(status=status):
                error = HTTPError("https://api.github.com", status, "request failed", None, None)
                with mock.patch.object(NOTES, "github_json", side_effect=error):
                    if status == 404:
                        self.assertEqual(NOTES.fetch_published_release(self.artifacts, "v1.2.3", "owner/repo", ""), (set(), {}))
                    else:
                        with self.assertRaises(HTTPError):
                            NOTES.fetch_published_release(self.artifacts, "v1.2.3", "owner/repo", "")
        with mock.patch.object(NOTES, "github_json", side_effect=TimeoutError("request timed out")):
            with self.assertRaises(TimeoutError):
                NOTES.fetch_published_release(self.artifacts, "v1.2.3", "owner/repo", "")

    def test_asset_pagination_keeps_packages_after_the_first_page(self):
        first = [asset(f"checksum-{index}", index + 10) for index in range(100)]
        package = "Pymss-Studio-1.2.3-linux-x64.AppImage"
        with mock.patch.object(NOTES, "github_json", side_effect=[self.release, first, [asset(package, 110)]]) as api:
            assets, _metadata = NOTES.fetch_published_release(self.artifacts, "v1.2.3", "owner/repo", "")
        self.assertIn(package, assets)
        self.assertEqual(api.call_args_list[-1].args[0], "/repos/owner/repo/releases/42/assets?per_page=100&page=2")

    def test_mismatched_published_metadata_and_tag_are_rejected(self):
        package = "Pymss-Studio-1.2.3-macos-arm64-mlx.dmg"
        with self.assertRaisesRegex(ValueError, "metadata version"):
            NOTES.render_release_notes(
                self.artifacts, self.notes, "v1.2.3", published_assets={package},
                published_metadata={"prerelease-v1-macos-arm64-update.json": update_metadata(True, "1.2.2")},
            )
        with mock.patch.object(NOTES, "github_json", return_value={"tag_name": "v1.2.2", "id": 42}):
            with self.assertRaisesRegex(ValueError, "requested tag"):
                NOTES.fetch_published_release(self.artifacts, "v1.2.3", "owner/repo", "")

    def test_a_missing_published_metadata_asset_does_not_hide_a_failed_query(self):
        error = HTTPError("https://api.github.com", 404, "asset missing", None, None)
        published = [asset("prerelease-v1-macos-arm64-update.json", 2)]
        with mock.patch.object(NOTES, "github_json", side_effect=[self.release, published, error]):
            with self.assertRaises(HTTPError):
                NOTES.fetch_published_release(self.artifacts, "v1.2.3", "owner/repo", "")

    def test_download_redirect_does_not_send_the_token_to_the_asset_host(self):
        request = Request("https://api.github.com/repos/owner/repo/releases/assets/2", headers={"Authorization": "Bearer token"})
        redirected = NOTES.GitHubRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/file",
        )
        self.assertIsNone(redirected.get_header("Authorization"))
        self.assertEqual(request.get_header("Authorization"), "Bearer token")

    def test_download_redirect_cannot_downgrade_to_http(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            NOTES.GitHubRedirectHandler().redirect_request(
                Request("https://api.github.com/file"), None, 302, "Found", {}, "http://example.com/file",
            )
