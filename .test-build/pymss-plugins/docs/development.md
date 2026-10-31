# Studio 工具插件开发约定

本文件说明当前可用的接口与发布方式。可运行的起点是 [音频增益插件](../examples/audio_gain/README.zh-CN.md)，它可以复制到独立仓库构建，不依赖官方仓库内部的前端或语言文件。

## 职责与结构

| 内容 | 所有者 |
| --- | --- |
| 处理算法、参数、Operation、结果 | 插件 |
| 页面组件、表单、说明、页面内翻译 | 插件 |
| 市场名称、描述、分类及操作名称 | 插件声明，目录生成器提取 |
| 任务、文件对话框、主题、语言通知、设置存储 | Studio SDK |
| 插件注册、参数描述和执行 | pymss Operation API |

一个插件目录应包含 `pyproject.toml`（pymss 插件协议 v2 清单）、入口、`backend/`、`ui/src/`、`ui/dist/`、许可证和使用说明。UI 可用 Vue 或其他技术实现，运行时只要求提供可加载的 HTML 页面和约定的 SDK 通信。

只提供 Operation 的插件可供脚本或工作流调用；工具页入口还需要 `placements = ["tool"]` 的视图。插件不应通过导入 Studio 源码或其他插件目录来完成处理。

## ID 与兼容性

- 插件 ID 使用作者命名空间，例如 `alice.audio_gain`。以点分隔非空命名段，每段以小写 ASCII 字母或数字开头，其余字符允许小写字母、数字、下划线和连字符；Studio 中总长度不超过 100 个字符。它是插件唯一的身份：安装目录名、registry 键、Studio 工具入口和设置都以它为准。
- Operation ID 以插件 ID 加点开头，例如 `alice.audio_gain.apply`，并在 `[[tool.pymss.plugin.operations]]` 中逐一声明。`setup()` 注册的集合必须与声明完全一致，否则 pymss 拒绝激活该插件。
- 插件 ID 与视图 ID 发布后保持稳定；源码或界面更新时提升插件版本。
- `api_version = 2` 与 `requires_pymss` 描述 Python 插件协议兼容性；Studio 页面另行声明 `sdk_version` 和 `required_capabilities`。两个协议版本分别维护。

清单分两层：

| 表 | 读取方 | 内容 |
| --- | --- | --- |
| `[tool.pymss.plugin]` | pymss（CLI、工作流、Studio Worker） | `id`、`api_version`、`requires_pymss`、`entry`、`dependencies`、`operations`、`contributions.graphs/nodes` |
| `[tool.pymss.studio]` | Studio 与本仓库构建脚本 | SDK 与运行环境要求、`category`、`icon`、`tags`、`locales`、`views`、`runtime_packages`、`deferred_dependency_operations`、`build_script` |

后端公共代码使用 `pymss.plugins.kit`，不要在插件里复制协议或文件工具代码：`register_tool` 注册文件工具 Operation，`emit` 上报结构化进度（宿主通过 `report_progress(..., event=...)` 接收），`require_file`、`render_output`、`run_ffmpeg` 等处理路径与外部进程。capability、workflow node 在 `setup(plugin)` 中通过 `plugin.register_*` 注册，不在 import 时产生副作用。

工作流节点必须在 `contributions.nodes` 中完整声明，并通过 `plugin.register_node()` 注册相同集合；禁止覆盖其他插件的节点或 capability。有等价 Operation 的节点通过 `plugin.run_operation()` 复用输入、参数和输出校验，保持既有 `widgets_values` 顺序。清单中的数值边界用于执行前校验和 Studio 控件，`step` 表示控件步长。

`AudioArtifact.source_path` 只记录来源。识别等文件后端须用 `with materialize_audio(audio) as path:` 读取处理后的缓冲，不能直接重读来源文件。持久产物写入 `ctx.output_dir`，并通过 `NodeResult.saved_paths/saved_records` 登记，不能只返回一个 STRING 路径。临时音频由工具包和 Worker 宿主共同管理；MIDI 等最终产物与临时输入分开存放。

复用现有 SDK 的插件不需要修改 Studio 菜单或增加专属 Worker 命令。新的原生能力需要宿主先提供接口，声明一个新的权限名称不会自动实现该接口。

## Studio 与运行环境要求

要求在插件级声明，适用于该包的所有 Studio 视图：

```toml
[tool.pymss.studio]
sdk_version = 1
required_capabilities = [
    "app.info", "settings.read", "settings.write",
    "files.pickAudio", "files.pickDirectory", "files.reveal",
    "operations.start", "operations.cancel", "ui.overlay",
]
```

有 Studio 视图时，`sdk_version` 必填，是必须精确匹配的 SDK 主版本；当前为 `1`。没有视图的插件可以省略它，目录中的 `sdkVersion` 为 `null`。`required_capabilities` 列出页面实际使用的完整服务名，宿主缺少其中任意一项时拒绝使用该插件。它与视图 `permissions` 分开：前者确认宿主具备能力，后者授权视图调用能力；`operations` 这样的权限族名称不能用作必需能力名。

`host.ready` 通知与 `app.info` 都提供 `sdkVersion` 和 `capabilities`。页面应在读取设置或开始处理前核对这些值。官方构建器和独立模板从同一份 `pyproject.toml` 注入页面的要求，不需要在 TypeScript 中再维护一份列表。

只有确实受限时才增加以下字段，省略或空数组均表示不限制：

| 字段 | 可用值 | 含义 |
| --- | --- | --- |
| `platforms` | `win32`、`linux`、`darwin` | 允许的操作系统 |
| `architectures` | `x86_64`、`arm64` | 允许的 CPU 架构 |
| `backends` | `torch`、`mlx` | 当前运行环境至少具备其中一个基础计算框架 |

例如 GAME 声明 `backends = ["torch"]`；ASR 声明 `backends = ["torch", "mlx"]`。这表示框架存在，不表示 CUDA、MPS 等设备可用，也不表示模型或 FunASR 等可选组件已经安装。平台字段不替代 Python 依赖的环境标记；插件仍应在执行时检查设备、组件和模型的实际状态。

Studio 在安装和打开页面前检查要求。官方目录生成器把它们写入每个条目的 `requirements`，字段名为 `sdkVersion`、`requiredCapabilities`、`platforms`、`architectures` 和 `backends`，使市场也能展示不兼容原因。

使用 Studio 管理的可选组件时，在 `[tool.pymss.studio]` 声明包级 `runtime_packages`。例如 ASR 使用 `runtime_packages = ["funasr"]`，使同一插件的所有 Operation 都能读取 Studio 已安装的 ASR 组件；宿主在 Windows、Linux 和 Intel macOS 上选择 FunASR，在 Apple Silicon 上选择 `funasr-mlx`。这里只能填写宿主支持的组件标识，不是任意 Python 包安装接口。

宿主先校验清单并解析插件依赖，只有通过解析的插件才会在实际加载时激活组件目录。导入或注册失败时，撤销本次新增的组件路径，并按完整包树清理本次导入的组件模块及其新增子模块，避免父子模块缓存失配；激活前已有的路径与模块保留。

`deferred_dependency_operations` 单独控制哪些 Operation 可以延迟依赖校验，适用于操作自身负责检查组件并返回安装引导的场景。它不决定加载哪些可选组件。ASR 只为 `pymss.asr.tool` 延迟校验；`pymss.asr.transcribe` 仍校验所需依赖，但两者共享包级声明的已安装组件。

## 插件语言文件

市场名称、描述、标签和操作名称继续放在各插件的 `[tool.pymss.studio.locales]` 中。生成的 `registry.json` 用于安装前展示，已安装插件的声明用于工具导航和卡片展示。

所有官方插件和独立模板的页面文案统一放在各自的 `ui/src/locales/`，与组件一起编译：

```text
ui/src/locales/
├── en.json
└── zh-CN.json
```

`en.json` 是必需的默认词典。官方构建器和独立模板自动收录这个目录中的 JSON 文件；新增语言时使用 `ja.json`、`fr.json` 等语言代码命名即可，无需编辑全局词典。语言匹配顺序为完整代码、基础语言、同语言变体、英文。缺少单条翻译时也回退到英文。

Studio 只发送当前语言，插件自行选择词典。语言切换不应重置输入或重启处理任务。Naive UI 自带控件的语言由页面的 ConfigProvider 单独配置，当前提供中文和英文；新增语言时按需补充对应的组件库 locale。

共享界面代码不再维护插件词典。按钮、状态、进度和日志组件需要的文案也放入各插件的语言文件，因此插件可以自行调整措辞，构建页面只包含本插件的词典。

使用 `OperationTool` 的插件通过 `tool.json` 中的 `titleKey`、`descriptionKey` 和操作 `titleKey` 引用词典，参数名称放在 `parameters.<参数名>`。不要把双语对象或语言判断写进工具配置与公共组件。

## 构建约定

官方仓库原有 Vue 工具使用 `component` 声明，由公共构建器打包。需要独立前端入口的包可声明：

```toml
[tool.pymss.studio]
build_script = "scripts/build.mjs"
```

该字段只用于仓库构建。构建脚本必须位于插件目录内，接受 `--check`，生成视图声明中的 HTML 和第三方许可说明。批量构建直接用 Node 调用文件，不解析 shell 命令字符串。

独立包可以自带 `package.json`、锁文件、TypeScript 配置和构建脚本。官方构建器和当前模板使用 Python 3.11+ 的 `tomllib` 读取构建清单；插件运行时支持范围仍由 `requires-python` 决定。提交或发布时必须带上已编译页面；用户安装时 Studio 不运行 Node 构建器。后端源码直接维护和运行，不需要复制生成。

参考插件使用运行时提供的 `pymss-sdk.js`，不复制一份 RPC 协议实现。路径按 Studio 的插件协议 URL 解析，而不是按源码目录深度解析。普通浏览器直接打开页面时没有宿主服务，应显示明确的连接状态。

## SDK 使用与生命周期

### 操作描述快照

官方插件随包提供根目录 `operations.json`。它由实际注册结果生成，供 Studio 构建表单时读取，查询命中不导入插件或推理框架。快照格式版本与插件 API 版本分开；当前格式为 `1`，描述现有 API v2，不改变 `setup()` 或旧工作流的执行方式。

修改后端代码、清单或影响注册的资源后，在安装了 `pymss>=2.2` 的开发环境中执行：

```bash
python -X utf8 tooling/scripts/generate_operation_metadata.py
python -X utf8 tooling/scripts/generate_operation_metadata.py --check
```

可以用 `--plugin analysis` 或 `--plugin pymss.analysis` 限定导出对象，`--plugins-dir` 指定插件集合。导出是可信构建步骤，只执行注册、不执行处理操作；用户浏览页面时不运行导出器。正式目录生成还会检查快照身份、声明集合和源摘要。

快照绑定后端与包资源摘要，忽略页面、安装 sidecar 和生成文件；文本行尾统一处理。不要手工修改描述。缺少或失效的 v2 快照仍可回退到实际注册，并在当前运行环境建立缓存。平台/可选依赖造成的真实接口差异以实际注册为准，不将构建机器结果作为跨平台保证。

`operations.describe` 在描述中附加 `specDigest`。使用已取得的描述启动操作时，可以传入 `expectedSpecDigest`；不一致会返回 `OPERATION_SPEC_CHANGED`，本次处理不会执行。共享 `OperationTool` 自动清除旧描述和参数缓存、强制刷新定义，并要求用户核对后重试，输入路径保留，不自动重新处理。自定义页面可通过 SDK 的 `startOperation` options 传入该字段，使用 `describeOperation(id, { refresh: true })` 获取实际定义。旧页面不传摘要时保持原参数校验路径。

只声明实际使用的接口权限。通过 SDK 获取文件、输出目录、设置和任务结果，不直接调用 Tauri。

桥接参数使用普通 JSON 数据，不发送 Vue 代理、函数或音频缓冲对象。文件输入通过路径描述符传入，音频 Artifact 在 Worker 内解析。

- 初次连接和普通 RPC 应有超时和失败状态；用户文件对话框、长处理任务不应用普通的短超时。
- 任务进度只使用真实回调，拒绝非有限值并避免倒退。
- 取消请求的返回值是确认结果；仍需等待原始任务的成功、失败或取消终态。
- 处理结果与日志分开，日志使用弹窗；界面文案可翻译，未知的底层错误保留原文。
- 设置按插件与视图保存，写入应去重或防抖，保存失败应可见。
- 跟随宿主主题、字体和语言，并报告自然页面高度。弹窗需要 `ui.overlay`，关闭时归还覆盖层。
- 下拉菜单等浮层仍受 iframe 边界裁切，不能靠提高 `z-index` 解决。页面测量需在浮层越界时临时扩展高度，关闭后恢复；官方共享前端已处理这一边界。
- Python 插件执行本地代码；iframe 权限控制的是前端桥接，不构成 Python 沙箱。

SDK 中的运行时组件和模型管理目前有具体支持范围，例如 FunASR；新插件不能假定它们是任意 Python 包或模型的通用管理接口。

## 发布途径

### 独立仓库（默认方式）

一个插件对应一个 Git 仓库。将模板内容放在仓库根目录，使 `pyproject.toml`、`__init__.py`、`backend/` 和 `ui/` 直接位于根目录下；不需要 Fork 整个官方源码集合，也不需要维护自己的 `registry.json`。

发布源码、编译页面和许可文件后，用户在 Studio 的“插件 → 从来源安装”中输入仓库链接即可安装。公开的 HTTPS GitHub 仓库通过源码归档下载，不需要用户安装 Git。默认获取默认分支的最新提交；追加 `@<tag>` 或指定源码 ref 时使用对应版本。其他 Git 来源仍需要本机 Git；本机开发也可以直接选择插件目录。

安装记录保留原始来源、选定 ref 和实际下载的 commit。更新继续追踪安装来源，默认分支更新可据实际 commit 判断，不只依赖作者是否提升版本号。记录 commit 用于来源核对和问题定位，不会把默认更新策略锁定在旧版本。

当前没有多源市场和外部仓库自动收录功能。独立发布不会自动出现在官方目录；外部索引清单和通用模型管理是后续扩展项。

### 贡献到官方源码集合

希望由官方仓库共同维护时，Fork 本仓库，把完整插件包放入 `plugins/<唯一名称>/` 后提交 PR。通过维护者审核后发布。`registry.json` 以插件 ID 为键、从插件声明生成，不能手工加入条目后依赖它长期保留。正式目录应指向已经发布的固定源码提交，Studio 会验证实际提交和插件版本。

### 本机开发

独立插件可直接选择本地目录安装。修改后先构建，再更新安装副本；本地源码不会直接替换正在运行的界面。只有需要调试整个插件目录来源时，才使用诊断页的来源覆盖。

安装与内容比较跳过 `node_modules`、虚拟环境、开发缓存、`.env` 等本机文件。用户模型、密钥和处理结果应存放在插件源码之外。

## 最低验收

参考插件的测试覆盖增益计算、输入不变、短多声道布局、峰值保护、非有限值、溢出、进度、取消和复制包独立加载。UI 还应验收连接失败、表单验证、主题与语言切换、结果、日志、设置失败和取消失败重试。

发布前应在临时运行环境执行安装、运行、更新、禁用和卸载，确认旧版在失败时仍可使用。不要用开发机已经存在的模块路径掩盖缺失依赖。


## Rust / 原生命令开发入口（尚未发布）

`examples/rust_text` 验证了用 Rust CLI 接入同一个 Operation、工作流和 Studio 页面。核心算法使用 Rust 标准库；包内 `setup()` 通过公共 `register_commands` 读取声明，不自行编写子进程胶水。详细构建与执行约定见 [示例说明](../examples/rust_text/README.md)。

此能力依赖尚未发布的 pymss command adapter 和支持 `files.pickFile` 的 Studio。PyPI pymss 2.2.0 不包含它，不能仅把示例加入正式目录后要求现有用户安装。当前 `format_version = 1` 是独立命令描述格式，现有 `api_version = 2` 和 SDK 1 保持兼容。

普通用户安装应使用已经构建的对应平台程序包。示例构建脚本只供开发者显式执行，安装器不会自动安装 Rust、运行 Cargo 或编译源码。目标映射不代表六个平台都已有发布物；目前只有 Windows x86_64 的本地调用链证据，其他目标仍待实际构建、系统依赖、签名和运行验证。

command 适配只报告宿主启动/发布阶段，原始 stdout/stderr 作为有界诊断，不当作 Worker 回调协议。需要细粒度进度、反向宿主服务、交互或常驻状态时，应等待 rpc-process 双向协议，不能把命令输出等同于通用 callback。


## 任务内双向 RPC 开发入口（尚未发布）

`examples/rust_rpc_text` 在独立进程里主动请求 `host.taskInfo` 与 `host.fileInfo`，再处理输入文件并发出细粒度进度。示例保留 v2 setup，以公共 `register_rpc_processes` 注册，说明见 [Rust RPC 模板](../examples/rust_rpc_text/README.md)。

这是任务内 callback：请求有独立 ID、宿主固定的插件/操作/任务身份、声明权限、输入名授权、截止时间和取消语义。宿主没有提供服务时返回 `HOST_UNAVAILABLE`，不会默认同意或打开交互窗口。协议使用有界 NDJSON/JSON-RPC；普通 CLI 日志不能当协议回调。

新能力尚未随 pymss 发布，PyPI 2.2.0 不包含它。现阶段不开放 GUI 请求、任意操作递归调用、全局事件或常驻 session；未实现新的 Studio/Worker 双向交互桥。正常用户应安装对应平台的预编译程序，不能把源码 checkout 的 Cargo 构建当安装步骤。


## 独立 Python 进程开发入口（尚未发布）

`examples/python_rpc_text` 使用与 Rust RPC 相同的任务协议，在独立 Python profile 中执行；详见 [Python RPC 模板](../examples/python_rpc_text/README.md)。每个操作使用 `python = "peer.py"` 替代 native targets，包级 `[tool.pymss.python]` 指向含平台和 SHA-256 的完整本地 wheel 锁。目标进程不需要导入 pymss，不借用宿主或其他插件的依赖。

Studio 通过现有安装事务准备独立环境；CLI 可显式执行 `pymss ops prepare-python`。profile 保持稳定路径并保存本次 peer 代码/资源，移动插件代码目录不会搬移 venv。安装、更新、准备失败均不修改旧可用环境；执行租约避免正在运行的环境被立即清理。旧 v2 handler 不会因为有插件目录就自动获得隔离，需明确使用进程入口。

当前只支持随包提供的本地 wheel 闭包，不在用户安装时联网解析、编译依赖或选择另一版本的 Python。PyPI pymss 2.2.0 不包含此能力；发布前须设置实际提供该能力的最低内核版本。Windows 标准 Python 3.12 已有本地验证，嵌入式安装包、macOS/Linux 和 ARM64 仍需独立验收。该模板没有进入官方目录或预装。
