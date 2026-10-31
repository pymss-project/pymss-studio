# analysis

Read-only audio measurement. Capabilities return dicts, not audio.

| Capability | Returns |
|---|---|
| `measure_lufs` | Integrated loudness in LUFS |
| `measure_true_peak` | True peak in dBTP (4x oversampled) + sample peak |
| `measure_rms` | RMS in dBFS and linear |
| `measure_dynamic_range` | DR estimate (Lmax - Lmin over 400 ms blocks, in LU) |
| `spectrum_analyze` | Average power spectrum (freqs + magnitudes in dB) |

Install: `pymss install analysis` + `pip install pyloudnorm`.

## Use

```python
from pymss.plugins import require_capability
print(require_capability("measure_lufs")(audio, 44100))  # {'lufs': -14.3}
```

`measure_lufs` and `measure_dynamic_range` accept `channel_layout` for multichannel
arrays, such as `"5.1(side)"` or `"7.1"`. Mono and stereo are inferred; more channels
require a layout. LUFS nodes and Operations read the layout from the AudioArtifact.
LFE is excluded from loudness measurement, and channel energies are summed with
conventional FFmpeg surround weights without phase cancellation. Height/ambisonic
layouts are not supported by this array API. Loudness measurement requires at least
400 ms; dynamic-range blocks must also be at least 400 ms long.
