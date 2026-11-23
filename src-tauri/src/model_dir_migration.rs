use crate::error::{AppError, AppResult};
use crate::python::worker::run_worker_with_payload;
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
    copied_files: Vec<CopiedModelFile>,
    source_dirs: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ModelFileSnapshot {
    size: u64,
    modified: std::time::SystemTime,
    link_target: Option<PathBuf>,
}

impl ModelFileSnapshot {
    fn read(path: &Path) -> std::io::Result<Self> {
        let metadata = fs::symlink_metadata(path)?;
        let link_target = if metadata.file_type().is_symlink() {
            Some(fs::read_link(path)?)
        } else if metadata.is_file() {
            None
        } else {
            return Err(std::io::Error::other("Expected a model file or link"));
        };
        Ok(Self {
            size: metadata.len(),
            modified: metadata.modified()?,
            link_target,
        })
    }

    fn matches(&self, path: &Path) -> bool {
        Self::read(path).is_ok_and(|current| current == *self)
    }
}

#[derive(Debug, Clone)]
struct CopiedModelFile {
    relative_path: String,
    source: ModelFileSnapshot,
    destination: ModelFileSnapshot,
}

impl CopiedModelFile {
    fn is_current(&self, source_root: &Path, target_root: &Path) -> bool {
        let source = source_root.join(&self.relative_path);
        let destination = target_root.join(&self.relative_path);
        validate_migration_directory(source_root, source.parent().unwrap_or(source_root)).is_ok()
            && validate_migration_directory(target_root, destination.parent().unwrap_or(target_root)).is_ok()
            && self.source.matches(&source)
            && self.destination.matches(&destination)
    }
}

#[derive(Debug, Default)]
struct MigrationSessionState {
    pending_conflict: Option<ModelDirMigrationConflictPayload>,
    resolution: Option<ModelDirMigrationConflictResolution>,
    finalize_payload: Option<FinalizeMigrationPayload>,
}

#[derive(Debug, Default)]
pub struct ModelDirMigrationSession {
    #[cfg(any(windows, target_os = "macos", target_os = "linux"))]
    _cli_access: Option<std::fs::File>,
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

fn absolute_directory_path(path: &str) -> AppResult<PathBuf> {
    let trimmed = path.trim();
    if trimmed.is_empty() {
        return Err(AppError::Worker("directory path is empty".into()));
    }
    let candidate = PathBuf::from(trimmed);
    Ok(if candidate.is_absolute() {
        candidate
    } else {
        std::env::current_dir()?.join(candidate)
    })
}

fn normalize_directory_path(path: &str) -> AppResult<PathBuf> {
    let absolute = absolute_directory_path(path)?;
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
    let target = if is_models_dir {
        normalized
    } else {
        normalized.join("models")
    };
    validate_migration_directory(&target, &target)?;
    Ok(target)
}

fn path_eq(left: &Path, right: &Path) -> bool {
    #[cfg(windows)]
    {
        display_path(left)
            .replace('/', "\\")
            .eq_ignore_ascii_case(&display_path(right).replace('/', "\\"))
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

fn validate_migration_directory(root: &Path, directory: &Path) -> AppResult<()> {
    if path_eq(root, directory) || path_is_ancestor_of(root, directory) {
        for ancestor in directory.ancestors() {
            match fs::symlink_metadata(ancestor) {
                Ok(metadata) => {
                    if metadata.is_dir() && path_eq(&ancestor.canonicalize()?, ancestor) {
                        return Ok(());
                    }
                    break;
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error.into()),
            }
        }
    }
    Err(AppError::Worker(format!(
        "模型迁移无法使用链接目录：{}",
        display_path(directory)
    )))
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
        validate_migration_directory(root, dir)?;
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
        validate_migration_directory(root, dir)?;
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
) -> AppResult<usize> {
    let mut count = 0;
    for file in files {
        let destination = target_model_dir.join(&file.relative_path);
        validate_migration_directory(target_model_dir, destination.parent().unwrap_or(target_model_dir))?;
        count += usize::from(migration_destination_exists(&destination)?);
    }
    Ok(count)
}

fn collect_remaining_paths(path: &Path, results: &mut Vec<String>) {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_dir() && validate_migration_directory(path, path).is_ok() => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return,
        _ => {
            results.push(display_path(path));
            return;
        }
    }
    let entries = match fs::read_dir(path) {
        Ok(entries) => entries,
        Err(_) => {
            results.push(display_path(path));
            return;
        }
    };
    let before = results.len();
    for entry in entries {
        match entry {
            Ok(entry) => collect_remaining_paths(&entry.path(), results),
            Err(_) => results.push(display_path(path)),
        }
    }
    if results.len() == before {
        results.push(display_path(path));
    }
}

fn finalize_migrated_files(
    source_root: &Path,
    target_root: &Path,
    copied: &[CopiedModelFile],
    dirs: &[String],
    remap: impl FnOnce(Vec<&str>) -> AppResult<()>,
) -> AppResult<Vec<String>> {
    let ready: Vec<_> = copied.iter().filter(|file| file.is_current(source_root, target_root)).collect();
    if !ready.is_empty() {
        remap(ready.iter().map(|file| file.relative_path.as_str()).collect())?;
    }
    for file in ready {
        if file.is_current(source_root, target_root) {
            let _ = remove_migrated_file(&source_root.join(&file.relative_path));
        }
    }
    let source_dirs = dirs.iter().rev().map(|dir| source_root.join(dir));
    for directory in source_dirs.chain(std::iter::once(source_root.to_path_buf())) {
        if validate_migration_directory(source_root, &directory).is_ok() {
            let _ = fs::remove_dir(directory);
        }
    }
    let mut remaining = Vec::new();
    collect_remaining_paths(source_root, &mut remaining);
    remaining.sort();
    remaining.dedup();
    Ok(remaining)
}

fn remove_migrated_file(path: &Path) -> std::io::Result<()> {
    #[cfg(windows)]
    {
        use std::os::windows::fs::FileTypeExt;
        if fs::symlink_metadata(path)?.file_type().is_symlink_dir() {
            return fs::remove_dir(path);
        }
    }
    fs::remove_file(path)
}

fn migration_destination_exists(destination: &Path) -> AppResult<bool> {
    match fs::symlink_metadata(destination) {
        Ok(metadata) if metadata.is_dir() || destination.is_dir() => Err(AppError::Worker(format!(
            "模型文件与目标文件夹同名：{}",
            display_path(destination)
        ))),
        Ok(_) => Ok(true),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error.into()),
    }
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
    let (Some(parent), Some(name)) = (destination.parent(), destination.file_name()) else {
        return Err(AppError::Worker("无效的模型迁移目标路径".into()));
    };
    fs::create_dir_all(parent)?;
    if migration_destination_exists(destination)? && !overwrite {
        return Err(AppError::Worker(format!("目标文件已存在：{}", display_path(destination))));
    }
    // Stage each entry on the destination filesystem and publish it only after copying succeeds.
    let parent = parent.canonicalize()?;
    let destination = parent.join(name);
    let staging = tempfile::tempdir_in(&parent)?;
    let staged_path = staging.path().join("entry");
    match kind {
        ModelDirMigrationFileKind::File => {
            fs::copy(source, &staged_path)?;
        }
        ModelDirMigrationFileKind::Symlink => {
            copy_symlink(source, &staged_path)?;
        }
    }
    let staged_path = tempfile::TempPath::try_from_path(staged_path)?;
    if overwrite {
        staged_path.persist(&destination).map_err(|error| error.error)?;
    } else {
        staged_path.persist_noclobber(&destination).map_err(|error| error.error)?;
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
        count_conflicts(&files, &target_model_dir)?
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

fn migration_roots_for_start(request: &StartModelDirMigrationRequest) -> AppResult<(PathBuf, PathBuf)> {
    // The request contains the paths already shown by preparation. A new link must not redirect them.
    let source = lexical_normalize_path(absolute_directory_path(&request.current_model_dir)?);
    let target = lexical_normalize_path(absolute_directory_path(&request.target_model_dir)?);
    validate_migration_directory(&source, &source)?;
    validate_migration_directory(&target, &target)?;
    if path_eq(&source, &target) || path_is_ancestor_of(&source, &target) || path_is_ancestor_of(&target, &source) {
        return Err(AppError::Worker("模型迁移的源目录与目标目录不能重叠".into()));
    }
    Ok((source, target))
}

fn run_model_dir_migration(
    app: AppHandle,
    task_id: String,
    session: SharedMigrationSession,
    request: StartModelDirMigrationRequest,
) -> AppResult<()> {
    let (source_root, target_root) = migration_roots_for_start(&request)?;
    let files = collect_directory_files(&source_root)?;
    let dirs = collect_directory_dirs(&source_root)?;
    let total_files = files.len();
    let total_bytes = files.iter().map(|item| item.size_bytes).sum::<u64>();

    count_conflicts(&files, &target_root)?;
    for dir in &dirs {
        validate_migration_directory(&target_root, &target_root.join(dir))?;
    }
    fs::create_dir_all(&target_root)?;
    validate_migration_directory(&target_root, &target_root)?;
    for dir in &dirs {
        let directory = target_root.join(dir);
        validate_migration_directory(&target_root, &directory)?;
        fs::create_dir_all(directory)?;
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
    let mut copied_files = Vec::new();
    let mut default_resolution: Option<ModelDirMigrationConflictResolution> = None;

    for file in &files {
        let source = source_root.join(&file.relative_path);
        let destination = target_root.join(Path::new(&file.relative_path));
        if fs::symlink_metadata(&source).is_err() {
            return Err(AppError::Worker(format!("源文件不存在：{}", source.display())));
        }

        validate_migration_directory(&target_root, destination.parent().unwrap_or(&target_root))?;
        let has_destination = migration_destination_exists(&destination)?;
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

        validate_migration_directory(&source_root, source.parent().unwrap_or(&source_root))?;
        validate_migration_directory(&target_root, destination.parent().unwrap_or(&target_root))?;
        let source_snapshot = ModelFileSnapshot::read(&source)?;
        copy_entry(&source, &destination, file.kind, should_overwrite)?;
        copied_files.push(CopiedModelFile {
            relative_path: file.relative_path.clone(),
            source: source_snapshot,
            destination: ModelFileSnapshot::read(&destination)?,
        });
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
        copied_files,
        source_dirs: dirs,
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
    let session = Arc::new(ModelDirMigrationSession {
        #[cfg(any(windows, target_os = "macos", target_os = "linux"))]
        _cli_access: Some(crate::cli_access::resource(&crate::storage::settings_dir(&app)?, crate::cli_access::MIGRATION, true)?),
        ..Default::default()
    });
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

    let mut ownership_update_failed = false;
    let cleanup_failed_files = finalize_migrated_files(
        Path::new(&finalize.source_model_dir),
        Path::new(&finalize.target_model_dir),
        &finalize.copied_files,
        &finalize.source_dirs,
        |copied_files| {
            let response = run_worker_with_payload(
                &app,
                "remap_custom_model_paths",
                Some(json!({
                    "fromRoot": finalize.source_model_dir,
                    "toRoot": finalize.target_model_dir,
                    "copiedFiles": copied_files,
                })),
            )?;
            if response.get("committed").and_then(Value::as_bool) != Some(true) {
                return Err(AppError::Worker("自定义模型路径更新未确认，原文件已保留".into()));
            }
            ownership_update_failed = response.get("ownershipUpdateFailed").and_then(Value::as_bool) == Some(true);
            Ok(())
        },
    )?;
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
                "模型目录已切换，未清理的文件仍保留在旧目录"
            },
            "skippedFiles": finalize.skipped_files,
            "overwrittenFiles": finalize.overwritten_files,
            "cleanupFailedFiles": cleanup_failed_files,
            "ownershipUpdateFailed": ownership_update_failed,
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

#[cfg(test)]
mod tests {
    use super::*;

    fn link_directory(link: &Path, target: &Path) {
        #[cfg(windows)]
        {
            let output = std::process::Command::new("cmd.exe")
                .args(["/d", "/c", "mklink", "/J"])
                .arg(link)
                .arg(target)
                .output()
                .unwrap();
            assert!(output.status.success(), "{:?}", output);
        }
        #[cfg(unix)]
        std::os::unix::fs::symlink(target, link).unwrap();
    }

    #[test]
    fn migration_rejects_a_linked_destination_parent_even_when_the_child_is_missing() {
        let sandbox = tempfile::tempdir().unwrap();
        let target = sandbox.path().join("models");
        let outside = sandbox.path().join("outside");
        fs::create_dir(&target).unwrap();
        fs::create_dir(&outside).unwrap();
        let external_file = outside.join("keep.dat");
        fs::write(&external_file, b"external").unwrap();
        let link = target.join("linked");
        link_directory(&link, &outside);

        let target = target.canonicalize().unwrap();
        validate_migration_directory(&target, &target.join("normal/new")).unwrap();
        assert!(validate_migration_directory(&target, &target.join("linked/new")).is_err());
        assert!(collect_directory_files(&target)
            .unwrap()
            .iter()
            .all(|entry| !entry.relative_path.starts_with("linked/")));
        let remaining = finalize_migrated_files(&link, &target, &[], &[], |_| {
            panic!("no files to remap")
        }).unwrap();
        assert_eq!(remaining, vec![display_path(&link)]);
        assert!(external_file.is_file());
        assert!(link.is_dir());
        assert!(!outside.join("new").exists());
    }

    #[test]
    fn migration_rejects_an_implicit_models_link_before_and_after_preparation() {
        let sandbox = tempfile::tempdir().unwrap();
        let source = sandbox.path().join("source");
        let selected = sandbox.path().join("selected");
        let outside = sandbox.path().join("outside").join("models");
        fs::create_dir(&source).unwrap();
        fs::create_dir(&selected).unwrap();
        fs::create_dir_all(&outside).unwrap();
        let external_file = outside.join("model.ckpt");
        fs::write(&external_file, b"original").unwrap();

        let prepared = normalize_target_model_dir_path(&display_path(&selected)).unwrap();
        let request = StartModelDirMigrationRequest {
            task_id: None,
            current_model_dir: display_path(&source.canonicalize().unwrap()),
            target_model_dir: display_path(&prepared),
        };
        let (_, target) = migration_roots_for_start(&request).unwrap();
        assert!(path_eq(&target, &prepared));
        assert!(!target.exists());

        link_directory(&selected.join("models"), &outside);
        assert!(normalize_target_model_dir_path(&display_path(&selected)).is_err());
        assert!(migration_roots_for_start(&request).is_err());
        assert_eq!(fs::read(external_file).unwrap(), b"original");
    }

    #[test]
    fn migration_overwrite_refuses_a_directory_and_preserves_its_contents() {
        let sandbox = tempfile::tempdir().unwrap();
        let source = sandbox.path().join("source.ckpt");
        let destination = sandbox.path().join("destination.ckpt");
        fs::write(&source, b"model").unwrap();
        fs::create_dir(&destination).unwrap();
        let existing = destination.join("keep.dat");
        fs::write(&existing, b"existing").unwrap();

        assert!(migration_destination_exists(&destination).is_err());
        assert!(copy_entry(&source, &destination, ModelDirMigrationFileKind::File, true).is_err());
        assert_eq!(fs::read(&source).unwrap(), b"model");
        assert_eq!(fs::read(&existing).unwrap(), b"existing");
    }

    #[test]
    fn migration_file_overwrite_requires_confirmation() {
        let sandbox = tempfile::tempdir().unwrap();
        let source = sandbox.path().join("source.ckpt");
        let destination = sandbox.path().join("destination.ckpt");
        fs::write(&source, b"model").unwrap();
        fs::write(&destination, b"existing").unwrap();

        assert!(migration_destination_exists(&destination).unwrap());
        assert!(copy_entry(&source, &destination, ModelDirMigrationFileKind::File, false).is_err());
        assert_eq!(fs::read(&destination).unwrap(), b"existing");
        copy_entry(&source, &destination, ModelDirMigrationFileKind::File, true).unwrap();
        assert_eq!(fs::read(&destination).unwrap(), b"model");
    }

    #[cfg(windows)]
    #[test]
    fn migration_copy_failure_preserves_the_existing_destination() {
        use std::os::windows::fs::OpenOptionsExt;

        let sandbox = tempfile::tempdir().unwrap();
        let source = sandbox.path().join("source.ckpt");
        let destination = sandbox.path().join("destination.ckpt");
        fs::write(&source, b"model").unwrap();
        fs::write(&destination, b"existing").unwrap();
        let _locked_source = fs::OpenOptions::new().read(true).share_mode(0).open(&source).unwrap();

        assert!(ModelFileSnapshot::read(&source).is_ok());
        assert!(copy_entry(&source, &destination, ModelDirMigrationFileKind::File, true).is_err());
        assert_eq!(fs::read(&destination).unwrap(), b"existing");
        assert_eq!(fs::read_dir(sandbox.path()).unwrap().count(), 2);
    }

    #[cfg(windows)]
    #[test]
    fn migration_publishes_models_under_long_windows_paths() {
        let sandbox = tempfile::tempdir().unwrap();
        let source = sandbox.path().join("source.ckpt");
        let destination = sandbox.path().join("a".repeat(120)).join("b".repeat(120)).join("model.ckpt");
        fs::write(&source, b"model").unwrap();

        copy_entry(&source, &destination, ModelDirMigrationFileKind::File, false).unwrap();
        assert_eq!(fs::read(&destination).unwrap(), b"model");
    }

    #[test]
    fn migration_preflight_rejects_a_later_directory_conflict_before_copying() {
        let sandbox = tempfile::tempdir().unwrap();
        let root = sandbox.path().canonicalize().unwrap();
        let source = root.join("source");
        let target = root.join("target");
        fs::create_dir(&source).unwrap();
        fs::create_dir_all(target.join("b.ckpt")).unwrap();
        fs::write(source.join("a.ckpt"), b"incoming").unwrap();
        fs::write(source.join("b.ckpt"), b"incoming").unwrap();
        fs::write(target.join("a.ckpt"), b"original target").unwrap();
        fs::write(target.join("b.ckpt/keep.dat"), b"unrelated").unwrap();

        let files = collect_directory_files(&source).unwrap();
        assert!(count_conflicts(&files, &target).is_err());
        assert_eq!(fs::read(target.join("a.ckpt")).unwrap(), b"original target");
        assert_eq!(fs::read(target.join("b.ckpt/keep.dat")).unwrap(), b"unrelated");
    }

    fn copied_model_file(source: &Path, target: &Path, relative_path: &str) -> CopiedModelFile {
        let old = source.join(relative_path);
        let new = target.join(relative_path);
        fs::write(&old, b"weights").unwrap();
        let snapshot = ModelFileSnapshot::read(&old).unwrap();
        copy_entry(&old, &new, ModelDirMigrationFileKind::File, false).unwrap();
        CopiedModelFile {
            relative_path: relative_path.into(),
            source: snapshot,
            destination: ModelFileSnapshot::read(&new).unwrap(),
        }
    }

    #[test]
    fn migration_cleans_nothing_if_registry_updates_fail() {
        let sandbox = tempfile::tempdir().unwrap();
        let root = sandbox.path().canonicalize().unwrap();
        let source = root.join("source");
        let target = root.join("target");
        fs::create_dir_all(source.join("nested")).unwrap();
        fs::create_dir(&target).unwrap();
        let copied = [copied_model_file(&source, &target, "nested/model.ckpt")];

        let result = finalize_migrated_files(&source, &target, &copied, &["nested".into()], |files| {
            assert_eq!(files, ["nested/model.ckpt"]);
            assert!(source.join(files[0]).is_file());
            Err(AppError::Worker("registry not writable".into()))
        });
        assert!(result.is_err());
        assert_eq!(fs::read(source.join("nested/model.ckpt")).unwrap(), b"weights");
        assert_eq!(fs::read(target.join("nested/model.ckpt")).unwrap(), b"weights");
    }

    #[test]
    fn migration_cleanup_preserves_skipped_new_and_changed_files() {
        let sandbox = tempfile::tempdir().unwrap();
        let source_dir = sandbox.path().join("source");
        let target_dir = sandbox.path().join("target");
        fs::create_dir_all(source_dir.join("nested")).unwrap();
        fs::create_dir_all(target_dir.join("nested")).unwrap();
        let source = source_dir.canonicalize().unwrap();
        let target = target_dir.canonicalize().unwrap();
        let skipped = "same.ckpt";
        fs::write(source.join(skipped), b"source model").unwrap();
        fs::write(target.join(skipped), b"different target model").unwrap();

        let mut copied = Vec::new();
        for relative_path in ["nested/copied.ckpt", "changed-source.ckpt", "changed-target.ckpt"] {
            copied.push(copied_model_file(&source, &target, relative_path));
        }
        fs::write(source.join("changed-source.ckpt"), b"new source weights").unwrap();
        fs::write(target.join("changed-target.ckpt"), b"new target weights").unwrap();
        fs::write(source.join("new.ckpt"), b"created after copying").unwrap();

        let remaining = finalize_migrated_files(&source, &target, &copied, &["nested".into()], |files| {
            assert_eq!(files, ["nested/copied.ckpt"]);
            assert!(source.join(files[0]).is_file());
            Ok(())
        }).unwrap();
        assert_eq!(remaining.len(), 4);
        assert!(!source.join("nested").exists());
        assert_eq!(fs::read(target.join("nested/copied.ckpt")).unwrap(), b"weights");
        assert_eq!(fs::read(source.join(skipped)).unwrap(), b"source model");
        assert_eq!(fs::read(target.join(skipped)).unwrap(), b"different target model");
        assert_eq!(fs::read(source.join("new.ckpt")).unwrap(), b"created after copying");
        assert_eq!(fs::read(source.join("changed-source.ckpt")).unwrap(), b"new source weights");
        assert_eq!(fs::read(source.join("changed-target.ckpt")).unwrap(), b"weights");
    }
}
