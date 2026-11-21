//! Run the upstream pymss CLI using the Studio-managed runtime.
#[path = "python/runtime_native_paths.rs"]
mod native;
#[cfg(windows)]
#[allow(dead_code)]
#[path = "python/process_tree_windows.rs"]
mod process_tree_windows;
#[path = "python/runtime_selection.rs"]
mod runtime;
#[cfg(unix)]
#[path = "studio_cli/unix_console.rs"]
mod unix_console;

use crate::{
    cli_access,
    error::{AppError, AppResult},
    studio_paths,
};
#[cfg(windows)]
use process_tree_windows::QueryJob;
use serde_json::Value;
#[cfg(unix)]
use std::os::unix::process::ExitStatusExt;
use std::{
    env,
    ffi::OsString,
    fs,
    path::{Path, PathBuf},
    process::{Command, ExitStatus},
};
#[cfg(windows)]
use windows_sys::Win32::System::Console::{SetConsoleCtrlHandler, CTRL_BREAK_EVENT, CTRL_C_EVENT};

#[cfg(windows)]
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

#[cfg(windows)]
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
        #[cfg(target_os = "macos")]
        {
            studio_paths::apply_macos_python_home(&mut cmd, python, Some(&self.bootstrap));
            if let Some((conf, modules)) = studio_paths::bundled_openssl_paths(&self.tools) {
                cmd.env("OPENSSL_CONF", conf)
                    .env("OPENSSL_MODULES", modules);
            }
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

    fn require_recovered_runtime(&self) -> AppResult<()> {
        if self.roots().iter().any(|root| {
            root.join(".rocm-transaction.json").exists()
                || root.join(".overlay-activation.json").exists()
        }) {
            return Err(AppError::Worker(
                "Recover the unfinished runtime operation in Pymss Studio before retrying.".into(),
            ));
        }
        Ok(())
    }

    fn prepare(&self, selected: Option<&Path>) -> AppResult<ExitStatus> {
        self.require_recovered_runtime()?;
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
        Ok(console_status(&mut cmd)?)
    }
}

fn access_for_args(args: &[OsString]) -> (bool, bool) {
    // Upstream removes binding selectors with argparse.parse_known_args
    // before parsing the command. Selectors can also follow the subcommand.
    let mut args = args.iter();
    let mut words = std::iter::from_fn(|| loop {
        let arg = args.next()?;
        if let Some(option) = arg.to_str().filter(|arg| arg.starts_with("--")) {
            let (name, has_value) = option
                .split_once('=')
                .map_or((option, false), |(name, _)| (name, true));
            if "--plugin-store".starts_with(name) ^ "--plugin-binding".starts_with(name) {
                if !has_value {
                    args.next();
                }
                continue;
            }
        }
        return Some(arg);
    });
    let command = words.next().and_then(|word| word.to_str());
    let action = words.next().and_then(|word| word.to_str());
    match (command, action) {
        (Some("register" | "unregister"), _) => (false, true),
        (Some("install" | "uninstall"), _)
        | (Some("ops"), Some("prepare-python"))
        | (
            Some("plugins"),
            Some(
                "install" | "uninstall" | "prepare" | "repair" | "enable" | "disable" | "update"
                | "recover",
            ),
        ) => (true, false),
        _ => (false, false),
    }
}

#[cfg(windows)]
fn console_status(command: &mut Command) -> std::io::Result<std::process::ExitStatus> {
    QueryJob::console_status(command)
}

#[cfg(unix)]
use unix_console::status as console_status;

#[cfg(target_os = "linux")]
fn watch_appimage_extractor(installation: &Path) -> AppResult<bool> {
    use std::os::unix::fs::MetadataExt;

    if !studio_paths::appimage_extract_and_run() {
        return Ok(true);
    }

    // Watch the verified extraction parent so abrupt parent exit cancels the
    // command even if the AppImage runtime cannot forward a signal.
    let parent = unsafe { libc::getppid() };
    if parent == 1 {
        return Ok(false);
    }
    let image = fs::metadata(installation)?;
    let parent_exe = fs::metadata(format!("/proc/{parent}/exe"));
    if unsafe { libc::getppid() } != parent {
        return Ok(false);
    }
    match parent_exe {
        Ok(exe) if (exe.dev(), exe.ino()) == (image.dev(), image.ino()) => {}
        result => {
            let detail = result.err().map_or_else(
                || "the parent executable differs from the launched AppImage".into(),
                |error| error.to_string(),
            );
            return Err(AppError::Worker(format!(
                "Could not verify the AppImage extraction process ({detail}). Reopen the AppImage and retry."
            )));
        }
    }
    if unsafe {
        libc::prctl(
            libc::PR_SET_PDEATHSIG,
            libc::SIGTERM as libc::c_ulong,
            0,
            0,
            0,
        )
    } == -1
    {
        return Err(std::io::Error::last_os_error().into());
    }
    // If the outer process exited before prctl registered the notification,
    // leave without starting Python.
    if unsafe { libc::getppid() } != parent {
        return Ok(false);
    }
    unix_console::set_extractor(parent);
    Ok(true)
}

fn run(args: Vec<OsString>, linux_resources: Option<PathBuf>) -> AppResult<ExitStatus> {
    #[cfg(windows)]
    if unsafe { SetConsoleCtrlHandler(Some(console_handler), 1) } == 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    #[cfg(unix)]
    unix_console::install()?;

    let exe = env::current_exe()?;
    #[cfg(target_os = "macos")]
    let exe = exe.canonicalize()?;
    let dir = exe
        .parent()
        .ok_or_else(|| AppError::Worker("Could not locate the Studio executable.".into()))?;
    #[cfg(windows)]
    let _application = cli_access::application(dir, false)?;
    #[cfg(windows)]
    let resource = Some(dir);
    #[cfg(target_os = "macos")]
    let installation = studio_paths::macos_bundle_from_executable(&exe).ok_or_else(|| {
        AppError::Worker("Run pymss from inside the Pymss Studio application bundle.".into())
    })?;
    #[cfg(target_os = "macos")]
    let app_resources = installation.join("Contents/Resources");
    #[cfg(target_os = "macos")]
    let resource = Some(app_resources.as_path());
    #[cfg(target_os = "linux")]
    let installation = crate::linux_update::current_image()?;
    #[cfg(target_os = "linux")]
    if !watch_appimage_extractor(&installation)? {
        return Ok(ExitStatus::from_raw(libc::SIGTERM));
    }
    #[cfg(target_os = "linux")]
    let resource = linux_resources.as_deref();
    #[cfg(not(target_os = "linux"))]
    let _ = linux_resources;
    #[cfg(unix)]
    let _application = cli_access::installed_application(&installation, false)?;
    let home = env::home_dir()
        .ok_or_else(|| AppError::Worker("Could not locate the user home directory.".into()))?;
    let data = std::path::absolute(studio_paths::data_root_for(&home)?)?;
    #[cfg(windows)]
    let runtime_dir = studio_paths::runtime_root_from(resource, dir);
    #[cfg(windows)]
    let root = studio_paths::windows_envs_root(&runtime_dir, &data)?;
    #[cfg(unix)]
    let root = data.join("runtime-envs");
    let bundled = runtime::distinct_bundled_runtime_env(
        root.canonicalize().ok(),
        studio_paths::bundled_runtime_envs_from(resource, dir),
    );
    let _migration = cli_access::resource(&data.join("settings"), cli_access::MIGRATION, false)?;
    let settings = read_settings(&data)?;
    let resources = resource.into_iter().flat_map(studio_paths::resource_roots);
    let helper = if studio_paths::is_development_executable() {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../python/studio_cli.py")
    } else {
        resources
            .map(|p| p.join("python/studio_cli.py"))
            .find(|p| p.is_file())
            .unwrap_or_else(|| dir.join("python/studio_cli.py"))
    };
    #[cfg(windows)]
    let bootstrap = if studio_paths::is_development_executable() {
        env::var_os("PYMSS_STUDIO_PYTHON").map(PathBuf::from)
    } else {
        None
    }
    .unwrap_or_else(|| runtime_dir.join("python.exe"));
    #[cfg(unix)]
    let embedded = studio_paths::embedded_python_from(resource, dir, false).ok_or_else(|| {
        AppError::Worker("Studio's bundled Python is missing. Reinstall Pymss Studio.".into())
    })?;
    #[cfg(target_os = "macos")]
    let bootstrap = embedded;
    #[cfg(target_os = "linux")]
    let bootstrap = crate::python::cached_bootstrap(
        embedded
            .parent()
            .and_then(Path::parent)
            .ok_or_else(|| AppError::Worker("Invalid bundled Python path.".into()))?,
        &data,
        env!("CARGO_PKG_VERSION"),
    )?;
    let ctx = Context {
        data,
        root,
        bundled,
        bootstrap,
        helper,
        tools: studio_paths::bundled_bin_candidates(resource, dir)
            .into_iter()
            .filter(|p| p.is_dir())
            .collect(),
        developer: settings["developerMode"].as_bool() == Some(true),
    };
    let (write_runtime, writes_registry) = access_for_args(&args);
    let mut lease = cli_access::resource(&ctx.root, cli_access::RUNTIME, write_runtime)?;
    let mut selected = ctx.selected()?;
    #[cfg(windows)]
    let cold = |path: Option<&Path>| {
        path.is_none_or(|p| {
            ctx.roots()
                .iter()
                .any(|root| needs_relocation(p, root, &runtime_dir))
        })
    };
    #[cfg(unix)]
    let cold = |path: Option<&Path>| path.is_none();
    if cold(selected.as_deref()) {
        drop(lease);
        let maintenance = cli_access::resource(&ctx.root, cli_access::RUNTIME, true)?;
        let current = ctx.selected()?;
        if cold(current.as_deref()) {
            let status = ctx.prepare(current.as_deref())?;
            #[cfg(unix)]
            if status.signal().is_some() {
                return Ok(status);
            }
            if !status.success() {
                return Err(AppError::Worker("环境初始化失败，请检查上述提示。".into()));
            }
        }
        drop(maintenance);
        lease = cli_access::resource(&ctx.root, cli_access::RUNTIME, write_runtime)?;
        selected = ctx.selected()?;
    }
    let _runtime = lease;
    #[cfg(unix)]
    ctx.require_recovered_runtime()?;
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
    let status = console_status(
        ctx.command(&python, overlay.as_deref())?
            .arg(&ctx.helper)
            .arg("bind"),
    )?;
    if !status.success() {
        return Ok(status);
    }
    cmd.env("PYMSS_MODEL_DIR", model_dir).env(
        "PYMSS_USER_MODELS",
        ctx.data.join("settings/user_models.json"),
    );
    #[cfg(unix)]
    cmd.arg(&ctx.helper).arg("run");
    #[cfg(windows)]
    cmd.args(["-m", "pymss"]);
    Ok(console_status(cmd.args(args))?)
}

#[cfg(not(target_os = "linux"))]
pub(crate) fn exit(args: Vec<OsString>) -> ! {
    finish(run(args, None))
}

#[cfg(target_os = "linux")]
pub(crate) fn exit_linux(args: Vec<OsString>, package: &tauri::PackageInfo) -> ! {
    let result = (|| {
        let resources = tauri::utils::platform::resource_dir(package, &tauri::Env::default())
            .map_err(|error| AppError::Worker(error.to_string()))?;
        if let Some(cwd) = env::var_os("OWD").filter(|path| Path::new(path).is_absolute()) {
            env::set_current_dir(cwd)?;
        }
        run(args, Some(resources))
    })();
    finish(result)
}

fn finish(result: AppResult<ExitStatus>) -> ! {
    #[cfg(unix)]
    if let Some(signal) = result
        .as_ref()
        .ok()
        .and_then(ExitStatusExt::signal)
        .or_else(unix_console::pending_signal)
    {
        unix_console::terminate(signal);
    }

    match result {
        Ok(status) => std::process::exit(status.code().unwrap_or(1)),
        Err(error) => {
            eprintln!("pymss: {error}");
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn classifies_mutations_with_binding_selectors_before_or_inside_commands() {
        for (args, expected) in [
            (
                &["--plugin-store=/binding", "plugins", "update"][..],
                (true, false),
            ),
            (
                &["plugins", "--plugin-binding", "/binding", "install"],
                (true, false),
            ),
            (
                &["plugins", "--plugin-st=/binding", "repair"],
                (true, false),
            ),
            (
                &["ops", "--plugin-store=/binding", "prepare-python"],
                (true, false),
            ),
            (&["--plugin-store", "/binding", "register"], (false, true)),
            (
                &["plugins", "--plugin-store=/binding", "list"],
                (false, false),
            ),
        ] {
            let args: Vec<_> = args.iter().map(std::ffi::OsString::from).collect();
            assert_eq!(super::access_for_args(&args), expected, "{args:?}");
        }
    }
}
