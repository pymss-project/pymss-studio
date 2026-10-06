//! Inno 风格目录常量展开。
//!
//! 支持的常量以小写在花括号中出现，例如 "{pf}\\My App"、"{app}/bin/tool.exe"。
//! 常量名大小写不敏感；未识别或不可用的常量都会报错（构建期即可校验发现）。

use crate::{Error, Result};
use std::collections::BTreeMap;
use std::path::Path;

/// 已知常量名（小写）。
pub const KNOWN_CONSTANTS: &[&str] = &[
    "app",            // 安装目标目录
    "pf",             // Program Files（64 位系统上为 64 位目录）
    "pf32",           // Program Files (x86)
    "localappdata",   // %LocalAppData%
    "appdata",        // %AppData%
    "programdata",    // %ProgramData%
    "win",            // Windows 目录
    "sys",            // System32
    "group",          // 开始菜单程序组目录（用户级）
    "userdesktop",    // 当前用户桌面
    "commondesktop",  // 公共桌面
    "userprograms",   // 用户「开始菜单-程序」目录
    "commonprograms", // 公共「开始菜单-程序」目录
    "tmp",            // 临时目录
];

/// 常量表：常量名（小写）→ 展开值。
///
/// 只登记当前环境可用的值；展开时若引用了缺失的常量则报错。
#[derive(Debug, Clone, Default)]
pub struct ConstEnv {
    values: BTreeMap<String, String>,
}

impl ConstEnv {
    /// 空常量表（测试用）。
    pub fn empty() -> Self {
        Self::default()
    }

    /// 从当前进程环境采集常量（不含 {app}）。
    ///
    /// {app} 由安装流程确定目标目录后通过 [Self::with_app] 注入。
    pub fn from_process_env() -> Self {
        let mut values = BTreeMap::new();
        fn s(v: std::ffi::OsString) -> String {
            v.to_string_lossy().into_owned()
        }
        let mut put = |k: &str, v: Option<String>| {
            if let Some(v) = v {
                values.insert(k.to_string(), v);
            }
        };
        put("pf", std::env::var_os("ProgramFiles").map(s));
        put("pf32", std::env::var_os("ProgramFiles(x86)").map(s));
        put("localappdata", std::env::var_os("LocalAppData").map(s));
        put("appdata", std::env::var_os("AppData").map(s));
        put("programdata", std::env::var_os("ProgramData").map(s));
        put("tmp", std::env::var_os("TEMP").map(s));
        if let Some(win) = std::env::var_os("windir").map(s) {
            values.insert("win".into(), win.clone());
            values.insert("sys".into(), format!("{win}\\System32"));
        }
        if let Some(public) = std::env::var_os("PUBLIC").map(s) {
            values.insert("commondesktop".into(), public + "\\Desktop");
        }
        if let Some(profile) = std::env::var_os("USERPROFILE").map(s) {
            values.insert("userdesktop".into(), profile + "\\Desktop");
        }
        if let Some(ad) = values.get("appdata").cloned() {
            values.insert(
                "userprograms".into(),
                ad + "\\Microsoft\\Windows\\Start Menu\\Programs",
            );
        }
        if let Some(pd) = values.get("programdata").cloned() {
            values.insert(
                "commonprograms".into(),
                pd + "\\Microsoft\\Windows\\Start Menu\\Programs",
            );
        }
        Self { values }
    }

    /// 注入 {app} 与 {group}（开始菜单组名默认取应用名）。
    /// userprograms 不可用时**不注入** {group}：展开处会明确报错，
    /// 避免退化为相对路径把快捷方式写进安装器工作目录。
    pub fn with_app(mut self, app_dir: &Path, group_name: &str) -> Self {
        self.values
            .insert("app".into(), app_dir.to_string_lossy().into_owned());
        if let Some(p) = self.values.get("userprograms") {
            let group_dir = p.clone() + "\\" + group_name;
            self.values.insert("group".into(), group_dir);
        }
        self
    }

    /// 注入单个常量（测试用）。
    pub fn with(mut self, name: &str, value: &str) -> Self {
        self.values
            .insert(name.to_ascii_lowercase(), value.to_string());
        self
    }

    /// 展开 {常量}。未闭合、未识别或不可用的常量都会报错。
    pub fn expand(&self, input: &str) -> Result<String> {
        let mut out = String::with_capacity(input.len());
        let mut rest = input;
        loop {
            let Some(i) = rest.find('{') else {
                out.push_str(rest);
                return Ok(out);
            };
            out.push_str(&rest[..i]);
            let after = &rest[i + 1..];
            let Some(close) = after.find('}') else {
                return Err(Error::BadConstant(format!("unclosed brace in {input:?}")));
            };
            let name = after[..close].trim().to_ascii_lowercase();
            if !KNOWN_CONSTANTS.contains(&name.as_str()) {
                return Err(Error::BadConstant(format!("unknown constant in {input:?}")));
            }
            let Some(val) = self.values.get(&name) else {
                return Err(Error::BadConstant(format!(
                    "constant not available in this context: {input:?}"
                )));
            };
            out.push_str(val);
            rest = &after[close + 1..];
        }
    }
}

/// 仅校验字符串中的常量引用是否已知（构建期清单校验用，不展开值）。
pub fn validate_placeholders(input: &str) -> Result<()> {
    let mut rest = input;
    while let Some(i) = rest.find('{') {
        let after = &rest[i + 1..];
        let Some(close) = after.find('}') else {
            return Err(Error::BadConstant(format!("unclosed brace in {input:?}")));
        };
        let name = after[..close].trim().to_ascii_lowercase();
        if !KNOWN_CONSTANTS.contains(&name.as_str()) {
            return Err(Error::BadConstant(format!("unknown constant in {input:?}")));
        }
        rest = &after[close + 1..];
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn expand_basic() {
        let env = ConstEnv::empty()
            .with("pf", "C:\\Program Files")
            .with("app", "D:\\Apps\\Demo");
        assert_eq!(
            env.expand("{pf}\\My App").unwrap(),
            "C:\\Program Files\\My App"
        );
        assert_eq!(
            env.expand("{APP}/bin/x.exe").unwrap(),
            "D:\\Apps\\Demo/bin/x.exe"
        );
        assert_eq!(env.expand("plain").unwrap(), "plain");
        assert!(env.expand("a{x}b").is_err());
    }

    #[test]
    fn expand_missing_value() {
        let env = ConstEnv::empty();
        assert!(env.expand("{pf}").is_err()); // 已知但在该环境缺失
    }

    #[test]
    fn expand_unclosed() {
        assert!(ConstEnv::empty().expand("{pf").is_err());
    }

    #[test]
    fn validate_placeholders_ok() {
        assert!(validate_placeholders("{app}\\x {localappdata}").is_ok());
        assert!(validate_placeholders("{nope}").is_err());
    }

    #[test]
    fn with_app_sets_group() {
        let env = ConstEnv::empty()
            .with("userprograms", "C:\\Users\\u\\AppData\\P\\Programs")
            .with_app(Path::new("D:\\InstDir"), "My App");
        assert_eq!(env.expand("{app}").unwrap(), "D:\\InstDir");
        assert!(env.expand("{group}").unwrap().ends_with("My App"));
    }
}
