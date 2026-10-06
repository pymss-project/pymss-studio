//! installer.toml 清单 schema。
//!
//! schema 即公共 API：字段与 installer.toml 一一对应，[Manifest::validate]
//! 在构建期完成全部静态校验（组件引用、事件名、常量名、枚举值等），
//! 使运行时拿到的清单一定是可执行的。

use crate::constants::validate_placeholders;
use crate::{Error, Result};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

/// 向导页名（theme.pages / page_order 合法取值）。
pub const KNOWN_PAGES: &[&str] = &[
    "welcome",
    "license",
    "dir",
    "components",
    "install",
    "finish",
];

/// 钩子事件名（[[hooks]] 与 L2 脚本共用的事件总线清单）。
pub const KNOWN_EVENTS: &[&str] = &[
    "init",
    "dir_chosen",
    "before_step",
    "after_step",
    "before_file",
    "after_file",
    "after_install",
    "before_uninstall",
    "after_uninstall",
    "exit",
];

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub app: App,
    #[serde(default)]
    pub options: Options,
    #[serde(default)]
    pub theme: Theme,
    #[serde(default)]
    pub components: Vec<Component>,
    #[serde(default)]
    pub files: Vec<FilesEntry>,
    #[serde(default)]
    pub shortcuts: Vec<Shortcut>,
    #[serde(default)]
    pub registry: Vec<RegistryEntry>,
    #[serde(default)]
    pub env: Vec<EnvEntry>,
    #[serde(default)]
    pub uninstall: Uninstall,
    #[serde(default)]
    pub run: Run,
    #[serde(default)]
    pub script: Option<Script>,
    #[serde(default)]
    pub hooks: Vec<Hook>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct App {
    /// 卸载注册表键名，全局唯一。
    pub id: String,
    pub name: String,
    pub version: String,
    pub publisher: String,
    /// GUI 内显示的图标（本地路径，非 exe 资源）。
    #[serde(default)]
    pub icon: Option<String>,
}

fn default_default_dir() -> String {
    "{pf}\\My App".into()
}

fn default_compression() -> String {
    "zstd19".into()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Options {
    /// true 则以管理员身份重启安装器。
    #[serde(default)]
    pub require_admin: bool,
    #[serde(default = "default_default_dir")]
    pub default_dir: String,
    /// 许可文件路径；存在时向导显示许可页。
    #[serde(default)]
    pub license: Option<String>,
    /// "store" 或 "zstd1".."zstd19"。
    #[serde(default = "default_compression")]
    pub compression: String,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            require_admin: false,
            default_dir: default_default_dir(),
            license: None,
            compression: default_compression(),
        }
    }
}

impl Options {
    pub fn compression_level(&self) -> Result<Compression> {
        Compression::parse(&self.compression).ok_or_else(|| {
            Error::ManifestInvalid(format!(
                "options.compression 非法: {:?}（应为 store 或 zstd1..zstd19）",
                self.compression
            ))
        })
    }
}

/// 压缩算法（由 options.compression 字符串解析而来）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Compression {
    Store,
    Zstd(u8),
}

impl Compression {
    pub fn parse(s: &str) -> Option<Self> {
        let s = s.trim();
        if s.eq_ignore_ascii_case("store") {
            return Some(Self::Store);
        }
        let num = s.strip_prefix("zstd").or_else(|| s.strip_prefix("zstd-"))?;
        let level = num.parse::<u8>().ok()?;
        if (1..=19).contains(&level) {
            Some(Self::Zstd(level))
        } else {
            None
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct Theme {
    /// 向导顶部横幅图片路径（打包进 overlay）。
    #[serde(default)]
    pub banner: Option<String>,
    /// 向导左侧大图路径。
    #[serde(default)]
    pub sidebar: Option<String>,
    /// 主色 "#RRGGBB"。
    #[serde(default)]
    pub accent: Option<String>,
    #[serde(default)]
    pub window: Option<WindowSize>,
    /// UI 文案覆盖：key → 文本（key 清单见文档）。
    #[serde(default)]
    pub strings: BTreeMap<String, String>,
    #[serde(default = "default_pages")]
    pub pages: Vec<String>,
    #[serde(default)]
    pub hide_pages: Vec<String>,
    /// 显式顺序覆盖；为空时按 pages 顺序。
    #[serde(default)]
    pub page_order: Vec<String>,
}

fn default_pages() -> Vec<String> {
    KNOWN_PAGES.iter().map(|s| s.to_string()).collect()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WindowSize {
    pub width: u32,
    pub height: u32,
}

fn default_true() -> bool {
    true
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Component {
    pub id: String,
    pub name: String,
    #[serde(default)]
    pub required: bool,
    /// 是否默认勾选（GUI 初始状态与静默安装的选择依据）。
    /// required = true 时恒为选中（等价 Inno Tasks 的 unchecked 反义）。
    #[serde(default = "default_true")]
    pub default: bool,
}

impl Component {
    /// 实际默认选中状态（required 蕴含选中）。
    pub fn default_selected(&self) -> bool {
        self.required || self.default
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, Default, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum Overwrite {
    #[default]
    Overwrite,
    SkipIfNewer,
    SkipIfExist,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FilesEntry {
    /// glob 模式（相对清单文件；不允许包含目录常量）。
    pub src: String,
    /// 目标目录模板，如 "{app}"。
    pub dst: String,
    #[serde(default)]
    pub component: Option<String>,
    #[serde(default)]
    pub overwrite: Overwrite,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum ShortcutDest {
    Desktop,
    StartMenu,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Shortcut {
    pub name: String,
    /// 目标，如 "{app}/myapp.exe"。
    pub target: String,
    pub dest: ShortcutDest,
    #[serde(default)]
    pub component: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum RegRoot {
    Hkcu,
    Hklm,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
pub enum RegValueType {
    #[serde(rename = "string")]
    #[default]
    String,
    #[serde(rename = "dword")]
    Dword,
    #[serde(rename = "expandSZ")]
    ExpandSz,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RegistryEntry {
    pub root: RegRoot,
    pub key: String,
    pub name: String,
    pub value: String,
    #[serde(default)]
    pub value_type: RegValueType,
    #[serde(default)]
    pub component: Option<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum EnvOp {
    Append,
    Prepend,
    Set,
    Remove,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum EnvScope {
    Machine,
    User,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EnvEntry {
    pub name: String,
    pub op: EnvOp,
    pub value: String,
    pub scope: EnvScope,
    #[serde(default)]
    pub component: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct Uninstall {
    /// 卸载时保留的用户数据（支持常量）。
    #[serde(default)]
    pub keep: Vec<String>,
    /// 控制面板卸载条目的 DisplayIcon（支持常量，如 "{app}\app.exe"）。
    #[serde(default)]
    pub display_icon: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct Run {
    /// 完成页可选运行的程序（支持常量）。
    #[serde(default)]
    pub after: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Script {
    /// 外部 rhai 脚本路径（打包进 overlay）。
    #[serde(default)]
    pub file: Option<String>,
    /// 内联脚本。
    #[serde(default)]
    pub inline: Option<String>,
    /// 单钩子执行超时（毫秒），默认 30000。
    #[serde(default)]
    pub timeout_ms: Option<u64>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, Default, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum HookOnError {
    #[default]
    Fail,
    Ignore,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Hook {
    /// 事件名（见 KNOWN_EVENTS）。
    pub event: String,
    /// 要执行的命令（支持常量）。
    pub run: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub on_error: HookOnError,
}

impl Manifest {
    /// 从 installer.toml 文本解析。
    pub fn from_toml_str(s: &str) -> Result<Self> {
        toml::from_str(s).map_err(|e| Error::ManifestParse(e.to_string()))
    }

    /// 序列化为 canonical JSON（BTreeMap 字母序，Vec 保序）。
    pub fn to_canonical_json(&self) -> Result<Vec<u8>> {
        serde_json::to_vec(self).map_err(|e| Error::Overlay(format!("manifest 序列化失败: {e}")))
    }

    /// 静态校验：构建期发现一切清单错误。
    pub fn validate(&self) -> Result<()> {
        let invalid = |msg: String| Err(Error::ManifestInvalid(msg));

        // app
        for (field, v) in [
            ("app.id", &self.app.id),
            ("app.name", &self.app.name),
            ("app.version", &self.app.version),
            ("app.publisher", &self.app.publisher),
        ] {
            if v.trim().is_empty() {
                return invalid(format!("{field} 不能为空"));
            }
        }
        for bad in ['\\', '/', ':', '*', '?', '"', '<', '>', '|'] {
            if self.app.id.contains(bad) {
                return invalid(format!("app.id 含非法字符 {bad:?}"));
            }
        }

        // options
        self.options.compression_level()?;
        if let Some(lic) = &self.options.license
            && lic.trim().is_empty()
        {
            return invalid("options.license 不能为空".into());
        }
        check_no_app_constant("options.default_dir", &self.options.default_dir)?;

        // theme
        if let Some(accent) = &self.theme.accent {
            let ok = accent.len() == 7
                && accent.starts_with('#')
                && accent[1..].bytes().all(|b| b.is_ascii_hexdigit());
            if !ok {
                return invalid(format!("theme.accent 非法: {accent:?}（应为 #RRGGBB）"));
            }
        }
        if let Some(w) = &self.theme.window
            && (w.width == 0 || w.height == 0)
        {
            return invalid("theme.window 尺寸必须大于 0".into());
        }
        check_pages("theme.pages", &self.theme.pages)?;
        check_pages("theme.hide_pages", &self.theme.hide_pages)?;
        if !self.theme.page_order.is_empty() {
            check_pages("theme.page_order", &self.theme.page_order)?;
        }
        let effective_pages: BTreeSet<&str> = self
            .theme
            .pages
            .iter()
            .map(|s| s.as_str())
            .filter(|p| !self.theme.hide_pages.iter().any(|h| h == *p))
            .collect();
        if effective_pages.contains("license") && self.options.license.is_none() {
            return invalid("pages 含 license 页但 options.license 未设置".into());
        }

        // components：id 唯一
        let mut comp_ids = BTreeSet::new();
        for c in &self.components {
            if c.id.trim().is_empty() {
                return invalid("components[].id 不能为空".into());
            }
            if !comp_ids.insert(c.id.as_str()) {
                return invalid(format!("components id 重复: {}", c.id));
            }
        }
        let comp_ok = |id: &Option<String>| -> Result<()> {
            match id {
                None => Ok(()),
                Some(id) => {
                    if comp_ids.contains(id.as_str()) {
                        Ok(())
                    } else {
                        invalid(format!("引用了未定义的组件: {id}"))
                    }
                }
            }
        };

        // files / shortcuts / registry / env
        for f in &self.files {
            if f.src.trim().is_empty() {
                return invalid("files.src 不能为空".into());
            }
            if f.src.contains('{') {
                return invalid(format!("files.src 是本地路径，不允许包含常量: {:?}", f.src));
            }
            if f.dst.trim().is_empty() {
                return invalid("files.dst 不能为空".into());
            }
            validate_placeholders(&f.dst)
                .map_err(|e| Error::ManifestInvalid(format!("files.dst 常量非法: {e}")))?;
            comp_ok(&f.component)?;
        }
        for s in &self.shortcuts {
            if s.name.trim().is_empty() {
                return invalid("shortcuts.name 不能为空".into());
            }
            if s.target.trim().is_empty() {
                return invalid("shortcuts.target 不能为空".into());
            }
            validate_placeholders(&s.target)
                .map_err(|e| Error::ManifestInvalid(format!("shortcuts.target 常量非法: {e}")))?;
            comp_ok(&s.component)?;
        }
        for r in &self.registry {
            if r.key.trim().is_empty() || r.name.trim().is_empty() {
                return invalid("registry.key / registry.name 不能为空".into());
            }
            validate_placeholders(&r.key)
                .and_then(|_| validate_placeholders(&r.value))
                .map_err(|e| Error::ManifestInvalid(format!("registry 常量非法: {e}")))?;
            comp_ok(&r.component)?;
        }
        for e in &self.env {
            if e.name.trim().is_empty() {
                return invalid("env.name 不能为空".into());
            }
            validate_placeholders(&e.value)
                .map_err(|er| Error::ManifestInvalid(format!("env.value 常量非法: {er}")))?;
        }

        // uninstall.keep / display_icon / run.after / theme 图片
        for k in &self.uninstall.keep {
            validate_placeholders(k)
                .map_err(|e| Error::ManifestInvalid(format!("uninstall.keep 常量非法: {e}")))?;
        }
        if let Some(icon) = &self.uninstall.display_icon {
            if icon.trim().is_empty() {
                return invalid("uninstall.display_icon 不能为空".into());
            }
            validate_placeholders(icon).map_err(|e| {
                Error::ManifestInvalid(format!("uninstall.display_icon 常量非法: {e}"))
            })?;
        }
        if let Some(a) = &self.run.after {
            validate_placeholders(a)
                .map_err(|e| Error::ManifestInvalid(format!("run.after 常量非法: {e}")))?;
        }
        for img in [&self.theme.banner, &self.theme.sidebar]
            .into_iter()
            .flatten()
        {
            if img.trim().is_empty() {
                return invalid("theme.banner / theme.sidebar 不能为空".into());
            }
        }

        // script：file / inline 二选一
        if let Some(sc) = &self.script {
            match (&sc.file, &sc.inline) {
                (Some(_), Some(_)) => {
                    return invalid("script.file 与 script.inline 只能二选一".into());
                }
                (None, None) => return invalid("script 需要 file 或 inline 之一".into()),
                _ => {}
            }
        }

        // hooks：事件名合法
        for h in &self.hooks {
            if !KNOWN_EVENTS.contains(&h.event.as_str()) {
                return invalid(format!(
                    "hooks.event 非法: {:?}（合法值: {}）",
                    h.event,
                    KNOWN_EVENTS.join(", ")
                ));
            }
            if h.run.trim().is_empty() {
                return invalid("hooks.run 不能为空".into());
            }
            validate_placeholders(&h.run)
                .map_err(|e| Error::ManifestInvalid(format!("hooks.run 常量非法: {e}")))?;
        }

        Ok(())
    }
}

fn check_pages(field: &str, pages: &[String]) -> Result<()> {
    let mut seen = BTreeSet::new();
    for p in pages {
        if !KNOWN_PAGES.contains(&p.as_str()) {
            return Err(Error::ManifestInvalid(format!(
                "{field} 含未知页面 {p:?}（合法值: {}）",
                KNOWN_PAGES.join(", ")
            )));
        }
        if !seen.insert(p.as_str()) {
            return Err(Error::ManifestInvalid(format!("{field} 页面重复: {p}")));
        }
    }
    Ok(())
}

/// default_dir 不允许引用 {app}（它定义 {app} 本身）。
fn check_no_app_constant(field: &str, s: &str) -> Result<()> {
    let lower = s.to_ascii_lowercase();
    if lower.contains("{app}") {
        return Err(Error::ManifestInvalid(format!(
            "{field} 不允许引用 {{app}} 常量"
        )));
    }
    validate_placeholders(s).map_err(|e| Error::ManifestInvalid(format!("{field} 常量非法: {e}")))
}

#[cfg(test)]
mod tests {
    use super::*;

    const MINIMAL: &str = r#"
[app]
id = "com.example.myapp"
name = "My App"
version = "1.2.3"
publisher = "Example Inc."

[[files]]
src = "dist/**/*"
dst = "{app}"
"#;

    #[test]
    fn parse_minimal() {
        let m = Manifest::from_toml_str(MINIMAL).unwrap();
        assert_eq!(m.app.id, "com.example.myapp");
        assert_eq!(m.files.len(), 1);
        assert_eq!(m.options.compression, "zstd19");
        assert_eq!(m.options.default_dir, "{pf}\\My App");
        assert!(m.validate().is_ok());
    }

    #[test]
    fn parse_full() {
        let m = Manifest::from_toml_str(FULL).unwrap();
        assert!(m.validate().is_ok());
        assert_eq!(m.components.len(), 2);
        assert_eq!(
            m.theme.strings.get("wizard.btn.install").unwrap(),
            "开始安装"
        );
        assert_eq!(m.hooks[0].event, "after_install");
        assert_eq!(
            m.script.as_ref().unwrap().file.as_deref(),
            Some("setup.rhai")
        );
    }

    #[test]
    fn canonical_json_roundtrip() {
        let m = Manifest::from_toml_str(FULL).unwrap();
        let json = m.to_canonical_json().unwrap();
        let m2: Manifest = serde_json::from_slice(&json).unwrap();
        assert_eq!(
            serde_json::to_vec(&m).unwrap(),
            serde_json::to_vec(&m2).unwrap()
        );
    }

    #[test]
    fn reject_unknown_field() {
        let bad = MINIMAL.to_string() + "\n[appx]\nx = 1\n";
        assert!(Manifest::from_toml_str(&bad).is_err());
    }

    #[test]
    fn reject_bad_component_ref() {
        let bad = FULL.replace("component = \"main\"", "component = \"nope\"");
        let m = Manifest::from_toml_str(&bad).unwrap();
        assert!(m.validate().is_err());
    }

    #[test]
    fn reject_bad_event() {
        let bad = FULL.replace("event = \"after_install\"", "event = \"after_lunch\"");
        let m = Manifest::from_toml_str(&bad).unwrap();
        assert!(m.validate().is_err());
    }

    #[test]
    fn reject_bad_compression() {
        let bad = MINIMAL.to_string() + "\ncompression = \"zip9\"\n";
        // 注意：compression 属于 [options]，直接拼在最后会被当成 files 段字段而报解析错，
        // 这里验证解析错误也算拒绝。
        assert!(Manifest::from_toml_str(&bad).is_err());
        let bad2 = MINIMAL.replace(
            "[[files]]",
            "[options]\ncompression = \"zip9\"\ndefault_dir = \"x\"\n\n[[files]]",
        );
        let m = Manifest::from_toml_str(&bad2).unwrap();
        assert!(m.validate().is_err());
    }

    #[test]
    fn reject_bad_constant() {
        let bad = MINIMAL.replace("dst = \"{app}\"", "dst = \"{apps}\"");
        let m = Manifest::from_toml_str(&bad).unwrap();
        assert!(m.validate().is_err());
    }

    #[test]
    fn reject_default_dir_app() {
        let bad = MINIMAL.replace(
            "[[files]]",
            "[options]\ndefault_dir = \"{app}\\\\Sub\"\n\n[[files]]",
        );
        let m = Manifest::from_toml_str(&bad).unwrap();
        assert!(m.validate().is_err());
    }

    #[test]
    fn reject_license_page_without_license() {
        let bad = MINIMAL.to_string()
            + "\n[theme]\npages = [\"welcome\", \"license\", \"dir\", \"components\", \"install\", \"finish\"]\n";
        let m = Manifest::from_toml_str(&bad).unwrap();
        assert!(m.validate().is_err());
    }

    #[test]
    fn reject_script_both() {
        let bad = FULL.replace(
            "file = \"setup.rhai\"",
            "file = \"setup.rhai\"\ninline = \"fn initialize_setup(ctx) { true }\"",
        );
        let m = Manifest::from_toml_str(&bad).unwrap();
        assert!(m.validate().is_err());
    }

    #[test]
    fn compression_parse() {
        assert_eq!(Compression::parse("store"), Some(Compression::Store));
        assert_eq!(Compression::parse("zstd19"), Some(Compression::Zstd(19)));
        assert_eq!(Compression::parse("zstd0"), None);
        assert_eq!(Compression::parse("zstd20"), None);
        assert_eq!(Compression::parse("zip"), None);
    }

    const FULL: &str = r##"
[app]
id = "com.example.myapp"
name = "My App"
version = "1.2.3"
publisher = "Example Inc."
icon = "assets/app.ico"

[options]
require_admin = false
default_dir = "{pf}\\My App"
license = "LICENSE.txt"
compression = "zstd19"

[theme]
banner = "assets/banner.png"
sidebar = "assets/side.png"
accent = "#2E7CF6"
window = { width = 660, height = 460 }
strings."wizard.btn.install" = "开始安装"
pages = ["welcome", "license", "dir", "components", "install", "finish"]
hide_pages = []
page_order = []

[[components]]
id = "main"
name = "主程序"
required = true

[[components]]
id = "docs"
name = "文档"
required = false

[[files]]
src = "dist/**/*"
dst = "{app}"
component = "main"
overwrite = "overwrite"

[[files]]
src = "docs/**/*"
dst = "{app}\\docs"
component = "docs"
overwrite = "skip-if-newer"

[[shortcuts]]
name = "My App"
target = "{app}/myapp.exe"
dest = "desktop"
component = "main"

[[registry]]
root = "hkcu"
key = "Software\\MyApp"
name = "InstallPath"
value = "{app}"
value_type = "expandSZ"
component = "docs"

[[env]]
name = "PATH"
op = "append"
value = "{app}"
scope = "machine"

[uninstall]
keep = ["{app}/settings.json"]

[run]
after = "{app}/myapp.exe"

[script]
file = "setup.rhai"

[[hooks]]
event = "after_install"
run = "{app}/tools/migrate.exe"
args = ["--from-old"]
"##;
}
