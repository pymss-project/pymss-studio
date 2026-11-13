from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("release_assets", ROOT / "scripts/release-assets.py")
ASSETS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ASSETS)


class ReleaseAssetsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.root / "artifacts"
        self.source.mkdir()
        self.output = self.root / "public"

    def file(self, name, body=b"package"):
        path = self.source / name
        path.write_bytes(body)
        return path

    def test_public_matrix_has_fifteen_assets_and_preserves_private_mirror_inputs(self):
        packages = [f"Pymss-Studio-1.2.3-windows-x64-{variant}{suffix}"
                    for variant in ("cpu", "cuda", "rocm", "online") for suffix in ("-setup.exe", ".7z")]
        packages += ["Pymss-Studio-1.2.3-macos-arm64-mlx.dmg", "Pymss-Studio-1.2.3-linux-x64.AppImage",
                     "Pymss-Studio-1.2.3-windows-x64-update.zip", "Pymss-Studio-1.2.3-macos-arm64-update.app.tar.gz", "README-Linux.txt",
                     ASSETS.MACOS_REPAIR_MANIFEST]
        private = ["stable-v1-windows-x64-update.json", "managed-update-metadata.txt",
                   "Pymss-Studio-1.2.3-macos-arm64-update.app.tar.gz.json",
                   "Pymss-Studio-1.2.3-windows-x64-update.zip.sig"]
        for name in packages + private:
            self.file(name)
        result = ASSETS.public_assets(self.source, self.output, "1.2.3")
        self.assertEqual({path.name for path in result}, {*packages, "SHA256SUMS"})
        self.assertEqual(len(result), 15)
        self.assertTrue(all((self.source / name).is_file() for name in private))
        checksum = (self.output / "SHA256SUMS").read_text(encoding="utf-8")
        self.assertEqual(len(checksum.splitlines()), len(packages))
        self.assertNotIn(".sig", checksum)
        mirror_checksum = (self.source / "SHA256SUMS").read_text(encoding="utf-8")
        for name in private:
            self.assertIn(name, mirror_checksum)
        self.assertIn(hashlib.sha256(b"package").hexdigest(), checksum)

    def test_macos_release_requires_its_version_pinned_repair_manifest(self):
        archive = self.file("Pymss-Studio-1.2.3-macos-arm64-update.app.tar.gz")
        with self.assertRaisesRegex(ValueError, "macOS repair manifest"):
            ASSETS.public_assets(self.source, self.output, "1.2.3")
        self.assertFalse(self.output.exists())
        self.file(ASSETS.MACOS_REPAIR_MANIFEST, b'{"version":"1.2.3"}')
        ASSETS.public_assets(self.source, self.output, "1.2.3")
        self.assertTrue((self.output / archive.name).is_file())
        self.assertTrue((self.output / ASSETS.MACOS_REPAIR_MANIFEST).is_file())

    def test_generated_macos_repair_metadata_keeps_its_version_specific_download(self):
        spec = importlib.util.spec_from_file_location("macos_repair_metadata", ROOT / "scripts/generate-update-metadata.py")
        metadata = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(metadata)
        archive = self.file("Pymss-Studio-1.2.3-macos-arm64-update.app.tar.gz")
        self.file(archive.name + ".sig", b"signature")
        self.file(archive.name + ".json", json.dumps({"version": "1.2.3", "pymss_unpacked_size": 1024,
                  "pymss_runtime_contract": {"schemaVersion": 1}}).encode())
        metadata.generate(self.source, {"RELEASE_TAG": "v1.2.3"})
        ASSETS.public_assets(self.source, self.output, "1.2.3")
        manifest = json.loads((self.output / "prerelease-v1-macos-arm64-update.json").read_text())
        target = manifest["platforms"]["darwin-aarch64"]
        self.assertEqual(target["signature"], "signature")
        self.assertEqual(target["url"], "https://github.com/pymss-project/pymss-studio/releases/download/v1.2.3/" + archive.name)
        self.assertTrue((self.output / archive.name).is_file())

    def test_partial_republication_preserves_previous_platform_and_mirror_only_checksums(self):
        previous_public = {"Pymss-Studio-1.2.3-windows-x64-cpu.7z": "a" * 64}
        previous_mirror = {**previous_public, "Pymss-Studio-1.2.3-windows-x64-rocm.7z": "b" * 64}
        previous_mirror[ASSETS.MACOS_REPAIR_MANIFEST] = "c" * 64
        self.file("Pymss-Studio-1.2.3-macos-arm64-mlx.dmg")
        self.file(ASSETS.MACOS_REPAIR_MANIFEST, b"new repair metadata")
        ASSETS.public_assets(self.source, self.output, "1.2.3", previous_public=previous_public,
                             previous_mirrors=[previous_mirror, previous_mirror.copy()])
        public = ASSETS.read_checksums((self.output / "SHA256SUMS").read_bytes())
        mirrored = ASSETS.read_checksums((self.source / "SHA256SUMS").read_bytes())
        self.assertEqual(public["Pymss-Studio-1.2.3-windows-x64-cpu.7z"], "a" * 64)
        self.assertNotIn("Pymss-Studio-1.2.3-windows-x64-rocm.7z", public)
        self.assertEqual(mirrored["Pymss-Studio-1.2.3-windows-x64-rocm.7z"], "b" * 64)
        self.assertEqual(mirrored[ASSETS.MACOS_REPAIR_MANIFEST], hashlib.sha256(b"new repair metadata").hexdigest())

    def test_only_rebuilt_files_can_resolve_existing_mirror_differences(self):
        with self.assertRaisesRegex(ValueError, "mirrors differ"):
            ASSETS.merge_mirror_checksums({}, [{"old.7z": "a" * 64}, {"old.7z": "b" * 64}])
        with self.assertRaisesRegex(ValueError, "mirrors differ"):
            ASSETS.merge_mirror_checksums({}, [{"old.7z": "a" * 64}, {}])
        self.assertEqual(ASSETS.merge_mirror_checksums({"old.7z": "c" * 64},
                         [{"old.7z": "a" * 64}, {"old.7z": "b" * 64}]), {"old.7z": "c" * 64})

    def test_previously_public_package_cannot_silently_become_mirror_only(self):
        archive = self.file("Pymss-Studio-1.2.3-windows-x64-rocm.7z", b"a" * 1024)
        with mock.patch.object(ASSETS, "ASSET_LIMIT", 1024), self.assertRaisesRegex(ValueError, "new release version"):
            ASSETS.public_assets(self.source, self.output, "1.2.3", previous_public={archive.name: "b" * 64})
        self.assertFalse(self.output.exists())
        self.assertFalse((self.source / "SHA256SUMS").exists())

    def test_checksum_reader_rejects_corrupt_or_conflicting_manifests(self):
        self.assertEqual(ASSETS.read_checksums(b"a" * 64 + b"  package.7z\r\n"), {"package.7z": "a" * 64})
        for name in ("../private", "folder/file", "folder\\file", "SHA256SUMS"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                ASSETS.read_checksums(("a" * 64 + "  " + name).encode())
        for body in (b"not a checksum", b"a" * 64 + b"  package\n" + b"b" * 64 + b"  package"):
            with self.assertRaises(ValueError):
                ASSETS.read_checksums(body)

    def test_published_checksum_download_only_treats_404_as_a_new_release(self):
        opener = mock.Mock()
        with mock.patch.object(ASSETS, "build_opener", return_value=opener):
            opener.open.return_value = io.BytesIO(b"a" * 64 + b"  package.7z\n")
            opener.open.return_value.headers = {}
            self.assertEqual(ASSETS.fetch_checksums("https://example.com/SHA256SUMS", "mirror"), {"package.7z": "a" * 64})
            opener.open.side_effect = HTTPError("https://example.com", 404, "missing", None, None)
            self.assertEqual(ASSETS.fetch_checksums("https://example.com/SHA256SUMS", "mirror"), {})
            for error in (HTTPError("https://example.com", 403, "denied", None, None), URLError("offline")):
                opener.open.side_effect = error
                with self.assertRaisesRegex(ValueError, "Cannot read published"):
                    ASSETS.fetch_checksums("https://example.com/SHA256SUMS", "mirror")

    def test_checksum_download_rejects_a_connection_closed_after_a_complete_entry(self):
        first = b"a" * 64 + b"  windows.7z\n"
        second = b"b" * 64 + b"  macos.dmg\n"

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                if self.path != "/without-length":
                    self.send_header("Content-Length", str(len(first + second)))
                self.end_headers()
                self.wfile.write(first if self.path == "/truncated" else first + second)
                self.close_connection = True

            def log_message(self, *args):
                pass

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                root = f"http://127.0.0.1:{server.server_port}"
                with self.assertRaisesRegex(ValueError, "Incomplete published mirror checksum download"):
                    ASSETS.fetch_checksums(root + "/truncated", "mirror")
                for path in ("/complete", "/without-length"):
                    with self.subTest(path=path):
                        self.assertEqual(ASSETS.fetch_checksums(root + path, "mirror"),
                                         {"windows.7z": "a" * 64, "macos.dmg": "b" * 64})
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_checksum_download_rejects_invalid_or_oversized_declared_lengths_before_reading(self):
        for length in ("invalid", "-1", str(ASSETS.MIB + 1)):
            with self.subTest(length=length):
                response = mock.MagicMock()
                response.headers = {"Content-Length": length}
                response.__enter__.return_value = response
                with mock.patch.object(ASSETS, "build_opener") as opener:
                    opener.return_value.open.return_value = response
                    with self.assertRaises(ValueError):
                        ASSETS.fetch_checksums("https://example.com/SHA256SUMS", "mirror")
                response.read.assert_not_called()

    def test_size_boundary_keeps_full_single_files_and_checksums_on_mirrors(self):
        small = self.file("Pymss-Studio-1.2.3-windows-x64-online-setup.exe", b"a" * 1023)
        exact = self.file("Pymss-Studio-1.2.3-windows-x64-rocm-setup.exe", b"b" * 1024)
        large = self.file("Pymss-Studio-1.2.3-windows-x64-rocm.7z", b"c" * 1025)
        with mock.patch.object(ASSETS, "ASSET_LIMIT", 1024):
            ASSETS.public_assets(self.source, self.output, "1.2.3")
        self.assertTrue((self.output / small.name).is_file())
        for package in (exact, large):
            self.assertFalse((self.output / package.name).exists())
            self.assertIn(package.name, (self.source / "SHA256SUMS").read_text())
            self.assertNotIn(package.name, (self.output / "SHA256SUMS").read_text())
            self.assertEqual(package.stat().st_size, 1024 if package == exact else 1025)
        self.assertFalse(list(self.source.glob("*.001")))

    def test_wheels_cannot_leak_into_software_release_and_existing_output_is_not_overwritten(self):
        self.file("Pymss-Studio-1.2.3-windows-x64-online.7z")
        wheel = self.file("runtime.whl")
        with self.assertRaisesRegex(ValueError, "Runtime wheels"):
            ASSETS.public_assets(self.source, self.output, "1.2.3")
        self.assertFalse(self.output.exists())
        wheel.unlink()
        self.output.mkdir()
        marker = self.output / "keep"
        marker.write_text("existing")
        with self.assertRaises(FileExistsError):
            ASSETS.public_assets(self.source, self.output, "1.2.3")
        self.assertEqual(marker.read_text(), "existing")

    def test_update_metadata_retains_the_same_published_archive_urls(self):
        spec = importlib.util.spec_from_file_location("release_asset_updates", ROOT / "scripts/generate-update-metadata.py")
        updates = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(updates)
        archive = self.file("Pymss-Studio-1.2.3-windows-x64-update.zip")
        self.file(archive.name + ".sig", b"signed-archive")
        self.file("Pymss-Studio-1.2.3-windows-x64-online-setup.exe")
        channels = updates.generate(self.source, {"RELEASE_TAG": "v1.2.3", "RELEASE_AUTO_UPDATE_SUPPORTED": "true"})
        ASSETS.public_assets(self.source, self.output, "1.2.3")
        for channel in channels:
            data = json.loads((self.source / channel).read_text())
            target = data["platforms"]["windows-x86_64"]
            self.assertEqual(target["signature"], "signed-archive")
            self.assertEqual(target["url"], "https://github.com/pymss-project/pymss-studio/releases/download/v1.2.3/" + archive.name)
            self.assertFalse((self.output / channel).exists())
        self.assertTrue((self.output / archive.name).is_file())

    def test_publication_keeps_complete_mirrors_and_existing_update_channel(self):
        jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))["jobs"]
        steps = jobs["github-release"]["steps"]
        self.assertEqual(jobs["github-release"]["concurrency"]["group"], "pymss-studio-release-publish")
        self.assertIs(jobs["github-release"]["concurrency"]["cancel-in-progress"], False)
        publish = next(step for step in steps if step.get("id") == "publish_release")
        self.assertEqual(publish["with"]["files"], "release-public/*")
        public = next(step for step in steps if step.get("name") == "Select public packages and generate their checksums")
        self.assertIn("--merge-published", public["run"])
        updater = steps[-1]
        self.assertEqual(updater["with"]["tag_name"], "updater")
        self.assertEqual(updater["with"]["files"], "artifacts/*-v1-*-update.json")
        names = [step.get("name") for step in steps]
        verify = names.index("Verify both runtime mirrors before publication")
        for provider in ("HuggingFace", "ModelScope"):
            wheel = names.index(f"Publish immutable runtime wheels to {provider}")
            version = names.index(f"Upload release files to {provider} dataset")
            self.assertLess(wheel, verify)
            self.assertLess(verify, version)
            self.assertLess(version, steps.index(publish))
            self.assertIn("success()", steps[version]["if"])
        stage = next(step for step in steps if step.get("id") == "stage_mirrors")
        self.assertIn('for file in artifacts/*', stage["run"])
        self.assertIn('runtime-artifacts/*.json', stage["run"])
        self.assertNotIn("release-public/SHA256SUMS", stage["run"])

    def test_real_portable_archive_is_single_file_and_preserves_runtime_content(self):
        seven_zip = shutil.which("7z") or str(Path("C:/Program Files/7-Zip/7z.exe"))
        if not Path(seven_zip).is_file():
            self.skipTest("7-Zip is not installed")
        stage = self.root / "Pymss-Studio-1.2.3-windows-x64-rocm"
        stage.mkdir()
        body = os.urandom(16 * 1024)
        (stage / "Pymss Studio.exe").write_bytes(body)
        result = ASSETS.portable(stage, seven_zip)
        self.assertEqual([path.name for path in result], [stage.name + ".7z"])
        destination = self.root / "extracted"
        subprocess.run([seven_zip, "x", str(result[0]), "-o" + str(destination)], check=True, capture_output=True)
        self.assertEqual((destination / stage.name / "Pymss Studio.exe").read_bytes(), body)
        self.assertTrue((destination / stage.name / "pymss-studio.portable").is_file())
        self.assertEqual((stage / "Pymss Studio.exe").read_bytes(), body)
