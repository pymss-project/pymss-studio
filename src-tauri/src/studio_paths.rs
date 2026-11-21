use crate::error::{AppError, AppResult};
use std::path::{Path, PathBuf};
#[cfg(windows)]
use std::{io::Write, sync::OnceLock};

const DATA_ROOT_ENV: &str = "PYMSS_STUDIO_DATA_ROOT";
const DATA_ROOT_DIR_NAME: &str = ".pymss-studio";
const LOCAL_DATA_ROOT_DIR_NAME: &str = "data";

#[cfg(target_os = "linux")]
pub(crate) fn extracted_appimage_mount(mount: Option<&std::ffi::OsStr>) -> bool {
    mount
        .and_then(|path| Path::new(path).file_name())
        .is_some_and(|name| name.as_encoded_bytes().starts_with(b"appimage_extracted_"))
}

#[cfg(target_os = "linux")]
pub(crate) fn appimage_extract_and_run() -> bool {
    extracted_appimage_mount(std::env::var_os("APPDIR").as_deref())
        || std::env::var_os("APPIMAGE_EXTRACT_AND_RUN").is_some()
}

#[cfg(windows)]
const PORTABLE_MARKER_FILE_NAME: &str = "pymss-studio.portable";
#[cfg(windows)]
static MANAGED_RUNTIME_ROOT: OnceLock<PathBuf> = OnceLock::new();

pub(crate) fn directory_writable(path: &Path) -> AppResult<bool> {
    let result = std::fs::create_dir_all(path).and_then(|()| {
        tempfile::Builder::new()
            .prefix(".pymss-write-check-")
            .tempfile_in(path)
            .map(drop)
    });
    match result {
        Ok(()) => Ok(true),
        Err(error)
            if error.kind() == std::io::ErrorKind::PermissionDenied
                || error.kind() == std::io::ErrorKind::ReadOnlyFilesystem =>
        {
            Ok(false)
        }
        Err(error) => Err(error.into()),
    }
}

fn development_data_root_dir() -> AppResult<PathBuf> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let project_root = manifest_dir
        .parent()
        .ok_or_else(|| AppError::Worker("failed to resolve project root".into()))?;
    Ok(project_root.join(LOCAL_DATA_ROOT_DIR_NAME))
}

fn portable_data_root_dir() -> AppResult<Option<PathBuf>> {
    #[cfg(windows)]
    {
        let exe = std::env::current_exe()?;
        let exe_dir = exe
            .parent()
            .ok_or_else(|| AppError::Worker("failed to resolve executable directory".into()))?;
        if exe_dir.join(PORTABLE_MARKER_FILE_NAME).exists() {
            return Ok(Some(exe_dir.join(LOCAL_DATA_ROOT_DIR_NAME)));
        }
    }
    Ok(None)
}

pub(crate) fn is_cargo_profile_executable(exe: &Path, target_dir: &Path) -> bool {
    let Some(profile_dir) = exe.parent() else {
        return false;
    };
    let profile = profile_dir.file_name().and_then(|value| value.to_str());
    if !matches!(profile, Some("debug" | "release")) {
        return false;
    }
    let Some(profile_parent) = profile_dir.parent() else {
        return false;
    };
    // Native builds live at target/{debug,release}; cross-compiled artifacts add exactly one
    // target-triple directory. Bundle contents are deeper and must use packaged semantics even
    // while CI is inspecting them in target/**/bundle.
    profile_parent == target_dir || profile_parent.parent() == Some(target_dir)
}

pub fn is_development_executable() -> bool {
    let target_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("target");
    std::env::current_exe()
        .ok()
        .and_then(|path| std::fs::canonicalize(path).ok())
        .zip(std::fs::canonicalize(target_dir).ok())
        .map(|(exe, target)| is_cargo_profile_executable(&exe, &target))
        .unwrap_or(false)
}

pub(crate) fn resolve_data_root(
    env_root: Option<PathBuf>,
    development_root: PathBuf,
    portable_root: Option<PathBuf>,
    legacy_root: PathBuf,
    is_development: bool,
) -> PathBuf {
    if let Some(root) = env_root {
        return root;
    }
    if is_development {
        return development_root;
    }
    if let Some(root) = portable_root {
        return root;
    }
    legacy_root
}

pub fn data_root_for(home: &Path) -> AppResult<PathBuf> {
    let env_root = std::env::var_os(DATA_ROOT_ENV)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from);
    let mut portable = portable_data_root_dir()?;
    if env_root.is_none() && !is_development_executable() {
        if let Some(root) = portable.as_ref() {
            if !directory_writable(root)? {
                portable = None;
            }
        }
    }
    Ok(resolve_data_root(
        env_root,
        development_data_root_dir()?,
        portable,
        home.join(DATA_ROOT_DIR_NAME),
        is_development_executable(),
    ))
}

pub(crate) fn resource_roots(resource: &Path) -> [PathBuf; 3] {
    [
        resource.to_path_buf(),
        resource.join("_up_"),
        resource.join("resources"),
    ]
}

pub(crate) fn runtime_root_from(resource: Option<&Path>, exe_dir: &Path) -> PathBuf {
    let mut candidates: Vec<_> = resource
        .into_iter()
        .flat_map(resource_roots)
        .map(|root| root.join("python-runtime"))
        .collect();
    candidates.push(exe_dir.join("python-runtime"));
    candidates
        .into_iter()
        .find(|path| path.is_dir())
        .unwrap_or_else(|| exe_dir.join("python-runtime"))
}

pub(crate) fn bundled_runtime_envs_from(
    resource: Option<&Path>,
    exe_dir: &Path,
) -> Option<PathBuf> {
    let mut candidates: Vec<_> = resource
        .into_iter()
        .flat_map(resource_roots)
        .map(|root| root.join("python-runtime").join("runtime-envs"))
        .collect();
    candidates.push(exe_dir.join("python-runtime").join("runtime-envs"));
    candidates.into_iter().find(|path| path.is_dir())
}

pub(crate) fn embedded_python_from(resource: Option<&Path>, exe_dir: &Path, windows: bool) -> Option<PathBuf> {
    let mut runtime_dirs: Vec<_> = resource
        .into_iter()
        .flat_map(resource_roots)
        .map(|root| root.join("python-runtime"))
        .collect();
    runtime_dirs.push(exe_dir.join("python-runtime"));

    for runtime in runtime_dirs {
        let candidates = if windows {
            vec![
                runtime.join("python.exe"),
                runtime.join("Scripts").join("python.exe"),
            ]
        } else {
            vec![
                runtime.join("bin").join("python3"),
                runtime.join("bin").join("python"),
            ]
        };
        if let Some(path) = candidates.into_iter().find(|candidate| candidate.is_file()) {
            return Some(path);
        }
    }
    None
}

pub(crate) fn bundled_bin_candidates(resource: Option<&Path>, exe_dir: &Path) -> Vec<PathBuf> {
    let mut dirs: Vec<_> = resource
        .into_iter()
        .flat_map(resource_roots)
        .map(|root| root.join("bin"))
        .collect();
    dirs.push(exe_dir.join("bin"));
    dirs
}

#[cfg(any(target_os = "macos", test))]
pub(crate) fn macos_bundle_from_executable(executable: &Path) -> Option<PathBuf> {
    let macos = executable.parent()?;
    let contents = macos.parent()?;
    let bundle = contents.parent()?;
    (macos.file_name()? == "MacOS"
        && contents.file_name()? == "Contents"
        && bundle.extension()? == "app")
        .then(|| bundle.to_path_buf())
}

#[cfg(target_os = "macos")]
pub(crate) fn apply_macos_python_home(
    command: &mut std::process::Command,
    python: &Path,
    embedded: Option<&Path>,
) {
    if let Some(embedded) = embedded {
        // Keep the executable component. Resolving the venv symlink would
        // make it look like the embedded interpreter and bypass pyvenv.cfg.
        if let (Some(parent), Some(name), Some(embedded_parent)) =
            (python.parent(), python.file_name(), embedded.parent())
        {
            if Some(name) == embedded.file_name()
                && (parent == embedded_parent
                    || parent.canonicalize().ok().zip(embedded_parent.canonicalize().ok())
                        .is_some_and(|(a, b)| a == b))
            {
                if let Some(root) = embedded_parent.parent() {
                    command.env("PYTHONHOME", root);
                }
            }
        }
    }
}

#[cfg(target_os = "macos")]
pub(crate) fn bundled_openssl_paths(bin_dirs: &[PathBuf]) -> Option<(PathBuf, PathBuf)> {
    bin_dirs.iter().find_map(|bin| {
        let dir = bin.join("openssl");
        let conf = dir.join("openssl.cnf");
        let modules = dir.join("ossl-modules");
        (conf.is_file() && modules.is_dir()).then_some((conf, modules))
    })
}

#[cfg(windows)]
pub(crate) fn windows_envs_root(runtime: &Path, data: &Path) -> AppResult<PathBuf> {
    if let Some(root) = MANAGED_RUNTIME_ROOT.get() {
        return Ok(root.clone());
    }
    let bundled = runtime.join("runtime-envs");
    let user = data.join("runtime-envs");
    let already_managed = user.join(".pymss-managed-root.json").is_file()
        || user.join("active-runtime.json").is_file();
    let root = choose_windows_runtime_root(
        &bundled,
        &user,
        already_managed,
        if already_managed {
            false
        } else {
            directory_writable(&bundled)?
        },
    );
    if root == user {
        std::fs::create_dir_all(&root)?;
        match std::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(root.join(".pymss-managed-root.json"))
        {
            Ok(mut marker) => marker.write_all(b"{\"schemaVersion\":1}\n")?,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (),
            Err(error) => return Err(error.into()),
        }
    }
    let _ = MANAGED_RUNTIME_ROOT.set(root.clone());
    Ok(MANAGED_RUNTIME_ROOT.get().cloned().unwrap_or(root))
}

#[cfg(windows)]
pub(crate) fn choose_windows_runtime_root(
    bundled: &Path,
    user: &Path,
    already_managed: bool,
    writable: bool,
) -> PathBuf {
    if already_managed || !writable {
        user.to_path_buf()
    } else {
        bundled.to_path_buf()
    }
}
