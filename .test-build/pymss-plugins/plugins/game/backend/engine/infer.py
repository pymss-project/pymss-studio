from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import librosa
import numpy as np
import torch
import torch.nn.functional as torch_functional

from .config import load_model_bundle
from .inference.me_infer import SegmentationEstimationInferenceModel
from .slicer_core import Slicer
from .midi import Note, build_midi_file


MAX_INFERENCE_CHUNK_SECONDS = 60.0
INFERENCE_CHUNK_CONTEXT_SECONDS = 1.0
INFERENCE_RANDOM_SEED = 0
STEREO_CANCELLATION_RATIO = 0.25
SILENCE_RMS = 1e-6
WARNING_NO_NOTES_DETECTED = "no_notes_detected"
WARNING_STEREO_DOWNMIX_FALLBACK = "stereo_downmix_fallback"


@dataclass(frozen=True)
class MidiInferenceResult:
    output_path: Path
    note_count: int
    input_duration: float
    first_note_at: float | None
    last_note_at: float | None
    warnings: tuple[str, ...]
    language: str | None = None


@dataclass(frozen=True)
class InferenceChunk:
    waveform: np.ndarray
    offset: float
    ownership_start: float
    ownership_end: float
    includes_ownership_end: bool


def _available_midi_path(output_dir: Path, audio_name: str) -> Path:
    path = output_dir / f"{audio_name}.mid"
    if not path.exists():
        return path
    for index in range(2, 10000):
        candidate = output_dir / f"{audio_name}_{index}.mid"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Unable to allocate a MIDI output filename for {audio_name}")


def _select_device(torch_module: Any = torch) -> torch.device:
    if torch_module.cuda.is_available():
        return torch.device("cuda")
    mps = getattr(torch_module.backends, "mps", None)
    if mps is not None and mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def _load_state_dict(model_path: Path) -> Mapping[str, torch.Tensor]:
    checkpoint = torch.load(model_path, map_location="cpu", weights_only=True)
    if not isinstance(checkpoint, Mapping):
        raise ValueError("GAME model.pt does not contain a checkpoint mapping")
    state_dict = checkpoint.get("state_dict")
    if not isinstance(state_dict, Mapping) or not state_dict:
        raise ValueError("GAME model.pt does not contain a valid state_dict")
    ema_state_dict = checkpoint.get("ema_state_dict")
    if isinstance(ema_state_dict, Mapping):
        state_dict = {**state_dict, **ema_state_dict}
    return state_dict


def _prepare_mono_waveform(waveform: np.ndarray) -> tuple[np.ndarray, tuple[str, ...]]:
    samples = np.asarray(waveform, dtype=np.float32)
    if samples.size == 0:
        raise ValueError("Input audio does not contain any samples")
    if not np.isfinite(samples).all():
        raise ValueError("Input audio contains non-finite samples")
    if samples.ndim == 1:
        return samples, ()
    if samples.ndim != 2 or samples.shape[0] == 0:
        raise ValueError("Input audio has an unsupported channel layout")

    mixed = np.mean(samples, axis=0, dtype=np.float32)
    channel_rms = np.sqrt(np.mean(np.square(samples, dtype=np.float64), axis=1))
    strongest_channel = int(np.argmax(channel_rms))
    strongest_rms = float(channel_rms[strongest_channel])
    mixed_rms = float(np.sqrt(np.mean(np.square(mixed, dtype=np.float64))))
    if strongest_rms > SILENCE_RMS and mixed_rms / strongest_rms < STEREO_CANCELLATION_RATIO:
        return np.asarray(samples[strongest_channel], dtype=np.float32), (
            WARNING_STEREO_DOWNMIX_FALLBACK,
        )
    return mixed, ()


def _prepare_inference_chunks(
    chunks: list[dict[str, Any]],
    sample_rate: int,
    max_duration: float = MAX_INFERENCE_CHUNK_SECONDS,
    context_duration: float = INFERENCE_CHUNK_CONTEXT_SECONDS,
) -> list[InferenceChunk]:
    max_samples = max(1, round(sample_rate * max_duration))
    context_samples = max(0, round(sample_rate * context_duration))
    prepared: list[InferenceChunk] = []
    for chunk in chunks:
        waveform = np.asarray(chunk["waveform"], dtype=np.float32)
        chunk_offset = float(chunk["offset"])
        total_samples = int(waveform.shape[-1])
        if total_samples <= 0:
            continue
        core_start = 0
        while core_start < total_samples:
            core_end = min(total_samples, core_start + max_samples)
            window_start = max(0, core_start - context_samples)
            window_end = min(total_samples, core_end + context_samples)
            ownership_start = chunk_offset + core_start / sample_rate
            ownership_end = chunk_offset + core_end / sample_rate
            prepared.append(InferenceChunk(
                waveform=waveform[window_start:window_end],
                offset=chunk_offset + window_start / sample_rate,
                ownership_start=ownership_start,
                ownership_end=ownership_end,
                includes_ownership_end=core_end == total_samples,
            ))
            core_start = core_end
    return prepared


def _owned_notes(notes: list[Note], chunk: InferenceChunk) -> list[Note]:
    owned: list[Note] = []
    for note in notes:
        midpoint = (note.onset + note.offset) / 2
        before_end = midpoint < chunk.ownership_end
        if chunk.includes_ownership_end:
            before_end = midpoint <= chunk.ownership_end
        if midpoint >= chunk.ownership_start and before_end:
            owned.append(note)
    return owned


def _decode_chunk(
    model: SegmentationEstimationInferenceModel,
    waveform: np.ndarray,
    offset: float,
    device: torch.device,
    language_id: int,
) -> list[Note]:
    duration = max(0.0, float(waveform.shape[-1]) / model.inference_config.features.audio_sample_rate)
    samples = torch.from_numpy(np.asarray(waveform, dtype=np.float32)).to(device)
    minimum_samples = int(model.inference_config.features.win_size)
    if samples.numel() < minimum_samples:
        samples = torch_functional.pad(samples, (0, minimum_samples - samples.numel()))
    samples = samples.unsqueeze(0)
    known_durations = torch.tensor([[duration]], dtype=torch.float32, device=device)
    language = None
    if model.model_config.use_languages:
        language = torch.tensor([language_id], dtype=torch.long, device=device)
    sample_steps = 8
    sample_t = torch.arange(sample_steps, dtype=torch.float32, device=device) / sample_steps
    with torch.inference_mode():
        durations, presence, scores = model(
            waveform=samples,
            known_durations=known_durations,
            language=language,
            t=sample_t,
            boundary_threshold=torch.tensor(0.2, dtype=torch.float32, device=device),
            boundary_radius=torch.tensor(
                max(1, round(0.02 / model.timestep)), dtype=torch.long, device=device
            ),
            score_threshold=torch.tensor(0.2, dtype=torch.float32, device=device),
        )

    duration_values = durations[0].detach().cpu().tolist()
    presence_values = presence[0].detach().cpu().tolist()
    score_values = scores[0].detach().cpu().tolist()
    notes: list[Note] = []
    position = 0.0
    for note_duration, is_present, score in zip(duration_values, presence_values, score_values):
        note_onset = min(position, duration)
        position = min(position + max(0.0, float(note_duration)), duration)
        if is_present and position > note_onset:
            notes.append(Note(offset + note_onset, offset + position, float(score)))
    return notes


def _normalize_notes(notes: list[Note]) -> list[Note]:
    normalized: list[Note] = []
    last_time = 0.0
    for note in sorted(notes, key=lambda item: (item.onset, item.offset, item.pitch)):
        onset = max(note.onset, last_time)
        offset = max(note.offset, onset)
        if offset <= onset:
            continue
        normalized.append(Note(onset, offset, note.pitch))
        last_time = offset
    return normalized


def infer(
    model_path: str | Path,
    audio_path: str | Path,
    output_dir: str | Path,
    tempo: float,
    language: str | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> MidiInferenceResult:
    model_path = Path(model_path).resolve()
    if model_path.suffix.lower() != ".pt":
        raise ValueError("GAME model weights must use the .pt extension")
    if progress_callback is not None:
        progress_callback("loading_model", 0, 0)
    model_config, inference_config, language_map = load_model_bundle(model_path)
    language_id = 0
    effective_language: str | None = None
    if language_map and language:
        if language not in language_map:
            supported = ", ".join(sorted(language_map or {})) or "none"
            raise ValueError(f"Unsupported GAME language '{language}'; supported: {supported}")
        language_id = language_map[language]
        effective_language = language

    device = _select_device()
    model = SegmentationEstimationInferenceModel(model_config, inference_config)
    model.load_state_dict(_load_state_dict(model_path), strict=True)
    model.eval().to(device)

    if progress_callback is not None:
        progress_callback("loading_audio", 0, 0)
    sample_rate = int(inference_config.features.audio_sample_rate)
    source_waveform, _ = librosa.load(Path(audio_path), sr=sample_rate, mono=False)
    waveform, audio_warnings = _prepare_mono_waveform(source_waveform)
    input_duration = float(waveform.shape[-1]) / sample_rate
    slicer = Slicer(
        sr=sample_rate,
        threshold=-40.0,
        min_length=1000,
        min_interval=200,
        max_sil_kept=100,
    )
    chunks = _prepare_inference_chunks(slicer.slice(waveform), sample_rate)
    notes: list[Note] = []
    cuda_devices = []
    if device.type == "cuda":
        cuda_devices.append(device.index if device.index is not None else torch.cuda.current_device())
    with torch.random.fork_rng(devices=cuda_devices):
        torch.manual_seed(INFERENCE_RANDOM_SEED)
        for index, chunk in enumerate(chunks, start=1):
            if progress_callback is not None:
                progress_callback("transcribing", index - 1, len(chunks))
            decoded = _decode_chunk(
                model=model,
                waveform=chunk.waveform,
                offset=chunk.offset,
                device=device,
                language_id=language_id,
            )
            notes.extend(_owned_notes(decoded, chunk))
    if progress_callback is not None:
        progress_callback("transcribing", len(chunks), len(chunks))

    normalized_notes = _normalize_notes(notes)
    warnings = list(audio_warnings)
    if not normalized_notes:
        warnings.append(WARNING_NO_NOTES_DETECTED)

    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = _available_midi_path(output_dir, Path(audio_path).stem)
    if progress_callback is not None:
        progress_callback("writing_output", 0, 1)
    build_midi_file(normalized_notes, tempo=tempo).save(output_path)
    if progress_callback is not None:
        progress_callback("writing_output", 1, 1)
    return MidiInferenceResult(
        output_path=output_path,
        note_count=len(normalized_notes),
        input_duration=input_duration,
        first_note_at=normalized_notes[0].onset if normalized_notes else None,
        last_note_at=normalized_notes[-1].offset if normalized_notes else None,
        warnings=tuple(warnings),
        language=effective_language,
    )
