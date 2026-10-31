# MSSeparator 参数说明

`MSSeparator` 是 pymss 的主要 Python API 入口，负责加载模型、执行分离、保存分离后的音轨。使用 catalog 模型时，推荐优先使用 `MSSeparator.from_model_name(...)`；只有在使用自定义权重、自定义 YAML 配置，或者需要完整控制运行参数时，才需要直接调用完整构造函数。

## 推荐入口

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

`from_model_name()` 会根据 pymss 的模型 catalog 或本地注册的用户模型解析 `model_type`、权重路径和配置路径，然后把剩余参数继续传给 `MSSeparator(...)`。

注册自定义本地模型后，可按名字复用：

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

## `from_model_name()` 参数

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `model_name` | `str` | 必填 | catalog 中的模型名，例如 `bs_roformer_voc_hyperacev2`。 |
| `model_dir` | `str \| None` | `None` | 查找或下载模型文件的目录。不传时使用 pymss 默认模型缓存位置。 |
| `download` | `bool` | `False` | 为 `True` 时，加载前会下载缺失的模型文件。为 `False` 时，缺少文件会直接报错。 |
| `source` | `str` | `"modelscope"` | 传给模型下载器的下载源。 |
| `endpoint` | `str \| None` | `None` | 可选的下载端点覆盖。 |
| `**kwargs` | 任意 | - | 直接转发给 `MSSeparator(...)`，例如 `device`、`output_format`、`store_dirs`、`save_as_folder`、`audio_params`、`debug`、`inference_params`。 |

## 构造函数

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

`model_type="auto"` 会在加载权重前调用 pymss-core 的 YAML 架构识别。配置中的显式架构声明优先于结构特征；声明冲突或信息不足时抛出 `ModelTypeDetectionError`（继承 `RuntimeError`），手动指定架构可覆盖自动识别。支持识别 BS/Mel RoFormer 与 Conformer、HTDemucs、MDX23C、SCNet、Apollo、Bandit v1/v2 的配置结构。BS PolarFormer 使用 `bs_roformer`，HyperACE 会进一步根据权重键区分。省略了识别特征的配置仍可能需要手动选择；没有 YAML 的 VR 和旧版模型需要显式指定类型。

## 构造函数参数

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `model_type` | `str` | 必填 | `auto` 根据 YAML 自动识别，或显式指定模型架构与运行类型。常见值包括 `bs_roformer`、`bs_conformer`、`mel_band_roformer`、`mel_band_conformer`、`htdemucs`、`mdx23c`、`bandit`、`bandit_v2`、`scnet`、`apollo`、`vr`、`legacy_demucs`、`legacy_tasnet`。使用 catalog 模型时通常不需要手动设置。 |
| `model_path` | `str` | 必填 | 模型权重文件路径。扩展名取决于模型类型，例如 `.ckpt`、`.th`、`.pth`。 |
| `config_path` | `str \| None` | `None` | MSS 类模型使用的 YAML 配置路径。不传时，pymss 会尝试使用 `model_path + ".yaml"`。VR 模型使用内置 VR 元数据，不使用 MSS YAML 配置。 |
| `device` | `str` | `"auto"` | 运行设备。可选值为 `auto`、`cpu`、`cuda`、`mps`、`mlx`。`auto` 优先选择 CUDA，其次 Apple MPS，最后 CPU。`mlx` 是 Apple Silicon MLX 后端的公开快捷写法，内部会通过 `device="mps"` 和 MLX 模型参数运行。 |
| `device_ids` | `list[int]` | `[0]` | CUDA 设备 ID 列表。传入多个 CUDA ID 时，支持的 Torch 模型可以使用 `torch.nn.DataParallel`。该参数不会选择多个 Apple MPS 或 MLX 设备。 |
| `output_format` | `str` | `"wav"` | `process_folder()` 和 `save_audio()` 保存文件时使用的格式。支持 `wav`、`flac`、`mp3`、`m4a`。 |
| `use_tta` | `bool` | `False` | 是否启用测试时增强。对 MSS 模型来说，会运行多个变换版本并合并结果。可能略微提升质量，但会增加推理时间。 |
| `store_dirs` | `str \| dict` | `"results"` | `process_folder()` 使用的输出路径。字符串表示所有保存的音轨都写入同一个文件夹；字典可以把不同音轨映射到文件夹、文件夹列表、`None` 或空值。`None` 或缺少某个音轨表示不保存该音轨。 |
| `save_as_folder` | `bool` | `False` | 为 `True` 且 `store_dirs` 最终只指向一个输出文件夹时，每个输入音频会单独保存到以音频 basename 命名的子文件夹中，例如 `results/song/song_vocals.wav`。当 `store_dirs` 是单一路径，或字典中所有需要保存的目标都指向同一文件夹时生效。 |
| `audio_params` | `dict` | 见下文 | 保存音频文件时使用的编码参数。 |
| `logger` | `logging.Logger \| None` | `None` | Logger 实例。不传时使用 `pymss.get_separation_logger()`。 |
| `debug` | `bool` | `False` | 是否启用 debug 日志，并关闭部分面向普通 CLI 输出的进度条行为。 |
| `progress_callback` | callable \| `None` | `None` | 可选进度回调，会传给底层 demix/VR 逻辑。demix 进度会以 `callback(done_seconds, total_seconds, message)` 的形式返回。 |
| `inference_params` | `dict` | 见下文 | 推理时的运行参数覆盖。支持的 key 取决于模型。 |

## 使用 `store_dirs` 控制输出

`store_dirs` 决定 `process_folder()` 会保存哪些音轨，以及保存到哪里。

```python
store_dirs = "results"
```

这会把所有音轨都写入 `results`。

```python
store_dirs = {
    "vocals": "results/vocals",
    "instrumental": ["results/instrumental", "backup/instrumental"],
    "drums": None,
}
```

这会把 `vocals` 写入一个文件夹，把 `instrumental` 写入两个文件夹，并跳过 `drums`。音轨名会和模型配置里的 instruments 匹配。初始化时，非法的音轨 key 会被移除并写入 warning 日志。

如果希望每个输入音频的输出音轨放在单独文件夹中，可以设置 `save_as_folder=True`：

```python
separator = MSSeparator.from_model_name(
    "bs_roformer_voc_hyperacev2",
    store_dirs="results",
    save_as_folder=True,
)
separator.process_folder("song.wav")
```

这会把音轨写入 `results/song/`，例如 `results/song/song_vocals.wav` 和 `results/song/song_instrumental.wav`。该选项只在 `store_dirs` 是单个文件夹路径，或字典里所有需要保存的目标都指向同一文件夹时生效。如果不同音轨被路由到不同文件夹，pymss 会保持原本的 `store_dirs` 布局。

当 `inference_params["normalize"]` 启用时，pymss 会把所有需要保存的音轨放在一起分离，以便根据这些输出音轨统一计算归一化增益。如果一个六轨模型只保存两个音轨，那么只会对这两个保存的音轨联动归一化。

## `audio_params`

`audio_params` 只影响文件保存，不影响模型推理。

| Key | 默认值 | 作用格式 | 说明 |
| --- | --- | --- | --- |
| `wav_bit_depth` | `"FLOAT"` | `wav` | WAV 编码。支持 `FLOAT`、`PCM_16`、`PCM_24`。 |
| `flac_bit_depth` | `"PCM_24"` | `flac` | FLAC 编码位深。`PCM_24` 会按 24-bit 风格写入；其他值会回退到 16-bit 行为。 |
| `mp3_bit_rate` | `"320k"` | `mp3` | 传给编码器的 MP3 码率。 |
| `m4a_bit_rate` | `"192k"` | `m4a` | 传给编码器的 M4A 码率。 |
| `m4a_codec` | `"aac"` | `m4a` | M4A 编码器。不传时使用 FFmpeg 内置的 `aac`。`aac_at` 在本地可用时仍可使用；如果当前 FFmpeg/PyAV 没有暴露 `aac_at`，会回退到 `aac`。 |
| `m4a_aac_at_quality` | `2` | 使用 `aac_at` 的 `m4a` | Apple AAC 编码器质量参数。选择 `aac` 时会被忽略。 |

## `inference_params`

`inference_params` 会在模型配置加载后覆盖运行时推理设置。大多数值默认来自模型 YAML，因此通常只需要传入想要覆盖的 key。

### 命名说明：`standardize` 和 `normalize`

这里有两个不同的归一化相关参数：

| 公开参数 | 含义 | 内部兼容细节 |
| --- | --- | --- |
| `standardize` | 旧的输入标准化。模型推理前会对输入 mix 做标准化，推理后再还原。 | 现有 MSS YAML 文件里仍然把这个开关写作 `inference.normalize`。为了兼容旧配置，pymss 不改 YAML key，但在公开 API/CLI 中把它命名为 `standardize`。当 `standardize` 为 `None` 时，使用 YAML 里的值；如果 YAML 没有该项，则视为 `False`。 |
| `normalize` | 新的输出峰值归一化。分离完成后，pymss 会从被返回或保存的音轨中找到最大峰值，计算一个共享增益，并把同一个增益应用到所有输出音轨。 | 这是 pymss 自己的运行时参数，不是旧 MSS YAML 里的 `inference.normalize`。目标峰值略低于 0 dBFS，也就是 `-0.01 dBFS`。 |

想控制模型输入端的旧标准化行为时，用 `standardize`。想让输出音轨统一做峰值音量归一化时，用 `normalize`。

### 常见 MSS 参数

| Key | 类型 | 说明 |
| --- | --- | --- |
| `batch_size` | int \| `None` | 同时处理的 chunk 数。较大的值可能提升吞吐，但会占用更多显存/内存。 |
| `overlap_size` | int \| `None` | MSS overlap 大小，具体单位跟模型实现有关。更大的 overlap 可能减少边界问题，但计算量更高。 |
| `chunk_size` | int \| `None` | 音频 chunk 大小覆盖。更大的 chunk 可能带来更好的连续性，但需要更多内存。 |
| `stem_batch_size` | int \| `None` | 在 `process_folder()` 中把输出音轨分批处理，用于降低峰值内存。`0` 或缺失表示不分批。输出 `normalize=True` 时会忽略该参数，因为联动归一化需要把所有保存音轨放在一起计算。 |
| `standardize` | bool \| `None` | 控制旧的输入标准化。`None` 表示使用模型 YAML 中的 `inference.normalize`；如果 YAML 没有该项，则为 `False`。 |
| `normalize` | bool | 是否启用输出音轨联动峰值归一化，目标峰值为 `-0.01 dBFS`。 |
| `mask_mode` | str \| `None` | 对暴露 `set_mask_mode()` 的模型设置 mask mode。 |
| `enable_tta` | bool | 某些模型/运行路径支持的 TTA 标志。顶层参数 `use_tta` 仍然是 `MSSeparator` 主要使用的 API 开关。 |
| `cuda_attention_backend` | str \| `None` | 支持的 RoFormer 类模块使用的 CUDA attention 后端。可选值包括 `auto`、`default`、`flash`、`cudnn`、`efficient`、`math`、`xformers`。 |
| `mps_attention_backend` | str \| `None` | 支持的 Apple MPS attention 后端。 |
| `mps_mlx_min_tokens` | int \| `None` | MPS/MLX attention 路径使用的最小 token 阈值。 |
| `mps_model_backend` | str \| `None` | 支持的 Apple Silicon 模型执行后端覆盖。 |
| `mps_model_compute_dtype` | str \| `None` | 支持的 Apple Silicon 模型执行 dtype，例如 `float16`。 |
| `use_amp` | bool | 在支持的模型/运行路径中启用自动混合精度。 |
| `fuse_conv_bn` | bool | 在支持的模型/运行路径中融合 convolution 和 batch normalization。 |
| `use_channels_last` | bool | 在支持的模型/运行路径中使用 channels-last 内存格式。 |
| `shifts` | int \| `None` | 支持 shift-based inference 的模型使用的 shift 数量。 |
| `split` | bool | 支持 split inference 的模型使用的开关。 |
| `overlap` | float \| `None` | 支持 Demucs 风格 split inference 的模型使用的比例型 overlap。 |

### VR 参数

VR 模型不使用 MSS YAML 配置。pymss 会在内部构建 VR 运行配置，然后应用支持的参数覆盖。

| Key | 类型 | 说明 |
| --- | --- | --- |
| `batch_size` | int | VR batch size。 |
| `window_size` | int | VR window size。 |
| `aggression` | int | VR 后端使用的分离强度。 |
| `enable_tta` | bool | 在支持时启用 VR TTA。 |
| `enable_post_process` | bool | 是否启用 VR 后处理。 |
| `post_process_threshold` | float | VR 后处理阈值。 |
| `high_end_process` | bool | 是否启用 VR high-end process。 |
| `use_amp` | bool | 在支持时启用混合精度。 |
| `fuse_conv_bn` | bool | 在支持时融合 convolution 和 batch normalization。 |
| `use_channels_last` | bool | 在支持时使用 channels-last 内存格式。 |
| `mps_model_backend` | str \| `None` | 支持时覆盖 Apple Silicon 后端。 |
| `mps_model_compute_dtype` | str \| `None` | 支持时覆盖 Apple Silicon 计算 dtype。 |
| `normalize` | bool | 对 VR 的 primary/secondary 输出音轨启用联动峰值归一化。 |

`standardize` 对 VR 模型没有实际意义。

## 推理和保存方法

### `process_folder(input_folder)`

接收单个音频文件路径或文件夹路径。它会加载音频、分离配置中的音轨、按照 `store_dirs` 保存输出，并返回成功处理的输入文件名列表。

```python
success_files = separator.process_folder("songs")
```

如果 `input_folder` 是文件夹，该方法会把文件夹直属子文件作为候选输入，不会递归遍历子文件夹。

### `separate(mix, pbar=True, stems=None, *, channel_layout=None)`

对已经加载好的音频数组执行分离，并返回 `dict[stem_name, audio_array]`。

```python
results = separator.separate(mix, stems=["vocals", "instrumental"])
vocals = results["vocals"]
```

`stems` 可以是 `None`、单个音轨名，或音轨名列表。为 `None` 时返回模型的全部音轨。输出 `normalize=True` 时，只会在返回的这些音轨之间计算归一化增益。

输入数组使用 `(samples,)` 或 `(channels, samples)`，返回数组通常使用 `(samples, channels)`。

| 输入 | 模型 | 处理方式与输出 |
|---|---|---|
| 单声道 | 单声道 | 推理一次，输出单声道。 |
| 单声道 | 立体声 | 复制输入声道，模型结果按声道取平均，输出单声道。 |
| 立体声 | 单声道 | 左右声道分别推理，再合成立体声。 |
| 立体声 | 立体声 | 保持立体声推理与输出。 |
| 大于两个声道 | 单声道 | 所有输入声道取平均，输出单声道。 |
| 大于两个声道 | 立体声 | 通过 PyAV 使用 FFmpeg 声道混合规则降混，输出立体声。 |

输出归一化在合并声道后执行，保留左右声道的相对音量。单声道模型处理立体声输入时需要两次推理，进度包含两次推理及启用的 TTA 轮次。

`process_folder()` 按文件中记录的声道布局降混。已经加载的多声道数组可通过 `channel_layout` 指定源声道顺序，例如 `"5.1(side)"`；未指定时采用 FFmpeg 对该声道数的默认布局，没有默认布局的声道数必须显式指定。Apollo 原生支持单声道和立体声，更大的输入会降混成立体声。

在 `process_folder()` 外读取文件时，可使用 `load_audio(path, downmix_stereo=True)` 保留单声道和立体声输入，并将更多声道降混成立体声。默认的 `load_audio()` 仍保留源文件的全部声道。

`load_audio(path, return_layout=True)` 返回 `(audio, sample_rate, channel_layout)`，普通调用仍返回两个值。两种工作流的内置文件加载器都会保留布局，经不改变声道的处理节点传到分离入口。自定义加载器返回 `(audio, sample_rate)` 时，沿用原有的单声道/立体声数组方向识别；返回三个值时，二维数组采用声道优先的 `(channels, samples)`，单声道也可使用一维数组。图工作流调用方可显式设置 `AudioArtifact.channel_layout`。模型结果使用新的单声道或立体声布局。合并不兼容的多声道布局，或在 ensemble 中截断多声道输入时，图节点会明确报错，避免丢失声道位置。

文件没有声道位置元数据时，加载器保留 `"3 channels"` 这样的未指定布局。librosa 成功解码但系统没有安装 ffprobe 时也采用这一规则。单声道模型仍可对这些声道取平均；工作流降混成立体声时需要明确的声道位置，缺失时会报错，不会猜测声道顺序。

### `save_audio(audio, sr, file_name, store_dir)`

使用 `output_format` 和 `audio_params` 写出单个音频数组。

```python
separator.save_audio(results["vocals"], 44100, "song_vocals", "results")
```

### `close()`

释放模型引用，并尽可能清理后端缓存。长时间运行的进程在不再需要某个 separator 时，可以调用它来更确定地释放内存。

```python
separator.close()
```

## 常见示例

### catalog 模型并启用输出归一化

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

### 自定义 MSS 权重并覆盖输入标准化

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

### 只保存指定音轨

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

这里只会保存 `vocals` 和 `drums`。启用输出归一化时，也只会让这两个保存的音轨共享同一个归一化增益。
