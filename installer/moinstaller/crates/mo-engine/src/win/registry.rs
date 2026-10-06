//! 注册表封装（windows-registry）。

use mo_core::manifest::{RegRoot, RegValueType};
use windows_registry::{CURRENT_USER, LOCAL_MACHINE};

/// 把 hive 键字符串（"hkcu"/"hklm"）转为 RegRoot。
pub fn root_from_str(s: &str) -> Option<RegRoot> {
    match s.to_ascii_lowercase().as_str() {
        "hkcu" => Some(RegRoot::Hkcu),
        "hklm" => Some(RegRoot::Hklm),
        _ => None,
    }
}

fn key_of(root: RegRoot) -> &'static windows_registry::Key {
    match root {
        RegRoot::Hkcu => CURRENT_USER,
        RegRoot::Hklm => LOCAL_MACHINE,
    }
}

/// 读字符串值（REG_SZ / REG_EXPAND_SZ 均可）。键或值不存在返回 None。
pub fn read_string(root: RegRoot, key: &str, name: &str) -> Option<String> {
    let k = key_of(root).open(key).ok()?;
    if let Ok(s) = k.get_string(name) {
        return Some(s);
    }
    // dword 以数字字符串返回（prev 记录兼容）
    k.get_u32(name).ok().map(|v| v.to_string())
}

/// 写值，返回动作前的旧值（None = 原本不存在）。
pub fn set_value(
    root: RegRoot,
    key: &str,
    name: &str,
    value: &str,
    vtype: RegValueType,
) -> Result<Option<String>, String> {
    let prev = read_string(root, key, name);
    let k = key_of(root).create(key).map_err(|e| e.to_string())?;
    match vtype {
        RegValueType::String => k.set_string(name, value).map_err(|e| e.to_string())?,
        RegValueType::ExpandSz => k
            .set_expand_string(name, value)
            .map_err(|e| e.to_string())?,
        RegValueType::Dword => {
            let v: u32 = value
                .parse()
                .map_err(|_| format!("dword 值非法: {value:?}"))?;
            k.set_u32(name, v).map_err(|e| e.to_string())?;
        }
    }
    Ok(prev)
}

/// 删除值（不存在则无操作）。返回底层错误便于诊断。
pub fn delete_value(root: RegRoot, key: &str, name: &str) -> Result<(), String> {
    // Key::open 默认只读；删值需要 write 权限
    let k = key_of(root)
        .options()
        .read()
        .write()
        .open(key)
        .map_err(|e| format!("open {key} 失败: {e}"))?;
    k.remove_value(name)
        .map_err(|e| format!("remove_value {name} 失败: {e}"))
}

/// 递归删除键（不存在则无操作）。
pub fn delete_tree(root: RegRoot, key: &str) {
    let _ = key_of(root).remove_tree(key);
}

/// 键是否存在。
pub fn key_exists(root: RegRoot, key: &str) -> bool {
    key_of(root).open(key).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    // 每次运行用唯一键名（pid），避免注册表删除延迟与并发竞态
    fn key(sub: &str) -> String {
        format!(
            r"Software\MoInstallerEngineTest\{}\{sub}",
            std::process::id()
        )
    }

    #[test]
    fn set_read_delete_roundtrip() {
        let k = key("roundtrip");
        let prev = set_value(RegRoot::Hkcu, &k, "StrVal", "hello", RegValueType::String).unwrap();
        assert_eq!(prev, None);
        assert_eq!(
            read_string(RegRoot::Hkcu, &k, "StrVal"),
            Some("hello".into())
        );

        let prev2 = set_value(RegRoot::Hkcu, &k, "StrVal", "world", RegValueType::String).unwrap();
        assert_eq!(prev2, Some("hello".into()));

        delete_value(RegRoot::Hkcu, &k, "StrVal").unwrap();
        assert_eq!(read_string(RegRoot::Hkcu, &k, "StrVal"), None);
        assert!(key_exists(RegRoot::Hkcu, &k));
        delete_tree(RegRoot::Hkcu, &k);
        assert!(!key_exists(RegRoot::Hkcu, &k));
    }

    #[test]
    fn dword_and_expand() {
        let k = key("dword");
        set_value(RegRoot::Hkcu, &k, "Dw", "42", RegValueType::Dword).unwrap();
        set_value(RegRoot::Hkcu, &k, "Ex", "%PATH%", RegValueType::ExpandSz).unwrap();
        assert_eq!(read_string(RegRoot::Hkcu, &k, "Dw"), Some("42".into()));
        assert!(set_value(RegRoot::Hkcu, &k, "Bad", "x", RegValueType::Dword).is_err());
        delete_tree(RegRoot::Hkcu, &k);
    }
}
