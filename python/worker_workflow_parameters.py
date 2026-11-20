"""Apply frozen workflow parameters to native DAG separation consumers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from worker_inference_constraints import InferenceParameterError
from worker_inference_snapshot import (
    BOOL_KEYS, INT_KEYS, MANAGED_KEYS, compute_inference_fingerprint,
    validate_snapshot_against_resolution,
)
from worker_models import load_parameter_resolution


_TTA = "enable_tta"
_MANAGED = MANAGED_KEYS | {_TTA}
_SOURCES = {
    "model-config", "runtime-default", "model-library", "custom", "workflow-default", "node", "link",
}
_SEPARATORS = {"mss_separate", "vr_separate", "custom_mss_separate"}
_BUILTIN_PARAMS = {"pymss_mss_params", "pymss_vr_params"}
_ADAPTER_PREFIX = "studio:workflow-parameters:"
_POLICY_KEY = "studio_workflow_parameter_policy"
_REGISTRATION_LOCK = Lock()
_MINIMUMS = {"batch_size": 1, "window_size": 1, "chunk_size": 1, "overlap_size": 0, "aggression": 0}
_ModelIdentity = tuple[str | None, str | None, str | None]
DownloadFingerprints = dict[tuple[_ModelIdentity, str | None], str]


def _scalar(value: Any) -> bool:
    return isinstance(value, (int, bool)) or isinstance(value, float) and math.isfinite(value)


def _validate_domains(parameters: dict[str, Any]) -> None:
    for key, value in parameters.items():
        if key in BOOL_KEYS or key == _TTA:
            if not isinstance(value, bool):
                raise InferenceParameterError(f"{key} must be a boolean")
        elif key in INT_KEYS:
            if type(value) is not int or not _MINIMUMS[key] <= value <= 2**53 - 1:
                raise InferenceParameterError(f"{key} must be an integer >= {_MINIMUMS[key]}")
            if key == "aggression" and value > 100:
                raise InferenceParameterError("aggression must be between 0 and 100")
        elif key == "post_process_threshold":
            if isinstance(value, bool) or not _scalar(value) or not 0 <= value <= 1:
                raise InferenceParameterError("post_process_threshold must be between 0 and 1")


def _snapshot_fields(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not key or not _scalar(item) for key, item in value.items()
    ):
        raise InferenceParameterError(f"{field} must contain finite numbers or booleans")
    _validate_domains(value)
    return dict(value)


def _resolve_execution_model(kwargs: dict[str, Any]) -> tuple[_ModelIdentity, bool]:
    if kwargs.get("model_path"):
        return (kwargs.get("model_type"), kwargs.get("config_path"), kwargs["model_path"]), False
    # This is the resolver used by MSSeparator.from_model_name. The DAG has
    # already removed category prefixes and resolved custom model aliases.
    from pymss.model_registry import resolve_model

    model = resolve_model(kwargs["model_name"], model_dir=kwargs.get("model_dir"), require_exists=False)
    return (model.get("model_type"), model.get("config_path"), model.get("model_path")), model.get("source") == "catalog"


def _validate_execution_model(
    identity: _ModelIdentity, parameters: dict[str, Any], sources: dict[str, str],
    fingerprint: str | None, downloads: DownloadFingerprints,
) -> str | None:
    resolution = load_parameter_resolution(*identity)
    installed_fingerprint = None
    if fingerprint is not None:
        actual = (resolution or {}).get("fingerprint") or compute_inference_fingerprint(*identity)
        if fingerprint != actual:
            recorded = downloads.get((identity, fingerprint))
            # A queued task can have been prepared before another task installed the
            # weights. Its fingerprint must still match the config and runtime.
            if recorded is None and fingerprint == compute_inference_fingerprint(
                *identity, ignore_model_stat=True,
            ):
                recorded = actual
            if recorded != actual:
                raise InferenceParameterError(
                    "The model configuration, weights or runtime changed after this task was submitted "
                    "(inference fingerprint mismatch). Refresh model metadata and submit a new task."
                )
            installed_fingerprint = actual

    defaults = (resolution or {}).get("defaults", {})
    chunk = parameters.get("chunk_size", defaults.get("chunk_size"))
    overlap = parameters.get("overlap_size")
    if overlap is not None and chunk is not None and overlap >= chunk:
        raise InferenceParameterError(f"overlap_size must be less than chunk_size ({chunk})")
    managed = {key: parameters[key] for key in defaults.keys() & parameters.keys()}
    if not managed:
        return installed_fingerprint

    # Validate the portion the single-model resolver describes. Native options
    # beyond that metadata stay on the actual request to the separator.
    validation_defaults = {key: defaults[key] for key in managed}
    if "overlap_size" in managed and "chunk_size" not in validation_defaults and chunk is not None:
        validation_defaults["chunk_size"] = chunk
        managed["chunk_size"] = chunk
    validation_sources = {
        # "unknown" describes missing metadata. Keep the same allowance as
        # model-library overrides for explicit native workflow values.
        key: "model-library" if source in {"link", "node", "workflow-default"} else source
        for key, source in sources.items()
    }
    validate_snapshot_against_resolution(
        {"fingerprint": resolution["fingerprint"], "parameters": managed, "sources": validation_sources},
        {**resolution, "defaults": validation_defaults},
    )
    return installed_fingerprint


@dataclass
class _ConsumerPolicy:
    id: str
    parameters: dict[str, Any]
    use_tta: bool
    sources: dict[str, str]
    overrides: dict[str, Any]
    fingerprint: str | None
    dynamic: bool
    linked: bool
    native: dict[str, tuple[Any, str]]
    model_dir: str | None
    report: Callable[[dict[str, Any]], None]
    downloads: DownloadFingerprints
    replay_extras: dict[str, Any] | None = None
    overlap_basis_points: int | None = None
    overlap_count: int | None = None
    scale_model_overlap: bool = False
    download_missing: bool | None = None
    download_source: str | None = None

    def merge(self, linked: dict[str, Any]) -> tuple[dict[str, Any], bool, dict[str, str]]:
        if self.replay_extras is not None:
            parameters = {**self.replay_extras, **self.parameters}
            _validate_domains(parameters)
            return parameters, self.use_tta, dict(self.sources)
        parameters = {key: value for key, value in linked.items() if key not in _MANAGED}
        sources = dict.fromkeys(parameters, "link" if self.linked else "runtime-default")
        for key, (value, source) in self.native.items():
            parameters[key], sources[key] = value, source
        parameters.update(self.parameters)
        parameters[_TTA] = self.use_tta
        sources.update(self.sources)
        if self.dynamic:
            parameters.update(linked)
            sources.update(dict.fromkeys(linked, "link"))
        parameters.update(self.overrides)
        sources.update(dict.fromkeys(self.overrides, "node"))
        _validate_domains(parameters)
        # A dynamic chunk may arrive without its own overlap. Semantic
        # percentages are relative to the consumer's final block length.
        if (self.dynamic and "chunk_size" in linked and "overlap_size" not in linked
                and "overlap_size" not in self.overrides and "chunk_size" in parameters):
            chunk = parameters["chunk_size"]
            if self.overlap_basis_points is not None and sources.get("overlap_size") == "custom":
                parameters["overlap_size"] = chunk - max(1, chunk * (10000 - self.overlap_basis_points) // 10000)
            elif sources.get("overlap_size") in {"model-config", "runtime-default", "model-library"}:
                if self.overlap_count is not None:
                    parameters["overlap_size"] = chunk - max(1, chunk // self.overlap_count)
                elif self.scale_model_overlap:
                    base_chunk = self.parameters.get("chunk_size")
                    base_overlap = self.parameters.get("overlap_size")
                    if base_chunk and base_overlap is not None:
                        parameters["overlap_size"] = chunk - max(1, chunk * (base_chunk - base_overlap) // base_chunk)
        use_tta = parameters.pop(_TTA)
        return parameters, use_tta, sources


class _ConsumerCache:
    """Verify identity using the arguments sent to the native cache."""

    def __init__(self, cache: Any, policy: _ConsumerPolicy, sources: dict[str, str], download: bool | None = None):
        self._cache = cache
        self._policy = policy
        self._sources = sources
        self._download = download
        self._verified = False

    def get(self, **kwargs: Any) -> Any:
        if self._policy.model_dir and kwargs.get("model_name"):
            kwargs["model_dir"] = self._policy.model_dir
        if self._download is not None and kwargs.get("model_name"):
            kwargs["download"] = self._download
        if self._policy.download_source and kwargs.get("model_name"):
            kwargs["source"] = self._policy.download_source
        if self._verified:
            return self._cache.get(**kwargs)
        parameters = kwargs["inference_params"]
        identity, is_catalog = _resolve_execution_model(kwargs)
        observed_key = (identity, None)
        fingerprint = self._policy.fingerprint or self._policy.downloads.get(observed_key)
        model_type, config_path, model_path = identity
        download = is_catalog and bool(kwargs.get("download"))
        weights_file = Path(model_path) if model_path else None
        config_file = Path(config_path) if config_path else None
        missing_weights = download and weights_file is not None and not weights_file.is_file()
        missing_config = download and config_file is not None and not config_file.is_file()
        installed_fingerprint = _validate_execution_model(
            identity, parameters, self._sources, fingerprint, self._policy.downloads,
        )
        separator = self._cache.get(**kwargs)
        if installed_fingerprint is not None and fingerprint is not None:
            _validate_execution_model(identity, parameters, self._sources, installed_fingerprint, {})
            self._policy.downloads[(identity, fingerprint)] = installed_fingerprint
        elif fingerprint is not None and (missing_weights or missing_config):
            before_install = compute_inference_fingerprint(
                model_type, None if missing_config else config_path, model_path, ignore_model_stat=missing_weights,
            )
            if fingerprint == before_install:
                _validate_execution_model(identity, parameters, self._sources, None, self._policy.downloads)
                installed_fingerprint = compute_inference_fingerprint(*identity)
                self._policy.downloads[(identity, fingerprint)] = installed_fingerprint
            else:
                _validate_execution_model(identity, parameters, self._sources, fingerprint, self._policy.downloads)
        if self._policy.fingerprint is None:
            # Metadata can first become available during the separator's native download.
            installed_fingerprint = installed_fingerprint or fingerprint or compute_inference_fingerprint(*identity)
            self._policy.downloads[observed_key] = installed_fingerprint
        visible = {key: value for key, value in parameters.items() if _scalar(value)}
        extras = {key: value for key, value in parameters.items() if key not in visible}
        self._policy.report({
            "nodeId": self._policy.id,
            "parameters": visible,
            "useTta": kwargs["use_tta"],
            "sources": {key: self._sources.get(key, "runtime-default") for key in (*visible, _TTA)},
            **({"nativeExtras": extras} if self._policy.dynamic and extras else {}),
            **({"fingerprint": installed_fingerprint} if installed_fingerprint else {}),
        })
        self._verified = True
        return separator


def _register_adapter(graph: Any, native_type: str, make_execute: Callable[..., Any]) -> str:
    adapter_type = _ADAPTER_PREFIX + native_type
    with _REGISTRATION_LOCK:
        try:
            graph.get_node_type(adapter_type)
        except graph.UnknownNodeError:
            native = graph.get_node_type(native_type)
            graph.register_node(adapter_type, signature=native.signature, execute=make_execute(native.execute))
    return adapter_type


def _execute_consumer(graph: Any, native_type: str, native_execute: Callable[..., Any]) -> Any:
    expected = "vr" if native_type.removeprefix("pymss_").startswith("vr_") else "mss"

    def execute(ctx: Any, inputs: dict[str, Any]) -> Any:
        node = ctx.nodes_by_id[ctx.current_node_id]
        policy: _ConsumerPolicy = node.data[_POLICY_KEY]
        artifact = inputs.get("params")
        try:
            if artifact is not None and (
                not isinstance(artifact, graph.ParamsArtifact) or artifact.params_type != expected
                or not isinstance(artifact.params, dict)
            ):
                raise InferenceParameterError(f"params input must be a {expected} parameter artifact")
            if artifact is None and policy.dynamic:
                raise InferenceParameterError("dynamic params input is missing")
            linked = artifact.params if artifact is not None else {}
            if any(not isinstance(key, str) or not key for key in linked):
                raise InferenceParameterError("linked parameter names must be non-empty strings")
            params, use_tta, sources = policy.merge(linked)
            merged = graph.ParamsArtifact(params={**params, _TTA: use_tta}, params_type=expected)
            cache, adapter_type = ctx.separator_cache, node.type
            # New snapshots freeze the normalized node setting. Older plans keep SDK behavior.
            download = None if policy.download_missing is None else bool(ctx.download and policy.download_missing)
            ctx.separator_cache, node.type = _ConsumerCache(cache, policy, sources, download), native_type
            try:
                return native_execute(ctx, {**inputs, "params": merged})
            finally:
                ctx.separator_cache, node.type = cache, adapter_type
        except InferenceParameterError as exc:
            raise InferenceParameterError(f"Workflow node {policy.id}: {exc}") from exc

    return execute


def _adapt_producer_zeros(graph: Any, node: Any) -> None:
    """Bridge builtin widget coercion while preserving the outgoing artifact."""
    slots = (
        {1: ("overlap_size", "Default", 0), 2: ("chunk_size", "Default", None)}
        if node.type == "pymss_mss_params"
        else {2: ("aggression", 5, 0), 6: ("post_process_threshold", 0.2, 0.0)}
    )
    widgets = node.data.get("widgets_values") or []
    repairs = {
        index: spec for index, spec in slots.items()
        if index < len(widgets) and str(widgets[index]).strip() in {"0", "0.0"}
    }
    if not repairs:
        return
    native_type = node.type

    def make_execute(native_execute: Callable[..., Any]) -> Any:
        def execute(ctx: Any, inputs: dict[str, Any]) -> Any:
            current = ctx.nodes_by_id[ctx.current_node_id]
            adapter_type = current.type
            original = current.data["widgets_values"]
            safe_widgets = list(original)
            zero_params = {}
            for index, (key, fallback, zero) in current.data[_POLICY_KEY].items():
                safe_widgets[index] = fallback
                if zero is not None:
                    zero_params[key] = zero
            current.type, current.data["widgets_values"] = native_type, safe_widgets
            try:
                result = native_execute(ctx, inputs)
            finally:
                current.type, current.data["widgets_values"] = adapter_type, original
            artifact = result.outputs[0]
            result.outputs[0] = graph.ParamsArtifact(
                params={**artifact.params, **zero_params}, params_type=artifact.params_type,
            )
            return result

        return execute

    node.type = _register_adapter(graph, native_type, make_execute)
    node.data[_POLICY_KEY] = repairs


def install_workflow_parameters(
    graph: Any, dag: Any, snapshot: Any, *,
    report: Callable[[dict[str, Any]], None],
    simple_params: dict[str, dict[str, Any]] | None = None,
    simple_step_params: dict[str, dict[str, Any]] | None = None,
    simple_model_dirs: dict[str, str | None] | None = None,
    downloads: DownloadFingerprints | None = None,
) -> None:
    if (not isinstance(snapshot, dict) or type(snapshot.get("version")) is not int
            or snapshot["version"] != 1 or not isinstance(snapshot.get("nodes"), dict)):
        raise InferenceParameterError("workflowParameters must contain version 1 and a nodes object")
    entries = snapshot["nodes"]
    context = snapshot.get("context", {})
    if not isinstance(context, dict) or not isinstance(context.get("intent", {}), dict):
        raise InferenceParameterError("Invalid global workflow parameter context")
    overlap_bp = context.get("intent", {}).get("overlapBasisPoints") if context.get("mode") == "custom" else None
    if overlap_bp is not None and (type(overlap_bp) is not int or not 1 <= overlap_bp <= 9900):
        raise InferenceParameterError("Invalid global overlap percentage")
    simple = simple_params is not None
    downloads = {} if downloads is None else downloads
    nodes_by_id = {node.id: node for node in dag.nodes}
    consumers = {
        str(node.id).removeprefix("step:") if simple else str(node.id): node
        for node in dag.nodes if node.type.removeprefix("pymss_").removesuffix("_list") in _SEPARATORS
    }
    if entries.keys() != consumers.keys():
        missing = ", ".join(sorted(consumers.keys() - entries.keys()))
        extra = ", ".join(sorted(str(key) for key in entries.keys() - consumers.keys()))
        raise InferenceParameterError(f"Workflow parameter nodes do not match the DAG (missing: {missing}; extra: {extra})")

    producers = {}
    for node_id, node in consumers.items():
        entry = entries[node_id]
        try:
            if not isinstance(entry, dict) or entry.get("id") != node_id or not isinstance(entry.get("model"), str):
                raise InferenceParameterError("invalid node id or model")
            if not isinstance(entry.get("useTta"), bool) or any(
                field in entry and not isinstance(entry[field], bool) for field in ("linked", "dynamic", "downloadMissing")
            ):
                raise InferenceParameterError("useTta, linked, dynamic and downloadMissing must be booleans")
            download_source = entry.get("downloadSource")
            if download_source is not None and download_source not in ("modelscope", "huggingface", "hf-mirror"):
                raise InferenceParameterError("invalid model download source")
            sources = entry.get("sources")
            if (not isinstance(sources, dict) or _TTA not in sources or any(
                not isinstance(key, str) or not isinstance(value, str) or value not in _SOURCES
                for key, value in sources.items()
            )):
                raise InferenceParameterError("sources must identify the parameter origins, including enable_tta")
            fingerprint = entry.get("fingerprint")
            if "fingerprint" in entry and (not isinstance(fingerprint, str) or not fingerprint):
                raise InferenceParameterError("fingerprint must be a non-empty string")
            parameters = _snapshot_fields(entry.get("parameters"), "parameters")
            if _TTA in parameters:
                raise InferenceParameterError("useTta must be supplied separately from parameters")
            overrides = _snapshot_fields(entry.get("overrides", {}), "overrides")
            params_link = node.inputs[1] if len(node.inputs) > 1 else None
            producer = nodes_by_id.get(params_link.source_node_id) if params_link is not None else None
            builtin = producer is not None and producer.type in _BUILTIN_PARAMS
            dynamic = not simple and (entry.get("dynamic", False) or (
                params_link is not None and (not builtin or any(producer.inputs))
            ))
            replay_extras = None
            overlap_count = entry.get("overlapCount")
            if overlap_count is not None and (type(overlap_count) is not int or overlap_count < 1):
                raise InferenceParameterError("Invalid overlap count")
            if entry.get("replay") is True:
                replay_extras = entry.get("nativeExtras", {})
                if not isinstance(replay_extras, dict) or _MANAGED.intersection(replay_extras):
                    raise InferenceParameterError("Invalid recorded native workflow parameters")
                dynamic = False
            if dynamic and params_link is None:
                raise InferenceParameterError("dynamic params link is missing from the DAG")
            if not simple and builtin:
                producers[producer.id] = producer

            step_params = (simple_step_params or {}).get(node_id, {})
            native = {
                key: (value, "node" if key in step_params else "workflow-default")
                for key, value in (simple_params or {}).get(node_id, {}).items() if key not in _MANAGED
            }
            native_type = node.type
            if native_type.removeprefix("pymss_").startswith("custom_mss_separate"):
                # The YAML compiler materializes these fields; Comfy loaders
                # may leave them in the raw node instead.
                raw = node.data.get("raw") or {}
                for field in ("model_path", "config_path"):
                    if not node.data.get(field) and isinstance(raw.get(field), str) and raw[field]:
                        node.data[field] = raw[field]
            node.data[_POLICY_KEY] = _ConsumerPolicy(
                node_id, parameters, entry["useTta"], dict(sources), {} if simple else overrides,
                fingerprint, dynamic, not simple and params_link is not None, native,
                (simple_model_dirs or {}).get(node_id), report, downloads, replay_extras, overlap_bp, overlap_count,
                entry.get("scaleModelOverlap") is True, download_missing=entry.get("downloadMissing"), download_source=download_source,
            )
            node.type = _register_adapter(graph, native_type, lambda execute: _execute_consumer(graph, native_type, execute))
        except InferenceParameterError as exc:
            raise InferenceParameterError(f"Workflow node {node_id}: {exc}") from exc

    for producer in producers.values():
        _adapt_producer_zeros(graph, producer)


def native_parameter_node_event(event: dict[str, Any]) -> dict[str, Any]:
    node_type = event.get("node_type")
    if not isinstance(node_type, str) or not node_type.startswith(_ADAPTER_PREFIX):
        return event
    result = {**event, "node_type": node_type.removeprefix(_ADAPTER_PREFIX)}
    message = event.get("message")
    if isinstance(message, str) and f"type={node_type}" in message:
        result["message"] = message.replace(f"type={node_type}", f"type={result['node_type']}")
    return result
