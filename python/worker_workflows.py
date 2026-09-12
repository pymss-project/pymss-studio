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
import re
import tempfile
import traceback
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
    """Return whether a simple definition uses Studio filename metadata."""
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
    has_output_names = _simple_output_names(definition)
    has_legacy_intermediate_policy = "save_intermediate" in definition
    if not has_output_names and "studio" not in definition and not has_legacy_intermediate_policy:
        return definition
    transient = json.loads(json.dumps(definition))
    # ``studio`` is editor-only metadata and is not part of pymss' YAML
    # schema. Keep it in the persisted Studio record, but never pass it to
    # the runtime parser.
    transient.pop("studio", None)
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
        if (
            not isinstance(step, dict)
            or not isinstance(step.get("output_names"), dict)
            or not step.get("output_names")
        ):
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


def _apply_simple_output_names(dag: Any, definition: dict[str, Any], *, input_path: str,
                               output_format: str, output_dir: Path | None = None) -> list[dict[str, str]]:
    """Wire per-save filename constants into compiled YAML save nodes.

    The returned stem list follows the save-node insertion order. It lets the
    worker keep the logical stem in result metadata even when a user-selected
    filename no longer contains the stem name.
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
    output_index = 0
    reserved_names: set[str] = set()
    output_metadata: list[dict[str, str]] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("id") or "").strip()
        save = step.get("save")
        names = step.get("output_names")
        if not step_id or not isinstance(save, dict) or not isinstance(names, dict):
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
            hint = names.get(stem_name)
            if hint is None:
                hint = next((value for key, value in names.items() if str(key).lower() == stem_name.lower()), None)
            output_index += 1
            filename = _render_simple_filename(
                hint,
                input_path=input_path,
                stem=stem_name,
                model=model,
                step_id=step_id,
                index=output_index,
                output_format=step_output_format,
            )
            if output_dir is not None:
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
            output_metadata.append({"stem": stem_name, "filename": filename})
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


def _finalize_simple_output_paths(
    saved_paths: list[str],
    output_metadata: list[dict[str, str]],
    output_dir: Path,
) -> list[str]:
    """Rename graph-produced temporary files to Studio's Unicode filenames."""
    if len(saved_paths) != len(output_metadata):
        return saved_paths
    finalized: list[str] = []
    reserved_names: set[str] = set()
    for source_value, metadata in zip(saved_paths, output_metadata):
        source = Path(source_value)
        filename = str(metadata.get("filename") or "").strip()
        if not filename:
            finalized.append(source_value)
            continue
        candidate = output_dir / filename
        base = candidate.stem
        suffix = candidate.suffix
        for collision_index in range(1, 1000):
            target = candidate if collision_index == 1 else output_dir / f"{base}_{collision_index}{suffix}"
            if target == source:
                candidate = target
                break
            if target.name.casefold() in reserved_names or target.exists():
                continue
            candidate = target
            break
        reserved_names.add(candidate.name.casefold())
        if source != candidate:
            if not source.is_file():
                finalized.append(str(source))
                continue
            try:
                source.replace(candidate)
            except OSError:
                # Keep the actual path when another process has the temporary
                # file open; the task still reports a valid generated output.
                finalized.append(str(source))
                continue
        finalized.append(str(candidate))
    return finalized


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
        wf = pwf.load_workflow_data(data)
        dag = graph.compile_workflow_to_dag(wf)
        if _simple_output_names(data):
            simple_output_metadata = _apply_simple_output_names(
                dag,
                data,
                input_path=primary,
                output_format=output_format,
                output_dir=task_output_dir,
            )
    else:
        dag = graph.load_comfy_file(workflow_path)

    task_output_dir.mkdir(parents=True, exist_ok=True)
    saved = graph.run_dag(
        dag,
        output_dir=task_output_dir,
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
    saved_paths = [str(path).strip() for path in saved if path is not None and str(path).strip()]
    output_stems: list[str] = []
    if fmt == "yaml" and len(simple_output_metadata) == len(saved_paths):
        saved_paths = _finalize_simple_output_paths(saved_paths, simple_output_metadata, task_output_dir)
        output_stems = [item["stem"] for item in simple_output_metadata]

    records = getattr(saved, "records", None) or []
    record_map = {Path(r.path).resolve(): r for r in records if getattr(r, "path", None)}
    outputs: list[dict[str, Any]] = []
    for index, path in enumerate(saved_paths):
        rec = record_map.get(Path(path).resolve())
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
        for item in raw_tasks:
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
                result = _run_pymss({**payload, "taskId": task_id}, task_id,
                                    input_path=input_path or None,
                                    inputs=item_inputs or None,
                                    output_dir=output_dir, output_layout=output_layout)
                emit("task_stage", {"stage": "writing_output", "message": "Collecting workflow outputs", "progress": 92}, task_id=task_id)
                emit("task_done", result, task_id=task_id)
            except Exception as exc:
                failed = True
                emit_error("WORKFLOW_RUN_FAILED", str(exc), traceback.format_exc(), task_id=task_id)
        return 1 if failed else 0
    except Exception as exc:
        detail = traceback.format_exc()
        for item in raw_tasks:
            if isinstance(item, dict):
                emit_error("WORKFLOW_RUN_FAILED", str(exc), detail, task_id=str(item.get("taskId") or ""))
        return 1
