# pymss
用于音乐源分离的 Python 包。
[English](./README.md)  [简体中文]

## 安装

pymss 2.2 及后续版本要求 Python 3.11 或更高版本。

如果想安装 CUDA 版 PyTorch，推荐先运行：

```sh
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
```

### 昇腾 NPU（Ascend）

pymss 通过 `torch_npu` 支持华为昇腾 NPU。请先安装与 CANN 工具链及 torch 版本匹配的昇腾 PyTorch 适配器（参见 [Ascend torch_npu 兼容性矩阵](https://github.com/Ascend/pytorch/blob/master/COMPATIBILITY.en.md)），选择 PyTorch 2.7.1 或更高版本的组合，再安装 pymss：

```sh
pip install pymss
```

使用 `--device npu`（或 `device="auto"`，其优先级为 CUDA → NPU → Apple Silicon）。`--device-id` 指定 NPU 编号；传入多个编号时仅使用第一项，目前不支持多 NPU 并行。NPU 上的 STFT/ISTFT 使用基于 `torch.fft` 原语的设备内实现；针对相关 `torch_npu` 版本，RNN/LSTM/GRU 循环层使用 CPU 推理回退，结果返回 NPU。此回退不支持训练，并会增加 CPU 数据传输开销。

### AMD ROCm

```sh
pip install torch --index-url https://download.pytorch.org/whl/rocm7.2
pip install pymss
```

**Windows：** 原生支持自 ROCm 7.2.1 起（Adrenalin 26.2.2+ 驱动、Python 3.12）。直链安装 AMD 官方 wheel 后再装 pymss：

```bat
pip install --no-cache-dir https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/rocm_sdk_core-7.2.1-py3-none-win_amd64.whl https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/rocm_sdk_devel-7.2.1-py3-none-win_amd64.whl https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/torch-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl
pip install pymss
```

WSL2（RDNA3/RDNA4）可改在 Ubuntu 子系统里按上面的 Linux 步骤安装。

如果只需要 CLI 和 Python API，安装：

```sh
pip install pymss
```

如果需要使用 API 或者 WebUI，则改为安装：

```sh
pip install "pymss[server]"
```

## 开发

开发需要 Git、Python 3.11 或更高版本，以及 [uv](https://docs.astral.sh/uv/)。WebUI 开发还需要 Node.js 和 npm。

克隆 Python 包仓库，并安装开发依赖：

```sh
git clone https://github.com/pymss-project/pymss
cd pymss
uv sync --group dev
```

如果需要开发或本地运行 WebUI, WebUI 源码位于单独仓库，需要使用 Node.js 构建：

```sh
git clone https://github.com/pymss-project/pymss-webui
cd pymss-webui
npm ci
npm run build
```

把构建后的 WebUI 静态资源复制到 Python 包目录：

```sh
cp -R dist/. ../pymss/server/webui_static/
```

回到 Python 包目录，构建源码包和 wheel：

```sh
cd ..
uv build
```

测试使用 `pytest`。迁移后的集成测试位于 `test/`，统一通过 `test/test_all.py` 参数化运行。这些测试依赖本地模型权重、配置文件和输入音频；缺少资源时会自动跳过。

```sh
uv run pytest test -q
```

## 用法

### CLI 推理

可以直接用 catalog 里的模型名推理。如果本地缺少模型、配置或辅助文件，CLI 会在推理前自动下载。

```sh
pymss infer bs_roformer_voc_hyperacev2 \
  -i path/to/input_file_or_folder \
  -o results \
  --save-as-folder \
  --device auto \
  --format wav     # wav | flac | mp3 | m4a | aac | opus | vorbis | ogg
```

`--device auto` 在有 NVIDIA GPU 时优先使用 CUDA，其次是昇腾 NPU（`torch_npu`），再次是 Apple Silicon MLX。可以用 `--device npu` 强制昇腾 NPU、`--device mlx` 强制 MLX，或用 `--device mps` 强制 PyTorch MPS。
`--save-as-folder` 会把每个输入音频的输出音轨保存到以该音频命名的子文件夹中，例如 `results/song/song_vocals.wav`。

默认下载源是 ModelScope。也可以指定下载源或模型目录：

```sh
pymss --model-dir /path/to/models infer bs_roformer_voc_hyperacev2 \
  --source hf-mirror \
  -i path/to/input_file_or_folder \
  -o results
```

如果是在源码目录里未安装运行，可以用 `python -m pymss.cli` 代替 `pymss`。

### CLI Workflow

可以用 workflow 文件把多个模型串成自动流程：

```sh
pymss workflow init -o vocal_chain.yaml
pymss workflow validate -c vocal_chain.yaml
pymss workflow run -c vocal_chain.yaml \
  -i path/to/input_file_or_folder \
  -o results \
  --download
```

workflow 中的 `input: input` 表示原始音频，`input: split.other` 表示使用 `split` 步骤输出的 `other` 音轨。文件夹输入会按 step/model 批处理：第 1 个 step 会先跑完所有输入，再加载第 2 个 step。`save` 控制保存哪些音轨以及保存到输出目录下的哪个子目录。默认批量输出会按 `results/song/vocal/song_vocals.wav` 分到每个音频的子目录；加 `--output-layout flat` 后会输出为 `results/vocal/song_vocals.wav`。同一批里输入 stem 重名时会自动加后缀，例如 `song_3_vocals.wav`。共享推理参数如 `batch_size` 可以放在 `defaults.inference_params`，每个模型单独的参数如各自的 `overlap_size` 可以放在该 step 的 `inference_params`。

### CLI Comfy（comfy-mss JSON 工作流）

直接运行原生 comfy-mss JSON 工作流，无需 ComfyUI 运行时。图引擎会解析 JSON、解析节点连线，并在 pymss 自己的 `MSSeparator` 和能力池之上执行节点：

```sh
pymss comfy run -c workflow.json \
  -i input.wav \
  -o results \
  --download
```

所有 comfy-mss 节点（`pymss_load_audio`、`pymss_mss_separate`、`pymss_audio_ensemble`、`pymss_save_audio` 等）和 ComfyUI 核心音频节点（`SaveAudio`/`SaveAudioMP3`/`SaveAudioOpus`/`SaveAudioAdvanced`、`AudioMerge`、`AudioConcat`、`TrimAudioDuration`、`SplitAudioChannels`/`JoinAudioChannels`、`AudioAdjustVolume`、`EmptyAudio`、`AudioEqualizer3Band`、`PreviewAudio`、`LoadAudio`）都已支持。节点执行器消费内置能力而不是重写 DSP，因此同一套 `eq`/`mix`/`ensemble`/`{fmt}_encode` 能力同时支撑 CLI、Python API 和工作流节点。加 `--no-strict` 可以在遇到未知节点时警告并跳过，而不是报错。

### 图执行事件（Python API）

宿主程序可以订阅执行状态，无需向 ComfyUI 工作流添加字段，也不改变节点参数：

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

`EXECUTION_EVENT_VERSION = 1` 表示这个可选同步回调接口的版本。它独立于 ComfyUI 的 WebSocket 通信协议：ComfyUI 的 `executed` 表示节点返回了界面更新，而这里的 `node_completed` 明确表示该节点执行完成。宿主程序负责将事件接入自己的显示或通信层。JSON 解析发生在 `run_dag` 之前，解析错误仍由加载器直接抛出。已有 `progress_callback`、`progress_event_callback` 和 `PROGRESS_EVENT_VERSION` 保持原有约定。

| 事件 | 含义 |
| --- | --- |
| `execution_started`、`execution_completed` | 图执行开始或成功结束，空 DAG 同样有完整事件。 |
| `node_started`、`node_completed` | 节点开始执行或成功返回。 |
| `node_progress` | 中间进度；比例为 1 可能只表示某个音频处理完成，不代表整个节点完成。 |
| `node_failed`、`execution_failed` | 执行失败，原始异常仍向调用方抛出。 |
| `node_cancelled`、`execution_cancelled` | 执行被 `KeyboardInterrupt` 或 `asyncio.CancelledError` 中断；中断仍向调用方抛出。 |
| `node_skipped` | 调用方明确指定 `strict=False` 后跳过了未知节点。 |

每个事件包含 `event`、`version`、`message`、`node_count` 和 `overall_fraction`。关联节点的事件还包含字符串形式的 `node_id`、`node_type`、从 0 开始的 `node_index` 和 `node_fraction`；未关联节点时这四项为 `None`。进度事件可包含从 1 开始的 `audio_index`、`audio_count`、`done`、`total`、`unit`。失败和取消事件包含 `error_type`，图级失败在能确定节点时也携带节点信息。图结构校验失败不会虚构执行节点；输入解析失败可能先于 `node_started` 产生 `node_failed`。

- 取消事件记录执行期间抛出的中断，取消方式由宿主程序控制；强制结束进程无法发送最终回调。已经写出的文件会保留。
- 回调应及时返回。`run_dag` 会抛出执行事件回调自身的异常，并清理本次执行拥有的分离器缓存；不会再次向这个出错的回调发送节点失败事件。如果回调在接收执行失败事件时又抛出异常，原始执行异常及其显式原因保持不变，回调异常保留在 `__context__` 中。
- 事件接口不改变默认只缓存一个模型的限制；调用方传入的 `SeparatorCache` 仍由调用方负责关闭。
- `LegacyWorkflowRunner` 同样接受 `execution_event_callback`，并为事件补充 `track`。即使指定 `continue_on_error=True`，`KeyboardInterrupt` 和 `asyncio.CancelledError` 仍会终止整个文件夹任务。

### CLI Ensemble

```sh
pymss ensemble path/to/model_a_vocals.wav path/to/model_b_vocals.wav \
  --algorithm avg_wave \
  --weights 1 0.8 \
  -o results/ensemble_vocals.wav
```

可用算法包括 `avg_wave`、`median_wave`、`min_wave`、`max_wave`、`avg_fft`、`median_fft`、`min_fft` 和 `max_fft`。输入文件需要使用相同采样率和声道数；如果长度不同，会裁剪到最短输入长度。不传 `--weights` 时，每个输入默认权重都是 `1`。

### Server 和 WebUI

安装可选 server 依赖后，可以启动 HTTP server，支持动态加载模型、浏览 catalog、下载模型文件，以及可选的浏览器 WebUI：

```sh
pip install "pymss[server]"
pymss serve --webui
```

详细用法见 [server CLI 文档](./docs/server/cli.md)、[server API 文档](./docs/server/api.md) 和 [server 错误文档](./docs/server/errors.md)。

### 插件系统

pymss 有插件系统，用于扩展能力——音频 DSP 操作、音频编解码器、以及工作流节点。内置功能（23 个能力：15 个 DSP/声道操作 + 8 个编解码器）和插件用的是同一套注册机制，核心与扩展共用同一个能力池。

**安装插件** —— 使用 pymss 2.2 或更新版本，通过官方目录中的完整插件 id、仓库 URL 或本地路径安装。在 URL 后加 `#子目录` 可从 monorepo 子目录安装：

```sh
pymss install pymss.loudnorm                             # 完整插件 id
pymss install "https://github.com/pymss-project/pymss-plugins#plugins/loudnorm"
pymss install https://github.com/pymss-project/pymss-plugins --subpath plugins/loudnorm
pymss install ./my-local-plugin
```

指定源码版本时使用 `pymss.loudnorm@<tag-or-commit>`，将占位符替换为源码仓库中实际存在的标签或完整提交 SHA。插件的包版本不代表仓库中一定存在同名 Git 标签。

插件的依赖（在其 `pyproject.toml` 里声明）会**自动安装**到当前环境——venv 由 uv 管理时用 uv，否则用 pip。加 `--no-deps` 可跳过。

**管理插件：**

```sh
pymss plugins list           # 已装插件：版本、来源、加载状态
pymss plugins available      # 浏览官方插件目录
pymss plugins search loudness   # 按名称/描述/标签搜索目录
pymss plugins update pymss.loudnorm   # 从记录的来源更新
pymss uninstall pymss.loudnorm
pymss plugins dir            # 打印插件目录
```

`update` 对**官方插件和第三方（URL/路径装的）插件完全一样**——pymss 会记录每次安装的来源。更新和卸载命令使用清单中的插件 id，安装目录也以该 id 命名。

插件放在 `~/.pymss/plugins/`（可用 `PYMSS_PLUGINS_DIR` 覆盖）。单个插件加载失败不会影响其他插件。官方插件目录是一个独立的 `pymss-plugins` 仓库（里面的 `registry.json` 以插件 id 为键）；插件需要 v2 清单，见 `docs/plugin-protocol.md`；可通过 `PYMSS_PLUGINS_REGISTRY` 指向自定义目录。

**编写插件** —— 在 `my-local-plugin/` 中创建 `pyproject.toml` 和 `__init__.py`。v2 清单声明插件身份，以及 `setup(plugin)` 注册的全部 Operation。

`pyproject.toml`：

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

`__init__.py`：

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

清单声明的 Operation id 必须与 `setup()` 实际注册的集合完全一致。能力、节点和 CLI 命令同样在 `setup()` 内，通过 `plugin.register_capability`、`plugin.register_node` 和 `plugin.register_cli` 注册。没有 v2 清单的插件不再加载，详见[插件协议](./docs/plugin-protocol.md)和[开发指南](./docs/plugins.md)。

能力是注册在扁平全局池里的具名函数；节点/CLI/库调用都按名字查找。提供者和消费者可以分别打包，仅通过能力名耦合——例如适配 `SaveAudioOpus` 节点时，可以直接复用已有的 `opus_encode` 能力，而不是重写编码器。

内置能力包括：`to_mono`、`split_channels`、`join_channels`、`adjust_volume`、`invert_phase`、`normalize_peak`、`standardize`/`destandardize`、`trim`、`concat`、`mix`、`empty_audio`、`eq`、`resample`、`ensemble`，以及 `{wav,flac,mp3,m4a,aac,opus,vorbis,ogg}_encode`。

### Python API

直接用 catalog 里的模型名即可，不需要传 `model_type`、`model_path`、`config_path`。

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

`download=True` 会在加载前下载缺失的模型文件；如果只想使用本地已有模型，可以省略它。

`MSSeparator` 也可以作为上下文管理器使用。退出 `with` 代码块时会自动调用 `separator.close()`，尽量释放模型引用并清理后端缓存。

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

### 手动模型路径

自定义权重不在模型 catalog 中时，可以使用完整构造方式。

```python
from pymss import MSSeparator, get_separation_logger
# 初始化
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
        "other": None # None 或缺少此音轨将导致不输出此音轨的文件。 此示例将在 ./output/vocals 中输出人声音轨，并忽略其他（乐器）音轨。 确保键与配置文件匹配。
    },
    save_as_folder=False,
    audio_params={"wav_bit_depth": "FLOAT", "flac_bit_depth": "PCM_24", "mp3_bit_rate": "320k", "m4a_bit_rate": "192k", "m4a_aac_at_quality": 2}, # 可以省略
    logger=get_separation_logger(), # 可以省略
    debug=False, # 可以省略
    inference_params={
        "batch_size": 4,
        "overlap_size": 512,
        "chunk_size": 1024,
        "standardize": True,
        "normalize": False
    } # 可以省略
)
with separator as s:
    s.process_folder('path/to/input_file_or_folder')
```

`model_type="auto"` 通过 pymss-core 根据 YAML 配置识别架构，支持显式架构声明，以及可识别的 BS/Mel RoFormer 与 Conformer、HTDemucs、MDX23C、SCNet、Apollo、Bandit v1/v2 配置。BS PolarFormer 沿用 `bs_roformer`；HyperACE 在加载时进一步根据权重键区分。未知或存在歧义的配置会在加载权重前抛出 `ModelTypeDetectionError`（继承 `RuntimeError`），此时需手动指定 `model_type`。没有 YAML 的 VR 和旧版模型需要显式指定类型。

### 手动构造参数

每个 `MSSeparator` 参数的详细说明见 [MSSeparator 参数文档](./docs/msseparator_cn.md)。

- model_type: 使用 'auto' 根据 YAML 识别架构，或手动指定类型，例如 'htdemucs'。显式类型包括
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
- model_path: 模型文件路径。
- config_path: 配置文件路径。
- device: 设备类型，默认为 'auto'。 必须是以下之一 ['auto', 'cuda', 'npu', 'mps', 'cpu']
- device_ids: 设备 ID 列表，默认为 [0]。
- output_format: 输出音频格式，默认为 'wav'。可选 wav、flac、mp3、m4a、aac、opus、vorbis、ogg。
- use_tta: 是否使用 TTA（测试时增强），默认为 False。 使用 TTA 会使处理时间增加三倍，但质量会略有提高。
- store_dirs: 存储目录，可以是单个文件夹路径或带有乐器键的字典。
- save_as_folder: 为 True 且 store_dirs 指向同一个输出文件夹时，会把每个输入音频的输出音轨保存到以音频名命名的子文件夹中。
- audio_params: 音频参数，包括 wav_bit_depth、flac_bit_depth、mp3_bit_rate、m4a_bit_rate 和 m4a_aac_at_quality。 默认为 {"wav_bit_depth": "FLOAT", "flac_bit_depth": "PCM_24", "mp3_bit_rate": "320k", "m4a_bit_rate": "192k", "m4a_aac_at_quality": 2}。
- logger: Logger 实例。 默认为 pymss.get_separation_logger()
- debug: 是否启用调试模式，默认为 False。
- inference_params: 推理参数，包括 batch_size、overlap_size、chunk_size、standardize、normalize 和 `cuda_attention_backend`。`standardize` 控制模型输入标准化，默认使用模型配置里的 `inference.normalize`，如果配置文件没有该项则为 `False`。`normalize` 控制所有输出音轨联动的峰值归一化。`model_type='vr'` 支持 `batch_size`、`window_size`、`aggression`、`enable_tta`、`enable_post_process`、`post_process_threshold`、`high_end_process` 和输出 `normalize`。

### CUDA Attention 后端

RoFormer 系列模型在已安装 PyTorch 暴露 cuDNN attention 时默认使用 cuDNN attention，在 ROCm/HIP 上默认使用 memory-efficient SDPA 后端（AOTriton），否则使用 PyTorch 默认 SDPA 路径。需要探测式回退时可通过 `inference_params={"cuda_attention_backend": "auto"}` 覆盖。可选值为 `auto`、`default`、`flash`、`cudnn`、`efficient`、`math` 和 `xformers`。`auto` 会优先尝试 cuDNN attention，然后回退到 PyTorch memory-efficient SDPA，再回退到 PyTorch 默认 SDPA。`xformers` 是本地可选安装项，不作为必需依赖。

### Apple Silicon MLX 后端

使用 `device='mlx'` 可以启用 Apple Silicon MLX 后端：

```python
separator = MSSeparator.from_model_name(
    "bs_roformer_voc_hyperacev2",
    download=True,
    device="mlx",
    output_format="wav",
    store_dirs="results",
)
```

在 Apple Silicon 上，`pyproject.toml` 会为该后端安装 `mlx>=0.31.0`。缺少 MLX 或 backend 运行失败时，非 VR 模型会记录 `_pymss_mlx_full_backend_error` 并回退到 Torch MPS。高级用户仍然可以通过 `inference_params` 覆盖 `mps_model_backend` 和 `mps_model_compute_dtype`。

### 模型兼容性

BS PolarFormer checkpoint 使用 `model_type="bs_roformer"`，匹配的 YAML 配置需保留 `model.use_pope: true`。无需单独安装 PoPE 或 Triton 包；这类模型的 MLX 后端请求会回退到 PyTorch。

配置为 `model: htdemucs` 且 `htdemucs.cac: true` 的 HTDemucs checkpoint 通过 `model_type='htdemucs'` 支持。

旧 Demucs/TasNet `.th` 权重可以使用 `model_type='legacy_demucs'` 或 `model_type='legacy_tasnet'`，不需要 MSST YAML 配置。当前无外部依赖 legacy loader 支持 classic Demucs、v3 time-domain Demucs、ConvTasNet、CaC HDemucs、package 形式 HTDemucs、multi-frequency CaC HDemucs 和简单 Demucs bag YAML。DiffQ 量化 checkpoint 和 non-CaC/Wiener HDemucs 仍需要专门的旧模型加载器。

UVR VR 支持已适配的 UVR/VR 系列 `.pth` 权重。和其他模型一样，直接用 catalog 里的模型名走 CLI 或 Python API。输出 stem 名称来自内置 VR 模型列表，例如 `Vocals`、`Instrumental`、`No Echo` 或 `Echo`。

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

### Hugging Face 配置提醒
一些从 Hugging Face 或 MSST-WebUI 下载的模型配置使用 `inference.num_overlap`。当前优化后的 pymss 路径使用 `inference.overlap_size`。若配置里只有 `num_overlap`，pymss 会自动换算：

```text
overlap_size = chunk_size - chunk_size // num_overlap
```

例如 `num_overlap: 2` 对应 50% overlap（与 MSST 一致，但更慢）。想加快推理请显式写更小的 `overlap_size`，或通过 `inference_params` 传入。

推荐快速设置：

```yaml
audio:
  chunk_size: 480000
inference:
  batch_size: 2
  overlap_size: 24000  # chunk_size 的 5%
```

### RTX 5090 实测

测试环境为 NVIDIA GeForce RTX 5090、PyTorch 2.9.1+cu128、CUDA 12.8，关闭 TTA，预热 1 次，正式运行 3 次。

| 模型 | 类型 | RTFx | 1 小时音频 |
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

VR 模型测试参数为 `batch_size=2`、`window_size=512`、`aggression=5`，关闭 TTA 和后处理。

| VR 模型 | RTFx | 1 小时音频 |
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
