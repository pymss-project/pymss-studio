"""Batch conversion using pymss's shared FFmpeg file operation."""

from __future__ import annotations

from pathlib import Path


def convert_batch(ctx, inputs, params):
    from pymss.operations import OperationContext, default_runtime

    files = inputs.get("files")
    paths = files.get("paths") if isinstance(files, dict) else None
    if not isinstance(paths, list) or not 1 <= len(paths) <= 4096:
        raise ValueError("Select between 1 and 4096 input audio files")
    if not all(isinstance(path, str) and path.strip() for path in paths):
        raise ValueError("Input audio paths must be non-empty strings")
    sources = list(dict.fromkeys(str(Path(path).expanduser()) for path in paths))
    for source in sources:
        if not Path(source).is_file():
            raise ValueError(f"Input audio file does not exist: {source}")
    if not params["output_dir"].strip():
        raise ValueError("Select an output directory")
    runtime = default_runtime()
    outputs, failures = [], []
    total = len(sources)
    for index, source in enumerate(sources):
        ctx.check_cancelled()
        ctx.report_progress(index / total, Path(source).name)
        context = OperationContext(
            cancelled=ctx.cancelled, logger=ctx.logger, workdir=ctx.workdir,
            progress=lambda fraction, message, event, offset=index: ctx.report_progress(
                (offset + fraction) / total, message, event=event,
            ),
        )
        try:
            result = runtime.run("pymss.kit.convert", inputs={"audio": source}, params=params, context=context)
            outputs.append(str(result["audio_file"]))
        except Exception as error:
            ctx.check_cancelled()
            failures.append({"path": source, "message": str(error)})
        ctx.report_progress((index + 1) / total, Path(source).name)
    if not outputs:
        raise ValueError(failures[0]["message"])
    return {"report": {
        "outputPaths": outputs, "outputDir": str(Path(params["output_dir"]).expanduser()),
        "succeeded": len(outputs), "failed": failures,
    }}


def setup(plugin):
    plugin.register_operation(
        id="pymss.convert.batch", title="Batch Audio Conversion",
        description="Convert audio files with independent sample-rate, channel and encoding settings",
        inputs={"files": {"type": "object"}},
        params={"type": "object", "properties": {
            "output_dir": {"type": "string"},
            "format": {"type": "string", "enum": ["wav", "flac", "mp3", "ogg"], "default": "wav"},
            "sample_rate": {"type": "integer", "enum": [32000, 44100, 48000], "default": 44100},
            "channels": {"type": "integer", "enum": [1, 2], "default": 2},
            "wav_bit_depth": {"type": "string", "enum": ["PCM-16", "PCM-24", "PCM-32"], "default": "PCM-24"},
            "flac_bit_depth": {"type": "string", "enum": ["16-bit", "32-bit"], "default": "16-bit"},
            "mp3_bit_rate": {"type": "string", "enum": ["192k", "256k", "320k"], "default": "320k"},
            "ogg_bit_rate": {"type": "string", "enum": ["192k", "256k", "320k", "450k"], "default": "320k"},
        }, "required": ["output_dir"], "additionalProperties": False},
        outputs={"report": {"type": "object"}}, run=convert_batch,
    )
