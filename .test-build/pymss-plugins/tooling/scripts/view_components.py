"""Read package-owned Studio component entries without a host-side tool registry."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if not __package__:
    sys.path.insert(0, str(ROOT))

from tooling.scripts.studio_requirements import studio_requirements  # noqa: E402


def component_entries(root: Path = ROOT) -> list[dict]:
    entries = []
    for project in sorted((root / "plugins").glob("*/pyproject.toml")):
        data = tomllib.loads(project.read_text(encoding="utf-8"))
        studio = data.get("tool", {}).get("pymss", {}).get("studio", {})
        if not isinstance(studio, dict):
            raise ValueError(f"Invalid Studio metadata: {project}")
        for view in studio.get("views", []):
            if not view.get("component") and "build_script" not in studio:
                continue

            def local_path(value: str, *, source=False, script=False) -> Path:
                if not isinstance(value, str) or not value:
                    raise ValueError("Build paths must be non-empty strings")
                relative = Path(value)
                path = (project.parent / relative).resolve()
                if (
                    relative.is_absolute()
                    or relative.drive
                    or ".." in relative.parts
                    or not path.is_relative_to(project.parent.resolve())
                ):
                    raise ValueError(
                        f"Component paths must stay inside plugin {project.parent.name}"
                    )
                if source and (path.suffix != ".vue" or not path.is_file()):
                    raise ValueError(f"Missing Vue component: {value}")
                if script and (path.suffix not in {".mjs", ".js"} or not path.is_file()):
                    raise ValueError(f"Missing JavaScript build script: {value}")
                if not source and not script and path.suffix != ".html":
                    raise ValueError("A component entry must be an HTML output file")
                return path

            entries.append(
                {
                    "plugin": project.parent.name,
                    "view": view["id"],
                    "fileTool": view.get("file_tool"),
                    "title": view["title"],
                    "component": str(local_path(view["component"], source=True)) if view.get("component") else None,
                    "entry": str(local_path(view["entry"])),
                    "operation": view.get("operation", ""),
                    "buildScript": str(local_path(studio["build_script"], script=True)) if "build_script" in studio else None,
                    "packageRoot": str(project.parent),
                    "requirements": studio_requirements(studio),
                }
            )
    return entries


if __name__ == "__main__":
    print(json.dumps(component_entries(), ensure_ascii=False))
