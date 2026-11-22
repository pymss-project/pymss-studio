"""Exercise the packaged Unix CLI and its terminal job control."""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path


def fixture_environment(
    work: Path, name: str, script: str, base_env: dict[str, str], appimage: bool,
) -> tuple[Path, dict[str, str]]:
    sandbox = work / name
    runtime = sandbox / "runtime-envs"
    interpreter = runtime / "cpu/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(script, encoding="utf-8")
    interpreter.chmod(0o755)
    (runtime / "active-runtime.json").write_text(
        json.dumps({"backend": "cpu", "pythonPath": "cpu/bin/python"}), encoding="utf-8",
    )
    environment = {**base_env, "PYMSS_STUDIO_DATA_ROOT": str(sandbox), "PYMSS_CLI_FIXTURE": str(sandbox)}
    if appimage:
        extraction = sandbox / "extract"
        extraction.mkdir()
        environment.update(APPIMAGE_EXTRACT_AND_RUN="1", TMPDIR=str(extraction))
        environment.pop("NO_CLEANUP", None)
    return sandbox, environment


def verify_cancellation(
    command: list[str], base_env: dict[str, str], work: Path, appimage: bool, *, group_interrupt: bool = False,
) -> None:
    name = "group-interrupt" if group_interrupt else "cancellation"
    sandbox, environment = fixture_environment(work, name, r"""#!/bin/sh
for arg do
    if [ "$arg" = "run" ]; then
        printf '%s\n' "$APPDIR" > "$PYMSS_CLI_FIXTURE/mount"
        trap ': > "$PYMSS_CLI_FIXTURE/terminated"; exit 0' TERM HUP
        trap '
            : > "$PYMSS_CLI_FIXTURE/interrupted"
            while [ ! -f "$PYMSS_CLI_FIXTURE/resumed" ]; do sleep 0.02; done
            sleep 0.2
            : > "$PYMSS_CLI_FIXTURE/terminated"
            exit 0
        ' INT
        /bin/sh -c '
            trap "" TERM HUP
            echo "$$" > "$1/tool"
            : > "$1/ready"
            while [ ! -f "$1/release" ]; do sleep 0.05; done
        ' tool "$PYMSS_CLI_FIXTURE" &
        wait
    fi
done
""", base_env, appimage)
    ready = sandbox / "ready"
    terminated = sandbox / "terminated"

    def ignore_child_exit() -> None:
        # The extractor and Studio must each retain their own child's status.
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)

    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen([*command, "list"], env=environment, cwd=work,
                                   stdout=log, stderr=log, start_new_session=True,
                                   preexec_fn=ignore_child_exit)
        try:
            diagnostic = "SIGTERM to the outer AppImage" if appimage else "SIGHUP to the launcher"
            deadline = time.monotonic() + 180
            while not ready.is_file() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not ready.is_file():
                log.seek(0)
                raise RuntimeError(f"CLI cancellation fixture did not start: {log.read().decode(errors='replace')}")
            tool_pid = int((sandbox / "tool").read_text(encoding="ascii"))
            if group_interrupt:
                diagnostic = "SIGINT to the AppImage process group"
                sent = signal.SIGINT
                # Let Studio receive the group signal before the extractor forwards it.
                os.kill(process.pid, signal.SIGSTOP)
                stopped = wait_for_job(process.pid, os.WUNTRACED, timeout=5)
                if not os.WIFSTOPPED(stopped):
                    process.returncode = os.waitstatus_to_exitcode(stopped)
                    raise RuntimeError("AppImage exited before the group cancellation test")
                try:
                    os.killpg(process.pid, sent)
                    wait_for_file(sandbox / "interrupted", process, timeout=5)
                finally:
                    os.kill(process.pid, signal.SIGCONT)
                    (sandbox / "resumed").touch()
            else:
                sent = signal.SIGTERM if appimage else signal.SIGHUP
                process.send_signal(sent)
            deadline = time.monotonic() + 15
            while not terminated.is_file() and time.monotonic() < deadline:
                time.sleep(0.05)
            if not terminated.is_file():
                raise RuntimeError(f"{diagnostic} did not stop the worker")
            if process.wait(timeout=10) != -sent:
                raise RuntimeError(f"{diagnostic} did not preserve the termination signal")

            if appimage:
                path = (sandbox / "mount").read_text(encoding="utf-8").rstrip("\n")
                if not path or Path(path).exists():
                    raise RuntimeError("AppImage did not remove its extraction directory after cancellation")

            # Check the stubborn tool and the lock owned by the inner launcher.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    os.kill(tool_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError(f"{diagnostic} left a tool running after the worker exited")
            with (sandbox / "runtime-envs/.pymss-cli-runtime.lock").open() as lease:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("CLI cancellation did not release the runtime lock") from None
                        time.sleep(0.05)
        finally:
            (sandbox / "release").touch()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def wait_for_job(pid: int, options: int, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waited, status = os.waitpid(pid, options | os.WNOHANG)
        if waited:
            return status
        time.sleep(0.05)
    raise TimeoutError("The CLI job did not change state")


def expect_terminal(fd: int, marker: bytes, timeout: float) -> None:
    output = bytearray()
    deadline = time.monotonic() + timeout
    while marker not in output:
        if not select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
            break
        try:
            received = os.read(fd, 4096)
        except OSError as error:
            if error.errno != errno.EIO:
                raise
            break
        if not received:
            break
        output.extend(received)
    if marker not in output:
        raise RuntimeError(f"Expected terminal output {marker!r}: {output.decode(errors='replace')}")


def run_terminal_job(command: list[str], environment: dict[str, str], work: Path, sandbox: Path, appimage: bool) -> None:
    args = [*command, "list"]
    if appimage:
        # Make the AppImage extractor a member of a shell wrapper's job group.
        args = ["/bin/sh", "-c", '"$@"; exit "$?"', "appimage", *args]
    # Give the PTY the keyboard and signal settings the test exercises.
    attributes = termios.tcgetattr(0)
    attributes[3] |= termios.ICANON | termios.ISIG
    attributes[6][termios.VINTR] = b"\x03"
    attributes[6][termios.VSUSP] = b"\x1a"
    termios.tcsetattr(0, termios.TCSANOW, attributes)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTSTP})
    process = subprocess.Popen(args, cwd=work, env=environment, preexec_fn=os.setpgrp)
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    try:
        os.tcsetpgrp(0, process.pid)
        stopped = wait_for_job(process.pid, os.WUNTRACED, timeout=180)
        if not os.WIFSTOPPED(stopped):
            process.returncode = os.waitstatus_to_exitcode(stopped)
            raise RuntimeError("Ctrl+Z did not suspend the CLI job")

        os.tcsetpgrp(0, os.getpgrp())
        os.killpg(process.pid, signal.SIGCONT)
        (sandbox / "background").touch()
        wait_for_file(sandbox / "background-resumed", process, timeout=10)

        # An already running job can get the terminal without another SIGCONT.
        os.tcsetpgrp(0, process.pid)
        (sandbox / "foreground").touch()
        print("cli-foreground", flush=True)
        try:
            status = process.wait(timeout=15)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(
                f"Ctrl+C did not stop launcher {process.pid}. "
                f"Terminal foreground group: {os.tcgetpgrp(0)}. "
                f"Worker received SIGINT: {(sandbox / 'interrupted').is_file()}."
            ) from error
        expected = 128 + signal.SIGINT if appimage else -signal.SIGINT
        if status != expected:
            raise RuntimeError(f"Ctrl+C exited with {status}, expected {expected}")
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGCONT)
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


def wait_for_file(path: Path, process: subprocess.Popen, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not path.is_file() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if not path.is_file():
        raise RuntimeError(f"CLI fixture did not create {path.name}")


def verify_terminal(command: list[str], base_env: dict[str, str], work: Path, appimage: bool) -> None:
    sandbox, environment = fixture_environment(work, "terminal", r"""#!/bin/sh
for arg do
    if [ "$arg" = "run" ]; then
        export PYTHONPATH="$PYMSS_CLI_FIXTURE/modules"
        exec "$PYMSS_CLI_FIXTURE_PYTHON" "$@"
    fi
done
""", base_env, appimage)
    environment["PYMSS_CLI_FIXTURE_PYTHON"] = sys.executable
    package = sandbox / "modules/pymss"
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    (package / "__main__.py").write_text('''from pathlib import Path
import os
import time

def main():
    root = Path(os.environ["PYMSS_CLI_FIXTURE"])
    try:
        print("cli-ready", flush=True)
        print(f"cli-started:{input()}", flush=True)
        while not (root / "background").is_file():
            time.sleep(0.05)
        (root / "background-resumed").touch()
        while not (root / "foreground").is_file():
            time.sleep(0.05)
        print(f"cli-read:{input()}", flush=True)
        time.sleep(60)
    except KeyboardInterrupt:
        # The upstream CLI translates KeyboardInterrupt into 130.
        (root / "interrupted").touch()
        return 130
    return 0

raise SystemExit(main())
''', encoding="utf-8")
    pid, fd = pty.fork()
    if pid == 0:
        try:
            run_terminal_job(command, environment, work, sandbox, appimage)
        except Exception as error:
            print(f"CLI terminal test: {error}", flush=True)
            os._exit(1)
        print("cli-terminal-complete", flush=True)
        os._exit(0)

    reaped = False
    try:
        expect_terminal(fd, b"cli-ready", timeout=180)
        os.write(fd, b"start\n")
        expect_terminal(fd, b"cli-started:start", timeout=10)
        os.write(fd, b"\x1a")
        expect_terminal(fd, b"cli-foreground", timeout=15)
        os.write(fd, b"continue\n")
        expect_terminal(fd, b"cli-read:continue", timeout=10)
        os.write(fd, b"\x03")
        # The controller owns the 15-second exit deadline and can report the
        # failure after cleaning up. Keep reading its output until then.
        expect_terminal(fd, b"cli-terminal-complete", timeout=25)
        status = os.waitstatus_to_exitcode(wait_for_job(pid, 0, timeout=5))
        reaped = True
        if status:
            raise RuntimeError(f"The terminal controller exited with {status}")
    finally:
        os.close(fd)
        if not reaped:
            try:
                wait_for_job(pid, 0, timeout=15)
            except TimeoutError:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)


def verify_extraction_isolation(command: list[str], base_env: dict[str, str], work: Path) -> None:
    sandbox, environment = fixture_environment(work, "overlapping-extraction", r"""#!/bin/sh
case "$*" in
    *' run list long')
        printf '%s\n' "$APPDIR" > "$PYMSS_CLI_FIXTURE/first"
        : > "$PYMSS_CLI_FIXTURE/ready"
        while [ ! -f "$PYMSS_CLI_FIXTURE/release" ]; do sleep 0.05; done
        test -f "$APPDIR/AppRun"
        ;;
    *' run list short')
        printf '%s\n' "$APPDIR"
        ;;
esac
""", base_env, appimage=True)
    with subprocess.Popen(
        [*command, "list", "long"], cwd=work, env=environment, start_new_session=True,
    ) as first:
        try:
            wait_for_file(sandbox / "ready", first, timeout=180)
            first_mount = Path((sandbox / "first").read_text(encoding="utf-8").rstrip("\n"))
            second = subprocess.run(
                [*command, "list", "short"], cwd=work, env=environment,
                capture_output=True, encoding="utf-8", check=True, timeout=180,
            )
            if not second.stdout:
                raise RuntimeError("The second AppImage run did not report its extraction directory")
            second_mount = Path(second.stdout.rstrip("\n"))
            if first_mount == second_mount or not (first_mount / "AppRun").is_file():
                raise RuntimeError("One AppImage run removed files still used by another run")
            if second_mount.exists():
                raise RuntimeError("The second AppImage run did not clean up its extraction directory")
        finally:
            (sandbox / "release").touch()
            try:
                status = first.wait(timeout=15)
            except subprocess.TimeoutExpired:
                first.kill()
                first.wait()
                raise
        if status or first_mount.exists():
            raise RuntimeError("The first AppImage run did not finish and clean up its own files")


def verify(launcher: Path, appimage: bool, data_root: Path | None) -> None:
    launcher = launcher.resolve(strict=True)
    command = [str(launcher), *(["--studio-cli"] if appimage else [])]
    with tempfile.TemporaryDirectory(prefix="pymss cli ") as temporary:
        work = Path(temporary)
        data = data_root.resolve(strict=True) if data_root else work / "studio data"
        settings = data / "settings"
        settings.mkdir(parents=True, exist_ok=True)
        models = work / "shared models"
        (settings / "app.json").write_text(json.dumps({"modelDir": str(models)}), encoding="utf-8")
        (settings / "user_models.json").write_text(json.dumps({"models": [{
            "name": "registry-smoke", "model_type": "vr", "model_path": str(work / "local.pth"),
        }]}), encoding="utf-8")
        (work / "pymss.py").write_text("raise RuntimeError('Unexpected import from cwd')\n", encoding="utf-8")
        env = {
            **os.environ,
            "PATH": "/usr/bin:/bin",
            "PYMSS_STUDIO_DATA_ROOT": str(data),
            "PYMSS_PLUGIN_ENVIRONMENTS_DIR": str(work / "bindings"),
            "PYTHONHOME": str(work / "unrelated python"),
            "PYTHONPATH": str(work),
        }

        def call(*args: str, success: bool = True) -> subprocess.CompletedProcess[str]:
            result = subprocess.run([*command, *args], cwd=work, env=env,
                                    capture_output=True, encoding="utf-8", timeout=180)
            if success and result.returncode:
                raise RuntimeError(f"Packaged CLI exited with {result.returncode}: {result.stderr}")
            return result

        if "infer" not in call("--help").stdout:
            raise RuntimeError("The packaged launcher did not return upstream help")
        registered = json.loads(call("list", "--user-only", "--json").stdout)
        if [row["name"] for row in registered] != ["registry-smoke"]:
            raise RuntimeError("The CLI did not read the Studio model registry")
        catalog = json.loads(call("list", "--json").stdout)
        model = next(row["name"] for row in catalog if row.get("source") != "user")
        for args, root in [((), models), (("--model-dir", "override models"), Path("override models"))]:
            found = Path(json.loads(call("info", model, *args).stdout)["model_path"])
            if found.is_absolute() != root.is_absolute():
                found = found if found.is_absolute() else work / found
                root = root if root.is_absolute() else work / root
            if not found.is_relative_to(root):
                raise RuntimeError(f"The CLI resolved an unexpected model path: {found}")
        invalid = call("--invalid-pymss-option", success=False)
        if invalid.returncode != 2 or invalid.stdout:
            raise RuntimeError("The CLI did not preserve the upstream argument error")
        if not appimage:
            link = work / "pymss"
            link.symlink_to(launcher)
            result = subprocess.run([str(link), "--help"], cwd=work, env=env,
                                    capture_output=True, encoding="utf-8", timeout=180, check=True)
            if "infer" not in result.stdout:
                raise RuntimeError("The CLI did not start through a symbolic link")
        verify_cancellation(command, env, work, appimage)
        verify_terminal(command, env, work, appimage)
        if appimage:
            verify_cancellation(command, env, work, appimage, group_interrupt=True)
            verify_extraction_isolation(command, env, work)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("launcher", type=Path)
    parser.add_argument("--appimage", action="store_true")
    parser.add_argument("--data-root", type=Path)
    arguments = parser.parse_args()
    verify(arguments.launcher, arguments.appimage, arguments.data_root)
