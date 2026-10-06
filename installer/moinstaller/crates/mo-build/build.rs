//! mo-build 的 build.rs：将 target/<profile>/mo-setup.exe 嵌入为安装器模板。
//!
//! 两阶段构建：workspace 首次构建时 mo-setup.exe 可能尚未产出，此时写入
//! 占位模板并警告；再次 cargo build --workspace 即可固化真实模板。

use std::path::Path;

fn main() {
    let out_dir = std::env::var("OUT_DIR").expect("OUT_DIR");
    let profile = std::env::var("PROFILE").unwrap_or_else(|_| "debug".into());
    // OUT_DIR = target/<profile>/build/mo-build-<hash>/out
    let target_profile_dir = Path::new(&out_dir)
        .ancestors()
        .nth(3)
        .expect("OUT_DIR 结构异常");
    let template = target_profile_dir.join("mo-setup.exe");
    let dest = Path::new(&out_dir).join("mo-setup-template.exe");

    if template.is_file() {
        println!("cargo:rerun-if-changed={}", template.display());
        std::fs::copy(&template, &dest).expect("复制模板失败");
    } else {
        // 跟踪尚不存在的模板文件：mo-setup.exe 产出后本脚本自动重跑并固化。
        // （若不声明，cargo 默认只在 build.rs 自身变化时重跑，全新环境两次构建
        //   不足以固化模板。）
        println!("cargo:rerun-if-changed={}", template.display());
        println!(
            "cargo:warning=mo-setup.exe 模板尚未构建（{}/mo-setup.exe 不存在）；mo-build 将使用占位模板。请再运行一次 cargo build --workspace。",
            target_profile_dir.display()
        );
        let _ = profile; // profile 仅用于定位
        std::fs::write(&dest, b"MO-PLACEHOLDER-TEMPLATE").expect("写占位模板失败");
    }
}
