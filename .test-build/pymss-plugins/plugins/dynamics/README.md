# dynamics

Dynamics processing — compression, limiting, expansion, multiband compression, de-essing.
Backed by Spotify's [pedalboard](https://github.com/spotify/pedalboard).

| Capability | Description |
|---|---|
| `compressor` | Feed-forward compressor |
| `limiter` | Brick-wall limiter |
| `expander` | Downward expander |
| `multiband_compressor` | 3-band (low/mid/high) compressor |
| `deesser` | High-band compressor for sibilance |

All dB thresholds are dBFS. Install: `pymss install dynamics` + `pip install pedalboard`.
