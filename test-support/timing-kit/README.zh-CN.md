# Pymss Studio Timing Test 测试包

本工具包用于人工验收 `Pymss Studio Timing Test.app`：

- 构建 ID：`b48688e26`
- 应用版本：`0.0.25-dev.timing.20261103.b48688e26`
- 适用设备：Apple Silicon（M1、M2、M3 或 M4）和 macOS 14 或更高版本
- 默认应用位置：`/Applications/Pymss Studio Timing Test.app`
- 备用位置：`~/Applications/Pymss Studio Timing Test.app`

将完整 ZIP 和同目录的 `SHA256SUMS.txt` 一起复制到 Mac，在 Terminal 切换到该目录并运行 `shasum -a 256 -c SHA256SUMS.txt`。通过后解压完整 ZIP，进入解出的目录，再运行一次 `shasum -a 256 -c SHA256SUMS.txt` 校验 DMG、文档与内部工具包。解压 `mac-timing-test-kit.zip` 后再进行后续步骤。

挂载 DMG 并把 `Pymss Studio Timing Test.app` 拖入 `/Applications`。此名称独立于正式版，不会覆盖正式应用。若没有写入 `/Applications` 的权限，可改放到 `~/Applications`，无需 sudo。本测试包采用临时签名，未公证；首次运行请按下方脚本处理本次已校验文件。

默认测试数据根为 `~/.pymss-studio-timing-test-b48688e26`，其中 `gui/` 保存应用数据，`fixtures/` 保存测试素材，`reports/` 保存验证结果。`GUI-PATHS.txt`、`GUI-PATHS.json` 和 `MANUAL-RESULTS.md` 也位于该数据根。测试中需要复制的完整数据和素材路径，请始终从 `GUI-PATHS.txt` 复制，不要手工猜测。

工具包最终目录名为 `mac-timing-test-kit`。请按顺序运行以下 macOS `.command` 文件。每个脚本都支持将应用路径作为第一个参数传入。

1. `Verify Test Package.command`：校验本次 app 与工具包、清理它们自身的隔离属性并执行 CLI 自动检查。不会使用 sudo，也不会修改 Gatekeeper 的全局设置。
2. `Prepare Test Fixtures.command`：生成 12 秒 48 kHz 双声道原始录音、16 kHz、96 kHz 和 180 秒取消测试素材，并创建建议的结果目录。
3. `Open Timing Test.command`：以隔离数据根启动测试应用；它会故意设置 `PIP_NO_INDEX=1`，供 GUI 在线内核更新复测使用。
4. `Check Test Results.command`：检查已生成结果的帧数、采样率、命名、旧文件保护、签名与报告收集状态。

若首次执行被系统阻止，只在 Terminal 中执行：

```sh
bash "/实际工具包路径/mac-timing-test-kit/Verify Test Package.command"
```

不要关闭 Gatekeeper 或执行全局安全设置。若脚本受应用目录权限限制，将测试 app 移到 `~/Applications` 后重试。所有脚本使用 app 内置 Python，无需安装系统 Python、Xcode 或开发工具。

脚本通过只代表自动检查通过，不能代替 GUI 本地来源安装、算法试听、取消、重启或在线更新的人工记录。请勿从在线市场安装 Timing 0.3.1；必须在 GUI 中从工具包中的 `plugins/timing` 安装 Timing 0.3.2。

本包不测试应用自更新，也不能证明 Windows Git 长路径修复。默认不会读取或修改正式版应用和正式数据。
