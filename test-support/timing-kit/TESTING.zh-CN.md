# macOS Timing 人工测试步骤

按以下顺序执行：运行 `Verify Test Package.command`，运行 `Prepare Test Fixtures.command`，再使用 `Open Timing Test.command` 启动应用。确认诊断或关于页面显示测试构建 ID 与版本，且数据根为 `~/.pymss-studio-timing-test-b48688e26/gui`。

在插件页面选择本地来源，定位到工具包中的 `plugins/timing` 并安装。确认版本为 `0.3.2`，操作中存在 `pymss.timing.process_file`。不得使用在线市场的旧版本。

进入 Timing 工具，选择 `fixtures/原始录音 48k.wav`。分别选择 Radius 与 Rubber Band，均使用 `n_steps=3`；每次完成后试听并记录结果。

先将输出目录分别选择为：

- `outputs/radius-50`：Radius，`n_steps=3`，时长 50%；
- `outputs/radius-200`：Radius，`n_steps=3`，时长 200%；
- `outputs/rubberband-50`：Rubber Band，`n_steps=3`，时长 50%；
- `outputs/rubberband-200`：Rubber Band，`n_steps=3`，时长 200%。

输出应保持 48 kHz 双声道，50% 的帧数约为原始一半，200% 约为两倍。处理进度在文件保存完成前不得显示 100%。

继续执行：

- 将输出目录手工输入到有效目录后运行；再打开目录选择器并取消，确认原输入值未被清空。
- 留空输出目录，以 48 kHz 原始录音、100% 时长运行一次，确认输出写入 `gui/outputs` 默认目录。
- 使用 16 kHz、96 kHz 素材分别输出到 `outputs/radius-16k`、`outputs/radius-96k`，均使用 Radius、`n_steps=3`、时长 100%，确认可完成且采样率保留。
- `outputs/repeat` 已预置 1 秒旧文件 `原始录音 48k_processed.wav`，不得删除或覆盖它。对 12 秒输入以 100% 时长重复运行两次到该目录，确认新增两个自动去重文件；不要要求首个新文件一定使用未去重名称。
- 使用 `取消测试 180s.wav` 输出到 `outputs/cancel-retry`，在运行态点击取消；确认无最终半成品。随后改用 12 秒正常输入在同一目录重试，确认可正常完成且不覆盖已有完整文件。此项必须人工检查 GUI 运行状态、取消动作和日志截图，不能由脚本自动推断成功。
- 设置自定义输出目录后退出并重新启动测试 app；确认目录设置恢复并仍可运行。

在 GUI 执行在线内核更新。启动环境故意含 `PIP_NO_INDEX=1`，更新仍应解析并升级到 `pymss 2.3.2`、`pymss-core 0.1.14`；Torch `2.7.1` 与 MLX 保持不变。若网络离线或更新无法完成，必须记录“未测”，不能标记通过。更新后再次运行一次 Timing，并重新执行签名检查。

完成 GUI 操作后，先用外部编辑器填写数据根的 `MANUAL-RESULTS.md`，并将截图放入数据根的 `screenshots/`。随后按 Cmd+Q 完全退出测试 app，再运行 `Check Test Results.command`。回传该命令输出的 `Timing-Test-Evidence-...zip`；其中包含报告、清单和截图，不应包含大音频文件或运行时 overlay。

本包不验收应用自更新，也不能通过 macOS 测试证明 Windows Git 修复。
