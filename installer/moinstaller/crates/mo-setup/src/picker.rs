//! Windows 原生目录选择对话框（IFileOpenDialog + FOS_PICKFOLDERS）。

use std::path::PathBuf;
use windows::Win32::System::Com::{
    CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED, CoCreateInstance, CoInitializeEx,
    CoTaskMemFree, CoUninitialize,
};
use windows::Win32::UI::Shell::{
    FOS_FORCEFILESYSTEM, FOS_PICKFOLDERS, FileOpenDialog, IFileOpenDialog, SIGDN_FILESYSPATH,
};

/// 弹出目录选择框；取消返回 None。
pub fn pick_folder() -> Option<PathBuf> {
    unsafe {
        let hr = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
        let need_uninit = hr.is_ok();
        let result = pick_inner();
        if need_uninit {
            CoUninitialize();
        }
        result
    }
}

unsafe fn pick_inner() -> Option<PathBuf> {
    unsafe {
        let dlg: IFileOpenDialog =
            CoCreateInstance(&FileOpenDialog, None, CLSCTX_INPROC_SERVER).ok()?;
        let opts = dlg.GetOptions().ok()?;
        dlg.SetOptions(opts | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM)
            .ok()?;
        dlg.Show(None).ok()?;
        let item = dlg.GetResult().ok()?;
        let pw = item.GetDisplayName(SIGDN_FILESYSPATH).ok()?;
        let path = pw.to_string().ok();
        CoTaskMemFree(Some(pw.as_ptr().cast()));
        path.map(PathBuf::from)
    }
}
