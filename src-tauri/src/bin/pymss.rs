//! Standalone console entry point for Windows and macOS.
#[cfg(any(windows, target_os = "macos"))]
#[path = "../cli_access.rs"]
mod cli_access;
#[cfg(any(windows, target_os = "macos"))]
#[path = "../error.rs"]
mod error;
#[cfg(any(windows, target_os = "macos"))]
#[path = "../studio_cli.rs"]
mod studio_cli;
#[cfg(any(windows, target_os = "macos"))]
#[path = "../studio_paths.rs"]
mod studio_paths;

#[cfg(any(windows, target_os = "macos"))]
fn main() {
    studio_cli::exit(std::env::args_os().skip(1).collect());
}

#[cfg(not(any(windows, target_os = "macos")))]
fn main() {
    eprintln!("Run the Linux AppImage with --studio-cli to use pymss.");
    std::process::exit(1);
}
