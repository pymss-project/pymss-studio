# MoInstaller

用 Rust 编写的 Windows 安装器工具链，目标是取代 Inno Setup 的核心使用场景：

> 开发者写一份 `installer.toml` 清单 → 运行 `mo build` → 得到单个 setup.exe
> （内嵌压缩文件 + 向导 GUI + 静默安装 + 卸载程序），双击即装，控制面板可卸载，
> 且具备分层定制扩展能力（取代 Inno Pascal Script 的定位）。

**作者指南：[AUTHORING.md](AUTHORING.md)**（清单字段、常量、钩子/脚本 API、Inno 对照表、签名）

## 里程碑

- [x] **M1 骨架**：workspace + mo-core（清单/overlay）+ mo-build + mo-setup 静默装文件
- [x] **M2 完整安装语义**：注册表/快捷方式/环境变量/卸载/回滚/日志/退出码 + 事件总线 + L1 外部命令钩子
- [x] **M3 egui 向导 + 主题**：六页向导、组件选择、进度、中英双语、L1 主题（横幅/色板/文案覆盖/页面显隐）
- [x] **M4 rhai 脚本扩展（L2）**：全生命周期钩子、沙箱白名单 API、超时与步数限制
- [x] **M5 打磨**：自删、体积优化（release 模板 ≈4.3MB < 4.5MB 预算）、AUTHORING.md、
      winresource 图标/版本资源、`--sign` 透传

## 架构

Cargo workspace，5 个 crate：

| crate | 职责 |
|-------|------|
| mo-core | 清单 schema（serde）+ overlay 包格式读写 + 目录常量（纯逻辑，全平台单测） |
| mo-engine | 安装/卸载引擎：文件/注册表/快捷方式/env/回滚日志/事件总线（windows crate） |
| mo-script | rhai 钩子宿主：沙箱白名单 API、事件分发、超时/步数限制 |
| mo-setup | 安装器运行时：egui 向导 + 静默模式 + overlay 解析 + 提权 + 卸载模式 |
| mo-build | builder CLI（mo）：读 TOML → 校验（含 rhai 预编译）→ 压缩 → overlay → 签名 |

## 开发

```powershell```
cargo build --workspace   # 模板两阶段构建，首次需运行两次
cargo build --workspace
cargo test --workspace    # 68 个测试（含 e2e：build→静默装→断言→卸载→断言）
cargo fmt --all && cargo clippy --workspace --all-targets
```

mo-build 通过 build.rs 将 `target/release/mo-setup.exe` 嵌入为模板。
开发期修改 mo-setup 后需重新 `cargo build --workspace` 两次使新模板生效。

## 状态与测试

- 68 个自动化测试全绿（单测 + 端到端：完整安装→卸载、钩子失败回滚、脚本跳文件/中止、
  主题包构建、CRC 篡改检测、中文/空格路径、覆盖安装）
- GUI 手动冒烟清单见 AUTHORING.md
- CI 建议：GitHub Actions windows-latest，`cargo fmt --check && cargo clippy -D warnings && cargo test`

## 协议

MIT OR Apache-2.0