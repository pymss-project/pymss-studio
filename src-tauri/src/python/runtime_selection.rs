use crate::error::{AppError, AppResult};
use serde::Deserialize;
use std::path::{Path, PathBuf};
use std::process::Command;

pub(super) const HOST_PYTHON_ENV_VARS: &[&str] = &[
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONUSERBASE",
    "PYTHONSTARTUP",
    "PYTHONEXECUTABLE",
    "__PYVENV_LAUNCHER__",
    "VIRTUAL_ENV",
    "PYENV_VERSION",
    "CONDA_PREFIX",
    "CONDA_PREFIX_1",
    "CONDA_DEFAULT_ENV",
    "CONDA_PROMPT_MODIFIER",
    "CONDA_SHLVL",
    "CONDA_EXE",
    "_CE_CONDA",
    "_CE_M",
];

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ActiveRuntimeRecord {
    python_path: String,
    backend: Option<String>,
    source: Option<String>,
    overlay_path: Option<String>,
    #[serde(default)]
    debug_override: bool,
}

pub(super) fn runtime_python_command_path(python: &Path) -> Option<PathBuf> {
    if !python.is_file() {
        return None;
    }
    // Resolve directory aliases while keeping a POSIX venv's executable symlink.
    // Resolving the executable itself launches the base interpreter and loses pyvenv.cfg.
    Some(
        python
            .parent()?
            .canonicalize()
            .ok()?
            .join(python.file_name()?),
    )
}

pub(super) fn resolve_active_runtime_record(file: &PathBuf) -> Option<(String, Option<String>)> {
    if !file.is_file() {
        return None;
    }
    let content = std::fs::read_to_string(file).ok()?;
    let record: ActiveRuntimeRecord = serde_json::from_str(&content).ok()?;
    let python = PathBuf::from(&record.python_path);
    // Resolve the directory, but retain the interpreter filename. On Unix a venv
    // python is usually a symlink to the base interpreter; canonicalizing that
    // final component both loses the venv and breaks backend ownership checks.
    let resolved = if python.is_absolute() {
        python
    } else {
        file.parent()?.join(python)
    };
    let executable = resolved
        .parent()?
        .canonicalize()
        .ok()?
        .join(resolved.file_name()?);
    executable
        .is_file()
        .then(|| (executable.to_string_lossy().to_string(), record.source))
}

pub(super) fn distinct_bundled_runtime_env(
    user_runtime: Option<PathBuf>,
    bundled: Option<PathBuf>,
) -> Option<PathBuf> {
    bundled.filter(|path| path.canonicalize().ok() != user_runtime)
}

pub(super) fn active_python(
    root: &Path,
    bundled: Option<&Path>,
    developer_mode: bool,
) -> AppResult<Option<String>> {
    let user_file = root.join("active-runtime.json");
    if let Some((path, _source)) = resolve_active_runtime_record(&user_file) {
        let is_managed = is_user_runtime_python_path(root, &path);
        let is_debug_override =
            active_debug_override_path_allowed(&user_file, &path, developer_mode);
        if (is_managed || is_debug_override) && active_path_backend_matches(&user_file, &path) {
            return Ok(Some(path));
        }
    };
    if let Some(envs_dir) = bundled {
        let bundled_file = envs_dir.join("active-runtime.json");
        if let Some((path, _source)) = resolve_active_runtime_record(&bundled_file) {
            if is_bundled_runtime_python_path(&bundled_file, &path)? {
                return Ok(Some(path));
            }
        }
    }
    Ok(None)
}

pub(super) fn resolve_runtime_overlay_for_record(
    file: &Path,
    active_python: &str,
    allowed_roots: &[PathBuf],
) -> Option<PathBuf> {
    let content = std::fs::read_to_string(file).ok()?;
    let record: ActiveRuntimeRecord = serde_json::from_str(&content).ok()?;
    let (record_python, _) = resolve_active_runtime_record(&file.to_path_buf())?;
    let expected = runtime_python_command_path(Path::new(active_python))?;
    let recorded = runtime_python_command_path(Path::new(&record_python))?;
    if expected != recorded {
        return None;
    }
    let raw_overlay = record.overlay_path?.trim().to_string();
    if raw_overlay.is_empty() {
        return None;
    }
    let overlay = PathBuf::from(raw_overlay);
    let overlay = if overlay.is_absolute() {
        overlay
    } else {
        file.parent()?.join(overlay)
    };
    let overlay = overlay.canonicalize().ok()?;
    if !overlay.is_dir() {
        return None;
    }
    allowed_roots
        .iter()
        .filter_map(|root| root.canonicalize().ok())
        .any(|root| overlay.starts_with(root.join(".overlays")))
        .then_some(overlay)
}

pub(super) fn active_overlay(
    root: &Path,
    bundled: Option<&Path>,
    active_python: &str,
) -> Option<PathBuf> {
    let mut records = vec![root.join("active-runtime.json")];
    let mut allowed_roots = vec![root.to_path_buf()];
    if let Some(bundled) = bundled {
        records.push(bundled.join("active-runtime.json"));
        allowed_roots.push(bundled.to_owned());
    }
    records
        .into_iter()
        .find_map(|file| resolve_runtime_overlay_for_record(&file, active_python, &allowed_roots))
}

pub(super) fn is_bundled_runtime_python_path(file: &Path, python_path: &str) -> AppResult<bool> {
    let content = std::fs::read_to_string(file)?;
    let record: ActiveRuntimeRecord = serde_json::from_str(&content)?;
    let backend = record
        .backend
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase();
    if !matches!(backend.as_str(), "cpu" | "cuda" | "rocm" | "mlx") {
        return Ok(false);
    }
    let Some(envs_dir) = file.parent() else {
        return Ok(false);
    };
    let envs_dir = envs_dir.canonicalize()?;
    let runtime_root = envs_dir
        .parent()
        .ok_or_else(|| AppError::Worker("bundled runtime root is missing".into()))?;
    let Some(python) = runtime_python_command_path(Path::new(python_path)) else {
        return Ok(false);
    };
    let bootstrap_matches = [
        runtime_root.join("python.exe"),
        runtime_root.join("bin").join("python3"),
        runtime_root.join("bin").join("python"),
    ]
    .into_iter()
    .filter_map(|candidate| runtime_python_command_path(&candidate))
    .any(|candidate| candidate == python);
    if bootstrap_matches {
        return Ok(backend == "mlx");
    }
    let Some(env_dir) = python.parent().and_then(Path::parent) else {
        return Ok(false);
    };
    Ok(env_dir.starts_with(&envs_dir)
        && env_dir
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.eq_ignore_ascii_case(&backend)))
}

pub(super) fn active_path_backend_matches(file: &Path, python_path: &str) -> bool {
    let Ok(content) = std::fs::read_to_string(file) else {
        return false;
    };
    let Ok(record) = serde_json::from_str::<ActiveRuntimeRecord>(&content) else {
        return false;
    };
    let Some(backend) = record.backend.filter(|value| !value.trim().is_empty()) else {
        return false;
    };
    if !matches!(
        backend.trim().to_ascii_lowercase().as_str(),
        "cpu" | "cuda" | "rocm" | "mlx"
    ) {
        return false;
    }
    let path = PathBuf::from(python_path);
    path.parent()
        .and_then(Path::parent)
        .and_then(|env| env.file_name())
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.eq_ignore_ascii_case(backend.trim()))
}

pub(super) fn active_debug_override_path_allowed(
    file: &Path,
    python_path: &str,
    developer_mode: bool,
) -> bool {
    if !developer_mode {
        return false;
    }
    let Ok(content) = std::fs::read_to_string(file) else {
        return false;
    };
    let Ok(record) = serde_json::from_str::<ActiveRuntimeRecord>(&content) else {
        return false;
    };
    record.debug_override && active_path_backend_matches(file, python_path)
}

fn is_user_runtime_python_path(runtime_envs: &Path, path: &str) -> bool {
    let runtime_root = runtime_envs
        .canonicalize()
        .unwrap_or_else(|_| runtime_envs.to_owned());
    let python_path = PathBuf::from(&path);
    let env_dir = python_path
        .parent()
        .and_then(Path::parent)
        .map(Path::to_path_buf);
    env_dir
        .and_then(|path| path.canonicalize().ok())
        .is_some_and(|path| path.starts_with(&runtime_root))
}

pub(super) fn path_separator() -> &'static str {
    if cfg!(windows) {
        ";"
    } else {
        ":"
    }
}

pub(super) fn prepend_path(existing: Option<String>, dirs: Vec<PathBuf>) -> Option<String> {
    if dirs.is_empty() {
        return existing;
    }

    let mut parts: Vec<String> = dirs
        .into_iter()
        .map(|dir| dir.to_string_lossy().to_string())
        .collect();

    if let Some(value) = existing {
        if !value.trim().is_empty() {
            parts.push(value);
        }
    }

    Some(parts.join(path_separator()))
}

pub(super) fn isolate_python_environment(cmd: &mut Command, cache_root: &Path) {
    for key in HOST_PYTHON_ENV_VARS {
        cmd.env_remove(key);
    }
    // An interpreter flag also works for isolated/embedded Python, which can
    // ignore PYTHONFAULTHANDLER inherited through the environment.
    cmd.args(["-X", "faulthandler"])
        .env("PYTHONNOUSERSITE", "1")
        .env("NUMBA_CACHE_DIR", cache_root.join("numba-cache"))
        .env("PIP_CACHE_DIR", cache_root.join("pip-cache"));
}
