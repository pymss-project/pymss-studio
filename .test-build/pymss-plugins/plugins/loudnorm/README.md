# loudnorm

LUFS-based loudness normalization plugin for pymss.

Provides the `lufs_normalize` capability and the `NormalizeLUFS` workflow node.

## Install

```sh
pymss install loudnorm
pip install pyloudnorm   # this plugin's dependency
```

## Use

Python API:

```python
from pymss.plugins import require_capability
out = require_capability("lufs_normalize")(audio, 44100, target_lufs=-14.0)
```

Workflow node: `NormalizeLUFS` with a `target_lufs` widget (default -14.0, the streaming-platform standard).

Mono and stereo layouts are inferred. For multichannel arrays, pass an explicit
FFmpeg layout, for example `channel_layout="5.1(side)"` or `channel_layout="7.1"`.
Workflow nodes and Operations use the input AudioArtifact's layout automatically;
multichannel audio without a layout is rejected rather than guessed.

Measurement uses K-weighted channel energy with the LFE excluded and conventional
FFmpeg surround weights. It does not mix channel amplitudes, so opposite-phase
channels do not cancel. The resulting gain is applied to every original channel,
including LFE, while preserving the layout and metadata. Height/ambisonic layouts
are not supported by this array API. At least 400 ms of audio is required.

## Why LUFS

Peak normalization (pymss's built-in `normalize_peak`) only caps amplitude. LUFS measures *perceived* loudness, which is what broadcast (EBU R128) and streaming platforms (Spotify/Apple/YouTube) target. Use this when you need loudness consistency across tracks, not just anti-clipping.
