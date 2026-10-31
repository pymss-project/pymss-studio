"""Plugin manifest parsing for the pymss plugin protocol (docs/plugin-protocol.md).

Reads ``[tool.pymss.plugin]`` from a plugin's pyproject.toml. Discovery only —
no plugin code is executed here. Every plugin must declare a v2 manifest;
host-specific tables such as ``[tool.pymss.studio]`` are ignored.
"""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .._identifiers import OPERATION_ID_RE, PLUGIN_ID_RE

_SINGLE_CONSTRAINT = r"(==|>=|<=|>|<|~)?\s*[0-9][0-9A-Za-z.\-+]*\s*"
VERSION_CONSTRAINT_RE = re.compile(rf"^\s*{_SINGLE_CONSTRAINT}(,\s*{_SINGLE_CONSTRAINT})*$")
API_VERSION = 2
MIGRATION_HINT = "see docs/plugin-protocol.md#migrating-from-v1"


class ManifestError(ValueError):
    """Raised when a plugin manifest is invalid."""


def _check_constraint(value: Any, field_name: str) -> str:
    text = ",".join(part.strip() for part in str(value or "").split(","))
    if not VERSION_CONSTRAINT_RE.match(text):
        raise ManifestError(f"{field_name} must be PEP 440-style version constraints, got {value!r}")
    return text


@dataclass
class GraphContribution:
    id: str
    file: Path
    operation: str


@dataclass
class NodeContribution:
    """A workflow-graph node type contributed by the plugin.

    Pure data: hosts render the node (palette, ports, widgets) from this
    declaration without any plugin-side frontend code.
    """

    type: str
    title: str
    category: str
    inputs: tuple[dict, ...]
    outputs: tuple[dict, ...]
    widgets: tuple[dict, ...]
    description: str = ""

    def validate_widgets(self, values: Any) -> None:
        """Validate declared widget slots without changing legacy slot order.

        Short arrays and null slots use the declared default, as graph.widget()
        does. Extra slots remain readable by custom executors for compatibility.
        """
        if values is None:
            values = ()
        if not isinstance(values, (list, tuple)):
            raise ValueError(f"node {self.type!r} widgets_values must be an array")
        for index, widget in enumerate(self.widgets):
            value = values[index] if index < len(values) else None
            if value is None:
                value = widget.get("default")
            if value is None:
                continue
            _validate_widget_value(value, widget, f"node {self.type!r} widget {widget['name']!r}")


@dataclass
class OperationDecl:
    """An operation the plugin promises to register from ``setup()``."""

    id: str
    title: str


@dataclass
class PluginManifest:
    """Parsed [tool.pymss.plugin] section; validated, paths resolved."""

    plugin_id: str
    api_version: int
    entry: str | None
    requires_pymss: str | None
    version: str | None
    dependencies: dict[str, str] = field(default_factory=dict)
    operations: list[OperationDecl] = field(default_factory=list)
    graphs: list[GraphContribution] = field(default_factory=list)
    nodes: list[NodeContribution] = field(default_factory=list)

    @property
    def operation_ids(self) -> set[str]:
        return {operation.id for operation in self.operations}

    @property
    def node_types(self) -> set[str]:
        return {node.type for node in self.nodes}

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "api_version": self.api_version,
            "requires_pymss": self.requires_pymss,
            "dependencies": dict(self.dependencies),
            "operations": [{"id": o.id, "title": o.title} for o in self.operations],
            "contributions": {
                "graphs": [{"id": g.id, "file": g.file.name, "operation": g.operation} for g in self.graphs],
                "nodes": [
                    {
                        "type": n.type, "title": n.title, "category": n.category,
                        "inputs": [dict(i) for i in n.inputs], "outputs": [dict(o) for o in n.outputs],
                        "widgets": [dict(w) for w in n.widgets], "description": n.description,
                    }
                    for n in self.nodes
                ],
            },
        }


def _resolve_inside(plugin_dir: Path, raw: Any, field_name: str, *, must_exist: bool = False) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestError(f"{field_name} must be a non-empty relative path")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise ManifestError(f"{field_name} must be relative to the plugin directory")
    resolved = (plugin_dir / candidate).resolve()
    root = plugin_dir.resolve()
    if resolved != root and root not in resolved.parents:
        raise ManifestError(f"{field_name} escapes the plugin directory: {raw!r}")
    if must_exist and not resolved.is_file():
        raise ManifestError(f"{field_name} does not exist inside the plugin: {raw!r}")
    return resolved


def _parse_graphs(plugin_dir: Path, contributions: Any) -> list[GraphContribution]:
    items = contributions.get("graphs", [])
    if not isinstance(items, list):
        raise ManifestError("contributions.graphs must be an array of tables")
    graphs: list[GraphContribution] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ManifestError("each graphs entry must be a table")
        graph_id = str(item.get("id") or "").strip()
        if not PLUGIN_ID_RE.fullmatch(graph_id):
            raise ManifestError(f"graphs entry id {item.get('id')!r} is invalid")
        if graph_id in seen:
            raise ManifestError(f"duplicate graphs id {graph_id!r}")
        seen.add(graph_id)
        operation = str(item.get("operation") or "").strip()
        if not operation:
            raise ManifestError(f"graphs entry {graph_id!r} must reference an operation")
        graphs.append(GraphContribution(
            id=graph_id,
            file=_resolve_inside(plugin_dir, item.get("file"), f"graphs[{graph_id}].file", must_exist=True),
            operation=operation,
        ))
    return graphs


NODE_PORT_TYPES = {"AUDIO", "STRING", "COMBO", "BOOLEAN", "PYMSS_MSS_PARAMS", "PYMSS_VR_PARAMS"}
NODE_WIDGET_TYPES = {"text", "number", "toggle", "combo", "slider"}
NODE_TYPE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_widget_value(value: Any, widget: dict, label: str) -> None:
    kind = widget["type"]
    if kind in {"number", "slider"}:
        if not _finite_number(value):
            raise ValueError(f"{label} must be a finite number")
        if "min" in widget and value < widget["min"]:
            raise ValueError(f"{label} is below minimum {widget['min']}")
        if "max" in widget and value > widget["max"]:
            raise ValueError(f"{label} is above maximum {widget['max']}")
    elif kind == "toggle":
        if not isinstance(value, bool):
            raise ValueError(f"{label} must be a boolean")
    elif not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if kind == "combo" and "options" in widget and value not in widget["options"]:
        raise ValueError(f"{label} must be one of {widget['options']}")


def _parse_node_ports(value: Any, field_name: str) -> tuple[dict, ...]:
    if not isinstance(value, list):
        raise ManifestError(f"{field_name} must be an array of tables")
    ports = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ManifestError(f"{field_name} entries must be tables")
        name = str(item.get("name") or "").strip()
        ptype = str(item.get("type") or "").strip().upper()
        if not name:
            raise ManifestError(f"{field_name} entry is missing 'name'")
        if name in seen:
            raise ManifestError(f"{field_name} contains duplicate name {name!r}")
        seen.add(name)
        if ptype not in NODE_PORT_TYPES:
            raise ManifestError(f"{field_name} entry {name!r} has unsupported port type {item.get('type')!r} (supported: {sorted(NODE_PORT_TYPES)})")
        entry = {"name": name, "type": ptype}
        if item.get("widget") is not None:
            entry["widget"] = str(item["widget"])
        ports.append(entry)
    return tuple(ports)


def _parse_node_widgets(value: Any, field_name: str) -> tuple[dict, ...]:
    if not isinstance(value, list):
        raise ManifestError(f"{field_name} must be an array of tables")
    widgets = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ManifestError(f"{field_name} entries must be tables")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ManifestError(f"{field_name} entry is missing 'name'")
        if name in seen:
            raise ManifestError(f"{field_name} contains duplicate name {name!r}")
        seen.add(name)
        wtype = str(item.get("type") or "text").strip().lower()
        if wtype not in NODE_WIDGET_TYPES:
            raise ManifestError(f"{field_name} entry {name!r} has unsupported widget type {wtype!r} (supported: {sorted(NODE_WIDGET_TYPES)})")
        widget: dict = {"name": name, "type": wtype}
        if item.get("default") is not None:
            widget["default"] = item["default"]
        if item.get("min") is not None:
            widget["min"] = item["min"]
        if item.get("max") is not None:
            widget["max"] = item["max"]
        if item.get("step") is not None:
            widget["step"] = item["step"]
        for constraint in ("min", "max", "step"):
            if constraint in widget and (wtype not in {"number", "slider"} or not _finite_number(widget[constraint])):
                raise ManifestError(f"{field_name} entry {name!r} {constraint} must be a finite numeric constraint")
        if "min" in widget and "max" in widget and widget["min"] > widget["max"]:
            raise ManifestError(f"{field_name} entry {name!r} min must not exceed max")
        if "step" in widget and widget["step"] <= 0:
            raise ManifestError(f"{field_name} entry {name!r} step must be positive")
        if item.get("options") is not None:
            options = item["options"]
            if not isinstance(options, list):
                raise ManifestError(f"{field_name} entry {name!r} options must be an array")
            widget["options"] = [str(option) for option in options]
        if "default" in widget:
            try:
                _validate_widget_value(widget["default"], widget, f"{field_name} entry {name!r} default")
            except ValueError as exc:
                raise ManifestError(str(exc)) from exc
        widgets.append(widget)
    return tuple(widgets)


def _parse_nodes(contributions: Any) -> list[NodeContribution]:
    items = contributions.get("nodes", [])
    if not isinstance(items, list):
        raise ManifestError("contributions.nodes must be an array of tables")
    nodes: list[NodeContribution] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ManifestError("each nodes entry must be a table")
        node_type = str(item.get("type") or "").strip()
        if not NODE_TYPE_RE.fullmatch(node_type):
            raise ManifestError(f"nodes entry type {item.get('type')!r} is invalid")
        if node_type in seen:
            raise ManifestError(f"duplicate nodes type {node_type!r}")
        seen.add(node_type)
        nodes.append(NodeContribution(
            type=node_type,
            title=str(item.get("title") or node_type),
            category=str(item.get("category") or "pymss/plugins"),
            inputs=_parse_node_ports(item.get("inputs", []), f"nodes[{node_type}].inputs"),
            outputs=_parse_node_ports(item.get("outputs", []), f"nodes[{node_type}].outputs"),
            widgets=_parse_node_widgets(item.get("widgets", []), f"nodes[{node_type}].widgets"),
            description=str(item.get("description") or ""),
        ))
    return nodes


def _parse_operations(plugin_id: str, items: Any) -> list[OperationDecl]:
    if not isinstance(items, list):
        raise ManifestError("operations must be an array of tables")
    operations: list[OperationDecl] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ManifestError("each operations entry must be a table")
        operation_id = str(item.get("id") or "").strip()
        if not OPERATION_ID_RE.fullmatch(operation_id):
            raise ManifestError(f"operation id {item.get('id')!r} must be a dot-namespaced lowercase identifier")
        if not operation_id.startswith(plugin_id + "."):
            raise ManifestError(f"operation id {item.get('id')!r} must start with {plugin_id + '.'!r}")
        if operation_id in seen:
            raise ManifestError(f"duplicate operation id {operation_id!r}")
        seen.add(operation_id)
        operations.append(OperationDecl(id=operation_id, title=str(item.get("title") or operation_id)))
    return operations


def read_manifest(plugin_dir: Path) -> PluginManifest:
    """Parse and validate the plugin manifest.

    Raises ManifestError for missing or malformed manifests — a broken manifest
    blocks the plugin at load time instead of failing later during import.
    """
    pyproject = plugin_dir / "pyproject.toml"
    if not pyproject.is_file():
        raise ManifestError(f"missing pyproject.toml with [tool.pymss.plugin]; {MIGRATION_HINT}")
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeError) as exc:
        raise ManifestError(f"cannot read pyproject.toml: {exc}") from exc
    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        raise ManifestError("tool must be a table")
    pymss_section = tool.get("pymss", {})
    if not isinstance(pymss_section, dict):
        raise ManifestError("tool.pymss must be a table")
    section = pymss_section.get("plugin")
    if not isinstance(section, dict) or not section:
        raise ManifestError(f"missing [tool.pymss.plugin]; {MIGRATION_HINT}")

    plugin_id = str(section.get("id") or "").strip()
    if not PLUGIN_ID_RE.fullmatch(plugin_id):
        raise ManifestError(f"plugin id {section.get('id')!r} must be lowercase dot/dash/underscore identifiers")
    api_version = section.get("api_version")
    if api_version != API_VERSION or isinstance(api_version, bool):
        raise ManifestError(f"api_version must be {API_VERSION}, got {api_version!r}; {MIGRATION_HINT}")

    dependencies: dict[str, str] = {}
    raw_deps = section.get("dependencies", {})
    if not isinstance(raw_deps, dict):
        raise ManifestError("plugin dependencies must be a table of plugin-id -> constraint")
    for dep_id, constraint in raw_deps.items():
        normalized = str(dep_id).strip()
        if not PLUGIN_ID_RE.fullmatch(normalized):
            raise ManifestError(f"dependency id {dep_id!r} is invalid")
        dependencies[normalized] = _check_constraint(constraint, f"dependencies[{dep_id}]")

    entry = section.get("entry")
    if entry is not None and (not isinstance(entry, str) or ":" not in entry):
        raise ManifestError(f"entry must be 'module:attr', got {entry!r}")

    contributions = section.get("contributions", {})
    if not isinstance(contributions, dict):
        raise ManifestError("contributions must be a table")

    project = data.get("project", {})
    return PluginManifest(
        plugin_id=plugin_id,
        api_version=api_version,
        entry=str(entry) if entry else None,
        requires_pymss=_check_constraint(section["requires_pymss"], "requires_pymss")
        if section.get("requires_pymss") else None,
        version=str(project.get("version")) if isinstance(project, dict) and project.get("version") else None,
        dependencies=dependencies,
        operations=_parse_operations(plugin_id, section.get("operations", [])),
        graphs=_parse_graphs(plugin_dir, contributions),
        nodes=_parse_nodes(contributions),
    )


def parse_entry_point(entry: str | None) -> tuple[str, str]:
    """Resolve the manifest entry to (module, attr). Defaults to ``__init__:setup``."""
    if entry:
        module, _, attr = entry.partition(":")
        return module.strip(), (attr.strip() or "setup")
    return "__init__", "setup"
