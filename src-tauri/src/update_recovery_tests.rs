use super::*;
use std::cell::Cell;
use std::collections::BTreeMap;
use std::sync::atomic::{AtomicU64, Ordering};

static FIXTURE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

struct RecoveryFixture {
    sandbox: PathBuf,
    root: PathBuf,
    backup: PathBuf,
    protected: BTreeMap<PathBuf, Vec<u8>>,
}

impl RecoveryFixture {
    fn new() -> Self {
        let sandbox = std::env::temp_dir().canonicalize().unwrap().join(format!(
            "pymss-recovery-contract-{}-{}-{}",
            std::process::id(),
            FIXTURE_SEQUENCE.fetch_add(1, Ordering::Relaxed),
            SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos(),
        ));
        fs::create_dir(&sandbox).unwrap();
        let root = sandbox.join("app");
        let backup = root.join(".pymss-studio-update-backup-contract");
        let mut fixture = Self { sandbox, root, backup, protected: BTreeMap::new() };
        for (relative, bytes) in [
            ("pymss-studio.portable", "portable"),
            ("data/settings/app.json", "{\"locale\":\"zh-CN\",\"customField\":7}"),
            ("data/settings/tasks.json", "[{\"id\":\"saved-task\"}]"),
            ("data/settings/workflows.json", "{\"workflows\":[{\"id\":\"saved-workflow\"}]}"),
            ("data/editor-projects/project.json", "{\"tracks\":[{\"id\":\"saved-track\"}]}"),
            ("data/models/model.bin", "saved-model"),
            ("data/outputs/audio.wav", "saved-audio"),
            ("python-runtime/runtime-envs/active-runtime.json", "{\"backend\":\"cpu\",\"pythonPath\":\"cpu/Scripts/python.exe\"}"),
            ("python-runtime/runtime-envs/cpu/Lib/site-packages/package.py", "saved-runtime"),
            ("Pymss Studio.exe", "new-executable"),
            ("python/worker.py", "new-worker"),
            ("bin/ffmpeg.exe", "original-tool"),
        ] {
            fixture.write(&fixture.root.join(relative), bytes.as_bytes());
        }
        fixture.protected = fixture.protected_snapshot();
        fixture
    }

    fn write(&self, path: &Path, bytes: &[u8]) {
        assert!(path.starts_with(&self.sandbox));
        assert!(!path.components().any(|part| matches!(part, Component::ParentDir)));
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, bytes).unwrap();
    }

    fn marker(&self, phase: UpdatePhase) {
        write_update_transaction(&self.root, &self.backup, phase).unwrap();
    }

    fn populate_backup(&self, phase: UpdatePhase) {
        self.marker(phase);
        for (path, value) in [
            ("python/worker.py", "old-worker"),
            ("bin/ffmpeg.exe", "old-tool"),
            ("Pymss Studio.exe", "old-executable"),
        ] {
            self.write(&self.backup.join(path), value.as_bytes());
        }
    }

    fn snapshot(&self) -> BTreeMap<PathBuf, Vec<u8>> {
        fn visit(root: &Path, dir: &Path, result: &mut BTreeMap<PathBuf, Vec<u8>>) {
            for entry in fs::read_dir(dir).unwrap() {
                let entry = entry.unwrap();
                let path = entry.path();
                if entry.file_type().unwrap().is_dir() {
                    visit(root, &path, result);
                } else {
                    result.insert(path.strip_prefix(root).unwrap().to_path_buf(), fs::read(path).unwrap());
                }
            }
        }
        let mut result = BTreeMap::new();
        visit(&self.sandbox, &self.sandbox, &mut result);
        result
    }

    fn protected_snapshot(&self) -> BTreeMap<PathBuf, Vec<u8>> {
        self.snapshot().into_iter().filter(|(path, _)| {
            path.starts_with("app/data") || path.starts_with("app/python-runtime")
                || path == Path::new("app/pymss-studio.portable")
        }).collect()
    }

    fn run(&self, interrupted: bool, relaunch: impl FnOnce() -> AppResult<()>) -> AppResult<()> {
        let result = if interrupted {
            recover_interrupted_update_with(&self.root, &self.backup, relaunch)
        } else {
            recover_or_relaunch_with(&self.root, relaunch)
        };
        assert_eq!(self.protected_snapshot(), self.protected);
        result
    }

    fn assert_restored(&self) {
        for (name, content) in [
            ("python/worker.py", "old-worker"),
            ("bin/ffmpeg.exe", "old-tool"),
            ("Pymss Studio.exe", "old-executable"),
        ] {
            assert_eq!(fs::read_to_string(self.root.join(name)).unwrap(), content);
        }
    }

    fn quarantined_markers(&self) -> Vec<PathBuf> {
        fs::read_dir(&self.root).unwrap().map(|entry| entry.unwrap().path())
            .filter(|path| path.file_name().unwrap().to_string_lossy()
                .starts_with(".pymss-studio-update-transaction.invalid-")).collect()
    }
}

impl Drop for RecoveryFixture {
    fn drop(&mut self) {
        let temp = std::env::temp_dir().canonicalize().ok();
        if self.sandbox.parent() == temp.as_deref()
            && self.sandbox.canonicalize().ok().as_ref() == Some(&self.sandbox)
        {
            let _ = fs::remove_dir_all(&self.sandbox);
        }
    }
}

fn restart_error() -> AppError {
    AppError::Worker("Application restart failed".into())
}

#[test]
fn missing_marker_is_required_only_for_interrupted_recovery() {
    for interrupted in [true, false] {
        let fixture = RecoveryFixture::new();
        let before = fixture.snapshot();
        let calls = Cell::new(0);
        let result = fixture.run(interrupted, || { calls.set(calls.get() + 1); Ok(()) });
        if interrupted {
            assert!(result.unwrap_err().to_string().contains("Interrupted update transaction is missing"));
            assert_eq!(calls.get(), 0);
        } else {
            result.unwrap();
            assert_eq!(calls.get(), 1);
        }
        assert_eq!(fixture.snapshot(), before);
    }
}

#[test]
fn restart_failure_without_a_marker_preserves_the_current_installation() {
    let fixture = RecoveryFixture::new();
    let before = fixture.snapshot();
    let error = fixture.run(false, || Err(restart_error())).unwrap_err();
    assert_eq!(error.to_string(), restart_error().to_string());
    assert_eq!(fixture.snapshot(), before);
}

#[test]
fn unmarked_targets_are_rejected_before_recovery_or_restart() {
    let fixture = RecoveryFixture::new();
    fixture.populate_backup(UpdatePhase::Replacing);
    fs::remove_file(fixture.root.join("pymss-studio.portable")).unwrap();
    let before = fixture.snapshot();
    for interrupted in [true, false] {
        let result = if interrupted {
            recover_interrupted_update_with(&fixture.root, &fixture.backup, || panic!("unexpected restart"))
        } else {
            recover_or_relaunch_with(&fixture.root, || panic!("unexpected restart"))
        };
        assert!(result.unwrap_err().to_string().contains("not a managed Pymss Studio installation"));
        assert_eq!(fixture.snapshot(), before);
    }
}

#[test]
fn invalid_marker_json_is_retained_by_both_helper_recovery_entries() {
    for interrupted in [true, false] {
        let fixture = RecoveryFixture::new();
        fixture.write(&transaction_path(&fixture.root), b"invalid-json");
        let before = fixture.snapshot();
        assert!(matches!(fixture.run(interrupted, || panic!("unexpected restart")), Err(AppError::Json(_))));
        assert_eq!(fixture.snapshot(), before);
    }
}

#[test]
fn helper_backup_argument_must_match_the_transaction_before_file_changes() {
    let fixture = RecoveryFixture::new();
    fixture.populate_backup(UpdatePhase::Replacing);
    let other = fixture.root.join(".pymss-studio-update-backup-other");
    fixture.write(&other.join("python/worker.py"), b"unrelated-backup");
    let before = fixture.snapshot();
    let error = recover_interrupted_update_with(&fixture.root, &other, || panic!("unexpected restart")).unwrap_err();
    assert!(error.to_string().contains("Interrupted update backup does not match its transaction"));
    assert_eq!(fixture.snapshot(), before);
}

#[test]
fn invalid_backup_locations_leave_the_transaction_and_files_unchanged() {
    for interrupted in [true, false] {
        let mut fixture = RecoveryFixture::new();
        for backup in [fixture.sandbox.join("outside/.pymss-studio-update-backup-other"), fixture.root.join("data")] {
            fixture.backup = backup;
            fixture.marker(UpdatePhase::Replacing);
            let before = fixture.snapshot();
            let error = fixture.run(interrupted, || panic!("unexpected restart")).unwrap_err();
            assert!(error.to_string().contains("Update backup"));
            assert_eq!(fixture.snapshot(), before);
        }
    }
}

#[test]
fn prepared_missing_backup_quarantines_the_exact_marker_before_restart() {
    for interrupted in [true, false] {
        let fixture = RecoveryFixture::new();
        fixture.marker(UpdatePhase::Prepared);
        let marker = fs::read(transaction_path(&fixture.root)).unwrap();
        let calls = Cell::new(0);
        fixture.run(interrupted, || {
            assert!(!transaction_path(&fixture.root).exists());
            assert!(!fixture.backup.exists());
            let quarantine = fixture.quarantined_markers();
            assert_eq!(quarantine.len(), 1);
            assert_eq!(fs::read(&quarantine[0]).unwrap(), marker);
            calls.set(calls.get() + 1);
            Ok(())
        }).unwrap();
        assert_eq!(calls.get(), 1);
        assert_eq!(fs::read_to_string(fixture.root.join("python/worker.py")).unwrap(), "new-worker");
    }
}

#[test]
fn replacing_missing_backup_preserves_entry_specific_errors_and_marker() {
    for interrupted in [true, false] {
        let fixture = RecoveryFixture::new();
        fixture.marker(UpdatePhase::Replacing);
        let before = fixture.snapshot();
        let expected = if interrupted {
            "Interrupted update backup is missing; the transaction marker was retained for recovery"
        } else {
            "Update backup is missing; the transaction marker was retained for recovery"
        };
        let error = fixture.run(interrupted, || panic!("unexpected restart")).unwrap_err();
        assert!(matches!(error, AppError::Worker(ref message) if message == expected));
        assert_eq!(fixture.snapshot(), before);
    }
}

#[test]
fn a_file_at_the_backup_path_is_not_treated_as_a_recoverable_directory() {
    for interrupted in [true, false] {
        for phase in [UpdatePhase::Prepared, UpdatePhase::Replacing] {
            let fixture = RecoveryFixture::new();
            fixture.marker(phase);
            fixture.write(&fixture.backup, b"retained-backup-path");
            let before = fixture.snapshot();
            let calls = Cell::new(0);
            let result = fixture.run(interrupted, || { calls.set(calls.get() + 1); Ok(()) });
            if phase == UpdatePhase::Prepared {
                result.unwrap();
                assert_eq!(calls.get(), 1);
                assert_eq!(fixture.quarantined_markers().len(), 1);
            } else {
                assert!(result.is_err());
                assert_eq!(calls.get(), 0);
                assert_eq!(fixture.snapshot(), before);
            }
            assert_eq!(fs::read(&fixture.backup).unwrap(), b"retained-backup-path");
        }
    }
}

#[test]
fn both_phases_with_a_backup_restore_before_clearing_and_restarting() {
    for interrupted in [true, false] {
        for phase in [UpdatePhase::Prepared, UpdatePhase::Replacing] {
            let fixture = RecoveryFixture::new();
            fixture.populate_backup(phase);
            let calls = Cell::new(0);
            fixture.run(interrupted, || {
                fixture.assert_restored();
                assert!(!transaction_path(&fixture.root).exists());
                assert!(!fixture.backup.exists());
                calls.set(calls.get() + 1);
                Ok(())
            }).unwrap();
            assert_eq!(calls.get(), 1);
        }
    }
}

#[test]
fn partial_recovery_snapshot_preserves_paths_already_restored_or_never_backed_up() {
    for interrupted in [true, false] {
        let fixture = RecoveryFixture::new();
        fixture.populate_backup(UpdatePhase::Replacing);
        fs::remove_dir_all(fixture.root.join("python")).unwrap();
        fs::rename(fixture.backup.join("python"), fixture.root.join("python")).unwrap();
        fs::remove_dir_all(fixture.backup.join("bin")).unwrap();
        fixture.run(interrupted, || Ok(())).unwrap();
        assert_eq!(fs::read_to_string(fixture.root.join("python/worker.py")).unwrap(), "old-worker");
        assert_eq!(fs::read_to_string(fixture.root.join("bin/ffmpeg.exe")).unwrap(), "original-tool");
        assert_eq!(fs::read_to_string(fixture.root.join("Pymss Studio.exe")).unwrap(), "old-executable");
    }
}

#[test]
fn restart_failure_propagates_after_successful_recovery_without_undoing_restored_files() {
    for interrupted in [true, false] {
        let fixture = RecoveryFixture::new();
        fixture.populate_backup(UpdatePhase::Replacing);
        let calls = Cell::new(0);
        let error = fixture.run(interrupted, || {
            calls.set(calls.get() + 1);
            fixture.assert_restored();
            assert!(!transaction_path(&fixture.root).exists());
            assert!(!fixture.backup.exists());
            Err(restart_error())
        }).unwrap_err();
        assert_eq!(calls.get(), 1);
        assert_eq!(error.to_string(), restart_error().to_string());
        fixture.assert_restored();
    }
}

#[test]
fn restart_failure_after_prepared_cleanup_keeps_the_quarantined_marker() {
    for interrupted in [true, false] {
        let fixture = RecoveryFixture::new();
        fixture.marker(UpdatePhase::Prepared);
        assert!(fixture.run(interrupted, || Err(restart_error())).is_err());
        assert!(!transaction_path(&fixture.root).exists());
        assert_eq!(fixture.quarantined_markers().len(), 1);
        assert_eq!(fs::read_to_string(fixture.root.join("Pymss Studio.exe")).unwrap(), "new-executable");
    }
}

#[test]
fn marker_cleanup_uses_quarantine_if_removing_the_marker_path_fails() {
    let fixture = RecoveryFixture::new();
    let marker = transaction_path(&fixture.root);
    // A directory makes remove_file fail while allowing the existing rename fallback.
    fixture.write(&marker.join("retained.json"), b"retained-marker");
    clear_recovered_transaction(&fixture.root).unwrap();
    assert!(!marker.exists());
    let quarantine = fixture.quarantined_markers();
    assert_eq!(quarantine.len(), 1);
    assert_eq!(fs::read(quarantine[0].join("retained.json")).unwrap(), b"retained-marker");
    assert_eq!(fixture.protected_snapshot(), fixture.protected);
}

#[cfg(windows)]
fn deny_delete(path: &Path) -> fs::File {
    use std::os::windows::fs::OpenOptionsExt;
    // Preserve reads and writes; deny deletion/rename without changing ACLs or global state.
    fs::OpenOptions::new().read(true).share_mode(0x1 | 0x2).open(path).unwrap()
}

#[cfg(windows)]
#[test]
fn partial_restore_failure_retains_recovery_state_and_can_be_retried() {
    for interrupted in [true, false] {
        for locked_path in ["bin/ffmpeg.exe", "Pymss Studio.exe"] {
            let fixture = RecoveryFixture::new();
            fixture.populate_backup(UpdatePhase::Replacing);
            let marker = fs::read(transaction_path(&fixture.root)).unwrap();
            let lock = deny_delete(&fixture.root.join(locked_path));
            assert!(fixture.run(interrupted, || panic!("unexpected restart")).is_err());
            assert_eq!(fs::read(transaction_path(&fixture.root)).unwrap(), marker);
            assert!(fixture.backup.join(locked_path).is_file());
            assert!(fixture.backup.join("Pymss Studio.exe").is_file());
            assert_eq!(fs::read_to_string(fixture.root.join("python/worker.py")).unwrap(), "old-worker");
            drop(lock);
            fixture.run(interrupted, || Ok(())).unwrap();
            fixture.assert_restored();
        }
    }
}

#[cfg(windows)]
#[test]
fn marker_cleanup_failure_keeps_backup_and_retry_does_not_remove_restored_files() {
    for interrupted in [true, false] {
        let fixture = RecoveryFixture::new();
        fixture.populate_backup(UpdatePhase::Replacing);
        let marker = transaction_path(&fixture.root);
        let bytes = fs::read(&marker).unwrap();
        let lock = deny_delete(&marker);
        assert!(fixture.run(interrupted, || panic!("unexpected restart")).is_err());
        fixture.assert_restored();
        assert_eq!(fs::read(&marker).unwrap(), bytes);
        assert!(fixture.backup.is_dir());
        drop(lock);
        fixture.run(interrupted, || Ok(())).unwrap();
        fixture.assert_restored();
        assert!(!marker.exists());
        assert!(!fixture.backup.exists());
    }
}

#[cfg(windows)]
#[test]
fn prepared_quarantine_failure_preserves_marker_and_does_not_restart() {
    for interrupted in [true, false] {
        let fixture = RecoveryFixture::new();
        fixture.marker(UpdatePhase::Prepared);
        let before = fixture.snapshot();
        let lock = deny_delete(&transaction_path(&fixture.root));
        assert!(fixture.run(interrupted, || panic!("unexpected restart")).is_err());
        assert_eq!(fixture.snapshot(), before);
        drop(lock);
        fixture.run(interrupted, || Ok(())).unwrap();
        assert!(!transaction_path(&fixture.root).exists());
    }
}

#[cfg(windows)]
#[test]
fn backup_cleanup_failure_is_best_effort_after_marker_clear() {
    for interrupted in [true, false] {
        let fixture = RecoveryFixture::new();
        fixture.populate_backup(UpdatePhase::Replacing);
        let retained = fixture.backup.join("retained.log");
        fixture.write(&retained, b"retained-backup-file");
        let lock = deny_delete(&retained);
        fixture.run(interrupted, || {
            fixture.assert_restored();
            assert!(!transaction_path(&fixture.root).exists());
            assert!(retained.is_file());
            Ok(())
        }).unwrap();
        assert_eq!(fs::read(&retained).unwrap(), b"retained-backup-file");
        drop(lock);
    }
}
