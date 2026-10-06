//! L1 外部命令钩子：[[hooks]] 在事件点以子进程运行命令。
//!
//! 注入环境变量 MO_APP_DIR / MO_VERSION / MO_SILENT；
//! 非零退出码按 on_error = fail|ignore 处理（fail 触发回滚，退出码 4）；
//! 静默模式不弹任何框，子进程输出重定向到 null。

use crate::event::{Decision, EngineCtx, Event, Subscriber};
use mo_core::manifest::Hook;
use mo_core::manifest::HookOnError;
use std::process::{Command, Stdio};

pub struct HookRunner {
    hooks: Vec<Hook>,
}

impl HookRunner {
    pub fn new(hooks: Vec<Hook>) -> Self {
        Self { hooks }
    }
}

impl Subscriber for HookRunner {
    fn id(&self) -> &str {
        "hooks"
    }

    fn on_event(&mut self, event: &Event, ctx: &EngineCtx) -> Decision {
        let name = event.name();
        for h in &self.hooks {
            if h.event != name {
                continue;
            }
            let cmd = match ctx.env.expand(&h.run) {
                Ok(c) => c,
                Err(e) => {
                    return Decision::Abort(format!("hooks.run 常量展开失败: {e}"));
                }
            };
            let mut args = Vec::with_capacity(h.args.len());
            for a in &h.args {
                match ctx.env.expand(a) {
                    Ok(v) => args.push(v),
                    Err(e) => {
                        return Decision::Abort(format!("hooks.args 常量展开失败: {e}"));
                    }
                }
            }
            let stdio = if ctx.silent {
                Stdio::null()
            } else {
                Stdio::inherit()
            };
            let status = Command::new(&cmd)
                .args(&args)
                .env("MO_APP_DIR", &ctx.app_dir)
                .env("MO_VERSION", &ctx.version)
                .env("MO_SILENT", ctx.silent.to_string())
                .stdout(stdio)
                .stderr(Stdio::null())
                .status();
            match status {
                Ok(st) if st.success() => {}
                Ok(st) => match h.on_error {
                    HookOnError::Ignore => {}
                    HookOnError::Fail => {
                        return Decision::Abort(format!(
                            "钩子 {name} 失败: {cmd}（退出码 {}）",
                            st.code().unwrap_or(-1)
                        ));
                    }
                },
                Err(e) => match h.on_error {
                    HookOnError::Ignore => {}
                    HookOnError::Fail => {
                        return Decision::Abort(format!("钩子 {name} 无法启动: {cmd} ({e})"));
                    }
                },
            }
        }
        Decision::Continue
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use mo_core::constants::ConstEnv;
    use std::collections::BTreeSet;
    use std::path::PathBuf;

    fn ctx(silent: bool) -> EngineCtx {
        EngineCtx {
            app_dir: PathBuf::from("D:\\App"),
            app_name: "App".into(),
            app_id: "com.app".into(),
            version: "1.0".into(),
            silent,
            selected_components: BTreeSet::new(),
            env: ConstEnv::empty().with("app", "D:\\App"),
        }
    }

    #[test]
    fn success_hook_passes() {
        let mut r = HookRunner::new(vec![Hook {
            event: "after_install".into(),
            run: "cmd".into(),
            args: vec!["/c".into(), "exit".into(), "0".into()],
            on_error: HookOnError::Fail,
        }]);
        assert!(matches!(
            r.on_event(&Event::AfterInstall, &ctx(true)),
            Decision::Continue
        ));
    }

    #[test]
    fn failing_hook_aborts() {
        let mut r = HookRunner::new(vec![Hook {
            event: "after_install".into(),
            run: "cmd".into(),
            args: vec!["/c".into(), "exit".into(), "3".into()],
            on_error: HookOnError::Fail,
        }]);
        match r.on_event(&Event::AfterInstall, &ctx(true)) {
            Decision::Abort(msg) => assert!(msg.contains("退出码 3")),
            Decision::Continue | Decision::SkipFile => panic!("应中止"),
        }
    }

    #[test]
    fn failing_hook_ignored() {
        let mut r = HookRunner::new(vec![Hook {
            event: "after_install".into(),
            run: "cmd".into(),
            args: vec!["/c".into(), "exit".into(), "3".into()],
            on_error: HookOnError::Ignore,
        }]);
        assert!(matches!(
            r.on_event(&Event::AfterInstall, &ctx(true)),
            Decision::Continue
        ));
    }

    #[test]
    fn event_filtered_and_env_injected() {
        let tmp = std::env::temp_dir().join(format!("mo-hook-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        std::fs::create_dir_all(&tmp).unwrap();
        let mark = tmp.join("mark.txt");
        let mut r = HookRunner::new(vec![Hook {
            event: "after_install".into(),
            run: "cmd".into(),
            args: vec![
                "/c".into(),
                format!("echo %MO_APP_DIR% %MO_VERSION% > {}", mark.display()),
            ],
            on_error: HookOnError::Fail,
        }]);
        // 不匹配的事件不触发
        assert!(matches!(
            r.on_event(&Event::Init, &ctx(true)),
            Decision::Continue
        ));
        assert!(!mark.exists());
        assert!(matches!(
            r.on_event(&Event::AfterInstall, &ctx(true)),
            Decision::Continue
        ));
        let content = std::fs::read_to_string(&mark).unwrap();
        assert!(content.contains("D:\\App"), "内容: {content}");
        assert!(content.contains("1.0"));
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn constant_expansion_in_run() {
        let mut r = HookRunner::new(vec![Hook {
            event: "init".into(),
            run: "{app}\\tools\\migrate.exe".into(),
            args: vec![],
            on_error: HookOnError::Ignore, // 文件不存在，忽略
        }]);
        // 静默：文件不存在也 Continue（ignore）
        assert!(matches!(
            r.on_event(&Event::Init, &ctx(true)),
            Decision::Continue
        ));
    }
}
