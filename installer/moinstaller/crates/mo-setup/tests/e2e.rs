//! M1 端到端：build → /VERYSILENT 安装 → 断言文件树。
//!
//! 模板直接取本 crate 刚构建的 mo-setup.exe（CARGO_BIN_EXE_mo-setup），
//! 不依赖两阶段模板固化，可与 cargo test --workspace 一起跑。

use std::fs;
use std::path::Path;
use std::process::Command;

const APP_EXE_BYTES: &[u8] = b"#!/usr/bin/env fake-exe\x00\x01\x02 moinstaller m1 e2e payload";
const README_ZH: &str = "MoInstaller M1 端到端测试\n中文内容 with spaces & 特殊字符 !@#$%\n";

fn setup_bin() -> &'static str {
    env!("CARGO_BIN_EXE_mo-setup")
}

fn make_fixture(tmp: &Path) -> std::path::PathBuf {
    let dist = tmp.join("dist");
    fs::create_dir_all(dist.join("bin")).unwrap();
    fs::create_dir_all(dist.join("数据 目录")).unwrap();
    fs::write(dist.join("bin").join("app.exe"), APP_EXE_BYTES).unwrap();
    fs::write(dist.join("readme-zh.txt"), README_ZH.as_bytes()).unwrap();
    fs::write(
        dist.join("数据 目录").join("配置 文件.json"),
        br#"{"k":"v"}"#,
    )
    .unwrap();
    fs::write(dist.join("empty.dat"), b"").unwrap();

    let toml = tmp.join("installer.toml");
    fs::write(
        &toml,
        r#"
[app]
id = "com.example.e2e"
name = "E2E Demo 应用"
version = "0.1.0"
publisher = "MoInstaller Tests"

[options]
default_dir = '{localappdata}\E2E Demo 应用'

[[files]]
src = "dist/**/*"
dst = "{app}"
"#,
    )
    .unwrap();
    toml
}

fn run_setup(setup: &Path, dir: &Path) -> std::process::Output {
    Command::new(setup)
        .arg("/VERYSILENT")
        .arg(format!("/DIR={}", dir.display()))
        .output()
        .expect("运行 setup.exe 失败")
}

#[test]
fn e2e_silent_install_files() {
    let tmp = tempfile::tempdir().unwrap();
    let toml = make_fixture(tmp.path());
    let out_exe = tmp.path().join("demo-setup.exe");

    // build
    let stats = mo_build::build(
        &toml,
        &mo_build::BuildOptions {
            template: Some(setup_bin().into()),
            out: Some(out_exe.clone()),
            sign: None,
        },
    )
    .unwrap_or_else(|e| panic!("build 失败: {e}"));
    assert_eq!(stats.file_count, 4);
    assert!(out_exe.is_file());

    // 静默安装（目标目录带中文与空格）
    let inst_dir = tmp.path().join("安装 目标");
    let out = run_setup(&out_exe, &inst_dir);
    let stdout = String::from_utf8_lossy(&out.stdout);
    assert!(
        out.status.success(),
        "安装失败 exit={:?}\nstdout:\n{stdout}\nstderr:\n{}",
        out.status.code(),
        String::from_utf8_lossy(&out.stderr)
    );

    // 断言文件树与内容
    assert_eq!(
        fs::read(inst_dir.join("bin").join("app.exe")).unwrap(),
        APP_EXE_BYTES
    );
    assert_eq!(
        fs::read_to_string(inst_dir.join("readme-zh.txt")).unwrap(),
        README_ZH
    );
    assert_eq!(
        fs::read(inst_dir.join("数据 目录").join("配置 文件.json")).unwrap(),
        br#"{"k":"v"}"#
    );
    assert_eq!(fs::read(inst_dir.join("empty.dat")).unwrap(), b"");
}

#[test]
fn e2e_bare_template_without_overlay_fails() {
    // 无 overlay 的裸 mo-setup.exe 应以非 0 退出
    let out = Command::new(setup_bin())
        .arg("/VERYSILENT")
        .output()
        .unwrap();
    assert_ne!(out.status.code(), Some(0));
    assert!(!out.stderr.is_empty());
}

#[test]
fn e2e_rerun_overwrites() {
    // 重复安装 = 覆盖式，仍然成功
    let tmp = tempfile::tempdir().unwrap();
    let toml = make_fixture(tmp.path());
    let out_exe = tmp.path().join("demo-setup.exe");
    mo_build::build(
        &toml,
        &mo_build::BuildOptions {
            template: Some(setup_bin().into()),
            out: Some(out_exe.clone()),
            sign: None,
        },
    )
    .unwrap();
    let inst_dir = tmp.path().join("twice");
    for _ in 0..2 {
        let out = run_setup(&out_exe, &inst_dir);
        assert!(out.status.success(), "二次安装应成功");
    }
    assert!(inst_dir.join("bin").join("app.exe").is_file());
}

// ===================== M2：完整安装语义 =====================

const M2_APP_NAME: &str = "MoInst E2E App";
const M2_APP_ID: &str = "moinst.e2e.m2";
const M2_ENV_VAR: &str = "MOINST_E2E_TEST_VAR";

fn make_m2_fixture(tmp: &Path, hooks_fail: bool, tag: &str) -> std::path::PathBuf {
    let dist = tmp.join("dist");
    fs::create_dir_all(dist.join("bin")).unwrap();
    fs::write(
        dist.join("bin").join("app.exe"),
        b"M2 fake exe payload bytes",
    )
    .unwrap();
    fs::write(dist.join("settings.json"), b"{\"keep\":true}").unwrap();

    let hook_section: String = if hooks_fail {
        r#"
[[hooks]]
event = "after_install"
run = "cmd"
args = ["/c", "exit 5"]
"#
        .to_string()
    } else {
        format!(
            r#"
[[hooks]]
event = "after_install"
run = "cmd"
args = ["/c", "echo hook-ok > %TEMP%\\mo-hook-{}.txt"]
"#,
            tag
        )
    };

    let toml = format!(
        r#"
[app]
id = "{M2_APP_ID}.{tag}"
name = "{M2_APP_NAME} {tag}"
version = "2.0.0"
publisher = "MoInstaller Tests"

[options]
default_dir = '{{localappdata}}\\{M2_APP_NAME}'

[[files]]
src = "dist/**/*"
dst = "{{app}}"

[[registry]]
root = "hkcu"
key = "Software\\MoInstE2E_{tag}"
name = "InstallPath"
value = "{{app}}"
value_type = "expandSZ"

[[registry]]
root = "hkcu"
key = "Software\\MoInstE2E_{tag}"
name = "Level"
value = "7"
value_type = "dword"

[[env]]
name = "{M2_ENV_VAR}_{tag}"
op = "set"
value = "m2-e2e-value"
scope = "user"

[[shortcuts]]
name = "MoInst E2E App {tag}"
target = "{{app}}/bin/app.exe"
dest = "start-menu"

[uninstall]
keep = ["{{app}}/settings.json"]
{hook_section}
"#
    );
    let path = tmp.join("installer-m2.toml");
    fs::write(&path, toml).unwrap();
    path
}

/// 清理 HKCU 下指定键（测试前置：确保 prev 干净，卸载语义为彻底删除）。
fn reg_delete_tree(key: &str) {
    let _ = Command::new("reg")
        .args(["delete", &format!(r"HKCU\{key}"), "/f"])
        .status();
}

fn build_m2_setup(toml: &Path, out: &Path) {
    mo_build::build(
        toml,
        &mo_build::BuildOptions {
            template: Some(setup_bin().into()),
            out: Some(out.to_path_buf()),
            sign: None,
        },
    )
    .unwrap_or_else(|e| panic!("build 失败: {e}"));
}

/// reg query 包装：返回值列（REG_* 之后的内容）。键/值不存在返回 None。
fn reg_query(root: &str, key: &str, name: &str) -> Option<String> {
    let out = Command::new("reg")
        .args(["query", &format!("{root}\\{key}"), "/v", name])
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&out.stdout);
    text.lines()
        .find(|l| l.contains("REG_"))?
        .split_whitespace()
        .skip(2) // 跳过名字与 REG 类型
        .collect::<Vec<_>>()
        .join(" ")
        .into()
}

#[test]
fn e2e_m2_full_install_then_uninstall() {
    // 预清理：避免上轮失败残留干扰 prev 语义
    reg_delete_tree(r"Software\MoInstE2E_full");
    reg_delete_tree(&format!(
        r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{M2_APP_ID}.full"
    ));
    let _ = Command::new("reg")
        .args([
            "delete",
            r"HKCU\Environment",
            "/v",
            &format!("{M2_ENV_VAR}_full"),
            "/f",
        ])
        .status();

    let tmp = tempfile::tempdir().unwrap();
    let toml = make_m2_fixture(tmp.path(), false, "full");
    let setup = tmp.path().join("m2-setup.exe");
    build_m2_setup(&toml, &setup);

    let inst_dir = tmp.path().join("目标 Dir M2");
    let out = Command::new(&setup)
        .arg("/VERYSILENT")
        .arg(format!("/DIR={}", inst_dir.display()))
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "安装失败: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    // 文件
    assert!(inst_dir.join("bin").join("app.exe").is_file());
    // 钩子副作用（%TEMP% 下的标记文件）
    let mark_path = std::env::temp_dir().join("mo-hook-full.txt");
    let mark = fs::read_to_string(&mark_path).unwrap_or_default();
    assert!(mark.contains("hook-ok"), "hook 标记: {mark}");
    let _ = std::fs::remove_file(&mark_path);
    // 注册表（应用键）
    assert_eq!(
        reg_query(
            "HKCU",
            &format!(r"Software\MoInstE2E_{}", "full"),
            "InstallPath"
        ),
        Some(inst_dir.to_string_lossy().into_owned())
    );
    assert_eq!(
        reg_query("HKCU", &format!(r"Software\MoInstE2E_{}", "full"), "Level"),
        Some("0x7".into())
    );
    // env（HKCU\Environment）
    assert_eq!(
        reg_query("HKCU", "Environment", &format!("{M2_ENV_VAR}_full")),
        Some("m2-e2e-value".into())
    );
    // 快捷方式（{group} = userprograms\app name）
    let programs = std::env::var("APPDATA").unwrap() + r"\Microsoft\Windows\Start Menu\Programs";
    let lnk = std::path::PathBuf::from(programs)
        .join(format!("{M2_APP_NAME} full"))
        .join("MoInst E2E App full.lnk");
    assert!(lnk.is_file(), "快捷方式缺失: {}", lnk.display());
    // 卸载程序与日志
    assert!(inst_dir.join("mo-uninstall.exe").is_file());
    assert!(inst_dir.join("mo-install.log").is_file());
    // Uninstall 注册表键
    let ukey = format!(r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{M2_APP_ID}.full");
    assert_eq!(
        reg_query("HKCU", &ukey, "DisplayName"),
        Some(format!("{M2_APP_NAME} full"))
    );

    // ---- 卸载 ----
    let out = Command::new(inst_dir.join("mo-uninstall.exe"))
        .arg("/VERYSILENT")
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "卸载失败: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    // 等待自删（cmd timeout 1s 后 del）
    for _ in 0..20 {
        if !inst_dir.join("mo-uninstall.exe").exists() {
            break;
        }
        std::thread::sleep(std::time::Duration::from_millis(300));
    }
    assert!(!inst_dir.join("mo-uninstall.exe").exists(), "自删失败");
    assert!(!inst_dir.join("bin").join("app.exe").exists());
    assert!(!inst_dir.join("mo-install.log").exists());
    // keep 文件保留
    assert!(inst_dir.join("settings.json").is_file(), "keep 文件被误删");
    // 注册表清理
    assert_eq!(
        reg_query(
            "HKCU",
            &format!(r"Software\MoInstE2E_{}", "full"),
            "InstallPath"
        ),
        None
    );
    assert_eq!(
        reg_query("HKCU", "Environment", &format!("{M2_ENV_VAR}_full")),
        None
    );
    assert_eq!(reg_query("HKCU", &ukey, "DisplayName"), None);
    // 快捷方式清理
    assert!(!lnk.exists());
}

#[test]
fn e2e_m2_hook_failure_rolls_back() {
    reg_delete_tree(r"Software\MoInstE2E_rb");
    reg_delete_tree(&format!(
        r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{M2_APP_ID}.rb"
    ));
    let _ = Command::new("reg")
        .args([
            "delete",
            r"HKCU\Environment",
            "/v",
            &format!("{M2_ENV_VAR}_rb"),
            "/f",
        ])
        .status();

    let tmp = tempfile::tempdir().unwrap();
    let toml = make_m2_fixture(tmp.path(), true, "rb");
    let setup = tmp.path().join("m2-setup-fail.exe");
    build_m2_setup(&toml, &setup);

    let inst_dir = tmp.path().join("rollback dir");
    let out = Command::new(&setup)
        .arg("/VERYSILENT")
        .arg(format!("/DIR={}", inst_dir.display()))
        .output()
        .unwrap();

    // 钩子失败 = 退出码 4
    assert_eq!(out.status.code(), Some(4), "钩子失败应为退出码 4");
    // 回滚：文件与注册表全部撤销
    assert!(!inst_dir.join("bin").join("app.exe").exists());
    assert_eq!(
        reg_query(
            "HKCU",
            &format!(r"Software\MoInstE2E_{}", "rb"),
            "InstallPath"
        ),
        None
    );
    assert_eq!(
        reg_query("HKCU", "Environment", &format!("{M2_ENV_VAR}_rb")),
        None
    );
    let ukey = format!(r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{M2_APP_ID}.rb");
    assert_eq!(reg_query("HKCU", &ukey, "DisplayName"), None);
    assert!(!inst_dir.join("mo-uninstall.exe").exists());
}

// ===================== M3：主题/许可资源打包 =====================

#[test]
fn e2e_m3_theme_license_package() {
    let tmp = tempfile::tempdir().unwrap();
    let dist = tmp.path().join("dist");
    fs::create_dir_all(&dist).unwrap();
    fs::write(dist.join("app.exe"), b"m3 themed").unwrap();
    fs::write(
        tmp.path().join("LICENSE.txt"),
        "测试许可协议\nMoInstaller E2E License\n",
    )
    .unwrap();

    fs::write(
        tmp.path().join("installer-m3.toml"),
        r##"
[app]
id = "moinst.e2e.m3"
name = "M3Theme"
version = "3.0.0"
publisher = "MoInstaller Tests"

[options]
license = "LICENSE.txt"
default_dir = '{localappdata}\M3Theme'

[theme]
accent = "#00AA55"
window = { width = 700, height = 500 }
strings."wizard.btn.install" = "马上安装"

[[files]]
src = "dist/**/*"
dst = "{app}"
"##,
    )
    .unwrap();

    let setup = tmp.path().join("m3-setup.exe");
    let stats = mo_build::build(
        &tmp.path().join("installer-m3.toml"),
        &mo_build::BuildOptions {
            template: Some(setup_bin().into()),
            out: Some(setup.clone()),
            sign: None,
        },
    )
    .unwrap();
    // dist/app.exe + __mo__/license 两个条目
    assert_eq!(stats.file_count, 2);

    // 静默安装仍然工作（license/主题页在静默模式下跳过）
    let inst_dir = tmp.path().join("m3 dir");
    let out = Command::new(&setup)
        .arg("/VERYSILENT")
        .arg(format!("/DIR={}", inst_dir.display()))
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "主题包静默安装失败: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(fs::read(inst_dir.join("app.exe")).unwrap(), b"m3 themed");
}

// ===================== M4：rhai 脚本钩子 =====================

fn make_m4_fixture(tmp: &Path, script: &str) -> std::path::PathBuf {
    let dist = tmp.join("dist");
    fs::create_dir_all(&dist).unwrap();
    fs::write(dist.join("keep.txt"), b"keep").unwrap();
    fs::write(dist.join("skipme.bin"), b"should be skipped").unwrap();

    let toml = format!(
        r#"
[app]
id = "moinst.e2e.m4"
name = "M4Script"
version = "4.0.0"
publisher = "T"

[[files]]
src = "dist/**/*"
dst = "{{app}}"

[script]
inline = '''{script}'''
"#
    );
    let path = tmp.join("installer-m4.toml");
    fs::write(&path, toml).unwrap();
    path
}

#[test]
fn e2e_m4_script_skips_file_and_installs() {
    let tmp = tempfile::tempdir().unwrap();
    let toml = make_m4_fixture(
        tmp.path(),
        "fn before_file(ctx, path) { !path.contains(\"skipme\") }",
    );
    let setup = tmp.path().join("m4-setup.exe");
    mo_build::build(
        &toml,
        &mo_build::BuildOptions {
            template: Some(setup_bin().into()),
            out: Some(setup.clone()),
            sign: None,
        },
    )
    .unwrap();

    let inst_dir = tmp.path().join("m4 dir");
    let out = Command::new(&setup)
        .arg("/VERYSILENT")
        .arg(format!("/DIR={}", inst_dir.display()))
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    // before_file false 的文件被跳过
    assert!(inst_dir.join("keep.txt").is_file());
    assert!(!inst_dir.join("skipme.bin").exists(), "skipme 应被脚本跳过");
}

#[test]
fn e2e_m4_script_abort_fails_with_code4() {
    let tmp = tempfile::tempdir().unwrap();
    let toml = make_m4_fixture(
        tmp.path(),
        "fn initialize_setup(ctx) { ctx.abort(\"测试拒绝安装\"); true }",
    );
    let setup = tmp.path().join("m4-abort.exe");
    mo_build::build(
        &toml,
        &mo_build::BuildOptions {
            template: Some(setup_bin().into()),
            out: Some(setup.clone()),
            sign: None,
        },
    )
    .unwrap();

    let inst_dir = tmp.path().join("abort dir");
    let out = Command::new(&setup)
        .arg("/VERYSILENT")
        .arg(format!("/DIR={}", inst_dir.display()))
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(4), "脚本 abort 应为退出码 4");
    // 回滚
    assert!(!inst_dir.join("keep.txt").exists());
}

#[test]
fn e2e_m4_bad_script_rejected_at_build() {
    let tmp = tempfile::tempdir().unwrap();
    let toml = make_m4_fixture(tmp.path(), "fn broken( { }");
    let err = mo_build::build(
        &toml,
        &mo_build::BuildOptions {
            template: Some(setup_bin().into()),
            out: Some(tmp.path().join("x.exe")),
            sign: None,
        },
    )
    .unwrap_err();
    assert!(err.to_string().contains("rhai"), "错误信息: {err}");
}

#[test]
fn e2e_m4_normal_script_installs() {
    let tmp = tempfile::tempdir().unwrap();
    let toml = make_m4_fixture(
        tmp.path(),
        "fn initialize_setup(ctx) { true }
fn on_exit(ctx, code) { ctx.log(\"done\"); }",
    );
    let setup = tmp.path().join("m4-ok.exe");
    mo_build::build(
        &toml,
        &mo_build::BuildOptions {
            template: Some(setup_bin().into()),
            out: Some(setup.clone()),
            sign: None,
        },
    )
    .unwrap();

    let inst_dir = tmp.path().join("ok dir");
    let out = Command::new(&setup)
        .arg("/VERYSILENT")
        .arg(format!("/DIR={}", inst_dir.display()))
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(inst_dir.join("keep.txt").is_file());
    assert!(inst_dir.join("skipme.bin").is_file());
}
