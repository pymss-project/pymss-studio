# MoInstaller：用 Rust 取代 Inno Setup — 设计与实施计划

日期：2026-10-06（R2：按用户反馈补充"定制化拓展能力"专章）｜ 状态：待审阅（关键选型以"假设"标注，可在审阅时否决）

## 1. 目标

构建一个 Rust 编写的 Windows 安装器工具链 **MoInstaller**，取代 Inno Setup 的核心使用场景：

> 开发者写一份 `installer.toml` 清单 → 运行 `mo build` → 得到**单个 setup.exe**（内嵌压缩文件 + 向导 GUI + 静默安装 + 卸载程序），双击即装，控制面板可卸载，且具备分层定制扩展能力（取代 Inno Pascal Script 的定位）。

**成功标准（v1 验收）**：
1. `mo build installer.toml` 产出单文件 setup.exe（模板开销压缩后 < 4.5 MB + payload；rhai 脚本引擎计入后）。
2. 双击运行：完整向导（欢迎 → 许可 → 目录 → 组件 → 安装进度 → 完成）成功安装示例应用。
3. `/VERYSILENT /DIR=D:\x` 静默安装成功，退出码 0（CI 可用）。
4. "应用和功能"中出现条目；卸载后文件/快捷方式/注册表/环境变量无残留。
5. `cargo test` 全绿（含端到端集成测试：build → 静默装 → 断言 → 静默卸载 → 断言）。
6. 带事件钩子脚本（rhai）与自定义主题的示例包通过同一套 e2e 流程。

## 2. 关键决策（含假设）

| # | 决策 | 理由 | 状态 |
|---|------|------|------|
| D1 | **完整工具链**（清单驱动 CLI → 单文件 setup.exe），非 Rust 库路线 | "取代 Inno"的语义就是 Inno 的形态；installrs（库路线）与 Hagane（YAML 路线）已验证两条路，工具链对非 Rust 用户零门槛 | ⚠️ 假设（提问超时，按目标语义推定） |
| D2 | **仅 Windows 10+ x64**（代码不写死，预留 ARM64） | Inno 本身 Windows 专属；egui/winit 对 Win7 支持差 | ⚠️ 假设 |
| D3 | GUI 用 **egui**（软渲染可回退，无运行时依赖） | 单 exe、不依赖 WebView2/系统组件、UI 现代化是与 Inno 的差异化；Hagane 用 WebView2 但旧系统有风险 | 推荐（可改为 native-windows-gui 或 WebView2） |
| D4 | 压缩用 **zstd**（默认 level 19，多线程） | Rust 生态最成熟；包格式头部带算法 ID，后续可加 LZMA | 推荐 |
| D5 | 静默参数**兼容 Inno 习惯**：`/SILENT /VERYSILENT /SUPPRESSMSGBOXES /DIR= /GROUP= /NORESTART` | 迁移成本低，直接卖点 | 推荐 |
| D6 | 清单格式 **TOML**（编译为 canonical JSON 内嵌） | Cargo/Rust 社区事实标准 | 推荐 |
| D7 | v1 界面语言：**中英双语**内置 | Inno 有 60+ 语言，YAGNI；架构上用字符串表，后续可扩 | 推荐 |
| D8 | 开源协议 **MIT OR Apache-2.0** | Rust 生态惯例 | 推荐 |
| D9 | **定制扩展采用三层模型（L1/L2/L3）**：L1 声明式主题+外部命令钩子、L2 rhai 脚本钩子进 v1；L3 代码级自定义向导页进 backlog | 用户明确要求定制化能力；分层使 v1 可交付、能力可增长 | 推荐（本轮新增） |
| D10 | 脚本引擎选 **rhai**（非 Lua/WASM/JS） | 纯 Rust 无 C 依赖、默认沙箱（无文件/网络 IO，对安装器场景安全关键）、serde 集成、专为宿主嵌入设计；mlua/WASM 留作 backlog | 推荐（详见 5.3） |
| D11 | v1 **不做**：安装包加密、双架构合并包、自动更新（Velopack 领域）、脚本 API 稳定性承诺（v1 标 experimental） | YAGNI，明确 out of scope | 推荐 |

## 3. v1 功能范围（对照 Inno）

**做**：
- 安装核心：向导 GUI、许可页、目录选择、组件（可选组）、磁盘空间预检、文件安装（zstd、CRC32）、覆盖/仅新/跳过策略、快捷方式、注册表、环境变量（广播 WM_SETTINGCHANGE）、运行安装后程序、静默/超静默、退出码语义、卸载（控制面板 + uninstall.exe + 用户数据可选保留）、失败尽力回滚、安装日志。
- **定制扩展**：声明式主题（横幅/侧图/色板/字体大小/窗口尺寸、全部页面文案覆盖）、页面显隐与顺序调整、L1 外部命令钩子、L2 rhai 事件钩子（安装+卸载全生命周期，见 5.3）。

**不做（backlog）**：L3 代码级自定义向导页面（动态表单/脚本生成 UI）、Lua/WASM 脚本后端、多语言扩展、自定义 exe 图标/版本资源（M5 用 winresource 支持）、签名（v1 仅文档 + `--sign` 透传 signtool 占位）。

## 4. 架构

Cargo workspace，5 个 crate（本仓库为全新空项目）：

```
crates/
  mo-core/     # 清单 schema（serde）+ overlay 包格式读写 + 错误类型（纯逻辑，可全平台单测）
  mo-engine/   # 安装/卸载引擎：文件、注册表、快捷方式、环境变量、回滚日志、事件总线（windows crate，仅 Windows）
  mo-script/   # rhai 钩子宿主：脚本加载、沙箱白名单 API、事件分发、静默模式语义（跨平台可单测）
  mo-setup/    # 安装器运行时 binary：egui 向导 + 静默模式 + 自身 overlay 解析 + 提权重启 + --uninstall 模式
  mo-build/    # builder CLI（命令名 mo）：读 TOML → 校验 → 压缩 payload → overlay 到模板 exe → 输出
```

**模板嵌入的鸡生蛋问题**：mo-build 通过 `build.rs` 两阶段构建——workspace 构建时先产出 `target/release/mo-setup.exe`，mo-build 的 build.rs 将其 `include_bytes!` 进 `OUT_DIR`。发布 mo-build 发行版时模板已固化，最终用户无感。开发期改 mo-setup 需 `cargo build --workspace` 两次（文档注明；后续可加 `mo build --refresh-template`）。

## 5. 数据格式

### 5.1 installer.toml（schema 即公共 API）

```toml
[app]
id = "com.example.myapp"        # 卸载注册表键名，全局唯一
name = "My App"
version = "1.2.3"
publisher = "Example Inc."
icon = "assets/app.ico"         # GUI 内显示（非 exe 资源）

[options]
require_admin = false           # true 则提权重启
default_dir = "{pf}\My App"    # 支持 {pf} {pf32} {localappdata} {group} 等常量
license = "LICENSE.txt"         # 可选，显示许可页
compression = "zstd19"          # zstd1..19 | store

[theme]                         # L1 声明式定制
banner = "assets/banner.png"    # 向导顶部横幅（497×58 同 Inno 比例或自定义）
sidebar = "assets/side.png"     # 左侧大图
accent = "#2E7CF6"              # 主色（按钮/进度条/链接）
window = { width = 660, height = 460 }
strings."wizard.btn.install" = "开始安装"   # 全部 UI 文案可覆盖（key 清单见文档）
pages = ["welcome", "license", "dir", "components", "install", "finish"]
hide_pages = []                 # 或直接从 pages 中省略即可
page_order = []                 # 可选：显式顺序覆盖

[[components]]                  # 可选；无 components 段 = 单组件全装
id = "main"; name = "主程序"; required = true
[[components]]
id = "docs"; name = "文档"; required = false

[[files]]
src = "dist/**/*"               # glob
dst = "{app}"
component = "main"
overwrite = "overwrite"         # overwrite | skip-if-newer | skip-if-exist

[[shortcuts]]
name = "My App"; target = "{app}/myapp.exe"; dest = "desktop|start-menu"; component = "main"

[[registry]]                     # 可选；值类型 string|dword|expandSZ
root = "hkcu|hklm"; key = "..."; name = "..."; value = "..."; component = "docs"

[[env]]                          # 可选；Inno 没有原生 env 支持——差异化功能
name = "PATH"; op = "append"; value = "{app}"; scope = "machine|user"

[uninstall]
keep = ["{app}/settings.json"]  # 卸载时保留的用户数据

[run]
after = "{app}/myapp.exe"        # 完成页可选运行

[script]                         # L2 rhai 钩子（见 5.3）；二选一
file = "setup.rhai"              # 外部脚本文件（打包进 overlay）
# inline = '''fn initialize_setup(ctx) { true }'''

[[hooks]]                        # L1 外部命令钩子（无脚本依赖的轻量定制）
event = "after_install"          # 事件名见 5.3 表
run = "{app}/tools/migrate.exe"  # 支持常量展开
args = ["--from-old"]
```

### 5.2 setup.exe overlay 布局（从文件尾定位）

```
[mo-setup.exe 原始 PE（asInvoker manifest）]
[entry 0..N]  每条目: path_len(u16le)+path(utf8)+orig_size(u64)+crc32(u32)+attrs(u32)+zstd frame
[payload 索引: 各 entry 偏移表，整体 zstd]
[manifest: canonical JSON(由 TOML 编译) 的 zstd 帧；含嵌入的 rhai 脚本与主题资源引用]
[footer 32B: magic "MOIS" | fmt_ver u32 | flags u32 | manifest_off u64 | index_off u64 | total_crc u32]
```

运行时 `current_exe()` 读自身尾部 footer 反向定位。**签名规则写入文档**：overlay 在签名之前完成，签名是最后一步（overlay 在 PE 证书表之后，先签名后追加会破坏签名）。

### 5.3 定制化扩展模型（L1/L2/L3）

**分层原则**：80% 的定制用 L1 声明式（零代码、零风险）；需要逻辑用 L2 沙箱脚本；L3 动态 UI 留给 backlog。

**事件总线**（mo-engine 内建，所有动作执行前后发事件；GUI、日志、钩子、进度条都是订阅者）：

| 事件 | 时机 | rhai 钩子签名（返回 false 中止安装） |
|------|------|------|
| init | 解析清单后、任何 UI/动作前 | `initialize_setup(ctx) -> bool` |
| dir_chosen | 用户选定目录后 | `check_dir(ctx, dir) -> string|()`（返回字符串=错误提示） |
| before_step / after_step | 每个大阶段（files/registry/env/shortcuts/finalize）前后 | `before_step(ctx, step)` / `after_step(ctx, step)` |
| before_file / after_file | 每个文件安装前后 | `before_file(ctx, path) -> bool`（false=跳过该文件） |
| before_uninstall / after_uninstall | 卸载生命周期 | 同名函数 |
| exit | 收尾（成功或失败） | `on_exit(ctx, code)` |

**L2 宿主 API（rhai 沙箱白名单，v1 experimental）**：
- 读：`ctx.app_dir / ctx.version / ctx.silent / ctx.selected_components() / ctx.env("NAME") / ctx.reg_read(root,key,name)`
- 写：`ctx.set_progress(pct,msg) / ctx.log(msg) / ctx.message_box(text,kind) -> bool / ctx.abort(reason) / ctx.run(cmd,args)（仅 after_install 等后置事件开放，且默认弹确认；静默模式禁用交互）`
- 明确不暴露：任意文件删除、网络、进程枚举——危险操作一律走声明式清单（清单在构建期可审计）。

**安全与失败语义**：rhai 默认沙箱（无 IO），宿主只挂载白名单函数；脚本编译错误/运行时错误 = 安装失败并回滚（静默模式退出码 4）；`ctx.abort` 同理。脚本执行超时（默认 30s/钩子，清单可配）防止卡死。资源限制（运算步数上限）防死循环。
**兼容定位**：事件命名对齐 Inno（InitializeSetup→`initialize_setup`、CurStepChanged→`before_step/after_step`），Inno 脚本用户可心理映射；提供 Inno→rhai 钩子对照表文档。
**L1 外部命令钩子**：`[[hooks]]` 在事件点以子进程运行命令，注入 `MO_APP_DIR / MO_VERSION / MO_SILENT` 环境变量；非零退出码按配置 `on_error = "fail|ignore"` 处理；静默模式不弹任何框。
**L3（backlog）**：脚本声明式生成额外向导页（字段 schema → egui 渲染 → 结果注入 ctx）；以及 Lua/WASM 备选后端。

## 6. 数据流

**build**：读 TOML → serde 校验（glob 展开、常量检查、组件/事件名引用检查、rhai 脚本**编译期预编译**以提前报错）→ 收集文件（含 license/主题图片/脚本）→ zstd 压缩 → 写 overlay 到模板 exe → 输出 `{name}-{version}-setup.exe`。

**install**：解析自身 overlay → 单实例互斥体 → require_admin 检查（不足则 ShellExecuteW "runas" 重启）→ 解析命令行（Inno 风格 flags）→ `init` 钩子 → GUI 或静默 → 磁盘预检 → 引擎逐动作执行（每步发事件 + 写 install.log）→ 复制自身为 `{app}/mo-uninstall.exe` → 写 Uninstall 注册表键 → `exit` 钩子 → 完成页/运行。

**uninstall**：`mo-uninstall.exe`（= setup.exe `--uninstall`）→ `before_uninstall` 钩子 → 读注册表定位 install.log → 确认对话框（含"保留用户数据"）→ 按日志逆序删除 → `after_uninstall` 钩子 → 清 Uninstall 键 → 自删（`cmd /c timeout 1 & del` 或 MoveFileEx DELETE_ON_REBOOT 兜底）→ 退出码。

## 7. 错误处理与边界

- **回滚**：install.log 每动作一条记录，失败时逆序撤销已完成动作后报错（退出码 2）；钩子失败同样触发回滚（退出码 4 = 脚本/钩子错误）。
- 文件被占用：重试对话框（静默模式 = 重试 3 次后失败）。
- 磁盘不足：安装前预检，退出码 3。
- CRC 失败：单文件级报错重试。
- 长路径：统一 `\\?\` 前缀；路径含中文/空格全测试覆盖。
- 权限不足：提示并以管理员重启。
- 重复运行：命名互斥体 + 提示。
- 升级安装：v1 = 覆盖式（检测既有安装提示先卸载；迁移钩子场景由 L1/L2 自行实现——这正是不做"自动迁移"而做扩展点的原因）。
- 脚本超时/死循环：步数上限 + 30s 超时 → 失败回滚。
- 杀软误报：文档章节（签名建议、白名单渠道），代码侧不改行为。

## 8. 测试

- mo-core/mo-build：全平台单测（清单解析、glob、overlay 往返、损坏 footer/截断、**rhai 构建期预编译报错**）。
- mo-script：跨平台单测——每个钩子事件触发、沙箱越权调用被拒（尝试 `import`/文件 IO 失败）、超时、`ctx.abort` 语义、静默模式无交互。
- mo-engine：Windows 单测用临时目录 + HKCU 测试子键。
- 集成测试（`tests/e2e.rs`）：两套包——纯声明式包、**带主题+钩子脚本包**；各自跑 build → /VERYSILENT 装载 → 断言（文件树/快捷方式/注册表/env/钩子副作用标记文件）→ --uninstall → 断言清理 → 退出码断言。
- GUI：手动冒烟清单（文档化）；引擎/脚本与 UI 解耦使 GUI 薄到可不单测。
- CI：GitHub Actions `windows-latest`，`cargo fmt --check && cargo clippy -D warnings && cargo test`。

## 9. 里程碑

- **M1 骨架**：workspace + mo-core（清单+overlay）+ mo-build 产出带 payload 的 exe + mo-setup 静默装文件（无 GUI）。e2e 最小路径走通。
- **M2 完整安装语义 + 事件总线**：注册表/快捷方式/env/卸载/回滚/日志/退出码/Inno 风格 flags；引擎事件总线；L1 外部命令钩子。
- **M3 egui 向导 + 主题**：完整向导页流 + 组件选择 + 进度 + 错误对话框 + 中英双语 + L1 主题定制（横幅/侧图/色板/文案覆盖/页面显隐）。
- **M4 rhai 脚本扩展（L2）**：mo-script crate、全部生命周期钩子、沙箱白名单 API、超时与步数限制、Inno 钩子对照表文档。
- **M5 打磨**：自删、体积优化 profile（opt-level=z、panic=abort、strip）、AUTHORING.md（中文，含扩展指南）、自定义 exe 资源（winresource，backlog 评估）、`--sign` 透传。

## 10. 假设与风险

- 假设 D1-D3 待用户确认；D3（egui）若改为 native-win32/WebView2，仅影响 M3，其余里程碑不受牵连。
- rhai 增加约 1 MB 模板开销（已计入 <4.5 MB 预算）；若在意可后续做"无脚本瘦身模板"feature 变体。
- rhai 脚本 API v1 标 **experimental**，v1.x 内可能调整——文档明示，避免生态过早锁定错误 API。
- PE overlay 是成熟做法（Inno/NSIS 同款），但某些打包/加固工具会剥离 overlay——文档列入已知限制。
- egui 软渲染兜底覆盖无 GPU 的服务器/远程桌面场景。
- 两阶段 build.rs 是唯一"工程怪招"，可换"运行时从 target 取模板"方案（发布体验略差）。

## 11. 实施第一步

1. `git init` + 提交本计划（plan mode 期间无法提交）。
2. 按 M1 建 workspace 与 5 个 crate 骨架，先落 mo-core 的清单 schema（含 [theme]/[script]/[[hooks]]）与 overlay footer 读写 + 单测。
3. 之后按里程碑推进，每个里程碑以对应 e2e 测试通过为完成定义。
