"""Prepare a Studio-managed interpreter for the upstream console entry point."""
from __future__ import annotations

import contextlib
import io
import json
import runpy
import signal
import sys
from pathlib import Path


def prepare() -> int:
    from worker_bootstrap import (
        ACTIVE_RUNTIME_FILE, RUNTIME_ENVS_DIR, _installed_envs, _manifest,
        cmd_activate_runtime,
    )

    from worker_runtime_lock import runtime_lock

    with runtime_lock(RUNTIME_ENVS_DIR / ".runtime-management.lock"):
        recorded_backend = None
        if ACTIVE_RUNTIME_FILE.is_file():
            try:
                record = json.loads(ACTIVE_RUNTIME_FILE.read_text(encoding="utf-8-sig"))
                recorded_backend = record["backend"].lower()
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                raise ValueError("当前运行环境记录无效，请在 Studio 中修复或重新选择环境。") from error
        candidates = [
            row for row in _installed_envs(_manifest())
            if row.get("pythonPath") and row.get("health") != "broken"
        ]
    if recorded_backend and not any(row["backend"] == recorded_backend for row in candidates):
        raise ValueError("所选运行环境不可用，请在 Studio 中修复或重新选择环境。")
    if len(candidates) == 1:
        row = candidates[0]
        events = io.StringIO()
        with contextlib.redirect_stdout(events):
            result = cmd_activate_runtime({"backend": row["backend"], "pythonPath": row["pythonPath"], "onlyIfNoActive": True})
        if result:
            for line in events.getvalue().splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "error":
                    print(event["payload"]["message"], file=sys.stderr)
        return result
    if candidates:
        raise ValueError("检测到多个运行环境。请在 Pymss Studio 的设置 → 环境管理中选择一个环境。")
    if ACTIVE_RUNTIME_FILE.exists() or any((RUNTIME_ENVS_DIR / name / "pyvenv.cfg").exists() for name in ("cpu", "cuda", "xpu", "rocm", "mlx")):
        raise ValueError("运行环境不可用。请在 Pymss Studio 的设置 → 环境管理中修复或重新选择环境。")
    raise ValueError("请在 Pymss Studio 的设置 → 环境管理中安装运行环境，然后重新执行命令。")


def run_cli(args: list[str]) -> None:
    interrupted = False

    def interrupt(signum, frame):
        nonlocal interrupted
        interrupted = True
        signal.default_int_handler(signum, frame)

    previous = signal.signal(signal.SIGINT, interrupt)
    sys.argv[:] = ["pymss", *args]
    try:
        runpy.run_module("pymss", run_name="__main__", alter_sys=True)
    except SystemExit as error:
        # pymss translates KeyboardInterrupt into 130. Preserve SIGINT when
        # that result came from a signal, including for shells waiting on us.
        if interrupted and error.code == 128 + signal.SIGINT:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.raise_signal(signal.SIGINT)
        raise
    finally:
        signal.signal(signal.SIGINT, previous)


def main() -> int:
    # -P excludes the current working directory. Add only Studio's own helper directory.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if sys.argv[1:] == ["prepare"]:
        with contextlib.redirect_stdout(sys.stderr):
            return prepare()
    if len(sys.argv) == 3 and sys.argv[1] == "relocate":
        from worker_bootstrap import BUNDLED_RUNTIME_ENVS_DIR, RUNTIME_ENVS_DIR, _repair_runtime_venv_config
        from worker_runtime_lock import runtime_lock

        venv = Path(sys.argv[2]).resolve(strict=True).parent.parent
        root = venv.parent
        if not any(candidate and root.samefile(candidate) for candidate in (RUNTIME_ENVS_DIR, BUNDLED_RUNTIME_ENVS_DIR)):
            raise ValueError("只能修复 Pymss Studio 管理或随包提供的运行环境。")
        try:
            with runtime_lock(root / ".runtime-management.lock"):
                _repair_runtime_venv_config(venv)
        except PermissionError as error:
            raise ValueError("无法修复运行环境。请确认运行环境目录可写，或在 Pymss Studio 中重新安装。") from error
        return 0
    if sys.argv[1:] == ["bind"]:
        from importlib.util import find_spec
        if find_spec("pymss") is None:
            raise ValueError("此环境缺少 pymss，请在 Studio 的设置 → 环境管理中修复环境。")
        from worker_plugins import register_cli_environment
        register_cli_environment()
        return 0
    raise ValueError("Invalid Studio CLI initialization request")


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"]:
        run_cli(sys.argv[2:])
        raise SystemExit(0)
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        print(f"pymss: {exc}", file=sys.stderr)
        raise SystemExit(1)
