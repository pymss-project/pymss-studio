"""Probe restart PID/FD behavior using the release's actual AppImage runtime/AppRun."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time


def verify_terminal_interrupt(image: Path, runtime_tmp: Path) -> None:
    import pty

    pid, terminal = pty.fork()
    if pid == 0:
        env = {**os.environ, "TMPDIR": str(runtime_tmp), "PYMSS_APPIMAGE_PROBE_INTERRUPT": "1"}
        env.pop("NO_CLEANUP", None)
        os.execve(image, [str(image), "--appimage-extract-and-run"], env)

    def expect(marker: bytes) -> None:
        output = bytearray()
        deadline = time.monotonic() + 10
        while marker not in output and select.select([terminal], [], [], max(0, deadline - time.monotonic()))[0]:
            received = os.read(terminal, 4096)
            if not received:
                break
            output.extend(received)
        if marker not in output:
            raise AssertionError(f"Expected {marker!r} from AppRun: {output!r}")

    def wait_for_status(options: int = 0) -> int:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            waited, status = os.waitpid(pid, options | os.WNOHANG)
            if waited:
                return status
            time.sleep(0.025)
        raise TimeoutError("AppImage did not change process state")

    reaped = False
    try:
        expect(b"interrupt-ready")
        os.kill(pid, signal.SIGSTOP)
        stopped = wait_for_status(os.WUNTRACED)
        if not os.WIFSTOPPED(stopped):
            reaped = True
            raise AssertionError("AppImage exited before the interrupt probe")
        os.write(terminal, b"\x03")
        # Drain the original SIGINT in AppRun before the extractor handles it.
        expect(b"interrupt-received")
        os.kill(pid, signal.SIGCONT)
        expect(b"interrupt-once")
        status = os.waitstatus_to_exitcode(wait_for_status())
        reaped = True
        assert status == -signal.SIGINT, status
        assert not any(runtime_tmp.iterdir()), "SIGINT left an AppImage extraction directory"
    finally:
        if not reaped:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        os.close(terminal)


def main(image: Path, appdir: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="pymss-appimage-restart-") as temporary:
        root = Path(temporary)
        miniature = root / "AppDir"
        (miniature / "usr/bin").mkdir(parents=True)
        (miniature / "usr/share/applications").mkdir(parents=True)
        shutil.copy2(appdir / "AppRun", miniature / "AppRun")
        wrapped = appdir / "AppRun.wrapped"
        if wrapped.is_file() or wrapped.is_symlink():
            shutil.copy2(wrapped, miniature / "AppRun.wrapped", follow_symlinks=False)
        # The packaged AppRun sources linuxdeploy's GTK/GStreamer startup hooks.
        if (appdir / "apprun-hooks").is_dir():
            shutil.copytree(appdir / "apprun-hooks", miniature / "apprun-hooks")
        for directory in (Path(), Path("usr/share/applications")):
            for desktop in (appdir / directory).glob("*.desktop"):
                shutil.copyfile(desktop, miniature / directory / desktop.name)
        probe = miniature / "usr/bin/pymss-studio"
        subprocess.run(["cc", "-x", "c", "-", "-o", str(probe)], check=True, input=r'''
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static int probe_interrupt(void) {
    sigset_t signals;
    sigemptyset(&signals);
    sigaddset(&signals, SIGINT);
    if (sigprocmask(SIG_BLOCK, &signals, NULL) != 0) return 88;
    puts("interrupt-ready");
    fflush(stdout);
    if (sigwaitinfo(&signals, NULL) != SIGINT) return 89;
    puts("interrupt-received");
    fflush(stdout);
    const struct timespec timeout = { .tv_sec = 2 };
    if (sigtimedwait(&signals, NULL, &timeout) != -1 || errno != EAGAIN) return 90;
    puts("interrupt-once");
    return 0;
}

int main(void) {
    if (getenv("PYMSS_APPIMAGE_PROBE_INTERRUPT")) return probe_interrupt();
    const char *value = getenv("PYMSS_STUDIO_INSTANCE_FD");
    if (!value) return 81;
    int fd = atoi(value), flags = fcntl(fd, F_GETFD);
    if (fd < 3 || flags < 0) return 82;
    struct stat inherited, expected;
    if (fstat(fd, &inherited) || stat(getenv("PYMSS_APPIMAGE_PROBE_LOCK"), &expected)
        || !S_ISREG(inherited.st_mode) || inherited.st_ino != expected.st_ino
        || inherited.st_dev != expected.st_dev) return 87;
    if (fcntl(fd, F_SETFD, flags | FD_CLOEXEC) < 0) return 83;
    const char *path = getenv("PYMSS_APPIMAGE_PROBE_ACK");
    char pending[4096];
    if (snprintf(pending, sizeof(pending), "%s.pending", path) >= sizeof(pending)) return 85;
    FILE *ack = fopen(pending, "w");
    if (!ack) return 84;
    fprintf(ack, "%ld\n%s\n", (long)getpid(), getenv("APPDIR"));
    fflush(ack); fsync(fileno(ack)); fclose(ack);
    if (rename(pending, path) != 0) return 86;
    for (;;) pause();
}
''', text=True)
        filesystem = root / "probe.squashfs"
        subprocess.run(["mksquashfs", str(miniature), str(filesystem), "-noappend", "-quiet", "-no-progress"], check=True)
        offset = int(subprocess.check_output([str(image), "--appimage-offset"], text=True).strip())
        packaged = root / "probe.AppImage"
        with image.open("rb") as original, packaged.open("wb") as output, filesystem.open("rb") as contents:
            output.write(original.read(offset))
            shutil.copyfileobj(contents, output)
        packaged.chmod(0o755)
        runtime_tmp = root / "runtime-tmp"
        runtime_tmp.mkdir()
        verify_terminal_interrupt(packaged, runtime_tmp)

        for mode in ("cli", "environment", "update"):
            ack = root / f"{mode}.ack"
            lock = (root / f"{mode}.lock").open("w+")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            environment = {key: value for key, value in os.environ.items() if key not in (
                "APPIMAGE", "APPDIR", "ARGV0", "APPIMAGE_EXTRACT_AND_RUN", "LD_LIBRARY_PATH",
            )}
            environment.update({
                "TMPDIR": str(runtime_tmp), "PYMSS_STUDIO_INSTANCE_FD": str(lock.fileno()),
                "PYMSS_APPIMAGE_PROBE_ACK": str(ack),
                "PYMSS_APPIMAGE_PROBE_LOCK": str(root / f"{mode}.lock"),
            })
            if mode != "cli":
                environment["APPIMAGE_EXTRACT_AND_RUN"] = "1"
            workspace = None
            if mode == "update":
                workspace = root / "pymss-update-extract-probe"
                workspace.mkdir(mode=0o700)
                environment["PYMSS_STUDIO_UPDATE_EXTRACTION_ROOT"] = str(workspace)
            arguments = [str(packaged)] + (["--appimage-extract-and-run"] if mode == "cli" else [])
            with (root / f"{mode}.log").open("w+") as log:
                process = subprocess.Popen(arguments, env=environment, pass_fds=(lock.fileno(),),
                                           start_new_session=True, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 20
                    while not ack.exists():
                        if process.poll() is not None or time.monotonic() > deadline:
                            log.seek(0)
                            raise AssertionError(f"AppImage {mode} restart failed: {log.read()}")
                        time.sleep(0.025)
                    lines = ack.read_text().splitlines()
                    pid = int(lines[0])
                    mount = Path(lines[1])
                    assert mount.name.startswith("appimage_extracted_"), lines
                    assert mount.parent == (workspace or runtime_tmp), lines
                    assert pid != process.pid, "Extraction mode must exercise a wrapped application process"
                    current = pid
                    for _ in range(64):
                        stat = Path(f"/proc/{current}/stat").read_text().rsplit(")", 1)[1].split()
                        current = int(stat[1])
                        if current == process.pid:
                            break
                    else:
                        raise AssertionError("The application is not a descendant of its runtime launcher")
                    lock.close()
                    with (root / f"{mode}.lock").open("r+") as contender:
                        try:
                            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            pass
                        else:
                            raise AssertionError("AppImage/AppRun lost the inherited instance lock")
                    os.kill(pid, signal.SIGTERM)
                    process.wait(timeout=15)
                    if workspace is not None:
                        assert not workspace.exists(), "AppImage did not release the update workspace"
                    with (root / f"{mode}.lock").open("r+") as contender:
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    lock.close()
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
        print("AppImage restart runtime smoke passed: CLI/environment extraction and instance lock handoff")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
