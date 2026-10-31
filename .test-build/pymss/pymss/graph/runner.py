"""Folder-batched runner for YAML workflows, built on the unified DAG core.

This replaces the legacy ``WorkflowRunner`` for the CLI ``workflow run`` path.
It preserves the externally visible semantics:

* Folder inputs are expanded one file at a time.
* Consecutive uses of the same model share a bounded separator cache across files.
* ``output_layout`` (``folders`` / ``flat``) controls whether each file's
  outputs land under ``<output>/<track>/...`` or directly under ``<output>``.
* ``continue_on_error`` keeps the batch going when one file fails.

The difference is that each per-file execution now goes through
:func:`pymss.dag.run_dag`, sharing the single :class:`SeparatorCache` across
the whole batch. Models are evicted before a different model is loaded.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from .core import SeparatorCache, run_dag
from ..workflow import (
    Workflow,
    WorkflowError,
    _DEFAULT_AUDIO_PARAMS,
    _default_audio_loader,
    _input_files,
    _step_option,
    _unique_track_names,
    _validate_output_layout,
    validate_workflow,
)
from .yaml_compiler import compile_workflow_to_dag


class LegacyWorkflowRunner:
    """Run a YAML workflow over one file or a folder, via the DAG core.

    Parameters mirror the old ``WorkflowRunner`` so the CLI can swap freely.
    """

    def __init__(
        self,
        workflow: Workflow,
        *,
        model_dir: str | os.PathLike | None = None,
        device: str | None = None,
        output_format: str | None = None,
        download: bool = False,
        source: str = "modelscope",
        endpoint: str | None = None,
        audio_params: dict[str, Any] | None = None,
        logger: Any = None,
        debug: bool = False,
        separator_factory: Callable[..., Any] | None = None,
        audio_loader: Callable[..., Any] | None = None,
        audio_saver: Callable[..., Any] | None = None,
        continue_on_error: bool = False,
        output_layout: str = "folders",
        progress_callback: Callable[[int, int, str | None], None] | None = None,
        progress_event_callback: Callable[[dict[str, Any]], None] | None = None,
        separator_cache_max_entries: int | None = 1,
        execution_event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.workflow = validate_workflow(workflow)
        self.model_dir = model_dir
        self.device = device
        self.output_format = output_format
        self.download = bool(download)
        self.source = source
        self.endpoint = endpoint
        self.audio_params = {**_DEFAULT_AUDIO_PARAMS, **(audio_params or {})}
        self.logger = logger
        self.debug = bool(debug)
        self.continue_on_error = bool(continue_on_error)
        self.output_layout = _validate_output_layout(output_layout)
        self.progress_callback = progress_callback
        self.progress_event_callback = progress_event_callback
        self.separator_cache_max_entries = separator_cache_max_entries
        self.execution_event_callback = execution_event_callback
        # ``separator_factory`` is accepted for API compatibility with the old
        # runner (tests inject fakes). When provided, we hand it to the cache so
        # the same fakes drive the DAG path.
        self.separator_factory = separator_factory
        self.audio_loader = audio_loader or _default_audio_loader

    def run(self, input_path: str | os.PathLike, output_dir: str | os.PathLike) -> list[str]:
        paths = _input_files(input_path)
        if not paths:
            raise WorkflowError(f"no input audio files found at {input_path!r}")

        dag = compile_workflow_to_dag(self.workflow)
        output_root = Path(output_dir)

        cache_kwargs: dict[str, Any] = {}
        if self.separator_factory is not None:
            cache_kwargs["factory"] = self._adapt_legacy_factory(self.separator_factory)
        cache = SeparatorCache(max_entries=self.separator_cache_max_entries, **cache_kwargs)

        processed: list[str] = []
        try:
            for path, track_name in zip(paths, _unique_track_names(paths)):
                file_output = self._file_output_dir(output_root, track_name)
                try:
                    run_dag(
                        dag,
                        output_dir=file_output,
                        input_path=path,
                        logger=self.logger,
                        debug=self.debug,
                        progress_callback=self._wrap_progress(track_name),
                        progress_event_callback=self._wrap_progress_event(track_name),
                        execution_event_callback=self._wrap_execution_event(track_name),
                        strict=True,
                        model_dir=self.model_dir,
                        download=self.download,
                        source=self.source,
                        endpoint=self.endpoint,
                        device=self.device,
                        output_format=self.output_format,
                        audio_params=self.audio_params,
                        separator_cache=cache,
                        name_prefix=track_name,
                    )
                    processed.append(os.path.basename(path))
                except Exception as exc:
                    if not self.continue_on_error:
                        raise
                    if self.logger is not None:
                        self.logger.warning("workflow step failed for %s: %s", path, exc)
        finally:
            cache.close()
        return processed

    def _file_output_dir(self, output_root: Path, track_name: str) -> Path:
        """Resolve where a single input file's outputs should land.

        ``folders`` (default): each file gets its own subdirectory, matching the
        legacy ``results/<track>/<save_dir>/...`` layout.
        ``flat``: outputs go straight under ``output_root``.
        """

        if self.output_layout == "flat":
            return output_root
        return output_root / track_name

    def _wrap_progress(self, track_name: str) -> Callable[[int, int, str | None], None] | None:
        if self.progress_callback is None:
            return None

        def _cb(done: int, total: int, message: str | None) -> None:
            self.progress_callback(done, total, f"track={track_name} {message or ''}".strip())

        return _cb

    def _wrap_progress_event(self, track_name: str):
        if self.progress_event_callback is None:
            return None

        def cb(event):
            self.progress_event_callback({**event, "track": track_name})

        return cb

    def _wrap_execution_event(self, track_name: str):
        if self.execution_event_callback is None:
            return None

        def cb(event):
            self.execution_event_callback({**event, "track": track_name})

        return cb

    @staticmethod
    def _adapt_legacy_factory(factory: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a legacy ``factory(model_name, **kwargs)`` to accept ``**kwargs``.

        Old ``WorkflowRunner`` called ``separator_factory(model_name, **kwargs)``
        positionally. The DAG cache always invokes factories with keyword args,
        pulling ``model_name`` out of kwargs. Custom-path models used to arrive
        as ``model_type``/``model_path``/``config_path`` without ``model_name``;
        we synthesize a placeholder so legacy factories still work.
        """

        def _adapted(**kwargs: Any) -> Any:
            kwargs = dict(kwargs)
            model_name = kwargs.pop("model_name", None)
            if model_name is None:
                model_name = Path(str(kwargs.get("model_path", "custom"))).stem
            return factory(model_name, **kwargs)

        return _adapted


__all__ = ["LegacyWorkflowRunner"]
