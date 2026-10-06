//! 快捷方式（.lnk）创建：IShellLinkW + IPersistFile。

use std::path::Path;
use windows::Win32::System::Com::IPersistFile;
use windows::Win32::System::Com::{
    CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED, CoCreateInstance, CoInitializeEx,
    CoUninitialize,
};
use windows::Win32::UI::Shell::{IShellLinkW, ShellLink};
use windows::core::{Interface, PCWSTR};

/// 创建 .lnk。workdir 为 None 时取 target 的父目录。
pub fn create_lnk(
    lnk_path: &Path,
    target: &Path,
    workdir: Option<&Path>,
    description: &str,
) -> Result<(), String> {
    unsafe {
        let hr = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
        let need_uninit = hr.is_ok();
        let result = (|| {
            let link: IShellLinkW = CoCreateInstance(&ShellLink, None, CLSCTX_INPROC_SERVER)
                .map_err(|e| format!("CoCreateInstance 失败: {e}"))?;
            let target_w: Vec<u16> = target
                .as_os_str()
                .to_string_lossy()
                .encode_utf16()
                .chain(std::iter::once(0))
                .collect();
            link.SetPath(PCWSTR(target_w.as_ptr()))
                .map_err(|e| format!("SetPath 失败: {e}"))?;
            let wd = workdir
                .map(|w| w.to_path_buf())
                .unwrap_or_else(|| target.parent().unwrap_or(Path::new("")).to_path_buf());
            let wd_w: Vec<u16> = wd
                .as_os_str()
                .to_string_lossy()
                .encode_utf16()
                .chain(std::iter::once(0))
                .collect();
            if !wd.as_os_str().is_empty() {
                link.SetWorkingDirectory(PCWSTR(wd_w.as_ptr()))
                    .map_err(|e| format!("SetWorkingDirectory 失败: {e}"))?;
            }
            if !description.is_empty() {
                let desc_w: Vec<u16> = description
                    .encode_utf16()
                    .chain(std::iter::once(0))
                    .collect();
                link.SetDescription(PCWSTR(desc_w.as_ptr()))
                    .map_err(|e| format!("SetDescription 失败: {e}"))?;
            }
            let persist: IPersistFile = link
                .cast()
                .map_err(|e| format!("cast IPersistFile 失败: {e}"))?;
            let lnk_w: Vec<u16> = lnk_path
                .as_os_str()
                .to_string_lossy()
                .encode_utf16()
                .chain(std::iter::once(0))
                .collect();
            persist
                .Save(PCWSTR(lnk_w.as_ptr()), true)
                .map_err(|e| format!("保存 .lnk 失败: {e}"))?;
            Ok(())
        })();
        if need_uninit {
            CoUninitialize();
        }
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn create_and_exists() {
        let dir = std::env::temp_dir().join(format!("mo-lnk-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let target = dir.join("t.exe");
        std::fs::write(&target, b"MZ fake").unwrap();
        let lnk = dir.join("t.lnk");
        create_lnk(&lnk, &target, None, "测试快捷方式").unwrap();
        assert!(lnk.is_file());
        assert!(std::fs::metadata(&lnk).unwrap().len() > 200);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
