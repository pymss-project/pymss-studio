//! 统一错误类型。

/// mo-core 统一错误。
#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("IO 错误: {0}")]
    Io(#[from] std::io::Error),

    #[error("清单解析失败: {0}")]
    ManifestParse(String),

    #[error("清单校验失败: {0}")]
    ManifestInvalid(String),

    #[error("常量引用错误: {0}")]
    BadConstant(String),

    #[error("overlay 包格式错误: {0}")]
    Overlay(String),

    #[error("文件内容 CRC 校验失败: {path}")]
    CrcMismatch { path: String },

    #[error("glob 模式错误: {0}")]
    Glob(String),
}

pub type Result<T> = std::result::Result<T, Error>;
