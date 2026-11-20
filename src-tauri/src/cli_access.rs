//! Cross-process leases for resources also opened by the standalone Windows CLI.
use crate::error::{AppError, AppResult};
use std::{
    fs::{self, File},
    path::Path,
};

fn lease(file: File, exclusive: bool) -> AppResult<File> {
    let result = if exclusive {
        file.try_lock()
    } else {
        file.try_lock_shared()
    };
    match result {
        Ok(()) => Ok(file),
        Err(std::fs::TryLockError::WouldBlock) => Err(AppError::Worker(
            "RUNTIME_BUSY: Studio or its command line is using this resource. Retry after the operation finishes.".into()
        )),
        Err(std::fs::TryLockError::Error(e)) => Err(e.into()),
    }
}

pub(crate) fn resource(root: &Path, name: &str, exclusive: bool) -> AppResult<File> {
    fs::create_dir_all(root)?;
    let file = File::options()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(root.join(name))?;
    lease(file, exclusive)
}

pub(crate) const RUNTIME: &str = ".pymss-cli-runtime.lock";
pub(crate) const MIGRATION: &str = ".pymss-cli-migration.lock";
pub(crate) const MODELS: &str = ".pymss-cli-models.lock";

pub(crate) fn application(root: &Path, exclusive: bool) -> AppResult<Option<File>> {
    // Both distribution markers survive application replacement, including rollback.
    for name in ["pymss-studio.inno-install", "pymss-studio.portable"] {
        match File::open(root.join(name)) {
            Ok(file) => return lease(file, exclusive).map(Some),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
            Err(e) => return Err(e.into()),
        }
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    #[test]
    fn users_share_the_marker_and_maintenance_excludes_them() {
        let tmp = tempfile::tempdir().unwrap();
        std::fs::write(tmp.path().join("pymss-studio.portable"), "portable").unwrap();
        let first = super::application(tmp.path(), false).unwrap();
        let second = super::application(tmp.path(), false).unwrap();
        assert!(super::application(tmp.path(), true).is_err());
        drop((first, second));
        assert!(super::application(tmp.path(), true).is_ok());
    }
}
