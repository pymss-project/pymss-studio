"""Check a staged native-plugin executable without opening its desktop interface."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import tempfile


def executable_path(path: Path) -> Path:
    path = path.resolve(strict=True)
    if path.is_dir() and path.suffix == ".app":
        with (path / "Contents/Info.plist").open("rb") as source:
            info = plistlib.load(source)
        if info.get("CFBundleIdentifier") != "studio.pymss.desktop.plugintest":
            raise ValueError(
                "The staged application does not use the plugin test bundle identifier"
            )
        name = info.get("CFBundleExecutable")
        if not isinstance(name, str) or not name or Path(name).name != name:
            raise ValueError("The staged application has an invalid executable name")
        executable = (path / "Contents/MacOS" / name).resolve(strict=True)
        if not executable.is_relative_to(path):
            raise ValueError("The staged executable escapes its application bundle")
        path = executable
    if not path.is_file() or (os.name != "nt" and not os.access(path, os.X_OK)):
        raise ValueError(
            "The staged native-plugin executable is missing or not executable"
        )
    return path


def run_helper(
    command: list[str], payload: str | None, environment: dict[str, str], timeout: float
) -> dict:
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=environment,
        start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(payload, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.kill()
        process.communicate()
        raise RuntimeError("The staged plugin helper timed out") from error
    if process.returncode:
        raise RuntimeError(
            f"The staged plugin helper failed ({process.returncode}): {stderr.strip()[:1000]}"
        )
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError(
            "The staged plugin helper did not return a single JSON response"
        )
    result = json.loads(lines[0])
    if not isinstance(result, dict):
        raise ValueError("The staged plugin helper returned an invalid JSON response")
    return result


def verify(executable: Path, platform: str, timeout: float = 15) -> dict:
    if platform == "macos":
        app = executable.resolve(strict=True)
        if not app.is_dir() or app.suffix != ".app":
            raise ValueError(
                "Verify the final macOS app bundle, including its signing entitlements"
            )
        signature = subprocess.run(
            ["codesign", "--display", "--entitlements", ":-", str(app)],
            capture_output=True,
            check=True,
            timeout=timeout,
        )
        entitlements = plistlib.loads(signature.stdout)
        for permission in (
            "com.apple.security.cs.disable-library-validation",
            "com.apple.security.cs.allow-jit",
            "com.apple.security.cs.allow-unsigned-executable-memory",
        ):
            if entitlements.get(permission) is not True:
                raise ValueError(
                    "The signed macOS app is missing required plugin permission: "
                    f"{permission}=true"
                )
    executable = executable_path(executable)
    expected = {"vst2", "vst3"} | ({"au"} if platform == "macos" else set())
    with tempfile.TemporaryDirectory(prefix="pymss-plugin-package-") as temporary:
        environment = dict(
            os.environ, PYMSS_STUDIO_DATA_ROOT=str(Path(temporary) / "data")
        )
        capabilities = run_helper(
            [str(executable), "--plugin-host-capabilities"], None, environment, timeout
        )
        if (
            capabilities.get("enabled") is not True
            or capabilities.get("labBuild") is not False
        ):
            raise ValueError(
                "The staged application is not a production native-plugin build"
            )
        if (
            capabilities.get("platform") != platform
            or set(capabilities.get("formats", [])) != expected
        ):
            raise ValueError(
                "The staged native-plugin formats do not match its release platform"
            )
        response = run_helper(
            [str(executable), "--audio-host"],
            json.dumps({"id": 1, "op": "shutdown"}) + "\n",
            environment,
            timeout,
        )
        if (
            response.get("id") != 1
            or response.get("ok") is not True
            or response.get("result", {}).get("closed") is not True
        ):
            raise ValueError(
                "The staged audio-host helper did not acknowledge shutdown"
            )
    return capabilities


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument(
        "--platform", choices=("windows", "macos", "linux"), required=True
    )
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()
    capabilities = verify(args.executable, args.platform, args.timeout)
    print(
        json.dumps(
            {"nativePluginPackageVerified": True, "capabilities": capabilities},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
