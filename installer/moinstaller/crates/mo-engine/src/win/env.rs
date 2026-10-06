//! 环境变量持久化（注册表）+ WM_SETTINGCHANGE 广播。
//!
//! user  -> HKCU\Environment
//! machine -> HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment
//! PATH 类值以 REG_EXPAND_SZ 存储（Windows 惯例），修改后广播 WM_SETTINGCHANGE。

use mo_core::manifest::{EnvOp, EnvScope};
use windows::Win32::Foundation::HWND;
use windows::Win32::Foundation::{LPARAM, WPARAM};
use windows::Win32::System::Registry::{
    HKEY, HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE, KEY_READ, KEY_WRITE, REG_EXPAND_SZ, REG_SZ,
    REG_VALUE_TYPE, RegCloseKey, RegDeleteValueW, RegOpenKeyExW, RegQueryValueExW, RegSetValueExW,
};
use windows::Win32::UI::WindowsAndMessaging::{
    SMTO_ABORTIFHUNG, SendMessageTimeoutW, WM_SETTINGCHANGE,
};
use windows::core::PCWSTR;

const USER_SUBKEY: &str = "Environment";
const MACHINE_SUBKEY: &str = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment";

fn subkey(scope: EnvScope) -> (&'static str, HKEY) {
    match scope {
        EnvScope::User => (USER_SUBKEY, HKEY_CURRENT_USER),
        EnvScope::Machine => (MACHINE_SUBKEY, HKEY_LOCAL_MACHINE),
    }
}

fn to_wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

/// 读持久化环境变量的原始字符串值（REG_SZ / REG_EXPAND_SZ）。
pub fn read_env(scope: EnvScope, name: &str) -> Option<String> {
    let (sub, root) = subkey(scope);
    let sub_w = to_wide(sub);
    let name_w = to_wide(name);
    unsafe {
        let mut hkey = HKEY::default();
        if RegOpenKeyExW(root, PCWSTR(sub_w.as_ptr()), None, KEY_READ, &mut hkey).is_err() {
            return None;
        }
        let mut buf = [0u16; 4096];
        let mut len = (buf.len() * 2) as u32;
        let mut vtype = REG_VALUE_TYPE(0);
        let r = RegQueryValueExW(
            hkey,
            PCWSTR(name_w.as_ptr()),
            None,
            Some(&mut vtype),
            Some(buf.as_mut_ptr().cast()),
            Some(&mut len),
        );
        let _ = RegCloseKey(hkey);
        if r.is_err() {
            return None;
        }
        let chars = (len as usize / 2).min(buf.len());
        let s: Vec<u16> = buf[..chars]
            .iter()
            .copied()
            .take_while(|&c| c != 0)
            .collect();
        Some(String::from_utf16_lossy(&s))
    }
}

/// 写持久化环境变量。返回旧值。写入失败返回 None（调用方以读旧值兜底）。
pub fn write_env(scope: EnvScope, name: &str, value: &str) -> Option<String> {
    let prev = read_env(scope, name);
    let (sub, root) = subkey(scope);
    unsafe {
        let sub_w = to_wide(sub);
        let name_w = to_wide(name);
        let val_w = to_wide(value);
        let mut hkey = HKEY::default();
        if RegOpenKeyExW(root, PCWSTR(sub_w.as_ptr()), None, KEY_WRITE, &mut hkey).is_err() {
            return prev;
        }
        let vtype = if name.eq_ignore_ascii_case("path") {
            REG_EXPAND_SZ
        } else {
            REG_SZ
        };
        let bytes: Vec<u8> = val_w.iter().flat_map(|&c| c.to_le_bytes()).collect();
        let r = RegSetValueExW(hkey, PCWSTR(name_w.as_ptr()), None, vtype, Some(&bytes));
        let _ = r;
        let _ = RegCloseKey(hkey);
    }
    prev
}

/// 删除持久化环境变量值。
pub fn delete_env(scope: EnvScope, name: &str) {
    let (sub, root) = subkey(scope);
    unsafe {
        let sub_w = to_wide(sub);
        let name_w = to_wide(name);
        let mut hkey = HKEY::default();
        if RegOpenKeyExW(root, PCWSTR(sub_w.as_ptr()), None, KEY_WRITE, &mut hkey).is_err() {
            return;
        }
        let _ = RegDeleteValueW(hkey, PCWSTR(name_w.as_ptr()));
        let _ = RegCloseKey(hkey);
    }
}

/// 广播 WM_SETTINGCHANGE（"Environment"），让资源管理器等感知 env 变化。
pub fn broadcast_env_change() {
    let param: Vec<u16> = "Environment"
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect();
    unsafe {
        let hwnd_broadcast = HWND(0xFFFF as *mut core::ffi::c_void);
        let _ = SendMessageTimeoutW(
            hwnd_broadcast,
            WM_SETTINGCHANGE,
            WPARAM(0),
            LPARAM(param.as_ptr() as isize),
            SMTO_ABORTIFHUNG,
            2000,
            None,
        );
    }
}

/// 应用 op 到当前值，返回应写入的新值（None = 保持不变）。
pub fn apply_op(current: Option<&str>, value: &str, op: EnvOp) -> Option<String> {
    match op {
        EnvOp::Set => Some(value.to_string()),
        EnvOp::Append => {
            let cur = current.unwrap_or("");
            if cur.split(';').any(|p| p.eq_ignore_ascii_case(value)) {
                None // 已存在
            } else if cur.is_empty() {
                Some(value.to_string())
            } else {
                Some(format!("{cur};{value}"))
            }
        }
        EnvOp::Prepend => {
            let cur = current.unwrap_or("");
            if cur.split(';').any(|p| p.eq_ignore_ascii_case(value)) {
                None
            } else if cur.is_empty() {
                Some(value.to_string())
            } else {
                Some(format!("{value};{cur}"))
            }
        }
        EnvOp::Remove => {
            let cur = current?;
            let kept: Vec<&str> = cur
                .split(';')
                .filter(|p| !p.eq_ignore_ascii_case(value))
                .collect();
            Some(kept.join(";"))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn user_env_roundtrip() {
        let name = "MO_ENGINE_TEST_XYZ";
        let prev = write_env(EnvScope::User, name, "a;b");
        assert_eq!(read_env(EnvScope::User, name).as_deref(), Some("a;b"));
        write_env(EnvScope::User, name, "final");
        assert_eq!(read_env(EnvScope::User, name).as_deref(), Some("final"));
        delete_env(EnvScope::User, name);
        assert_eq!(read_env(EnvScope::User, name), None);
        if let Some(p) = prev {
            write_env(EnvScope::User, name, &p);
        }
        broadcast_env_change();
    }

    #[test]
    fn op_semantics() {
        use mo_core::manifest::EnvOp;
        assert_eq!(
            apply_op(Some("a;b"), "c", EnvOp::Append).as_deref(),
            Some("a;b;c")
        );
        assert_eq!(apply_op(Some("a;b"), "b", EnvOp::Append), None);
        assert_eq!(
            apply_op(Some("a;b"), "c", EnvOp::Prepend).as_deref(),
            Some("c;a;b")
        );
        assert_eq!(apply_op(None, "x", EnvOp::Append).as_deref(), Some("x"));
        assert_eq!(
            apply_op(Some("a;b;c"), "b", EnvOp::Remove).as_deref(),
            Some("a;c")
        );
        assert_eq!(apply_op(Some("a"), "a", EnvOp::Remove).as_deref(), Some(""));
        assert_eq!(apply_op(None, "x", EnvOp::Remove), None);
        assert_eq!(
            apply_op(Some("old"), "new", EnvOp::Set).as_deref(),
            Some("new")
        );
    }
}
