//! mo-engine：MoInstaller 安装/卸载引擎。
//!
//! 结构：
//! - [event]：事件总线（GUI、日志、钩子、进度条都是订阅者）
//! - [hook]：L1 外部命令钩子（订阅者）
//! - [actions]：install.log 动作记录（回滚 + 卸载共用）
//! - [executor]：安装/卸载执行器（事件发射 + 日志 + 失败回滚）
//! - [win]：Windows API 薄封装（注册表、快捷方式、环境变量、杂项）

pub mod actions;
pub mod event;
pub mod hook;

#[cfg(windows)]
pub mod executor;
#[cfg(windows)]
pub mod win;
