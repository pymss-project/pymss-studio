//! rhai 脚本宿主：事件 -> 脚本函数映射 + 沙箱白名单 API + 超时/步数限制。
//!
//! 事件与 Inno 钩子的心理映射：
//! InitializeSetup -> initialize_setup、CurStepChanged -> before_step/after_step。
//!
//! 钩子返回值语义：
//! - initialize_setup(ctx) -> bool：false 中止安装（回滚）
//! - check_dir(ctx, dir) -> string：非空字符串 = 目录错误提示（中止）
//! - before_file(ctx, path) -> bool：false 跳过该文件
//! - 其余钩子忽略返回值
//!
//! 失败语义：编译错误在构建期即报；运行时错误/超时/步数超限/ctx.abort
//! 均以钩子错误中止（退出码 4，回滚）。未定义的钩子函数静默跳过。

use crate::host::HostApi;
use mo_core::manifest::Manifest;
use mo_engine::event::{Decision, EngineCtx, Event, Subscriber};
use rhai::{AST, Dynamic, Engine, EvalAltResult, Position, Scope};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

/// 脚本执行限制。
#[derive(Debug, Clone, Copy)]
pub struct ScriptOptions {
    /// 单钩子执行超时（毫秒）。0 = 禁用。
    pub timeout_ms: u64,
    /// 单钩子最大运算步数（防死循环）。0 = 禁用。
    pub max_operations: u64,
}

impl Default for ScriptOptions {
    fn default() -> Self {
        Self {
            timeout_ms: 30_000,
            max_operations: 10_000_000,
        }
    }
}

/// 传给脚本的 ctx 对象（rhai 自定义类型）。
#[derive(Clone)]
pub struct ScriptCtx {
    inner: Arc<CtxInner>,
}

struct CtxInner {
    app_dir: Mutex<String>,
    app_name: String,
    app_id: String,
    version: String,
    silent: bool,
    components: Vec<String>,
    host: Mutex<Box<dyn HostApi>>,
    run_allowed: AtomicBool,
}

impl ScriptCtx {
    fn host<R>(&self, f: impl FnOnce(&Box<dyn HostApi>) -> R) -> R {
        f(&self.inner.host.lock().unwrap())
    }

    fn app_dir_str(&self) -> String {
        self.inner.app_dir.lock().unwrap().clone()
    }

    /// 规范化路径用于前缀比较（小写、正斜杠归一为反斜杠）。
    fn norm_for_cmp(p: &str) -> String {
        p.to_ascii_lowercase().replace('/', "\\")
    }

    /// 写/删护栏：目标必须严格位于 {app} 目录内（防止脚本误伤系统目录）。
    /// 这是作者脚本的失误护栏，不是安全边界。
    fn guard_app(&self, p: &str) -> bool {
        let app = Self::norm_for_cmp(&self.app_dir_str());
        if app.is_empty() {
            return false;
        }
        let t = Self::norm_for_cmp(p);
        t.starts_with(&app) && t.len() > app.len() && t.as_bytes()[app.len()] == b'\\'
    }
}

/// 沙箱文件系统 API 的 ctx 侧实现（探测不限路径，写/删限 {app} 内）。
mod fs_api {
    use super::ScriptCtx;

    impl ScriptCtx {
        pub fn file_exists(&self, path: &str) -> bool {
            std::path::Path::new(path).is_file()
        }
        pub fn dir_exists(&self, path: &str) -> bool {
            std::path::Path::new(path).is_dir()
        }
        pub fn write_text(&self, path: &str, content: &str) -> bool {
            if !self.guard_app(path) {
                return false;
            }
            std::fs::write(path, content).is_ok()
        }
        pub fn delete_file(&self, path: &str) -> bool {
            if !self.guard_app(path) {
                return false;
            }
            std::fs::remove_file(path).is_ok()
        }
        pub fn delete_dir(&self, path: &str) -> bool {
            if !self.guard_app(path) {
                return false;
            }
            std::fs::remove_dir_all(path).is_ok()
        }
    }
}

/// 构建期预编译检查：语法/编译错误在此暴露。
pub fn compile_check(source: &str) -> Result<(), String> {
    Engine::new()
        .compile(source)
        .map(|_| ())
        .map_err(|e| format!("rhai 脚本编译失败: {e}"))
}

/// rhai 事件宿主（事件总线订阅者）。
pub struct ScriptHost {
    engine: std::rc::Rc<Engine>,
    ast: AST,
    ctx: ScriptCtx,
    deadline: Arc<Mutex<Option<Instant>>>,
    opts: ScriptOptions,
}

impl ScriptHost {
    /// 从清单构造。silent 与选中组件来自引擎上下文。
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        manifest: &Manifest,
        source: &str,
        silent: bool,
        selected_components: &[String],
        host: Box<dyn HostApi>,
        opts: ScriptOptions,
    ) -> Result<Self, String> {
        let mut engine = Engine::new();
        let ast = engine
            .compile(source)
            .map_err(|e| format!("rhai 脚本编译失败: {e}"))?;

        if opts.max_operations > 0 {
            engine.set_max_operations(opts.max_operations);
        }
        engine.set_max_call_levels(64);
        engine.set_max_string_size(1_000_000);
        engine.set_max_array_size(10_000);

        let ctx = ScriptCtx {
            inner: Arc::new(CtxInner {
                app_dir: Mutex::new(String::new()),
                app_name: manifest.app.name.clone(),
                app_id: manifest.app.id.clone(),
                version: manifest.app.version.clone(),
                silent,
                components: selected_components.to_vec(),
                host: Mutex::new(host),
                run_allowed: AtomicBool::new(false),
            }),
        };

        // ---- 白名单属性与方法 ----
        engine.register_get("app_dir", |c: &mut ScriptCtx| {
            c.inner.app_dir.lock().unwrap().clone()
        });
        engine.register_get("app_name", |c: &mut ScriptCtx| c.inner.app_name.clone());
        engine.register_get("app_id", |c: &mut ScriptCtx| c.inner.app_id.clone());
        engine.register_get("version", |c: &mut ScriptCtx| c.inner.version.clone());
        engine.register_get("silent", |c: &mut ScriptCtx| c.inner.silent);
        engine.register_fn("selected_components", |c: &mut ScriptCtx| {
            c.inner.components.clone()
        });
        engine.register_fn("env", |_c: &mut ScriptCtx, name: &str| {
            std::env::var(name).unwrap_or_default()
        });
        engine.register_fn(
            "reg_read",
            |c: &mut ScriptCtx, root: &str, key: &str, name: &str| {
                c.host(|h| h.reg_read(root, key, name)).unwrap_or_default()
            },
        );
        engine.register_fn("log", |c: &mut ScriptCtx, msg: &str| {
            c.host(|h| h.log(msg));
        });
        // ---- 沙箱文件系统（探测不限路径；写/删限 {app} 内）----
        engine.register_fn("file_exists", |c: &mut ScriptCtx, path: &str| {
            c.file_exists(path)
        });
        engine.register_fn("dir_exists", |c: &mut ScriptCtx, path: &str| {
            c.dir_exists(path)
        });
        engine.register_fn(
            "write_text",
            |c: &mut ScriptCtx, path: &str, content: &str| c.write_text(path, content),
        );
        engine.register_fn("delete_file", |c: &mut ScriptCtx, path: &str| {
            c.delete_file(path)
        });
        engine.register_fn("delete_dir", |c: &mut ScriptCtx, path: &str| {
            c.delete_dir(path)
        });
        engine.register_fn("set_progress", |c: &mut ScriptCtx, pct: f64, msg: &str| {
            c.host(|h| h.set_progress(pct, msg));
        });
        engine.register_fn(
            "message_box",
            |c: &mut ScriptCtx, text: &str, kind: &str| c.host(|h| h.message_box(text, kind)),
        );
        engine.register_fn(
            "abort",
            |_c: &mut ScriptCtx, reason: &str| -> Result<(), Box<EvalAltResult>> {
                Err(Box::new(EvalAltResult::ErrorRuntime(
                    Dynamic::from(format!("script-abort: {reason}")),
                    Position::NONE,
                )))
            },
        );
        engine.register_fn(
            "run",
            |c: &mut ScriptCtx, cmd: &str, args: rhai::Array| -> Result<i64, Box<EvalAltResult>> {
                if !c.inner.run_allowed.load(Ordering::Relaxed) {
                    return Err(Box::new(EvalAltResult::ErrorRuntime(
                        Dynamic::from(
                            "ctx.run 仅在 after_install / after_uninstall / exit 等后置事件开放"
                                .to_string(),
                        ),
                        Position::NONE,
                    )));
                }
                let args: Vec<String> = args.iter().map(|a| a.to_string()).collect();
                match c.host(|h| h.run(cmd, &args)) {
                    Some(code) => Ok(code as i64),
                    None => Err(Box::new(EvalAltResult::ErrorRuntime(
                        Dynamic::from(format!("ctx.run 无法启动: {cmd}")),
                        Position::NONE,
                    ))),
                }
            },
        );

        // 超时：on_progress 检查 deadline（每次 call_fn 前设置）
        let deadline = Arc::new(Mutex::new(None::<Instant>));
        let dl = deadline.clone();
        let timeout_ms = opts.timeout_ms;
        engine.on_progress(move |_ops| {
            if timeout_ms == 0 {
                return None;
            }
            if let Some(d) = *dl.lock().unwrap()
                && Instant::now() >= d
            {
                return Some(Dynamic::from("script-timeout".to_string()));
            }
            None
        });

        Ok(Self {
            engine: std::rc::Rc::new(engine),
            ast,
            ctx,
            deadline,
            opts,
        })
    }

    /// 事件名 -> rhai 函数名。
    fn fn_name(event: &Event) -> &'static str {
        match event.name() {
            "init" => "initialize_setup",
            "dir_chosen" => "check_dir",
            "before_step" => "before_step",
            "after_step" => "after_step",
            "before_file" => "before_file",
            "after_file" => "after_file",
            "after_install" => "after_install",
            "before_uninstall" => "before_uninstall",
            "after_uninstall" => "after_uninstall",
            "exit" => "on_exit",
            other => other,
        }
    }

    /// 后置事件（允许 ctx.run）。
    fn is_post_event(event: &Event) -> bool {
        matches!(event.name(), "after_install" | "after_uninstall" | "exit")
    }

    fn ast_has_fn(&self, name: &str) -> bool {
        self.ast.iter_functions().any(|f| f.name == name)
    }

    fn call(&self, fn_name: &str, extra_args: Vec<Dynamic>) -> (Option<Dynamic>, Option<String>) {
        let mut scope = Scope::new();
        let mut args = vec![Dynamic::from(self.ctx.clone())];
        args.extend(extra_args);
        if self.opts.timeout_ms > 0 {
            *self.deadline.lock().unwrap() =
                Some(Instant::now() + std::time::Duration::from_millis(self.opts.timeout_ms));
        }
        let result: Result<Dynamic, Box<EvalAltResult>> =
            self.engine.call_fn(&mut scope, &self.ast, fn_name, args);
        *self.deadline.lock().unwrap() = None;
        match result {
            Ok(v) => (Some(v), None),
            Err(e) => (None, Some(e.to_string())),
        }
    }
}

impl Subscriber for ScriptHost {
    fn id(&self) -> &str {
        "script"
    }

    fn on_event(&mut self, event: &Event, ectx: &EngineCtx) -> Decision {
        let fn_name = Self::fn_name(event);
        if !self.ast_has_fn(fn_name) {
            return Decision::Continue;
        }

        *self.ctx.inner.app_dir.lock().unwrap() = ectx.app_dir.to_string_lossy().into_owned();
        self.ctx
            .inner
            .run_allowed
            .store(Self::is_post_event(event), Ordering::Relaxed);

        let extra: Vec<Dynamic> = match event {
            Event::DirChosen { dir } => vec![Dynamic::from(dir.to_string_lossy().into_owned())],
            Event::BeforeStep(s) | Event::AfterStep(s) => vec![Dynamic::from(s.name().to_string())],
            Event::BeforeFile { path } | Event::AfterFile { path } => {
                vec![Dynamic::from(path.clone())]
            }
            Event::Exit { code } => vec![Dynamic::from(*code)],
            _ => Vec::new(),
        };

        let (ret, err) = self.call(fn_name, extra);
        if let Some(e) = err {
            return Decision::Abort(format!("脚本钩子 {fn_name} 失败: {e}"));
        }
        let ret = ret.unwrap_or(Dynamic::UNIT);

        match event {
            Event::Init => {
                if let Some(false) = ret.clone().try_cast::<bool>() {
                    return Decision::Abort("initialize_setup 返回 false".into());
                }
            }
            Event::DirChosen { .. } => {
                if let Some(s) = ret.clone().try_cast::<String>()
                    && !s.is_empty()
                {
                    return Decision::Abort(format!("check_dir: {s}"));
                }
            }
            Event::BeforeFile { .. } => {
                if let Some(false) = ret.clone().try_cast::<bool>() {
                    return Decision::SkipFile;
                }
            }
            _ => {}
        }
        Decision::Continue
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::host::FakeHost;
    use mo_core::constants::ConstEnv;
    use mo_engine::event::Step;
    use std::collections::BTreeSet;
    use std::path::PathBuf;

    fn manifest() -> Manifest {
        Manifest::from_toml_str(
            r#"
[app]
id = "com.test.script"
name = "S"
version = "1.0"
publisher = "T"

[[files]]
src = "a/**"
dst = "{app}"
"#,
        )
        .unwrap()
    }

    fn ectx() -> EngineCtx {
        EngineCtx {
            app_dir: PathBuf::from("D:\\App"),
            app_name: "S".into(),
            app_id: "com.test.script".into(),
            version: "1.0".into(),
            silent: false,
            selected_components: BTreeSet::from(["main".to_string()]),
            env: ConstEnv::empty().with("app", "D:\\App"),
        }
    }

    fn host(source: &str, opts: ScriptOptions) -> ScriptHost {
        ScriptHost::new(
            &manifest(),
            source,
            false,
            &["main".to_string()],
            Box::new(FakeHost::default()),
            opts,
        )
        .unwrap()
    }

    #[test]
    fn compile_check_ok_and_bad() {
        assert!(compile_check("fn initialize_setup(ctx) { true }").is_ok());
        assert!(compile_check("fn broken( { }").is_err());
    }

    #[test]
    fn init_false_aborts() {
        let mut h = host(
            "fn initialize_setup(ctx) { ctx.silent == false }",
            ScriptOptions::default(),
        );
        assert!(matches!(
            h.on_event(&Event::Init, &ectx()),
            Decision::Continue
        ));
        let mut h2 = host(
            "fn initialize_setup(ctx) { false }",
            ScriptOptions::default(),
        );
        assert!(matches!(
            h2.on_event(&Event::Init, &ectx()),
            Decision::Abort(_)
        ));
    }

    #[test]
    fn missing_hooks_skip() {
        let mut h = host("fn nothing() {}", ScriptOptions::default());
        assert!(matches!(
            h.on_event(&Event::Init, &ectx()),
            Decision::Continue
        ));
    }

    #[test]
    fn check_dir_error_message() {
        let mut h = host(
            r#"fn check_dir(ctx, dir) { if dir.len() > 3 { "目录太长" } else { "" } }"#,
            ScriptOptions::default(),
        );
        match h.on_event(
            &Event::DirChosen {
                dir: PathBuf::from("D:\\App"),
            },
            &ectx(),
        ) {
            Decision::Abort(msg) => assert!(msg.contains("目录太长"), "{msg}"),
            other => panic!("应中止: {other:?}"),
        }
    }

    #[test]
    fn before_file_skip() {
        let mut h = host(
            r#"fn before_file(ctx, path) { !path.contains("skipme") }"#,
            ScriptOptions::default(),
        );
        assert!(matches!(
            h.on_event(
                &Event::BeforeFile {
                    path: "ok.txt".into()
                },
                &ectx()
            ),
            Decision::Continue
        ));
        assert!(matches!(
            h.on_event(
                &Event::BeforeFile {
                    path: "skipme.bin".into()
                },
                &ectx()
            ),
            Decision::SkipFile
        ));
    }

    #[test]
    fn abort_reason_propagates() {
        let mut h = host(
            r#"fn initialize_setup(ctx) { ctx.abort("环境不满足"); true }"#,
            ScriptOptions::default(),
        );
        match h.on_event(&Event::Init, &ectx()) {
            Decision::Abort(msg) => assert!(msg.contains("环境不满足"), "{msg}"),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn runtime_error_aborts() {
        let mut h = host(
            "fn after_step(ctx, step) { let x = 1 / 0; }",
            ScriptOptions::default(),
        );
        assert!(matches!(
            h.on_event(&Event::AfterStep(Step::Files), &ectx()),
            Decision::Abort(_)
        ));
    }

    #[test]
    fn ops_limit_terminates() {
        let mut h = host(
            "fn after_install(ctx) { let i = 0; while true { i += 1; } }",
            ScriptOptions {
                timeout_ms: 0,
                max_operations: 50_000,
            },
        );
        assert!(matches!(
            h.on_event(&Event::AfterInstall, &ectx()),
            Decision::Abort(_)
        ));
    }

    #[test]
    fn timeout_terminates() {
        let mut h = host(
            "fn after_install(ctx) { let i = 0; while i >= 0 { i += 1; } }",
            ScriptOptions {
                timeout_ms: 100,
                max_operations: 0,
            },
        );
        let t = std::time::Instant::now();
        let d = h.on_event(&Event::AfterInstall, &ectx());
        assert!(matches!(d, Decision::Abort(_)));
        assert!(t.elapsed().as_secs() < 10, "应快速终止");
    }

    #[test]
    fn run_restricted_to_post_events() {
        let mut h = host(
            r#"fn before_file(ctx, path) { ctx.run("evil.exe", []); true }"#,
            ScriptOptions::default(),
        );
        match h.on_event(&Event::BeforeFile { path: "x".into() }, &ectx()) {
            Decision::Abort(msg) => assert!(msg.contains("后置事件"), "{msg}"),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn sandbox_no_import() {
        // rhai 编译期不解析模块，但运行期未注册任何模块 -> import 失败 -> 钩子错误中止
        let mut h = host(
            "import \"fs\" as fs;
fn initialize_setup(ctx) { true }",
            ScriptOptions::default(),
        );
        assert!(matches!(
            h.on_event(&Event::Init, &ectx()),
            Decision::Abort(_)
        ));
    }

    #[test]
    fn ctx_properties_visible() {
        let mut h = host(
            r#"fn initialize_setup(ctx) {
                ctx.app_name == "S" && ctx.version == "1.0" && ctx.silent == false
            }"#,
            ScriptOptions::default(),
        );
        assert!(matches!(
            h.on_event(&Event::Init, &ectx()),
            Decision::Continue
        ));
    }

    #[test]
    fn exit_receives_code() {
        let mut h = host(
            r#"fn on_exit(ctx, code) { ctx.log("exit " + code); }"#,
            ScriptOptions::default(),
        );
        assert!(matches!(
            h.on_event(&Event::Exit { code: 4 }, &ectx()),
            Decision::Continue
        ));
    }

    #[test]
    fn fs_api_roundtrip_and_guard() {
        let tmp = std::env::temp_dir().join(format!("mo-fs-api-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        std::fs::create_dir_all(tmp.join("python-runtime/runtime-envs")).unwrap();
        // {app} 外的哨兵文件（写保护目标）
        let outside = std::env::temp_dir().join(format!("mo-fs-out-{}.txt", std::process::id()));

        let e = EngineCtx {
            app_dir: tmp.clone(),
            ..ectx()
        };
        let mut h = host(
            r#"fn after_install(ctx) {
                let app = ctx.app_dir;
                // 探测：不存在 -> 存在（rhai 字符串内反斜杠双写）
                let before = ctx.file_exists(app + "\\python-runtime\\runtime-envs\\active-runtime.json");
                ctx.write_text(app + "\\python-runtime\\runtime-envs\\active-runtime.json", "cuda");
                let after = ctx.file_exists(app + "\\python-runtime\\runtime-envs\\active-runtime.json");
                // dir_exists
                let dir_ok = ctx.dir_exists(app + "\\python-runtime\\runtime-envs");
                let dir_bad = ctx.dir_exists(app + "\\python-runtime\\nope");
                // 护栏：{app} 外写入必须失败
                let outside_write = ctx.write_text("C:\\Windows\\mo-fs-evil.txt", "x");
                // 护栏：路径前缀相似也不行（app2 != app）
                let prefix_write = ctx.write_text(app + "2\\evil.txt", "x");
                // 护栏：删 {app} 外失败
                let outside_delete = ctx.delete_file("C:\\Windows\\notepad.exe");
                // {app} 内删除成功
                ctx.write_text(app + "\\tmp-mark.txt", "m");
                let del_ok = ctx.delete_file(app + "\\tmp-mark.txt");
                let del_gone = ctx.file_exists(app + "\\tmp-mark.txt");
                // 递归删目录
                ctx.write_text(app + "\\python-runtime\\runtime-envs\\cpu\\x.txt", "x");
                let rmdir_ok = ctx.delete_dir(app + "\\python-runtime\\runtime-envs\\cpu");
                let rmdir_gone = ctx.dir_exists(app + "\\python-runtime\\runtime-envs\\cpu");
                before == false && after == true && dir_ok == true && dir_bad == false
                    && outside_write == false && prefix_write == false && outside_delete == false
                    && del_ok == true && del_gone == false && rmdir_ok == true && rmdir_gone == false
            }"#,
            ScriptOptions::default(),
        );
        match h.on_event(&Event::AfterInstall, &e) {
            Decision::Continue => {}
            other => panic!("fs api 断言失败: {other:?}"),
        }
        let _ = std::fs::remove_dir_all(&tmp);
        let _ = std::fs::remove_file(&outside);
    }
}
