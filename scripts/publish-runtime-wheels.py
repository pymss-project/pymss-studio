"""Publish immutable runtime wheels to the two existing distribution datasets."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.parse import quote
from urllib.request import Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from worker_runtime_artifacts import (  # noqa: E402
    _RuntimeRedirectHandler, load_artifact_lock, verify_artifact_file,
)

PROVIDERS = {
    "huggingface": ("HuanLin/Pymss-Studio-Dist", "main", "HF_TOKEN",
                    "https://huggingface.co/datasets/HuanLin/Pymss-Studio-Dist/resolve/main"),
    "modelscope": ("huanlinoto/Pymss-Studio-Dist", "master", "MODELSCOPE_API_TOKEN",
                   "https://www.modelscope.cn/datasets/huanlinoto/Pymss-Studio-Dist/resolve/master"),
}


class PublicationError(RuntimeError):
    pass


def locked_wheels(directory: Path, wheelhouse: Path) -> list[dict]:
    catalog = json.loads((directory / "catalog.json").read_text(encoding="utf-8-sig"))
    profiles = catalog.get("profiles")
    if (catalog.get("schemaVersion") != 1 or catalog.get("resolutionStatus") != "locked"
            or not isinstance(profiles, list) or not 0 < len(profiles) <= 128):
        raise PublicationError("Invalid runtime artifact catalog")
    result = {}
    seen = set()
    for entry in profiles:
        profile = entry.get("profileId")
        if not isinstance(profile, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,95}", profile) or profile in seen:
            raise PublicationError("Invalid or duplicate runtime profile identifier")
        seen.add(profile)
        lock = load_artifact_lock(directory / f"{profile}.json")
        if any(lock.get(key) != entry.get(key) for key in ("profileId", "profileRevision", "lockId")):
            raise PublicationError("Runtime catalog and artifact lock differ")
        for artifact in lock["artifacts"]:
            filename = artifact["filename"]
            if f"-1{artifact['sha256']}-" not in filename:
                raise PublicationError("Shared wheel filenames must contain their SHA-256")
            previous = result.get(filename)
            if previous and any(previous[key] != artifact[key] for key in ("sha256", "bytes")):
                raise PublicationError(f"Conflicting locked wheel identity: {filename}")
            result[filename] = artifact
    if {file.name for file in wheelhouse.glob("*.whl")} != set(result):
        raise PublicationError("The wheelhouse differs from the catalog's locked artifacts")
    for artifact in result.values():
        verify_artifact_file(wheelhouse / artifact["filename"], artifact)
    return sorted(result.values(), key=lambda item: item["filename"])


class Mirror:
    def __init__(self, provider: str, token: str):
        self.provider = provider
        self.repo, self.revision, _, self.root = PROVIDERS[provider]
        if provider == "huggingface":
            from huggingface_hub import HfApi
            self.api = HfApi(token=token)
        else:
            from modelscope_hub import HubApi
            self.api = HubApi(token=token)

    def files(self) -> dict[str, tuple[int, str | None]]:
        # Listing the repository root also works before wheels/ first exists.
        # Both SDKs handle the provider's pagination before returning all files.
        if self.provider == "huggingface":
            entries = self.api.list_repo_tree(self.repo, repo_type="dataset", revision=self.revision, recursive=True)
        else:
            entries = self.api.list_repo_files(self.repo, repo_type="dataset", revision=self.revision, recursive=True)
        result = {}
        for entry in entries:
            if not entry.path.startswith("wheels/") or not hasattr(entry, "size"):
                continue
            lfs = getattr(entry, "lfs", None)
            digest = getattr(lfs, "sha256", None) if self.provider == "huggingface" else getattr(entry, "sha256", None)
            result[entry.path] = (entry.size, digest)
        return result

    def upload(self, directory: Path) -> None:
        self.api.upload_folder(repo_id=self.repo, repo_type="dataset", revision=self.revision,
                               folder_path=str(directory), path_in_repo="wheels",
                               commit_message="Add verified runtime wheels")

    def small_file_hash(self, artifact: dict) -> str:
        # Regular Git blobs have a Git SHA-1, not an LFS SHA-256. Hash their
        # actual bytes; never compare these two different hash formats.
        if artifact["bytes"] > 1024 * 1024:
            raise PublicationError("Large remote wheel has no SHA-256 metadata")
        url = self.root + "/wheels/" + quote(artifact["filename"], safe="")
        opener = build_opener(_RuntimeRedirectHandler())
        with opener.open(Request(url, headers={"Accept-Encoding": "identity"}), timeout=30) as response:
            body = response.read(artifact["bytes"] + 1)
        if len(body) != artifact["bytes"]:
            raise PublicationError(f"Remote wheel size mismatch: {artifact['filename']}")
        return hashlib.sha256(body).hexdigest()


def missing_wheels(mirror: Mirror, artifacts: list[dict]) -> list[dict]:
    files = mirror.files()
    missing = []
    for artifact in artifacts:
        filename = artifact["filename"]
        record = files.get("wheels/" + filename)
        if record is None:
            missing.append(artifact)
            continue
        size, digest = record
        if size != artifact["bytes"]:
            raise PublicationError(f"Existing remote wheel size mismatch: {filename}")
        digest = digest or mirror.small_file_hash(artifact)
        if digest != artifact["sha256"]:
            raise PublicationError(f"Existing remote wheel SHA-256 mismatch: {filename}")
    return missing


def publish(mirror: Mirror, artifacts: list[dict], wheelhouse: Path, *, verify_only: bool = False) -> None:
    # Validate every existing object before starting any upload. A mismatch is
    # never repaired by overwriting an object used by a previously released lock.
    missing = missing_wheels(mirror, artifacts)
    if missing and verify_only:
        raise PublicationError(f"Runtime mirror is missing {len(missing)} locked wheel(s)")
    if missing:
        with tempfile.TemporaryDirectory(prefix="runtime-wheel-upload-", dir=wheelhouse.parent) as temporary:
            directory = Path(temporary)
            for artifact in missing:
                os.link(wheelhouse / artifact["filename"], directory / artifact["filename"])
            mirror.upload(directory)
        for attempt in range(3):
            if not missing_wheels(mirror, artifacts):
                break
            if attempt == 2:
                raise PublicationError("Runtime mirror is incomplete after upload")
            time.sleep(2 ** attempt)
    print(f"Verified {len(artifacts)} runtime wheel(s) on {mirror.provider}; uploaded {len(missing)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=PROVIDERS, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--locks", type=Path, required=True)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    token = os.environ.get(PROVIDERS[args.provider][2], "")
    if not token:
        parser.error("The selected mirror's publication token is required")
    try:
        artifacts = locked_wheels(args.locks, args.wheelhouse)
        publish(Mirror(args.provider, token), artifacts, args.wheelhouse, verify_only=args.verify_only)
    except PublicationError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        # SDK errors can contain signed URLs or request headers.
        print(f"Runtime wheel publication failed ({type(error).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
