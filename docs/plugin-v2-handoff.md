# 插件协议 v2 交接文档

> 日期：2026-10-28。面向接手的开发者或 AI。
> 涉及三个仓库：`D:\项目\pymss`、`D:\项目\pymss-plugins`、`D:\项目\pymss-studio`。
> **所有改动都还没有提交。**

---

## 1. 背景与已定决策

Studio 支持用户自己写插件；同一个插件既能在 pymss（CLI、工作流）里运行，也能在 Studio 里运行，Studio 额外提供图形界面。原实现的问题：

- 同一个插件有三套身份：`plugin_id`、目录名、`tool_id`。
- pymss 核心协议里混入了 Studio 专属字段（`views`、`player`）。
- 列出插件时必须 import 插件代码，所以 Studio 需要 45 秒超时的 probe 子进程和一层 inventory 缓存。
- Studio 靠临时改写 `PYMSS_PLUGINS_DIR` / `PYMSS_PLUGINS_REGISTRY` 环境变量来复用 pymss 的加载器。
- 官方插件里的样板代码被复制了 7 份；进度信息是把 JSON 塞进字符串（`pymssToolEvent`）传递的。

用户确认过的决策：

| 决策 | 内容 |
|---|---|
| 范围 | 只改 `pymss`、`pymss-plugins`、`pymss-studio`。**`pymss-core`（模型、配置、checkpoint）不改。** |
| 兼容性 | **直接破坏性升级到 `api_version = 2`**，不兼容 v1，也不加载没有清单的插件。 |
| 阶段 | 只做了 P0。P1/P2 见第 8 节。 |
| 版本锁 | **不加版本上限。** 用户的说法是"未来更新一般不会对老版本破坏性更新"。见第 6 节。 |
| 发布 | **暂不发布 pymss 2.2.0**，先在本地测试。 |

最初的设计评审结论（问题清单和目标架构）在对话中给出，核心思想是**一个包，两层契约**：

- `[tool.pymss.plugin]`：pymss 读取，Studio 也读取。
- `[tool.pymss.studio]`：只有 Studio 读取，pymss 完全忽略。

---

## 2. v2 清单规格

规范文档：`D:\项目\pymss\docs\plugin-protocol.md`，末尾有"Migrating from v1"一节。

```toml
[project]
name = "pymss-plugin-loudnorm"
version = "0.4.0"
dependencies = ["pyloudnorm"]

[tool.pymss.plugin]                 # pymss 读取
id = "pymss.loudnorm"               # 小写 [a-z0-9][a-z0-9._-]*；同时是安装目录名和 registry 的 key
api_version = 2                     # 必须是 2
requires_pymss = ">=2.2"            # 只是下限
# entry = "__init__:setup"          # 可省略，默认就是这个

[[tool.pymss.plugin.operations]]    # setup() 注册的集合必须与这里声明的完全一致，否则回滚
id = "pymss.loudnorm.normalize"     # 必须以 "<插件 id>." 开头
title = "Normalize Loudness (LUFS)"

# 可选：[[tool.pymss.plugin.contributions.graphs]] / [[...contributions.nodes]]（通用能力，保留在核心表）

[tool.pymss.studio]                 # 只有 Studio（以及插件仓库的构建脚本）读取
category = "edit"                   # convert | analyze | recognize | edit
icon = "VolumeHighOutline"
deferred_dependency_operations = ["pymss.loudnorm.tool"]   # 这些 operation 的后端作为可选运行时包单独管理
tags = [...]
build_script = "scripts/build.mjs"  # 仅插件仓库构建时使用
[tool.pymss.studio.locales."zh-CN"] # title / description / tags / operations{id: 名称}

[[tool.pymss.studio.views]]
id = "tool"
title = "Loudness"
entry = "ui/dist/index.html"        # 必须是插件目录内已存在的文件
component = "ui/src/LoudnessTool.vue"  # 仅插件仓库构建时使用
placements = ["tool"]               # tool | workspace
operation = "pymss.loudnorm.tool"
permissions = ["operations", "files", "settings", "ui.overlay"]
file_tool = "loudness"              # 仅共享前端构建时使用，见第 7 节
```

v2 删除的字段：`[project.entry-points."pymss.plugins"]`、`tool_id`、`[[tool.pymss.plugin.contributions.views]]`、`[[...contributions.player]]`。

---

## 3. 各仓库改动

### 3.1 pymss（分支 `feat/plugin-manifest-v2`，从干净的 main 新建）

| 文件 | 改动 |
|---|---|
| `pymss/plugins/manifest.py` | `API_VERSION = 2`，其他版本抛出 `ManifestError`；删除 `ViewContribution` 和 `PlayerContribution`；新增 `OperationDecl` 和 `operation_ids`；没有清单时抛错，不再返回 None；`parse_entry_point` 默认为 `__init__:setup` |
| `pymss/plugins/lifecycle.py` | `PluginManager(roots)` 支持多个根目录；删除 legacy 分支；`setup()` 执行后核对 `handle.registered_operations` 与声明是否一致，不一致就回滚并标记为 failed；报告行带上 `path` |
| `pymss/operations/__init__.py` | `Runtime.load_plugins(roots=None)`，不传时用 `get_plugins_dir()`；删除 legacy 扫描；`OperationContext.report_progress(fraction, message="", *, event=None)`，**`progress` 回调签名改为 `(fraction, message, event)`**；`_RuntimePluginHandle.registered_operations` |
| `pymss/plugins/loader.py` | 删除 entry-points 扫描；`bootstrap(force, roots)` |
| `pymss/plugins/install.py` | 先 staging，校验清单，再发布到 `<plugins_dir>/<plugin_id>`；所有接口支持显式 `registry_url`，环境变量仅作后备；registry 条目必须安装出同名的插件 id |
| `pymss/plugins/kit.py`（新增） | `register_tool`、`emit`/`operation_context`、`isolate_stdout`、`ToolError`、`require_file`/`require_directory`/`available_path`、`render_output`/`publish_export_path`（沿用 Studio 的临时文件令牌环境变量约定）、`ffmpeg_path`/`ffprobe_path`/`run_ffmpeg`/`run_process`/`subprocess_detail` |
| `pymss/operations/kit_builtins.py`、`graph_bridge.py` | 适配三参数的进度回调 |
| `pymss/cli.py` | 新增 `pymss plugins validate <dir>`，只读清单，不执行代码 |
| `pyproject.toml` | `version = "2.2.0"`（见第 6 节的疑问） |
| 文档 | `docs/plugin-protocol.md` 重写为 v2；更新 `docs/plugins.md` 和 README |
| 测试 | 改写 `test_plugin_protocol*.py`、`test_plugins.py`、`test_operations.py`、`test_legacy_loudnorm_node.py`；新增 `test_plugin_kit.py`；CI 列表已加入 |

### 3.2 pymss-plugins（分支 `feat/studio-tool-views`，在用户大量未提交的工作之上修改）

- 13 个 `plugins/*/pyproject.toml` 和 `examples/audio_gain/pyproject.toml` 已改为 v2，版本号各升一个 minor。`tempo_key` 没有声明 operation，它只有页面，使用 Studio 原生的 `audio.analyze`。
- **已删除** 7 个插件里的 `backend/{protocol,entry,common}.py`，共 21 个文件，改用 `pymss.plugins.kit`。
- import 时的 `register_capability`/`register_node` 副作用已移进 `setup()`，涉及 analysis、dynamics、spectral、timing、loudnorm、asr、game。
- 修复 `convert/backend/operations.py` 的嵌套进度回调：签名改变后 `offset` 会被 `event` 占位。
- `frontend/src/sdk.ts`：`__TOOL_ID__` 改名为 `__FILE_TOOL__`，只读取 `update.event`，删除解析 `pymssToolEvent` 的分支。convert 的参数映射**暂时保留**，等 P1 处理。
- `scripts/view_components.py` 和 `build_tool_views.mjs` 改为读取 `[[tool.pymss.studio.views]]` 和 `file_tool`。
- `scripts/generate_registry.py`：key 改为插件 id，条目增加 `id`、`api_version`、`operations`，并校验 `api_version == 2`。`registry.json` 已重新生成。
- 所有 `ui/dist` 已用 `pnpm build` 重新构建。
- 测试已更新：`test_tool_plugins`、`test_convert`、`test_view_components`、`test_tool_output_safety`、示例插件的测试、manual smoke。
- 已更新 `README.md` 和 `PLUGIN-DEVELOPMENT.md`。

### 3.3 pymss-studio（分支 `feat/preinstalled-tool-plugins`，在用户未提交的工作之上修改）

**Python worker**

`python/worker_plugins.py`：

- `_NAME` 改为插件 id 规则：小写，允许点，不允许以点结尾，不允许 `..`。
- `_read_manifest()` 用 tomllib 读取 v2 字段，**不导入 pymss**。
- `_studio_views()` 校验页面声明，entry 必须位于插件目录内。
- `_inventory(root, verify=False)` 只读清单；传 `verify=True`（对应前端的 `forceVerify`）时才运行 `_probe`。
- `_directory_plugins()` 把 `~/.pymss/plugins`（或 `PYMSS_PLUGINS_DIR`）里的插件作为只读行列出。
- `_migrate_v1()` 在 `_recover` 中把非 v2 的安装，或目录名与插件 id 不一致的安装，移到 `<root>/legacy-v1/`；同名归档保留，新增归档追加唯一数字后缀。inventory 中带 `legacyPlugins` 字段。
- `_plugin_registry()` 只设置 ContextVar（`_LOCAL_SOURCE`、`_REGISTRY_URL`），调用方显式传 `registry_url`。
- update/repair 时，如果来源提供的是另一个插件 id，直接报错，不再重命名。
- `operation_runtime()` 加了 `_RUNTIME_LOCK`。批量工作流会在多线程里并发调用它，而 stdout 重定向是进程级全局状态，不加锁会串线，导致后续协议输出丢失。
- `_validate_requirements` 能识别插件自己的 `.studio-deps` 依赖目录。
- 新增 `operation_uses_optional_backend()`。
- 已删除：`_marketplace_inventory`、`_inventory_stamp`、`_cache_inventory`、`_manual_plugin_fallback`、`_plugin_dir`。

其他 Python 文件：

- `worker_operations.py`：`operation_progress` 带上可选的 `event`；改用 `kit.render_output`。
- `worker_nodes.py`：直接用 pymss 的 `read_manifest` 读取节点声明。
- `worker.py`：删除 `audio_tools` 路由；`run_operation` 只在 operation 属于 `deferred_dependency_operations` 时才激活可选运行时包（例如 ASR）。
- `worker_plugin_config.py`：预装列表改为插件 id 规则。
- **已删除** `worker_tools.py` 和 `worker_tool_output.py`。

**Rust**

- `src-tauri/src/plugin_views.rs`：读取 `tool.pymss.studio.views`；新增一个测试，确认 v1 位置的声明会被忽略。
- 删除 `start_audio_tool` 命令（`app_cmd.rs`、`main.rs`），以及 `worker.rs` 里 audio_tools 的输出作用域分支。`tempo_key.rs` 和诊断模块里仍有 `"audio_tools"` 字样，那只是日志标签，保留。
- `storage.rs` 和 `app_cmd.rs`：删除 `legacy_tool_id` 以及按工具名硬编码的旧设置迁移。
- `python/audio_output.rs` 的测试改为调用 `pymss.plugins.kit.render_output`。原来引用的 `audio_tools.common` 在用户的 WIP 里已经被删掉，这个测试本来就是坏的。

**前端**

- `src/features/plugin-marketplace/{composable,toolEntries,toolState}.ts`：删除 `toolId` 和 `toolTitleKey`。
- `PluginViewHost.vue`：删除 `legacyToolId`。
- `src/views/ToolsView.vue`：删除 `?tool=` 路由和旧的 `activeTool` 恢复逻辑。`restoreStoredTool` 返回布尔值，没恢复成功时退回默认工具：优先 category 为 `convert` 的工具，否则取第一个。
- `src/views/PluginsView.vue`：标题和描述只来自插件自己的 locales；新增 legacy 插件提示。
- `src/i18n/{zh-CN,en}.json`：删除 18 个 `tools.<id>Title/Description`，新增 `plugins.legacyPluginsMoved`。

**脚本与测试**

- `scripts/prepare-preinstalled-plugins.py`：要求 `api_version == 2`，registry 的 key 必须等于插件 id，从 studio views 校验页面，manifest 中不再写 `toolId`/`pluginId`/`titleKey`。
- 测试：改写了全部 `tests/python/test_plugin_*`、`test_preinstalled_plugins.py`、`test_worker_plugins.py`、`test_worker_operations.py`、`test_worker_tools.py`（现在测 kit 文件工具经过 run_operation 的链路）、`test_prepare_preinstalled_plugins.py`，以及前端的 `pluginToolEntries`、`pluginToolSelection`、`i18nKeys`。
- 新增 `tests/manual/plugin_v2_worker_smoke.py`：worker 端到端冒烟，共 24 项检查。

**2026-10-29：并发安装与卡片状态**

- 不同插件最多 3 个安装任务并行，额外任务排队；更新、卸载、运行时切换和插件执行仍与安装互斥。
- 获取源码与依赖在独立事务中并行；每个事务持有自己的 `.lease`，恢复清理跳过活跃事务。注册验证、同 ID 与依赖冲突复核、发布在同一根目录锁内执行。
- 移除市场顶部任务横条，安装进度在各卡片按钮原地显示；右上角日志弹窗按任务查看与取消。前端按任务关联 ACK/事件，合并并发安装结果，关闭窗口时等待所有任务终态。

---

## 4. 如何验证

### 4.1 本机环境的坑

- 系统 Python 3.12 里装的是**旧版** pymss（元数据显示 2.1.8 或 2.0.15）和旧版 pymss-core。
- pymss 仓库根目录有一个过期的 `pymss.egg-info`（Version 2.1.8），会影响 `importlib.metadata`。
- Studio 启动 worker 时会**清掉外部 `PYTHONPATH`**（`worker.rs` 里的 `HOST_PYTHON_ENV_VARS`），再按选中的运行时设置覆盖层。GUI 本地开发需要同时核对 bootstrap 和 active runtime，不能依靠外部 `PYTHONPATH` 切换源码。
- `PYMSS_STUDIO_PYTHON` 指定的是 bootstrap 解释器；已选中的 active runtime 对插件安装和执行仍有优先权，不能只改环境变量就认为 GUI 已切换。2026-10-29 本机通过独立的 `.tmp/plugin-v2-runtime/cuda/Scripts/python.exe` 开发环境解决旧覆盖层仍加载 pymss 2.1.12 的问题，原 active pointer 已备份；CUDA、插件安装和实际 WAV RMS 分析均已验证。需要重新配置时，通过诊断页切换开发运行时。
- 已建好隔离环境 `D:\项目\pymss-studio\.tmp\pymss-v2-venv`（`--system-site-packages`，复用系统的 torch 2.7.1+cu128），并用 **compat 模式**可编辑安装了本地的 pymss 2.2.0 和 pymss-core。不用 compat 模式的话，系统里的旧 pymss 会盖过本地源码，造成新旧代码混用。重建方法：
  ```bash
  python -m venv --system-site-packages .tmp/pymss-v2-venv
  .tmp/pymss-v2-venv/Scripts/python.exe -m pip install --no-deps --config-settings editable_mode=compat -e D:/项目/pymss-core -e D:/项目/pymss
  ```
- 跑单元测试时用 PYTHONPATH 把本地源码放到最前面。第一项是一个伪造的 `pymss-2.2.0.dist-info`，用来让版本约束检查通过：
  ```bash
  # 伪造元数据（已存在：C:/Users/19090/AppData/Local/Temp/pymss-v2-meta/pymss-2.2.0.dist-info/METADATA）
  export PYTHONPATH="C:/Users/19090/AppData/Local/Temp/pymss-v2-meta;D:/项目/pymss;D:/项目/pymss-core"
  ```
  这个临时目录可能被系统清理掉。重建只需要写一个 METADATA 文件，内容为 `Metadata-Version: 2.1\nName: pymss\nVersion: 2.2.0\n`。

### 4.2 命令和最后一次结果（2026-10-28）

| 范围 | 命令 | 结果 |
|---|---|---|
| pymss 全量 | `cd D:/项目/pymss && python -m pytest -q -p no:cacheprovider -m "not integration"`（带上面的 PYTHONPATH） | 652 passed，4 skipped |
| pymss 跨仓库用例 | `test/test_legacy_loudnorm_node.py`（需要 sibling 目录里有 pymss-plugins，且版本为 2.2.0） | 1 passed |
| 插件仓库 | `pnpm build`、`pnpm test:frontend`、`python -m unittest discover -s tests`、`python -m unittest discover -s examples/audio_gain/tests`、`python scripts/generate_registry.py --check`、`node scripts/build_tool_views.mjs --check` | 105 / 31 / 9 全部通过，两项检查通过 |
| Studio Python | `python -m unittest discover -s tests/python -t tests -p "test_*.py"`（带 PYTHONPATH） | **837 passed，6 skipped** |
| Studio 前端 | `pnpm test` 和 `pnpm exec vue-tsc --noEmit` | 1389 passed，3 failed（`tauriLogColor.test.mjs`，与本次无关，见第 5 节）；vue-tsc 通过 |
| Studio Rust | `cd src-tauri && cargo test` | 383 passed，2 failed（VST 扫描超时用例，与本次无关）；`audio_output` 测试需要 PATH 中的 `python` 能导入 pymss 2.2 |
| worker 端到端 | `.tmp/pymss-v2-venv/Scripts/python.exe -X utf8 tests/manual/plugin_v2_worker_smoke.py D:/项目/pymss-studio D:/项目/pymss-plugins D:/项目/pymss-studio/.tmp/e2e-v2` | **24/24 通过** |

### 4.3 图形界面冒烟（尚未进行）

```powershell
$env:PYMSS_STUDIO_PYTHON="D:\项目\pymss-studio\.tmp\pymss-v2-venv\Scripts\python.exe"; $env:PYMSS_STUDIO_PLUGINS_SOURCE="D:\项目\pymss-plugins"; pnpm tauri dev
```

插件安装根目录按"运行时身份 + 解释器路径哈希"区分，用这个解释器启动会得到独立的插件目录，不影响用户已有的安装。重点检查：

- 插件市场的列表和安装；
- 工具页能否打开插件页面，iframe 里的 SDK 和进度条是否正常；
- 禁用、卸载后工具入口是否消失；
- legacy 提示是否出现。

---

## 5. 已知问题与和本次无关的失败

- `tests/tauriLogColor.test.mjs` 的 3 个失败：`scripts/tauri.mjs` 被当成 CommonJS 执行。本次没碰过这两个文件。
- Rust 的 `plugin_host::scan_tests::a_hung_scanner_has_a_bounded_deadline_and_is_reaped` 和 `plugin_host::tests::a_child_that_stops_reading_cannot_block_the_input_timeout`：全量运行时失败，单独运行能通过，属于原生 VST 宿主的时序问题。
- `cargo` 的 dead_code 警告（例如 `plugin_views.rs` 的 `PluginView`、`update_startup.rs`）原本就存在。
- `tests/python/test_plugin_sources.py` 里有一段未暂存的新增用例 `test_local_packages_exclude_frontend_dependencies_and_development_state`，**不是本次加的**，判断为用户自己的 WIP，没有动。
- Studio 的 `docs/plugin-system-design*.md` 是旧的设计文档，仍是 v1 写法，没有更新。

---

## 6. 版本与发布（尚未执行）

- **pymss v2 尚未发布。** 2026-10-29 按用户确认，Studio 的默认运行时、开发和 CI 依赖改为无版本上下限的 `pymss[proxy]`、`pymss-core`，安装时获取可用的最新稳定版本。发布解析脚本支持无版本输入，解析后记录实际构建版本用于产物校验；该记录不限制用户通过内核更新入口升级。v2 发布前，旧版已发布包仍不提供新接口，本地开发必须使用第 4.3 节的隔离解释器。
- 我把 pymss 的 `pyproject.toml` 从 `2.1.10` 改成了 `2.2.0`，但 PyPI 和 Studio 的基线已经是 2.1.12，说明发布流程可能另外设置版本号。**发布前要和用户确认这一处要不要保留。**
- 插件的 `requires_pymss = ">=2.2"` 只是下限，没有上限。

---

## 7. 关键设计点（改动时不要破坏）

1. **插件 id 是唯一身份。** 安装目录名、registry 的 key、Studio 路由（`?pluginView=<id>/<viewId>`）、工具设置的 key（`plugin-tools` 存储里的 `<id>:<viewId>`），全部以它为准。
2. **列出插件不执行插件代码。** Studio 的 `_inventory` 不导入 pymss，有测试守护（`test_plugin_inventory_cache.py::test_listing_does_not_import_pymss`）。导入插件代码只发生在安装前的验证、`forceVerify`，以及实际运行 operation 时。
3. **operation 的声明必须与注册完全一致**，由 pymss 强制执行。由于 id 必须带插件前缀，不同插件之间不可能出现 operation 冲突；只有插件 id 重复时才会冲突，pymss 会报 "already activated from"。
4. **pymss 不读取 `[tool.pymss.studio]`**，Studio 的 Rust 和 worker 也不读取 `[tool.pymss.plugin.contributions.views]`。
5. **`file_tool` 是构建期字段。** 共享前端 `frontend/src/sdk.ts` 里有一个 Tauri 仿真层，把 UI 发出的 `start_audio_tool` 请求转换成 `operations.start`，请求名（`loudness`、`midi` 等）要和 `kit.register_tool(..., tool=...)` 一致。Studio 不读取这个字段。
6. **Studio worker 返回给前端的 JSON 结构**仍沿用 `contributions.views`，这是 Studio 内部接口，以减少前端改动。
7. **进度事件链路**：
   - 插件调用 `kit.emit(type, payload)`；
   - pymss 执行 `report_progress(fraction, message, event={type, payload})`；
   - worker 发出带 `event` 的 `operation_progress`；
   - `PluginViewHost.vue` 把 payload 展开后作为 `task.progress` 转发给 iframe；
   - SDK 读取 `update.event`。
8. **破坏性升级对用户的影响**：旧安装被移到 `legacy-v1/`；预装插件会自动重新准备；工具表单设置会重置一次（旧设置是按目录名保存的）。

---

## 8. 后续工作

### P0 收尾

- [ ] 图形界面冒烟（第 4.3 节）。
- [ ] 按仓库分别提交。pymss 已在独立分支上；另外两个仓库的改动叠加在用户自己的 WIP 上，提交时要和用户确认怎么拆分。提交信息末尾加上约定的 Co-Authored-By 行。
- [ ] 发布 pymss 2.2.0（先确认 `pyproject.toml` 的版本号），并验证 Studio 从公开包源安装最新版后可以运行 v2 插件。
- [ ] 视情况更新或归档 Studio 的旧设计文档 `docs/plugin-system-design*.md`。

### P1（设计评审中提出，尚未开始）

- **统一前端 SDK。** 目前有两套模型：官方插件把共享前端和 Tauri 仿真层 `frontend/src/sdk.ts` 打包进页面；示例插件用宿主注入的 `pymss-sdk.js`。目标：
  - 发布 `@pymss/plugin-sdk`（带 TS 类型）和 `@pymss/plugin-ui`；
  - 用 `pymss-plugin://sdk/v1/` 绝对路径加载；
  - 能力协商：`requires_studio`、`sdk`、`app.info().capabilities`；
  - 去掉 SDK 里写死的业务方法，例如 `runtime.packageStatus` 写死了 funasr，以及 `models.deleteTool`；
  - 删除 convert 的特殊参数映射，以及 `sdk.ts` 里 `start_tempo_key_analysis` 这类分支。
- **无 UI 插件自动生成表单。** 把插件仓库里的 `OperationTool.vue` 和 `operationSchema.ts` 能力迁到宿主：view 没有 entry 时由 Studio 根据 operation schema 渲染。目前它只支持音频输入和基础类型参数，组件里还硬编码了一份参数标签字典。
- **两级模板**：`examples/minimal_operation`（纯 Python）和 `examples/audio_gain`（自定义 UI）。另加 `pymss plugin new` 脚手架，以及 Studio 的开发模式（链接本地目录、热重载）。
- **依赖统一解析**：每个运行时只用一个插件依赖目录，以"运行时 freeze + 所有启用插件的依赖"作为约束一起解析；用声明式的 `optional_packages` 替代 `deferred_dependency_operations`。
- **把 file tool 换成真正的参数 schema。** 目前 inspect、merge、sdr、slicer 只提供不透明的 `request` 输入，loudnorm、asr、game 则两种都有。另外 pymss 内置的 `pymss.kit.{convert,inspect,sdr,slice,merge}`（`operations/kit_builtins.py`）和官方插件的实现有重叠，可以考虑合并。

### P2

- registry 改为 zip 分发并带 sha256 校验，用户安装时不再需要本机有 git。
- 支持多个插件源。
- 重模型插件（ASR、GAME）可选进程隔离。

---

## 9. 速查：本次新增或删除的 API

- pymss：
  - `Runtime.load_plugins(roots)`
  - `PluginManager(roots)`
  - `bootstrap(force, roots)`
  - `install(..., registry_url=)`
  - `list_available(registry_url=None, plugins_dir=None)`
  - `OperationContext.report_progress(..., event=)`
  - `pymss.plugins.kit.*`
  - `OperationDecl`
  - `pymss plugins validate`
- pymss 已删除：`ENTRYPOINT_GROUP`、`_scan_entrypoints`、`ViewContribution`、`PlayerContribution`、加载 legacy（无清单）插件的路径。
- Studio worker 新增：`_read_manifest`、`_studio_views`、`_migrate_v1`、`_legacy_plugins`、`_directory_plugins`、`operation_uses_optional_backend`、`_REGISTRY_URL`、`_RUNTIME_LOCK`。
- Studio 已删除：worker 命令 `audio_tools`、Tauri 命令 `start_audio_tool`、参数 `load_plugin_tool_settings(legacy_tool_id)`、inventory 缓存文件 `inventory-cache.json`。
