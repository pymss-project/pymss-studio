"""gamemlx backend: pure-MLX GAME inference on Apple Silicon.

Wraps https://pypi.org/project/gamemlx/ to return the same
``MidiInferenceResult`` shape the torch backend produces, so the plugin
Operation and the Studio MIDI tool work unchanged on either backend.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable


@dataclass(frozen=True)
class MlxInferenceResult:
    output_path: Path
    note_count: int
    input_duration: float
    first_note_at: float | None
    last_note_at: float | None
    warnings: tuple[str, ...]
    language: str | None = None


def have_gamemlx() -> bool:
    try:
        import gamemlx  # noqa: F401
        return True
    except Exception:
        return False


def is_apple_silicon() -> bool:
    import platform

    return platform.system() == "Darwin" and platform.machine() == "arm64"


def infer_mlx(
    audio_path: str | Path,
    model_path: str | Path,
    output_dir: str | Path,
    tempo: float = 120.0,
    language: str | None = None,
    precision: str = "fp32",
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> MlxInferenceResult:
    """Run GAME inference through gamemlx; mirrors infer.infer's contract."""
    import mido  # provided by gamemlx dependencies

    from gamemlx import extract as mlx_extract, load_model as mlx_load_model
    from pymss.plugins.kit import publish_export_path

    audio_path = Path(audio_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if progress_callback is not None:
        progress_callback("loading_model", 0, 0)
    # gamemlx accepts aliases (small/medium/large), HF repo ids, or local dirs.
    model, lang_map = mlx_load_model(str(model_path), precision=precision)

    language_id_supported = bool(language) and (lang_map or {}).get(language) is not None
    if language and lang_map and language not in lang_map:
        supported = ", ".join(sorted(lang_map)) or "none"
        raise ValueError(f"Unsupported GAME language '{language}'; supported: {supported}")

    if progress_callback is not None:
        progress_callback("loading_audio", 0, 0)
    with TemporaryDirectory(prefix=".pymss-midi-", dir=output_dir) as temporary:
        outputs = mlx_extract(
            model,
            audio_path,
            language=language if language_id_supported or not lang_map else None,
            output_dir=Path(temporary),
            tempo=tempo,
        )
        if progress_callback is not None:
            progress_callback("writing_output", 0, 1)
        midi_paths = [Path(out) for out in outputs if Path(out).suffix.lower() == ".mid"]
        if not midi_paths:
            raise RuntimeError("gamemlx produced no MIDI output")
        output_path = publish_export_path(midi_paths[0], output_dir / midi_paths[0].name)

    note_count = 0
    first_note_at: float | None = None
    last_note_at: float | None = None
    warnings: list[str] = []
    try:
        midi_file = mido.MidiFile(str(output_path))
        elapsed = 0.0
        for message in midi_file:
            elapsed += message.time
            if message.type == "note_on" and message.velocity > 0:
                note_count += 1
                first_note_at = elapsed if first_note_at is None else first_note_at
            elif message.type == "note_off" or (message.type == "note_on" and message.velocity == 0):
                last_note_at = elapsed
    except Exception:
        warnings.append("timestamps_unavailable")
    if not note_count:
        warnings.append("no_notes_detected")

    if progress_callback is not None:
        progress_callback("writing_output", 1, 1)
    import soundfile

    with soundfile.SoundFile(str(audio_path)) as handle:
        input_duration = float(handle.frames) / float(handle.samplerate)
    return MlxInferenceResult(
        output_path=output_path,
        note_count=note_count,
        input_duration=input_duration,
        first_note_at=first_note_at,
        last_note_at=last_note_at,
        warnings=tuple(warnings),
        language=language,
    )
