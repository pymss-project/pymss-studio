"""Validate release inputs and derive the Windows runtime recipe before building."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from worker_runtime_profiles import profile_release_state, resolve_profile  # noqa: E402


NUMBER = r"(?:0|[1-9][0-9]*)"
IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER = re.compile(
    rf"{NUMBER}\.{NUMBER}\.{NUMBER}(?:-(?P<pre>{IDENTIFIER}(?:\.{IDENTIFIER})*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


def verify_release_tag(tag: str, commit: str) -> None:
    result = subprocess.run(
        ["git", "ls-remote", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
        cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True, text=True, timeout=30,
    )
    refs = dict(line.split()[::-1] for line in result.stdout.splitlines())
    existing = refs.get(f"refs/tags/{tag}^{{}}", refs.get(f"refs/tags/{tag}"))
    if existing is not None and existing != commit:
        raise ValueError("The release tag already points to a different commit")


def release_plan(manifest: dict, version: str, *, target: str = "all",
                 release_type: str = "", allow_auto_update: bool = False,
                 rocm_profile: str = "", rocm_preview_candidate: bool = False) -> dict:
    version = version.strip().removeprefix("v")
    match = SEMVER.fullmatch(version)
    if not match:
        raise ValueError("Release version must be a valid semantic version")
    if target not in {"all", "windows", "macos", "linux"}:
        raise ValueError("Unknown release build target")
    release_type = release_type or ("prerelease" if match["pre"] else "stable")
    if release_type not in {"build-only", "prerelease", "stable"}:
        raise ValueError("Unknown release type")
    if release_type == "stable" and match["pre"]:
        raise ValueError("A stable release cannot have a prerelease version")
    if release_type == "build-only" and allow_auto_update:
        raise ValueError("Automatic updates require publishing a release")

    profile = resolve_profile(manifest, "rocm", rocm_profile or None, platform_name="win32")
    state = profile_release_state(profile)
    if target in {"all", "windows"}:
        if rocm_preview_candidate:
            if (release_type != "prerelease" or not match["pre"]
                    or profile["id"] != "win-rocm100-multiarch" or state not in {"disabled", "preview"}):
                raise ValueError("The ROCm candidate requires an explicit prerelease version and preview recipe")
            state = "preview"
        elif state not in {"legacy", "preview", "stable"}:
            raise ValueError("The selected ROCm recipe is not enabled for release")
    elif rocm_preview_candidate:
        raise ValueError("The ROCm candidate requires a Windows build target")
    return {
        "version": version, "tag": f"v{version}", "target": target, "release_type": release_type,
        "publish": release_type != "build-only", "prerelease": release_type == "prerelease",
        "allow_auto_update": allow_auto_update,
        "rocm_profile": profile["id"], "rocm_revision": profile["revision"],
        "rocm_targets": "all" if profile["installMode"] == "multiarch" else "",
        "rocm_channel": state, "rocm_preview_candidate": rocm_preview_candidate,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-tag", nargs=2, metavar=("TAG", "COMMIT"))
    arguments = parser.parse_args()
    if arguments.verify_tag:
        verify_release_tag(*arguments.verify_tag)
        sys.exit(0)
    manifest = json.loads(Path("python/runtime-manifest.json").read_text(encoding="utf-8"))
    plan = release_plan(
        manifest, os.environ["RELEASE_VERSION_INPUT"],
        target=os.environ.get("RELEASE_TARGET_INPUT", "all"),
        release_type=os.environ.get("RELEASE_TYPE_INPUT", ""),
        allow_auto_update=os.environ.get("RELEASE_AUTO_UPDATE_INPUT", "false") == "true",
        rocm_profile=os.environ.get("RELEASE_ROCM_PROFILE_INPUT", ""),
        rocm_preview_candidate=os.environ.get("RELEASE_ROCM_PREVIEW_INPUT", "false") == "true",
    )
    if plan["publish"] and os.environ.get("RELEASE_COMMIT_INPUT"):
        verify_release_tag(plan["tag"], os.environ["RELEASE_COMMIT_INPUT"])
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for name, value in plan.items():
            text = value if isinstance(value, str) else json.dumps(value)
            if "\n" in text or "\r" in text:
                raise ValueError("Release plan values must not contain newlines")
            output.write(f"{name}={text}\n")
    print(json.dumps(plan, indent=2))
