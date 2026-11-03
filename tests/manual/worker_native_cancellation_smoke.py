"""Exercise Studio's real process-group code with pymss's real native gate.

Only a small standalone Rust harness is compiled; no desktop build is needed.
Use --gate to select a published or checked-out _command_bootstrap.py on POSIX.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


STUDIO = Path(__file__).resolve().parents[2]
MODULE = STUDIO / "src-tauri/src/python/process_tree.rs"
HARNESS = r'''
use std::io;
use std::process::{Command, Stdio};

fn main() {
    let mut arguments = std::env::args_os().skip(1);
    let mut command = Command::new(arguments.next().expect("worker executable"));
    command.args(arguments).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    #[cfg(unix)]
    process_tree::configure_worker(&mut command);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(process_tree::CREATE_NO_WINDOW);
    }
    let mut worker = command.spawn().expect("start worker");
    let mut control = String::new();
    // EOF also stops this fixture if its test controller fails.
    let _ = io::stdin().read_line(&mut control);
    let status = process_tree::terminate_worker(&mut worker).expect("reap worker");
    println!("worker exit: {status}");
}
'''


def alive(pid):
    if os.name == "nt":
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return api.WaitForSingleObject(handle, 0) == 258
        finally:
            api.CloseHandle(handle)
    status = Path(f"/proc/{pid}/stat")
    try:
        if status.read_text().rsplit(") ", 1)[1].startswith("Z"):
            return False
    except FileNotFoundError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_for(condition, description, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(description)


def case(binary, directory, gate, sentinel):
    marker = directory / "processes.json"
    engine = (
        "import json,os,subprocess,sys,time\nfrom pathlib import Path\n"
        "leaf=subprocess.Popen([sys.executable,'-I','-B','-c','import time;time.sleep(60)'])\n"
        f"marker=Path({str(marker)!r})\n"
        "record=dict(worker=int(os.environ['PYMSS_SMOKE_WORKER']),engine=os.getpid(),leaf=leaf.pid)\n"
        + ("record['gate']=os.getppid()\n" if gate else "")
        + "partial=marker.with_suffix('.pending')\npartial.write_text(json.dumps(record))\npartial.replace(marker)\n"
        "time.sleep(60)\n"
    )
    worker = directory / "worker.py"
    source = "import json,os,subprocess,sys\nos.environ['PYMSS_SMOKE_WORKER']=str(os.getpid())\n"
    if gate:
        source += (
            f"child=subprocess.Popen([sys.executable,'-I','-B','-S',{str(gate)!r}],stdin=subprocess.PIPE,start_new_session=True)\n"
            f"request=dict(arguments=[sys.executable,'-I','-B','-c',{engine!r}],pluginBindings=[],forwardStdin=False)\n"
            "child.stdin.write(json.dumps(request).encode()+b'\\n');child.stdin.flush()\n"
        )
    else:
        source += f"child=subprocess.Popen([sys.executable,'-I','-B','-c',{engine!r}])\n"
    source += "child.wait()\n"
    worker.write_text(source, encoding="utf-8")
    owner = subprocess.Popen([str(binary), sys.executable, "-I", "-B", str(worker)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    record = None
    owned_gate = None
    try:
        wait_for(marker.exists, "The worker did not start its native descendants", seconds=15)
        record = json.loads(marker.read_text())
        if gate:
            assert os.getpgid(record["gate"]) == record["gate"]
            owned_gate = record["gate"]
        started = time.monotonic()
        stdout, stderr = owner.communicate(b"cancel\n", timeout=10)
        assert owner.returncode == 0, stderr.decode(errors="replace")
        wait_for(lambda: not any(alive(pid) for pid in record.values()),
                 f"Cancellation left a running process: {record}")
        assert sentinel.poll() is None, "Cancellation terminated an unrelated process"
        return {"mode": "pymss-native-gate" if gate else "ordinary-descendants", "processes": record,
                "exitSeconds": round(time.monotonic() - started, 3), "status": "PASS",
                "stdout": stdout.decode("oem" if os.name == "nt" else "utf-8", errors="replace")}
    finally:
        if owner.poll() is None:
            if owner.stdin is not None:
                owner.stdin.close()
                owner.stdin = None
            owner.communicate(timeout=10)
        # A failed regression also cleans its own old-behavior orphan fixture.
        if owned_gate and record and any(alive(record[key]) for key in ("engine", "leaf")):
            if any(alive(record[key]) and os.getpgid(record[key]) == owned_gate for key in ("engine", "leaf")):
                try:
                    os.killpg(owned_gate, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if record:
            for pid in record.values():
                if alive(pid):
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
                    else:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", type=Path)
    parser.add_argument("--module", type=Path, default=MODULE)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--mode", choices=("all", "ordinary", "managed"), default="all")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if os.name != "nt" and (not args.gate or not args.gate.is_file()):
        parser.error("POSIX validation requires --gate pointing to pymss's native bootstrap")
    if os.name == "nt" and args.mode == "managed":
        parser.error("The isolated native-gate fixture is POSIX-only; Windows uses its Job scope")
    with tempfile.TemporaryDirectory(prefix="studio-worker-cancel-") as temporary:
        root = Path(temporary)
        (root / "src").mkdir()
        module = args.module.resolve().as_posix()
        (root / "src/main.rs").write_text(f"#[path = {json.dumps(module, ensure_ascii=False)}]\nmod process_tree;\n" + HARNESS, encoding="utf-8")
        (root / "Cargo.toml").write_text(
            '[package]\nname="studio-worker-cancel-smoke"\nversion="0.0.0"\nedition="2021"\n'
            '[target.\'cfg(unix)\'.dependencies]\nlibc="0.2"\n', encoding="utf-8")
        base = ["cargo", "--offline"] if args.offline else ["cargo"]
        for command in ("test", "build"):
            subprocess.run([*base, command, "--manifest-path", str(root / "Cargo.toml")], check=True, timeout=180)
        binary = root / "target/debug" / ("studio-worker-cancel-smoke.exe" if os.name == "nt" else "studio-worker-cancel-smoke")
        sentinel = subprocess.Popen([sys.executable, "-I", "-B", "-c", "import time;time.sleep(90)"])
        try:
            results = []
            modes = []
            if args.mode != "managed":
                modes.append(("ordinary", None))
            if os.name != "nt" and args.mode != "ordinary":
                modes.append(("native", args.gate.resolve()))
            for mode, gate in modes:
                for attempt in range(2):
                    directory = root / f"{mode}-{attempt}"
                    directory.mkdir()
                    result = case(binary, directory, gate, sentinel)
                    results.append(result)
                    print(json.dumps(result), flush=True)
        finally:
            if sentinel.poll() is None:
                sentinel.kill()
            sentinel.wait(timeout=5)
        report = {"platform": sys.platform, "module": module, "gate": str(args.gate) if args.gate else None,
                  "cases": results, "status": "PASS"}
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
