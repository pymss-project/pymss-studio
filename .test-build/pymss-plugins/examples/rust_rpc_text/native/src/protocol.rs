use serde_json::{json, Value};
use std::io::{self, BufRead, Write};

const MAX_FRAME: usize = 256 * 1024;

pub struct Peer<R: BufRead, W: Write> {
    input: R,
    output: W,
    sequence: u64,
}

impl<R: BufRead, W: Write> Peer<R, W> {
    pub fn new(input: R, output: W) -> Self {
        Self {
            input,
            output,
            sequence: 0,
        }
    }

    pub fn read(&mut self) -> Result<Value, String> {
        let mut bytes = Vec::new();
        loop {
            let available = self
                .input
                .fill_buf()
                .map_err(|_| "The host channel could not be read")?;
            if available.is_empty() {
                return Err("The host channel closed".into());
            }
            let count = available
                .iter()
                .position(|byte| *byte == b'\n')
                .map_or(available.len(), |index| index + 1);
            if bytes.len() + count > MAX_FRAME {
                return Err("The host frame exceeds the size limit".into());
            }
            let done = available[count - 1] == b'\n';
            bytes.extend_from_slice(&available[..count]);
            self.input.consume(count);
            if done {
                break;
            }
        }
        let value: Value =
            serde_json::from_slice(&bytes).map_err(|_| "The host sent invalid protocol JSON")?;
        if value["jsonrpc"] != "2.0" || !value.is_object() {
            return Err("The host sent an invalid protocol object".into());
        }
        Ok(value)
    }

    pub fn send(&mut self, value: Value) -> Result<(), String> {
        let bytes =
            serde_json::to_vec(&value).map_err(|_| "The protocol message could not be encoded")?;
        if bytes.len() + 1 > MAX_FRAME {
            return Err("The protocol message exceeds the size limit".into());
        }
        self.output
            .write_all(&bytes)
            .and_then(|_| self.output.write_all(b"\n"))
            .and_then(|_| self.output.flush())
            .map_err(|_| "The host channel could not be written".into())
    }

    pub fn request(&mut self, expected: &str) -> Result<Value, String> {
        let value = self.read()?;
        if value["method"] != expected || !value["id"].is_string() || !value["params"].is_object() {
            return Err("The host sent an unexpected request".into());
        }
        Ok(value)
    }

    pub fn respond(&mut self, request: &Value, result: Value) -> Result<(), String> {
        self.send(json!({"jsonrpc":"2.0","id":request["id"],"result":result}))
    }

    pub fn reject(&mut self, request: &Value, message: &str) -> Result<(), String> {
        self.send(
            json!({"jsonrpc":"2.0","id":request["id"],"error":{"code":-32000,"message":message}}),
        )
    }

    pub fn host(&mut self, method: &str, params: Value) -> Result<Value, String> {
        self.sequence += 1;
        let id = format!("plugin:{}", self.sequence);
        self.send(json!({"jsonrpc":"2.0","id":id,"method":method,"params":params}))?;
        let response = self.read()?;
        if response["id"] != id || response.get("method").is_some() {
            return Err("The host returned an unexpected callback response".into());
        }
        if response.get("error").is_some() {
            return Err(response["error"]["message"]
                .as_str()
                .unwrap_or("The host service failed")
                .to_string());
        }
        response
            .get("result")
            .cloned()
            .ok_or_else(|| "The host omitted its callback result".into())
    }

    pub fn progress(&mut self, fraction: f64, message: &str) -> Result<(), String> {
        self.send(json!({"jsonrpc":"2.0","method":"task.progress","params":{"fraction":fraction,"message":message}}))
    }
}

pub fn stdio() -> Peer<io::BufReader<io::Stdin>, io::Stdout> {
    Peer::new(io::BufReader::new(io::stdin()), io::stdout())
}
