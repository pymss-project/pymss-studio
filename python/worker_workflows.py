"""
Workflow inference worker — thin shell over pymss.graph (in-process).

Replaces the legacy v2-graph runtime + pymss CLI fallback. The frontend now
sends a native comfy-mss JSON workflow (litegraph serialize output) or a pymss
YAML workflow dict. We hand it straight to pymss.graph and forward progress as
worker events.

Contract (payload fields, sent by stores/task.ts via start_workflow_inference):
  taskId, workflowName, workflow (dict: comfy-mss JSON or pymss YAML),
  input (str path, single-file mode), inputs ({name: path}, named runtime
  inputs for comfy load nodes),
  tasks (batch mode: [{taskId, input | inputs, output}], ...),
  output, outputFormat, outputLayout, modelDir, source, downloadMethod,
  device, deviceIds, useTta, debug, audioParams.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
import traceback
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any

from worker_protocol import emit, emit_error


def _normalize_output_dir(value: Any) -> str:
    text = str(value or "").strip()
    return text or "results"


def _normalize_output_layout(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in ("folders", "flat") else "folders"


def _normalize_inputs(value: Any) -> dict[str, str]:
    """Payload `inputs`: mapping of runtime slot name -> file path (str).

    Comfy graphs with named load nodes send this instead of the legacy single
    `input` path. pymss 2.1.2 requires named inputs — no positional fallback.
    """
    if not isinstance(value, dict):
        return {}
    return {str(k).strip(): str(v).strip() for k, v in value.items() if str(k).strip() and str(v).strip()}


def _prepare_legacy_global_input(
    payload: dict[str, Any],
    input_path: str | None,
    inputs: dict[str, str] | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Keep the pre-input-slot workflow contract working for global files.

    Older studio versions always supplied the selected file as ``input`` and
    stored ``input.wav`` (or an empty value) in load-node widgets.  Newer
    pymss runtimes require a named ``inputs`` mapping for those placeholders.
    Build that mapping at the worker boundary so existing workflows and the
    global input picker continue to use the selected file without rewriting
    their persisted definitions.
    """
    merged_inputs = dict(inputs or {})
    if not input_path:
        return payload, merged_inputs
    definition = payload.get("workflow")
    if not isinstance(definition, dict) or not isinstance(definition.get("nodes"), list):
        return payload, merged_inputs

    # Do not mutate the payload retained by the caller; only the transient
    # workflow file written for this task may be adjusted.
    transient = json.loads(json.dumps(definition))
    for node in transient.get("nodes", []):
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("type") or "").strip()
        widgets = node.get("widgets_values")
        if not isinstance(widgets, list):
            widgets = []
        if node_type in {"pymss_load_audio", "LoadAudio"}:
            # LiteGraph serializes an untouched optional widget as ``null``;
            # normalize it the same way pymss' node executor does instead of
            # turning it into the literal slot name "None".
            input_name = str((widgets[1] if len(widgets) > 1 else "") or "").strip()
            if input_name:
                # The global picker is authoritative after the input-slot UI
                # rollback, including for graphs saved with input_name.
                merged_inputs[input_name] = input_path
            else:
                widget_name = str((widgets[0] if widgets else "") or "").strip()
                if widget_name:
                    merged_inputs[widget_name] = input_path
                # pymss resolves an existing audio-widget path before looking
                # at the legacy inputs mapping. Replace it in the transient
                # graph as well, otherwise a graph saved with an embedded path
                # would silently ignore the file selected on the inference page.
                while len(widgets) <= 0:
                    widgets.append("")
                widgets[0] = input_path
            node["widgets_values"] = widgets
        elif node_type == "pymss_load_audio_batch":
            input_name = str((widgets[3] if len(widgets) > 3 else "") or "").strip()
            if input_name:
                merged_inputs[input_name] = input_path
            else:
                # Legacy batch nodes had only folder/recursive/sort widgets.
                # Give them a transient slot so the shared picker remains the
                # authoritative source instead of silently scanning a stale
                # folder (or an empty folder) from the saved graph.
                input_name = "__pymss_studio_global_input__"
                while len(widgets) <= 3:
                    widgets.append("")
                widgets[3] = input_name
                merged_inputs[input_name] = input_path
            node["widgets_values"] = widgets

    return {**payload, "workflow": transient}, merged_inputs


def _write_workflow_file(payload: dict[str, Any], task_id: str) -> tuple[Path, str]:
    """Write the workflow dict to a temp file and return (path, format).

    format is 'comfy' for a ComfyUI graph dict or 'yaml' for a pymss linear
    workflow dict (detected by the presence of a top-level 'steps' list).
    """
    definition = payload.get("workflow")
    if not isinstance(definition, dict):
        return Path(""), "comfy"

    fmt = "yaml" if "steps" in definition and "nodes" not in definition else "comfy"
    ext = "yaml" if fmt == "yaml" else "json"
    temp_dir = Path(tempfile.gettempdir()) / "pymss-studio-workflows"
    temp_dir.mkdir(parents=True, exist_ok=True)
    path = temp_dir / f"{task_id}.{ext}"
    if fmt == "yaml":
        # The YAML compiler consumes a parsed mapping; write the dict as JSON so
        # pymss.workflow.load_workflow_data can read it back losslessly.
        path.write_text(json.dumps(definition, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        path.write_text(json.dumps(definition, ensure_ascii=False, indent=2), encoding="utf-8")
    return path, fmt


def _cleanup_workflow_file(task_id: str) -> None:
    """Remove the transient graph definition created for one workflow task."""
    temp_dir = Path(tempfile.gettempdir()) / "pymss-studio-workflows"
    for extension in ("json", "yaml"):
        try:
            (temp_dir / f"{task_id}.{extension}").unlink(missing_ok=True)
        except OSError:
            pass


def _resolve_device(payload: dict[str, Any]) -> str | None:
    device = str(payload.get("device") or "").strip().lower()
    return device or None


def _emit_progress(task_id: str) -> Any:
    """Build a progress_callback(i, total, message) that emits task events."""
    def cb(index: int, total: int, message: str | None) -> None:
        if total <= 0:
            total = 1
        # Map node index (1-based feel) onto the 35..92 progress band used by
        # the UI's STAGE_META, leaving room for validate(12) and write(92).
        progress = 35 + int((index / max(1, total)) * 55)
        stage = "separating"
        emit("task_progress", {
            "stage": stage,
            "done": index,
            "total": total,
            "message": message or "Running workflow",
            "progress": min(92, progress),
        }, task_id=task_id)

    return cb


def _workflow_task_output_dir(output_dir: str, input_path: str, output_layout: str) -> Path:
    return Path(output_dir) / Path(input_path).stem if output_layout == "folders" else Path(output_dir)


def _workflow_output_stem(path: str, input_path: str | None = None) -> str:
    """Return the shared display stem used by single-separation results.

    ``pymss.graph.run_dag`` returns saved file paths, and older graph
    versions did not include a ``stem`` field in the task payload.  Depending
    on the graph/save-node configuration, the filename may include the input
    basename (for example ``song_vocals.wav``).  Strip that stable prefix so
    advanced-workflow outputs use the same labels as regular separation.
    """
    raw_path = str(path or "").strip()
    file_name = raw_path.replace("\\", "/").rsplit("/", 1)[-1]
    stem = Path(file_name).stem.strip()
    input_file_name = str(input_path or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    input_stem = Path(input_file_name).stem.strip()
    prefix = f"{input_stem}_"
    if input_stem and stem.casefold().startswith(prefix.casefold()):
        stem = stem[len(prefix):].strip()
    return stem or file_name or "output"


_SIMPLE_FILENAME_TOKENS = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")
_AUDIO_SUFFIX = re.compile(r"\.(?:wav|flac|mp3|m4a)$", re.IGNORECASE)
_INVALID_FILENAME_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]+')
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def _simple_output_names(definition: dict[str, Any]) -> bool:
    """Return whether a simple definition uses Studio filename behavior."""
    studio = definition.get("studio")
    if isinstance(studio, dict) and studio.get("editor") == "simple":
        return True
    steps = definition.get("steps")
    return isinstance(steps, list) and any(
        isinstance(step, dict)
        and isinstance(step.get("output_names"), dict)
        and bool(step.get("output_names"))
        for step in steps
    )


def _prepare_simple_runtime_definition(definition: dict[str, Any]) -> dict[str, Any]:
    """Make Studio's file-oriented save settings explicit for pymss.

    pymss YAML treats ``save`` values as subdirectory names. The simple editor
    stores those entries as ``Default`` and keeps user-facing filename
    templates in ``output_names``. For definitions created by the new editor,
    force the default directory and apply the workflow output format to every
    save node; imported YAML without this metadata keeps its original behavior.
    """
    studio = definition.get("studio")
    uses_studio_editor = isinstance(studio, dict) and studio.get("editor") == "simple"
    has_output_names = _simple_output_names(definition)
    has_ensembles = isinstance(definition.get("ensembles"), list) and bool(definition.get("ensembles"))
    has_legacy_intermediate_policy = "save_intermediate" in definition
    if not has_output_names and not has_ensembles and "studio" not in definition and not has_legacy_intermediate_policy:
        return definition
    transient = json.loads(json.dumps(definition))
    # ``studio`` is editor-only metadata and is not part of pymss' YAML
    # schema. Keep it in the persisted Studio record, but never pass it to
    # the runtime parser.
    transient.pop("studio", None)
    # ``ensembles`` is a Studio simple-workflow extension. The worker compiles
    # it into DAG nodes after pymss has parsed the native linear YAML subset.
    transient.pop("ensembles", None)
    raw_ensembles = definition.get("ensembles")
    ensemble_outputs = {
        f"{str(ensemble.get('id') or '').strip()}.{str(ensemble.get('output_stem') or '').strip()}".casefold()
        for ensemble in (raw_ensembles if isinstance(raw_ensembles, list) else [])
        if isinstance(ensemble, dict)
        and str(ensemble.get("id") or "").strip()
        and str(ensemble.get("output_stem") or "").strip()
    }
    # pymss' native YAML compiler does not know about Studio Ensemble records.
    # Give downstream steps a valid temporary input, then reconnect their slot
    # to the generated Ensemble node in _apply_simple_ensembles.
    for step in transient.get("steps", []):
        if not isinstance(step, dict):
            continue
        input_ref = str(step.get("input") or "").strip().casefold()
        if input_ref in ensemble_outputs:
            step["input"] = "input"
    # Saving is controlled solely by explicit save-node links. Older
    # definitions may still carry the retired global switch; ignore it.
    transient.pop("save_intermediate", None)
    if not has_output_names:
        return transient
    defaults = transient.get("defaults")
    output_format = "wav"
    if isinstance(defaults, dict):
        output_format = str(defaults.get("output_format") or "wav").strip().lower() or "wav"
    for step in transient.get("steps", []):
        if not isinstance(step, dict):
            continue
        if uses_studio_editor:
            if not isinstance(step.get("output_names"), dict):
                step["output_names"] = {}
        elif not isinstance(step.get("output_names"), dict) or not step.get("output_names"):
            continue
        save = step.get("save")
        if isinstance(save, dict):
            step["save"] = {str(stem): ("Default" if target not in (None, False, "") else target)
                             for stem, target in save.items()}
        if not str(step.get("output_format") or "").strip():
            step["output_format"] = output_format
    return transient


def _render_simple_filename(template: Any, *, input_path: str, stem: str, model: str,
                            step_id: str, index: int, output_format: str) -> str:
    """Render and sanitize a Studio simple-workflow filename template."""
    value = str(template or "%filename%_%stem%_%model%").strip()
    value = _AUDIO_SUFFIX.sub("", value)
    input_stem = Path(input_path).stem if input_path else "audio"
    model_stem = Path(model).stem if model else "model"
    replacements = {
        "filename": input_stem,
        "track": input_stem,
        "stem": stem,
        "model": model_stem,
        "step": step_id,
        "index": str(index),
    }
    value = _SIMPLE_FILENAME_TOKENS.sub(lambda match: replacements.get(match.group(1).lower(), match.group(0)), value)
    # Keep Unicode (including Chinese input names) while removing path
    # separators, control characters and Windows-reserved device names. The
    # pymss graph sanitizer is intentionally ASCII-only, so using it here would
    # turn `小蓝背心 - 灯火通明` into the broken `-__` prefix seen by users.
    safe = _INVALID_FILENAME_CHARS.sub("_", value).strip(" .") or stem or "audio"
    if safe.upper().split(".", 1)[0] in _WINDOWS_RESERVED_NAMES:
        safe = f"_{safe}"
    return f"{safe or stem or 'audio'}.{output_format}"


def _reserve_simple_filename(filename: str, output_dir: Path | None,
                             reserved_names: set[str]) -> str:
    if output_dir is None:
        reserved_names.add(filename.casefold())
        return filename
    candidate = Path(filename)
    base = candidate.stem
    suffix = candidate.suffix
    for collision_index in range(1, 1000):
        name = filename if collision_index == 1 else f"{base}_{collision_index}{suffix}"
        if name.casefold() in reserved_names or (output_dir / name).exists():
            continue
        filename = name
        break
    reserved_names.add(filename.casefold())
    return filename


def _simple_inference_params(workflow: Any) -> dict[str, dict[str, Any]]:
    """Validate known parameter domains before the SDK converts numeric values."""
    integer_minimums = {"batch_size": 1, "window_size": 1, "aggression": 0,
                        "overlap_size": 0, "chunk_size": 0}
    boolean_fields = {"standardize", "normalize", "enable_tta", "enable_post_process", "high_end_process"}
    defaults = workflow.defaults.get("inference_params") or {}
    result = {}
    for step in workflow.steps:
        params = {**defaults, **step.inference_params}
        for key, minimum in integer_minimums.items():
            value = params.get(key)
            if value is None:
                continue
            if key in {"overlap_size", "chunk_size"} and isinstance(value, str) and value.strip().lower() == "default":
                continue
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < minimum or int(value) != value):
                raise RuntimeError(f"Step {step.id} {key} must be an integer >= {minimum}")
        threshold = params.get("post_process_threshold")
        if threshold is not None and (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                                      or not math.isfinite(threshold) or not 0 <= threshold <= 1):
            raise RuntimeError(f"Step {step.id} post_process_threshold must be between 0 and 1")
        for key in boolean_fields:
            if params.get(key) is not None and not isinstance(params[key], bool):
                raise RuntimeError(f"Step {step.id} {key} must be a boolean")
        result[step.id] = params
    return result


def _apply_simple_inference_params(dag: Any, params_by_step: dict[str, dict[str, Any]]) -> None:
    """Adapt unset MSS sizes and preserve explicit VR zeros at the SDK boundary."""
    import pymss.graph as graph

    nodes = {str(node.id): node for node in dag.nodes}
    for step_id, params in params_by_step.items():
        node = nodes.get(f"params:{step_id}")
        if node is None:
            continue
        if node.type == "pymss_mss_params":
            widgets = node.data["widgets_values"]
            for key, index in (("overlap_size", 1), ("chunk_size", 2)):
                if params.get(key) == 0:
                    widgets[index] = "Default"
            continue
        if node.type != "pymss_vr_params":
            continue
        zeros = {
            key: (0 if key == "aggression" else 0.0)
            for key in ("aggression", "post_process_threshold")
            if isinstance(params.get(key), (int, float))
            and not isinstance(params[key], bool)
            and params[key] == 0
        }
        if not zeros:
            continue
        node_type = "studio_vr_params"
        try:
            graph.get_node_type(node_type)
        except graph.UnknownNodeError:
            native = graph.get_node_type("pymss_vr_params")

            def execute(ctx: Any, inputs: dict[str, Any]) -> Any:
                result = native.execute(ctx, inputs)
                artifact = result.outputs[0]
                if not isinstance(artifact, graph.ParamsArtifact) or artifact.params_type != "vr":
                    raise RuntimeError("VR parameter node returned an incompatible artifact")
                artifact.params.update(ctx.nodes_by_id[ctx.current_node_id].data["studio_zero_params"])
                return result

            graph.register_node(node_type, signature=native.signature, execute=execute)
        node.type = node_type
        node.data["studio_zero_params"] = zeros


def _apply_simple_output_names(dag: Any, definition: dict[str, Any], *, input_path: str,
                               output_format: str, output_dir: Path | None = None,
                               reserved_names: set[str] | None = None,
                               start_index: int = 0,
                               apply_names: bool = True) -> list[dict[str, str]]:
    """Collect simple saves and optionally wire Studio filename constants.

    Each record carries the save-node ID so execution order cannot change its
    logical stem or filename. Native/legacy saves without ``output_names`` still
    reserve their root filenames before processing outputs choose their names.
    """
    import pymss.graph as graph

    steps = definition.get("steps")
    if not isinstance(steps, list):
        return []
    next_link_id = max(
        (int(link.link_id) for node in dag.nodes for link in node.inputs
         if link is not None and isinstance(link.link_id, int)),
        default=0,
    ) + 1
    output_index = start_index
    reserved_names = reserved_names if reserved_names is not None else set()
    output_metadata: list[dict[str, str]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("id") or "").strip()
        save = step.get("save")
        names = step.get("output_names")
        if not step_id or not isinstance(save, dict):
            continue
        model = str(step.get("model") or "").strip()
        step_output_format = str(step.get("output_format") or output_format).strip().lower() or output_format
        for stem, target in save.items():
            if target in (None, False, ""):
                continue
            stem_name = str(stem).strip()
            if not stem_name:
                continue
            node_id = f"save:{step_id}:{stem_name}"
            save_node = next((node for node in dag.nodes if str(node.id) == node_id), None)
            if save_node is None:
                continue
            metadata = {"node_id": node_id, "stem": stem_name, "filename": ""}
            output_metadata.append(metadata)
            output_index += 1
            if not apply_names or not isinstance(names, dict):
                if str(target or "").strip().lower() == "default":
                    native_filename = _render_simple_filename(
                        "%stem%",
                        input_path=input_path,
                        stem=stem_name,
                        model=model,
                        step_id=step_id,
                        index=output_index,
                        output_format=step_output_format,
                    )
                    _reserve_simple_filename(native_filename, output_dir, reserved_names)
                continue
            hint = names.get(stem_name)
            if hint is None:
                hint = next((value for key, value in names.items() if str(key).lower() == stem_name.lower()), None)
            filename = _render_simple_filename(
                hint,
                input_path=input_path,
                stem=stem_name,
                model=model,
                step_id=step_id,
                index=output_index,
                output_format=step_output_format,
            )
            filename = _reserve_simple_filename(filename, output_dir, reserved_names)
            metadata["filename"] = filename
            # pymss graph sanitizer now natively supports Unicode filenames.
            # Use the target filename stem directly so files are created with
            # their intended names; _finalize_simple_output_paths acts as a no-op
            # when source and target match, while remaining compatible with older runtimes.
            filename_hint = Path(filename).stem
            constant_id = f"studio:filename:{step_id}:{stem_name}"
            # The compiler emits eight slots for pymss_save_audio. Slot 1 is the
            # filename STRING input; keeping the remaining widgets untouched
            # preserves sample-rate and codec settings.
            while len(save_node.inputs) <= 1:
                save_node.inputs.append(None)
            save_node.inputs[1] = graph.DAGLink(
                link_id=next_link_id,
                source_node_id=constant_id,
                source_slot=0,
                target_node_id=save_node.id,
                target_slot=1,
                type=graph.STRING,
            )
            next_link_id += 1
            if not any(node.id == constant_id for node in dag.nodes):
                dag.nodes.append(graph.DAGNode(
                    id=constant_id,
                    type="StringConstant",
                    inputs=[],
                    # pymss_save_audio appends the selected codec extension.
                    data={"widgets_values": [filename_hint]},
                    title=constant_id,
                ))
    return output_metadata


_SIMPLE_ENSEMBLE_ALGORITHMS = {
    "avg_wave", "median_wave", "min_wave", "max_wave",
    "avg_fft", "median_fft", "min_fft", "max_fft",
}
_SIMPLE_AUDIO_OPERATIONS = {"sum", "subtract", "invert"}


def _validate_simple_audio_dependencies(steps: list[Any], extensions: list[Any]) -> None:
    """Reject cycles and extension forward references through separation steps."""
    sources: dict[str, tuple[str, list[str]]] = {"input": ("input", [])}
    extension_indices: dict[str, int] = {}
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("stems"), list):
            continue
        step_id = str(step.get("id") or "").strip()
        for stem in step["stems"]:
            ref = f"{step_id}.{str(stem or '').strip()}".casefold()
            sources[ref] = (f"step:{step_id}", [str(step.get("input") or "input").strip().casefold()])
    for index, extension in enumerate(extensions):
        if not isinstance(extension, dict):
            continue
        extension_id = str(extension.get("id") or f"ensemble{index + 1}").strip()
        output_stem = str(extension.get("output_stem") or "").strip()
        key = f"extension:{extension_id.casefold()}"
        ref = f"{extension_id}.{output_stem}".casefold()
        if key in extension_indices or ref in sources:
            raise RuntimeError(f"Duplicate Ensemble id or output: {extension_id}")
        extension_indices[key] = index
        inputs = extension.get("inputs")
        sources[ref] = (key, [
            str(item.get("source") or "").strip().casefold()
            for item in (inputs if isinstance(inputs, list) else [])
            if isinstance(item, dict)
        ])

    resolved: dict[str, set[int]] = {}
    visiting: set[str] = set()

    def dependencies(ref: str) -> set[int]:
        source = sources.get(ref)
        if source is None:
            raise RuntimeError(f"Simple workflow references unknown output: {ref}")
        key, refs = source
        if key in visiting:
            raise RuntimeError(f"Simple workflow dependency cycle involving: {ref}")
        if key in resolved:
            return resolved[key]
        visiting.add(key)
        indices = {extension_indices[key]} if key in extension_indices else set()
        for upstream in refs:
            indices.update(dependencies(upstream))
        visiting.remove(key)
        resolved[key] = indices
        return indices

    for index, extension in enumerate(extensions):
        if not isinstance(extension, dict) or not isinstance(extension.get("inputs"), list):
            continue
        for item in extension["inputs"]:
            if not isinstance(item, dict):
                continue
            ref = str(item.get("source") or "").strip().casefold()
            if any(upstream >= index for upstream in dependencies(ref)):
                raise RuntimeError(f"Ensemble {extension.get('id') or index + 1} has a forward reference: {ref}")


def _apply_simple_ensembles(dag: Any, definition: dict[str, Any], *, input_path: str,
                            output_format: str, output_dir: Path | None = None,
                            reserved_names: set[str] | None = None,
                            start_index: int = 0) -> list[dict[str, str]]:
    """Compile simple Ensemble and audio-operation records into native nodes."""
    import pymss.graph as graph

    raw_ensembles = definition.get("ensembles")
    if not isinstance(raw_ensembles, list) or not raw_ensembles:
        return []
    steps = definition.get("steps")
    if not isinstance(steps, list):
        raise RuntimeError("Simple workflow steps are required for Ensemble")
    _validate_simple_audio_dependencies(steps, raw_ensembles)

    produced: dict[str, tuple[str, int]] = {"input": ("input", 0)}
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("id") or "").strip()
        stems = step.get("stems")
        if not step_id or not isinstance(stems, list):
            continue
        for stem_index, stem in enumerate(stems):
            stem_name = str(stem or "").strip()
            if stem_name:
                produced[f"{step_id}.{stem_name}".casefold()] = (f"step:{step_id}", stem_index * 2)

    next_link_id = max(
        (int(link.link_id) for node in dag.nodes for link in node.inputs
         if link is not None and isinstance(link.link_id, int)),
        default=0,
    ) + 1
    reserved_names = reserved_names if reserved_names is not None else set()
    output_index = start_index
    output_metadata: list[dict[str, str]] = []
    used_ids = {str(node.id) for node in dag.nodes}
    ensemble_outputs: dict[str, tuple[str, int]] = {}

    def audio_links(sources: list[tuple[str, int]], target_id: str) -> list[Any]:
        nonlocal next_link_id
        links = [graph.DAGLink(
            link_id=next_link_id + slot,
            source_node_id=source_id,
            source_slot=source_slot,
            target_node_id=target_id,
            target_slot=slot,
            type=graph.AUDIO,
        ) for slot, (source_id, source_slot) in enumerate(sources)]
        next_link_id += len(links)
        return links

    for ensemble_index, raw in enumerate(raw_ensembles, 1):
        if not isinstance(raw, dict):
            raise RuntimeError(f"Ensemble {ensemble_index} is invalid")
        ensemble_id = str(raw.get("id") or f"ensemble{ensemble_index}").strip()
        output_stem = str(raw.get("output_stem") or "").strip()
        algorithm = str(raw.get("algorithm") or "avg_wave").strip()
        raw_inputs = raw.get("inputs")
        if not ensemble_id or not output_stem:
            raise RuntimeError(f"Ensemble {ensemble_index} requires an id and output stem")
        if algorithm not in _SIMPLE_ENSEMBLE_ALGORITHMS | _SIMPLE_AUDIO_OPERATIONS:
            raise RuntimeError(f"Unsupported Ensemble algorithm: {algorithm}")
        expected_count = 1 if algorithm == "invert" else 2 if algorithm == "subtract" else None
        if expected_count is not None and (not isinstance(raw_inputs, list) or len(raw_inputs) != expected_count):
            raise RuntimeError(f"Audio {algorithm} {ensemble_id} requires exactly {expected_count} inputs")
        if expected_count is None and (not isinstance(raw_inputs, list) or not 2 <= len(raw_inputs) <= 10):
            raise RuntimeError(f"Ensemble {ensemble_id} requires 2 to 10 inputs")

        node_id = f"studio:ensemble:{ensemble_id}"
        if node_id in used_ids:
            raise RuntimeError(f"Duplicate Ensemble id: {ensemble_id}")
        used_ids.add(node_id)
        sources: list[tuple[str, int]] = []
        weights: list[float] = []
        source_refs: set[str] = set()
        for input_index, value in enumerate(raw_inputs):
            if not isinstance(value, dict):
                raise RuntimeError(f"Ensemble {ensemble_id} input {input_index + 1} is invalid")
            source_ref = str(value.get("source") or "").strip()
            produced_source = produced.get(source_ref.casefold())
            if produced_source is None:
                raise RuntimeError(f"Ensemble {ensemble_id} references unknown output: {source_ref}")
            if source_ref.casefold() in source_refs:
                raise RuntimeError(f"Ensemble {ensemble_id} uses the same output more than once: {source_ref}")
            source_refs.add(source_ref.casefold())
            try:
                weight = float(value.get("weight", 1))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"Ensemble {ensemble_id} input weight is invalid") from exc
            if not math.isfinite(weight) or weight <= 0:
                raise RuntimeError(
                    f"Ensemble {ensemble_id} input weight must be finite and greater than zero"
                )
            if algorithm in _SIMPLE_AUDIO_OPERATIONS and weight != 1:
                raise RuntimeError(f"Audio {algorithm} {ensemble_id} input weight must be 1")
            weights.append(weight)
            sources.append(produced_source)

        if algorithm == "invert":
            dag.nodes.append(graph.DAGNode(
                id=node_id,
                type="pymss_audio_invert_phase",
                inputs=audio_links(sources, node_id),
                data={},
                title=ensemble_id,
            ))
        elif algorithm in {"sum", "subtract"}:
            # Native AudioMerge retains A's duration and aligns sample rates and
            # channels. Disable peak protection on every intermediate merge so
            # adding stems and subtracting vocals preserve their original gain.
            previous = sources[0]
            for merge_index in range(1, len(sources)):
                merge_id = node_id if merge_index == len(sources) - 1 else f"{node_id}:mix:{merge_index}"
                if merge_id != node_id:
                    if merge_id in used_ids:
                        raise RuntimeError(f"Duplicate audio operation node id: {merge_id}")
                    used_ids.add(merge_id)
                dag.nodes.append(graph.DAGNode(
                    id=merge_id,
                    type="AudioMerge",
                    inputs=audio_links([previous, sources[merge_index]], merge_id),
                    data={"widgets_values": ["subtract" if algorithm == "subtract" else "add", False]},
                    title=ensemble_id,
                ))
                previous = (merge_id, 0)
        else:
            dag.nodes.append(graph.DAGNode(
                id=node_id,
                type="pymss_audio_ensemble",
                inputs=audio_links(sources, node_id),
                data={"widgets_values": [len(sources), algorithm, *weights]},
                title=ensemble_id,
            ))
        output_ref = f"{ensemble_id}.{output_stem}".casefold()
        produced[output_ref] = (node_id, 0)
        ensemble_outputs[output_ref] = (node_id, 0)

        save_target = raw.get("save")
        if save_target in (None, False, ""):
            continue
        save_id = f"studio:ensemble-save:{ensemble_id}"
        audio_link = graph.DAGLink(
            link_id=next_link_id,
            source_node_id=node_id,
            source_slot=0,
            target_node_id=save_id,
            target_slot=0,
            type=graph.AUDIO,
        )
        next_link_id += 1
        save_inputs: list[Any] = [audio_link, None, None, None, None, None, None, None]

        output_index += 1
        filename_template = raw.get("output_name")
        if algorithm in _SIMPLE_AUDIO_OPERATIONS and not str(filename_template or "").strip():
            filename_template = "%filename%_%stem%_%step%"
        filename = _render_simple_filename(
            filename_template,
            input_path=input_path,
            stem=output_stem,
            model={"sum": "AudioSum", "subtract": "AudioSubtract", "invert": "AudioInvert"}.get(algorithm, "Ensemble"),
            step_id=ensemble_id,
            index=output_index,
            output_format=output_format,
        )
        filename = _reserve_simple_filename(filename, output_dir, reserved_names)
        output_metadata.append({"node_id": save_id, "stem": output_stem, "filename": filename})
        constant_id = f"studio:ensemble-filename:{ensemble_id}"
        save_inputs[1] = graph.DAGLink(
            link_id=next_link_id,
            source_node_id=constant_id,
            source_slot=0,
            target_node_id=save_id,
            target_slot=1,
            type=graph.STRING,
        )
        next_link_id += 1
        dag.nodes.append(graph.DAGNode(
            id=constant_id,
            type="StringConstant",
            inputs=[],
            data={"widgets_values": [Path(filename).stem]},
            title=constant_id,
        ))
        dag.nodes.append(graph.DAGNode(
            id=save_id,
            type="pymss_save_audio",
            inputs=save_inputs,
            data={"widgets_values": [output_format, "Default", "44100", "FLOAT", "PCM_24", "320k"]},
            title=save_id,
        ))

    for step in steps:
        if not isinstance(step, dict):
            continue
        source_ref = str(step.get("input") or "").strip().casefold()
        produced_source = ensemble_outputs.get(source_ref)
        if produced_source is None:
            continue
        step_id = str(step.get("id") or "").strip()
        target_node_id = f"step:{step_id}"
        target_node = next((node for node in dag.nodes if str(node.id) == target_node_id), None)
        if target_node is None:
            raise RuntimeError(f"Ensemble output target step is missing: {step_id}")
        if not isinstance(target_node.inputs, list):
            target_node.inputs = list(target_node.inputs or [])
        while len(target_node.inputs) <= 0:
            target_node.inputs.append(None)
        source_node_id, source_slot = produced_source
        target_node.inputs[0] = graph.DAGLink(
            link_id=next_link_id,
            source_node_id=source_node_id,
            source_slot=source_slot,
            target_node_id=target_node_id,
            target_slot=0,
            type=graph.AUDIO,
        )
        next_link_id += 1

    return output_metadata


def _match_simple_output_metadata(output_records: list[Any | None],
                                  output_metadata: list[dict[str, str]]) -> list[dict[str, str]]:
    """Associate completed files with their declared save nodes before publishing."""
    by_node: dict[str, dict[str, str]] = {}
    for metadata in output_metadata:
        node_id = metadata["node_id"]
        if node_id in by_node:
            raise RuntimeError(f"Duplicate workflow save metadata: {node_id}")
        by_node[node_id] = metadata
    matched = []
    completed: set[str] = set()
    for record in output_records:
        node_id = str(getattr(record, "node_id", "") or "")
        if not node_id:
            raise RuntimeError("Workflow runtime did not provide save-node identities; update the runtime core")
        if node_id not in by_node:
            raise RuntimeError(f"Workflow returned an undeclared save output: {node_id}")
        matched.append(by_node[node_id])
        completed.add(node_id)
    if completed != set(by_node):
        raise RuntimeError("Workflow did not produce all requested save outputs")
    return matched


def _finalize_simple_output_paths(
    saved_paths: list[str],
    output_metadata: list[dict[str, str]],
    output_dir: Path,
    *,
    source_root: Path | None = None,
) -> list[str]:
    """Publish graph outputs without overwriting files from concurrent tasks.

    Simple workflows run inside a task-owned directory.  Each completed file
    is hard-linked into the final directory, which makes claiming the target
    name atomic while avoiding a second copy of large audio files.  Filesystems
    without hard-link support fall back to an exclusive-create copy.  Legacy
    save folders are preserved when Studio filename metadata is unavailable.
    """

    def publish(source: Path, preferred: Path) -> Path:
        preferred.parent.mkdir(parents=True, exist_ok=True)
        if source == preferred:
            return source
        for collision_index in range(1, 1000):
            target = preferred if collision_index == 1 else preferred.with_name(
                f"{preferred.stem}_{collision_index}{preferred.suffix}"
            )
            try:
                os.link(source, target)
            except FileExistsError:
                continue
            except OSError:
                # Some removable/network filesystems do not support hard
                # links.  ``xb`` still claims the name atomically, so another
                # worker cannot open the same destination concurrently.
                try:
                    writer = target.open("xb")
                except FileExistsError:
                    continue
                try:
                    with writer, source.open("rb") as reader:
                        shutil.copyfileobj(reader, writer, length=1024 * 1024)
                except Exception:
                    writer.close()
                    target.unlink(missing_ok=True)
                    raise
            try:
                source.unlink(missing_ok=True)
            except OSError:
                # The published target is already complete. The enclosing
                # TemporaryDirectory will retry removal of the private source.
                pass
            return target
        raise FileExistsError(f"Failed to reserve a unique workflow output filename: {preferred}")

    sources = [Path(value) for value in saved_paths]
    resolved_root = source_root.resolve() if source_root is not None else None
    seen_sources: set[Path] = set()
    for source in sources:
        resolved_source = source.resolve()
        if resolved_source in seen_sources:
            raise RuntimeError("Workflow returned the same output file more than once")
        seen_sources.add(resolved_source)
        if not source.is_file():
            raise RuntimeError(f"Workflow output is missing: {source}")
        if resolved_root is not None and not resolved_source.is_relative_to(resolved_root):
            raise RuntimeError("Workflow output is outside its task directory")

    finalized: list[str] = []
    for index, source_value in enumerate(saved_paths):
        source = Path(source_value)
        metadata = output_metadata[index] if index < len(output_metadata) else {}
        filename = str(metadata.get("filename") or "").strip()
        if filename:
            candidate = output_dir / filename
        elif source_root is not None:
            try:
                relative = source.resolve().relative_to(source_root.resolve())
            except ValueError as exc:
                raise RuntimeError("Workflow output is outside its task directory") from exc
            candidate = output_dir / relative
        else:
            finalized.append(source_value)
            continue
        finalized.append(str(publish(source, candidate)))
    return finalized


def _ensemble_output_stem(definition: Any) -> str | None:
    """Only Studio-generated ensemble graphs use the separation naming policy."""
    if not isinstance(definition, dict) or not isinstance(definition.get("nodes"), list):
        return None
    extra = definition.get("extra")
    if not isinstance(extra, dict) or "studioEnsemble" not in extra:
        return None
    ensemble = extra["studioEnsemble"]
    stem = ensemble.get("outputStem") if isinstance(ensemble, dict) else None
    if not isinstance(stem, str) or not stem.strip():
        raise RuntimeError("Ensemble output stem is required")
    return stem.strip()


def _finalize_ensemble_output(saved: Any, *, payload: dict[str, Any], input_path: str,
                              stem: str, output_dir: Path, temporary_dir: Path,
                              started_at: datetime) -> dict[str, Any]:
    from worker_infer import _claim_output_path, _normalize_output_naming, _replace_output_tokens
    from worker_protocol import _as_int

    paths = [Path(path) for path in saved if path is not None and str(path).strip()]
    if len(paths) != 1:
        raise RuntimeError("Ensemble must produce exactly one audio output")
    source = paths[0]
    if not source.is_file() or not source.resolve().is_relative_to(temporary_dir.resolve()):
        raise RuntimeError("Ensemble audio output is missing or outside its output directory")
    naming = _normalize_output_naming(payload.get("outputNaming"))
    name = _replace_output_tokens(
        naming["template"] if naming["enabled"] else "%filename%_%stem%",
        input_path=input_path, stem=stem, stem_index=0,
        input_index=_as_int(payload.get("inputIndex")) or 1, model="Ensemble", now=started_at,
    )
    record = next((record for record in getattr(saved, "records", []) or []
                   if getattr(record, "path", None) and Path(record.path).resolve() == source.resolve()), None)
    target = _claim_output_path(output_dir / f"{name}{source.suffix}")
    try:
        source.replace(target)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    output = {"stem": stem, "path": str(target), "name": target.name}
    if record and record.sample_rate:
        output["sampleRate"] = record.sample_rate
    return {
        "files": [str(target)], "outputs": [output],
        "outputDir": str(output_dir.resolve()), "outputFormat": target.suffix.lstrip("."),
    }


def _run_pymss(payload: dict[str, Any], task_id: str, input_path: str | None,
               inputs: dict[str, str] | None, output_dir: str, output_layout: str) -> dict[str, Any]:
    try:
        import pymss.graph as graph
    except (ImportError, ModuleNotFoundError) as exc:
        raise RuntimeError(
            "Advanced workflows require pymss.graph. Update the runtime core from Settings and retry."
        ) from exc

    runtime_payload, runtime_inputs = _prepare_legacy_global_input(payload, input_path, inputs)
    primary = input_path or (list(runtime_inputs.values())[0] if runtime_inputs else "")
    workflow_definition = runtime_payload.get("workflow")
    simple_definition = workflow_definition if isinstance(workflow_definition, dict) and "steps" in workflow_definition else None
    ensemble_stem = _ensemble_output_stem(workflow_definition)
    started_at = datetime.now()
    if isinstance(workflow_definition, dict) and "steps" in workflow_definition:
        runtime_payload = {
            **runtime_payload,
            "workflow": _prepare_simple_runtime_definition(workflow_definition),
        }
    # Output-folder naming follows the primary input: the explicit single
    # input, else the first value of the named inputs mapping.
    task_output_dir = _workflow_task_output_dir(output_dir, primary, output_layout)

    workflow_path, fmt = _write_workflow_file(runtime_payload, task_id)
    if not workflow_path.is_file():
        raise RuntimeError("Workflow definition is required")

    simple_output_metadata: list[dict[str, str]] = []
    output_format = str(payload.get("outputFormat") or "").strip().lower()
    output_format = output_format or "wav"
    if fmt == "yaml":
        import pymss.workflow as pwf
        data = json.loads(workflow_path.read_text(encoding="utf-8"))
        if not str(payload.get("outputFormat") or "").strip():
            defaults = data.get("defaults")
            if isinstance(defaults, dict):
                output_format = str(defaults.get("output_format") or output_format).strip().lower() or output_format
        if data.get("steps") == [] and simple_definition is not None and simple_definition.get("ensembles"):
            # pymss' YAML loader requires a separation step. Its native DAG
            # compiler accepts an empty Workflow, allowing pure audio processing
            # while still applying the compiler's normal defaults validation.
            if data.get("version") != 1:
                raise RuntimeError("workflow version must be 1")
            defaults = data.get("defaults") or {}
            if not isinstance(defaults, dict):
                raise RuntimeError("defaults must be a mapping")
            wf = pwf.Workflow(version=1, defaults=defaults, steps=[])
        else:
            wf = pwf.load_workflow_data(data)
        inference_params = _simple_inference_params(wf)
        dag = graph.compile_workflow_to_dag(wf)
        _apply_simple_inference_params(dag, inference_params)
        reserved_simple_names: set[str] = set()
        simple_output_metadata = _apply_simple_output_names(
            dag,
            data,
            input_path=primary,
            output_format=output_format,
            output_dir=task_output_dir,
            reserved_names=reserved_simple_names,
            apply_names=_simple_output_names(simple_definition if simple_definition is not None else data),
        )
        if simple_definition is not None:
            simple_output_metadata.extend(_apply_simple_ensembles(
                dag,
                simple_definition,
                input_path=primary,
                output_format=output_format,
                output_dir=task_output_dir,
                reserved_names=reserved_simple_names,
                start_index=len(simple_output_metadata),
            ))
    else:
        dag = graph.load_comfy_file(workflow_path)

    task_output_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[str] = []
    output_records: list[Any | None] = []
    with ExitStack() as cleanup:
        graph_output_dir = task_output_dir
        if simple_definition is not None:
            # Never let pymss write a simple-workflow output directly into the
            # shared destination.  Publishing below atomically claims the
            # final filename, including when multiple workers finish together.
            graph_output_dir = Path(cleanup.enter_context(tempfile.TemporaryDirectory(
                prefix=".pymss-workflow-", dir=task_output_dir,
            )))
        elif ensemble_stem is not None:
            # The ensemble node loses source/stem metadata and saves as audio.ext. Isolate that
            # intermediate file, then reserve and rename the final output on the same filesystem.
            graph_output_dir = Path(cleanup.enter_context(tempfile.TemporaryDirectory(
                prefix=".pymss-ensemble-", dir=task_output_dir,
            )))
        saved = graph.run_dag(
            dag,
            output_dir=graph_output_dir,
            input_path=input_path,
            inputs=runtime_inputs or None,
            progress_callback=_emit_progress(task_id),
            device=_resolve_device(payload),
            model_dir=payload.get("modelDir") or None,
            download=bool(payload.get("downloadMethod") and payload.get("downloadMethod") != "never"),
            source=str(payload.get("source") or "modelscope"),
            output_format=output_format,
            audio_params=payload.get("audioParams") if isinstance(payload.get("audioParams"), dict) else None,
            debug=bool(payload.get("debug")),
            strict=True,
        )
        if ensemble_stem is not None:
            return _finalize_ensemble_output(
                saved, payload=payload, input_path=primary, stem=ensemble_stem,
                output_dir=task_output_dir, temporary_dir=graph_output_dir, started_at=started_at,
            )
        original_saved_paths = [str(path).strip() for path in saved if path is not None and str(path).strip()]
        records = getattr(saved, "records", None) or []
        record_map = {Path(r.path).resolve(): r for r in records if getattr(r, "path", None)}
        output_records = [record_map.get(Path(path).resolve()) for path in original_saved_paths]
        saved_paths = original_saved_paths
        if fmt == "yaml" and simple_definition is not None:
            if simple_output_metadata:
                simple_output_metadata = _match_simple_output_metadata(output_records, simple_output_metadata)
            saved_paths = _finalize_simple_output_paths(
                original_saved_paths,
                simple_output_metadata,
                task_output_dir,
                source_root=graph_output_dir,
            )

    output_stems = [item["stem"] for item in simple_output_metadata] \
        if len(simple_output_metadata) == len(saved_paths) else []
    outputs: list[dict[str, Any]] = []
    for index, path in enumerate(saved_paths):
        rec = output_records[index] if index < len(output_records) else None
        stem = output_stems[index] if index < len(output_stems) else ""
        if not stem and rec and rec.stem:
            stem = rec.stem
        if not stem:
            stem = _workflow_output_stem(path, primary)
        item: dict[str, Any] = {
            "stem": stem,
            "path": path,
            "name": Path(path.replace("\\", "/")).name,
        }
        if rec and rec.sample_rate:
            item["sampleRate"] = rec.sample_rate
        outputs.append(item)

    return {
        "files": saved_paths,
        "outputs": outputs,
        "outputDir": str(task_output_dir.resolve()),
        "outputFormat": output_format,
    }


def cmd_infer_workflow(payload: dict[str, Any]) -> int:
    # Batch mode: a list of per-input tasks shares one workflow.
    raw_tasks = payload.get("tasks")
    if isinstance(raw_tasks, list) and raw_tasks:
        return _cmd_infer_workflow_batch(payload, raw_tasks)

    task_id = str(payload.get("taskId") or "")
    input_path = str(payload.get("input") or "").strip()
    inputs = _normalize_inputs(payload.get("inputs"))
    output_dir = _normalize_output_dir(payload.get("output"))
    output_layout = _normalize_output_layout(payload.get("outputLayout"))
    if not task_id:
        return emit_error("WORKFLOW_TASK_ID_MISSING", "Workflow task id is required")
    # No input required: a self-contained graph (load widgets carry real
    # paths) runs without runtime inputs. pymss raises a precise DAGError
    # otherwise, which we forward below.

    try:
        source_path = Path(input_path) if input_path else (Path(next(iter(inputs.values()))) if inputs else None)
        emit("task_started", {
            "workflow": payload.get("workflowName"),
            "input": str(source_path) if source_path else "(graph inputs)",
            "output": str(_workflow_task_output_dir(output_dir, str(source_path) if source_path else "workflow", output_layout)),
        }, task_id=task_id)
        emit("task_stage", {"stage": "validating_input", "message": "Validating workflow input", "progress": 12}, task_id=task_id)
        if source_path and not source_path.exists():
            return emit_error("INPUT_NOT_FOUND", f"Input not found: {source_path}", task_id=task_id)

        emit("task_stage", {"stage": "separating", "message": "Running workflow", "progress": 35}, task_id=task_id)
        result = _run_pymss(payload, task_id, input_path=input_path or None, inputs=inputs or None,
                            output_dir=output_dir, output_layout=output_layout)
        emit("task_stage", {"stage": "writing_output", "message": "Collecting workflow outputs", "progress": 92}, task_id=task_id)
        emit("task_done", result, task_id=task_id)
        return 0
    except Exception as exc:
        return emit_error("WORKFLOW_RUN_FAILED", str(exc), traceback.format_exc(), task_id=task_id)
    finally:
        _cleanup_workflow_file(task_id)


def _cmd_infer_workflow_batch(payload: dict[str, Any], raw_tasks: list[Any]) -> int:
    first_task_id = ""
    if raw_tasks and isinstance(raw_tasks[0], dict):
        first_task_id = str(raw_tasks[0].get("taskId") or "")
    root_task_id = str(payload.get("taskId") or first_task_id or "")
    output_dir = _normalize_output_dir(payload.get("output"))
    output_layout = _normalize_output_layout(payload.get("outputLayout"))
    output_format = str(payload.get("outputFormat") or "wav")
    if not root_task_id:
        return emit_error("WORKFLOW_TASK_ID_MISSING", "Workflow task id is required")

    failed = False
    try:
        for input_index, item in enumerate(raw_tasks, 1):
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("taskId") or "")
            input_path = str(item.get("input") or "").strip()
            item_inputs = _normalize_inputs(item.get("inputs"))
            if not task_id:
                failed = True
                emit_error("WORKFLOW_TASK_ID_MISSING", "Batch task missing taskId", task_id=root_task_id)
                continue
            # input/inputs optional when the graph carries its own paths; pymss
            # raises a precise DAGError if a load node ends up unresolved.
            source_name = input_path or (next(iter(item_inputs.values())) if item_inputs else "")
            emit("task_started", {
                "workflow": payload.get("workflowName"),
                "input": source_name,
                "output": str(_workflow_task_output_dir(output_dir, source_name or "workflow", output_layout)),
            }, task_id=task_id)
            emit("task_stage", {"stage": "validating_input", "message": "Validating workflow input", "progress": 12}, task_id=task_id)
            try:
                result = _run_pymss({**payload, "taskId": task_id, "inputIndex": item.get("inputIndex", input_index)}, task_id,
                                    input_path=input_path or None,
                                    inputs=item_inputs or None,
                                    output_dir=output_dir, output_layout=output_layout)
                emit("task_stage", {"stage": "writing_output", "message": "Collecting workflow outputs", "progress": 92}, task_id=task_id)
                emit("task_done", result, task_id=task_id)
            except Exception as exc:
                failed = True
                emit_error("WORKFLOW_RUN_FAILED", str(exc), traceback.format_exc(), task_id=task_id)
            finally:
                _cleanup_workflow_file(task_id)
        return 1 if failed else 0
    except Exception as exc:
        detail = traceback.format_exc()
        for item in raw_tasks:
            if isinstance(item, dict):
                emit_error("WORKFLOW_RUN_FAILED", str(exc), detail, task_id=str(item.get("taskId") or ""))
        return 1
