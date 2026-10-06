//! install.log 动作记录。
//!
//! 每个已执行的安装动作一条 JSON 记录（JSONL）；失败回滚与卸载清理
//! 都按逆序消费这份日志。prev 字段记录动作前的旧值，用于精确还原。

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "t", rename_all = "snake_case")]
pub enum ActionRecord {
    CreatedDir {
        path: PathBuf,
    },
    WroteFile {
        path: PathBuf,
    },
    SetReg {
        root: String,
        key: String,
        name: String,
        /// 动作前的旧值（None = 值原本不存在）。
        prev: Option<String>,
    },
    SetEnv {
        scope: String,
        name: String,
        prev: Option<String>,
    },
    CreatedShortcut {
        path: PathBuf,
    },
    WroteUninstallKey {
        root: String,
        key: String,
    },
}

/// 动作日志（内存中的记录序列）。
#[derive(Debug, Clone, Default)]
pub struct ActionLog {
    records: Vec<ActionRecord>,
}

impl ActionLog {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn push(&mut self, r: ActionRecord) {
        self.records.push(r);
    }

    pub fn len(&self) -> usize {
        self.records.len()
    }

    pub fn is_empty(&self) -> bool {
        self.records.is_empty()
    }

    /// 逆序迭代（回滚顺序）。
    pub fn iter_rollback(&self) -> impl Iterator<Item = &ActionRecord> {
        self.records.iter().rev()
    }

    pub fn to_jsonl(&self) -> String {
        let mut out = String::new();
        for r in &self.records {
            out.push_str(&serde_json::to_string(r).unwrap_or_default());
            out.push('\n');
        }
        out
    }

    pub fn parse_jsonl(text: &str) -> Result<Self, String> {
        let mut records = Vec::new();
        for (i, line) in text.lines().enumerate() {
            let line = line.trim();
            if line.is_empty() {
                continue;
            }
            let r: ActionRecord = serde_json::from_str(line)
                .map_err(|e| format!("install.log 第 {} 行解析失败: {e}", i + 1))?;
            records.push(r);
        }
        Ok(Self { records })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn jsonl_roundtrip() {
        let mut log = ActionLog::new();
        log.push(ActionRecord::CreatedDir {
            path: PathBuf::from("D:\\App\\bin"),
        });
        log.push(ActionRecord::WroteFile {
            path: PathBuf::from("D:\\App\\bin\\a.exe"),
        });
        log.push(ActionRecord::SetReg {
            root: "hkcu".into(),
            key: "Software\\A".into(),
            name: "Path".into(),
            prev: Some("old".into()),
        });
        log.push(ActionRecord::SetEnv {
            scope: "user".into(),
            name: "PATH".into(),
            prev: None,
        });
        log.push(ActionRecord::CreatedShortcut {
            path: PathBuf::from("C:\\Users\\u\\Desktop\\a.lnk"),
        });
        log.push(ActionRecord::WroteUninstallKey {
            root: "hkcu".into(),
            key: "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\com.a".into(),
        });

        let text = log.to_jsonl();
        let parsed = ActionLog::parse_jsonl(&text).unwrap();
        assert_eq!(parsed.len(), 6);

        // 逆序
        let names: Vec<&ActionRecord> = parsed.iter_rollback().collect();
        assert!(matches!(names[0], ActionRecord::WroteUninstallKey { .. }));
        assert!(matches!(names[5], ActionRecord::CreatedDir { .. }));
    }

    #[test]
    fn parse_bad_line_fails() {
        assert!(ActionLog::parse_jsonl("not json").is_err());
        assert!(ActionLog::parse_jsonl("").unwrap().is_empty());
        assert!(ActionLog::parse_jsonl("\n \n").unwrap().is_empty());
    }
}
