"""Plugin discovery and loading.

Plugins are directories with a v2 ``[tool.pymss.plugin]`` manifest under one or
more plugin roots. The default root is ``~/.pymss/plugins`` (or
``$PYMSS_PLUGINS_DIR``); hosts such as Studio pass their own roots explicitly.

A single plugin failing to load never blocks others: failures are recorded and
surfaced via ``pymss plugins list``.
"""

from __future__ import annotations

import logging
import os
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

logger = logging.getLogger(__name__)

DEFAULT_PLUGINS_DIR = Path.home() / ".pymss" / "plugins"


def get_plugins_dir() -> Path:
    """Resolve the default plugins directory from env var or default."""
    env = os.environ.get("PYMSS_PLUGINS_DIR")
    if env:
        return Path(env)
    return DEFAULT_PLUGINS_DIR


@dataclass
class PluginLoadResult:
    """Result of loading one plugin."""

    name: str
    source: str  # "folder:<path>"
    loaded: bool
    error: str | None = None
    path: str | None = None
    plugin_id: str | None = None


@dataclass
class PluginLoadReport:
    """Aggregate load report across all plugins in a bootstrap run."""

    results: list[PluginLoadResult] = field(default_factory=list)
    bootstrapped: bool = False

    @property
    def loaded_names(self) -> list[str]:
        return [r.name for r in self.results if r.loaded]

    @property
    def failed(self) -> list[PluginLoadResult]:
        return [r for r in self.results if not r.loaded]


_LAST_REPORT: PluginLoadReport | None = None


def get_last_report() -> PluginLoadReport | None:
    """Return the most recent bootstrap report (for `pymss plugins list`)."""
    return _LAST_REPORT


def bootstrap(
    force: bool = False, roots: Sequence[Path] | None = None,
    *, validate_plugin: Callable[[Path], None] | None = None,
    activation_context: Callable[[Path], AbstractContextManager[None]] | None = None,
) -> PluginLoadReport:
    """Discover and load all plugins into the default runtime.

    Idempotent: subsequent calls return the cached report unless ``force=True``.
    Host validation and activation context must be supplied on the first load.
    """
    global _LAST_REPORT
    if _LAST_REPORT is not None and _LAST_REPORT.bootstrapped and not force:
        return _LAST_REPORT

    from ..operations import default_runtime

    report = PluginLoadReport(bootstrapped=True)
    for row in default_runtime().load_plugins(
        roots, validate_plugin=validate_plugin, activation_context=activation_context,
    ):
        path = str(row.get("path") or "")
        report.results.append(PluginLoadResult(
            name=str(row.get("name") or ""),
            source=f"folder:{path}",
            loaded=bool(row.get("loaded")),
            error=row.get("error"),
            path=path,
            plugin_id=row.get("plugin_id"),
        ))
    _LAST_REPORT = report
    return report


def ensure_plugins_loaded(
    *, validate_plugin: Callable[[Path], None] | None = None,
    activation_context: Callable[[Path], AbstractContextManager[None]] | None = None,
) -> PluginLoadReport:
    """Load user plugins once per process before an entry point uses them.

    Business entry points (infer / workflow / comfy run / serve / ops) call
    this instead of ``bootstrap()`` directly: same idempotent semantics, but
    named for "prepare the plugin state this process depends on" rather than
    "produce a report for the plugins command". Failures are already isolated
    per plugin; the report is retrievable via ``get_last_report()``.
    """
    return bootstrap(validate_plugin=validate_plugin, activation_context=activation_context)


def reset() -> None:
    """Clear the cached bootstrap report (mainly for tests)."""
    global _LAST_REPORT
    _LAST_REPORT = None


__all__ = [
    "DEFAULT_PLUGINS_DIR",
    "PluginLoadResult",
    "PluginLoadReport",
    "bootstrap",
    "ensure_plugins_loaded",
    "get_plugins_dir",
    "get_last_report",
    "reset",
]
