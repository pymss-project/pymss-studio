# pymss 插件与操作开发指南

pymss 2.2+ 采用双层扩展模型（见 [plugin-redesign-plan.md](plugin-redesign-plan.md) 的完整背景）：

- **Operation（操作）**：可执行的一项音频处理。声明输入、参数、输出，宿主（CLI / pymss-studio / ComfyUI / 工作流图）从同一份描述生成界面与节点。
- **Provider（服务实现）**：底层服务（当前为重采样）的可替换实现，显式注册与选择。

## 快速开始：写一个操作插件

在 `~/.pymss/plugins/myplugin/`（或 `PYMSS_PLUGINS_DIR` 指定目录）放两个文件。`pyproject.toml` 声明插件身份和它注册的操作（[插件协议 v2](plugin-protocol.md)）：

```toml
[project]
name = "pymss-plugin-myplugin"
version = "0.1.0"

[tool.pymss.plugin]
id = "myplugin"
api_version = 2

[[tool.pymss.plugin.operations]]
id = "myplugin.gain"
title = "Apply Gain"
```

`__init__.py` 中的 `setup()` 注册同一个操作：

```python
def setup(plugin):
    plugin.register_operation(
        id="myplugin.gain",                 # 以插件 id 加点开头
        schema_version=1,
        title="Apply Gain",
        inputs={"audio": {"type": "audio"}},
        params={
            "type": "object",
            "properties": {
                "gain_db": {"type": "number", "default": 0.0,
                             "minimum": -60.0, "maximum": 60.0},
            },
            "additionalProperties": False,
        },
        outputs={"audio": {"type": "audio"}},
        run=run_gain,
    )


def run_gain(ctx, inputs, params):
    audio = inputs["audio"]        # AudioArtifact: [channels, samples] float32 + sample_rate
    gain = 10 ** (params["gain_db"] / 20.0)
    out = audio.audio * gain
    return {"audio": type(audio)(out, audio.sample_rate, channel_layout=audio.channel_layout)}
```

`setup()` 失败，或注册的操作与清单声明不一致时，该插件的所有注册会被回滚，错误进入加载报告。用 `pymss plugins validate <目录>` 可以在不执行代码的情况下检查清单。

## 参数 schema

当前支持 `number` / `integer` / `string` / `boolean` 与 `default` / `minimum` / `maximum` / `enum` / `required` / `additionalProperties`。默认值填充与校验由 runtime 统一执行；宿主只负责展示与传值。

## 使用入口

| 入口 | 命令 / 位置 |
|---|---|
| CLI | `pymss ops list` / `pymss ops describe <id>` / `pymss ops run <id> --input audio=a.wav --param gain_db=-3` |
| 工作流图 | 节点类型 `op.<operation id>`，参数写在 `node.data["params"]`（命名参数，不是 widgets 位置） |
| pymss-studio | 工具页 "插件操作"：自动生成表单，后台任务执行 |
| ComfyUI | 节点 `pymss_op_<id 下划线>`：音频输入端口 + 参数 widget 自动生成 |

## 内置操作

pymss 自带 `pymss.audio.adjust_volume` / `invert_phase` / `normalize_peak` / `trim`，与外部插件走完全相同的注册与执行路径（source 标记为 `pymss.builtin`）。

## Python API

```python
from pymss.operations import create_runtime, default_runtime

runtime = create_runtime(load_plugins=True)   # 隔离实例
runtime = default_runtime()                   # 进程默认（含内置操作）
result = runtime.run("myplugin.gain",
                     inputs={"audio": artifact},
                     params={"gain_db": -3.0})
```

## Provider：替换重采样实现

```python
from pymss.audio_services import default_audio_service

class MyResampler:
    name = "my-resampler"
    def resample(self, audio, orig_sr, target_sr):
        ...

svc = default_audio_service()
svc.register_resampler(MyResampler())
svc.select_resampler("my-resampler")     # 显式选择；selected_resampler() 可查询
svc.resample(audio, 44100, 48000)        # 所有通用路径（图、保存转换、ensemble 对齐）随之改变
```

同名冲突：`register_resampler` 默认拒绝重复注册（传 `replace=True` 覆盖）；能力（capability）注册表同样不再允许内置编码器覆盖插件实现。

## capability / node / CLI

插件在 `setup(plugin)` 中通过 `plugin.register_capability` / `plugin.register_node` / `plugin.register_cli` 注册到全局注册表；`register_cli` 的命令会挂载到 `pymss` CLI（与内置命令同名则跳过并告警）。没有 v2 清单的插件不再加载，迁移方式见 [plugin-protocol.md](plugin-protocol.md#migrating-from-v1)。
