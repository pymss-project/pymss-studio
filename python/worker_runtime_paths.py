"""Discover relocatable ROCm paths using the selected environment, without Torch.

The SDK package table and tool locations were checked against AMD's 7.2.1 sdist
and TheRock's Python packaging templates. Unlike ``rocm-sdk path --bin``, this
query does not expand devel archives or select a GPU on the build machine.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

# Packaging invokes this file with -I; only this tool needs its bundled helpers.
# The SDK query below still runs in a separate isolated interpreter.
if __name__ == "__main__" and sys.flags.isolated:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from worker_protocol import _redact_sensitive_text  # noqa: E402


_QUERY = r'''
import importlib.util, json, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
prefix = Path(sys.prefix).resolve()

def fail(message, **details):
    context = {**details, 'selectedEnvironment': str(root), 'sysPrefix': str(prefix),
               'pythonExecutable': sys.executable}
    raise RuntimeError(message + '; queryContext=' + json.dumps(context, ensure_ascii=False))

if prefix != root:
    # Rust supplies an extended-length Windows path, while the venv launcher
    # can report the same directory without that prefix.
    try:
        same_environment = sys.platform == 'win32' and root.samefile(prefix)
    except OSError:
        same_environment = False
    if not same_environment:
        fail('ROCm SDK query interpreter is outside the selected environment')

def path_in_environment(origin):
    resolved = Path(origin).resolve()
    for base in (root, prefix):
        if resolved.is_relative_to(base):
            return root / resolved.relative_to(base)
    return None

spec = importlib.util.find_spec('rocm_sdk')
result = {'modules': [], 'sdkAvailable': spec is not None, 'querySupported': False}
if spec is not None:
    if not spec.origin:
        fail('ROCm SDK module has no file origin', module='rocm_sdk', origin=spec.origin,
             searchLocations=list(spec.submodule_search_locations or [])[:8])
    if path_in_environment(spec.origin) is None:
        fail('ROCm SDK module is outside the selected environment', module='rocm_sdk', origin=spec.origin)
    try:
        from rocm_sdk import _dist_info as di
    except ImportError:
        di = None
    supported = di is not None and hasattr(di, 'ALL_PACKAGES') and hasattr(di, 'AVAILABLE_TARGET_FAMILIES')
    result['querySupported'] = supported
    for package in di.ALL_PACKAGES.values() if supported else []:
        families = di.AVAILABLE_TARGET_FAMILIES if package.is_target_specific else [None]
        for family in families:
            name = package.get_py_package_name(family)
            found = importlib.util.find_spec(name)
            if found is not None and found.origin:
                module_file = path_in_environment(found.origin)
                if module_file is None:
                    fail('ROCm SDK package is outside the selected environment', module=name, origin=found.origin)
                result['modules'].append({'name': name, 'root': str(module_file.parent)})
print(json.dumps(result))
'''


def _relative_directory(root: Path, value: Path) -> str:
    resolved = value.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError("Native runtime path is outside the selected environment")
    if not resolved.is_dir():
        raise ValueError("Native runtime path does not exist")
    return resolved.relative_to(root).as_posix()


def resolve_rocm_native_paths(env_root: Path, report: dict[str, Any]) -> dict[str, list[str]]:
    """Validate persisted relative paths before constructing a process environment."""
    root = Path(env_root).resolve()
    result = {}
    for field in ("nativeLibraryDirs", "nativeToolDirs"):
        values = report.get(field, [])
        if not isinstance(values, list):
            raise ValueError(f"Invalid native runtime path list: {field}")
        paths = []
        for value in values:
            if (not isinstance(value, str) or not value or "\\" in value
                    or PureWindowsPath(value).drive or PureWindowsPath(value).is_absolute()
                    or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts):
                raise ValueError("Native runtime paths must be environment-relative paths")
            relative = _relative_directory(root, root / value)
            absolute = str(root / relative)
            if absolute not in paths:
                paths.append(absolute)
        result[field] = paths
    return result


def discover_rocm_native_paths(
    env_root: Path, python_exe: Path | None = None, *, timeout: float = 15.0,
) -> dict[str, Any]:
    """Return existing SDK directories relative to this environment only.

    The target interpreter queries AMD's own package table, including its version
    nonce and installed device families. Old SDKs without this query retain the
    existing ``_rocm_sdk*`` layout fallback. No global interpreter or PATH lookup
    is used. Invalid/foreign query results fail closed rather than using fallback.
    """
    root = Path(env_root).resolve()
    if python_exe is None:
        candidates = (root / "Scripts/python.exe", root / "python.exe", root / "bin/python")
        python_exe = next((path for path in candidates if path.is_file()), None)
    if python_exe is None:
        raise FileNotFoundError("The selected environment has no Python interpreter")
    environment = {key: value for key, value in os.environ.items()
                   if key not in ("PYTHONPATH", "PYTHONHOME")}
    query = subprocess.run(
        [str(python_exe), "-I", "-B", "-X", "utf8", "-c", _QUERY, str(root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=environment, check=False,
    )
    if query.returncode:
        # Failed queries and interpreter/path mismatches never authorize
        # scanning another runtime or mixing stale SDK payloads.
        detail = query.stderr.strip().splitlines()[-1] if query.stderr.strip() else f"exit code {query.returncode}"
        context = json.dumps({"selectedEnvironment": str(root), "pythonExecutable": str(python_exe),
                              "exitCode": query.returncode}, ensure_ascii=False)
        detail = _redact_sensitive_text(detail)[:4096]
        context = _redact_sensitive_text(context)[:4096]
        raise RuntimeError(f"ROCm SDK path query failed: {detail}; processContext={context}")
    else:
        data = json.loads(query.stdout)
        query_status = ("ok" if data.get("querySupported") else
                        "sdk-query-unsupported" if data.get("sdkAvailable") else "sdk-not-installed")
    module_roots = []
    names = []
    for module in data["modules"]:
        path = root / _relative_directory(root, Path(module["root"]))
        if path not in module_roots:
            module_roots.append(path)
            names.append(module["name"])
    source = "rocm-sdk" if module_roots else "none"
    if not module_roots:
        sites = [root / "Lib/site-packages", *root.glob("lib/python*/site-packages")]
        for site in sites:
            for path in sorted(site.glob("_rocm_sdk*")):
                if path.is_dir() and not path.name.endswith((".dist-info", ".egg-info")):
                    _relative_directory(root, path)
                    module_roots.append(path)
                    names.append(path.name)
        if module_roots:
            source = "legacy"
            for prefix in ("_rocm_sdk_core", "_rocm_sdk_devel"):
                if sum(name.startswith(prefix) for name in names) > 1:
                    raise ValueError("Multiple legacy ROCm SDK versions exist in the selected environment")
    libraries = []
    tools = []
    for package in module_roots:
        # These SDK-relative locations are used by AMD's find_libraries and
        # core _cli trampolines; do not add the environment's Scripts directory.
        for relative in ("bin", "lib", "lib/host-math/lib", "lib/rocm_sysdeps/bin", "lib/rocm_sysdeps/lib"):
            path = package / relative
            if path.is_dir():
                value = _relative_directory(root, path)
                if value not in libraries:
                    libraries.append(value)
        for relative in ("lib/llvm/bin", "bin"):
            path = package / relative
            if path.is_dir():
                value = _relative_directory(root, path)
                if value not in tools:
                    tools.append(value)
    return {"nativeLibraryDirs": libraries, "nativeToolDirs": tools,
            "nativePathsSource": source, "nativePathsModules": names,
            "nativePathsQueryStatus": query_status}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Discover environment-local ROCm SDK paths")
    parser.add_argument("environment", type=Path)
    parser.add_argument("--python", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(discover_rocm_native_paths(arguments.environment, arguments.python)))
