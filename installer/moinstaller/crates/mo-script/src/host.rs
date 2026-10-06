//! 宿主 API：脚本沙箱内可调用的白名单能力。
//!
//! mo-script 只定义接口与测试实现（FakeHost）；真实实现（注册表/弹窗/
//! 运行进程）由 mo-setup 注入，避免本 crate 依赖平台代码。

/// 脚本可用的宿主能力（全部白名单，危险操作一律走声明式清单）。
pub trait HostApi: Send + Sync {
    /// 读注册表字符串值（root: "hkcu"|"hklm"）。
    fn reg_read(&self, root: &str, key: &str, name: &str) -> Option<String>;
    /// 写安装日志。
    fn log(&self, msg: &str);
    /// 更新进度（0.0-100.0 + 消息）。
    fn set_progress(&self, pct: f64, msg: &str);
    /// 弹窗（kind: "info"|"warn"|"error"|"confirm"）。静默模式由实现方直接返回 true。
    fn message_box(&self, text: &str, kind: &str) -> bool;
    /// 运行子进程，返回退出码（被拒绝返回 None）。
    fn run(&self, cmd: &str, args: &[String]) -> Option<i32>;
}

/// 测试/预览用假实现（全部记录、无副作用）。
#[derive(Default)]
pub struct FakeHost {
    pub logs: std::sync::Mutex<Vec<String>>,
    pub progresses: std::sync::Mutex<Vec<(f64, String)>>,
    pub run_results: std::sync::Mutex<Vec<String>>,
}

impl HostApi for FakeHost {
    fn reg_read(&self, _root: &str, _key: &str, _name: &str) -> Option<String> {
        None
    }
    fn log(&self, msg: &str) {
        self.logs.lock().unwrap().push(msg.to_string());
    }
    fn set_progress(&self, pct: f64, msg: &str) {
        self.progresses.lock().unwrap().push((pct, msg.to_string()));
    }
    fn message_box(&self, _text: &str, _kind: &str) -> bool {
        true
    }
    fn run(&self, cmd: &str, args: &[String]) -> Option<i32> {
        self.run_results
            .lock()
            .unwrap()
            .push(format!("{cmd} {}", args.join(" ")));
        Some(0)
    }
}
