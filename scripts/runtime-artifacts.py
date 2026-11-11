#!/usr/bin/env python3
"""Resolve, publish and consume the same ROCm binary closure in CI and Online.

Only the release resolver may turn the upstream ``rocm`` sdist into a wheel.
Its build environment has an explicit, hash-locked binary closure and disables
build isolation, so pip cannot silently fetch new build dependencies.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from pip._vendor.packaging.markers import default_environment  # noqa: E402
from pip._vendor.packaging.requirements import Requirement  # noqa: E402
from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename  # noqa: E402
from pip._vendor.packaging.version import Version  # noqa: E402
from worker_runtime_artifacts import (  # noqa: E402
    artifact_lock_id, build_locked_requirements, load_artifact_lock,
    resolve_artifact_plan, validate_artifact_lock,
    verify_installed_coverage,
)
from worker_runtime_profiles import profile_release_state, resolve_profile, runtime_install_plan  # noqa: E402

BUILD_REQUIREMENTS = ("setuptools==80.9.0", "wheel==0.45.1", "packaging==25.0")


def python_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    # The selected interpreter owns package discovery. Developer overlays and
    # Python homes inherited from a launcher must never affect a managed runtime.
    return {key: value for key, value in (os.environ if env is None else env).items()
            if not key.upper().startswith("PYTHON")}


def run(arguments: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(arguments, check=True, env=python_environment(env))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def file_identity(path: Path, url: str) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"url": url, "sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def source_identity(item: dict) -> dict:
    download = item.get("download_info") or {}
    parsed = urlsplit(str(download.get("url") or ""))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query:
        raise RuntimeError("The resolver returned a non-public or untrusted source URL")
    if parsed.hostname not in {"files.pythonhosted.org", "repo.radeon.com", "stable.repo.amd.com", "download.pytorch.org"}:
        raise RuntimeError("The resolver returned a source outside the release recipe authorities")
    digest = (download.get("archive_info") or {}).get("hashes", {}).get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise RuntimeError("The resolver source has no exact SHA-256")
    return {"url": urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), "sha256": digest}


def download_source(identity: dict, directory: Path) -> Path:
    filename = unquote(urlsplit(identity["url"]).path.rsplit("/", 1)[-1])
    if not filename or Path(filename).name != filename or "\\" in filename:
        raise RuntimeError("Invalid source artifact filename")
    path = directory / filename
    directory.mkdir(parents=True, exist_ok=True)
    # A cache hit is never authoritative: rehash every byte before using it.
    if path.is_file() and not path.is_symlink():
        actual = file_identity(path, identity["url"])
        if actual["sha256"] == identity["sha256"]:
            identity["bytes"] = actual["bytes"]
            return path
    partial = path.with_name(path.name + ".download")
    digest = hashlib.sha256()
    size = 0
    try:
        with urlopen(Request(identity["url"], headers={"User-Agent": "Pymss-Studio-Runtime-Lock/1"}), timeout=120) as response, partial.open("wb") as output:
            if urlsplit(response.url).scheme != "https":
                raise RuntimeError("Artifact download redirected to an insecure URL")
            for block in iter(lambda: response.read(1024 * 1024), b""):
                size += len(block)
                if size > 32 * 1024**3:
                    raise RuntimeError("Artifact exceeds the supported byte limit")
                digest.update(block)
                output.write(block)
        if digest.hexdigest() != identity["sha256"] or size <= 0:
            raise RuntimeError("Downloaded artifact does not match its resolver SHA-256")
        if "bytes" in identity and size != identity["bytes"]:
            raise RuntimeError("Downloaded artifact does not match its locked byte size")
        partial.replace(path)
        identity["bytes"] = size
        return path
    finally:
        partial.unlink(missing_ok=True)


def resolve_report(python: Path, requirements: list[str], report: Path, index: str | None = None) -> dict:
    args = [str(python), "-I", "-X", "utf8", "-m", "pip", "--isolated", "install", "--dry-run", "--ignore-installed",
            "--no-build-isolation", "--only-binary=:all:", "--no-binary=rocm", "--report", str(report)]
    if index:
        args += ["--extra-index-url", index]
    run([*args, *requirements])
    result = json.loads(report.read_text(encoding="utf-8"))
    if result.get("version") != "1" or not result.get("install"):
        raise RuntimeError("pip did not return a complete supported resolver report")
    return result


def _binary_record(path: Path, source: dict, provenance: dict, base_url: str) -> dict:
    name, version, _, tags = parse_wheel_filename(path.name)
    with zipfile.ZipFile(path) as archive:
        # Vendored libraries can include nested dist-info directories. Only
        # root metadata identifies the distribution published by this wheel.
        names = [entry for entry in archive.namelist()
                 if entry.count("/") == 1 and entry.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise RuntimeError(
                f"Wheel must contain exactly one top-level distribution metadata record: {path.name} (found {len(names)})"
            )
        metadata = BytesParser().parsebytes(archive.read(names[0]))
    if canonicalize_name(metadata["Name"]) != name or metadata["Version"] != str(version):
        raise RuntimeError("Wheel metadata differs from its filename")
    environment = default_environment()
    environment["extra"] = ""
    dependencies = []
    for text in metadata.get_all("Requires-Dist", []):
        requirement = Requirement(text)
        if requirement.marker is None or requirement.marker.evaluate(environment):
            dependencies.append(canonicalize_name(requirement.name))
    return {"name": name, "version": str(version), "filename": path.name,
            **file_identity(path, base_url.rstrip("/") + "/" + quote(path.name)),
            "tags": sorted(str(tag) for tag in tags), "role": "host", "targets": [],
            "dependencies": sorted(set(dependencies)), "provenance": provenance}


def build_environment(directory: Path, cache: Path) -> tuple[Path, list[dict]]:
    report = resolve_report(Path(sys.executable), list(BUILD_REQUIREMENTS), directory / "build-inputs.json")
    inputs = []
    paths = []
    for item in report["install"]:
        identity = source_identity(item)
        path = download_source(identity, cache)
        if path.suffix != ".whl":
            raise RuntimeError("Build dependencies must be available as binary wheels")
        name, version, _, _ = parse_wheel_filename(path.name)
        inputs.append({"name": name, "version": str(version), "filename": path.name, **identity})
        paths.append(path)
    environment = directory / "builder"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    requirements = directory / "build-inputs.txt"
    requirements.write_text("".join(f"{p.as_uri()} --hash=sha256:{i['sha256']}\n" for p, i in zip(paths, inputs)), encoding="utf-8")
    run([str(python), "-I", "-X", "utf8", "-m", "pip", "--isolated", "install", "--no-index", "--no-deps", "--require-hashes", "-r", str(requirements)])
    return python, sorted(inputs, key=lambda item: item["name"])


def materialize_item(item: dict, python: Path, build_inputs: list[dict], output: Path,
                     cache: Path, base_url: str) -> dict:
    source = source_identity(item)
    path = download_source(source, cache)
    if path.suffix == ".whl":
        target = output / path.name
        shutil.copyfile(path, target)
        provenance = {"kind": "upstream-wheel", "source": source}
    else:
        if canonicalize_name(str(item["metadata"]["name"])) != "rocm":
            raise RuntimeError("Only the explicitly approved ROCm meta-package may be built from source")
        with tempfile.TemporaryDirectory(prefix="rocm-wheel-", dir=output.parent) as temporary:
            build_env = {**os.environ, "SOURCE_DATE_EPOCH": "315532800"}
            run([str(python), "-I", "-X", "utf8", "-m", "pip", "--isolated", "wheel", "--no-cache-dir", "--no-index", "--no-deps",
                 "--no-build-isolation", "--wheel-dir", temporary, str(path)], env=build_env)
            wheels = list(Path(temporary).glob("*.whl"))
            if len(wheels) != 1:
                raise RuntimeError("The ROCm source build must produce exactly one wheel")
            target = output / wheels[0].name
            shutil.copyfile(wheels[0], target)
        provenance = {"kind": "derived-wheel", "source": source,
                      "builder": "pip wheel --no-index --no-deps --no-build-isolation",
                      "buildInputs": build_inputs, "pythonVersion": platform.python_version(),
                      "sourceDateEpoch": 315532800}
    # A wheel build tag carries the content identity. Re-running an old release
    # cannot replace a previously bundled lock's download URL with other bytes.
    # Renaming the archive leaves its package metadata and wheel RECORD intact.
    identity = file_identity(target, "")
    name, version, _, _ = parse_wheel_filename(target.name)
    suffix = "-".join(target.stem.rsplit("-", 3)[-3:])
    published_name = f"{name.replace('-', '_')}-{version}-1{identity['sha256']}-{suffix}.whl"
    published = output / published_name
    target.replace(published)
    target = published
    record = _binary_record(target, source, provenance, base_url)
    if record["name"] != canonicalize_name(item["metadata"]["name"]) or record["version"] != item["metadata"]["version"]:
        raise RuntimeError("The released wheel differs from the resolved distribution")
    return record


def requirements_for(manifest: dict, plan: dict, *, host: bool = False) -> list[str]:
    torch = plan["torch"]
    roots = [*torch.get("rocmRequirements", []), *torch.get("requirements", [torch.get("requirement")])]
    roots = [root for root in roots if root]
    if host and plan["installMode"] == "multiarch":
        roots = [re.sub(r"\[device-[^\]]+\]", "", root) for root in roots]
    # Resolve common/kernel packages in the same transaction as Torch. Installing
    # them later from floating requirements would make Online and Offline differ.
    common = {**manifest["common"], **manifest.get("releaseCore", {}).get("requirements", {})}
    return [*roots, *common.values()]


def distribution_manifest(manifest: dict, profile_id: str, revision: int, channel: str,
                          version: str, *, enable_preview_candidate: bool = False) -> dict:
    """Release-author operation; runtime clients cannot request this override."""
    snapshot = json.loads(json.dumps(manifest))
    # Consumers such as PowerShell read the snapshot as data, without the Python
    # compatibility helper. Materialize legacy defaults for every bundled recipe.
    profiles = snapshot["backends"]["rocm"]["platformOverrides"]["win32"]["profiles"]
    for recipe in profiles.values():
        recipe["releaseState"] = profile_release_state(recipe)
    profile = resolve_profile(snapshot, "rocm", profile_id, platform_name="win32")
    if profile["revision"] != revision:
        raise ValueError("The requested release profile revision differs from the recipe")
    state = profile.get("releaseState", "legacy" if profile.get("installationEnabled") else "disabled")
    if enable_preview_candidate:
        if channel != "preview" or not re.fullmatch(r"\d+\.\d+\.\d+-[0-9A-Za-z][0-9A-Za-z.-]*(?:\+[0-9A-Za-z][0-9A-Za-z.-]*)?", version):
            raise ValueError("A preview recipe snapshot requires an explicit prerelease version and preview channel")
        if state not in ("disabled", "preview") or profile_id != "win-rocm100-multiarch":
            raise ValueError("Only the versioned ROCm candidate may be enabled in a preview snapshot")
        selected = snapshot["backends"]["rocm"]["platformOverrides"]["win32"]["profiles"][profile_id]
        selected.update({"releaseState": "preview", "installationEnabled": True, "validationStatus": "preview"})
    elif state != channel or not profile.get("installationEnabled"):
        raise ValueError("The release channel does not match the enabled versioned recipe")
    snapshot["distribution"] = {"schemaVersion": 1, "version": version, "profileId": profile_id,
                                "profileRevision": revision, "channel": channel,
                                "sourceManifestSha256": hashlib.sha256(json.dumps(manifest, sort_keys=True,
                                    separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()}
    return snapshot


def snapshot_command(args: argparse.Namespace) -> None:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    value = distribution_manifest(manifest, args.profile, args.profile_revision, args.channel,
                                  args.version, enable_preview_candidate=args.enable_preview_candidate)
    write_json(args.output, value)


def resolve_command(args: argparse.Namespace) -> None:
    if sys.platform != "win32" or platform.machine().lower() not in ("amd64", "x86_64"):
        raise RuntimeError("Windows ROCm release locks must be resolved on Windows x64")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    profile = resolve_profile(manifest, "rocm", args.profile, platform_name="win32")
    if profile["revision"] != args.profile_revision:
        raise RuntimeError("The release profile revision differs from the versioned recipe")
    release_state = profile.get("releaseState", "legacy" if profile.get("installationEnabled") else "disabled")
    if not profile.get("installationEnabled") or release_state in ("disabled", "revoked"):
        raise RuntimeError("The selected profile is disabled for release installation")
    if args.channel != release_state:
        raise RuntimeError("The requested release channel does not match the versioned recipe")
    targets = sorted(set(args.targets))
    if profile["installMode"] != "multiarch" and targets:
        raise RuntimeError("Legacy ROCm recipes do not support independently selected device targets")
    if targets == ["all"]:
        targets = sorted(target for target in profile.get("availableTargets", []) if target != "all")
    if profile["installMode"] == "multiarch" and not targets:
        raise RuntimeError("Multi-architecture releases require an explicit target set")
    if args.channel == "stable" and not set(targets) <= set(profile.get("validatedTargets", [])):
        raise RuntimeError("Stable package targets must have Studio hardware/model validation")
    selection = targets or None
    plan = runtime_install_plan(manifest, "rocm", args.profile, selection, platform_name="win32",
                                machine_name=platform.machine(), python_version=platform.python_version())
    args.output.mkdir(parents=True, exist_ok=True)
    wheels = args.output / "wheels"
    wheels.mkdir(exist_ok=True)
    cache = args.cache.resolve()
    with tempfile.TemporaryDirectory(prefix="runtime-resolve-", dir=args.output) as temporary:
        work = Path(temporary)
        python, build_inputs = build_environment(work, cache)
        host = resolve_report(python, requirements_for(manifest, plan, host=True), work / "host.json", plan["torch"].get("indexUrl"))
        reports = {None: host}
        for target in targets:
            target_plan = runtime_install_plan(manifest, "rocm", args.profile, [target], platform_name="win32")
            reports[target] = resolve_report(python, requirements_for(manifest, target_plan), work / f"{target}.json", plan["torch"].get("indexUrl"))
        records = {}
        closures = {}
        host_names = sorted(canonicalize_name(i["metadata"]["name"]) for i in host["install"])
        for target, report in reports.items():
            names = []
            for item in report["install"]:
                name = canonicalize_name(item["metadata"]["name"])
                identity = source_identity(item)
                if name not in records:
                    records[name] = materialize_item(item, python, build_inputs, wheels, cache, args.base_url)
                elif records[name]["version"] != item["metadata"]["version"] or records[name]["provenance"]["source"]["sha256"] != identity["sha256"]:
                    raise RuntimeError("Target resolution changes an existing host/device binary: " + name)
                names.append(name)
            if target is not None:
                if not set(host_names) <= set(names):
                    raise RuntimeError("A target resolution removed host dependencies")
                closures[target] = sorted(set(names) - set(host_names))
        for name, record in records.items():
            record["targets"] = sorted(t for t in targets if name in closures[t])
            record["role"] = "device" if record["targets"] else "host"
        for name, expected in profile["packages"].items():
            artifact = records.get(canonicalize_name(name))
            if artifact is None or Version(artifact["version"]) != Version(expected):
                raise RuntimeError("The resolved binary does not match the recipe package identity: " + name)
        if profile["installMode"] == "multiarch" and any(not closures[target] for target in targets):
            raise RuntimeError("The upstream recipe did not resolve independent device binary closures")
        lock = {"schemaVersion": 1, "backend": "rocm", "profileId": profile["id"],
                "profileRevision": profile["revision"], "platform": "win32", "machine": "amd64",
                "python": profile["python"], "pythonAbi": "cp" + profile["python"].replace(".", ""),
                "platformTag": "win_amd64", "channel": args.channel, "targets": targets,
                "hostArtifacts": host_names, "targetArtifacts": closures,
                "artifacts": sorted(records.values(), key=lambda item: item["name"])}
        lock["lockId"] = artifact_lock_id(lock)
        validate_artifact_lock(lock)
        if any(item["bytes"] >= 2 * 1024**3 for item in lock["artifacts"]):
            raise RuntimeError("A wheel exceeds the release asset limit; an authenticated binary mirror is required")
        write_json(args.output / f"{profile['id']}.json", lock)
        catalog_profiles = []
        for path in sorted(args.output.glob("*.json")):
            if path.name in ("catalog.json", "build-manifest.json"):
                continue
            published = load_artifact_lock(path)
            catalog_profiles.append({"profileId": published["profileId"], "profileRevision": published["profileRevision"],
                                     "lockId": published["lockId"], "filename": path.name,
                                     "channel": published["channel"], "targets": published["targets"]})
        write_json(args.output / "catalog.json", {"schemaVersion": 1, "resolutionStatus": "locked", "profiles": catalog_profiles})
        write_json(args.output / "build-manifest.json", {"schemaVersion": 1, "profiles": catalog_profiles,
                   "builtAt": datetime.now(timezone.utc).isoformat(), "hardwareValidation": "not_run"})


def selected_plan(args: argparse.Namespace) -> dict:
    lock = load_artifact_lock(args.lock)
    return resolve_artifact_plan(lock, args.profile or lock["profileId"], args.targets,
                                 profile_revision=args.profile_revision,
                                 python_abi="cp" + ".".join(platform.python_version_tuple()[:2]).replace(".", ""),
                                 platform_tag="win_amd64")


def install_command(args: argparse.Namespace) -> None:
    plan = selected_plan(args)
    text = build_locked_requirements(plan, wheelhouse=args.wheelhouse)
    with tempfile.TemporaryDirectory(prefix="locked-runtime-") as temporary:
        requirements = Path(temporary) / "requirements.txt"
        requirements.write_text(text, encoding="utf-8")
        run([str(args.python), "-I", "-X", "utf8", "-m", "pip", "--isolated", "install", "--no-index", "--no-deps", "--force-reinstall",
             "--only-binary=:all:", "--require-hashes", "-r", str(requirements)])
    run([str(args.python), "-I", "-X", "utf8", "-m", "pip", "--isolated", "check"])
    verify_command(args)


def detector_command(args: argparse.Namespace) -> None:
    plan = selected_plan(args)
    records = {item["name"]: item for item in plan["artifacts"]}
    names = {name for name, item in records.items() if name.startswith("rocm") and item["role"] == "host"}
    if not names:
        raise RuntimeError("The locked profile has no host SDK distributions for the HIP detector")
    while True:
        expanded = names | {dependency for name in names for dependency in records[name]["dependencies"]}
        if expanded == names:
            break
        names = expanded
    text = build_locked_requirements(plan, artifacts=[records[name] for name in sorted(names)], wheelhouse=args.wheelhouse)
    with tempfile.TemporaryDirectory(prefix="locked-rocm-detector-") as temporary:
        requirements = Path(temporary) / "requirements.txt"
        requirements.write_text(text, encoding="utf-8")
        run([str(args.python), "-I", "-X", "utf8", "-m", "pip", "--isolated", "install", "--no-index", "--no-deps",
             "--only-binary=:all:", "--require-hashes", "-r", str(requirements)])
    run([str(args.python), "-I", "-X", "utf8", "-m", "pip", "--isolated", "check"])


def verify_command(args: argparse.Namespace) -> None:
    plan = selected_plan(args)
    result = subprocess.run([str(args.python), "-I", "-X", "utf8", "-B", "-c",
        "import json; from importlib import metadata; print(json.dumps({d.metadata['Name']: d.version for d in metadata.distributions() if d.metadata['Name']}))"],
        check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", env=python_environment())
    coverage = verify_installed_coverage(plan, json.loads(result.stdout))
    print(json.dumps({"artifactLockId": plan["lockId"], "hostLockId": plan["hostLockId"], **coverage}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--manifest", type=Path, default=ROOT / "python/runtime-manifest.json")
    snapshot.add_argument("--profile", required=True)
    snapshot.add_argument("--profile-revision", type=int, required=True)
    snapshot.add_argument("--channel", choices=("legacy", "preview", "stable"), required=True)
    snapshot.add_argument("--version", required=True)
    snapshot.add_argument("--enable-preview-candidate", action="store_true")
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.set_defaults(handler=snapshot_command)
    resolver = sub.add_parser("resolve")
    resolver.add_argument("--manifest", type=Path, default=ROOT / "python/runtime-manifest.json")
    resolver.add_argument("--profile", required=True)
    resolver.add_argument("--profile-revision", type=int, required=True)
    resolver.add_argument("--targets", nargs="*", default=[])
    resolver.add_argument("--channel", choices=("legacy", "preview", "stable"), required=True)
    resolver.add_argument("--base-url", required=True, help="Versioned release asset directory")
    resolver.add_argument("--output", type=Path, required=True)
    resolver.add_argument("--cache", type=Path, default=ROOT / ".cache/runtime-artifact-sources")
    resolver.set_defaults(handler=resolve_command)
    for command, handler in (("install", install_command), ("verify", verify_command), ("detector", detector_command)):
        consumer = sub.add_parser(command)
        consumer.add_argument("--lock", type=Path, required=True)
        consumer.add_argument("--profile")
        consumer.add_argument("--profile-revision", type=int)
        consumer.add_argument("--targets", nargs="*", default=[])
        consumer.add_argument("--python", type=Path, required=True)
        consumer.add_argument("--wheelhouse", type=Path)
        consumer.set_defaults(handler=handler)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
