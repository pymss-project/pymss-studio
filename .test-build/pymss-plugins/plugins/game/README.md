# pymss-plugin-game

OpenVPI GAME (General Audio Manipulation and Estimation v1.0.0) vocal to MIDI transcription plugin for [pymss](https://github.com/pymss-project/pymss).

## Features

- High accuracy vocal melody extraction into standard MIDI format (.mid).
- Dynamic chunking with integrated silence slicer.
- Registers `vocal_to_midi` capability into pymss plugin pool.
- On Apple Silicon, uses `gamemlx` with a `small`, `medium`, or `large` model alias, or a local MLX model directory. An alias downloads its weights on first use.
- Other platforms use a local GAME `.pt` checkpoint with the Torch backend. Select the downloaded checkpoint in Studio.
- Passes the selected vocal language to checkpoints that provide a language map.
- Exposes the `GameVocalToMidi` workflow node and the v2 `pymss.game.vocal_to_midi` Operation.

## Installation

```sh
pymss install pymss.game
```

## Usage in Python

```python
from pymss.plugins import get_registry

vocal_to_midi = get_registry().get_capability("vocal_to_midi")
result = vocal_to_midi(
    audio_path="vocal.wav",
    model_path="model.pt",
    output_dir="./output",
    tempo=120.0,
)
print("MIDI exported to:", result.output_path)
```
