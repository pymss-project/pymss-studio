import json
import subprocess

import av
import numpy as np


def downmix_to_stereo(audio, sample_rate, channel_layout=None):
    """Rematrix channel-first audio using FFmpeg's standard stereo mix.

    Array inputs without layout metadata use FFmpeg's default layout for
    their channel count (for example, six channels mean 5.1). Pass a layout
    name such as ``"5.1(side)"`` when the source uses another ordering.
    """
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim != 2 or audio.shape[0] < 1:
        raise ValueError("Expected channel-first audio with at least one channel.")
    channels = audio.shape[0]
    if channels == 1:
        return np.repeat(audio, 2, axis=0)
    if channels == 2:
        return audio
    try:
        layout = av.AudioLayout(channel_layout or f"{channels}c")
    except ValueError as exc:
        if channel_layout is None:
            raise ValueError(
                f"No default layout for {channels} audio channels; provide channel_layout explicitly."
            ) from exc
        raise
    if len(layout.channels) != channels:
        raise ValueError(f"Channel layout {layout.name!r} does not match {channels} audio channels.")
    if any(channel.name == "NONE" for channel in layout.channels):
        raise ValueError("Multichannel stereo downmix requires named channel positions; provide channel_layout explicitly.")
    if audio.shape[1] == 0:
        return np.empty((2, 0), dtype=np.float32)
    resampler = av.AudioResampler(format="fltp", layout="stereo", rate=int(sample_rate))
    chunks = []
    for start in range(0, audio.shape[1], 65536):
        frame = av.AudioFrame.from_ndarray(
            # Packed samples also work with PyAV 14 for eight or more channels.
            np.ascontiguousarray(audio[:, start:start + 65536].T).reshape(1, -1),
            format="flt", layout=layout.name,
        )
        frame.sample_rate = int(sample_rate)
        chunks.extend(out.to_ndarray() for out in resampler.resample(frame))
    chunks.extend(out.to_ndarray() for out in resampler.resample(None))
    return np.ascontiguousarray(np.concatenate(chunks, axis=1))


def _frame_to_audio(frame, mono):
    """Implement the frame to audio helper.

    Args:
        frame (Any): Frame value.
        mono (bool): Mono value.

    Returns:
        Any: Computed result."""
    audio = frame.to_ndarray()
    if not frame.format.is_planar:
        audio = audio.reshape(-1, len(frame.layout.channels)).T
    audio = audio[None, :] if audio.ndim == 1 else audio
    return (audio.mean(axis=0, keepdims=True) if mono and audio.shape[0] > 1 else audio).astype(np.float32, copy=False)


def resample_audio(audio, orig_sr, target_sr):
    """Resample mono or channel-first audio with the file loader's FFmpeg filter.

    Return float32 samples with the input array's rank. Same-rate and empty
    inputs pass through; frame-sized copies bound temporary memory, and the
    final flush preserves samples buffered by the resampler. Independent
    channel groups stay within FFmpeg's 64-channel resampling limit.
    """
    audio = np.asarray(audio, dtype=np.float32)
    source_rate, target_rate = int(orig_sr), int(target_sr)
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("Sample rates must be positive.")
    if audio.ndim not in (1, 2) or (audio.ndim == 2 and audio.shape[0] == 0):
        raise ValueError("Expected mono or channel-first audio with at least one channel.")
    if source_rate == target_rate or audio.shape[-1] == 0:
        return audio

    mono = audio.ndim == 1
    samples = audio.reshape(1, -1) if mono else audio
    channels, frames = samples.shape
    capacity = (frames * target_rate + source_rate - 1) // source_rate
    result = np.empty((channels, capacity), dtype=np.float32)
    output_frames = None

    for channel_start in range(0, channels, 64):
        channel_stop = min(channel_start + 64, channels)
        group_channels = channel_stop - channel_start
        layout = av.AudioLayout(f"{group_channels} channels")
        # PyAV 14 cannot safely convert planar frames with eight or more channels.
        packed_frames = group_channels >= 8
        frame_format = "flt" if packed_frames else "fltp"
        resampler = av.AudioResampler(format=frame_format, layout=layout, rate=target_rate)
        written = 0

        def append_outputs(outputs):
            nonlocal written
            for output in outputs:
                block = _frame_to_audio(output, mono=False)
                end = written + block.shape[-1]
                result[channel_start:channel_stop, written:end] = block
                written = end

        for start in range(0, frames, 65536):
            block = samples[channel_start:channel_stop, start:start + 65536]
            if packed_frames:
                block = np.ascontiguousarray(block.T).reshape(1, -1)
            else:
                block = np.ascontiguousarray(block)
            frame = av.AudioFrame.from_ndarray(block, format=frame_format, layout=layout.name)
            frame.sample_rate = source_rate
            append_outputs(resampler.resample(frame))
        append_outputs(resampler.resample(None))
        if output_frames is None:
            output_frames = written
        elif written != output_frames:
            raise RuntimeError("Resampled channel groups produced different sample counts.")

    result = np.ascontiguousarray(result[:, :output_frames])
    return result[0] if mono else result


def _ffmpeg_audio_stream_info(path):
    """Return basic audio stream information from ffprobe."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=sample_rate,channels,channel_layout",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    streams = json.loads(result.stdout or "{}").get("streams") or []
    if not streams:
        raise ValueError(f"No audio stream found in {path!s}.")
    stream = streams[0]
    return int(stream["sample_rate"]), int(stream["channels"]), stream.get("channel_layout")


def _load_audio_ffmpeg(path, sr=None, mono=False, offset=0.0, duration=None, downmix_stereo=False, return_layout=False):
    """Load audio through the ffmpeg CLI as a fallback for damaged streams."""
    source_rate, source_channels, source_layout = _ffmpeg_audio_stream_info(path)
    out_rate = int(sr or source_rate)
    channels = 1 if mono else (2 if downmix_stereo and source_channels > 2 else source_channels)
    layout = "mono" if channels == 1 else ("stereo" if channels == 2 else source_layout)
    command = ["ffmpeg", "-nostdin", "-v", "error"]
    if offset:
        command += ["-ss", str(float(offset))]
    command += ["-i", str(path), "-map", "0:a:0", "-vn"]
    if duration is not None:
        command += ["-t", str(float(duration))]
    if layout:
        # -ac alone selects the count's default layout, which can rematrix
        # 3.0 to 2.1 even when preserving all three source channels.
        command += ["-channel_layout", layout]
    command += ["-f", "f32le", "-acodec", "pcm_f32le", "-ar", str(out_rate), "-ac", str(channels), "-"]
    result = subprocess.run(command, check=True, capture_output=True)
    audio = np.frombuffer(result.stdout, dtype="<f4")
    complete_samples = audio.size // channels
    audio = audio[: complete_samples * channels]
    audio = audio.reshape(complete_samples, channels).T
    audio = np.ascontiguousarray(audio.astype(np.float32, copy=False))
    audio = audio[0] if mono or channels == 1 else audio
    if not layout:
        layout = f"{channels} channels"
    return (audio, out_rate, layout) if return_layout else (audio, out_rate)


def _load_audio_librosa(path, sr=None, mono=False, offset=0.0, duration=None, downmix_stereo=False, return_layout=False):
    """Load audio through librosa as a fallback before the ffmpeg CLI."""
    import librosa

    audio, out_rate = librosa.load(
        path,
        sr=sr,
        mono=mono,
        offset=float(offset or 0.0),
        duration=None if duration is None else float(duration),
        dtype=np.float32,
    )
    audio = np.ascontiguousarray(np.asarray(audio, dtype=np.float32))
    if downmix_stereo and audio.ndim == 2 and audio.shape[0] > 2:
        # librosa does not expose the source layout. Let the ffmpeg fallback
        # rematrix from the file rather than guess its channel positions.
        raise ValueError("Multichannel stereo downmix requires source channel layout metadata.")
    audio = audio[0] if audio.ndim > 1 and (mono or audio.shape[0] == 1) else audio
    if not return_layout:
        return audio, int(out_rate)
    channels = 1 if audio.ndim == 1 else audio.shape[0]
    if channels <= 2:
        layout = "mono" if channels == 1 else "stereo"
    else:
        try:
            _, source_channels, layout = _ffmpeg_audio_stream_info(path)
        except FileNotFoundError:
            # The audio is decoded; absent ffprobe only leaves its positions unknown.
            layout = None
        else:
            if source_channels != channels:
                raise ValueError("The source channel count does not match the decoded audio.")
        # Match PyAV's unspecified layout without assuming speaker positions.
        layout = layout or f"{channels} channels"
    return audio, int(out_rate), layout


def _load_audio_av(path, sr=None, mono=False, offset=0.0, duration=None, downmix_stereo=False, return_layout=False):
    """Load audio through PyAV."""
    chunks = []
    out_rate = None
    output_layout = None
    with av.open(path) as container:
        stream = container.streams.audio[0]
        out_rate = int(sr or stream.rate)
        resampler = None
        stop_samples = None if duration is None else int(round((offset + duration) * out_rate))
        decoded = 0

        for frame in container.decode(stream):
            if resampler is None:
                layout = "stereo" if downmix_stereo and not mono and len(frame.layout.channels) > 2 else frame.layout.name
                # PyAV 14 planar-to-NumPy conversion can crash at eight or
                # more channels. Packed float frames preserve every channel.
                resampler = av.AudioResampler(format="flt", layout=layout, rate=out_rate)
                output_layout = "mono" if mono else layout

            for out in resampler.resample(frame):
                chunks.append(audio := _frame_to_audio(out, mono))
                decoded += audio.shape[-1]
            if stop_samples is not None and decoded >= stop_samples:
                break

        if resampler is not None:
            for out in resampler.resample(None):
                chunks.append(_frame_to_audio(out, mono))

    start = int(round(offset * out_rate))
    stop = None if duration is None else start + int(round(duration * out_rate))
    channels = 1 if mono else 0
    audio = np.ascontiguousarray(
        (np.concatenate(chunks, axis=-1) if chunks else np.empty((channels, 0), dtype=np.float32))[..., start:stop]
    )
    audio = audio[0] if mono or audio.shape[0] == 1 else audio
    return (audio, out_rate, output_layout) if return_layout else (audio, out_rate)


def load_audio(path, sr=None, mono=False, offset=0.0, duration=None, *, downmix_stereo=False, return_layout=False):
    """Load an audio file as float32 NumPy samples.

    Audio decoding is attempted in this order: PyAV, librosa, then the
    ffmpeg CLI fallback. Stereo or multi-channel output is returned
    channel-first as ``(channels, samples)``. Mono output is returned as a
    one-dimensional array.

    Args:
        path (str | os.PathLike): Input audio file path. Any format supported
            by the local FFmpeg/PyAV build can be decoded.
        sr (int | None, optional): Target sample rate. ``None`` keeps the
            source stream sample rate. Defaults to None.
        mono (bool, optional): Whether to downmix multi-channel audio to mono.
            Defaults to False.
        offset (float, optional): Start offset in seconds. Defaults to 0.0.
        duration (float | None, optional): Maximum duration to return in
            seconds after ``offset``. ``None`` reads to the end. Defaults to
            None.
        downmix_stereo (bool, optional): Downmix sources with more than two
            channels to stereo using their encoded channel layout. Mono and
            stereo sources keep their channel count. Ignored when ``mono``
            is True. Defaults to False.
        return_layout (bool, optional): Include the decoded channel layout as
            a third return value. Defaults to False, preserving the usual
            ``(audio, sample_rate)`` pair. Unknown multichannel positions use
            an unspecified layout such as ``"3 channels"``.

    Returns:
        tuple[np.ndarray, int]: Audio samples and sample rate. The array is
        channel-first for multi-channel audio and one-dimensional for mono
        output. With ``return_layout=True``, returns
        ``(audio, sample_rate, channel_layout)`` instead.

    Example:
        >>> from pymss import load_audio
        >>> audio, sample_rate = load_audio("song.wav", sr=44100)
        >>> sample_rate
        44100

    Example:
        >>> clip, sample_rate = load_audio(
        ...     "song.wav",
        ...     mono=True,
        ...     offset=30.0,
        ...     duration=10.0,
        ... )
        >>> clip.ndim
        1"""
    loaders = [
        ("PyAV", _load_audio_av),
        ("librosa", _load_audio_librosa),
        ("ffmpeg CLI", _load_audio_ffmpeg),
    ]

    errors = []
    for name, loader in loaders:
        try:
            return loader(path, sr=sr, mono=mono, offset=offset, duration=duration,
                          downmix_stereo=downmix_stereo, return_layout=return_layout)
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue

    raise RuntimeError(f"All audio loading methods failed: {'; '.join(errors)}")


def _bitrate_to_int(value):
    """Implement the bitrate to int helper.

    Args:
        value (Any): Value value.

    Returns:
        Any: Computed result."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    value = str(value).strip().lower()
    return int(float(value[:-1]) * 1000) if value.endswith("k") else int(value)


def _format_audio(audio):
    """Format audio.

    Args:
        audio (np.ndarray): Audio samples.

    Returns:
        Any: Computed result."""
    audio = np.asarray(audio)
    audio = np.ascontiguousarray(audio[:, None] if audio.ndim == 1 else audio)
    # We can use "fltp" container for all output formats, while the final result is determined by the codec.
    # Using the fltp sample format can also help avoid some clipping distortion that occurs with integer formats.
    return np.ascontiguousarray(audio.astype(np.float32).T)


def save_audio(path, audio, sr, output_format, audio_params):
    """Save a NumPy audio array to wav, flac, mp3, or m4a.

    Audio is expected as sample-major data, either ``(samples,)`` for mono or
    ``(samples, channels)`` for multi-channel audio. The output codec is chosen
    from ``output_format`` and ``audio_params``.

    Args:
        path (str | os.PathLike): Output file path.
        audio (np.ndarray): Audio samples. Mono arrays may be one-dimensional;
            stereo arrays should be shaped as ``(samples, 2)``.
        sr (int): Sample rate in Hz.
        output_format (str): Output format. Supported values are ``wav``,
            ``flac``, ``mp3``, ``m4a``, ``aac``, ``opus``, ``vorbis``, and
            ``ogg`` (alias for vorbis).
        audio_params (dict): Encoding options. Supported keys include
            ``wav_bit_depth`` (``FLOAT``, ``PCM_16``, ``PCM_24``),
            ``flac_bit_depth`` (currently ``PCM_24`` uses soundfile),
            ``mp3_bit_rate`` (for example ``"320k"``), ``m4a_bit_rate``,
            ``m4a_codec``, and ``m4a_aac_at_quality``.

    Returns:
        None: The file is written to ``path``.

    Example:
        >>> from pymss import save_audio
        >>> save_audio(
        ...     "vocals.wav",
        ...     vocals,
        ...     44100,
        ...     "wav",
        ...     {"wav_bit_depth": "FLOAT"},
        ... )

    Example:
        >>> save_audio(
        ...     "instrumental.flac",
        ...     instrumental,
        ...     44100,
        ...     "flac",
        ...     {"flac_bit_depth": "PCM_24"},
        ... )"""
    output_format = output_format.lower()

    # Dispatch to the registered codec capability. Each format registers a
    # f"{format}_encode" capability; this lets plugins add new formats without
    # touching save_audio, and unknown formats raise CapabilityNotFound instead
    # of silently falling through to wav.
    from .plugins.codecs import register_builtin_codecs
    from .plugins.registry import _REGISTRY

    register_builtin_codecs()  # idempotent
    cap_name = f"{output_format}_encode"
    if cap_name not in _REGISTRY.capabilities:
        from .plugins.registry import CapabilityNotFound

        raise CapabilityNotFound(cap_name)
    encode = _REGISTRY.capabilities[cap_name].func

    # Map legacy audio_params keys to the codec's keyword args.
    if output_format == "wav":
        encode(audio, sr, path, bit_depth=audio_params.get("wav_bit_depth", "FLOAT"))
    elif output_format == "flac":
        encode(audio, sr, path, bit_depth=audio_params.get("flac_bit_depth", "PCM_24"))
    elif output_format == "mp3":
        encode(audio, sr, path, bit_rate=audio_params.get("mp3_bit_rate", "320k"))
    elif output_format == "m4a":
        encode(
            audio, sr, path,
            bit_rate=audio_params.get("m4a_bit_rate", "512k"),
            codec=audio_params.get("m4a_codec", "aac"),
            aac_at_quality=audio_params.get("m4a_aac_at_quality", 2),
        )
    elif output_format == "aac":
        encode(audio, sr, path, bit_rate=audio_params.get("aac_bit_rate", "128k"))
    else:
        # opus / vorbis / ogg / any plugin-registered codec: no legacy params.
        encode(audio, sr, path)
