# timing

Pitch-shift and time-stretch, backed by librosa (no extra dependencies beyond pymss).

| Capability | Description |
|---|---|
| `pitch_shift` | Shift pitch by fractional semitones; duration preserved |
| `time_stretch` | Stretch by rate without changing pitch |

## Install

```sh
pymss install timing
```

## Use

```python
from pymss.plugins import require_capability
out = require_capability("pitch_shift")(audio, 44100, n_steps=2)      # up a tone
out = require_capability("time_stretch")(audio, 44100, rate=1.5)      # 1.5x faster
```
