from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pymss.plugins.kit import (
    SUPPORTED_AUDIO_SUFFIXES,
    ToolError as AudioToolError,
    available_path as _available_path,
    emit,
    render_output as _render_output,
    require_directory as _require_directory,
    run_ffmpeg as _run_ffmpeg,
    subprocess_detail as _subprocess_detail,
)


def _audio_files_in_folder(folder: Path) -> list[Path]:
    return sorted(
        (
            path.resolve()
            for path in folder.iterdir()
            if path.is_file() and not path.name.startswith('.pymss-') and path.suffix.lower() in SUPPORTED_AUDIO_SUFFIXES
        ),
        key=lambda path: path.name.casefold(),
    )


_NATURAL_TOKEN_PATTERN = re.compile(r"(\d+)")


def _natural_sort_key(value: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in _NATURAL_TOKEN_PATTERN.split(value)
    )


def _order_audio_files(
    inputs: list[Path],
    sort_by: str,
    direction: str,
    regex_pattern: str = "",
) -> list[Path]:
    if sort_by not in {"name", "modified", "regex"}:
        raise ValueError("Unsupported merge sort rule")
    if direction not in {"asc", "desc"}:
        raise ValueError("Unsupported merge sort direction")
    descending = direction == "desc"

    if sort_by == "name":
        return sorted(inputs, key=lambda path: _natural_sort_key(path.name), reverse=descending)
    if sort_by == "modified":
        def modified_key(path: Path) -> tuple[int, tuple[tuple[int, int | str], ...]]:
            try:
                modified = path.stat().st_mtime_ns
            except OSError:
                modified = 0
            return modified, _natural_sort_key(path.name)

        return sorted(inputs, key=modified_key, reverse=descending)

    pattern_value = regex_pattern.strip()
    if not pattern_value:
        raise ValueError("A regular expression is required for regex merge ordering")
    try:
        pattern = re.compile(pattern_value)
    except re.error as error:
        raise ValueError(f"Invalid merge regular expression: {error}") from error

    matched: list[tuple[tuple[tuple[int, int | str], ...], Path]] = []
    unmatched: list[Path] = []
    for path in inputs:
        match = pattern.search(path.name)
        if match is None:
            unmatched.append(path)
            continue
        groups = [group for group in match.groups() if group is not None]
        extracted = groups[0] if groups else match.group(0)
        matched.append((_natural_sort_key(extracted), path))
    if not matched:
        raise ValueError("The merge regular expression did not match any filenames")

    matched.sort(key=lambda item: item[0], reverse=descending)
    unmatched.sort(key=lambda path: _natural_sort_key(path.name))
    return [path for _, path in matched] + unmatched


def _ffmpeg_concat_path(path: Path) -> str:
    # FFmpeg's concat demuxer accepts forward slashes on every supported desktop platform.
    return str(path).replace("\\", "/").replace("'", "'\\''")


def _merge_audio(payload: dict[str, Any]) -> dict[str, Any]:
    input_dir = _require_directory(payload.get("inputDir"), "Input directory")
    output_value = str(payload.get("outputDir") or '').strip()
    output_candidate = Path(output_value).expanduser()
    if output_value and (output_candidate.resolve() == input_dir
                         or (output_candidate.exists() and output_candidate.samefile(input_dir))):
        raise AudioToolError('MERGE_SAME_DIRECTORY', 'Input and output directories must be different')
    output_dir = _require_directory(output_value, "Output directory", create=True)
    sort_by = str(payload.get("sortBy") or "name").strip().lower()
    sort_direction = str(payload.get("sortDirection") or "asc").strip().lower()
    regex_pattern = str(payload.get("regexPattern") or "")
    inputs = _audio_files_in_folder(input_dir)
    if not inputs:
        raise ValueError("The input directory does not contain supported audio files")
    if len(inputs) > 2000:
        raise ValueError("A maximum of 2000 files can be merged at once")
    inputs = _order_audio_files(inputs, sort_by, sort_direction, regex_pattern)

    output_name = f"merged_audio_{input_dir.name or 'output'}.wav"
    output_path = _available_path(output_dir / output_name)
    skipped: list[dict[str, str]] = []
    normalized: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="pymss-merge-") as temp_value:
        temp_dir = Path(temp_value)
        for index, input_path in enumerate(inputs, start=1):
            emit("audio_tool_progress", {
                "operation": "merge",
                "phase": "normalizing",
                "completed": index - 1,
                "total": len(inputs),
                "current": input_path.name,
            })
            segment_path = temp_dir / f"segment_{index:06d}.wav"
            try:
                _run_ffmpeg([
                    "-i", str(input_path), "-vn", "-ar", "44100", "-ac", "2",
                    "-c:a", "pcm_s24le", "-y", str(segment_path),
                ])
                normalized.append(segment_path)
            except subprocess.CalledProcessError as error:
                skipped.append({"path": str(input_path), "message": _subprocess_detail(error)})

        if not normalized:
            detail = skipped[0]["message"] if skipped else "No readable audio files were found"
            raise RuntimeError(detail)
        concat_file = temp_dir / "segments.txt"
        concat_file.write_text(
            "".join(f"file '{_ffmpeg_concat_path(path)}'\n" for path in normalized),
            encoding="utf-8",
        )
        emit("audio_tool_progress", {
            "operation": "merge",
            "phase": "merging",
            "completed": 0,
            "total": 1,
            "current": output_path.name,
        })
        try:
            output_path = _render_output(output_path, lambda temporary: _run_ffmpeg([
                "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-c:a", "copy", "-y", str(temporary),
            ]))
            emit("audio_tool_progress", {
                "operation": "merge",
                "phase": "merging",
                "completed": 1,
                "total": 1,
                "current": output_path.name,
            })
        except subprocess.CalledProcessError as error:
            raise RuntimeError(_subprocess_detail(error)) from error

    return {
        "operation": "merge",
        "outputDir": str(output_dir),
        "outputPath": str(output_path),
        "merged": len(normalized),
        "skipped": skipped,
        "sortBy": sort_by,
        "sortDirection": sort_direction,
    }
