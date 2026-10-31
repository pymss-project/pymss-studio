from __future__ import annotations

import subprocess
from pathlib import Path


def test_serve_parser_accepts_missing_model():
    from pymss.cli import build_parser

    args = build_parser().parse_args(["serve"])

    assert args.model is None


def test_start_server_places_model_dir_before_serve(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    uv.chmod(0o755)
    model_dir = tmp_path / "models"
    repo_root = Path(__file__).resolve().parents[2]

    result = subprocess.run(
        ["bash", "test/server/start_server.sh"],
        cwd=repo_root,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "MODEL_DIR": str(model_dir),
            "MODEL": "",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines()[:7] == [
        "run",
        "--extra",
        "server",
        "pymss",
        "--model-dir",
        str(model_dir),
        "serve",
    ]
