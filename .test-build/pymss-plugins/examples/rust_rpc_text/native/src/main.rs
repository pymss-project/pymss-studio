mod protocol;

use serde_json::{json, Value};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::Path;

fn convert<R: std::io::BufRead, W: Write>(
    peer: &mut protocol::Peer<R, W>,
    run: &Value,
) -> Result<Value, String> {
    let task = peer.host("host.taskInfo", json!({}))?;
    if task["operationId"] != run["params"]["operationId"]
        || task["taskId"] != run["params"]["taskId"]
    {
        return Err("The host callback belongs to another task".into());
    }
    let source = peer.host("host.fileInfo", json!({"input":"source"}))?;
    let size = source["size"]
        .as_u64()
        .ok_or("The host did not provide the input file size")?;
    if size > 64 * 1024 * 1024 {
        return Err("The input file exceeds 64 MiB".into());
    }
    peer.progress(0.2, "Reading input")?;
    let input = run["params"]["inputs"]["source"]["path"]
        .as_str()
        .ok_or("An input file reference is required")?;
    let output = run["params"]["outputs"]["document"]["path"]
        .as_str()
        .ok_or("An output file reference is required")?;
    let text = fs::read_to_string(input).map_err(|_| "The input must be readable UTF-8 text")?;
    let normalized = text.replace("\r\n", "\n").replace('\r', "\n");
    let normalized = match run["params"]["params"]["line_ending"].as_str() {
        Some("lf") => normalized,
        Some("crlf") => normalized.replace('\n', "\r\n"),
        _ => return Err("Line endings must be lf or crlf".into()),
    };
    peer.progress(0.7, "Writing output")?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(Path::new(output))
        .map_err(|_| "The output file could not be created")?;
    file.write_all(normalized.as_bytes())
        .and_then(|_| file.sync_all())
        .map_err(|_| "The output file could not be written")?;
    peer.progress(1.0, "Processing complete")?;
    Ok(json!({"outputs":run["params"]["outputs"]}))
}

fn run() -> Result<(), String> {
    let mut peer = protocol::stdio();
    let initialize = peer.request("rpc.initialize")?;
    if initialize["params"]["protocolVersion"] != 1 {
        peer.reject(&initialize, "Unsupported process protocol version")?;
        return Err("Unsupported process protocol version".into());
    }
    peer.respond(&initialize, json!({"protocolVersion":1}))?;
    let request = peer.request("operations.run")?;
    match convert(&mut peer, &request) {
        Ok(result) => peer.respond(&request, result)?,
        Err(error) => {
            peer.reject(&request, &error)?;
            return Err(error);
        }
    }
    let shutdown = peer.request("rpc.shutdown")?;
    peer.respond(&shutdown, json!({}))?;
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(2);
    }
}
