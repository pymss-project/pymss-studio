//! mo-core：MoInstaller 的纯逻辑核心。
//!
//! 包含三部分：
//! - [manifest]：installer.toml 清单 schema（公共 API）
//! - [overlay]：setup.exe 尾部 overlay 包格式的读写
//! - [constants]：Inno 风格目录常量（{pf}、{app} 等）的展开

pub mod constants;
pub mod error;
pub mod manifest;
pub mod overlay;

pub use error::{Error, Result};
