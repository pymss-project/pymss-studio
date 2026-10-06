//! mo-setup 的 build.rs：winresource 嵌入版本资源与图标。
//!
//! 图标/版本信息属于模板自身（所有 setup.exe 继承）；
//! 应用专属信息在安装时经清单提供（M3 主题/文案）。

use std::path::Path;

fn main() {
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }
    let mut res = winresource::WindowsResource::new();
    if let Some(rc_dir) = find_x64_rc_dir() {
        res.set_toolkit_path(&rc_dir);
    }
    if let Ok(manifest_dir) = std::env::var("CARGO_MANIFEST_DIR") {
        let ico = std::path::Path::new(&manifest_dir).join("assets/mo-setup.ico");
        res.set_icon(ico.to_str().unwrap_or("assets/mo-setup.ico"));
    }
    res.set("FileDescription", "MoInstaller Setup");
    res.set("ProductName", "MoInstaller");
    res.set("LegalCopyright", "MIT OR Apache-2.0");
    if let Err(e) = res.compile() {
        println!("cargo:warning=winresource 编译失败（不影响功能）: {e}");
    }
    println!("cargo:rerun-if-changed=assets/mo-setup.ico");
}

/// 定位最新 Windows SDK 的 x64 rc.exe 目录（默认搜索可能落在 arm64）。
fn find_x64_rc_dir() -> Option<String> {
    for base in [
        r"C:\Program Files (x86)\Windows Kits\10\bin",
        r"C:\Program Files\Windows Kits\10\bin",
    ] {
        let bin = Path::new(base);
        let Ok(versions) = std::fs::read_dir(bin) else {
            continue;
        };
        let mut best: Option<(String, String)> = None;
        for v in versions.flatten() {
            let x64 = v.path().join("x64");
            if x64.join("rc.exe").is_file() {
                let name = v.file_name().to_string_lossy().into_owned();
                let dir = x64.to_string_lossy().into_owned();
                if best.as_ref().map(|(n, _)| name > *n).unwrap_or(true) {
                    best = Some((name, dir));
                }
            }
        }
        if let Some((_, dir)) = best {
            return Some(dir);
        }
    }
    None
}
