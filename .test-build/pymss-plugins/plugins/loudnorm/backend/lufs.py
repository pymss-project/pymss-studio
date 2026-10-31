"""Layout-aware loudness metering, bundled with each independent plugin.

Keep this module in sync with plugins/analysis/backend/lufs.py.
"""

from __future__ import annotations

import numpy as np


def integrated_loudness(audio, sample_rate, *, channel_layout=None):
    """Measure channel-first audio without mixing channel amplitudes.

    Mono/stereo layouts can be inferred. More channels require an explicit
    FFmpeg layout so that LFE exclusion and surround weights are unambiguous.
    """
    import av
    from pyloudnorm import IIRfilter, Meter

    audio = np.asarray(audio, dtype=np.float64)
    if audio.ndim == 1:
        audio = audio[None, :]
    if audio.ndim != 2 or not audio.shape[0] or not np.isfinite(audio).all():
        raise ValueError("Audio must contain finite mono or channel-first samples")
    if not np.isfinite(sample_rate) or sample_rate <= 0 or int(sample_rate) != sample_rate:
        raise ValueError("sample_rate must be a positive integer")
    sample_rate = int(sample_rate)
    if audio.shape[1] < 0.4 * sample_rate:
        raise ValueError("Loudness measurement requires at least 400 ms of audio")
    if channel_layout is None:
        channel_layout = {1: "mono", 2: "stereo"}.get(audio.shape[0])
        if channel_layout is None:
            raise ValueError("Multichannel loudness measurement requires channel_layout")
    channels = av.AudioLayout(channel_layout).channels
    if len(channels) != audio.shape[0]:
        raise ValueError("channel_layout does not match the audio channel count")
    # WAV files without speaker masks can decode as "1 channels"/"2 channels"
    # with unnamed speakers. Their conventional mono/stereo mapping is safe;
    # larger anonymous layouts still require explicit speaker positions.
    if len(channels) in (1, 2) and all(channel.name == "NONE" for channel in channels):
        channels = av.AudioLayout({1: "mono", 2: "stereo"}[len(channels)]).channels

    # Conventional FFmpeg speaker positions, matching its ebur128 scanner.
    weights = {"FL": 1.0, "FR": 1.0, "FC": 1.0, "FLC": 1.0, "FRC": 1.0,
               "BL": 1.41, "BR": 1.41, "BC": 1.41, "SL": 1.41, "SR": 1.41,
               "LFE": 0.0, "LFE2": 0.0}
    if any(channel.name not in weights for channel in channels):
        raise ValueError(f"Unsupported loudness channel_layout: {channel_layout}")

    # Public pyloudnorm filters; DeMan coefficients match BS.1770 K-weighting.
    shelf = IIRfilter(3.99984385397, 0.7071752369554193, 1681.9744509555319,
                      sample_rate, "high_shelf_DeMan")
    highpass = IIRfilter(0.0, 0.5003270373253953, 38.13547087613982,
                        sample_rate, "high_pass_DeMan")
    energy = np.zeros(audio.shape[1], dtype=np.float64)
    for waveform, channel in zip(audio, channels):
        weight = weights[channel.name]
        if weight:
            filtered = highpass.apply_filter(shelf.apply_filter(waveform))
            energy += weight * np.square(filtered)
    if not np.any(energy):
        return float("-inf")

    # The meter's custom mode skips filtering. Squaring this energy envelope
    # inside its gate yields the SUM of weighted channel energies, without
    # phase cancellation or its five-channel limit. This is not output audio.
    return float(Meter(sample_rate, filter_class="custom").integrated_loudness(np.sqrt(energy)))
