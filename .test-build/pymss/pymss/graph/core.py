"""Unified DAG execution core for pymss workflows.

This module is the single execution engine shared by:

* :mod:`pymss.graph.comfy_loader` — loads native comfy-mss JSON graphs.
* :mod:`pymss.graph.yaml_compiler` — compiles the legacy linear YAML workflow into a DAG.

Design constraints:

* Zero dependency on the ComfyUI runtime. We only parse the comfy-mss JSON
  structure and re-implement node semantics on top of pymss' own
  :class:`pymss.separator.MSSeparator`, :mod:`pymss.audio_io`, and the
  capability pool from :mod:`pymss.plugins`.
* Node executors consume built-in capabilities (invert_phase, normalize_peak,
  ensemble, {fmt}_encode, ...) rather than reimplementing DSP — pymss eats its
  own dogfood.
* ``OUTPUT_NODE`` s (e.g. ``pymss_save_audio``) are the only nodes that write
  to disk; everything else passes artifacts around in memory.

The node registry is the plugin system's (``pymss.plugins``); graph nodes
register through :func:`pymss.plugins.register_node` with an optional
``signature`` factory and the executor reads them back via
:func:`get_node_type`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from asyncio import CancelledError as AsyncCancelledError
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from ..plugins.registry import _REGISTRY as _PLUGIN_REGISTRY

PROGRESS_EVENT_VERSION = 1
EXECUTION_EVENT_VERSION = 1


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


@dataclass
class AudioArtifact:
    """A single in-memory audio buffer.

    ``audio`` is stored channel-first as ``[channels, samples]`` float32.
    """

    audio: np.ndarray
    sample_rate: int
    source_path: str = ""
    stem_name: str = ""
    channel_layout: str | None = None

    def __post_init__(self) -> None:
        arr = np.asarray(self.audio, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr[None, :]
        if arr.ndim != 2:
            raise ValueError(f"AudioArtifact expects 1D/2D audio, got shape {arr.shape}")
        self.audio = np.ascontiguousarray(arr, dtype=np.float32)
        self.sample_rate = int(self.sample_rate)
        if self.channel_layout is not None:
            from av import AudioLayout

            layout = AudioLayout(self.channel_layout)
            if len(layout.channels) != arr.shape[0]:
                raise ValueError("Audio channel layout does not match the waveform channel count.")
            self.channel_layout = layout.name
        elif arr.shape[0] in (1, 2):
            self.channel_layout = "mono" if arr.shape[0] == 1 else "stereo"


@dataclass
class StringArtifact:
    value: str


@dataclass
class ParamsArtifact:
    """Opaque parameter dict produced by ``pymss_mss_params`` / ``pymss_vr_params``.

    Carries the ``params_type`` tag ("mss" or "vr") so separate nodes can reject
    a mismatched params source instead of silently misinterpreting it.
    """

    params: dict[str, Any]
    params_type: str  # "mss" | "vr"


Artifact = Any  # AudioArtifact | StringArtifact | ParamsArtifact | list[...]

# Port type tags.
AUDIO = "AUDIO"
STRING = "STRING"
MSS_PARAMS = "PYMSS_MSS_PARAMS"
VR_PARAMS = "PYMSS_VR_PARAMS"


# ---------------------------------------------------------------------------
# Node signatures
# ---------------------------------------------------------------------------


@dataclass
class PortSpec:
    name: str
    type: str
    shape: int | None = None
    label: str | None = None


@dataclass
class NodeSignature:
    """Static description of a node type."""

    inputs: list[PortSpec]
    output_names: list[str] | None
    output_types: list[str] | None
    is_output_node: bool = False
    is_list: bool = False


NodeExecute = Callable[["NodeContext", dict[str, Artifact]], "NodeResult"]


@dataclass
class NodeTypeInfo:
    """A registered node type: signature factory + executor."""

    type: str
    signature: Callable[["DAGNode"], NodeSignature]
    execute: NodeExecute


# ---------------------------------------------------------------------------
# Node registry (delegates to the plugin system's registry)
# ---------------------------------------------------------------------------


def register_node(
    node_type: str,
    *,
    signature: Callable[["DAGNode"], NodeSignature],
    execute: NodeExecute,
) -> None:
    """Register a graph node type.

    Stored in the plugin system's registry so graph nodes and plugin nodes
    share one pool. The signature factory is carried in the registration's
    ``signature`` field; the executor in ``func``.
    """
    _PLUGIN_REGISTRY.register_node(
        node_type,
        execute,
        source="builtin",
        signature=signature,
    )


def register_alias(alias: str, canonical: str) -> None:
    """Register ``alias`` as another name for an already-registered ``canonical`` type.

    comfy-mss graphs in the wild use inconsistent node-type naming: comfy-mss
    upstream registers ``mss_separate``, but graphs exported from some
    front-ends carry a ``pymss_`` prefix. Aliases let both run.
    """
    target = _PLUGIN_REGISTRY.nodes.get(canonical)
    if target is None:
        raise KeyError(f"cannot alias {alias!r}: canonical type {canonical!r} is not registered")
    _PLUGIN_REGISTRY.register_node(
        alias, target.func, source=target.source,
        description=target.description, signature=target.signature,
        saves_output=target.saves_output,
    )


class UnknownNodeError(KeyError):
    """Raised when a graph references a node type that has no executor."""

    def __init__(self, node_type: str) -> None:
        self.node_type = node_type
        super().__init__(node_type)


def get_node_type(node_type: str) -> NodeTypeInfo:
    """Look up a node type, triggering built-in registration on first call."""
    _load_builtin_nodes()
    reg = _PLUGIN_REGISTRY.nodes.get(node_type)
    if reg is None:
        raise UnknownNodeError(node_type)
    # Signature defaults to an empty signature if the registration is a plain
    # plugin node (no graph signature); graph nodes carry their own.
    sig = reg.signature
    if sig is None:
        sig = lambda node: NodeSignature(inputs=[], output_names=[], output_types=[])  # noqa: E731
    return NodeTypeInfo(type=node_type, signature=sig, execute=reg.func)


# ---------------------------------------------------------------------------
# Graph structure
# ---------------------------------------------------------------------------


@dataclass
class DAGLink:
    link_id: int
    source_node_id: object
    source_slot: int
    target_node_id: object
    target_slot: int
    type: str


@dataclass
class DAGNode:
    """A node instance inside a DAG."""

    id: object
    type: str
    inputs: list[DAGLink | None] = field(default_factory=list)
    signature: NodeSignature = field(default_factory=lambda: NodeSignature(inputs=[], output_names=[], output_types=[]))
    data: dict[str, Any] = field(default_factory=dict)
    title: str = ""
    _explicit_order: int | None = None


@dataclass(frozen=True)
class DAGOutputRecord:
    """A single artifact written to disk by a graph output node."""

    path: str
    stem: str = ""
    node_id: object = None
    node_type: str = ""
    sample_rate: int = 0
    format: str = ""
    source_path: str = ""
    kind: str = ""
    media_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "stem": self.stem,
            "node_id": str(self.node_id) if self.node_id is not None else "",
            "node_type": self.node_type,
            "sample_rate": self.sample_rate,
            "format": self.format,
            "source_path": self.source_path,
            "name": Path(self.path).name,
            **({"kind": self.kind, "media_type": self.media_type} if self.kind else {}),
        }


class DAGExecutionResult(list):
    """Execution result list that preserves backwards-compatibility.

    Acts as a standard list[str] of file paths for legacy callers,
    while exposing structured metadata via `.records` and `.by_stem`.
    """

    def __init__(self, saved_paths: list[str], records: list[DAGOutputRecord] | None = None):
        super().__init__(saved_paths)
        self.records: list[DAGOutputRecord] = list(records or [])
        self._by_stem: dict[str, DAGOutputRecord] = {r.stem: r for r in self.records if r.stem}

    def by_stem(self, stem: str) -> DAGOutputRecord | None:
        return self._by_stem.get(stem)


@dataclass
class NodeResult:
    """What a node executor returns."""

    outputs: dict[int, Artifact] = field(default_factory=dict)
    saved_paths: list[str] = field(default_factory=list)
    saved_records: list[DAGOutputRecord] = field(default_factory=list)


@dataclass
class NodeContext:
    """Per-execution services handed to every node executor."""

    output_dir: Path
    logger: Any
    debug: bool
    progress_callback: Callable[[int, int, str | None], None] | None
    separator_cache: "SeparatorCache"
    input_path: str | None = None
    # Named runtime inputs: logical name -> file path. LoadAudio-family nodes
    # resolve their input_name widget against this mapping (see _execute_load_audio).
    inputs: dict[str, str] = field(default_factory=dict)
    strict: bool = True
    model_dir: str | os.PathLike | None = None
    download: bool = False
    source: str = "modelscopes"
    endpoint: str | None = None
    device: str | None = None
    output_format: str | None = None
    audio_params: dict[str, Any] = field(default_factory=dict)
    nodes_by_id: dict[object, "DAGNode"] = field(default_factory=dict)
    current_node_id: object | None = None
    name_prefix: str = ""
    progress_event_callback: Callable[[dict[str, Any]], None] | None = None
    node_index: int = 0
    node_count: int = 0
    _overall_fraction: float = 0.0
    execution_event_callback: Callable[[dict[str, Any]], None] | None = None
    _node_fraction: float = 0.0
    _execution_callback_failed: bool = False
    host_request: Callable[[Any], Any] | None = None

    def _emit_execution_event(self, event_type: str, node: DAGNode | None = None,
                              message: str = "", **details: Any) -> None:
        if self.execution_event_callback is None:
            return
        event = {
            "event": event_type, "version": EXECUTION_EVENT_VERSION,
            "node_id": str(node.id) if node is not None else None,
            "node_type": node.type if node is not None else None,
            "node_fraction": self._node_fraction if node is not None else None,
            "overall_fraction": self._overall_fraction,
            "node_index": self.node_index if node is not None else None,
            "node_count": self.node_count, "message": message,
            **details,
        }
        self._execution_callback_failed = False
        try:
            self.execution_event_callback(event)
        except BaseException:
            # Observer errors abort the call, but are not node-execution errors.
            self._execution_callback_failed = True
            raise

    def report_progress(self, node: DAGNode, fraction: float, message: str, *,
                        audio_index: int | None = None, audio_count: int | None = None,
                        done: int | float | None = None, total: int | float | None = None,
                        unit: str | None = None, _event_type: str = "node_progress") -> None:
        """Report structured graph progress without changing legacy callbacks."""
        if self.progress_event_callback is None and self.execution_event_callback is None:
            return
        fraction = min(1.0, max(0.0, fraction))
        self._node_fraction = fraction
        self._overall_fraction = max(self._overall_fraction, (self.node_index + fraction) / max(1, self.node_count))
        event = {"node_id": str(node.id), "node_type": node.type, "overall_fraction": self._overall_fraction,
                 "node_fraction": fraction, "message": message}
        if audio_index is not None:
            event.update(audio_index=audio_index, audio_count=audio_count)
        if done is not None and total is not None:
            event.update(done=done, total=total, unit=unit)
        if self.progress_event_callback is not None:
            self.progress_event_callback(dict(event))
        # A separate observer keeps lifecycle events out of the version-1
        # progress stream, whose consumers may interpret fraction=1 as done.
        self._emit_execution_event(_event_type, node, **{key: value for key, value in event.items()
                                                       if key not in {"node_id", "node_type"}})

    def require(self, capability_name: str) -> Callable[..., Any]:
        """Look up a capability by name (delegates to the plugin registry)."""
        return _PLUGIN_REGISTRY.get_capability(capability_name)


# ---------------------------------------------------------------------------
# Separator cache
# ---------------------------------------------------------------------------


class SeparatorCache:
    """Reuse a bounded number of loaded separators within a run.

    Keys include constructor options so different model configurations do not
    share an instance.

    One separator is kept by default. Eviction closes the least recently used
    separator before the next model is loaded. Pass ``max_entries=None`` to
    explicitly retain the historical unbounded cache.
    """

    def __init__(self, factory: Callable[..., Any] | None = None, *, max_entries: int | None = 1) -> None:
        if max_entries is not None and (isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1):
            raise ValueError("max_entries must be a positive integer or None")
        self._entries: dict[str, Any] = {}
        self._pending_close: Any = None
        self._factory = factory or self._default_factory
        self.max_entries = max_entries

    @staticmethod
    def _default_factory(**kwargs: Any) -> Any:
        from ..separator import MSSeparator

        model_path = kwargs.pop("model_path", None)
        if model_path:
            kwargs.pop("model_dir", None)
            model_type = kwargs.pop("model_type")
            config_path = kwargs.pop("config_path", None)
            return MSSeparator(
                model_type=model_type, model_path=model_path, config_path=config_path, **kwargs
            )
        model_name = kwargs.pop("model_name")
        return MSSeparator.from_model_name(model_name, **kwargs)

    @staticmethod
    def key_for(**kwargs: Any) -> str:
        blob = json.dumps(_make_jsonable(kwargs), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get(self, **kwargs: Any) -> Any:
        if self._pending_close is not None:
            self._close_separator(self._pending_close)
            self._pending_close = None
        key = self.key_for(**kwargs)
        if key in self._entries:
            entry = self._entries.pop(key)
        else:
            while self.max_entries is not None and len(self._entries) >= self.max_entries:
                oldest = next(iter(self._entries))
                self._pending_close = self._entries.pop(oldest)
                self._close_separator(self._pending_close)
                self._pending_close = None
            entry = self._factory(**kwargs)
        self._entries[key] = entry
        return entry

    @staticmethod
    def _close_separator(separator: Any) -> None:
        close = getattr(separator, "close", None)
        if callable(close):
            close()

    def close(self) -> None:
        entries = list(self._entries.values())
        self._entries = {}
        if self._pending_close is not None:
            entries.append(self._pending_close)
            self._pending_close = None
        for separator in entries:
            try:
                self._close_separator(separator)
            except Exception:  # pragma: no cover - best-effort cleanup
                pass

    def __enter__(self) -> "SeparatorCache":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Value helpers (shared by executors)
# ---------------------------------------------------------------------------


def _make_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _make_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_make_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class DAGError(ValueError):
    """Raised for malformed graphs: cycles, dangling links, bad widgets."""


def widget(values: Sequence[Any] | None, index: int, default: Any = None) -> Any:
    """Read a widget value by index, tolerating short lists."""
    if not values:
        return default
    if 0 <= index < len(values):
        value = values[index]
        return default if value is None else value
    return default


def parse_default_int(value: Any, name: str = "value") -> int | None:
    """Parse comfy-mss ``"Default"`` / integer widget strings."""
    text = str(value if value is not None else "").strip()
    if not text or text.lower() == "default":
        return None
    try:
        parsed = int(text)
    except ValueError as exc:
        raise DAGError(f"{name} must be 'Default' or a positive integer, got {value!r}") from exc
    if parsed <= 0:
        raise DAGError(f"{name} must be a positive integer, got {parsed}")
    return parsed


def parse_device_ids(raw: Any) -> list[int]:
    values = [int(item.strip()) for item in str(raw or "0").split(",") if str(item).strip()]
    return values or [0]


def audio_to_numpy(audio: Artifact) -> tuple[np.ndarray, int]:
    """Extract a channel-first ``[channels, samples]`` float32 array."""
    if not isinstance(audio, AudioArtifact):
        raise DAGError("expected an AUDIO input")
    return np.asarray(audio.audio, dtype=np.float32), int(audio.sample_rate)


def numpy_to_audio(
    value: np.ndarray, sample_rate: int, *, stem_name: str = "", source_path: str = "", channel_layout: str | None = None
) -> AudioArtifact:
    arr = np.asarray(value, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr[None, :]
    elif arr.ndim == 2:
        # pymss separators return [samples, channels]; we store channel-first.
        if arr.shape[0] > arr.shape[1] and arr.shape[1] <= 8:
            arr = arr.T
    else:
        raise DAGError(f"unsupported audio shape {arr.shape}")
    return AudioArtifact(arr, int(sample_rate), source_path=source_path, stem_name=stem_name, channel_layout=channel_layout)


def _combined_channel_layout(audios: list[AudioArtifact], channels: int) -> str | None:
    """Keep compatible multichannel positions when combining audio buffers."""
    if channels <= 2:
        return "mono" if channels == 1 else "stereo"
    # A mono input can be broadcast to all channels without reordering them.
    multichannel = [audio for audio in audios if audio.audio.shape[0] != 1]
    if any(audio.audio.shape[0] != channels for audio in multichannel):
        raise DAGError("Cannot combine multichannel audio with different channel counts.")
    layouts = {audio.channel_layout for audio in multichannel}
    if len(layouts) > 1:
        raise DAGError("Cannot combine multichannel audio with different or missing channel layouts.")
    return next(iter(layouts), None)


def string_value(artifact: Artifact) -> str:
    if isinstance(artifact, StringArtifact):
        return artifact.value
    if isinstance(artifact, str):
        return artifact
    if artifact is None:
        return ""
    raise DAGError(f"expected a STRING input, got {type(artifact).__name__}")


_INVALID_FILENAME_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')
_WINDOWS_RESERVED_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})


def safe_filename_part(value: str) -> str:
    text = str(value or "").strip()
    cleaned = _INVALID_FILENAME_CHARS.sub("_", text).strip(" ._")
    if not cleaned:
        return "audio"
    if cleaned.upper().split(".", 1)[0] in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def ctx_node_of(ctx: NodeContext, node_type: str) -> DAGNode:
    """Return the DAGNode currently being executed (looked up by id)."""
    return ctx.nodes_by_id[ctx.current_node_id]


# ---------------------------------------------------------------------------
# Topological ordering
# ---------------------------------------------------------------------------


def topological_order(nodes: Sequence[DAGNode]) -> list[DAGNode]:
    """Return nodes in dependency order (Kahn's algorithm)."""
    by_id: dict[object, DAGNode] = {node.id: node for node in nodes}
    incoming: dict[object, set[int]] = {node.id: set() for node in nodes}
    outgoing: dict[object, list[object]] = {node.id: [] for node in nodes}

    for node in nodes:
        for link in node.inputs:
            if link is None:
                continue
            if link.source_node_id not in by_id:
                raise DAGError(
                    f"node {node.id!r} input references unknown source node {link.source_node_id!r}"
                )
            if link.target_node_id != node.id:
                raise DAGError(
                    f"node {node.id!r} carries a link whose target is {link.target_node_id!r}"
                )
            incoming[node.id].add(link.link_id)
            outgoing[link.source_node_id].append(node.id)

    # Kahn's algorithm. The comfy ``order`` field is the editor's *layout*
    # order (it reflects canvas position, not execution readiness) — use it
    # only as a tie-breaker among ready nodes so visually-upper nodes run
    # first, never as an authoritative execution order (a load node placed
    # below its consumer would otherwise fail validation).
    index = {
        node.id: (
            getattr(node, "_explicit_order", None)
            if getattr(node, "_explicit_order", None) is not None
            else i
        )
        for i, node in enumerate(nodes)
    }
    order: list[DAGNode] = []
    ready = sorted(
        (node.id for node in nodes if not incoming[node.id]), key=lambda nid: index[nid]
    )
    pending = dict(incoming)

    while ready:
        nid = ready.pop(0)
        node = by_id[nid]
        order.append(node)
        for child_id in outgoing[nid]:
            child_links = {
                link.link_id
                for link in by_id[child_id].inputs
                if link is not None and link.source_node_id == nid
            }
            pending[child_id] -= child_links
            if not pending[child_id] and child_id not in {n.id for n in order} and child_id not in ready:
                _bisect_insert(ready, child_id, key=lambda rid: index[rid])

    if len(order) != len(nodes):
        remaining = [by_id[nid] for nid in by_id if nid not in {n.id for n in order}]
        raise DAGError(
            "workflow graph has a cycle involving: " + ", ".join(repr(n.id) for n in remaining)
        )
    return order


def _bisect_insert(
    sorted_list: list[object], value: object, *, key: Callable[[object], int]
) -> None:
    """Insert ``value`` into ``sorted_list`` keeping it sorted by ``key``."""
    kv = key(value)
    lo, hi = 0, len(sorted_list)
    while lo < hi:
        mid = (lo + hi) // 2
        if key(sorted_list[mid]) < kv:
            lo = mid + 1
        else:
            hi = mid
    sorted_list.insert(lo, value)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_dag(
    dag: "DAG",
    *,
    output_dir: str | os.PathLike,
    input_path: str | os.PathLike | None = None,
    inputs: Mapping[str, str | os.PathLike] | None = None,
    logger: Any = None,
    debug: bool = False,
    progress_callback: Callable[[int, int, str | None], None] | None = None,
    progress_event_callback: Callable[[dict[str, Any]], None] | None = None,
    strict: bool = True,
    model_dir: str | os.PathLike | None = None,
    download: bool = False,
    source: str = "modelscopes",
    endpoint: str | None = None,
    device: str | None = None,
    output_format: str | None = None,
    audio_params: dict[str, Any] | None = None,
    separator_cache: SeparatorCache | None = None,
    name_prefix: str = "",
    node_results: dict[int, Any] | None = None,
    execution_event_callback: Callable[[dict[str, Any]], None] | None = None,
    host_request: Callable[[Any], Any] | None = None,
) -> list[str]:
    """Execute a DAG and return the list of files written by output nodes.

    ``node_results`` (optional) receives ``{node_id: NodeResult}`` for every
    executed node — used by graph-backed operations to lift intermediate
    artifacts as named outputs without forcing a Save node into the graph.

    ``execution_event_callback`` observes lifecycle events independently of
    legacy progress callbacks. Execution failures and interruptions still
    propagate to the caller.
    """
    _load_builtin_nodes()
    output_path = Path(output_dir)

    owns_cache = separator_cache is None
    cache = separator_cache if separator_cache is not None else SeparatorCache()
    ctx: NodeContext | None = None
    try:
        ctx = NodeContext(
            output_dir=output_path,
            logger=logger,
            debug=debug,
            progress_callback=progress_callback,
            separator_cache=cache,
            input_path=str(input_path) if input_path else None,
            inputs={str(name): str(path) for name, path in (inputs or {}).items()},
            strict=strict,
            model_dir=model_dir,
            download=download,
            source=source,
            endpoint=endpoint,
            device=device,
            output_format=output_format,
            audio_params=dict(audio_params or {}),
            nodes_by_id={node.id: node for node in dag.nodes},
            name_prefix=name_prefix or "",
            progress_event_callback=progress_event_callback,
            execution_event_callback=execution_event_callback,
            host_request=host_request,
            node_count=len(dag.nodes),
        )

        ctx._emit_execution_event("execution_started")
        _load_builtin_nodes()
        output_path.mkdir(parents=True, exist_ok=True)
        order = topological_order(dag.nodes)
        total = len(order)
        ctx.node_count = total
        results: dict[object, NodeResult] = {}
        saved: list[str] = []
        all_records: list[DAGOutputRecord] = []

        for index, node in enumerate(order):
            ctx.node_index = index
            ctx.current_node_id = node.id
            ctx._node_fraction = 0.0
            if (
                not node.signature.inputs
                and not node.signature.output_names
                and not node.signature.output_types
            ):
                try:
                    node_info_for_sig = get_node_type(node.type)
                    node.signature = node_info_for_sig.signature(node)
                except UnknownNodeError:
                    pass
            try:
                node_info = get_node_type(node.type)
            except UnknownNodeError as exc:
                if not strict:
                    if logger is not None:
                        logger.warning(
                            "Skipping unknown node type %r (id=%r)", exc.node_type, node.id
                        )
                    if progress_callback is not None:
                        progress_callback(index + 1, total, f"node={node.id} skipped unknown")
                    ctx.current_node_id = None
                    ctx.report_progress(node, 1.0, f"node={node.id} skipped unknown", _event_type="node_skipped")
                    continue
                raise

            gathered: dict[str, Artifact] = {}
            for slot, link in enumerate(node.inputs):
                if link is None:
                    continue
                source_result = results.get(link.source_node_id)
                if source_result is None:
                    raise DAGError(
                        f"node {node.id!r} input #{slot} reads from node {link.source_node_id!r} "
                        "which produced no output (it may have been skipped)"
                    )
                artifact = source_result.outputs.get(link.source_slot)
                if artifact is None:
                    raise DAGError(
                        f"node {node.id!r} input #{slot} reads from "
                        f"node {link.source_node_id!r} output slot {link.source_slot}, "
                        "which was not produced"
                    )
                input_name = (
                    node.signature.inputs[slot].name
                    if slot < len(node.signature.inputs)
                    else f"input_{slot}"
                )
                gathered[input_name] = artifact

            if progress_callback is not None:
                progress_callback(index, total, f"node={node.id} type={node.type}")
            ctx.report_progress(node, 0.0, f"node={node.id} type={node.type}", _event_type="node_started")

            result = node_info.execute(ctx, gathered)
            results[node.id] = result
            if node_results is not None:
                node_results[node.id] = result
            saved.extend(result.saved_paths)
            all_records.extend(result.saved_records)

            ctx.current_node_id = None
            if progress_callback is not None:
                progress_callback(index + 1, total, f"node={node.id} type={node.type}")
            ctx.report_progress(node, 1.0, f"node={node.id} type={node.type}", _event_type="node_completed")

        ctx._overall_fraction = 1.0
        ctx._emit_execution_event("execution_completed")
        return DAGExecutionResult(saved, records=all_records)
    except BaseException as exc:
        if ctx is not None and ctx.execution_event_callback is not None and not ctx._execution_callback_failed:
            node = ctx.nodes_by_id.get(ctx.current_node_id) if ctx.current_node_id is not None else None
            status = "cancelled" if isinstance(exc, (KeyboardInterrupt, AsyncCancelledError)) else "failed"
            try:
                if node is not None:
                    ctx._emit_execution_event(f"node_{status}", node, str(exc), error_type=type(exc).__name__)
                ctx._emit_execution_event(f"execution_{status}", node, str(exc), error_type=type(exc).__name__)
            except BaseException as notification_error:
                # Keep the original exception and any explicit cause. The
                # observer failure is retained as its exception context.
                if notification_error is exc:
                    raise
                raise exc
        raise
    finally:
        if owns_cache:
            cache.close()


# ---------------------------------------------------------------------------
# DAG container
# ---------------------------------------------------------------------------


@dataclass
class DAG:
    """A resolved graph ready to execute."""

    nodes: list[DAGNode] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


# Filled in by ``_load_builtin_nodes``. Kept as a module-level set so loaders
# can classify nodes without poking the registry directly.
OUTPUT_NODE_TYPES: set[str] = set()


def _load_builtin_nodes() -> None:
    """Register the built-in comfy-mss node executors on first call."""
    global _builtins_loaded
    if _builtins_loaded:
        return
    with _PLUGIN_REGISTRY._builtin_registration():
        from . import nodes  # noqa: F401  (side-effect: registers comfy-mss nodes)
        from . import builtin_nodes  # noqa: F401  (side-effect: registers ComfyUI built-in nodes)

    OUTPUT_NODE_TYPES.update(nodes.OUTPUT_NODE_TYPES)
    OUTPUT_NODE_TYPES.update(builtin_nodes.OUTPUT_NODE_TYPES)
    _builtins_loaded = True


_builtins_loaded = False


__all__ = [
    "AUDIO",
    "Artifact",
    "AudioArtifact",
    "DAG",
    "DAGError",
    "DAGLink",
    "DAGNode",
    "DAGOutputRecord",
    "DAGExecutionResult",
    "MSS_PARAMS",
    "NodeContext",
    "NodeResult",
    "NodeSignature",
    "NodeTypeInfo",
    "OUTPUT_NODE_TYPES",
    "ParamsArtifact",
    "PortSpec",
    "SeparatorCache",
    "STRING",
    "StringArtifact",
    "UnknownNodeError",
    "VR_PARAMS",
    "audio_to_numpy",
    "ctx_node_of",
    "get_node_type",
    "numpy_to_audio",
    "parse_default_int",
    "parse_device_ids",
    "register_alias",
    "register_node",
    "run_dag",
    "safe_filename_part",
    "string_value",
    "topological_order",
    "widget",
]
