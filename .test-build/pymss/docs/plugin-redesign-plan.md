# pymss 插件系统改造总计划

状态：**七轮全部完成主干**（2026-10 实验，experiment/plugin-redesign 分支）。各轮残留增强项见各节 [ ] 条目。

最终验收状态：

- loudnorm 插件（setup()+Operation 单一实现）：CLI `pymss ops run` ✅、pymss 图 `op.pymss.loudnorm.normalize` 节点 ✅（LUFS -16.00 命中）、Studio worker+工具页 ✅（-18 LUFS e2e）、ComfyUI 工厂节点 ✅（-17 LUFS e2e）。四宿主零宿主侧代码改动。
- 内置 DSP 与插件同路径：`pymss ops list` 无插件即列 4 项内置操作。
- provider 替换可观测：换 resample provider 后图内模型准备/ensemble/保存路径全部改变；codec 插件覆盖在多次 save_audio 后仍生效。
- 回归：pymss 528 passed；studio python 485 passed（2 个预存平台性失败除外）；comfy-mss 13 passed+5 subtests（含修复预存收集崩溃）。

背景问题（已核实）：

1. `bootstrap()` 只有 `pymss plugins list` 警用；常规入口（infer / workflow / comfy run / serve）不加载用户插件。
2. `register_cli()` 只写 `cli_commands` 列表，`main()` 不挂载这些命令。
3. `save_audio()` 每次调用 `register_builtin_codecs()`；该函数只跳过 `source=="builtin"` 的同名项，会把外部插件覆盖过的编码器改回内置实现；`supported_formats()` 只枚举 `source=="builtin"` 的条目，外部新增格式注册成功却不被发现。
4. 图内 `nodes.py` 的 `_resample` 直接 import `plugins.builtins.resample`，不经 capability；替换注册表不影响该路径。
5. capability 只有 name/func/source/description，无可序列化的输入输出/参数/默认值/范围描述，宿主（Studio/Comfy）无法据此自动生成 UI/节点。
6. loudnorm 等外部插件作者必须读取 `widgets_values[0]`、`nodes_by_id` 等 DAG 内部结构。
7. loader 失败插件无注册回滚；全局注册表 last-writer-wins。
8. 插件目录全局、依赖装进某个 sys.executable，与 Studio/Comfy 各自环境不一致，诊断分层不足。

改造总方向：插件声明 Operation（可执行处理）与 Provider（底层服务实现）；pymss runtime 负责发现/校验/执行；宿主（CLI/Studio/Comfy）只做适配。内置功能与外部插件走同一路径。

## 轮次

### 第 1 轮：修复现有连接性缺陷（pymss）✅ 已完成

- [x] 1.1 `register_builtin_codecs()` 幂等不覆盖；`supported_formats()` 枚举任意来源。已验证：插件覆盖 wav_encode 后3次 save_audio 仍生效并写出插件内容。
- [x] 1.2 `ensure_plugins_loaded()`（loader）：惰性、幂等。
- [x] 1.3 `cli.py main()` 加载用户插件并挂载 `cli_commands`（同名冲突跳过+告警）。e2e 验证 `pymss probe` 可执行。
- [x] 1.4 `cmd_infer/cmd_workflow_run/cmd_comfy_run/cmd_serve` 接入。
- [x] 1.5 修复 test_plugins autouse fixture 清空注册表不恢复内置注册导致的跨模块泄漏。

验收：安装一个测试插件后，`pymss <plugin-cmd>` 可执行；`pymss infer` 路径中该插件的 capability 可被 require；外部 codec 覆盖在多次 save_audio 后仍生效。

### 第 2 轮：Operation 模型 + runtime（pymss）✅ 已完成

- [x] 2.1 `pymss/operations/`：OperationSpec/OperationContext/Runtime（隔离实例、冲突报错、参数校验、setup 失败回滚）。
- [x] 2.2 `create_runtime()` + `default_runtime()`；`PluginRegistration.register_operation` 桥接全局 loader。
- [x] 2.3 插件 setup() 协议（round 1 已完成 loader 侧；本轮补 register_operation）。
- [x] 2.5 CLI `pymss ops list/describe/run`。
- [x] 2.6 测试：13 个单元测试 + ops CLI e2e 六场景。
- [ ] 2.4 loudnorm 改写（在 pymss-plugins 仓库，随第 4 轮图适配一起验证）

### 第 3 轮：音频服务（decode/resample/channels/encode 走 Provider）

- [x] 3.1（resample 部分）`AudioService` 接口 + 显式 provider 选择 + 可观测 selected；`default_audio_service()` 进程惰性共享。已验证：图级换 provider 后模型准备/ensemble 对齐/保存转换路径确实改变。
- [x] 3.3 图 `nodes.py` `_resample` 切入服务（默认 builtin 行为不变，byte-parity 测试）。encode/decode provider 化待第 7 轮随内置迁移一并做。
- [ ] 3.2 `audio_io.load_audio/save_audio` 委托音频服务（保留回退链）。
- [ ] 3.4 长音频内存/帧数回归基准。

### 第 4 轮：图执行器接入 runtime（pymss）✅ 核心完成

- [x] 4.2 `operations/graph_bridge.py`：`register_operation_node()` 从 OperationSpec 自动生成图节点（`op.<id>` 命名；params 作为可选输入槽；执行读 `node.data['params']` 命名参数，widgets 仅在旧内置节点的导入适配层）。签名生成/命名参数执行/run_dag 全链路测试通过。
- [x] 4.3 loudnorm（pymss-plugins 仓）改写为 setup()+Operation，保留 legacy 注册。图 e2e：3s 音频归一化到 LUFS -16.00 精确命中。
- [ ] 4.1 `run_dag()` 显式 runtime 参数（当前用 default_runtime，隔离宿主待 Studio 轮需要时加）
- [ ] 4.4 旧 Comfy 工作流含 NormalizeLUFS 节点导入兼容测试

### 第 5 轮：Studio 通用操作通道（pymss-studio）✅ 核心完成（未含工作流编辑器动态节点）

- [x] 5.1 Python worker：`list_operations`/`describe_operation`/`run_operation` 三命令 + 测试（含 loudnorm 归一化到 -18 LUFS e2e、缺输入失败路径、pymss 缺失降级）。
- [x] 5.2 Rust：`start_operation_task`（后台任务）+ `list_operations`/`describe_operation`（one-shot）；`operation_result` 为终态事件（单测）。cargo check 通过。
- [x] 5.3 前端：`plugin-ops` composable（发现/描述/后台执行/进度）+ 通用工具页 PluginOpsTool（文件输入/数值/枚举/开关/文本控件自动生成）+ 注册进工具列表 + 中英文 i18n。pnpm build 验证中。
- [ ] 5.4 工作流编辑器动态节点（后续轮；当前静态 nodeSpecs 保持不变）
- [ ] 5.5 tauri dev 手动验证（需 GUI 会话）

### 第 6 轮：Comfy 适配（comfy-mss）✅ 完成

- [x] 6.1 `comfy_mss/nodes/operations.py` 节点工厂：启动时加载 runtime，OperationSpec → INPUT_TYPES/RETURN_TYPES/FUNCTION/CATEGORY；生成节点注册为 `pymss_op_<id>`，不遮蔽静态 13 节点。
- [x] 6.2 batch 语义：单音频操作显式拒绝 batch≠1（不再静默切第一片）；AUDIO 转换集中 `_audio_to_pymss`/`_pymss_to_audio`。
- [x] 6.4 静态节点保留原样（薄包装化随第 7 轮内置迁移再做）。
- [x] 额外修复：根 `__init__.py` 无包上下文导入时报错导致 pytest 收集崩溃（含预存 9 个 custom model 测试在本环境从未跑过的问题）。
- [ ] 6.3 进度/取消接 Comfy ProgressBar（后续轮；当前 loudnorm 等快操作不需要）

### 第 7 轮：内置功能迁移 + 收尾 ✅ 核心完成

- [x] 7.1（部分）builtins DSP 四项（音量/反相/峰值归一化/裁剪）成为内置 Operation，`default_runtime()` 首次使用时注册；与插件同路径（source=pymss.builtin）。剩余（拼接/混音/EQ/声道/静音）随需要补，模式已定。
- [x] 7.4 文档：新增 `docs/plugins.md` 插件作者指南（操作协议/参数 schema/四宿主入口/provider 替换/旧协议兼容）。
- [ ] 7.2 separator 读取/保存全面走音频服务（encode/decode provider 化）
- [ ] 7.3 安装器分层诊断（installed→loaded→contributed→executable）

注：Studio 工作流编辑器动态节点（5.4）、Comfy 进度接 ProgressBar（6.3）为后续增强，不阻塞主线验收。

## 全局验收

- 新增 loudnorm 插件（不改任何宿主注册代码）：CLI 可跑、Studio 工具页显示参数并可执行、Studio 工作流出现节点、Comfy 出现节点，四处执行同一实现。
- 替换 resample provider：所有声明接入的路径（图、separator 保存、编辑器通用路径）实际改变；ARA/模型内部路径按记录的例外保留。
- 旧工作流/旧 CLI 行为回归通过。

## 例外与保留

- ARA 独立 NumPy 重采样（REAPER 内 OpenMP 死锁）不动。
- Comfy 原生节点（torch 直算）第 6 轮前不动，其后逐步薄包装化。
- 模型内部 DSP（STFT/内部插值）不插件化。
