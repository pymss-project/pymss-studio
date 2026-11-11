use crate::error::{AppError, AppResult};
use serde_json::Value;
use std::path::{Component, Path, PathBuf};

pub(super) fn command_executable(python: &Path) -> PathBuf {
    if python.components().count() != 1 || python.is_absolute() {
        return python.to_path_buf();
    }
    let mut candidates = if cfg!(windows) {
        vec![std::env::current_dir().unwrap_or_default()]
    } else {
        Vec::new()
    };
    candidates.extend(std::env::split_paths(
        &std::env::var_os("PATH").unwrap_or_default(),
    ));
    for directory in candidates {
        let candidate = directory.join(python);
        if candidate.is_file() {
            return candidate;
        }
        if cfg!(windows) && candidate.extension().is_none() {
            let candidate = candidate.with_extension("exe");
            if candidate.is_file() {
                return candidate;
            }
        }
    }
    python.to_path_buf()
}

fn environment_root(python: &Path) -> Option<PathBuf> {
    let python = command_executable(python);
    let parent = python.parent()?.canonicalize().ok()?;
    if parent
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.eq_ignore_ascii_case("Scripts") || name == "bin")
    {
        Some(parent.parent()?.to_path_buf())
    } else {
        Some(parent)
    }
}

fn relative_directory(root: &Path, raw: &str) -> AppResult<Option<PathBuf>> {
    // Validate the serialized POSIX form on every host, including Windows prefixes.
    if raw.is_empty()
        || raw.contains('\\')
        || raw.contains(':')
        || Path::new(raw)
            .components()
            .any(|part| !matches!(part, Component::Normal(_)))
    {
        return Err(AppError::Worker(
            "RUNTIME_NATIVE_PATH_INVALID: Native paths must be relative environment directories"
                .into(),
        ));
    }
    let candidate = root.join(raw);
    if !candidate.exists() {
        return Err(AppError::Worker(
            "RUNTIME_NATIVE_PATH_INVALID: A declared native directory is missing".into(),
        ));
    }
    let resolved = candidate.canonicalize()?;
    if !resolved.starts_with(root) || !resolved.is_dir() {
        return Err(AppError::Worker(
            "RUNTIME_NATIVE_PATH_INVALID: Native directory escapes its environment".into(),
        ));
    }
    Ok(Some(resolved))
}

pub(super) fn native_directories(python: &Path) -> AppResult<Vec<PathBuf>> {
    let Some(root) = environment_root(python) else {
        return Ok(Vec::new());
    };
    let state_file = root.join("pymss-runtime-state.json");
    let state = if state_file.is_file() {
        serde_json::from_slice::<Value>(&std::fs::read(&state_file)?)?
    } else {
        Value::Null
    };
    let mut directories = Vec::new();
    if state.get("nativeToolDirs").is_some() || state.get("nativeLibraryDirs").is_some() {
        for key in ["nativeToolDirs", "nativeLibraryDirs"] {
            let Some(values) = state.get(key) else {
                continue;
            };
            let values = values.as_array().ok_or_else(|| {
                AppError::Worker(format!(
                    "RUNTIME_NATIVE_PATH_INVALID: {key} must be an array"
                ))
            })?;
            for value in values {
                let raw = value.as_str().ok_or_else(|| {
                    AppError::Worker(format!(
                        "RUNTIME_NATIVE_PATH_INVALID: {key} entries must be strings"
                    ))
                })?;
                if let Some(directory) = relative_directory(&root, raw)? {
                    if !directories.contains(&directory) {
                        directories.push(directory);
                    }
                }
            }
        }
        return Ok(directories);
    }
    // Older environments predate native metadata. Only inspect this interpreter's SDK.
    let site = root.join("Lib/site-packages");
    let Ok(entries) = std::fs::read_dir(&site) else {
        return Ok(directories);
    };
    let mut packages: Vec<_> = entries
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.starts_with("_rocm_sdk_core"))
        })
        .collect();
    packages.sort();
    for package in packages {
        for suffix in ["lib/llvm/bin", "bin"] {
            let candidate = package.join(suffix);
            if !candidate.is_dir() {
                continue;
            }
            let relative = candidate.strip_prefix(&root).map_err(|_| {
                AppError::Worker("RUNTIME_NATIVE_PATH_INVALID: Invalid SDK directory".into())
            })?;
            let raw = relative.to_string_lossy().replace('\\', "/");
            if let Some(directory) = relative_directory(&root, &raw)? {
                if !directories.contains(&directory) {
                    directories.push(directory);
                }
            }
        }
    }
    Ok(directories)
}

fn reserved_component(path: &Path) -> bool {
    path.components().any(|component| {
        component
            .as_os_str()
            .to_str()
            .is_some_and(|name| name.to_ascii_lowercase().starts_with(".rocm-"))
    })
}

pub(super) fn reject_reserved_interpreter(
    python: &Path,
    managed_roots: &[PathBuf],
) -> AppResult<()> {
    let python = command_executable(python);
    let python = python.as_path();
    let mut candidates = vec![python.to_path_buf()];
    if let Some(parent) = python
        .parent()
        .and_then(|parent| parent.canonicalize().ok())
    {
        if let Some(name) = python.file_name() {
            candidates.push(parent.join(name));
        }
    }
    // Inspect the final symlink too: aliases into preserved environments remain forbidden.
    if let Ok(resolved) = python.canonicalize() {
        candidates.push(resolved);
    }
    for root in managed_roots {
        let canonical = root.canonicalize().unwrap_or_else(|_| root.clone());
        for candidate in &candidates {
            for boundary in [root, &canonical] {
                if let Ok(relative) = candidate.strip_prefix(boundary) {
                    if reserved_component(relative) {
                        return Err(AppError::Worker("RUNTIME_RESERVED_ENVIRONMENT: Preserved runtime environments cannot execute workers".into()));
                    }
                }
            }
        }
    }
    Ok(())
}

pub(super) fn reject_pending_environment(
    python: &Path,
    managed_roots: &[PathBuf],
) -> AppResult<()> {
    let Some(environment) = environment_root(python) else {
        return Ok(());
    };
    for root in managed_roots {
        let root = root.canonicalize().unwrap_or_else(|_| root.clone());
        if root.join(".rocm-transaction.json").exists() && environment == root.join("rocm") {
            return Err(AppError::Worker("RUNTIME_RECOVERY_REQUIRED: A runtime transaction must be recovered before executing this environment".into()));
        }
    }
    Ok(())
}

pub(super) fn reject_managed_bootstrap(python: &Path, managed_roots: &[PathBuf]) -> AppResult<()> {
    let python = command_executable(python);
    let mut environments = environment_root(&python).into_iter().collect::<Vec<_>>();
    if let Ok(resolved) = python.canonicalize() {
        environments.extend(environment_root(&resolved));
    }
    for root in managed_roots {
        let root = root.canonicalize().unwrap_or_else(|_| root.clone());
        if environments.iter().any(|environment| {
            ["cpu", "cuda", "rocm", "mlx"]
                .iter()
                .any(|backend| environment == &root.join(backend))
        }) {
            return Err(AppError::Worker("RUNTIME_BOOTSTRAP_INVALID: Runtime management requires an interpreter outside managed runtime environments".into()));
        }
    }
    Ok(())
}

pub(super) fn clean_managed_rocm_path(
    existing: Option<String>,
    managed_roots: &[PathBuf],
) -> Option<String> {
    existing.map(|existing| {
        let roots: Vec<_> = managed_roots
            .iter()
            .map(|root| root.canonicalize().unwrap_or_else(|_| root.clone()))
            .collect();
        let retained: Vec<_> = std::env::split_paths(&existing)
            .filter(|path| {
                let resolved = path.canonicalize().unwrap_or_else(|_| path.clone());
                !roots.iter().any(|root| {
                    resolved.strip_prefix(root).ok().is_some_and(|relative| {
                        relative.components().any(|part| {
                            part.as_os_str().to_str().is_some_and(|name| {
                                let name = name.to_ascii_lowercase();
                                name == "rocm"
                                    || name.starts_with(".rocm-")
                                    || name.starts_with("_rocm_sdk")
                            })
                        })
                    })
                })
            })
            .collect();
        std::env::join_paths(retained)
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned()
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_state_is_relative_contained_and_overrides_legacy_layout() {
        let fixture = tempfile::tempdir().unwrap();
        let root = fixture.path();
        std::fs::create_dir_all(root.join("Scripts")).unwrap();
        let python = root.join("Scripts/python.exe");
        std::fs::write(&python, "stub").unwrap();
        std::fs::create_dir_all(root.join("sdk/bin")).unwrap();
        std::fs::write(
            root.join("pymss-runtime-state.json"),
            r#"{"nativeToolDirs":["sdk/bin"],"nativeLibraryDirs":["sdk/bin"]}"#,
        )
        .unwrap();
        assert_eq!(
            native_directories(&python).unwrap(),
            vec![root.join("sdk/bin").canonicalize().unwrap()]
        );
        for raw in ["../outside", "/outside", "C:/outside", "sdk\\bin", "."] {
            assert!(
                relative_directory(&root.canonicalize().unwrap(), raw).is_err(),
                "{raw}"
            );
        }
        std::fs::create_dir_all(root.join("Lib/site-packages/_rocm_sdk_core/bin")).unwrap();
        std::fs::write(
            root.join("pymss-runtime-state.json"),
            r#"{"nativeToolDirs":[],"nativeLibraryDirs":[]}"#,
        )
        .unwrap();
        assert!(native_directories(&python).unwrap().is_empty());
        std::fs::write(
            root.join("pymss-runtime-state.json"),
            r#"{"nativeToolDirs":["missing-sdk/bin"],"nativeLibraryDirs":[]}"#,
        )
        .unwrap();
        assert!(native_directories(&python)
            .err()
            .unwrap()
            .to_string()
            .contains("RUNTIME_NATIVE_PATH_INVALID"));
        std::fs::remove_file(root.join("pymss-runtime-state.json")).unwrap();
        assert_eq!(
            native_directories(&python).unwrap(),
            vec![root
                .join("Lib/site-packages/_rocm_sdk_core/bin")
                .canonicalize()
                .unwrap()]
        );
    }

    #[test]
    fn reserved_runtime_slots_cannot_execute_and_are_removed_from_inherited_path() {
        let fixture = tempfile::tempdir().unwrap();
        let root = fixture.path().canonicalize().unwrap();
        let previous = root.join(".rocm-previous/environment/Scripts/python.exe");
        std::fs::create_dir_all(previous.parent().unwrap()).unwrap();
        std::fs::write(&previous, "stub").unwrap();
        assert!(reject_reserved_interpreter(&previous, std::slice::from_ref(&root)).is_err());
        let ordinary = root.join("cpu/Scripts/python.exe");
        assert!(reject_reserved_interpreter(&ordinary, std::slice::from_ref(&root)).is_ok());
        let inherited = std::env::join_paths([
            previous.parent().unwrap(),
            &root.join("rocm/Lib/site-packages/_rocm_sdk_core/bin"),
            &root.join("regular"),
        ])
        .unwrap();
        let cleaned = clean_managed_rocm_path(
            Some(inherited.to_string_lossy().into_owned()),
            &[root.clone()],
        )
        .unwrap();
        assert_eq!(
            std::env::split_paths(&cleaned).collect::<Vec<_>>(),
            vec![root.join("regular")]
        );
    }

    #[test]
    fn pending_transactions_and_managed_bootstrap_paths_cannot_execute() {
        let fixture = tempfile::tempdir().unwrap();
        let root = fixture.path().canonicalize().unwrap();
        let scripts = root.join("rocm/Scripts");
        std::fs::create_dir_all(&scripts).unwrap();
        let python = scripts.join("python.exe");
        std::fs::write(&python, "stub").unwrap();
        let roots = std::slice::from_ref(&root);
        assert!(reject_pending_environment(&python, roots).is_ok());
        std::fs::write(root.join(".rocm-transaction.json"), "{}").unwrap();
        assert!(reject_pending_environment(&python, roots).is_err());
        assert!(reject_managed_bootstrap(&python, roots).is_err());
        let independent = root.parent().unwrap().join("bootstrap/python.exe");
        assert!(reject_managed_bootstrap(&independent, roots).is_ok());
    }

    #[cfg(unix)]
    #[test]
    fn symlink_native_directory_and_interpreter_cannot_escape_validation() {
        use std::os::unix::fs::symlink;
        let fixture = tempfile::tempdir().unwrap();
        let root = fixture.path().canonicalize().unwrap();
        let environment = root.join("rocm");
        std::fs::create_dir_all(&environment).unwrap();
        let outside = tempfile::tempdir().unwrap();
        symlink(outside.path(), environment.join("sdk")).unwrap();
        assert!(relative_directory(&environment, "sdk").is_err());
        let preserved = root.join(".rocm-previous/environment/python");
        std::fs::create_dir_all(preserved.parent().unwrap()).unwrap();
        std::fs::write(&preserved, "stub").unwrap();
        symlink(&preserved, environment.join("python")).unwrap();
        assert!(reject_reserved_interpreter(&environment.join("python"), &[root]).is_err());
    }
}
