from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from . import _bootstrap  # noqa: F401
from worker_runtime_artifacts import artifact_lock_id

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("publish_runtime_wheels", ROOT / "scripts/publish-runtime-wheels.py")
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


class RuntimeWheelPublicationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(publication.time, "sleep"))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        body = b"immutable runtime wheel"
        digest = hashlib.sha256(body).hexdigest()
        filename = f"fixture-1.0-1{digest}-py3-none-any.whl"
        self.artifact = {"name": "fixture", "version": "1.0", "filename": filename,
                         "url": publication.PROVIDERS["modelscope"][3] + "/wheels/" + filename,
                         "bytes": len(body), "sha256": digest, "tags": ["py3-none-any"],
                         "role": "host", "targets": [], "dependencies": [],
                         "provenance": {"kind": "upstream-wheel", "source": {
                             "url": "https://example.invalid/fixture.whl", "bytes": len(body), "sha256": digest}}}
        self.lock = {"schemaVersion": 1, "backend": "rocm", "profileId": "fixture", "profileRevision": 1,
                     "platform": "win32", "machine": "amd64", "python": "3.12", "pythonAbi": "cp312",
                     "platformTag": "win_amd64", "channel": "stable", "targets": [],
                     "hostArtifacts": ["fixture"], "targetArtifacts": {}, "artifacts": [self.artifact]}
        self.seal()
        self.path = self.root / filename
        self.path.write_bytes(body)
        self.mirror = mock.Mock(provider="huggingface")
        self.present = {"wheels/" + filename: (len(body), digest)}
        self.mirror.files.return_value = self.present

    def seal(self):
        self.lock["lockId"] = artifact_lock_id(self.lock)
        (self.root / "fixture.json").write_text(json.dumps(self.lock), encoding="utf-8")
        (self.root / "catalog.json").write_text(json.dumps({"schemaVersion": 1, "resolutionStatus": "locked",
            "profiles": [{key: self.lock[key] for key in ("profileId", "profileRevision", "lockId")}]}), encoding="utf-8")

    def test_catalog_verifies_every_locked_local_wheel(self):
        self.assertEqual(publication.locked_wheels(self.root, self.root), [self.artifact])
        self.path.write_bytes(b"x" * self.artifact["bytes"])
        with self.assertRaisesRegex(RuntimeError, "SHA-256"):
            publication.locked_wheels(self.root, self.root)

    def test_unreferenced_and_missing_wheels_are_rejected(self):
        extra = self.root / "unreferenced.whl"
        extra.touch()
        with self.assertRaisesRegex(publication.PublicationError, "wheelhouse"):
            publication.locked_wheels(self.root, self.root)
        extra.unlink()
        self.path.unlink()
        with self.assertRaisesRegex(publication.PublicationError, "wheelhouse"):
            publication.locked_wheels(self.root, self.root)

    def test_catalog_identity_and_duplicate_profiles_are_rejected(self):
        path = self.root / "catalog.json"
        catalog = json.loads(path.read_text())
        catalog["profiles"][0]["lockId"] = "a" * 64
        path.write_text(json.dumps(catalog))
        with self.assertRaisesRegex(publication.PublicationError, "catalog and artifact lock"):
            publication.locked_wheels(self.root, self.root)
        self.seal()
        catalog = json.loads(path.read_text())
        catalog["profiles"] *= 2
        path.write_text(json.dumps(catalog))
        with self.assertRaisesRegex(publication.PublicationError, "duplicate"):
            publication.locked_wheels(self.root, self.root)

    def test_shared_names_without_content_hash_are_rejected(self):
        self.artifact["filename"] = "fixture-1.0-py3-none-any.whl"
        self.artifact["url"] = publication.PROVIDERS["modelscope"][3] + "/wheels/" + self.artifact["filename"]
        self.seal()
        with self.assertRaisesRegex(publication.PublicationError, "filenames must contain"):
            publication.locked_wheels(self.root, self.root)

    def test_existing_verified_objects_are_reused_without_upload(self):
        publication.publish(self.mirror, [self.artifact], self.root)
        self.mirror.upload.assert_not_called()
        self.mirror.small_file_hash.assert_not_called()

    def test_missing_objects_only_are_uploaded_and_rechecked(self):
        other = {**self.artifact, "filename": "already-present.whl"}
        present = {"wheels/already-present.whl": self.present["wheels/" + self.path.name]}
        self.mirror.files.side_effect = [present, {**present, **self.present}]
        def upload(directory):
            self.assertEqual([p.name for p in directory.iterdir()], [self.path.name])
            self.assertEqual((directory / self.path.name).read_bytes(), self.path.read_bytes())
        self.mirror.upload.side_effect = upload
        publication.publish(self.mirror, [other, self.artifact], self.root)
        self.mirror.upload.assert_called_once()
        self.assertEqual(self.mirror.files.call_count, 2)
        self.assertFalse(self.mirror.upload.call_args.args[0].exists())

    def test_any_existing_mismatch_prevents_all_uploads(self):
        missing = {**self.artifact, "filename": "missing.whl"}
        for size, digest in ((self.artifact["bytes"] + 1, self.artifact["sha256"]),
                             (self.artifact["bytes"], "a" * 64)):
            with self.subTest(size=size, digest=digest):
                self.mirror.files.return_value = {"wheels/" + self.path.name: (size, digest)}
                with self.assertRaises(publication.PublicationError):
                    publication.publish(self.mirror, [missing, self.artifact], self.root)
                self.mirror.upload.assert_not_called()

    def test_partial_upload_cannot_pass_verification(self):
        self.mirror.files.return_value = {}
        with self.assertRaisesRegex(publication.PublicationError, "incomplete after upload"):
            publication.publish(self.mirror, [self.artifact], self.root)

    def test_short_metadata_visibility_delay_does_not_reupload_objects(self):
        self.mirror.files.side_effect = [{}, {}, self.present]
        publication.publish(self.mirror, [self.artifact], self.root)
        self.mirror.upload.assert_called_once()
        publication.time.sleep.assert_called_once_with(1)

    def test_conflicting_profiles_cannot_share_a_wheel_filename(self):
        other = json.loads(json.dumps(self.lock))
        other["profileId"] = "other"
        other["artifacts"][0]["bytes"] += 1
        other["artifacts"][0]["provenance"]["source"]["bytes"] += 1
        other["lockId"] = artifact_lock_id(other)
        (self.root / "other.json").write_text(json.dumps(other))
        catalog_path = self.root / "catalog.json"
        catalog = json.loads(catalog_path.read_text())
        catalog["profiles"].append({key: other[key] for key in ("profileId", "profileRevision", "lockId")})
        catalog_path.write_text(json.dumps(catalog))
        with self.assertRaisesRegex(publication.PublicationError, "Conflicting locked wheel identity"):
            publication.locked_wheels(self.root, self.root)

    def test_verify_only_never_attempts_to_repair_missing_objects(self):
        self.mirror.files.return_value = {}
        with self.assertRaisesRegex(publication.PublicationError, "missing"):
            publication.publish(self.mirror, [self.artifact], self.root, verify_only=True)
        self.mirror.upload.assert_not_called()

    def test_non_lfs_object_is_verified_from_actual_content(self):
        self.mirror.files.return_value = {"wheels/" + self.path.name: (self.artifact["bytes"], None)}
        self.mirror.small_file_hash.return_value = self.artifact["sha256"]
        publication.publish(self.mirror, [self.artifact], self.root)
        self.mirror.small_file_hash.assert_called_once_with(self.artifact)

    def test_sdk_errors_fail_closed_without_upload(self):
        self.mirror.files.side_effect = TimeoutError("request failed")
        with self.assertRaises(TimeoutError):
            publication.publish(self.mirror, [self.artifact], self.root)
        self.mirror.upload.assert_not_called()

    def test_sdk_listing_and_upload_contracts(self):
        for provider, module, constructor in (("huggingface", "huggingface_hub", "HfApi"),
                                              ("modelscope", "modelscope_hub", "HubApi")):
            with self.subTest(provider=provider):
                api = mock.Mock()
                entry = SimpleNamespace(path="wheels/file.whl", size=23, sha256="digest",
                                        lfs=SimpleNamespace(sha256="digest"))
                api.list_repo_tree.return_value = iter([entry])
                api.list_repo_files.return_value = [entry]
                with mock.patch.dict(sys.modules, {module: SimpleNamespace(**{constructor: lambda **_: api})}):
                    mirror = publication.Mirror(provider, "token")
                self.assertEqual(mirror.files(), {"wheels/file.whl": (23, "digest")})
                mirror.upload(self.root)
                self.assertEqual(api.upload_folder.call_args.kwargs["path_in_repo"], "wheels")
                self.assertEqual(api.upload_folder.call_args.kwargs["repo_type"], "dataset")

    def test_small_blob_size_is_bounded_and_actual_digest_checked(self):
        mirror = publication.Mirror.__new__(publication.Mirror)
        mirror.root = publication.PROVIDERS["huggingface"][3]
        opener = mock.Mock()
        with mock.patch.object(publication, "build_opener", return_value=opener):
            opener.open.return_value = io.BytesIO(self.path.read_bytes())
            self.assertEqual(mirror.small_file_hash(self.artifact), self.artifact["sha256"])
            opener.open.return_value = io.BytesIO(self.path.read_bytes() + b"extra")
            with self.assertRaisesRegex(publication.PublicationError, "size mismatch"):
                mirror.small_file_hash(self.artifact)
            with self.assertRaisesRegex(publication.PublicationError, "no SHA-256 metadata"):
                mirror.small_file_hash({**self.artifact, "bytes": 1024 * 1024 + 1})
