//! mo CLI：MoInstaller 打包工具。

use clap::{Parser, Subcommand};
use std::path::PathBuf;
use std::process::ExitCode;

#[derive(Parser)]
#[command(
    name = "mo",
    version,
    about = "MoInstaller 打包工具：installer.toml → 单文件 setup.exe"
)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// 按 installer.toml 构建 setup.exe
    Build {
        /// installer.toml 路径
        manifest: PathBuf,
        /// 安装器模板 exe（默认使用内置模板）
        #[arg(long)]
        template: Option<PathBuf>,
        /// 输出路径（默认输出到清单所在目录）
        #[arg(short, long)]
        out: Option<PathBuf>,
        /// 构建后签名命令（{out} 占位替换，如 signtool sign /f c.pfx {out}）
        #[arg(long)]
        sign: Option<String>,
    },
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Build {
            manifest,
            template,
            out,
            sign,
        } => match mo_build::build(
            &manifest,
            &mo_build::BuildOptions {
                template,
                out,
                sign,
            },
        ) {
            Ok(stats) => {
                println!("构建完成: {}", stats.out_path.display());
                println!(
                    "  文件数: {}，输入 {}，输出 {}",
                    stats.file_count,
                    human(stats.total_input),
                    human(stats.total_output)
                );
                ExitCode::SUCCESS
            }
            Err(e) => {
                eprintln!("构建失败: {e}");
                ExitCode::FAILURE
            }
        },
    }
}

fn human(n: u64) -> String {
    if n >= 1024 * 1024 {
        format!("{:.2} MB", n as f64 / 1048576.0)
    } else if n >= 1024 {
        format!("{:.1} KB", n as f64 / 1024.0)
    } else {
        format!("{n} B")
    }
}
