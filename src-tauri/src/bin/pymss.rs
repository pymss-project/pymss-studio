//! Console-subsystem entry point shipped beside Pymss Studio on Windows.
#[cfg(windows)]
#[path = "../cli_access.rs"]
mod cli_access;
#[cfg(windows)]
#[path = "../error.rs"]
mod error;
#[cfg(windows)]
#[path = "../python/runtime_native_paths.rs"]
mod runtime_native_paths;
#[cfg(windows)]
#[path = "../python/runtime_selection.rs"]
mod runtime_selection;
#[cfg(windows)]
#[path = "../studio_paths.rs"]
mod studio_paths;

#[cfg(windows)]
#[allow(dead_code)]
#[path = "../python/process_tree_windows.rs"]
mod process_tree_windows;

#[cfg(windows)]
mod windows {
    use super::process_tree_windows::QueryJob;
    use super::{
        cli_access,
        error::{AppError, AppResult},
        runtime_native_paths as native, runtime_selection as runtime, studio_paths,
    };
    use serde_json::Value;
    use std::{
        env,
        ffi::OsString,
        fs,
        path::{Path, PathBuf},
        process::Command,
    };
    use windows_sys::Win32::System::Console::{
        SetConsoleCtrlHandler, CTRL_BREAK_EVENT, CTRL_C_EVENT,
    };

    unsafe extern "system" fn console_handler(event: u32) -> i32 {
        // Python receives the same event. Stay alive until its exit code is available.
        i32::from(matches!(event, CTRL_C_EVENT | CTRL_BREAK_EVENT))
    }

    fn read_settings(data: &Path) -> AppResult<Value> {
        match fs::read(data.join("settings/app.json")) {
            Ok(raw) => {
                let value: Value = serde_json::from_slice(&raw)?;
                if !(value.is_null() || value.is_object())
                    || value
                        .get("modelDir")
                        .is_some_and(|v| !v.is_null() && !v.is_string())
                {
                    return Err(AppError::Worker("Studio 设置文件中的模型目录无效。".into()));
                }
                Ok(value)
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Value::Null),
            Err(e) => Err(e.into()),
        }
    }

    fn needs_relocation(python: &Path, root: &Path, base: &Path) -> bool {
        let Some(venv) = python.parent().and_then(Path::parent) else {
            return false;
        };
        if !root.canonicalize().is_ok_and(|p| venv.starts_with(p)) {
            return false;
        }
        let Ok(cfg) = fs::read_to_string(venv.join("pyvenv.cfg")) else {
            return false;
        };
        let home = cfg
            .lines()
            .filter_map(|s| s.split_once('='))
            .find(|(key, _)| key.trim() == "home")
            .map(|(_, v)| Path::new(v.trim()));
        if venv.join("pymss-bootstrap-binding.json").is_file() {
            return !home
                .and_then(|p| p.canonicalize().ok())
                .zip(root.canonicalize().ok())
                .is_some_and(|(p, r)| p.starts_with(r.join(".bootstrap")));
        }
        base.join("python.exe").is_file()
            && !home
                .and_then(|p| p.canonicalize().ok())
                .zip(base.canonicalize().ok())
                .is_some_and(|(a, b)| a == b)
    }

    struct Context {
        data: PathBuf,
        root: PathBuf,
        bundled: Option<PathBuf>,
        bootstrap: PathBuf,
        helper: PathBuf,
        tools: Vec<PathBuf>,
        developer: bool,
    }

    impl Context {
        fn command(&self, python: &Path, overlay: Option<&Path>) -> AppResult<Command> {
            let mut cmd = Command::new(python);
            runtime::isolate_python_environment(&mut cmd, &self.data.join("temp"));
            cmd.args(["-X", "utf8", "-P"])
                .env("PYTHONIOENCODING", "utf-8")
                .env("PYTHONDONTWRITEBYTECODE", "1")
                .env("PYMSS_STUDIO_RUNTIME_ENVS_DIR", &self.root)
                .env(
                    "PYMSS_STUDIO_ACTIVE_RUNTIME_FILE",
                    self.root.join("active-runtime.json"),
                )
                .env("PYMSS_STUDIO_BOOTSTRAP_PYTHON", &self.bootstrap)
                .env(
                    "PYMSS_STUDIO_ALLOW_DEBUG_RUNTIME_OVERRIDE",
                    if self.developer { "1" } else { "0" },
                )
                .env_remove("PYMSS_STUDIO_BUNDLED_RUNTIME_ENVS_DIR");
            if let Some(dir) = &self.bundled {
                cmd.env("PYMSS_STUDIO_BUNDLED_RUNTIME_ENVS_DIR", dir);
            }
            if let Some(dir) = overlay {
                cmd.env("PYTHONPATH", dir);
            }
            let roots = self.roots();
            let mut dirs = native::native_directories(python)?;
            dirs.extend(self.tools.iter().cloned());
            let path = native::clean_managed_rocm_path(env::var("PATH").ok(), &roots);
            if let Some(value) = runtime::prepend_path(path, dirs) {
                cmd.env("PATH", value);
            }
            Ok(cmd)
        }

        fn roots(&self) -> Vec<PathBuf> {
            std::iter::once(&self.root)
                .chain(&self.bundled)
                .cloned()
                .collect()
        }

        fn selected(&self) -> AppResult<Option<PathBuf>> {
            let path = runtime::active_python(&self.root, self.bundled.as_deref(), self.developer)?
                .map(PathBuf::from);
            if let Some(python) = &path {
                native::reject_reserved_interpreter(python, &self.roots())?;
                native::reject_pending_environment(python, &self.roots())?;
            }
            Ok(path)
        }

        fn prepare(&self, selected: Option<&Path>) -> AppResult<()> {
            if self.roots().iter().any(|root| {
                root.join(".rocm-transaction.json").exists()
                    || root.join(".overlay-activation.json").exists()
            }) {
                return Err(AppError::Worker(
                    "请先在 Studio 中恢复未完成的运行环境更新。".into(),
                ));
            }
            if !self.bootstrap.is_file() {
                return Err(AppError::Worker(
                    "请在 Pymss Studio 的设置 → 环境管理中安装或修复运行环境。".into(),
                ));
            }
            native::reject_managed_bootstrap(&self.bootstrap, &self.roots())?;
            let mut cmd = self.command(&self.bootstrap, None)?;
            cmd.arg(&self.helper);
            if let Some(path) = selected {
                cmd.arg("relocate").arg(path);
            } else {
                cmd.arg("prepare");
            }
            let status = QueryJob::console_status(&mut cmd)?;
            if status.success() {
                Ok(())
            } else {
                Err(AppError::Worker("环境初始化失败，请检查上述提示。".into()))
            }
        }
    }

    fn access_for_args(args: &[OsString]) -> (bool, bool) {
        // Only classify commands which mutate shared Studio resources. Every original
        // argument, including plugin-specific options, goes through to upstream.
        let mut words = args.iter().filter_map(|arg| arg.to_str());
        while let Some(word) = words.next() {
            match word {
                "--plugin-store" | "--plugin-binding" => {
                    words.next();
                }
                flag if flag.starts_with("--plugin-store=")
                    || flag.starts_with("--plugin-binding=") =>
                {
                    ()
                }
                "register" | "unregister" => return (false, true),
                "install" | "uninstall" => return (true, false),
                "plugins" => {
                    return (
                        matches!(
                            words.next(),
                            Some(
                                "install"
                                    | "uninstall"
                                    | "prepare"
                                    | "repair"
                                    | "enable"
                                    | "disable"
                                    | "update"
                                    | "recover"
                            )
                        ),
                        false,
                    )
                }
                "ops" => return (words.next() == Some("prepare-python"), false),
                _ => break,
            }
        }
        (false, false)
    }

    pub fn run() -> AppResult<i32> {
        if unsafe { SetConsoleCtrlHandler(Some(console_handler), 1) } == 0 {
            return Err(std::io::Error::last_os_error().into());
        }
        let exe = env::current_exe()?;
        let dir = exe
            .parent()
            .ok_or_else(|| AppError::Worker("无法定位 Studio 目录。".into()))?;
        let _application = cli_access::application(dir, false)?;
        let home = env::home_dir().ok_or_else(|| AppError::Worker("无法定位用户目录。".into()))?;
        let data = std::path::absolute(studio_paths::data_root_for(&home)?)?;
        let runtime_dir = studio_paths::runtime_root_from(Some(dir), dir);
        let root = studio_paths::windows_envs_root(&runtime_dir, &data)?;
        let bundled = runtime::distinct_bundled_runtime_env(
            root.canonicalize().ok(),
            studio_paths::bundled_runtime_envs_from(Some(dir), dir),
        );
        let _migration =
            cli_access::resource(&data.join("settings"), cli_access::MIGRATION, false)?;
        let settings = read_settings(&data)?;
        let resources = studio_paths::resource_roots(dir);
        let helper = if studio_paths::is_development_executable() {
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../python/studio_cli.py")
        } else {
            resources
                .iter()
                .map(|p| p.join("python/studio_cli.py"))
                .find(|p| p.is_file())
                .unwrap_or_else(|| dir.join("python/studio_cli.py"))
        };
        let bootstrap = if studio_paths::is_development_executable() {
            env::var_os("PYMSS_STUDIO_PYTHON").map(PathBuf::from)
        } else {
            None
        }
        .unwrap_or_else(|| runtime_dir.join("python.exe"));
        let ctx = Context {
            data,
            root,
            bundled,
            bootstrap,
            helper,
            tools: resources
                .iter()
                .map(|p| p.join("bin"))
                .filter(|p| p.is_dir())
                .collect(),
            developer: settings["developerMode"].as_bool() == Some(true),
        };
        let args: Vec<OsString> = env::args_os().skip(1).collect();
        let (write_runtime, writes_registry) = access_for_args(&args);
        let mut lease = cli_access::resource(&ctx.root, cli_access::RUNTIME, write_runtime)?;
        let mut selected = ctx.selected()?;
        let cold = |path: Option<&Path>| {
            path.is_none_or(|p| {
                ctx.roots()
                    .iter()
                    .any(|root| needs_relocation(p, root, &runtime_dir))
            })
        };
        if cold(selected.as_deref()) {
            drop(lease);
            let maintenance = cli_access::resource(&ctx.root, cli_access::RUNTIME, true)?;
            let current = ctx.selected()?;
            if cold(current.as_deref()) {
                ctx.prepare(current.as_deref())?;
            }
            drop(maintenance);
            lease = cli_access::resource(&ctx.root, cli_access::RUNTIME, write_runtime)?;
            selected = ctx.selected()?;
        }
        let _runtime = lease;
        let python = selected.ok_or_else(|| {
            AppError::Worker("请在 Studio 设置 → 环境管理中选择可用的运行环境。".into())
        })?;
        let overlay =
            runtime::active_overlay(&ctx.root, ctx.bundled.as_deref(), &python.to_string_lossy());
        let _queries = cli_access::resource(&ctx.root, ".pymss-cli-query.lock", false)?;
        let _models = cli_access::resource(
            &ctx.data.join("settings"),
            cli_access::MODELS,
            writes_registry,
        )?;
        let model_dir = settings["modelDir"]
            .as_str()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| ctx.data.join("models"));
        let mut cmd = ctx.command(&python, overlay.as_deref())?;
        let status = QueryJob::console_status(
            ctx.command(&python, overlay.as_deref())?
                .arg(&ctx.helper)
                .arg("bind"),
        )?;
        if !status.success() {
            return Ok(status.code().unwrap_or(1));
        }
        cmd.env("PYMSS_MODEL_DIR", model_dir).env(
            "PYMSS_USER_MODELS",
            ctx.data.join("settings/user_models.json"),
        );
        let status = QueryJob::console_status(cmd.args(["-m", "pymss"]).args(args))?;
        Ok(status.code().unwrap_or(1))
    }
}

#[cfg(windows)]
fn main() {
    match windows::run() {
        Ok(code) => std::process::exit(code),
        Err(e) => {
            eprintln!("pymss: {e}");
            std::process::exit(1);
        }
    }
}

#[cfg(not(windows))]
fn main() {
    eprintln!("This Studio launcher is available on Windows.");
    std::process::exit(1);
}
