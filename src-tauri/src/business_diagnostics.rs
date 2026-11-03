use crate::commands::{app_cmd::require_runtime_debug_developer_mode, plugin_host};
use crate::error::{AppError, AppResult};
use crate::{session_log, storage};
use serde_json::{json, Value};
use std::path::Path;
use tauri::{AppHandle, Emitter, Manager, State, WebviewWindow};

#[derive(Clone, Copy, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DiagnosticScope {
    Business,
    Rendering,
}

fn validate_request(request_id: &str) -> AppResult<()> {
    if request_id.is_empty()
        || request_id.len() > 64
        || !request_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
    {
        return Err(AppError::Worker(
            "Invalid diagnostic request identifier".into(),
        ));
    }
    Ok(())
}

#[tauri::command]
pub fn begin_business_diagnostics(
    app: AppHandle,
    request_id: String,
    scope: Option<DiagnosticScope>,
) -> AppResult<()> {
    require_runtime_debug_developer_mode(&app)?;
    validate_request(&request_id)?;
    session_log::begin_window_diagnostics(&request_id)?;
    let event = match scope.unwrap_or(DiagnosticScope::Business) {
        DiagnosticScope::Business => "pymss://diagnostic-state-request",
        DiagnosticScope::Rendering => "pymss://renderer-state-request",
    };
    app.emit(event, json!({"requestId": request_id}))
        .map_err(|error| AppError::Worker(error.to_string()))?;
    Ok(())
}

#[tauri::command]
pub fn publish_debug_window_state(
    app: AppHandle,
    window: WebviewWindow,
    request_id: String,
    snapshot: Value,
) -> AppResult<bool> {
    require_runtime_debug_developer_mode(&app)?;
    validate_request(&request_id)?;
    if !matches!(
        snapshot["kind"].as_str(),
        Some("workflow" | "editor" | "renderer")
    ) {
        return Err(AppError::Worker("Invalid diagnostic window kind".into()));
    }
    if app.get_webview_window(window.label()).is_none() {
        return Ok(false);
    }
    session_log::record_window_diagnostics(&request_id, window.label(), &snapshot)
}

#[tauri::command]
pub fn get_renderer_diagnostics(app: AppHandle, request_id: String) -> AppResult<Value> {
    require_runtime_debug_developer_mode(&app)?;
    validate_request(&request_id)?;
    let expected: Vec<_> = app.webview_windows().keys().cloned().collect();
    let (version, error) = match tauri::webview_version() {
        Ok(value) => (Some(value), None),
        Err(error) => (None, Some(error.to_string())),
    };
    Ok(
        json!({"checkedAt": chrono::Utc::now().to_rfc3339(), "requestId": request_id,
        "webviewVersion": version, "webviewError": error, "os": std::env::consts::OS, "arch": std::env::consts::ARCH,
        "windows": session_log::window_diagnostics(&request_id, &expected)}),
    )
}

#[tauri::command]
pub async fn get_business_diagnostics(
    app: AppHandle,
    state: State<'_, plugin_host::PluginHostState>,
    request_id: String,
) -> AppResult<Value> {
    require_runtime_debug_developer_mode(&app)?;
    validate_request(&request_id)?;
    let expected: Vec<_> = app
        .webview_windows()
        .keys()
        .filter(|label| {
            label.starts_with("editor-")
                || label.as_str() == "editor"
                || label.starts_with("workflow-node-editor")
                || label.starts_with("workflow-simple-editor")
        })
        .cloned()
        .collect();
    let windows = session_log::window_diagnostics(&request_id, &expected);
    let root = storage::data_root_dir(&app)?;
    let state = state.inner().clone();
    let native = tauri::async_runtime::spawn_blocking(move || {
        plugin_host::diagnostic_snapshot(&root, &state)
    })
    .await
    .map_err(|error| AppError::Worker(error.to_string()))?;
    Ok(
        json!({"checkedAt": chrono::Utc::now().to_rfc3339(), "requestId": request_id, "windows": windows, "nativeHost": native}),
    )
}

fn inspect_output(path: &str) -> Value {
    if !Path::new(path).is_absolute() {
        return json!({"path": path, "state": "unresolved"});
    }
    match std::fs::metadata(path) {
        Ok(metadata) if metadata.is_file() => {
            json!({"path": path, "state": "present", "bytes": metadata.len()})
        }
        Ok(_) => json!({"path": path, "state": "not_file"}),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            json!({"path": path, "state": "missing"})
        }
        Err(error) => json!({"path": path, "state": "unreadable", "error": error.to_string()}),
    }
}

#[tauri::command]
pub async fn inspect_diagnostic_outputs(app: AppHandle, paths: Vec<String>) -> AppResult<Value> {
    require_runtime_debug_developer_mode(&app)?;
    if paths.len() > 64
        || paths
            .iter()
            .any(|path| path.is_empty() || path.len() > 4096)
    {
        return Err(AppError::Worker(
            "Output checks require at most 64 recorded paths of up to 4096 bytes".into(),
        ));
    }
    let files = tauri::async_runtime::spawn_blocking(move || {
        paths
            .iter()
            .map(|path| inspect_output(path))
            .collect::<Vec<_>>()
    })
    .await
    .map_err(|error| AppError::Worker(error.to_string()))?;
    Ok(json!({"checkedAt": chrono::Utc::now().to_rfc3339(), "files": files}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn output_checks_distinguish_missing_files_directories_and_relative_paths() {
        let root = tempfile::tempdir().unwrap();
        let file = root.path().join("output.txt");
        std::fs::write(&file, "result").unwrap();
        assert_eq!(inspect_output(&file.to_string_lossy())["state"], "present");
        assert_eq!(
            inspect_output(&root.path().to_string_lossy())["state"],
            "not_file"
        );
        assert_eq!(
            inspect_output(&root.path().join("missing").to_string_lossy())["state"],
            "missing"
        );
        assert_eq!(inspect_output("relative.txt")["state"], "unresolved");
        assert_eq!(std::fs::read_to_string(file).unwrap(), "result");
    }
}
