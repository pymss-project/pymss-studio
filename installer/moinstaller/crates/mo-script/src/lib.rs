//! mo-script：rhai 钩子宿主（L2 定制层）。
//!
//! - [host]：宿主白名单 API（HostApi trait + FakeHost）
//! - [script]：ScriptHost（事件总线订阅者）+ 构建期预编译检查
//!
//! 安全模型：rhai 默认无文件/网络 IO；宿主只挂载白名单函数；
//! 编译错误/运行时错误/超时/步数超限/ctx.abort 都会以钩子错误中止安装
//! （退出码 4，触发回滚）。静默模式下 message_box 直接返回 true。

pub mod host;
pub mod script;

pub use host::{FakeHost, HostApi};
pub use script::{ScriptHost, ScriptOptions, compile_check};
