//! 引擎事件总线。
//!
//! 所有安装/卸载动作执行前后发事件；GUI、日志、L1 钩子、进度条、
//! （M4 的）rhai 脚本都是订阅者。事件命名与 Inno 钩子可心理映射：
//! InitializeSetup -> init、CurStepChanged -> before_step/after_step。

use mo_core::constants::ConstEnv;
use std::collections::BTreeSet;
use std::path::PathBuf;

/// 安装大阶段。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Step {
    Files,
    Registry,
    Env,
    Shortcuts,
    Finalize,
}

impl Step {
    pub fn name(&self) -> &'static str {
        match self {
            Step::Files => "files",
            Step::Registry => "registry",
            Step::Env => "env",
            Step::Shortcuts => "shortcuts",
            Step::Finalize => "finalize",
        }
    }
}

/// 事件。生命周期：init -> dir_chosen -> (before_step -> ... -> after_step)*
/// -> after_install -> exit；卸载：before_uninstall -> ... -> after_uninstall -> exit。
#[derive(Debug, Clone)]
pub enum Event {
    Init,
    DirChosen { dir: PathBuf },
    BeforeStep(Step),
    AfterStep(Step),
    BeforeFile { path: String },
    AfterFile { path: String },
    AfterInstall,
    BeforeUninstall,
    AfterUninstall,
    Exit { code: i32 },
}

impl Event {
    /// 事件名（与 [[hooks]].event / rhai 钩子共用清单）。
    pub fn name(&self) -> &'static str {
        match self {
            Event::Init => "init",
            Event::DirChosen { .. } => "dir_chosen",
            Event::BeforeStep(_) => "before_step",
            Event::AfterStep(_) => "after_step",
            Event::BeforeFile { .. } => "before_file",
            Event::AfterFile { .. } => "after_file",
            Event::AfterInstall => "after_install",
            Event::BeforeUninstall => "before_uninstall",
            Event::AfterUninstall => "after_uninstall",
            Event::Exit { .. } => "exit",
        }
    }
}

/// 订阅者对事件的裁决。
#[derive(Debug, Clone)]
pub enum Decision {
    Continue,
    /// 跳过当前文件（仅 before_file 有意义；其余事件视同 Continue）。
    SkipFile,
    /// 中止安装/卸载（触发回滚）。
    Abort(String),
}

/// 事件分发的聚合结果。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Flow {
    Continue,
    SkipFile,
}

/// 引擎上下文：订阅者可见的安装期状态。
pub struct EngineCtx {
    pub app_dir: PathBuf,
    pub app_name: String,
    pub app_id: String,
    pub version: String,
    pub silent: bool,
    /// 选中的组件 id 集合（M2 无 GUI：全部组件视为选中）。
    pub selected_components: BTreeSet<String>,
    /// 常量表（含 {app}，订阅者展开命令用）。
    pub env: ConstEnv,
}

impl EngineCtx {
    /// 组件是否被选中：None（未指定组件）恒为选中。
    pub fn component_selected(&self, component: Option<&str>) -> bool {
        match component {
            None => true,
            Some(c) => self.selected_components.contains(c),
        }
    }
}

/// 事件订阅者。
pub trait Subscriber {
    fn id(&self) -> &str;
    fn on_event(&mut self, event: &Event, ctx: &EngineCtx) -> Decision;
}

/// 事件总线：按注册顺序分发；任何订阅者 Abort 即停止后续分发。
pub struct EventBus {
    subscribers: Vec<Box<dyn Subscriber>>,
}

impl EventBus {
    pub fn new() -> Self {
        Self {
            subscribers: Vec::new(),
        }
    }

    pub fn subscribe(&mut self, s: Box<dyn Subscriber>) {
        self.subscribers.push(s);
    }

    pub fn subscriber_count(&self) -> usize {
        self.subscribers.len()
    }

    /// 发事件。Err(原因) = 有订阅者要求中止；
    /// Ok(Flow::SkipFile) = 有订阅者要求跳过当前文件（仅 before_file 语义有效）。
    pub fn emit(&mut self, event: &Event, ctx: &EngineCtx) -> Result<Flow, String> {
        let mut flow = Flow::Continue;
        for s in &mut self.subscribers {
            match s.on_event(event, ctx) {
                Decision::Continue => {}
                Decision::SkipFile => flow = Flow::SkipFile,
                Decision::Abort(reason) => {
                    return Err(format!("[{}] {}", s.id(), reason));
                }
            }
        }
        Ok(flow)
    }
}

impl Default for EventBus {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Recorder {
        id: String,
        seen: std::sync::Arc<std::sync::Mutex<Vec<&'static str>>>,
    }

    impl Subscriber for Recorder {
        fn id(&self) -> &str {
            &self.id
        }
        fn on_event(&mut self, event: &Event, _ctx: &EngineCtx) -> Decision {
            self.seen.lock().unwrap().push(event.name());
            Decision::Continue
        }
    }

    struct Aborter;

    impl Subscriber for Aborter {
        fn id(&self) -> &str {
            "aborter"
        }
        fn on_event(&mut self, event: &Event, _ctx: &EngineCtx) -> Decision {
            if event.name() == "before_file" {
                Decision::Abort("不要这个文件".into())
            } else {
                Decision::Continue
            }
        }
    }

    fn ctx() -> EngineCtx {
        EngineCtx {
            app_dir: PathBuf::from("D:\\App"),
            app_name: "App".into(),
            app_id: "com.app".into(),
            version: "1.0".into(),
            silent: true,
            selected_components: BTreeSet::from(["main".to_string()]),
            env: ConstEnv::empty().with("app", "D:\\App"),
        }
    }

    #[test]
    fn emit_order_and_names() {
        let seen = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let mut bus = EventBus::new();
        bus.subscribe(Box::new(Recorder {
            id: "rec".into(),
            seen: seen.clone(),
        }));
        assert!(bus.emit(&Event::Init, &ctx()).is_ok());
        assert!(
            bus.emit(&Event::BeforeFile { path: "x".into() }, &ctx())
                .is_ok()
        );
        assert_eq!(*seen.lock().unwrap(), vec!["init", "before_file"]);
    }

    #[test]
    fn abort_stops_propagation() {
        let seen = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let mut bus = EventBus::new();
        bus.subscribe(Box::new(Aborter));
        bus.subscribe(Box::new(Recorder {
            id: "rec".into(),
            seen: seen.clone(),
        }));
        let err = bus
            .emit(&Event::BeforeFile { path: "x".into() }, &ctx())
            .unwrap_err();
        assert!(err.contains("aborter"));
        assert!(seen.lock().unwrap().is_empty()); // 后注册的订阅者未收到
    }

    #[test]
    fn step_names() {
        assert_eq!(Step::Files.name(), "files");
        assert_eq!(Step::Finalize.name(), "finalize");
    }

    #[test]
    fn component_selected() {
        let c = ctx();
        assert!(c.component_selected(None));
        assert!(c.component_selected(Some("main")));
        assert!(!c.component_selected(Some("docs")));
    }
}
