//! mo-setup：MoInstaller 安装器运行时。
//!
//! 模式分派：--uninstall / mo-uninstall.exe → 卸载（静默或 GUI 确认）；
//! /SILENT /VERYSILENT → 控制台静默安装；其余 → egui 向导。
//! 退出码：0 成功；1 致命；2 安装失败（已回滚）；3 磁盘不足；4 钩子错误。

mod gui;
mod picker;
mod strings;
mod theme;

use mo_core::constants::ConstEnv;
use mo_core::manifest::Manifest;
use mo_core::overlay::Package;
use mo_engine::event::{EngineCtx, EventBus};
use mo_engine::executor::{EngineError, Executor};
use mo_engine::hook::HookRunner;
use mo_engine::win::misc;
use mo_engine::win::registry as winreg;
use mo_script::{ScriptHost, ScriptOptions};
use std::collections::BTreeSet;
use std::path::PathBuf;
use std::process::ExitCode;

struct Args {
    dir: Option<String>,
    group: Option<String>,
    verysilent: bool,
    silent: bool,
    uninstall: bool,
    raw: Vec<String>,
}

fn parse_args(argv: &[String]) -> Args {
    let mut a = Args {
        dir: None,
        group: None,
        verysilent: false,
        silent: false,
        uninstall: false,
        raw: argv.to_vec(),
    };
    for arg in argv {
        match arg.to_ascii_lowercase().as_str() {
            "/verysilent" => a.verysilent = true,
            "/silent" => a.silent = true,
            "/uninstall" | "--uninstall" => a.uninstall = true,
            _ => {
                if let Some(v) = arg
                    .strip_prefix("/DIR=")
                    .or_else(|| arg.strip_prefix("/dir="))
                {
                    a.dir = Some(v.trim_matches('"').to_string());
                } else if let Some(v) = arg
                    .strip_prefix("/GROUP=")
                    .or_else(|| arg.strip_prefix("/group="))
                {
                    a.group = Some(v.trim_matches('"').to_string());
                }
            }
        }
    }
    a
}

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let args = parse_args(&argv);
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("mo-setup: {}", e.message());
            ExitCode::from(e.exit_code())
        }
    }
}

fn fatal(msg: impl Into<String>) -> EngineError {
    EngineError::Fatal(msg.into())
}

fn run(args: &Args) -> Result<(), EngineError> {
    let exe: PathBuf = std::env::current_exe().map_err(|e| fatal(format!("定位自身失败: {e}")))?;
    let mut pkg = Package::open(&exe).map_err(|e| fatal(format!("解析安装包: {e}")))?;
    let manifest = pkg.manifest.clone();

    // 卸载模式：显式参数，或自身文件名即 mo-uninstall.exe
    let is_uninstaller = exe
        .file_stem()
        .map(|s| s.eq_ignore_ascii_case("mo-uninstall"))
        .unwrap_or(false);
    let silent = args.silent || args.verysilent;

    if args.uninstall || is_uninstaller {
        if silent {
            return uninstall_flow(&manifest, &exe);
        }
        return run_uninstall_gui(manifest, exe);
    }
    if silent {
        return install_flow(&manifest, &mut pkg, &exe, args);
    }
    install_gui_flow(manifest, pkg, exe, args)
}

fn install_gui_flow(
    manifest: Manifest,
    mut pkg: Package,
    exe: PathBuf,
    args: &Args,
) -> Result<(), EngineError> {
    if manifest.options.require_admin && !misc::is_elevated() {
        misc::relaunch_elevated(&exe, &args.raw)
            .map_err(|e| fatal(format!("需要管理员权限，重启失败: {e}")))?;
        return Ok(()); // 新进程接手
    }
    let _mutex = misc::SingleInstance::new(&format!("MoInstaller.{}", manifest.app.id))
        .ok_or_else(|| fatal("安装程序已在运行"))?;

    let base_env = ConstEnv::from_process_env();
    let default_dir = match &args.dir {
        Some(d) => d.clone(),
        None => base_env
            .expand(&manifest.options.default_dir)
            .map_err(|e| fatal(format!("展开 default_dir: {e}")))?,
    };

    // 包内 UI 资源一次性读出
    let banner = read_meta(&mut pkg, "__mo__/theme/banner");
    let sidebar = read_meta(&mut pkg, "__mo__/theme/sidebar");
    let license = read_meta(&mut pkg, "__mo__/license")
        .map(|b| String::from_utf8_lossy(&b).into_owned())
        .unwrap_or_default();

    let app = gui::WizardApp::install_wizard(manifest, exe, default_dir, banner, sidebar, license);
    let (ok, run_target) = gui::run_install_gui(app);
    if ok {
        if let Some(prog) = run_target {
            let _ = std::process::Command::new(prog).spawn();
        }
        Ok(())
    } else {
        Err(EngineError::InstallFailed("安装失败（详见向导）".into()))
    }
}

fn run_uninstall_gui(manifest: Manifest, exe: PathBuf) -> Result<(), EngineError> {
    let app = gui::WizardApp::uninstaller(manifest, exe);
    let ok = gui::run_uninstall_gui(app);
    if ok {
        Ok(())
    } else {
        Err(EngineError::InstallFailed("卸载失败（详见向导）".into()))
    }
}

/// 宿主 API 真实实现：注册表只读 + 原生弹窗 + 子进程。
struct RealHost {
    silent: bool,
}

impl mo_script::HostApi for RealHost {
    fn reg_read(&self, root: &str, key: &str, name: &str) -> Option<String> {
        winreg::root_from_str(root).and_then(|r| winreg::read_string(r, key, name))
    }
    fn log(&self, msg: &str) {
        println!("[script] {msg}");
    }
    fn set_progress(&self, pct: f64, msg: &str) {
        println!("[progress {:.0}%] {msg}", pct);
    }
    fn message_box(&self, text: &str, kind: &str) -> bool {
        if self.silent {
            return true; // 静默模式禁交互，视为确认
        }
        misc::message_box(text, kind)
    }
    fn run(&self, cmd: &str, args: &[String]) -> Option<i32> {
        let mut c = std::process::Command::new(cmd);
        c.args(args);
        // GUI 子进程启动控制台程序时不闪黑框
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            c.creation_flags(CREATE_NO_WINDOW);
        }
        c.output().ok().map(|o| o.status.code().unwrap_or(-1))
    }
}

/// 读取脚本源：__mo__/setup.rhai（文件方式）或 manifest 内联。
pub(crate) fn script_source(manifest: &Manifest, pkg: &mut Package) -> Option<String> {
    let sc = manifest.script.as_ref()?;
    match (&sc.file, &sc.inline) {
        (Some(_), _) => {
            let e = pkg.entry("__mo__/setup.rhai")?.clone();
            Some(String::from_utf8_lossy(&pkg.read_entry(&e).ok()?).into_owned())
        }
        (None, Some(inline)) => Some(inline.clone()),
        _ => None,
    }
}

/// 构造事件总线：L1 钩子 + L2 脚本。
pub(crate) fn build_bus(
    manifest: &Manifest,
    silent: bool,
    selected: Vec<String>,
    script_src: Option<String>,
) -> EventBus {
    let mut bus = EventBus::new();
    if !manifest.hooks.is_empty() {
        bus.subscribe(Box::new(HookRunner::new(manifest.hooks.clone())));
    }
    if let Some(src) = script_src {
        let opts = ScriptOptions {
            timeout_ms: manifest
                .script
                .as_ref()
                .and_then(|s| s.timeout_ms)
                .unwrap_or(30_000),
            max_operations: 10_000_000,
        };
        match ScriptHost::new(
            manifest,
            &src,
            silent,
            &selected,
            Box::new(RealHost { silent }),
            opts,
        ) {
            Ok(h) => bus.subscribe(Box::new(h)),
            Err(e) => eprintln!("mo-setup: {e}"),
        }
    }
    bus
}

fn read_meta(pkg: &mut Package, path: &str) -> Option<Vec<u8>> {
    let e = pkg.entry(path)?.clone();
    pkg.read_entry(&e).ok()
}

fn install_flow(
    manifest: &Manifest,
    pkg: &mut Package,
    exe: &std::path::Path,
    args: &Args,
) -> Result<(), EngineError> {
    if manifest.options.require_admin && !misc::is_elevated() {
        misc::relaunch_elevated(exe, &args.raw)
            .map_err(|e| fatal(format!("需要管理员权限，重启失败: {e}")))?;
        return Ok(());
    }
    let _mutex = misc::SingleInstance::new(&format!("MoInstaller.{}", manifest.app.id))
        .ok_or_else(|| fatal("安装程序已在运行"))?;

    let base_env = ConstEnv::from_process_env();
    let target_str = match &args.dir {
        Some(d) => d.clone(),
        None => base_env
            .expand(&manifest.options.default_dir)
            .map_err(|e| fatal(format!("展开 default_dir: {e}")))?,
    };
    let target = PathBuf::from(&target_str);
    let group_name = args
        .group
        .clone()
        .unwrap_or_else(|| manifest.app.name.clone());
    let env = base_env.with_app(&target, &group_name);

    println!(
        "安装 {} {} 到: {}",
        manifest.app.name,
        manifest.app.version,
        target.display()
    );

    // 静默安装的组件选择：required 或 default（对齐 Inno 未勾选任务的静默语义）
    let selected: Vec<String> = manifest
        .components
        .iter()
        .filter(|c| c.default_selected())
        .map(|c| c.id.clone())
        .collect();
    let ctx = EngineCtx {
        app_dir: target.clone(),
        app_name: manifest.app.name.clone(),
        app_id: manifest.app.id.clone(),
        version: manifest.app.version.clone(),
        silent: true,
        selected_components: selected.iter().cloned().collect::<BTreeSet<_>>(),
        env,
    };
    let script = script_source(manifest, pkg);
    let bus = build_bus(manifest, true, selected, script);
    let mut executor = Executor::new(bus, ctx, target.join("mo-install.log"));
    executor.install(manifest, pkg, exe)
}

fn uninstall_flow(manifest: &Manifest, exe: &std::path::Path) -> Result<(), EngineError> {
    let app_dir = exe
        .parent()
        .ok_or_else(|| fatal("无法定位安装目录"))?
        .to_path_buf();
    let env = ConstEnv::from_process_env().with_app(&app_dir, &manifest.app.name);

    println!("卸载 {} ({})", manifest.app.name, app_dir.display());

    let ctx = EngineCtx {
        app_dir,
        app_name: manifest.app.name.clone(),
        app_id: manifest.app.id.clone(),
        version: manifest.app.version.clone(),
        silent: true,
        selected_components: BTreeSet::new(),
        env,
    };
    let bus = build_bus(manifest, true, Vec::new(), None);
    let log_path = ctx.app_dir.join("mo-install.log");
    let mut executor = Executor::new(bus, ctx, log_path);
    executor.uninstall(manifest, exe)
}
