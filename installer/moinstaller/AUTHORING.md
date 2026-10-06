# AUTHORING.md — MoInstaller 清单编写指南

> 开发者写一份 `installer.toml` → 运行 `mo build` → 得到单文件 setup.exe。
> 本文是作者侧唯一参考：字段、常量、事件、脚本 API、签名与已知限制。

## 目录

1. 快速开始  2. 命令行  3. 清单参考  4. 目录常量  5. 静默安装与退出码
6. 定制三层模型  7. L1 主题  8. L1 外部命令钩子  9. L2 rhai 脚本  10. Inno 对照表
11. 代码签名  12. 已知限制  13. GUI 手动冒烟清单  14. 开发备注

## 快速开始

```toml
# installer.toml
[app]
id = "com.example.myapp"      # 卸载注册表键名，全局唯一
name = "My App"
version = "1.2.3"
publisher = "Example Inc."

[options]
default_dir = '{pf}\My App'  # 单引号 TOML 字面串避免转义

[[files]]
src = "dist/**/*"             # glob，相对本文件
dst = "{app}"
```

```powershell
mo build installer.toml        # 产出 My App-1.2.3-setup.exe
```

双击 setup.exe 打开向导；`/VERYSILENT /DIR=D:\x` 静默安装；控制面板卸载。

## 命令行

```
mo build <installer.toml> [--template <mo-setup.exe>] [-o <out.exe>] [--sign "<命令>"]
```

- `--template`：默认使用内置模板（开发期见"开发备注"）
- `--sign`：构建完成后执行签名命令，`{out}` 占位替换为输出路径
  （例：`--sign "signtool sign /f cert.pfx /tr http://ts ... {out}"`）

## 清单参考

### [app]

| 字段 | 必填 | 说明 |
|------|------|------|
| id | 是 | 卸载键名；禁止 \ / : * ? " < > | |
| name / version / publisher | 是 | 展示信息 |
| icon | 否 | 预留（GUI 内显示） |

### [options]

| 字段 | 默认 | 说明 |
|------|------|------|
| require_admin | false | true 时以管理员重启（UAC） |
| default_dir | {pf}\My App | 不允许引用 {app} |
| license | 无 | 许可文件路径；存在时向导显示许可页 |
| compression | zstd19 | store 或 zstd1..zstd19 |

### [[files]]

| 字段 | 必填 | 说明 |
|------|------|------|
| src | 是 | glob（相对清单）；不允许常量 |
| dst | 是 | 目标目录模板（支持常量） |
| component | 否 | 组件 id 引用 |
| overwrite | overwrite | overwrite / skip-if-newer / skip-if-exist |

glob 相对路径以**静态前缀目录**为基准：`dist/**/*` 匹配 dist/bin/app.exe → 安装为 {app}/bin/app.exe。

### [[components]]

`id`（唯一）、`name`、`required`（锁定勾选）。无 components 时单组件全装。

### [[shortcuts]]

`name`、`target`（支持常量）、`dest`（desktop / start-menu）、`component`。
start-menu 落在 {group} = 开始菜单\程序\<应用名>。

### [[registry]]

`root`（hkcu / hklm）、`key`、`name`、`value`（支持常量）、
`value_type`（string / dword / expandSZ）、`component`。
卸载时精确还原（记录安装前旧值）。

### [[env]]

`name`、`op`（append / prepend / set / remove）、`value`、`scope`（user / machine）、`component`。
修改后广播 WM_SETTINGCHANGE；PATH 以 REG_EXPAND_SZ 存储。

### [uninstall]

`keep = ["{app}/settings.json"]`：卸载保留的用户数据（支持常量）。

### [run]

`after = "{app}/myapp.exe"`：完成页"运行"勾选项。

### [theme]

`banner` / `sidebar`（图片路径，打包进包）、`accent`（#RRGGBB）、
`window = { width = 660, height = 460 }`、
`strings."wizard.btn.install" = "..."`（任意文案覆盖）、
`pages = [...] / hide_pages = [...] / page_order = [...]`。

### [script] / [[hooks]]

见"定制三层模型"。

## 目录常量

`{app} {pf} {pf32} {localappdata} {appdata} {programdata} {win} {sys}` `{group} {userdesktop} {commondesktop} {userprograms} {commonprograms} {tmp}`

大小写不敏感；未识别常量在构建期报错。

## 静默安装与退出码

| 参数 | 说明 |
|------|------|
| /SILENT /VERYSILENT | 控制台静默安装（跳过全部向导页） |
| /DIR=x | 覆盖安装目录 |
| /GROUP=x | 覆盖开始菜单组名 |
| /SUPPRESSMSGBOXES /NORESTART | 兼容占位（接受并忽略） |

| 退出码 | 含义 |
|--------|------|
| 0 | 成功 |
| 1 | 致命错误（包损坏等） |
| 2 | 安装失败（已回滚） |
| 3 | 磁盘空间不足 |
| 4 | 钩子/脚本错误（已回滚） |

卸载：运行 `{app}\mo-uninstall.exe`（/VERYSILENT 静默卸载）。

## 定制三层模型

- **L1 声明式**（零代码零风险）：主题 + 外部命令钩子
- **L2 沙箱脚本**：rhai 事件钩子（experimental）
- **L3 动态 UI**：backlog（脚本生成向导页、Lua/WASM 后端）

## L1 主题

```toml
[theme]
banner = "assets/banner.png"       # 向导顶部横幅
sidebar = "assets/side.png"        # 左侧大图
accent = "#2E7CF6"                 # 按钮/进度/高亮色
window = { width = 660, height = 460 }

[theme.strings]
"wizard.btn.install" = "马上安装"   # 全部文案可覆盖
"wizard.welcome.text" = "欢迎使用我的应用"
```

内置语言：中/英（按系统区域自动选择；theme.strings 优先级最高）。
常用 key：wizard.title / wizard.welcome.* / wizard.license.* / wizard.dir.* /
wizard.components.* / wizard.install.* / wizard.finish.* / wizard.btn.*
（完整清单见 crates/mo-setup/src/strings.rs）。

页面显隐：`pages = ["welcome", "license", "dir", "components", "install", "finish"]`
（license 页需 options.license；components 页需 [[components]]）。

## L1 外部命令钩子

```toml
[[hooks]]
event = "after_install"
run = "{app}/tools/migrate.exe"   # 支持常量
args = ["--from-old"]
on_error = "fail"                  # fail（默认，退出码 4+回滚）| ignore
```

注入环境变量：MO_APP_DIR / MO_VERSION / MO_SILENT。
静默模式下子进程输出被丢弃且不弹任何框。

事件清单：init / dir_chosen / before_step / after_step / before_file /
after_file / after_install / before_uninstall / after_uninstall / exit。

## L2 rhai 脚本（experimental）

```toml
[script]
file = "setup.rhai"      # 外部文件（打包进 setup.exe，构建期预编译）
# 或 inline = '''fn initialize_setup(ctx) { true }'''
timeout_ms = 30000       # 单钩子超时（默认 30s）
```

```rhai
// setup.rhai —— 只写需要的钩子，未定义的自动跳过
fn initialize_setup(ctx) {
    // 返回 false 中止安装
    ctx.reg_read("hkcu", "Software\\MyApp", "Installed") == ""
}
fn check_dir(ctx, dir) {
    // 返回非空字符串 = 目录错误提示
    if dir.contains("临时") { "请不要装在临时目录" } else { "" }
}
fn before_file(ctx, path) {
    // 返回 false 跳过该文件
    !path.contains("debug.pdb")
}
fn before_step(ctx, step) { ctx.log("进入阶段 " + step); }
fn after_install(ctx) {
    // 后置事件才能 ctx.run
    if !ctx.silent { ctx.run("cmd", ["/c", "echo ok"]); }
}
fn on_exit(ctx, code) { ctx.log("退出码 " + code); }
```

ctx API（v1 experimental，可能调整）：

| 访问 | 说明 |
|------|------|
| ctx.app_dir / app_name / app_id / version / silent | 属性 |
| ctx.selected_components() | 选中组件 id 数组 |
| ctx.env("NAME") | 进程环境变量 |
| ctx.reg_read(root, key, name) | 注册表只读（hkcu/hklm） |
| ctx.log(msg) | 写安装日志 |
| ctx.set_progress(pct, msg) | 更新进度 |
| ctx.message_box(text, kind) | 弹窗（静默模式直接返回 true） |
| ctx.abort(reason) | 中止安装（退出码 4 + 回滚） |
| ctx.run(cmd, args) | 子进程（仅 after_install / after_uninstall / exit） |

**沙箱**：rhai 默认无文件/网络 IO；任意删除、网络、进程枚举一律不暴露——
需要它们就写进声明式清单（构建期可审计）。
死循环由步数上限（10M ops）与 timeout_ms 兜底；触发即安装失败回滚。

## Inno Setup 对照表

| Inno | MoInstaller |
|------|-------------|
| [Setup] AppId | [app] id |
| DefaultDirName | [options] default_dir |
| PrivilegesRequired=admin | require_admin = true |
| OutputBaseFilename | mo build -o（默认 name-version-setup.exe） |
| LZMA2 / zip | compression = zstd19 / store |
| [Files] Source/DestDir | [[files]] src / dst |
| [Dirs] | 自动（按文件路径创建） |
| [Registry] | [[registry]] |
| [Components] | [[components]] |
| [Icons] | [[shortcuts]] |
| UninstallFilesDir | 自动 {app}\mo-uninstall.exe |
| [Code] InitializeSetup | fn initialize_setup(ctx) |
| [Code] CurStepChanged | fn before_step / after_step |
| [Code] ShouldProcessEntry | fn before_file |
| [Code] InitializeUninstall | fn before_uninstall |
| [Code] DeinitializeSetup | fn on_exit |
| /VERYSILENT /DIR= | 相同 |
| Pascal Script | rhai（沙箱子集） |

## 代码签名

- 流程：`mo build ... --sign "signtool sign /fd SHA256 /tr <TSA> {out}"`
- **顺序契约**：overlay 追加在签名之前完成，签名是最后一步。
  Authenticode 把签名插入 PE 证书表（overlay 之前），footer 从文件尾定位不受影响。
- 未签名安装器可能被 SmartScreen/杀软提示；分发给最终用户前建议签名。

## 已知限制

- 某些打包/加固工具会剥离 PE overlay——本包格式不可用于此类壳之后。
- 脚本 API 为 experimental，v1.x 内可能调整。
- 升级安装 = 覆盖式；迁移逻辑用 L1/L2 钩子自行实现。
- 杀软误报：签名 + 白名单渠道（代码侧行为不变）。
- 双架构合并包 / 安装包加密 / 自动更新：不在 v1 范围。

## GUI 手动冒烟清单

自动化测试覆盖静默全链路；GUI 需人工冒烟：

1. 无参数运行 setup.exe → 向导打开，横幅/侧图/主题色正确
2. 许可页：未勾选接受时"下一步"置灰；勾选后可继续
3. 目录页：浏览按钮弹原生对话框；显示可用空间；中文/空格路径可用
4. 组件页：required 组件置灰锁定；取消勾选可选组件后安装只含所选
5. 安装页：进度条推进、当前文件滚动
6. 故意选只读目录 → 失败页显示错误（已回滚）
7. 完成页：勾选"运行"→ 应用启动
8. 控制面板"应用和功能"出现条目 → 卸载向导（保留数据勾选）→ 全清理
9. 再次安装（覆盖）→ 正常；mo-uninstall.exe 在 {app}
10. /SILENT 与 /VERYSILENT 均无窗口完成

## 开发备注

- **两阶段模板构建**：`cargo build --workspace` 跑两次后 mo-build 内置模板才固化
  （第一次构建时模板尚不存在，使用占位并警告）。
- 测试：`cargo test --workspace`（含 e2e：build→静默装→断言→卸载→断言）。
- 包格式：footer 32B（"MOIS"）从文件尾定位；fmt_ver 2；
  total_crc 覆盖索引+manifest（刻意不含 PE，兼容 Authenticode）。
- release 体积：模板（含 egui+rhai+zstd）约 4.3 MB < 4.5 MB 预算。
