//! 内置 UI 字符串表（中英双语）；theme.strings 覆盖优先。
//!
//! key 清单即公共文案 API（theme.strings 可覆盖任意 key）。

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Lang {
    Zh,
    En,
}

impl Lang {
    /// 按系统首选语言猜默认语言。
    pub fn detect() -> Self {
        // Windows 中文系统 UI 语言或区域以 zh 开头
        match std::env::var("LANG")
            .ok()
            .or_else(|| std::env::var("UI_LANGUAGE").ok())
        {
            Some(v) if v.to_ascii_lowercase().starts_with("zh") => Lang::Zh,
            _ => match windows_ui_language() {
                Some(v) if v.starts_with("zh") => Lang::Zh,
                _ => Lang::En,
            },
        }
    }
}

fn windows_ui_language() -> Option<String> {
    // GetUserDefaultUILanguage 不在依赖里；用安装器约定：中文 Windows 的
    // ProgramFiles 等环境不变，因此退化为检查系统区域（够用且零依赖）。
    std::env::var("OS").ok().and_then(|_| reg_query_lang())
}

fn reg_query_lang() -> Option<String> {
    // HKCU\Control Panel\International\LocaleName（zh-CN 等）
    let out = std::process::Command::new("reg")
        .args([
            "query",
            r"HKCU\Control Panel\International",
            "/v",
            "LocaleName",
        ])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&out.stdout);
    text.lines()
        .find(|l| l.contains("LocaleName"))
        .and_then(|l| l.split_whitespace().last().map(|s| s.to_string()))
}

/// 内置字符串。key -> (zh, en)
pub fn builtin(key: &str, lang: Lang) -> Option<&'static str> {
    let (zh, en): (&str, &str) = match key {
        "wizard.title" => ("安装向导", "Setup Wizard"),
        "wizard.welcome.title" => ("欢迎使用", "Welcome"),
        "wizard.welcome.text" => (
            "本向导将引导您完成安装。",
            "This wizard will guide you through the installation.",
        ),
        "wizard.license.title" => ("许可协议", "License Agreement"),
        "wizard.license.prompt" => (
            "请阅读以下许可协议。继续安装前您必须接受该协议。",
            "Please read the following license. You must accept it to continue.",
        ),
        "wizard.license.accept" => ("我接受协议", "I accept the agreement"),
        "wizard.dir.title" => ("选择安装位置", "Choose Install Location"),
        "wizard.dir.prompt" => (
            "选择安装程序安装文件的目录，然后点击「安装」。",
            "Choose the folder to install, then click Install.",
        ),
        "wizard.dir.needspace" => ("所需空间", "Space required"),
        "wizard.dir.freespace" => ("可用空间", "Space available"),
        "wizard.dir.browse" => ("浏览...", "Browse..."),
        "wizard.components.title" => ("选择组件", "Choose Components"),
        "wizard.components.prompt" => ("选择要安装的功能。", "Select features to install."),
        "wizard.components.size" => ("大小", "Size"),
        "wizard.install.title" => ("正在安装", "Installing"),
        "wizard.install.wait" => (
            "请稍候，正在安装文件...",
            "Please wait while files are installed...",
        ),
        "wizard.finish.title" => ("安装完成", "Installation Complete"),
        "wizard.finish.text" => ("安装已成功完成。", "Setup has finished installing."),
        "wizard.finish.failed" => ("安装失败（已回滚）。", "Installation failed (rolled back)."),
        "wizard.finish.run" => ("运行", "Run"),
        "wizard.uninstall.title" => ("卸载", "Uninstall"),
        "wizard.uninstall.confirm" => (
            "确认要完全卸载此应用及其所有组件吗？",
            "Completely remove this application and all its components?",
        ),
        "wizard.uninstall.keepdata" => ("保留用户数据", "Keep user data"),
        "wizard.uninstall.done" => ("卸载完成。", "Uninstall complete."),
        "wizard.btn.back" => ("< 上一步", "< Back"),
        "wizard.btn.next" => ("下一步 >", "Next >"),
        "wizard.btn.install" => ("安装", "Install"),
        "wizard.btn.finish" => ("完成", "Finish"),
        "wizard.btn.cancel" => ("取消", "Cancel"),
        "wizard.btn.close" => ("关闭", "Close"),
        "wizard.btn.uninstall" => ("卸载", "Uninstall"),
        "error.dir.required" => ("请选择安装目录。", "Please choose an install folder."),
        "error.license.required" => (
            "必须接受许可协议才能继续。",
            "You must accept the license to continue.",
        ),
        _ => return None,
    };
    Some(match lang {
        Lang::Zh => zh,
        Lang::En => en,
    })
}
