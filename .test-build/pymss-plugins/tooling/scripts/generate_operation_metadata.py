"""Export v2 operation descriptions using an installed pymss runtime."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[2]
if not __package__:
    sys.path.insert(0, str(ROOT))

from tooling.scripts.operation_metadata import build_snapshot, render_snapshot, source_digest  # noqa: E402


def render_metadata(plugins_dir: Path, selected: set[str] | None = None) -> dict[Path, str]:
    # Exporting is a trusted build step, never part of browsing installed tools.
    from pymss.operations import create_runtime

    plugins_dir = plugins_dir.resolve()
    projects = {}
    for path in sorted(plugins_dir.glob("*/pyproject.toml")):
        try:
            project = tomllib.loads(path.read_text(encoding="utf-8"))
            project["tool"]["pymss"]["plugin"]["id"]
        except (OSError, ValueError, KeyError, TypeError):
            if not selected or path.parent.name in selected:
                raise
            continue
        projects[path.parent] = project
    requested = {
        directory: project for directory, project in projects.items()
        if not selected or directory.name in selected or project["tool"]["pymss"]["plugin"]["id"] in selected
    }
    known = {value for directory, project in projects.items() for value in (directory.name, project["tool"]["pymss"]["plugin"]["id"])}
    if selected and selected - known:
        raise ValueError("Unknown plugin: " + ", ".join(sorted(selected - known)))
    before = {directory: source_digest(directory) for directory in requested}
    allowed = set(requested)
    if selected:
        by_id = {project["tool"]["pymss"]["plugin"]["id"]: directory for directory, project in projects.items()}
        pending = list(allowed)
        while pending:
            directory = pending.pop()
            for dependency in projects[directory]["tool"]["pymss"]["plugin"].get("dependencies", {}):
                target = by_id.get(dependency)
                if target is not None and target not in allowed:
                    allowed.add(target)
                    pending.append(target)

    def validate(directory: Path) -> None:
        if selected and directory not in allowed:
            raise ValueError("Plugin is outside the selected metadata export")

    with redirect_stdout(sys.stderr):
        runtime = create_runtime()
        reports = runtime.load_plugins([plugins_dir], validate_plugin=validate if selected else None)
        allowed_ids = {projects[directory]["tool"]["pymss"]["plugin"]["id"] for directory in allowed}
        failed = [row for row in reports if row.get("status") != "activated" and (not selected or row.get("plugin_id") in allowed_ids)]
        if failed:
            raise ValueError("Plugin registration failed: " + "; ".join(str(row.get("error") or row.get("plugin_id")) for row in failed))
        operations = {spec["id"]: runtime.describe_operation(spec["id"]) for spec in runtime.list_operations()}
    output = {}
    for directory, project in requested.items():
        ids = [row["id"] for row in project["tool"]["pymss"]["plugin"].get("operations", [])]
        missing = set(ids) - operations.keys()
        if missing:
            raise ValueError("Missing operation registrations: " + ", ".join(sorted(missing)))
        snapshot = build_snapshot(directory, project, [operations[operation] for operation in ids])
        if snapshot["sourceDigest"] != before[directory]:
            raise ValueError(f"Plugin sources changed during export: {directory.name}")
        output[directory / "operations.json"] = render_snapshot(snapshot)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugins-dir", type=Path, default=ROOT / "plugins")
    parser.add_argument("--plugin", action="append", default=[], help="Plugin directory name or manifest id; repeatable")
    parser.add_argument("--check", action="store_true", help="Check committed descriptions without changing files")
    args = parser.parse_args(argv)
    try:
        rendered = render_metadata(args.plugins_dir, set(args.plugin))
        stale = []
        for path, content in rendered.items():
            if path.is_symlink():
                raise ValueError(f"Operation metadata cannot overwrite a symbolic link: {path}")
            if args.check:
                if not path.is_file() or path.read_text(encoding="utf-8") != content:
                    stale.append(str(path))
            else:
                path.write_text(content, encoding="utf-8", newline="\n")
        if stale:
            print("Operation metadata needs regeneration:\n" + "\n".join(stale), file=sys.stderr)
            return 1
        print(f"{'Checked' if args.check else 'Generated'} operation metadata for {len(rendered)} plugins")
        return 0
    except (ImportError, OSError, ValueError, KeyError) as error:
        print(f"Operation metadata export failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
