from __future__ import annotations

import argparse
import io
import json
import os
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import numpy as np

from pymss.audio_io import load_audio


def _headers(api_key: str | None, content_type: str | None = None) -> dict[str, str]:
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _read_json(url: str, api_key: str | None, timeout: float) -> dict:
    request = urllib.request.Request(url, headers=_headers(api_key), method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _audio_to_interleaved_f32le(audio) -> tuple[bytes, int, int]:
    array = np.asarray(audio, dtype=np.float32)
    if array.ndim == 1:
        channels = 1
        interleaved = array
    elif array.shape[0] in (1, 2):
        channels = int(array.shape[0])
        interleaved = array.T
    elif array.shape[1] in (1, 2):
        channels = int(array.shape[1])
        interleaved = array
    else:
        raise ValueError(f"Unsupported audio shape: {array.shape}")

    frames = int(interleaved.shape[0])
    return np.ascontiguousarray(interleaved, dtype="<f4").tobytes(), channels, frames


def _safe_extract_zip(payload: bytes, output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename)
            if name.is_absolute() or ".." in name.parts or len(name.parts) != 1:
                raise ValueError(f"Unsafe ZIP entry name from server: {info.filename!r}")
            target = output_dir / name.name
            target.write_bytes(archive.read(info))
            saved.append(target)
    return saved


def _model_metadata(base_url: str, model: str, api_key: str | None, timeout: float) -> dict:
    model_path = urllib.parse.quote(model, safe="")
    return _read_json(f"{base_url.rstrip('/')}/v1/models/{model_path}", api_key, timeout)


def run_client(args: argparse.Namespace) -> int:
    api_key = args.api_key or os.environ.get("PYMSS_API_KEY")
    base_url = args.base_url.rstrip("/")
    output_dir = Path(args.output_dir)

    metadata = _model_metadata(base_url, args.model, api_key, args.timeout)
    pymss_info = metadata["pymss"]
    sample_rate = int(pymss_info["sample_rate"])
    available_stems = pymss_info["instruments"]

    print(f"Server model: {metadata['id']}")
    print(f"Sample rate: {sample_rate}")
    print(f"Available stems: {available_stems}")

    audio, _ = load_audio(args.input, sr=sample_rate, mono=False)
    body, channels, frames = _audio_to_interleaved_f32le(audio)
    input_seconds = frames / float(sample_rate)

    query = {
        "model": args.model,
        "format": "pcm_f32le",
        "sample_rate": sample_rate,
        "channels": channels,
        "response_format": "zip",
        "output_audio_format": args.output_audio_format,
    }
    stems = ",".join(args.stems)
    if stems:
        query["stems"] = stems

    url = f"{base_url}/v1/audio/separations?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(
        url,
        data=body,
        headers=_headers(api_key, "application/octet-stream"),
        method="POST",
    )

    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=args.timeout) as response:
        payload = response.read()
        content_type = response.headers.get("content-type", "")
    elapsed = time.perf_counter() - started

    if not content_type.startswith("application/zip"):
        raise RuntimeError(f"Expected application/zip response, got {content_type!r}: {payload[:500]!r}")

    saved = _safe_extract_zip(payload, output_dir)
    print(f"Input: {args.input} ({input_seconds:.3f}s, {channels} channel(s), {len(body)} PCM bytes)")
    print(f"Request elapsed: {elapsed:.3f}s")
    print(f"Saved {len(saved)} file(s) to {output_dir}:")
    for path in saved:
        print(f"  {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send a local audio file to a running pymss HTTP server.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default="bs_roformer_voc_hyperacev2")
    parser.add_argument("--input", default="test_long.wav")
    parser.add_argument("--output-dir", default="results/server_client_demo")
    parser.add_argument("--stem", action="append", default=[], dest="stems", help="Stem to request. Can be repeated.")
    parser.add_argument("--output-audio-format", default="wav", choices=["pcm_f32le", "wav", "flac"])
    parser.add_argument("--api-key", help="Bearer token. Defaults to PYMSS_API_KEY when set.")
    parser.add_argument("--timeout", default=300.0, type=float)
    return parser


def main() -> int:
    return run_client(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
