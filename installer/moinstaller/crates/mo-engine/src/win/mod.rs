//! Windows API 封装（注册表、快捷方式、环境变量、杂项）。
//!
//! 每个模块一层薄封装 + 明确错误，供 executor 调用。

pub mod env;
pub mod misc;
pub mod registry;
pub mod shortcut;
