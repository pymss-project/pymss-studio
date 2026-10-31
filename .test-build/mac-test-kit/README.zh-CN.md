# 第二轮 macOS ARM64 插件验收

适用：Apple Silicon（M 系列），macOS 14 或更高版本。请使用本次整套文件，不能混用上一轮应用或工具包。这是临时签名、未经 Apple 公证的测试应用；自动更新已关闭。

本轮修复了通用文件工作流在界面被拒绝、文件结果被当作音频以及读取节点信息时的取消处理。**上一轮主题／动画空白现象尚未确认根因，本轮必须复测，不能预先标为通过。**

## 1. 安装并核对版本

1. 解压完整测试包，在该目录执行 `shasum -a 256 -c SHA256SUMS.txt`，全部应为 `OK`。这一步在修改／生成任何测试文件之前完成。
2. 退出旧的 Plugin Test 应用，打开本次 DMG，将 **Pymss Studio Plugin Test.app** 放入“应用程序”，替换旧测试应用；正式版 Pymss Studio 不需要修改。旧测试报告和数据请保留。
3. 解压 `mac-plugin-test-kit.zip`。以下所有 `.command` 都在解压后的 `mac-plugin-test-kit` 目录内。
4. 首次启动若被系统阻止，可到“系统设置 → 隐私与安全”允许打开。若已核对校验值但遇到隔离标记阻断，只针对这套测试应用和本次解压的工具包处理：

   ```bash
   xattr -dr com.apple.quarantine "/Applications/Pymss Studio Plugin Test.app"
   xattr -dr com.apple.quarantine "/本次解压位置/mac-plugin-test-kit"
   ```

   第二条必须替换成实际完整路径。不要对个人主目录、整个下载目录或系统目录运行；不要关闭全局 Gatekeeper。上一轮 Rust CLI 首次握手失败与工具包隔离标记有关。
5. 此后通过 `Open Plugin Test.command` 启动。它会校验应用／工具包一致，并使用 `~/.pymss-studio-plugin-test-<本次快照前8位>`，从干净测试数据开始；上一轮测试数据不会删除。
6. 在设置确认 Studio 版本含 `0.0.24-dev.plugins.20261031`，pymss 版本含 `2.2.0+plugins.20261031`，末尾快照值与本包 `build-info.json` 一致。

不要执行“更新内核”“同步依赖”或重装运行环境，以免将本轮源码快照换回公开旧版。不要与旧测试应用同时运行。

双击脚本无法运行时，在终端执行 `bash "脚本完整路径.command"`。应用不在“应用程序”时，把 `.app` 完整路径作为脚本第一个参数，例如 `bash "Open Plugin Test.command" "/其他位置/Pymss Studio Plugin Test.app"`。

## 2. 自动检查（先做一次，手测结束后再做一次）

退出测试应用，在工具包目录执行：

```bash
bash "Verify Plugin Runtime.command"
```

检查会使用新建私有目录，不碰正式版、不下载模型、不要求安装 Python／Rust／Cargo／Homebrew。它检查应用签名、快照、四个基础工具、Python 独立依赖、Rust callback、UTF-8 错误恢复、旧输出不覆盖、文件节点能力以及 5 节点图的 LF／CRLF 文件输出。

**通过条件**：控制台均为 `PASS`，最终 `summary.json` 为 `passed`；`expected rejection` 是故意输入非法 UTF-8 的正常拒绝。Worker 自动通过不代表 GUI 安装或主题显示已通过。

报告位于 `~/Library/Logs/PymssStudioPluginTest/<时间与进程号>/`。保留两次报告。失败时先保存错误与报告，不要连续重装。

## 3. 一键准备手测素材

运行 `Prepare GUI Fixtures.command`。会生成并打开工具包下新的 `gui-<时间>-<进程号>` 目录，内含：

- `GUI-PATHS.txt`：插件安装来源、输入和各项输出目录的完整路径，可直接复制。
- `inputs/stereo-48k-12s.wav`：12 秒／48 kHz／双声道／PCM24 音频。
- `inputs/中文 input.txt`、`invalid-utf8.txt`、`cancel-64MiB.txt`：普通、错误与取消测试输入。
- `RPC-Text-Workflow.json`：3 个字符串节点 + Python／Rust 转换节点，共 5 节点、4 连线。
- `expected/`：LF 和 CRLF 的参考文件。
- `outputs/`、`screenshots/`、`MANUAL-RESULTS.md`：本次输出和验收记录。

素材完全自动生成，无需提供自己的音频。准备完成后不要移动工具包；若移动，重新生成一套素材。每次生成都是新目录，不覆盖旧文件。

## 4. GUI 安装与通用文件工作流（本轮重点）

1. 从启动脚本打开应用。首次进入工具页应有转换、合并、切片、音频信息四个基础工具。
2. **必须在插件页用“从来源安装”最终提交安装**：从 `GUI-PATHS.txt` 复制 Python 插件目录，完成安装；再安装 Rust 插件目录。不要用 Worker 命令代替这一项。
3. 两个工具名称都为“文本转换”，用插件 ID `example.python_rpc_text` 与 `example.rust_rpc_text` 区分。
4. Python 工具选择普通文本，输出到 `outputs/python`，分别运行 LF 和 CRLF；Rust 工具同样输出到 `outputs/rust`。再重复运行一次，确认旧结果路径和内容保持不变。
5. 各选择一次非法 UTF-8 文件，应显示明确错误；换回正常输入后应能重试成功。截图保留错误详情。
6. 各选择 64 MiB 输入，**观察到运行状态后**点击取消；确认取消后能够再次运行普通文本。若任务已结束才点击，不计为取消通过。取消生成的大文件可以保留，结果检查会跳过大文件。
7. 工作流页导入 `RPC-Text-Workflow.json`。应显示 **节点 5 · 输出 2**，没有“无有效保存输出”的阻断。
8. 打开独立节点编辑器，确认风险提示后继续。输出目录选择 `outputs/workflow`，运行一次。
9. 在“日志与结果 → 结果”中应看到两份文本文件及 `text/plain`，可以打开位置；不能出现文本文件的音频播放按钮、播放进度条或播放失败提示。Python 输出 LF、Rust 输出 CRLF。保存列表与结果截图。
10. 退出应用，通过启动脚本重开，确认插件、工作流、设置与文件结果仍可恢复。

## 5. 主题、动画和音频

请在 `MANUAL-RESULTS.md` 中逐项记录，不要把“关动画正常”当作“开动画正常”。

| 操作 | 预期／记录 |
| --- | --- |
| 默认 WAV → FLAC → WAV | 使用提供音频，保存到 `outputs/audio`；保留 48 kHz、双声道，最终 WAV 选择 PCM24；两份输出均为 12 秒 |
| 开启动画：设置中切浅色／深色，立即返回工具 | 往返至少 5 次；页面、输入框、下拉选项与日志的配色一致；没有残留遮罩或空白页面 |
| 转换运行时打开日志，完成后关闭 | 背景保留，日志可见，关闭后结果不裁切；深浅主题各做一次 |
| 关动画后重复前两项，再恢复开动画 | 分别填写结果。若只在开动画异常，保留开启时截图和触发顺序 |
| 安装工具包 `plugins/analysis` | 用 GUI 从来源安装；音频分析可直接选文件、切操作不丢输入，结果为指标；频谱显示曲线 |
| 静音音频响度 | 可选自己的静音副本；无效／无限值不应导致整个结果页失败 |
| 切片器清空任一数字字段 | 运行按钮禁用，不发送任务 |
| 停用再启用音频信息插件 | 工具入口与状态一致，恢复后可使用 |

若出现空白：先截图，再记录动画开关、主题、上一步路由、任务是否运行、日志是否打开，以及是否需重启才能恢复。不要先清空数据或重装。

## 6. 结果校验与反馈

1. 退出应用，运行 `Check GUI Results.command`。它检查 Python／Rust／工作流三组 LF／CRLF、音频往返元数据及应用最终签名；只读结果文件。`FAIL / NOT COMPLETED` 也可能表示对应步骤尚未执行，查看本次 `gui-results-*.json`。
2. 再运行一次 `Verify Plugin Runtime.command`，确认完整自动检查及签名仍通过。
3. 填写 `MANUAL-RESULTS.md`。反馈本包 `build-info.json`、两次自动检查的 `summary.json`／`console.log`，以及 GUI 目录中的 `gui-results-*.json`、`MANUAL-RESULTS.md`、`screenshots/`。不需要发送 64 MiB 输入或个人音频。

这些检查不会自动上传，不会删除旧报告、旧应用数据或正式版文件。本轮仍未覆盖模型下载与推理；若另测，请单独注明模型和运行环境，不计入上述插件验收结论。
