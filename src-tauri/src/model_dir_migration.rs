use crate::error::{AppError, AppResult};
use crate::state::{AppState, SharedMigrationSession};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::sync::{Arc, Condvar, Mutex};
use tauri::{AppHandle, Emitter, State};

/// Returns available bytes for the partition containing `path`.
/// Walks up to find an existing ancestor. Returns None if unavailable.
pub(crate) fn disk_available_bytes(path: &Path) -> Option<u64> {
    let mut check = path;
    loop {
        if check.exists() {
            break;
        }
        check = check.parent()?;
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        #[link(name = "kernel32")]
        extern "system" {
            fn GetDiskFreeSpaceExW(
                lpDirectoryName: *const u16,
                lpFreeBytesAvailableToCaller: *mut u64,
                lpTotalNumberOfBytes: *mut u64,
                lpTotalNumberOfFreeBytes: *mut u64,
            ) -> i32;
        }
        let wide: Vec<u16> = check.as_os_str().encode_wide().chain(Some(0)).collect();
        let mut free: u64 = 0;
        let ok = unsafe { GetDiskFreeSpaceExW(wide.as_ptr(), &mut free, &mut 0u64, &mut 0u64) };
        if ok != 0 { Some(free) } else { None }
    }
    #[cfg(not(windows))]
    {
        // `df -Pk` is POSIX-compatible and works on both GNU/Linux and macOS/BSD.
        // The `Available` column is reported in 1024-byte blocks.
        let output = std::process::Command::new("df")
            .arg("-Pk")
            .arg(check)
            .output()
            .ok()?;
        if !output.status.success() {
            return None;
        }
        let text = String::from_utf8_lossy(&output.stdout);
        let available_blocks = text
            .lines()
            .nth(1)?
            .split_whitespace()
            .nth(3)?
            .parse::<u64>()
            .ok()?;
        available_blocks.checked_mul(1024)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelDirMigrationFilePayload {
    pub source_path: String,
    pub relative_path: String,
    pub size_bytes: u64,
    pub kind: ModelDirMigrationFileKind,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub enum ModelDirMigrationFileKind {
    File,
    Symlink,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelDirMigrationConflictPayload {
    pub source_path: String,
    pub relative_path: String,
    pub destination_path: String,
    pub existing_size_bytes: u64,
    pub incoming_size_bytes: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PrepareModelDirChangeRequest {
    pub current_model_dir: String,
    pub target_model_dir: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PrepareModelDirChangePayload {
    pub current_model_dir: String,
    pub target_model_dir: String,
    pub same_as_current: bool,
    pub source_dir_exists: bool,
    pub target_dir_exists: bool,
    pub target_dir_empty: bool,
    pub file_count: usize,
    pub total_bytes: u64,
    pub conflict_count: usize,
    pub disk_available_bytes: Option<u64>,
    pub disk_insufficient: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct StartModelDirMigrationRequest {
    pub task_id: Option<String>,
    pub current_model_dir: String,
    pub target_model_dir: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RespondModelDirMigrationConflictRequest {
    pub task_id: String,
    pub resolution: ModelDirMigrationConflictResolution,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ConfirmModelDirMigrationSwitchRequest {
    pub task_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CancelModelDirMigrationRequest {
    pub task_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ModelDirMigrationConflictResolution {
    Overwrite,
    Skip,
    Abort,
}

#[derive(Debug, Clone)]
struct FinalizeMigrationPayload {
    source_model_dir: String,
    target_model_dir: String,
    total_files: usize,
    total_bytes: u64,
    copied_bytes: u64,
    skipped_files: Vec<String>,
    overwritten_files: Vec<String>,
}

#[derive(Debug, Default)]
struct MigrationSessionState {
    pending_conflict: Option<ModelDirMigrationConflictPayload>,
    resolution: Option<ModelDirMigrationConflictResolution>,
    finalize_payload: Option<FinalizeMigrationPayload>,
}

#[derive(Debug, Default)]
pub struct ModelDirMigrationSession {
    state: Mutex<MigrationSessionState>,
    condvar: Condvar,
}

impl ModelDirMigrationSession {
    pub fn resolve_conflict(&self, resolution: ModelDirMigrationConflictResolution) -> bool {
        let mut guard = self.state.lock().expect("lock migration conflict state");
        if guard.pending_conflict.is_none() {
            return false;
        }
        guard.resolution = Some(resolution);
        self.condvar.notify_all();
        true
    }

    fn wait_for_resolution(
        &self,
        conflict: ModelDirMigrationConflictPayload,
    ) -> ModelDirMigrationConflictResolution {
        let mut guard = self.state.lock().expect("lock migration conflict state");
        guard.pending_conflict = Some(conflict);
        guard.resolution = None;
        while guard.resolution.is_none() {
            guard = self
                .condvar
                .wait(guard)
                .expect("wait for migration conflict resolution");
        }
        let resolution = guard
            .resolution
            .take()
            .unwrap_or(ModelDirMigrationConflictResolution::Abort);
        guard.pending_conflict = None;
        resolution
    }

    fn store_finalize_payload(&self, payload: FinalizeMigrationPayload) {
        let mut guard = self.state.lock().expect("lock migration finalize state");
        guard.finalize_payload = Some(payload);
    }

    fn take_finalize_payload(&self) -> Option<FinalizeMigrationPayload> {
        let mut guard = self.state.lock().expect("lock migration finalize state");
        guard.finalize_payload.take()
    }

    fn cancel(&self) {
        let mut guard = self.state.lock().expect("lock migration cancel state");
        guard.finalize_payload = None;
        if guard.pending_conflict.is_some() {
            guard.resolution = Some(ModelDirMigrationConflictResolution::Abort);
            self.condvar.notify_all();
        }
    }
}

fn emit_model_dir_migration_event(
    app: &AppHandle,
    task_id: &str,
    event_type: &str,
    payload: Value,
) {
    let _ = app.emit(
        "pymss://worker-event",
        json!({
            "type": event_type,
            "requestId": Value::Null,
            "taskId": task_id,
            "timestamp": chrono_like_timestamp(),
            "payload": payload,
        }),
    );
}

fn chrono_like_timestamp() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_millis())
        .unwrap_or_default()
}

fn lexical_normalize_path(path: PathBuf) -> PathBuf {
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                if !normalized.pop() {
                    normalized.push(component.as_os_str());
                }
            }
            Component::Prefix(_) | Component::RootDir | Component::Normal(_) => {
                normalized.push(component.as_os_str());
            }
        }
    }
    normalized
}

fn normalize_directory_path(path: &str) -> AppResult<PathBuf> {
    let trimmed = path.trim();
    if trimmed.is_empty() {
        return Err(AppError::Worker("directory path is empty".into()));
    }
    let candidate = PathBuf::from(trimmed);
    let absolute = if candidate.is_absolute() {
        candidate
    } else {
        std::env::current_dir()?.join(candidate)
    };
    if absolute.exists() {
        Ok(absolute.canonicalize()?)
    } else {
        Ok(lexical_normalize_path(absolute))
    }
}

fn normalize_target_model_dir_path(path: &str) -> AppResult<PathBuf> {
    let normalized = normalize_directory_path(path)?;
    let leaf = normalized
        .file_name()
        .map(|value| value.to_string_lossy().to_string())
        .unwrap_or_default();
    let is_models_dir = if cfg!(windows) {
        leaf.eq_ignore_ascii_case("models")
    } else {
        leaf == "models"
    };
    if is_models_dir {
        Ok(normalized)
    } else {
        Ok(normalized.join("models"))
    }
}

fn path_eq(left: &Path, right: &Path) -> bool {
    #[cfg(windows)]
    {
        left.to_string_lossy()
            .replace('/', "\\")
            .eq_ignore_ascii_case(&right.to_string_lossy().replace('/', "\\"))
    }
    #[cfg(not(windows))]
    {
        left == right
    }
}

fn display_path(path: &Path) -> String {
    let value = path.to_string_lossy().to_string();
    #[cfg(windows)]
    {
        if let Some(rest) = value.strip_prefix("\\\\?\\UNC\\") {
            return format!("\\\\{}", rest);
        }
        if let Some(rest) = value.strip_prefix("\\\\?\\") {
            return rest.to_string();
        }
    }
    value
}

fn canonical_relative_path(base: &Path, full: &Path) -> String {
    let relative = full
        .strip_prefix(base)
        .ok()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from(full.file_name().unwrap_or_default()));
    relative
        .components()
        .filter_map(|component| match component {
            Component::Normal(value) => Some(value.to_string_lossy().to_string()),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join("/")
}

fn path_is_ancestor_of(ancestor: &Path, descendant: &Path) -> bool {
    let mut cur = descendant;
    while let Some(parent) = cur.parent() {
        if path_eq(parent, ancestor) {
            return true;
        }
        cur = parent;
    }
    false
}

fn target_dir_empty(path: &Path) -> AppResult<bool> {
    if !path.exists() {
        return Ok(true);
    }
    Ok(fs::read_dir(path)?.next().is_none())
}

fn collect_directory_files(root: &Path) -> AppResult<Vec<ModelDirMigrationFilePayload>> {
    fn walk(
        root: &Path,
        dir: &Path,
        files: &mut Vec<ModelDirMigrationFilePayload>,
    ) -> AppResult<()> {
        for entry in fs::read_dir(dir)? {
            let entry = entry?;
            let path = entry.path();
            let file_type = entry.file_type()?;
            if file_type.is_dir() {
                walk(root, &path, files)?;
                continue;
            }
            let (size_bytes, kind) = if file_type.is_file() {
                (entry.metadata()?.len(), ModelDirMigrationFileKind::File)
            } else if file_type.is_symlink() {
                // Keep symlinks as symlinks during migration. Use the link metadata
                // size for progress/space estimation; do not follow it here.
                (fs::symlink_metadata(&path)?.len(), ModelDirMigrationFileKind::Symlink)
            } else {
                continue;
            };
            let relative_path = canonical_relative_path(root, &path);
            if relative_path.is_empty() {
                continue;
            }
            files.push(ModelDirMigrationFilePayload {
                source_path: display_path(&path),
                relative_path,
                size_bytes,
                kind,
            });
        }
        Ok(())
    }

    if !root.exists() {
        return Ok(Vec::new());
    }
    if !root.is_dir() {
        return Err(AppError::Worker(format!(
            "模型目录不是文件夹：{}",
            root.display()
        )));
    }

    let mut files = Vec::new();
    walk(root, root, &mut files)?;
    files.sort_by(|left, right| left.relative_path.cmp(&right.relative_path));
    Ok(files)
}

fn collect_directory_dirs(root: &Path) -> AppResult<Vec<String>> {
    fn walk(root: &Path, dir: &Path, dirs: &mut Vec<String>) -> AppResult<()> {
        for entry in fs::read_dir(dir)? {
            let entry = entry?;
            let path = entry.path();
            let file_type = entry.file_type()?;
            if !file_type.is_dir() {
                continue;
            }
            let relative_path = canonical_relative_path(root, &path);
            if !relative_path.is_empty() {
                dirs.push(relative_path);
            }
            walk(root, &path, dirs)?;
        }
        Ok(())
    }

    if !root.exists() {
        return Ok(Vec::new());
    }
    if !root.is_dir() {
        return Err(AppError::Worker(format!(
            "模型目录不是文件夹：{}",
            root.display()
        )));
    }

    let mut dirs = Vec::new();
    walk(root, root, &mut dirs)?;
    dirs.sort();
    Ok(dirs)
}

fn count_conflicts(
    files: &[ModelDirMigrationFilePayload],
    target_model_dir: &Path,
) -> usize {
    files
        .iter()
        .filter(|file| {
            let destination = target_model_dir.join(Path::new(&file.relative_path));
            destination.exists() && !path_eq(Path::new(&file.source_path), &destination)
        })
        .count()
}

fn collect_remaining_paths(path: &Path, results: &mut Vec<String>) {
    if !path.exists() {
        return;
    }
    if path.is_file() {
        results.push(display_path(path));
        return;
    }
    let entries = match fs::read_dir(path) {
        Ok(entries) => entries,
        Err(_) => {
            results.push(display_path(path));
            return;
        }
    };
    for entry in entries.flatten() {
        let child = entry.path();
        if child.is_dir() {
            collect_remaining_paths(&child, results);
        } else {
            results.push(display_path(&child));
        }
    }
    if results.is_empty() {
        results.push(display_path(path));
    }
}

fn cleanup_source_tree(source_root: &Path) -> Vec<String> {
    if !source_root.exists() {
        return Vec::new();
    }
    if fs::remove_dir_all(source_root).is_ok() {
        return Vec::new();
    }
    let mut remaining = Vec::new();
    collect_remaining_paths(source_root, &mut remaining);
    remaining.sort();
    remaining.dedup();
    remaining
}

fn remove_existing_destination(destination: &Path) -> AppResult<()> {
    let metadata = fs::symlink_metadata(destination)?;
    if metadata.is_dir() && !metadata.file_type().is_symlink() {
        fs::remove_dir_all(destination)?;
    } else {
        fs::remove_file(destination)?;
    }
    Ok(())
}

#[cfg(unix)]
fn copy_symlink(source: &Path, destination: &Path) -> AppResult<()> {
    use std::os::unix::fs::symlink;
    let target = fs::read_link(source)?;
    symlink(target, destination)?;
    Ok(())
}

#[cfg(windows)]
fn copy_symlink(source: &Path, destination: &Path) -> AppResult<()> {
    use std::os::windows::fs::{symlink_dir, symlink_file};
    let target = fs::read_link(source)?;
    let target_metadata = fs::metadata(source).ok();
    let is_dir = target_metadata.as_ref().map(|m| m.is_dir()).unwrap_or(false);
    let result = if is_dir {
        symlink_dir(&target, destination)
    } else {
        symlink_file(&target, destination)
    };
    if result.is_err() {
        // Symlink creation requires SeCreateSymbolicLinkPrivilege or Developer Mode.
        // Fall back to copying the file content so migration can still succeed.
        if is_dir {
            return Err(AppError::Worker(format!(
                "无法创建目录符号链接（权限不足），已跳过：{}",
                destination.display()
            )));
        }
        fs::copy(source, destination)?;
    }
    Ok(())
}

fn copy_entry(source: &Path, destination: &Path, kind: ModelDirMigrationFileKind, overwrite: bool) -> AppResult<()> {
    if let Some(parent) = destination.parent() {
        fs::create_dir_all(parent)?;
    }
    if overwrite && fs::symlink_metadata(destination).is_ok() {
        remove_existing_destination(destination)?;
    }
    match kind {
        ModelDirMigrationFileKind::File => {
            fs::copy(source, destination)?;
        }
        ModelDirMigrationFileKind::Symlink => {
            copy_symlink(source, destination)?;
        }
    }
    Ok(())
}

pub fn prepare_model_dir_change(
    _app: &AppHandle,
    request: PrepareModelDirChangeRequest,
) -> AppResult<PrepareModelDirChangePayload> {
    let current_model_dir = normalize_directory_path(&request.current_model_dir)?;
    let target_model_dir = normalize_target_model_dir_path(&request.target_model_dir)?;
    let same_as_current = path_eq(&current_model_dir, &target_model_dir);

    if path_is_ancestor_of(&current_model_dir, &target_model_dir) {
        return Err(AppError::Worker(format!(
            "目标目录位于源目录内部，禁止迁移（源：{}，目标：{}）",
            display_path(&current_model_dir),
            display_path(&target_model_dir)
        )));
    }
    if path_is_ancestor_of(&target_model_dir, &current_model_dir) {
        return Err(AppError::Worker(format!(
            "源目录位于目标目录内部，禁止迁移（源：{}，目标：{}）",
            display_path(&current_model_dir),
            display_path(&target_model_dir)
        )));
    }

    let source_dir_exists = current_model_dir.exists();
    if source_dir_exists && !current_model_dir.is_dir() {
        return Err(AppError::Worker(format!(
            "模型目录不是文件夹：{}",
            current_model_dir.display()
        )));
    }

    let files = if same_as_current || !source_dir_exists {
        Vec::new()
    } else {
        collect_directory_files(&current_model_dir)?
    };
    let total_bytes = files.iter().map(|item| item.size_bytes).sum::<u64>();
    let conflict_count = if same_as_current {
        0
    } else {
        count_conflicts(&files, &target_model_dir)
    };

    let disk_available = disk_available_bytes(&target_model_dir);
    let disk_insufficient = match disk_available {
        Some(avail) => total_bytes > 0 && avail < (total_bytes as f64 * 1.1) as u64,
        None => false,
    };

    Ok(PrepareModelDirChangePayload {
        current_model_dir: display_path(&current_model_dir),
        target_model_dir: display_path(&target_model_dir),
        same_as_current,
        source_dir_exists,
        target_dir_exists: target_model_dir.exists(),
        target_dir_empty: target_dir_empty(&target_model_dir)?,
        file_count: files.len(),
        total_bytes,
        conflict_count,
        disk_available_bytes: disk_available,
        disk_insufficient,
    })
}

fn build_conflict_payload(
    file: &ModelDirMigrationFilePayload,
    destination: &Path,
) -> ModelDirMigrationConflictPayload {
    ModelDirMigrationConflictPayload {
        source_path: file.source_path.clone(),
        relative_path: file.relative_path.clone(),
        destination_path: display_path(destination),
        existing_size_bytes: fs::symlink_metadata(destination).map(|meta| meta.len()).unwrap_or_default(),
        incoming_size_bytes: file.size_bytes,
    }
}

fn run_model_dir_migration(
    app: AppHandle,
    task_id: String,
    session: SharedMigrationSession,
    request: StartModelDirMigrationRequest,
) -> AppResult<()> {
    let source_root = normalize_directory_path(&request.current_model_dir)?;
    let target_root = normalize_target_model_dir_path(&request.target_model_dir)?;
    let files = collect_directory_files(&source_root)?;
    let dirs = collect_directory_dirs(&source_root)?;
    let total_files = files.len();
    let total_bytes = files.iter().map(|item| item.size_bytes).sum::<u64>();

    fs::create_dir_all(&target_root)?;
    for dir in &dirs {
        fs::create_dir_all(target_root.join(Path::new(dir)))?;
    }
    emit_model_dir_migration_event(
        &app,
        &task_id,
        "model_dir_migration_started",
        json!({
            "sourceModelDir": display_path(&source_root),
            "targetModelDir": display_path(&target_root),
            "totalFiles": total_files,
            "completedFiles": 0,
            "totalBytes": total_bytes,
            "copiedBytes": 0u64,
            "currentPath": Value::Null,
            "message": "正在复制模型目录",
        }),
    );

    let mut copied_bytes = 0u64;
    let mut completed_files = 0usize;
    let mut skipped_files = Vec::new();
    let mut overwritten_files = Vec::new();
    let mut default_resolution: Option<ModelDirMigrationConflictResolution> = None;

    for file in &files {
        let source = PathBuf::from(&file.source_path);
        let destination = target_root.join(Path::new(&file.relative_path));
        if fs::symlink_metadata(&source).is_err() {
            return Err(AppError::Worker(format!("源文件不存在：{}", source.display())));
        }

        let has_destination = fs::symlink_metadata(&destination).is_ok();
        let mut should_overwrite = false;
        if has_destination && !path_eq(&source, &destination) {
            let resolution = if let Some(choice) = &default_resolution {
                choice.clone()
            } else {
                let conflict = build_conflict_payload(file, &destination);
                emit_model_dir_migration_event(
                    &app,
                    &task_id,
                    "model_dir_migration_conflict",
                    json!({
                        "sourceModelDir": display_path(&source_root),
                        "targetModelDir": display_path(&target_root),
                        "totalFiles": total_files,
                        "completedFiles": completed_files,
                        "totalBytes": total_bytes,
                        "copiedBytes": copied_bytes,
                        "currentPath": file.source_path,
                        "conflict": conflict,
                        "message": "目标目录存在同名文件，请选择处理方式",
                    }),
                );
                let response = session.wait_for_resolution(conflict);
                if response != ModelDirMigrationConflictResolution::Abort {
                    default_resolution = Some(response.clone());
                }
                response
            };

            match resolution {
                ModelDirMigrationConflictResolution::Overwrite => {
                    overwritten_files.push(display_path(&destination));
                    should_overwrite = true;
                }
                ModelDirMigrationConflictResolution::Skip => {
                    skipped_files.push(display_path(&destination));
                    completed_files += 1;
                    emit_model_dir_migration_event(
                        &app,
                        &task_id,
                        "model_dir_migration_progress",
                        json!({
                            "sourceModelDir": display_path(&source_root),
                            "targetModelDir": display_path(&target_root),
                            "totalFiles": total_files,
                            "completedFiles": completed_files,
                            "totalBytes": total_bytes,
                            "copiedBytes": copied_bytes,
                            "currentPath": file.source_path,
                            "message": format!("已跳过冲突文件：{}", file.relative_path),
                            "skippedFiles": skipped_files,
                            "overwrittenFiles": overwritten_files,
                        }),
                    );
                    continue;
                }
                ModelDirMigrationConflictResolution::Abort => {
                    return Err(AppError::Worker("__user_aborted__".into()));
                }
            }
        }

        copy_entry(&source, &destination, file.kind, should_overwrite)?;
        completed_files += 1;
        copied_bytes = copied_bytes.saturating_add(file.size_bytes);
        emit_model_dir_migration_event(
            &app,
            &task_id,
            "model_dir_migration_progress",
            json!({
                "sourceModelDir": display_path(&source_root),
                "targetModelDir": display_path(&target_root),
                "totalFiles": total_files,
                "completedFiles": completed_files,
                "totalBytes": total_bytes,
                "copiedBytes": copied_bytes,
                "currentPath": file.source_path,
                "message": format!("正在复制 {}", file.relative_path),
                "skippedFiles": skipped_files,
                "overwrittenFiles": overwritten_files,
            }),
        );
    }

    session.store_finalize_payload(FinalizeMigrationPayload {
        source_model_dir: display_path(&source_root),
        target_model_dir: display_path(&target_root),
        total_files,
        total_bytes,
        copied_bytes,
        skipped_files: skipped_files.clone(),
        overwritten_files: overwritten_files.clone(),
    });

    emit_model_dir_migration_event(
        &app,
        &task_id,
        "model_dir_migration_ready_to_switch",
        json!({
            "sourceModelDir": display_path(&source_root),
            "targetModelDir": display_path(&target_root),
            "totalFiles": total_files,
            "completedFiles": completed_files,
            "totalBytes": total_bytes,
            "copiedBytes": copied_bytes,
            "currentPath": Value::Null,
            "message": "复制完成，正在切换模型目录",
            "skippedFiles": skipped_files,
            "overwrittenFiles": overwritten_files,
        }),
    );
    Ok(())
}

pub fn start_model_dir_migration(
    app: AppHandle,
    state: State<'_, AppState>,
    request: StartModelDirMigrationRequest,
) -> AppResult<Value> {
    let application_access = crate::python::worker::acquire_application_operation()?;
    let task_id = request
        .task_id
        .clone()
        .unwrap_or_else(|| format!("model_dir_migration_{}", chrono_like_timestamp()));
    let session = Arc::new(ModelDirMigrationSession::default());
    let migrations = state.migrations.clone();
    migrations
        .lock()
        .map_err(|_| AppError::Worker("failed to lock migrations state".into()))?
        .insert(task_id.clone(), session.clone());

    let response_task_id = task_id.clone();
    let cleanup_task_id = task_id.clone();
    let failed_source_dir = request.current_model_dir.clone();
    let failed_target_dir = request.target_model_dir.clone();
    std::thread::spawn(move || {
        let _application_access = application_access;
        let result = run_model_dir_migration(app.clone(), task_id.clone(), session, request);
        if let Err(error) = result {
            let msg = error.to_string();
            if msg == "__user_aborted__" {
                emit_model_dir_migration_event(
                    &app,
                    &task_id,
                    "model_dir_migration_aborted",
                    json!({
                        "message": "用户终止了模型目录迁移",
                        "sourceModelDir": failed_source_dir,
                        "targetModelDir": failed_target_dir,
                    }),
                );
            } else {
                emit_model_dir_migration_event(
                    &app,
                    &task_id,
                    "model_dir_migration_failed",
                    json!({
                        "message": msg,
                        "sourceModelDir": failed_source_dir,
                        "targetModelDir": failed_target_dir,
                    }),
                );
            }
            let _ = migrations
                .lock()
                .map(|mut map| map.remove(&cleanup_task_id));
        }
    });

    Ok(json!({
        "taskId": response_task_id,
        "started": true,
    }))
}

pub fn respond_model_dir_migration_conflict(
    state: State<'_, AppState>,
    request: RespondModelDirMigrationConflictRequest,
) -> AppResult<Value> {
    let migrations = state
        .migrations
        .lock()
        .map_err(|_| AppError::Worker("failed to lock migrations state".into()))?;
    let session = migrations
        .get(&request.task_id)
        .cloned()
        .ok_or_else(|| AppError::Worker("migration task not found".into()))?;
    drop(migrations);
    if !session.resolve_conflict(request.resolution.clone()) {
        return Err(AppError::Worker(
            "migration task is not waiting for conflict resolution".into(),
        ));
    }
    Ok(json!({
        "taskId": request.task_id,
        "accepted": true,
        "resolution": request.resolution,
    }))
}

pub fn confirm_model_dir_migration_switch(
    app: AppHandle,
    state: State<'_, AppState>,
    request: ConfirmModelDirMigrationSwitchRequest,
) -> AppResult<Value> {
    let session = {
        let migrations = state
            .migrations
            .lock()
            .map_err(|_| AppError::Worker("failed to lock migrations state".into()))?;
        migrations
            .get(&request.task_id)
            .cloned()
            .ok_or_else(|| AppError::Worker("migration task not found".into()))?
    };

    let finalize = session
        .take_finalize_payload()
        .ok_or_else(|| AppError::Worker("migration task is not ready to finalize".into()))?;

    let cleanup_failed_files = cleanup_source_tree(Path::new(&finalize.source_model_dir));
    emit_model_dir_migration_event(
        &app,
        &request.task_id,
        "model_dir_migration_done",
        json!({
            "sourceModelDir": finalize.source_model_dir,
            "targetModelDir": finalize.target_model_dir,
            "totalFiles": finalize.total_files,
            "completedFiles": finalize.total_files,
            "totalBytes": finalize.total_bytes,
            "copiedBytes": finalize.copied_bytes,
            "currentPath": Value::Null,
            "message": if cleanup_failed_files.is_empty() {
                "模型目录迁移完成"
            } else {
                "模型目录已切换，但旧目录仍有残留需要手动清理"
            },
            "skippedFiles": finalize.skipped_files,
            "overwrittenFiles": finalize.overwritten_files,
            "cleanupFailedFiles": cleanup_failed_files,
        }),
    );

    let _ = state
        .migrations
        .lock()
        .map(|mut map| map.remove(&request.task_id));

    Ok(json!({
        "taskId": request.task_id,
        "accepted": true,
    }))
}

pub fn cancel_model_dir_migration(
    state: State<'_, AppState>,
    request: CancelModelDirMigrationRequest,
) -> AppResult<Value> {
    let session = {
        let mut migrations = state
            .migrations
            .lock()
            .map_err(|_| AppError::Worker("failed to lock migrations state".into()))?;
        migrations
            .remove(&request.task_id)
            .ok_or_else(|| AppError::Worker("migration task not found".into()))?
    };
    session.cancel();
    Ok(json!({
        "taskId": request.task_id,
        "accepted": true,
    }))
}
