"""Package and validate the companion kit on a disposable macOS runner."""
from pathlib import Path
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

studio, downloaded, plugin_source, output = map(Path, sys.argv[1:])
output.mkdir(parents=True, exist_ok=True)
dmg, = downloaded.glob("*.dmg")
app_report = json.loads((downloaded / "timing-ci-verification.json").read_text())
assert app_report["signatureAfterTests"] == "PASS"
assert app_report["commit"] == "521998013936dcf0c897412707a7bde8e80d8311"
assert all(item["exitCode"] == 0 for item in app_report["checks"])
kit = output / "mac-timing-test-kit"
shutil.copytree(studio / "test-support/timing-kit", kit)
shutil.copytree(plugin_source / "plugins/timing", kit / "plugins/timing",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules", ".DS_Store"))
tests = kit / "tooling/tests"
tests.mkdir(parents=True)
shutil.copyfile(plugin_source / "tooling/tests/test_timing_file_operation.py", tests / "test_timing_file_operation.py")
shutil.copyfile(studio / "python/timing-test-build.json", kit / "build-info.json")

def sha(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(data)
    return result.hexdigest()

with tempfile.TemporaryDirectory(prefix="timing-kit-app-") as temp:
    temporary = Path(temp)
    mount = temporary / "mounted"
    subprocess.run(["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount), str(dmg)], check=True)
    try:
        app = temporary / "Pymss Studio Timing Test.app"
        subprocess.run(["ditto", str(mount / app.name), str(app)], check=True)
    finally:
        subprocess.run(["hdiutil", "detach", str(mount)], check=True)
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    executable_sha = sha(app / "Contents/MacOS" / info["CFBundleExecutable"])
    receipt = {"buildId": "b48688e26", "applicationCommit": app_report["commit"], "applicationRunId": 41603639907,
               "compilationRunId": app_report["compilationRunId"], "applicationVerificationCommit": app_report["verificationCommit"],
               "kitCommit": os.environ["GITHUB_SHA"], "kitRunId": os.environ["GITHUB_RUN_ID"],
               "pluginSnapshotCommit": "9660b8439ed3666a22916e88498eb06309ea1cd2",
               "coreSnapshotCommit": "d7015a69cc7022df1666bd3d4b3f72b27275d435",
               "dmg": {"name": dmg.name, "sha256": sha(dmg), "bytes": dmg.stat().st_size},
               "executableSha256": executable_sha, "signing": "ad-hoc", "notarized": False}
    (kit / "build-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    common = kit / "common.sh"
    common.write_text(common.read_text().replace("__CI_EXECUTABLE_SHA256__", executable_sha))
    for command in kit.glob("*.command"):
        command.chmod(0o755)
    common.chmod(0o755)
    sums = []
    for path in sorted(kit.rglob("*")):
        if path.is_file():
            sums.append(f"{sha(path)}  {path.relative_to(kit).as_posix()}")
    (kit / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n")
    checks = []
    # Repeat preparation to confirm reruns retain existing fixtures and the old-output sentinel.
    for name in ("Verify Test Package.command", "Prepare Test Fixtures.command", "Prepare Test Fixtures.command"):
        started = time.time()
        result = subprocess.run(["/bin/bash", str(kit / name), str(app)], capture_output=True, text=True, timeout=360)
        print(name, result.stdout[-3000:], result.stderr[-5000:], flush=True)
        checks.append({"command": name, "exitCode": result.returncode, "seconds": round(time.time() - started, 2),
                       "stdout": result.stdout[-5000:], "stderr": result.stderr[-7000:]})
        if result.returncode:
            raise RuntimeError(f"Kit acceptance failed: {name}")
    res = next(root for root in (app / "Contents/Resources", app / "Contents/Resources/resources")
               if (root / "python/worker.py").is_file())
    env = dict(os.environ, PYTHONHOME=str(res / "python-runtime"), PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    result = subprocess.run([str(res / "python-runtime/bin/python3"), "-B", str(studio / "test-support/check-timing-kit-checker.py"),
                             str(kit), str(app)], env=env, capture_output=True, text=True, timeout=180)
    print(result.stdout[-4000:], result.stderr[-4000:], flush=True)
    checks.append({"name": "synthetic report checker success and failure", "exitCode": result.returncode,
                   "stdout": result.stdout[-7000:], "stderr": result.stderr[-4000:], "manualGUIStatus": "NOT_RUN"})
    if result.returncode:
        raise RuntimeError("The companion result checker selftest failed")
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    ci = {"buildId": "b48688e26", "checks": checks, "signatureAfterTests": "PASS",
          "manualGUIStatus": "NOT_RUN_IN_CI", "receipt": receipt}
    (output / "timing-kit-ci-verification.json").write_text(json.dumps(ci, indent=2))
    shutil.copyfile(kit / "build-receipt.json", output / "build-receipt.json")
    with zipfile.ZipFile(output / "mac-timing-test-kit.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(kit.rglob("*")):
            if path.is_file():
                archive.write(path, (Path(kit.name) / path.relative_to(kit)).as_posix())
    for name in ("README.zh-CN.md", "TESTING.zh-CN.md", "CHECKLIST.zh-CN.md", "build-info.json"):
        shutil.copyfile(kit / name, output / name)
print("Companion kit verified and packaged")
