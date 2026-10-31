from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile


@dataclass(frozen=True)
class Note:
    onset: float
    offset: float
    pitch: float


def _encode_variable_length(value: int) -> bytes:
    value = max(0, int(value))
    encoded = [value & 0x7F]
    value >>= 7
    while value:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(encoded))


class StandardMidiFile:
    """Minimal type-0 Standard MIDI file writer without an extra MIDI dependency."""

    def __init__(self, track: bytes, ticks_per_beat: int = 480):
        self.track = track
        self.ticks_per_beat = ticks_per_beat

    def save(self, path: str | Path) -> None:
        target = Path(path)
        header = b"MThd" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big")
        header += (1).to_bytes(2, "big") + self.ticks_per_beat.to_bytes(2, "big")
        track = self.track + b"\x00\xff\x2f\x00"
        content = header + b"MTrk" + len(track).to_bytes(4, "big") + track
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def build_midi_file(notes: list[Note], tempo: float = 120) -> StandardMidiFile:
    tempo_microseconds = max(1, round(60_000_000 / float(tempo)))
    track = bytearray(b"\x00\xff\x51\x03" + tempo_microseconds.to_bytes(3, "big"))
    last_tick = 0
    for note in notes:
        onset_tick = max(last_tick, round(note.onset * tempo * 8))
        offset_tick = max(onset_tick, round(note.offset * tempo * 8))
        if offset_tick <= onset_tick:
            continue
        pitch = max(0, min(127, round(note.pitch)))
        track.extend(_encode_variable_length(onset_tick - last_tick))
        track.extend((0x90, pitch, 64))
        track.extend(_encode_variable_length(offset_tick - onset_tick))
        track.extend((0x80, pitch, 64))
        last_tick = offset_tick
    return StandardMidiFile(bytes(track))
