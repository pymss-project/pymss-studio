# pymss

Python package for music source separation. <br>
[English]   [简体中文](./README_CN.md)

## Install

pymss 2.2 and later requires Python 3.11 or newer.

If you want the CUDA build of PyTorch, install it first:

```sh
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
```

### AMD ROCm


```sh
pip install torch --index-url https://download.pytorch.org/whl/rocm7.2
pip install pymss
```

**Windows:** native support starts with ROCm 7.2.1 (Adrenalin 26.2.2+ driver, Python 3.12). Install the AMD wheels by direct URL, then pymss:

```bat
pip install --no-cache-dir https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/rocm_sdk_core-7.2.1-py3-none-win_amd64.whl https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/rocm_sdk_devel-7.2.1-py3-none-win_amd64.whl https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/torch-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl
pip install pymss
```

WSL2 (RDNA3/RDNA4) can follow the Linux steps instead.

### Ascend NPU

pymss runs on Huawei Ascend NPUs through `torch_npu`. Install the Ascend PyTorch adapter matching
your CANN toolkit and torch build (see the [Ascend torch_npu compatibility matrix](https://github.com/Ascend/pytorch/blob/master/COMPATIBILITY.en.md)).
Choose a combination with PyTorch 2.7.1 or later, then install pymss:

```sh
pip install pymss
```

Use `--device npu` (or `device="auto"`, which prefers CUDA, then NPU, then Apple Silicon).
`--device-id` selects the NPU index; if multiple IDs are supplied, only the first is used.
Multi-NPU parallel execution is not supported. On NPU, STFT/ISTFT uses `torch.fft` primitives.
RNN/LSTM/GRU layers use a CPU inference fallback for affected `torch_npu` builds, with results
returned to the NPU. This fallback does not support training and adds CPU transfer overhead.

For CLI and Python API usage, install:

```sh
pip install pymss
```

If you need API or WebUI, install this instead:

```sh
pip install "pymss[server]"
```

## Develop

Development requires Git, Python 3.11 or later, and [uv](https://docs.astral.sh/uv/). WebUI development also requires Node.js and npm.

Clone the Python package repository and install development dependencies:

```sh
git clone https://github.com/pymss-project/pymss
cd pymss
uv sync --group dev
```

If you need to develop or locally serve the WebUI, the WebUI source lives in a separate repository and must be built with Node.js:

```sh
git clone https://github.com/pymss-project/pymss-webui
cd pymss-webui
npm ci
npm run build
```

Copy the built WebUI assets into the Python package checkout:

```sh
cp -R dist/. ../pymss/server/webui_static/
```

Build source and wheel distributions from the Python package checkout:

```sh
cd ..
uv build
```

The test suite uses `pytest`. The migrated integration tests live in `test/` and are parameterized through `test/test_all.py`. They require local model weights, configs, and input audio; missing assets are skipped automatically.

```sh
uv run pytest test -q
```

## Usage

### CLI inference

Run inference by catalog model name. If the model, config, or auxiliary files are missing locally, the CLI downloads them automatically before inference.

```sh
pymss infer bs_roformer_voc_hyperacev2 \
  -i path/to/input_file_or_folder \
  -o results \
  --save-as-folder \
  --device auto \
  --format wav     # wav | flac | mp3 | m4a | aac | opus | vorbis | ogg
```

`--device auto` uses CUDA first when an NVIDIA GPU is available, then Ascend NPU (`torch_npu`), then Apple Silicon MLX. Use `--device npu` to force Ascend NPU, `--device mlx` to force MLX, or `--device mps` to force PyTorch MPS.
`--save-as-folder` writes each input audio file's stems under a subfolder named after that audio file, for example `results/song/song_vocals.wav`.

The default download source is ModelScope. You can choose another source or model directory:

```sh
pymss --model-dir /path/to/models infer bs_roformer_voc_hyperacev2 \
  --source hf-mirror \
  -i path/to/input_file_or_folder \
  -o results
```

When running from a source checkout without installation, use `python -m pymss.cli` instead of `pymss`.

### CLI workflow

Use a workflow file to chain multiple models automatically:

```sh
pymss workflow init -o vocal_chain.yaml
pymss workflow validate -c vocal_chain.yaml
pymss workflow run -c vocal_chain.yaml \
  -i path/to/input_file_or_folder \
  -o results \
  --download
```

In a workflow, `input: input` means the original audio, and `input: split.other` means the `other` stem produced by the `split` step. For folder inputs, workflow inference batches by step/model: step 1 runs for every input before step 2 is loaded. `save` controls which stems are written and which output subdirectory they use. By default, workflow batch outputs are grouped as `results/song/vocal/song_vocals.wav`; pass `--output-layout flat` to write them as `results/vocal/song_vocals.wav` instead. Duplicate input stems in the same batch are disambiguated with suffixes such as `song_3_vocals.wav`. Put shared inference options such as `batch_size` under `defaults.inference_params`, and put model-specific options such as each step's `overlap_size` under that step's `inference_params`.

### CLI comfy (comfy-mss JSON workflows)

Run native comfy-mss JSON workflows directly — no ComfyUI runtime required. The graph engine parses the JSON, resolves node links, and executes nodes on top of pymss's own `MSSeparator` and the capability pool:

```sh
pymss comfy run -c workflow.json \
  -i input.wav \
  -o results \
  --download
```

All comfy-mss nodes (`pymss_load_audio`, `pymss_mss_separate`, `pymss_audio_ensemble`, `pymss_save_audio`, ...) and ComfyUI core audio nodes (`SaveAudio`/`SaveAudioMP3`/`SaveAudioOpus`/`SaveAudioAdvanced`, `AudioMerge`, `AudioConcat`, `TrimAudioDuration`, `SplitAudioChannels`/`JoinAudioChannels`, `AudioAdjustVolume`, `EmptyAudio`, `AudioEqualizer3Band`, `PreviewAudio`, `LoadAudio`) are supported. Node executors consume built-in capabilities rather than reimplementing DSP, so the same `eq` / `mix` / `ensemble` / `{fmt}_encode` capabilities back the CLI, the Python API, and the workflow nodes. Pass `--no-strict` to skip unknown node types with a warning instead of failing.

### Graph execution events (Python API)

Graph hosts can observe execution without adding fields to ComfyUI workflows or changing node widgets:

```python
from pymss.graph import load_comfy_file, run_dag

def on_event(event):
    print(event["event"], event["node_id"], event["node_fraction"])

outputs = run_dag(
    load_comfy_file("workflow.json"),
    output_dir="results",
    execution_event_callback=on_event,
)
```

`EXECUTION_EVENT_VERSION = 1` describes this optional, synchronous callback API. It is independent of ComfyUI's WebSocket transport; in particular, ComfyUI's `executed` message represents a UI update, while `node_completed` here always means the executor completed that node. Hosts translate events to their own display or transport. JSON parsing errors occur before `run_dag` and are raised by the loader. Existing `progress_callback`, `progress_event_callback`, and `PROGRESS_EVENT_VERSION` retain their contracts.

| Events | Meaning |
| --- | --- |
| `execution_started`, `execution_completed` | Graph execution begins or finishes successfully, including empty DAGs. |
| `node_started`, `node_completed` | A node begins execution or returns successfully. |
| `node_progress` | Intermediate progress. A fraction of 1 can finish one audio item without completing the node. |
| `node_failed`, `execution_failed` | Execution failed; the original exception still propagates. |
| `node_cancelled`, `execution_cancelled` | Execution was interrupted by `KeyboardInterrupt` or `asyncio.CancelledError`; the interruption still propagates. |
| `node_skipped` | An unknown node was skipped under an explicitly requested `strict=False`. |

Every event contains `event`, `version`, `message`, `node_count`, and `overall_fraction`. Node-associated events also contain `node_id` (string), `node_type`, zero-based `node_index`, and `node_fraction`; these four fields are `None` when no node is involved. Progress events may include `audio_index` (one-based), `audio_count`, `done`, `total`, and `unit`. Failure/cancellation events contain `error_type`; an execution-level failure also identifies its node when available. Graph validation errors have no fabricated active node. Input-resolution errors can report `node_failed` before `node_started`.

- Cancellation events report interruptions raised during execution. Cancellation remains controlled by the host; killing the process cannot deliver a final callback. Already written files are retained.
- Callbacks must return promptly. `run_dag` propagates execution-observer errors and releases its owned separator cache; they are not reported back to the failing observer as node failures. If an observer raises while receiving an execution failure, the original execution exception and its explicit cause are preserved; the observer exception remains in `__context__`.
- Event delivery does not change the default single-model cache limit. The caller remains responsible for closing a supplied `SeparatorCache`.
- `LegacyWorkflowRunner` accepts `execution_event_callback` and adds `track` to events. `KeyboardInterrupt` and `asyncio.CancelledError` stop the whole folder run even with `continue_on_error=True`.

### CLI ensemble

```sh
pymss ensemble path/to/model_a_vocals.wav path/to/model_b_vocals.wav \
  --algorithm avg_wave \
  --weights 1 0.8 \
  -o results/ensemble_vocals.wav
```

Available algorithms are `avg_wave`, `median_wave`, `min_wave`, `max_wave`, `avg_fft`, `median_fft`, `min_fft`, and `max_fft`. Input files must use the same sample rate and channel count. Files with different lengths are truncated to the shortest input. If `--weights` is omitted, every input uses weight `1`.

### Server and WebUI

Install the optional server dependencies to run a HTTP server with dynamic model loading, catalog browsing, model downloads, and an optional browser WebUI:

```sh
pip install "pymss[server]"
pymss serve --webui
```

See [server CLI docs](./docs/server/cli.md), [server API docs](./docs/server/api.md), and [server error docs](./docs/server/errors.md) for details.

### Plugin system

pymss has a plugin system for extending capabilities — audio DSP operations, audio codecs, and workflow nodes. Built-in functionality (23 capabilities: 15 DSP/channel ops + 8 codecs) is registered through the same mechanism plugins use, so core and extensions share one pool.

**Install a plugin** — use pymss 2.2 or later and specify a full plugin id from the official registry, a repository URL, or a local path. Append `#subdir` to install from a monorepo subdirectory:

```sh
pymss install pymss.loudnorm                             # full plugin id
pymss install "https://github.com/pymss-project/pymss-plugins#plugins/loudnorm"
pymss install https://github.com/pymss-project/pymss-plugins --subpath plugins/loudnorm
pymss install ./my-local-plugin
```

To select a source revision, use `pymss.loudnorm@<tag-or-commit>`, replacing the placeholder with an existing tag or full commit SHA from the source repository. A plugin's package version does not imply that a matching Git tag exists.

Plugin dependencies (declared in its `pyproject.toml`) are **installed automatically** into the current environment — uv is used when the venv is uv-managed, pip otherwise. Pass `--no-deps` to skip.

**Manage plugins:**

```sh
pymss plugins list           # installed plugins: version, source, load status
pymss plugins available      # browse the official registry
pymss plugins search loudness   # search the registry by name/description/tag
pymss plugins update pymss.loudnorm   # update from the recorded source
pymss uninstall pymss.loudnorm
pymss plugins dir            # print the plugins directory
```

`update` works identically for **official and third-party (URL/path-installed) plugins** — pymss records each install's provenance. Update and uninstall commands use the manifest's plugin id, which is also the installed directory name.

Plugins live in `~/.pymss/plugins/` (override with `PYMSS_PLUGINS_DIR`) and need a v2 manifest (see `docs/plugin-protocol.md`). A single plugin failing to load never blocks others. The official plugin registry is a separate `pymss-plugins` repo (a `registry.json` keyed by plugin id) — point `pymss install <id>` at a custom one via `PYMSS_PLUGINS_REGISTRY`.

**Write a plugin** — create `pyproject.toml` and `__init__.py` in `my-local-plugin/`. The v2 manifest declares the plugin identity and every Operation registered by `setup(plugin)`.

`pyproject.toml`:

```toml
[project]
name = "pymss-plugin-gain"
version = "0.1.0"

[tool.pymss.plugin]
id = "author.gain"
api_version = 2
requires_pymss = ">=2.2"

[[tool.pymss.plugin.operations]]
id = "author.gain.apply"
title = "Apply Gain"
```

`__init__.py`:

```python
def run_gain(ctx, inputs, params):
    audio = inputs["audio"]
    gain = 10 ** (params["gain_db"] / 20.0)
    return {"audio": type(audio)(
        audio.audio * gain, audio.sample_rate, channel_layout=audio.channel_layout,
    )}


def setup(plugin):
    plugin.register_operation(
        id="author.gain.apply",
        title="Apply Gain",
        inputs={"audio": {"type": "audio"}},
        params={
            "type": "object",
            "properties": {
                "gain_db": {"type": "number", "default": 0.0, "minimum": -60.0, "maximum": 60.0},
            },
            "additionalProperties": False,
        },
        outputs={"audio": {"type": "audio"}},
        run=run_gain,
    )
```

```sh
pymss plugins validate ./my-local-plugin
pymss install ./my-local-plugin
pymss ops describe author.gain.apply
```

The declared Operation ids must exactly match those registered by `setup()`. Register capabilities, nodes, and CLI commands inside `setup()` through `plugin.register_capability`, `plugin.register_node`, and `plugin.register_cli`. Plugins without a v2 manifest are not loaded. See the [plugin protocol](./docs/plugin-protocol.md) and [development guide](./docs/plugins.md) for details.

A capability is a named function in a flat global pool; nodes/CLI/library calls look it up by name. Providers and consumers can ship in different packages, coupled only by the capability name — so someone adapting `SaveAudioOpus` reuses the `opus_encode` capability instead of rewriting the encoder.

Built-in capabilities include: `to_mono`, `split_channels`, `join_channels`, `adjust_volume`, `invert_phase`, `normalize_peak`, `standardize`/`destandardize`, `trim`, `concat`, `mix`, `empty_audio`, `eq`, `resample`, `ensemble`, and `{wav,flac,mp3,m4a,aac,opus,vorbis,ogg}_encode`.

### Python API

Use a catalog model name directly. You do not need to pass `model_type`, `model_path`, or `config_path`.

```python
from pymss import MSSeparator

separator = MSSeparator.from_model_name(
    "bs_roformer_voc_hyperacev2",
    download=True,
    device="auto",
    output_format="wav",
    store_dirs="results",
)
separator.process_folder("path/to/input_file_or_folder")
separator.close()
```

`download=True` downloads missing model files before loading. Omit it for strict local-only loading.

`MSSeparator` can also be used as a context manager. Leaving the `with` block automatically calls `separator.close()`, which releases model references and clears backend caches where possible.

```python
from pymss import MSSeparator

with MSSeparator.from_model_name(
    "bs_roformer_voc_hyperacev2",
    download=True,
    device="auto",
    output_format="wav",
    store_dirs="results",
) as separator:
    separator.process_folder("path/to/input_file_or_folder")
```

### Register custom models

Register local weights + config once, then reuse the name like a catalog model (`~/.cache/pymss/user_models.json`, override with `PYMSS_USER_MODELS`):

```sh
pymss register my_bs --type bs_conformer --model /path/model.ckpt --config /path/config.yaml --overlap-size 44100
pymss infer my_bs -i song.wav -o results
pymss list --user-only
pymss unregister my_bs
```

```python
from pymss import register_model, MSSeparator

register_model(
    "my_bs",
    "bs_conformer",
    "/path/model.ckpt",
    "/path/config.yaml",
    overlap_size=44100,
)
separator = MSSeparator.from_model_name("my_bs")
```

### Manual model paths

Use the full constructor for custom weights that are not in the model catalog.

```python
from pymss import MSSeparator, get_separation_logger

# init
separator = MSSeparator(
    model_type='auto',
    model_path='path/to/model',
    config_path='path/to/config',
    device='cuda',
    device_ids=[0],
    output_format='wav',
    use_tta=True,
    store_dirs={
        "vocals": "./output/vocals",
        "other": None # None or missing this stem will result in no output file for this stem. This example will output the vocal's stem in ./output/vocals and ignoring the other(instrumental) stem. Making sure the key(s) match the config file.
    },
    save_as_folder=False,
    audio_params={"wav_bit_depth": "FLOAT", "flac_bit_depth": "PCM_24", "mp3_bit_rate": "320k", "m4a_bit_rate": "192k", "m4a_aac_at_quality": 2}, # Can be omitted
    logger=get_separation_logger(), # Can be omitted
    debug=False, # Can be omitted
    inference_params={
        "batch_size": 4,
        "overlap_size": 512,
        "chunk_size": 1024,
        "standardize": True,
        "normalize": False
    } # Can be omitted
)
with separator as s:
    s.process_folder('path/to/input_file_or_folder')
```

`model_type="auto"` detects the architecture from the YAML configuration using pymss-core. It supports explicit architecture declarations and recognized configurations for BS/Mel RoFormer and Conformer, HTDemucs, MDX23C, SCNet, Apollo, and Bandit v1/v2. BS PolarFormer uses `bs_roformer`; HyperACE is refined from checkpoint keys during loading. Unknown or ambiguous configurations raise `ModelTypeDetectionError` (a `RuntimeError` subclass) before weights are loaded; specify `model_type` manually in that case. VR and legacy models without YAML require an explicit type.

### Manual Constructor Parameters

For a detailed explanation of every `MSSeparator` argument, see the [MSSeparator parameter guide](./docs/msseparator.md).

- model_type: Use 'auto' for YAML-based detection, or specify an architecture, e.g., 'htdemucs'. Explicit types include
    ['bs_roformer', 
    'bs_conformer',
    'mel_band_roformer', 
    'mel_band_conformer',
    'htdemucs', 
    'mdx23c', 
    'bandit', 
    'bandit_v2', 
    'scnet', 
    'apollo',
    'vr']
- model_path: The path to the model file.
- config_path: The path to the configuration file.
- device: The type of device, default is 'auto'. Must be one of ['auto', 'cuda', 'npu', 'mps', 'cpu']
- device_ids: List of device IDs, default is [0].
- output_format: The output audio format, default is 'wav'. One of wav, flac, mp3, m4a, aac, opus, vorbis, ogg.
- use_tta: Whether to use TTA, default is False. Using TTA will triple the processing time with a little bit improvement in quality.
- store_dirs: Storage directories, can be a single folder path or a dictionary with instrument keys.
- save_as_folder: When True and store_dirs points to one output folder, save each input audio file's stems in a subfolder named after the audio file.
- audio_params: Audio parameters including wav_bit_depth, flac_bit_depth, mp3_bit_rate, m4a_bit_rate, and m4a_aac_at_quality. Default is {"wav_bit_depth": "FLOAT", "flac_bit_depth": "PCM_24", "mp3_bit_rate": "320k", "m4a_bit_rate": "192k", "m4a_aac_at_quality": 2}.
- logger: Logger instance. Default is pymss.get_separation_logger()
- debug: Whether to enable debug mode, default is False.
- inference_params: Inference parameters including batch_size, overlap_size, chunk_size, standardize, normalize, and `cuda_attention_backend`. `standardize` controls model input standardization and defaults to the model config's `inference.normalize` value, or `False` when missing. `normalize` controls linked output peak normalization for all returned stems. For `model_type='vr'`, supported keys are `batch_size`, `window_size`, `aggression`, `enable_tta`, `enable_post_process`, `post_process_threshold`, `high_end_process`, and output `normalize`.

### CUDA Attention Backend

RoFormer-family models default to cuDNN attention on CUDA when the installed PyTorch build exposes it, and to the memory-efficient SDPA backend (AOTriton) on ROCm/HIP; otherwise they use PyTorch's default SDPA path. Override with `inference_params={"cuda_attention_backend": "auto"}` if you want fallback probing. Valid values are `auto`, `default`, `flash`, `cudnn`, `efficient`, `math`, and `xformers`. `auto` tries cuDNN attention first, then PyTorch memory-efficient SDPA, then PyTorch default SDPA. `xformers` is optional and only used if installed locally; it is not a required dependency.

### Apple Silicon MLX Backend

Use `device='mlx'` to run the Apple Silicon MLX backend:

```python
separator = MSSeparator.from_model_name(
    "bs_roformer_voc_hyperacev2",
    download=True,
    device="mlx",
    output_format="wav",
    store_dirs="results",
)
```

On Apple Silicon, `pyproject.toml` installs `mlx>=0.31.0` for this backend. If MLX is missing or a non-VR backend fails, the model records `_pymss_mlx_full_backend_error` and falls back to Torch MPS. Advanced users can still override `mps_model_backend` and `mps_model_compute_dtype` through `inference_params`.

### Model Compatibility

BS PolarFormer checkpoints use `model_type="bs_roformer"` with `model.use_pope: true` in the matching YAML configuration. No separate PoPE or Triton package is required; MLX backend requests for these models fall back to PyTorch.

HTDemucs checkpoints whose config uses `model: htdemucs` and `htdemucs.cac: true` are supported through `model_type='htdemucs'`.

Legacy Demucs/TasNet `.th` weights can use `model_type='legacy_demucs'` or `model_type='legacy_tasnet'` without a MSST YAML config. The dependency-free legacy loader supports classic Demucs, v3 time-domain Demucs, ConvTasNet, CaC HDemucs, package-style HTDemucs, multi-frequency CaC HDemucs, and simple Demucs bag YAML files. DiffQ-quantized checkpoints and non-CaC/Wiener HDemucs still need a dedicated legacy loader.

UVR VR support is available for the supported UVR/VR series `.pth` weights. Use the catalog model name in the same CLI/API paths as other models. The output stems are read from the built-in VR model list, for example `Vocals`, `Instrumental`, `No Echo`, or `Echo`.

```sh
pymss infer 1_HP-UVR \
  -i path/to/input_folder \
  -o results \
  --device auto \
  --param batch_size=2 \
  --param window_size=512 \
  --param aggression=5
```

```python
separator = MSSeparator.from_model_name(
    "1_HP-UVR",
    download=True,
    device="auto",
    output_format="wav",
    store_dirs="results",
    inference_params={
        "batch_size": 2,
        "window_size": 512,
        "aggression": 5,
    },
)
with separator as s:
    s.process_folder("path/to/input_file_or_folder")
```

### Hugging Face Configs

Some model configs downloaded from Hugging Face or MSST-WebUI use `inference.num_overlap`. This optimized pymss path uses `inference.overlap_size` instead. When a config only has `num_overlap`, pymss now converts it automatically:

```text
overlap_size = chunk_size - chunk_size // num_overlap
```

For `num_overlap: 2` that is 50% overlap (correct MSST match, but slower). Prefer an explicit smaller `overlap_size` for speed, or pass it through `inference_params`.

Recommended fast setting:

```yaml
audio:
  chunk_size: 480000
inference:
  batch_size: 2
  overlap_size: 24000  # 5% of chunk_size
```

### RTX 5090 Benchmark

Measured on an NVIDIA GeForce RTX 5090 with PyTorch 2.9.1+cu128, CUDA 12.8, no TTA, one warmup and three measured runs.

| model | type | RTFx | 1-hour audio |
|---|---|---:|---:|
| BS-Roformer-HyperACE_v2_voc | bs_roformer | 231.83x | 15.5s |
| model_bs_roformer_ep_368_sdr_12.9628 | bs_roformer | 109.06x | 33.0s |
| logic_bs_roformer | bs_roformer | 159.71x | 22.5s |
| mel-band-roformer-deux | mel_band_roformer | 169.93x | 21.2s |
| Mel-Band-Roformer-big | mel_band_roformer | 194.05x | 18.6s |
| model_vocals_mdx23c_sdr_10.17 | mdx23c | 209.41x | 17.2s |
| HTDemucs4 | htdemucs | 200.52x | 18.0s |
| scnet_checkpoint_musdb18 | scnet | 356.85x | 10.1s |
| model_bandit_plus_dnr_sdr_11.47 | bandit | 122.76x | 29.3s |
| checkpoint-multi_state_dict | bandit_v2 | 112.33x | 32.0s |
| Apollo_LQ_MP3_restoration | apollo | 100.62x | 35.8s |

VR models were measured with `batch_size=2`, `window_size=512`, `aggression=5`, TTA off, post-processing off.

| VR model | RTFx | 1-hour audio |
|---|---:|---:|
| UVR-DeNoise-Lite | 243.62x | 14.8s |
| Harmonic_Noise_Separation_yxlllc | 221.22x | 16.3s |
| MGM_HIGHEND_v4 | 217.39x | 16.6s |
| MGM_LOWEND_A_v4 | 133.67x | 26.9s |
| MGM_MAIN_v4 | 118.56x | 30.4s |
| 11_SP-UVR-2B-32000-2 | 109.73x | 32.8s |
| 10_SP-UVR-2B-32000-1 | 109.03x | 33.0s |
| 12_SP-UVR-3B-44100 | 104.67x | 34.4s |
| MGM_LOWEND_B_v4 | 100.64x | 35.8s |
| 15_SP-UVR-MID-44100-1 | 99.00x | 36.4s |
| 16_SP-UVR-MID-44100-2 | 98.76x | 36.5s |
| 13_SP-UVR-4B-44100-1 | 97.78x | 36.8s |
| 14_SP-UVR-4B-44100-2 | 94.97x | 37.9s |
| 5_HP-Karaoke-UVR | 94.72x | 38.0s |
| 2_HP-UVR | 93.94x | 38.3s |
| UVR-De-Echo-Aggressive | 90.99x | 39.6s |
| UVR-DeNoise | 90.39x | 39.8s |
| UVR-De-Echo-Normal | 87.25x | 41.3s |
| UVR-DeReverb-aufr33-jarredou_4band_v4_ms_fullband | 86.70x | 41.5s |
| UVR-DeEcho-DeReverb | 86.58x | 41.6s |
| 3_HP-Vocal-UVR | 85.15x | 42.3s |
| 4_HP-Vocal-UVR | 84.23x | 42.7s |
| 1_HP-UVR | 84.06x | 42.8s |
| 17_HP-Wind_Inst-UVR | 82.92x | 43.4s |
| 6_HP-Karaoke-UVR | 81.81x | 44.0s |
| UVR-BVE-4B_SN-44100-1 | 81.54x | 44.2s |
| 9_HP2-UVR | 58.48x | 61.6s |
| 8_HP2-UVR | 57.23x | 62.9s |
| 7_HP2-UVR | 56.10x | 64.2s |
