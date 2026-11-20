"""Exercise the staged console executable independently of the GUI and the runner's PATH."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def verify(stage: Path, online: bool) -> None:
    launcher = stage.resolve() / "pymss.exe"
    with tempfile.TemporaryDirectory(prefix="pymss cli ") as temporary:
        work = Path(temporary)
        data = work / "Studio 数据"
        managed = data / "runtime-envs"
        managed.mkdir(parents=True)
        (managed / ".pymss-managed-root.json").write_text('{"schemaVersion":1}', encoding="utf-8")
        settings = data / "settings"
        settings.mkdir()
        models = work / "shared models"
        (settings / "app.json").write_text(json.dumps({"modelDir": str(models)}), encoding="utf-8")
        (settings / "user_models.json").write_text(json.dumps({"models": [{
            "name": "registry-smoke", "model_type": "vr", "model_path": str(work / "local.pth"),
        }]}), encoding="utf-8")
        (work / "pymss.py").write_text("raise RuntimeError('Unexpected import from cwd')", encoding="utf-8")
        env = {**os.environ, "PYMSS_STUDIO_DATA_ROOT": str(data),
               "PYMSS_PLUGIN_ENVIRONMENTS_DIR": str(work / "bindings"),
               "PYTHONHOME": str(work / "unrelated python"), "PYTHONPATH": str(work)}
        def call(*args, success=True):
            process = subprocess.run([str(launcher), *args], cwd=work, env=env,
                                     capture_output=True, encoding="utf-8", timeout=120)
            if success and process.returncode:
                raise RuntimeError(f"CLI exited with {process.returncode}: {process.stderr}")
            return process

        if online:
            result = call("--help", success=False)
            if not result.returncode or result.stdout or "安装运行环境" not in result.stderr:
                raise ValueError("Empty runtime did not provide installation guidance on stderr")
            return
        if "infer" not in call("--help").stdout:
            raise ValueError("Upstream command help is missing")
        registered = json.loads(call("list", "--user-only", "--json").stdout)
        if [row["name"] for row in registered] != ["registry-smoke"]:
            raise ValueError("The CLI did not read the Studio model registry")
        catalog = json.loads(call("list", "--json").stdout)
        model = next(row["name"] for row in catalog if row.get("source") != "user")
        for args, root in [((), models), (("--model-dir", "override folder"), Path("override folder"))]:
            resolved = json.loads(call("info", model, *args).stdout)
            if not Path(resolved["model_path"]).is_relative_to(root):
                raise ValueError("The CLI returned an unexpected model location")
        result = call("--invalid-pymss-option", success=False)
        if result.returncode != 2 or result.stdout:
            raise ValueError("The CLI did not propagate the upstream argument error")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", type=Path)
    parser.add_argument("--online", action="store_true")
    options = parser.parse_args()
    verify(options.stage, options.online)
