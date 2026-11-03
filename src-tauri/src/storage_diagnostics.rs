use crate::commands::app_cmd::require_runtime_debug_developer_mode;
use crate::error::{AppError, AppResult};
use serde::Deserialize;
use serde_json::{json, Value};
use std::io::Write;
use std::path::Path;
use tauri::AppHandle;

#[derive(Deserialize)]
pub struct DirectoryRequest {
    id: String,
    path: String,
}

fn inspect_directory(path: &Path, write_probe: bool) -> Value {
    let mut result = json!({"path": path.to_string_lossy(), "writeStatus": "not_checked", "availableBytes": null});
    if !path.is_absolute() {
        result["state"] = "unresolved".into();
        return result;
    }
    result["availableBytes"] = json!(crate::model_dir_migration::disk_available_bytes(path));
    match std::fs::metadata(path) {
        Ok(metadata) if metadata.is_dir() => {
            result["state"] = "directory".into();
            result["readonlyAttribute"] = metadata.permissions().readonly().into();
        }
        Ok(_) => {
            result["state"] = "not_directory".into();
            return result;
        }
        Err(error) => {
            result["state"] = if error.kind() == std::io::ErrorKind::NotFound {
                "missing"
            } else {
                "unreadable"
            }
            .into();
            result["error"] = error.to_string().into();
            return result;
        }
    }
    if !write_probe {
        return result;
    }
    match tempfile::Builder::new()
        .prefix(".pymss-diagnostics-")
        .suffix(".tmp")
        .tempfile_in(path)
    {
        Ok(mut probe) => {
            let probe_path = probe.path().to_path_buf();
            let writing = probe
                .write_all(b"pymss storage probe\n")
                .and_then(|_| probe.flush());
            result["writeStatus"] = if writing.is_ok() { "ok" } else { "failed" }.into();
            if let Err(error) = writing {
                result["writeError"] = error.to_string().into();
            }
            if let Err(error) = probe.close() {
                result["writeStatus"] = "cleanup_failed".into();
                result["probePath"] = probe_path.to_string_lossy().into_owned().into();
                result["cleanupError"] = error.to_string().into();
            }
        }
        Err(error) => {
            result["writeStatus"] = "failed".into();
            result["writeError"] = error.to_string().into();
        }
    }
    result
}

#[tauri::command]
pub async fn inspect_diagnostic_storage(
    app: AppHandle,
    directories: Vec<DirectoryRequest>,
    write_probe: bool,
) -> AppResult<Value> {
    require_runtime_debug_developer_mode(&app)?;
    let allowed = [
        "data", "models", "outputs", "settings", "editor", "logs", "temp",
    ];
    if directories.len() > allowed.len()
        || directories
            .iter()
            .any(|entry| !allowed.contains(&entry.id.as_str()) || entry.path.len() > 4096)
    {
        return Err(AppError::Worker("Invalid diagnostic directory list".into()));
    }
    let rows = tauri::async_runtime::spawn_blocking(move || {
        directories
            .iter()
            .map(|entry| {
                let mut row = inspect_directory(Path::new(&entry.path), write_probe);
                row["id"] = entry.id.clone().into();
                row
            })
            .collect::<Vec<_>>()
    })
    .await
    .map_err(|error| AppError::Worker(error.to_string()))?;
    Ok(
        json!({"checkedAt": chrono::Utc::now().to_rfc3339(), "writeProbe": write_probe, "directories": rows}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn storage_reads_do_not_create_directories_and_explicit_probes_clean_up() {
        let root = tempfile::tempdir().unwrap();
        let absent = root.path().join("missing");
        assert_eq!(inspect_directory(&absent, true)["state"], "missing");
        assert!(!absent.exists());
        std::fs::write(root.path().join("existing"), "keep").unwrap();
        assert_eq!(
            inspect_directory(root.path(), false)["writeStatus"],
            "not_checked"
        );
        assert_eq!(inspect_directory(root.path(), true)["writeStatus"], "ok");
        assert_eq!(std::fs::read_dir(root.path()).unwrap().count(), 1);
        assert_eq!(
            std::fs::read_to_string(root.path().join("existing")).unwrap(),
            "keep"
        );
    }
    #[test]
    fn storage_reads_distinguish_files_and_unresolved_paths() {
        let root = tempfile::tempdir().unwrap();
        let file = root.path().join("file");
        std::fs::write(&file, "data").unwrap();
        assert_eq!(inspect_directory(&file, true)["state"], "not_directory");
        assert_eq!(
            inspect_directory(Path::new("relative"), false)["state"],
            "unresolved"
        );
    }
}
