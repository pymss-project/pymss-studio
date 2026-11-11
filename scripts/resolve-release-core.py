"""Resolve stable pymss and its declared core dependency, or verify build pins."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path

from pip._vendor.packaging.markers import default_environment
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


def resolve_wheel(requirement: Requirement, python_version: str) -> dict:
    # Resolve metadata only; accelerator dependencies remain owned by each
    # backend's runtime recipe, rather than the preflight host's platform.
    with tempfile.TemporaryDirectory(prefix="pymss-release-core-") as temporary:
        report_path = Path(temporary) / "report.json"
        subprocess.run([
            sys.executable, "-m", "pip", "--isolated", "install",
            "--dry-run", "--ignore-installed", "--no-deps", "--no-cache-dir",
            "--only-binary=:all:", "--disable-pip-version-check",
            "--index-url", "https://pypi.org/simple",
            "--python-version", python_version,
            "--report", str(report_path), str(requirement),
        ], check=True, timeout=120)
        report = json.loads(report_path.read_text(encoding="utf-8"))

    if report.get("version") != "1":
        raise ValueError("Unsupported release core resolution report")
    if len(report["install"]) != 1:
        raise ValueError("Release core resolution must return one package")
    item = report["install"][0]
    name = canonicalize_name(item["metadata"]["name"])
    version = Version(item["metadata"]["version"])
    if (
        name != canonicalize_name(requirement.name) or item.get("is_yanked")
        or version.is_prerelease or version.is_devrelease or version.local
        or not requirement.specifier.contains(version, prereleases=False)
    ):
        raise ValueError(f"Invalid stable release core: {name} {version}")
    return item["metadata"]


def core_dependencies(package: dict, extras: set[str], python_version: str):
    # A shared snapshot must satisfy core constraints on every supported host,
    # including dependencies activated by the requested package extras.
    for text in package.get("requires_dist", []):
        dependency = Requirement(text)
        if canonicalize_name(dependency.name) not in CORE_PACKAGES:
            continue
        if dependency.marker is not None:
            applicable = False
            for platform, system, machine in (
                ("win32", "Windows", "AMD64"), ("linux", "Linux", "x86_64"),
                ("darwin", "Darwin", "arm64"),
            ):
                environment = {**default_environment(), "python_version": python_version,
                               "python_full_version": python_version + ".0",
                               "sys_platform": platform, "platform_system": system,
                               "os_name": "nt" if platform == "win32" else "posix",
                               "platform_machine": machine}
                if any(dependency.marker.evaluate({**environment, "extra": extra})
                       for extra in ("", *extras)):
                    applicable = True
                    break
            if not applicable:
                continue
        if dependency.url:
            raise ValueError("Release core dependencies must use versioned index packages")
        yield dependency


def resolve_manifest(path: Path) -> dict[str, str]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    requirements = core_requirements(manifest)
    # Resolution deliberately treats source versions as minimum release floors.
    # Resolved snapshots are consumed with --verify, never resolved again.
    for requirement in requirements.values():
        bounds = list(requirement.specifier)
        if bounds:
            requirement.specifier = type(requirement.specifier)(f">={bounds[0].version}")

    python_version = manifest["python"]
    pymss = resolve_wheel(requirements["pymss"], python_version)
    core = requirements["pymss-core"]
    dependencies = [dependency for dependency in core_dependencies(
        pymss, requirements["pymss"].extras, python_version,
    ) if canonicalize_name(dependency.name) == "pymss-core"]
    if dependencies:
        core.specifier = type(core.specifier)()
        for dependency in dependencies:
            core.specifier &= dependency.specifier
    packages = {"pymss": pymss, "pymss-core": resolve_wheel(core, python_version)}
    versions = {name: str(Version(package["version"])) for name, package in packages.items()}
    for name, package in packages.items():
        for dependency in core_dependencies(package, requirements[name].extras, python_version):
            if not dependency.specifier.contains(versions[canonicalize_name(dependency.name)]):
                raise ValueError("The resolved release core packages are incompatible")

    pins = {}
    for name, version in versions.items():
        requirement = requirements[name]
        requirement.specifier = type(requirement.specifier)(f"=={version}")
        pins[name] = str(requirement)
    # Build reproducibility must not raise the runtime's compatibility floor.
    # Only packaging tools consume these pins; common remains runtime policy.
    manifest["releaseCore"] = {"requirements": pins}
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return versions


def verify_installed(path: Path) -> dict[str, str]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    versions = {}
    snapshot = {"common": manifest.get("releaseCore", {}).get("requirements", manifest["common"])}
    for name, requirement in core_requirements(snapshot, require_pins=True).items():
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
