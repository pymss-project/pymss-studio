# MSSeparator Parameter Guide

`MSSeparator` is the main Python API entry point for loading a separation model, running inference, and saving separated stems. For catalog models, prefer `MSSeparator.from_model_name(...)`; use the full constructor when you need custom weights, a custom YAML config, or full control over runtime parameters.

## Recommended Entry Point

```python
from pymss import MSSeparator

separator = MSSeparator.from_model_name(
    "bs_roformer_voc_hyperacev2",
    download=True,
    model_dir="models",
    device="auto",
    output_format="wav",
    store_dirs="results",
    inference_params={
        "standardize": None,
        "normalize": False,
    },
)
separator.process_folder("path/to/input_file_or_folder")
```

`from_model_name()` resolves the model type, weight path, and config path from the pymss model catalog or a locally registered user model, then forwards the remaining keyword arguments to `MSSeparator(...)`.

To register a custom local model for reuse by name:

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

## `from_model_name()` Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `model_name` | `str` | required | Catalog model name, for example `bs_roformer_voc_hyperacev2`. |
| `model_dir` | `str \| None` | `None` | Directory used to find or download model files. When omitted, pymss uses its default model cache location. |
| `download` | `bool` | `False` | If `True`, missing model files are downloaded before loading. If `False`, loading fails when files are missing. |
| `source` | `str` | `"modelscope"` | Download source passed to the model downloader. |
| `endpoint` | `str \| None` | `None` | Optional downloader endpoint override. |
| `**kwargs` | any | - | Forwarded directly to `MSSeparator(...)`, such as `device`, `output_format`, `store_dirs`, `save_as_folder`, `audio_params`, `debug`, and `inference_params`. |

## Constructor

```python
separator = MSSeparator(
    model_type="auto",
    model_path="path/to/model",
    config_path="path/to/config.yaml",
    device="auto",
    device_ids=[0],
    output_format="wav",
    use_tta=False,
    store_dirs="results",
    save_as_folder=False,
    audio_params={
        "wav_bit_depth": "FLOAT",
        "flac_bit_depth": "PCM_24",
        "mp3_bit_rate": "320k",
        "m4a_bit_rate": "192k",
        "m4a_codec": "aac",
        "m4a_aac_at_quality": 2,
    },
    logger=None,
    debug=False,
    progress_callback=None,
    inference_params={
        "batch_size": None,
        "overlap_size": None,
        "chunk_size": None,
        "standardize": None,
        "normalize": False,
        "mask_mode": None,
    },
)
```

`model_type="auto"` delegates YAML architecture detection to pymss-core before loading weights. Explicit YAML architecture declarations take precedence over structural hints. Conflicting or insufficient information raises `ModelTypeDetectionError` (a `RuntimeError` subclass); use an explicit architecture to override detection. The detector recognizes BS/Mel RoFormer and Conformer, HTDemucs, MDX23C, SCNet, Apollo, and Bandit v1/v2 configuration layouts. BS PolarFormer uses `bs_roformer`, and HyperACE is refined from checkpoint keys. Configurations that omit identifying fields can still require manual selection. VR and legacy models without YAML require an explicit type.

## Constructor Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `model_type` | `str` | required | `auto` for YAML detection, or an explicit model architecture/runtime type. Common values include `bs_roformer`, `bs_conformer`, `mel_band_roformer`, `mel_band_conformer`, `htdemucs`, `mdx23c`, `bandit`, `bandit_v2`, `scnet`, `apollo`, `vr`, `legacy_demucs`, and `legacy_tasnet`. Catalog users normally do not set this manually. |
| `model_path` | `str` | required | Path to the model weights file. The extension depends on the model family, for example `.ckpt`, `.th`, or `.pth`. |
| `config_path` | `str \| None` | `None` | YAML config path for MSS-style models. If omitted, pymss tries `model_path + ".yaml"`. VR models are loaded from built-in VR metadata and do not use an MSS YAML config. |
| `device` | `str` | `"auto"` | Runtime device. Valid values are `auto`, `cpu`, `cuda`, `mps`, and `mlx`. `auto` chooses CUDA first, then Apple MPS, then CPU. `mlx` is a public shortcut for the Apple Silicon MLX backend and internally runs through `device="mps"` with MLX model settings. |
| `device_ids` | `list[int]` | `[0]` | CUDA device IDs. When more than one CUDA ID is provided, supported Torch models can be wrapped with `torch.nn.DataParallel`. This does not select multiple Apple MPS or MLX devices. |
| `output_format` | `str` | `"wav"` | File format used by `process_folder()` and `save_audio()`. Supported values are `wav`, `flac`, `mp3`, and `m4a`. |
| `use_tta` | `bool` | `False` | Enables test-time augmentation. For MSS models this runs multiple transformed variants and merges the result. It may improve quality slightly, but it increases inference time. |
| `store_dirs` | `str \| dict` | `"results"` | Output destination used by `process_folder()`. A string writes every saved stem to the same folder. A dict maps stem names to a folder, a list of folders, `None`, or an empty value. `None` or a missing stem means that stem is not saved. |
| `save_as_folder` | `bool` | `False` | When `True` and `store_dirs` resolves to one output folder, each input audio file gets its own subfolder named after the audio basename, for example `results/song/song_vocals.wav`. This applies when `store_dirs` is a single path, or when every saved dict destination points to the same folder. |
| `audio_params` | `dict` | see below | Encoding options used when saving audio. |
| `logger` | `logging.Logger \| None` | `None` | Logger instance. If omitted, pymss uses `pymss.get_separation_logger()`. |
| `debug` | `bool` | `False` | Enables debug logging and disables some progress bar behavior intended for normal CLI-style output. |
| `progress_callback` | callable \| `None` | `None` | Optional callback used by lower-level demixing code. Demix progress is reported as `callback(done_seconds, total_seconds, message)`. |
| `inference_params` | `dict` | see below | Runtime inference overrides. Keys are model-dependent. Unsupported keys are rejected by the server validation layer and ignored only when not passed to the relevant runtime path. |

## Output Routing With `store_dirs`

`store_dirs` controls which stems are saved by `process_folder()`.

```python
store_dirs = "results"
```

This writes every stem to `results`.

```python
store_dirs = {
    "vocals": "results/vocals",
    "instrumental": ["results/instrumental", "backup/instrumental"],
    "drums": None,
}
```

This writes `vocals` to one folder, writes `instrumental` to two folders, and skips `drums`. Stem names are matched against the model config instruments. Invalid stem keys are removed during initialization and logged as warnings.

Set `save_as_folder=True` when all saved stems should be grouped by input audio file:

```python
separator = MSSeparator.from_model_name(
    "bs_roformer_voc_hyperacev2",
    store_dirs="results",
    save_as_folder=True,
)
separator.process_folder("song.wav")
```

This writes stems to `results/song/`, such as `results/song/song_vocals.wav` and `results/song/song_instrumental.wav`. The option is active only when `store_dirs` is a single folder path, or when every saved destination in a `dict` points to the same folder. If different stems are routed to different folders, pymss keeps the normal `store_dirs` layout.

When `inference_params["normalize"]` is enabled, pymss separates all stems that will be saved together so the shared output normalization gain is computed across those stems. If you save only two stems from a six-stem model, those two saved stems are normalized together.

## `audio_params`

`audio_params` is only used when writing files. It does not affect model inference.

| Key | Default | Used by | Description |
| --- | --- | --- | --- |
| `wav_bit_depth` | `"FLOAT"` | `wav` | WAV encoding. Supported values are `FLOAT`, `PCM_16`, and `PCM_24`. |
| `flac_bit_depth` | `"PCM_24"` | `flac` | FLAC encoding depth. `PCM_24` writes 24-bit style samples; other values fall back to 16-bit behavior. |
| `mp3_bit_rate` | `"320k"` | `mp3` | MP3 bitrate passed to the encoder. |
| `m4a_bit_rate` | `"192k"` | `m4a` | M4A bitrate passed to the encoder. |
| `m4a_codec` | `"aac"` | `m4a` | M4A codec. If omitted, pymss uses FFmpeg's built-in `aac` encoder. `aac_at` is still accepted when available and falls back to `aac` when the local FFmpeg/PyAV build does not expose it. |
| `m4a_aac_at_quality` | `2` | `m4a` with `aac_at` | Apple AAC encoder quality option. Ignored when the selected encoder is `aac`. |

## `inference_params`

`inference_params` overrides runtime inference settings after the model config is loaded. Most values default to the model YAML, so you usually only pass keys that you want to override.

### Naming Note: `standardize` vs `normalize`

There are two different normalization-related options:

| Public parameter | Meaning | Internal compatibility detail |
| --- | --- | --- |
| `standardize` | Old input standardization. The input mix is standardized before model inference and restored afterward. | Existing MSS YAML files store this switch as `inference.normalize`. pymss keeps that YAML key for compatibility, but exposes the public/API/CLI name as `standardize`. If `standardize` is `None`, pymss uses the YAML value. If the YAML key is missing, it is treated as `False`. |
| `normalize` | New output peak normalization. After separation, pymss computes one shared gain from the loudest selected output stem and applies that same gain to every returned/saved stem. | This is a pymss runtime parameter, not the old MSS YAML `inference.normalize`. The target peak is just below 0 dBFS (`-0.01 dBFS`). |

Use `standardize` when you want to control the model's old input standardization behavior. Use `normalize` when you want the saved/returned stems to be peak-normalized together.

### Common MSS Parameters

| Key | Type | Description |
| --- | --- | --- |
| `batch_size` | int \| `None` | Number of chunks processed together. Larger values can improve throughput but use more memory. |
| `overlap_size` | int \| `None` | MSS overlap size in samples/chunks according to the model implementation. Higher overlap can reduce boundary artifacts but costs more compute. |
| `chunk_size` | int \| `None` | Audio chunk size override. Larger chunks can improve continuity but require more memory. |
| `stem_batch_size` | int \| `None` | Splits output stems into smaller groups during `process_folder()` to reduce peak memory. `0` or missing disables stem batching. Ignored when output `normalize=True`, because linked normalization needs all saved stems together. |
| `standardize` | bool \| `None` | Controls legacy input standardization. `None` means use the model config value from YAML `inference.normalize`; missing YAML value becomes `False`. |
| `normalize` | bool | Enables linked output peak normalization to `-0.01 dBFS`. |
| `mask_mode` | str \| `None` | Mask mode for models that expose `set_mask_mode()`. |
| `enable_tta` | bool | Model/runtime TTA flag where supported. The top-level `use_tta` parameter is still the main API switch used by `MSSeparator`. |
| `cuda_attention_backend` | str \| `None` | CUDA attention backend for supported RoFormer-style modules. Valid values include `auto`, `default`, `flash`, `cudnn`, `efficient`, `math`, and `xformers`. |
| `mps_attention_backend` | str \| `None` | Apple MPS attention backend for supported modules. |
| `mps_mlx_min_tokens` | int \| `None` | Minimum token threshold used by the MPS/MLX attention path. |
| `mps_model_backend` | str \| `None` | Backend override for supported Apple Silicon model execution paths. |
| `mps_model_compute_dtype` | str \| `None` | Compute dtype for supported Apple Silicon model execution paths, for example `float16`. |
| `use_amp` | bool | Enables automatic mixed precision where the model/runtime supports it. |
| `fuse_conv_bn` | bool | Fuses convolution and batch normalization where supported. |
| `use_channels_last` | bool | Uses channels-last memory format where supported. |
| `shifts` | int \| `None` | Shift count for model families that support shift-based inference. |
| `split` | bool | Split inference flag for model families that support it. |
| `overlap` | float \| `None` | Fractional overlap used by model families that expose Demucs-style split inference. |

### VR Parameters

VR models do not use MSS YAML configs. pymss builds a VR runtime config internally and then applies supported overrides.

| Key | Type | Description |
| --- | --- | --- |
| `batch_size` | int | VR batch size. |
| `window_size` | int | VR window size. |
| `aggression` | int | Separation aggressiveness used by the VR backend. |
| `enable_tta` | bool | Enables VR TTA where supported. |
| `enable_post_process` | bool | Enables VR post-processing. |
| `post_process_threshold` | float | Threshold used by VR post-processing. |
| `high_end_process` | bool | Enables high-end processing in the VR backend. |
| `use_amp` | bool | Enables mixed precision where supported. |
| `fuse_conv_bn` | bool | Fuses convolution and batch normalization where supported. |
| `use_channels_last` | bool | Uses channels-last memory format where supported. |
| `mps_model_backend` | str \| `None` | Apple Silicon backend override where supported. |
| `mps_model_compute_dtype` | str \| `None` | Apple Silicon compute dtype override where supported. |
| `normalize` | bool | Enables linked output peak normalization for the VR primary and secondary stems. |

`standardize` is not meaningful for VR models.

## Inference And Saving Methods

### `process_folder(input_folder)`

Accepts either a single audio file path or a folder path. It loads audio, separates configured stems, saves outputs according to `store_dirs`, and returns a list of successfully processed input file names.

```python
success_files = separator.process_folder("songs")
```

If `input_folder` is a folder, every direct child file in that folder is considered an input candidate. The method does not recursively walk subfolders.

### `separate(mix, pbar=True, stems=None, *, channel_layout=None)`

Runs separation on an already-loaded audio array and returns a dictionary mapping stem name to audio array.

```python
results = separator.separate(mix, stems=["vocals", "instrumental"])
vocals = results["vocals"]
```

`stems` can be `None`, a single stem name, or an iterable of stem names. When `None`, all model stems are returned. When output `normalize=True`, normalization is computed only across the returned stems.

Input arrays use `(samples,)` or `(channels, samples)`; returned arrays normally use `(samples, channels)`.

| Input | Model | Processing and output |
|---|---|---|
| Mono | Mono | Separate once and output mono. |
| Mono | Stereo | Duplicate the input channel, then average the model's output channels to mono. |
| Stereo | Mono | Separate left and right independently, then recombine them into stereo. |
| Stereo | Stereo | Preserve stereo processing and output. |
| More than two channels | Mono | Average all input channels and output mono. |
| More than two channels | Stereo | Use FFmpeg channel rematrixing through PyAV and output stereo. |

Output normalization runs after the output channels have been assembled, preserving their relative levels. Mono models require two inference passes for stereo inputs; progress includes both passes and any TTA variants.

`process_folder()` uses the file's encoded channel layout when downmixing to stereo. For already-loaded multichannel arrays, `channel_layout` specifies the source ordering (for example, `"5.1(side)"`). Without it, FFmpeg's default layout for the channel count is assumed; counts without a default layout require an explicit value. Apollo accepts mono or stereo natively and downmixes larger inputs to stereo.

For file decoding outside `process_folder()`, `load_audio(path, downmix_stereo=True)` preserves mono/stereo sources and downmixes larger layouts to stereo. The default `load_audio()` behavior continues to preserve all source channels.

`load_audio(path, return_layout=True)` returns `(audio, sample_rate, channel_layout)`; ordinary calls still return two values. Built-in file loaders in both workflow engines retain this layout through channel-preserving operations and pass it to separation. Custom workflow loaders returning `(audio, sample_rate)` retain the existing mono/stereo array orientation detection. The three-value form uses channel-first `(channels, samples)` arrays, or one-dimensional mono audio. Graph callers may set `AudioArtifact.channel_layout` explicitly. Model outputs receive their new mono/stereo layout. Graph operations that combine incompatible multichannel layouts or would truncate multichannel ensemble inputs raise an error instead of discarding channel positions.

When a file has no channel-position metadata, loaders preserve an unspecified layout such as `"3 channels"`. This also applies when librosa decodes successfully but ffprobe is not installed. Mono models can still average those channels. Workflow stereo downmix requires named channel positions and reports an error rather than assuming their ordering.

### `save_audio(audio, sr, file_name, store_dir)`

Writes one audio array using `output_format` and `audio_params`.

```python
separator.save_audio(results["vocals"], 44100, "song_vocals", "results")
```

### `close()`

Releases model references and clears backend caches where possible. Call this when a long-running process is done with a separator and wants to free memory deterministically.

```python
separator.close()
```

## Practical Examples

### Catalog Model With Output Normalization

```python
separator = MSSeparator.from_model_name(
    "bs_roformer_voc_hyperacev2",
    download=True,
    output_format="flac",
    inference_params={
        "normalize": True,
    },
)
separator.process_folder("input.wav")
```

### Custom MSS Weights With Input Standardization Override

```python
separator = MSSeparator(
    model_type="mel_band_roformer",
    model_path="models/custom.ckpt",
    config_path="models/custom.yaml",
    device="cuda",
    inference_params={
        "standardize": True,
        "normalize": False,
    },
)
```

### Save Only Selected Stems

```python
separator = MSSeparator.from_model_name(
    "some_six_stem_model",
    store_dirs={
        "vocals": "out/vocals",
        "drums": "out/drums",
    },
    inference_params={
        "normalize": True,
    },
)
```

Only `vocals` and `drums` are saved. With output normalization enabled, those two saved stems share one normalization gain.
