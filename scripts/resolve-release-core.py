"""Resolve stable release cores and verify their installed versions."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path

from pip._vendor.packaging.requirements import Requirement
from pip._vendor.packaging.utils import canonicalize_name
from pip._vendor.packaging.version import Version


CORE_PACKAGES = ("pymss", "pymss-core")


def core_requirements(manifest: dict, *, require_pins: bool = False) -> dict[str, Requirement]:
    requirements = {}
    for name in CORE_PACKAGES:
        requirement = Requirement(manifest["common"][name])
        bounds = list(requirement.specifier)
        if (
            canonicalize_name(requirement.name) != name
            or requirement.url or requirement.marker
            or (bounds and (len(bounds) != 1 or bounds[0].operator not in {"==", ">="} or "*" in bounds[0].version))
            or (require_pins and (not bounds or bounds[0].operator != "=="))
        ):
            raise ValueError(
                "Release core must be unversioned or have one exact version or minimum version; "
                f"bundle verification requires an exact version: {name}"
            )
        requirements[name] = requirement
    return requirements


def resolve_manifest(path: Path) -> dict[str, str]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    requirements = core_requirements(manifest)
    candidates = []
    baselines = {}
    for name, requirement in requirements.items():
        bounds = list(requirement.specifier)
        if bounds:
            baselines[name] = Version(bounds[0].version)
            requirement.specifier = type(requirement.specifier)(f">={baselines[name]}")
        candidates.append(str(requirement))

    # Resolve only the two core wheels. The runtime builder installs their full
    # dependencies afterwards and fails if the resolved pair is incompatible.
    with tempfile.TemporaryDirectory(prefix="pymss-release-core-") as temporary:
        report_path = Path(temporary) / "report.json"
        subprocess.run([
            sys.executable, "-m", "pip", "--isolated", "install",
            "--dry-run", "--ignore-installed", "--no-deps", "--no-cache-dir",
            "--only-binary=:all:", "--disable-pip-version-check",
            "--index-url", "https://pypi.org/simple",
            "--python-version", manifest["python"],
            "--report", str(report_path), *candidates,
        ], check=True, timeout=120)
        report = json.loads(report_path.read_text(encoding="utf-8"))

    if report.get("version") != "1":
        raise ValueError("Unsupported release core resolution report")
    versions = {}
    for item in report["install"]:
        name = canonicalize_name(item["metadata"]["name"])
        version = Version(item["metadata"]["version"])
        if (
            name not in requirements or name in versions or item.get("is_yanked")
            or version.is_prerelease or version.is_devrelease or version.local
            or (name in baselines and version < baselines[name])
        ):
            raise ValueError(f"Invalid stable release core: {name} {version}")
        versions[name] = str(version)
    if set(versions) != set(CORE_PACKAGES):
        raise ValueError("Release core resolution did not return both packages")

    for name, version in versions.items():
        requirement = requirements[name]
        requirement.specifier = type(requirement.specifier)(f"=={version}")
        manifest["common"][name] = str(requirement)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return versions


def verify_installed(path: Path) -> dict[str, str]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    versions = {}
    for name, requirement in core_requirements(manifest, require_pins=True).items():
        installed = metadata.version(name)
        if not requirement.specifier.contains(installed, prereleases=True):
            raise ValueError(f"Bundled {name} {installed} does not match {requirement}")
        versions[name] = installed
    return versions


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--verify", action="store_true")
    arguments = parser.parse_args()
    versions = verify_installed(arguments.manifest) if arguments.verify else resolve_manifest(arguments.manifest)
    print("Release core versions: " + json.dumps(versions, sort_keys=True))
