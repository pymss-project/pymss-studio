use std::env;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::PathBuf;

fn run() -> Result<(), String> {
    let mut source = None;
    let mut output = None;
    let mut ending = None;
    let mut arguments = env::args_os().skip(1);
    while let Some(flag) = arguments.next() {
        let value = arguments.next().ok_or("A command argument is missing")?;
        match flag.to_str() {
            Some("--input") if source.is_none() => source = Some(PathBuf::from(value)),
            Some("--output") if output.is_none() => output = Some(PathBuf::from(value)),
            Some("--line-ending") if ending.is_none() => ending = Some(value),
            _ => return Err("Unsupported or duplicate command argument".into()),
        }
    }
    let source = source.ok_or("An input file is required")?;
    let output = output.ok_or("An output file is required")?;
    let ending = ending.ok_or("Line endings are required")?;
    if fs::metadata(&source).map_err(|_| "The input file is unavailable")?.len() > 64 * 1024 * 1024 {
        return Err("The input file exceeds 64 MiB".into());
    }
    let text = fs::read_to_string(&source).map_err(|_| "The input must be readable UTF-8 text")?;
    let normalized = text.replace("\r\n", "\n").replace('\r', "\n");
    let normalized = match ending.to_str() {
        Some("lf") => normalized,
        Some("crlf") => normalized.replace('\n', "\r\n"),
        _ => return Err("Line endings must be lf or crlf".into()),
    };
    let mut target = OpenOptions::new().write(true).create_new(true).open(output)
        .map_err(|_| "The output file could not be created")?;
    target.write_all(normalized.as_bytes()).map_err(|_| "The output file could not be written")?;
    target.sync_all().map_err(|_| "The output file could not be flushed")?;
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(2);
    }
}
