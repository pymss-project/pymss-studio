//! Remove AppRun's mounted library and module paths before launching host programs.

use std::ffi::{OsStr, OsString};
use std::path::Path;
use std::process::Command;

pub(crate) fn clean_value(value: &OsStr, mount: &Path) -> Option<OsString> {
    let original: Vec<_> = std::env::split_paths(value).collect();
    let paths: Vec<_> = original
        .iter()
        .filter(|path| !path.starts_with(mount))
        .collect();
    if paths.len() == original.len() {
        return Some(value.to_owned());
    }
    if paths.is_empty() {
        return None;
    }
    std::env::join_paths(paths).ok()
}

pub(crate) fn clean_environment(command: &mut Command, mount: Option<&OsStr>) {
    for key in ["APPIMAGE", "APPDIR", "ARGV0"] {
        command.env_remove(key);
    }
    let Some(mount) = mount else {
        return;
    };
    let mount = Path::new(mount);
    if !mount.is_absolute() || mount.parent().is_none() {
        return;
    }
    for (key, value) in std::env::vars_os() {
        if key == "APPIMAGE" || key == "APPDIR" || key == "ARGV0" {
            continue;
        }
        match clean_value(&value, mount) {
            Some(clean) if clean != value => {
                command.env(key, clean);
            }
            None => {
                command.env_remove(key);
            }
            _ => (),
        }
    }
}
