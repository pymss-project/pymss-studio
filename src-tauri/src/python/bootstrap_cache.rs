use std::fs;
use std::io;
use std::path::{Component, Path, PathBuf};
use std::sync::Mutex;

static CACHE_LOCK: Mutex<()> = Mutex::new(());

// AppImage mount points change on every launch. Venvs must refer to an interpreter
// outside the mount, and older interpreters must remain available after an update.
pub(crate) fn cached_python(source: &Path, data_root: &Path, version: &str) -> io::Result<PathBuf> {
    let _lock = CACHE_LOCK
        .lock()
        .map_err(|_| io::Error::other("Bootstrap cache lock poisoned"))?;
    let identity = fs::read_to_string(source.join("bootstrap-id"))?;
    let identity = identity.trim();
    if identity.is_empty()
        || !identity
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"-_.".contains(&c))
        || !version
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"-_.+".contains(&c))
    {
        return Err(io::Error::other("Invalid bundled Python identity"));
    }
    let cache_root = data_root.join("runtime-bootstrap");
    fs::create_dir_all(&cache_root)?;
    let name = format!("{version}-{identity}");
    let destination = cache_root.join(&name);
    let ready = |root: &Path| {
        root.join("bin/python3").is_file()
            && root.join("lib/python3.12/encodings/__init__.py").is_file()
            && fs::read_to_string(root.join(".complete")).is_ok_and(|value| value == identity)
    };
    if ready(&destination) {
        return Ok(destination.join("bin/python3"));
    }
    if destination.exists() {
        return Err(io::Error::other(format!(
            "Incomplete Python cache: {}",
            destination.display()
        )));
    }
    let staging = cache_root.join(format!(".{name}-{}.staging", std::process::id()));
    if staging.exists() {
        fs::remove_dir_all(&staging)?;
    }
    let result = (|| {
        copy_tree(source, &staging, &source.canonicalize()?)?;
        fs::write(staging.join(".complete"), identity)?;
        if !ready(&staging) {
            return Err(io::Error::other("Bundled Python is incomplete"));
        }
        if let Err(error) = fs::rename(&staging, &destination) {
            // Another application process may have populated the same version.
            if !ready(&destination) {
                return Err(error);
            }
        }
        Ok(destination.join("bin/python3"))
    })();
    if staging.exists() {
        let _ = fs::remove_dir_all(&staging);
    }
    result
}

fn copy_tree(source: &Path, destination: &Path, source_root: &Path) -> io::Result<()> {
    fs::create_dir(destination)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let from = entry.path();
        let to = destination.join(entry.file_name());
        let metadata = fs::symlink_metadata(&from)?;
        if metadata.file_type().is_symlink() {
            let target = fs::read_link(&from)?;
            if target.is_absolute()
                || target
                    .components()
                    .any(|part| matches!(part, Component::Prefix(_) | Component::RootDir))
                || !from.canonicalize()?.starts_with(source_root)
            {
                return Err(io::Error::other(
                    "Bundled Python contains an external symlink",
                ));
            }
            #[cfg(unix)]
            std::os::unix::fs::symlink(target, to)?;
            #[cfg(not(unix))]
            return Err(io::Error::other(
                "POSIX bootstrap symlinks require a Unix host",
            ));
        } else if metadata.is_dir() {
            copy_tree(&from, &to, source_root)?;
        } else if metadata.is_file() {
            fs::copy(&from, &to)?;
        } else {
            return Err(io::Error::other("Unsupported bundled Python file type"));
        }
    }
    fs::set_permissions(destination, fs::metadata(source)?.permissions())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture(root: &Path) -> PathBuf {
        let source = root.join("mount/python-runtime");
        fs::create_dir_all(source.join("bin")).unwrap();
        fs::create_dir_all(source.join("lib/python3.12/encodings")).unwrap();
        fs::write(source.join("bootstrap-id"), "pbs-3.12.13-linux-x64").unwrap();
        fs::write(source.join("bin/python3"), "interpreter").unwrap();
        fs::write(
            source.join("lib/python3.12/encodings/__init__.py"),
            "stdlib",
        )
        .unwrap();
        source
    }

    #[test]
    fn bootstrap_cache_survives_mount_changes_and_keeps_previous_versions() {
        let root = tempfile::tempdir().unwrap();
        let source = fixture(root.path());
        let data = root.path().join("data");
        let python = cached_python(&source, &data, "1.0.0").unwrap();
        fs::remove_dir_all(&source).unwrap();
        let new_mount = fixture(&root.path().join("other"));
        assert_eq!(cached_python(&new_mount, &data, "1.0.0").unwrap(), python);
        let upgraded = cached_python(&new_mount, &data, "1.0.1").unwrap();
        assert_ne!(upgraded, python);
        assert_eq!(fs::read_to_string(python).unwrap(), "interpreter");
    }

    #[test]
    fn bootstrap_cache_does_not_publish_partial_copies_or_accept_path_ids() {
        let root = tempfile::tempdir().unwrap();
        let source = fixture(root.path());
        let data = root.path().join("data");
        fs::remove_file(source.join("bin/python3")).unwrap();
        assert!(cached_python(&source, &data, "1.0.0").is_err());
        assert_eq!(
            fs::read_dir(data.join("runtime-bootstrap"))
                .unwrap()
                .count(),
            0
        );
        fs::write(source.join("bootstrap-id"), "../../escape").unwrap();
        assert!(cached_python(&source, &data, "1.0.0").is_err());
    }

    #[cfg(unix)]
    #[test]
    fn bootstrap_cache_preserves_relative_links_and_executable_modes() {
        use std::os::unix::fs::{symlink, PermissionsExt};
        let root = tempfile::tempdir().unwrap();
        let source = fixture(root.path());
        fs::rename(source.join("bin/python3"), source.join("bin/python3.12")).unwrap();
        fs::set_permissions(
            source.join("bin/python3.12"),
            fs::Permissions::from_mode(0o755),
        )
        .unwrap();
        symlink("python3.12", source.join("bin/python3")).unwrap();
        let python = cached_python(&source, &root.path().join("data"), "1.0.0").unwrap();
        assert_eq!(fs::read_link(&python).unwrap(), Path::new("python3.12"));
        assert_eq!(
            fs::metadata(python).unwrap().permissions().mode() & 0o777,
            0o755
        );
    }

    #[cfg(unix)]
    #[test]
    fn bootstrap_cache_rejects_symlinks_outside_the_bundle() {
        let root = tempfile::tempdir().unwrap();
        let source = fixture(root.path());
        fs::write(root.path().join("outside"), "host file").unwrap();
        std::os::unix::fs::symlink("../../outside", source.join("external")).unwrap();
        let data = root.path().join("data");
        assert!(cached_python(&source, &data, "1.0.0").is_err());
        assert_eq!(fs::read_dir(data.join("runtime-bootstrap")).unwrap().count(), 0);
    }
}
