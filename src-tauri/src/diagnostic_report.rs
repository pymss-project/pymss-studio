//! Bounded, allow-listed snapshots for diagnostic reports and request tracking.
//! These helpers deliberately do not serialize arbitrary worker or store payloads.

use serde_json::{json, Map, Value};

const MAX_ITEMS: usize = 24;
const MAX_TASKS: usize = 12;
const MAX_TEXT: usize = 200;
const MAX_ERROR: usize = 600;
const ACTIVE_TASK_STATUSES: &[&str] = &[
    "queued",
    "preparing",
    "validating_input",
    "downloading_model",
    "ensuring_model",
    "loading_model",
    "separating",
    "writing_output",
    "running",
];

const INFERENCE_FIELDS: &[&str] = &[
    "batch_size",
    "overlap_size",
    "num_overlap",
    "chunk_size",
    "standardize",
    "normalize",
    "window_size",
    "aggression",
    "enable_post_process",
    "post_process_threshold",
    "high_end_process",
    "enable_tta",
    "use_tta",
    "segment_size",
    "shifts",
    "overlap",
];
const AUDIO_FIELDS: &[&str] = &[
    "wavBitDepth",
    "flacBitDepth",
    "mp3BitRate",
    "m4aBitRate",
    "m4aCodec",
    "sampleRate",
    "peakProtection",
    "wav_bit_depth",
    "flac_bit_depth",
    "mp3_bit_rate",
    "m4a_bit_rate",
    "m4a_codec",
    "sample_rate",
    "peak_protection",
];
const PACKAGE_FIELDS: &[&str] = &[
    "pymss",
    "pymss-core",
    "torch",
    "torchaudio",
    "torchvision",
    "mlx",
    "av",
    "librosa",
    "audioread",
    "numpy",
    "filelock",
    "fsspec",
    "jinja2",
    "networkx",
    "sympy",
    "typing-extensions",
    "pysocks",
    "requests",
    "pyyaml",
    "tqdm",
    "funasr",
    "modelscope",
];

#[derive(Default)]
struct Summary {
    truncated_strings: usize,
    omitted_items: usize,
    redacted_strings: usize,
}

impl Summary {
    fn text(&mut self, value: &str, limit: usize) -> Value {
        let clean = sanitize_text(value).replace(['\r', '\n', '\t'], " ");
        if clean != value {
            self.redacted_strings += 1;
        }
        let mut chars = clean.chars();
        let mut output: String = chars.by_ref().take(limit).collect();
        if chars.next().is_some() {
            output.push('…');
            self.truncated_strings += 1;
        }
        Value::String(output)
    }

    fn scalar(&mut self, value: &Value) -> Option<Value> {
        match value {
            Value::String(text) => Some(self.text(text, MAX_TEXT)),
            Value::Bool(_) | Value::Number(_) | Value::Null => Some(value.clone()),
            _ => None,
        }
    }

    fn pick(&mut self, source: &Value, fields: &[&str]) -> Value {
        let mut result = Map::new();
        for field in fields {
            if let Some(value) = source.get(*field).and_then(|value| self.scalar(value)) {
                result.insert((*field).into(), value);
            }
        }
        Value::Object(result)
    }

    fn basename(&mut self, value: &Value) -> Value {
        let Some(text) = value.as_str() else {
            return Value::Null;
        };
        let clean = sanitize_text(text);
        let trimmed = clean.trim_end_matches(['/', '\\']);
        let name = trimmed.rsplit(['/', '\\']).next().unwrap_or_default();
        self.text(name, MAX_TEXT)
    }

    fn paths(&mut self, source: &Value) -> Value {
        let values: Vec<&Value> = match source {
            Value::Array(items) => items.iter().collect(),
            Value::Object(items) => items.values().collect(),
            Value::String(_) => vec![source],
            _ => Vec::new(),
        };
        let total = values.len();
        let items = values
            .into_iter()
            .take(MAX_ITEMS)
            .map(|item| self.basename(item))
            .collect();
        self.list(total, items)
    }

    fn scalars(&mut self, source: &Value) -> Value {
        let items = source.as_array().map(Vec::as_slice).unwrap_or_default();
        let values = items
            .iter()
            .take(MAX_ITEMS)
            .filter_map(|value| self.scalar(value))
            .collect();
        self.list(items.len(), values)
    }

    fn list(&mut self, total: usize, items: Vec<Value>) -> Value {
        let omitted = total.saturating_sub(items.len());
        self.omitted_items += omitted;
        json!({ "total": total, "included": items.len(), "omitted": omitted,
            "truncated": omitted > 0, "items": items })
    }

    fn finish(self, mut value: Value) -> Value {
        value["_summary"] = json!({
            "truncated": self.truncated_strings > 0 || self.omitted_items > 0,
            "truncatedStrings": self.truncated_strings,
            "omittedItems": self.omitted_items,
            "redactedStrings": self.redacted_strings,
            "limits": { "items": MAX_ITEMS, "tasks": MAX_TASKS, "stringChars": MAX_TEXT, "errorChars": MAX_ERROR },
        });
        value
    }

    fn parameters(&mut self, source: &Value) -> Value {
        let mut result = self.pick(
            source,
            &[
                "runMode",
                "model",
                "modelType",
                "workflowId",
                "workflowName",
                "downloadSource",
                "downloadMethod",
                "source",
                "device",
                "outputFormat",
                "outputLayout",
                "useTta",
                "debug",
                "inferenceParamsVersion",
                "saveAsFolder",
                "download",
                "workbench",
                "nativeGraph",
            ],
        );
        for key in ["modelDir", "output", "outputDir"] {
            if let Some(value) = source.get(key) {
                result[key] = self.basename(value);
            }
        }
        for key in ["deviceIds", "selectedStems"] {
            if let Some(value) = source.get(key) {
                result[key] = self.scalars(value);
            }
        }
        if let Some(value) = source.get("inferenceParams") {
            result["inferenceParams"] = self.pick(value, INFERENCE_FIELDS);
        }
        if let Some(value) = source.get("audioParams") {
            result["audioParams"] = self.pick(value, AUDIO_FIELDS);
        }
        if let Some(value) = source.get("outputNaming") {
            let mut naming = self.pick(value, &["enabled"]);
            naming["template"] = template_summary(value.get("template").unwrap_or(&Value::Null));
            if let Some(order) = value.get("stemOrder") {
                naming["stemOrder"] = self.scalars(order);
            }
            result["outputNaming"] = naming;
        }
        if let Some(value) = source
            .get("workflow")
            .filter(|value| value.is_object())
            .or_else(|| source.get("workflowDefinition"))
        {
            result["workflow"] = self.workflow(value);
        }
        if let Some(workbench) = source.get("workbench").filter(|value| value.is_object()) {
            let mut snapshot = self.pick(workbench, &["version", "nativeGraph", "sourceUpdatedAt"]);
            if let Some(definition) = workbench.get("sourceDefinition") {
                snapshot["workflow"] = self.workflow(definition);
            }
            if let Some(bindings) = workbench.get("bindings") {
                snapshot["bindings"] = self.bindings(bindings);
            }
            result["workbench"] = snapshot;
        }
        for key in ["inputs", "workbenchOutputs"] {
            if let Some(value) = source.get(key) {
                result[key] = self.paths(value);
            }
        }
        result
    }

    fn bindings(&mut self, source: &Value) -> Value {
        let mut result = self.pick(source, &["primaryInput", "outputLayout"]);
        if let Some(directory) = source.get("outputDirectory") {
            result["outputDirectory"] = self.basename(directory);
        }
        if let Some(outputs) = source.get("outputs") {
            result["outputs"] = self.paths(outputs);
        }
        if let Some(inputs) = source.get("inputs").and_then(Value::as_object) {
            let items = inputs.iter().take(MAX_ITEMS).map(|(id, binding)| {
                json!({ "nodeId": self.text(id, MAX_TEXT),
                    "files": self.paths(binding.get("files").unwrap_or(&Value::Null)),
                    "folderConfigured": binding.get("folder").and_then(Value::as_str).is_some_and(|path| !path.is_empty()) })
            }).collect();
            result["inputs"] = self.list(inputs.len(), items);
        }
        result
    }

    fn workflow(&mut self, source: &Value) -> Value {
        let mut result = self.pick(source, &["version", "formatVersion"]);
        if let Some(defaults) = source.get("defaults") {
            let mut value = self.pick(defaults, &["device", "output_format", "use_tta"]);
            if let Some(params) = defaults.get("inference_params") {
                value["inference_params"] = self.pick(params, INFERENCE_FIELDS);
            }
            result["defaults"] = value;
        }
        if let Some(nodes) = source.get("nodes").and_then(Value::as_array) {
            result["format"] = json!("graph");
            result["connectionCount"] = json!(collection_len(source.get("links")));
            let items = nodes
                .iter()
                .take(MAX_ITEMS)
                .map(|node| self.graph_node(node))
                .collect();
            result["nodes"] = self.list(nodes.len(), items);
        } else if let Some(steps) = source.get("steps").and_then(Value::as_array) {
            result["format"] = json!("simple");
            let items = steps
                .iter()
                .take(MAX_ITEMS)
                .map(|step| {
                    let mut item = self.pick(
                        step,
                        &[
                            "id",
                            "model",
                            "model_type",
                            "device",
                            "output_format",
                            "use_tta",
                        ],
                    );
                    item["type"] = json!("separation");
                    // A simple input is a step reference or an external path, never copy the path.
                    if let Some(input) = step.get("input") {
                        item["input"] = self.basename(input);
                    }
                    for key in ["model_path", "config_path"] {
                        if let Some(path) = step.get(key) {
                            item[key] = self.basename(path);
                        }
                    }
                    if let Some(stems) = step.get("stems") {
                        item["stems"] = self.scalars(stems);
                    }
                    if let Some(params) = step.get("inference_params") {
                        item["inference_params"] = self.pick(params, INFERENCE_FIELDS);
                    }
                    item["saveCount"] = json!(step
                        .get("save")
                        .and_then(Value::as_object)
                        .map(|values| values
                            .values()
                            .filter(|value| !value.is_null() && **value != false && **value != "")
                            .count())
                        .unwrap_or(0));
                    item["outputTemplateCount"] = json!(collection_len(step.get("output_names")));
                    item
                })
                .collect();
            result["steps"] = self.list(steps.len(), items);
            if let Some(ensembles) = source.get("ensembles").and_then(Value::as_array) {
                let items = ensembles
                    .iter()
                    .take(MAX_ITEMS)
                    .map(|ensemble| {
                        let mut item = self.pick(ensemble, &["id", "algorithm", "output_stem"]);
                        item["saveEnabled"] =
                            json!(ensemble.get("save").is_some_and(|value| !value.is_null()
                                && *value != false
                                && *value != ""));
                        if let Some(inputs) = ensemble.get("inputs").and_then(Value::as_array) {
                            let items = inputs
                                .iter()
                                .take(MAX_ITEMS)
                                .map(|input| self.pick(input, &["source", "weight"]))
                                .collect();
                            item["inputs"] = self.list(inputs.len(), items);
                        }
                        item
                    })
                    .collect();
                result["ensembles"] = self.list(ensembles.len(), items);
            }
            result["connectionCount"] = json!(
                steps
                    .iter()
                    .filter(|step| step.get("input").is_some())
                    .count()
                    + source
                        .get("ensembles")
                        .and_then(Value::as_array)
                        .map(|items| items
                            .iter()
                            .map(|item| collection_len(item.get("inputs")))
                            .sum::<usize>())
                        .unwrap_or(0)
            );
        } else {
            result["format"] = json!("unknown");
        }
        result
    }

    fn graph_node(&mut self, node: &Value) -> Value {
        let mut result = self.pick(node, &["id", "type", "class_type"]);
        result["inputCount"] = json!(collection_len(node.get("inputs")));
        result["outputCount"] = json!(collection_len(node.get("outputs")));
        let node_type = node
            .get("type")
            .or_else(|| node.get("class_type"))
            .and_then(Value::as_str)
            .unwrap_or_default();
        let widgets = node
            .get("widgets_values")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or_default();
        let parameter_type = node_type.trim_end_matches("_list");
        let keys: &[&str] = match parameter_type {
            "mss_separate" | "vr_separate" | "pymss_mss_separate" | "pymss_vr_separate" => &[
                "model",
                "device",
                "download",
                "source",
                "deviceIds",
                "debug",
            ],
            "custom_mss_separate" | "pymss_custom_mss_separate" => {
                &["model", "modelType", "device", "deviceIds", "debug"]
            }
            "pymss_mss_params" => &[
                "batch_size",
                "overlap_size",
                "chunk_size",
                "normalize",
                "enable_tta",
                "standardize",
            ],
            "pymss_vr_params" => &[
                "batch_size",
                "window_size",
                "aggression",
                "enable_tta",
                "high_end_process",
                "enable_post_process",
                "post_process_threshold",
                "normalize",
            ],
            "pymss_save_audio" => &[
                "output_format",
                "sample_rate",
                "wav_bit_depth",
                "flac_bit_depth",
                "mp3_bit_rate",
            ],
            "pymss_audio_ensemble" => &[
                "input_count",
                "ensemble_type",
                "weight_1",
                "weight_2",
                "weight_3",
                "weight_4",
                "weight_5",
                "weight_6",
                "weight_7",
                "weight_8",
                "weight_9",
                "weight_10",
            ],
            "AudioMerge" => &["merge_method", "normalize"],
            "AudioAdjustVolume" => &["volume"],
            "TrimAudioDuration" => &["start_index", "duration"],
            "AudioConcat" => &["direction"],
            _ => &[],
        };
        let is_digits = |value: Option<&Value>| {
            value.is_some_and(|value| match value {
                Value::String(text) => !text.is_empty() && text.bytes().all(|c| c.is_ascii_digit()),
                Value::Number(number) => number.as_u64().is_some(),
                _ => false,
            })
        };
        // Match the editor's layout detection, including numeric legacy folders and
        // current layouts with a trailing ComfyUI button value.
        let legacy_save = parameter_type == "pymss_save_audio"
            && (is_digits(widgets.get(2)) || (widgets.len() >= 6 && !is_digits(widgets.get(1))));
        let legacy_custom = matches!(
            parameter_type,
            "custom_mss_separate" | "pymss_custom_mss_separate"
        ) && widgets.get(3).is_some_and(Value::is_boolean)
            && widgets.get(4).is_some_and(Value::is_string);
        let mut params = Map::new();
        for (index, key) in keys.iter().enumerate() {
            let widget_index = if legacy_save && index > 0 {
                index + 1
            } else if legacy_custom && index >= 3 {
                index + 2
            } else {
                index
            };
            if let Some(value) = widgets
                .get(widget_index)
                .and_then(|value| self.scalar(value))
            {
                params.insert((*key).into(), value);
            } else if legacy_custom && *key == "debug" {
                params.insert((*key).into(), json!(false));
            }
        }
        if !params.is_empty() {
            result["parameters"] = Value::Object(params);
        }
        if matches!(
            node_type,
            "pymss_load_audio" | "LoadAudio" | "pymss_load_audio_batch"
        ) {
            result["inputConfigured"] = json!(widgets
                .first()
                .and_then(Value::as_str)
                .is_some_and(|value| !value.is_empty()));
            if node_type == "pymss_load_audio_batch" {
                for (index, key) in [(1, "recursive"), (2, "sort_files")] {
                    if let Some(value) = widgets.get(index).and_then(|value| self.scalar(value)) {
                        result[key] = value;
                    }
                }
            }
        }
        // Text nodes and output filename widgets can hold arbitrary user content.
        result["widgetCount"] = json!(widgets.len());
        result
    }
}

fn collection_len(value: Option<&Value>) -> usize {
    match value {
        Some(Value::Array(items)) => items.len(),
        Some(Value::Object(items)) => items.len(),
        _ => 0,
    }
}

fn template_summary(value: &Value) -> Value {
    let text = value.as_str().unwrap_or_default();
    let tokens: Vec<&str> = [
        "filename", "track", "stem", "model", "step", "index", "date", "time",
    ]
    .into_iter()
    .filter(|token| text.contains(&format!("%{token}%")) || text.contains(&format!("{{{token}}}")))
    .collect();
    json!({ "configured": !text.is_empty(), "chars": text.chars().count(), "knownTokens": tokens,
        "literalOmitted": !text.is_empty() })
}

fn sanitize_urls(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut urls = text
        .match_indices("://")
        .filter_map(|(scheme_end, _)| {
            let mut start = scheme_end;
            while start > 0
                && (bytes[start - 1].is_ascii_alphanumeric()
                    || matches!(bytes[start - 1], b'+' | b'-' | b'.'))
            {
                start -= 1;
            }
            (start < scheme_end && bytes[start].is_ascii_alphabetic())
                .then_some((start, scheme_end + 3))
        })
        .peekable();
    let mut output = String::with_capacity(text.len());
    let mut copied = 0;
    while let Some((start, authority_start)) = urls.next() {
        // Bound each scan by the next URL so JSON arrays and adjacent links are all visited.
        let next_start = urls.peek().map(|(start, _)| *start);
        let limit = next_start.unwrap_or(text.len());
        let single_quoted = text[..start].ends_with('\'');
        let paren_wrapped = text[..start].ends_with('(');
        let square_wrapped = text[..start].ends_with('[');
        let authority_end = text[authority_start..limit]
            .find(|c: char| c.is_whitespace() || matches!(c, '/' | '?' | '#' | '"' | '<' | '>'))
            .map(|offset| authority_start + offset)
            .unwrap_or(limit);
        let userinfo_end = text[authority_start..authority_end]
            .rfind('@')
            .map(|offset| authority_start + offset);
        let mut square_depth = 0;
        let mut paren_depth = 0;
        let end = text[authority_start..limit]
            .char_indices()
            .find_map(|(offset, c)| {
                let position = authority_start + offset;
                // Userinfo may legally contain closing brackets or parentheses.
                if userinfo_end.is_some_and(|end| position < end) {
                    return None;
                }
                match c {
                    '[' => square_depth += 1,
                    ']' if square_depth > 0 => square_depth -= 1,
                    '(' => paren_depth += 1,
                    ')' if paren_depth > 0 => paren_depth -= 1,
                    ')' if paren_wrapped => return Some(position),
                    ']' if square_wrapped
                        || (single_quoted && text[..position].ends_with('\'')) =>
                    {
                        return Some(position);
                    }
                    _ if c.is_whitespace() || matches!(c, '"' | '<' | '>') => {
                        return Some(position);
                    }
                    _ => {}
                }
                None
            })
            .unwrap_or_else(|| {
                if next_start.is_some() {
                    // Preserve punctuation separating unquoted, comma-delimited URLs.
                    authority_start
                        + text[authority_start..limit]
                            .trim_end_matches([',', ';'])
                            .len()
                } else {
                    limit
                }
            });
        // Apostrophes are valid in userinfo, paths and queries. Only peel off a
        // closing wrapper at the end, so embedded apostrophes never expose a tail.
        let end = if single_quoted {
            let candidate = text[start..end].trim_end_matches([',', ';']);
            if candidate.ends_with('\'') {
                start + candidate.len() - 1
            } else {
                end
            }
        } else {
            end
        };
        output.push_str(&text[copied..start]);
        let url_text = &text[start..end];
        if let Ok(mut url) = url::Url::parse(url_text) {
            let _ = url.set_username("");
            let _ = url.set_password(None);
            url.set_query(None);
            url.set_fragment(None);
            output.push_str(url.as_str());
        } else {
            // Malformed ports and partially copied URLs still must not expose authentication.
            let safe = url_text.split(['?', '#']).next().unwrap_or_default();
            let authority_start = authority_start - start;
            let authority_end = safe[authority_start..]
                .find('/')
                .map(|index| authority_start + index)
                .unwrap_or(safe.len());
            let host = safe[authority_start..authority_end]
                .rsplit('@')
                .next()
                .unwrap_or_default();
            output.push_str(&safe[..authority_start]);
            output.push_str(host);
            output.push_str(&safe[authority_end..]);
        }
        copied = end;
    }
    output.push_str(&text[copied..]);
    output
}

/// Keep diagnostic context while stripping URL authentication and common credential assignments.
pub(crate) fn sanitize_text(value: &str) -> String {
    let text: String = value
        .chars()
        .map(|c| {
            if c.is_control() && !matches!(c, '\r' | '\n' | '\t') {
                ' '
            } else {
                c
            }
        })
        .collect();
    let mut text = sanitize_urls(&text);
    // ASCII case conversion preserves byte positions, including surrounding Unicode.
    let sensitive = [
        "authorization",
        "proxy-authorization",
        "password",
        "passwd",
        "api_key",
        "api-key",
        "apikey",
        "access_token",
        "refresh_token",
        "token",
        "secret",
        "credential",
    ];
    let mut ranges = Vec::new();
    let lower = text.to_ascii_lowercase();
    for key in sensitive {
        for (start, _) in lower.match_indices(key) {
            if start > 0 && lower.as_bytes()[start - 1].is_ascii_alphanumeric() {
                continue;
            }
            let mut at = start + key.len();
            while lower
                .as_bytes()
                .get(at)
                .is_some_and(|c| c.is_ascii_whitespace() || matches!(c, b'\'' | b'"'))
            {
                at += 1;
            }
            if !lower
                .as_bytes()
                .get(at)
                .is_some_and(|c| matches!(c, b'=' | b':'))
            {
                continue;
            }
            at += 1;
            while lower
                .as_bytes()
                .get(at)
                .is_some_and(u8::is_ascii_whitespace)
            {
                at += 1;
            }
            let quote = lower
                .as_bytes()
                .get(at)
                .copied()
                .filter(|c| matches!(c, b'\'' | b'"'));
            if quote.is_some() {
                at += 1;
            }
            let value_start = at;
            let mut escaped = false;
            while let Some(c) = lower.as_bytes().get(at) {
                if quote.is_some() {
                    if escaped {
                        escaped = false;
                    } else if *c == b'\\' {
                        escaped = true;
                    } else if quote == Some(*c) {
                        break;
                    }
                } else if c.is_ascii_whitespace() || matches!(c, b',' | b';' | b'&' | b'}') {
                    break;
                }
                at += 1;
            }
            if at > value_start {
                ranges.push((value_start, at));
            }
            // Unquoted authorization headers can contain multiple words or comma-separated
            // Digest fields. Redact the whole line rather than just its authentication scheme.
            if key.ends_with("authorization") && quote.is_none() {
                let end = lower[value_start..]
                    .find(['\r', '\n'])
                    .map(|offset| value_start + offset)
                    .unwrap_or(lower.len());
                ranges.push((value_start, end));
            }
        }
    }
    ranges.sort_unstable();
    let mut merged: Vec<(usize, usize)> = Vec::new();
    for (start, end) in ranges {
        if let Some(last) = merged.last_mut().filter(|last| start <= last.1) {
            last.1 = last.1.max(end);
        } else {
            merged.push((start, end));
        }
    }
    for (start, end) in merged.into_iter().rev() {
        text.replace_range(start..end, "[redacted]");
    }
    text
}

pub(crate) fn summarize_request(command: &str, payload: &Value) -> Value {
    let mut summary = Summary::default();
    let mut result = summary.pick(
        payload,
        &[
            "requestId",
            "taskId",
            "jobId",
            "workflowId",
            "operation",
            "action",
        ],
    );
    result["command"] = summary.text(command, MAX_TEXT);
    let feature = match command {
        "infer" | "infer_batch" | "start_separation" => "separation",
        "infer_workflow" | "infer_workflow_batch" | "start_workflow_inference" => "workflow",
        "run_operation" | "start_operation_task" => "plugin-operations",
        "export_editor_mix" | "start_editor_mix_export" => "editor-export",
        "download_model" | "start_model_download" => "model-download",
        "delete_model"
        | "delete_tool_model"
        | "import_custom_model"
        | "inspect_custom_model"
        | "unregister_custom_model"
        | "relink_custom_model"
        | "remap_custom_model_paths"
        | "cleanup_model_residual_files" => "models",
        command if command.contains("runtime") || command.contains("optional_package") => "runtime",
        command if command.contains("update") => "application-update",
        _ => "other",
    };
    result["feature"] = json!(feature);
    if matches!(feature, "separation" | "workflow") {
        result["configuration"] = summary.parameters(payload);
        if let Some(input) = payload.get("input") {
            result["input"] = summary.basename(input);
        }
        if let Some(tasks) = payload.get("tasks").and_then(Value::as_array) {
            let items = tasks
                .iter()
                .take(MAX_ITEMS)
                .map(|task| {
                    let mut item = summary.pick(
                        task,
                        &["taskId", "jobId", "batchId", "batchIndex", "inputIndex"],
                    );
                    if let Some(input) = task.get("input") {
                        item["input"] = summary.basename(input);
                    }
                    if let Some(inputs) = task.get("inputs") {
                        item["inputs"] = summary.paths(inputs);
                    }
                    item
                })
                .collect();
            result["tasks"] = summary.list(tasks.len(), items);
            result["taskIds"] = Value::Array(
                tasks
                    .iter()
                    .take(MAX_ITEMS)
                    .filter_map(|task| task.get("taskId").and_then(|value| summary.scalar(value)))
                    .collect(),
            );
            result["taskIdsOmitted"] = json!(tasks.len().saturating_sub(MAX_ITEMS));
        }
    } else if feature == "plugin-operations" {
        // Plugin port values and parameters may contain paths or transcribed text.
        // Track the contract and counts without copying arbitrary plugin data.
        for (key, count) in [("inputs", "inputCount"), ("params", "parameterCount")] {
            result[count] = json!(payload.get(key).and_then(Value::as_object).map_or(0, |fields| fields.len()));
        }
    } else if feature == "editor-export" {
        result["parameters"] = summary.pick(payload, &["format", "projectId"]);
        if let Some(params) = payload.get("audioParams") {
            result["audioParams"] = summary.pick(params, AUDIO_FIELDS);
        }
        if let Some(directory) = payload.get("exportDir") {
            result["exportDir"] = summary.basename(directory);
        }
        if let Some(project) = payload.get("project") {
            let mut item = summary.pick(
                project,
                &[
                    "id",
                    "sourceTaskId",
                    "masterVolume",
                    "masterPan",
                    "createdAt",
                    "updatedAt",
                ],
            );
            item["sourceCount"] = json!(collection_len(project.get("sources")));
            item["assetCount"] = json!(collection_len(project.get("assets")));
            if let Some(tracks) = project.get("tracks").and_then(Value::as_array) {
                item["clipCount"] = json!(tracks
                    .iter()
                    .map(|track| collection_len(track.get("clips")))
                    .sum::<usize>());
                let items = tracks
                    .iter()
                    .take(MAX_ITEMS)
                    .map(|track| {
                        let mut item = summary.pick(
                            track,
                            &[
                                "id", "sourceId", "role", "type", "volume", "pan", "muted", "solo",
                                "fadeIn", "fadeOut",
                            ],
                        );
                        item["clipCount"] = json!(collection_len(track.get("clips")));
                        item
                    })
                    .collect();
                item["tracks"] = summary.list(tracks.len(), items);
            }
            result["project"] = item;
        }
    } else if matches!(
        feature,
        "runtime" | "application-update" | "model-download" | "models"
    ) {
        result["parameters"] = summary.pick(
            payload,
            &[
                "backend",
                "mode",
                "channel",
                "mirror",
                "locale",
                "prepareOnly",
                "repairDependencies",
                "planId",
                "onlyIfNoActive",
                "activate",
                "force",
                "model",
                "name",
                "modelName",
                "modelType",
                "architecture",
                "kind",
                "source",
                "downloadMethod",
                "timeout",
                "package",
                "packageId",
                "version",
                "targetVersion",
                "distribution",
                "prerelease",
                "modelPreset",
            ],
        );
        for key in [
            "modelDir",
            "pythonPath",
            "outputDir",
            "weightsPath",
            "configPath",
            "weights",
            "config",
            "path",
        ] {
            if let Some(value) = payload.get(key) {
                result[key] = summary.basename(value);
            }
        }
        if let Some(versions) = payload.get("versions") {
            result["versions"] = summary.pick(
                versions,
                &[
                    "pymss",
                    "pymss-core",
                    "pymss_core",
                    "torch",
                    "torchaudio",
                    "torchvision",
                ],
            );
        }
        result["customEndpointConfigured"] = json!(payload
            .get("endpoint")
            .and_then(Value::as_str)
            .is_some_and(|text| !text.is_empty()));
    }
    summary.finish(result)
}

fn task_priority(task: &Value) -> (u8, std::cmp::Reverse<i64>) {
    let status = task
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let priority = if ACTIVE_TASK_STATUSES.contains(&status) {
        0
    } else if status == "failed" {
        1
    } else {
        2
    };
    let time = task
        .get("updatedAt")
        .or_else(|| task.get("createdAt"))
        .and_then(Value::as_i64)
        .unwrap_or(0);
    (priority, std::cmp::Reverse(time))
}

pub(crate) fn summarize_task_history(store: &Value) -> Value {
    let mut summary = Summary::default();
    let tasks = store
        .get("tasks")
        .or_else(|| store.as_array().map(|_| store))
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or_default();
    let mut selected: Vec<&Value> = tasks.iter().collect();
    selected.sort_by_key(|task| task_priority(task));
    let items = selected
        .into_iter()
        .take(MAX_TASKS)
        .map(|task| {
            let mut item = summary.pick(
                task,
                &[
                    "id",
                    "jobId",
                    "resultId",
                    "batchId",
                    "batchIndex",
                    "model",
                    "status",
                    "createdAt",
                    "updatedAt",
                    "startedAt",
                    "finishedAt",
                    "durationMs",
                    "progress",
                    "stageLabel",
                    "progressCurrent",
                    "progressTotal",
                    "currentNodeId",
                    "taskHidden",
                    "resultHidden",
                ],
            );
            if let Some(error) = task.get("error").and_then(Value::as_str) {
                item["error"] = summary.text(error, MAX_ERROR);
            }
            for key in ["input", "output", "jobOutput"] {
                if let Some(path) = task.get(key) {
                    item[key] = summary.basename(path);
                }
            }
            for key in ["files", "outputDirectories"] {
                if let Some(paths) = task.get(key) {
                    item[key] = summary.paths(paths);
                }
            }
            item["outputCount"] = json!(collection_len(task.get("outputs")));
            item["logCount"] = json!(collection_len(task.get("logs")));
            if let Some(config) = task.get("runConfig") {
                item["runConfig"] = summary.parameters(config);
            }
            if let Some(order) = task.get("nodeExecutionOrder") {
                item["nodeExecutionOrder"] = summary.scalars(order);
            }
            if let Some(states) = task.get("nodeStates").and_then(Value::as_object) {
                let mut states: Vec<_> = states.iter().collect();
                states.sort_by_key(|(_, state)| {
                    match state.get("status").and_then(Value::as_str) {
                        Some("running") | Some("failed") => 0,
                        _ => 1,
                    }
                });
                let items = states
                    .iter()
                    .take(MAX_ITEMS)
                    .map(|(id, state)| {
                        let mut item = summary.pick(state, &["status", "progress"]);
                        if let Some(message) = state.get("message").and_then(Value::as_str) {
                            item["message"] = summary.text(message, MAX_ERROR);
                        }
                        item["nodeId"] = summary.text(id, MAX_TEXT);
                        if let Some(counts) = state.get("summary") {
                            item["summary"] = summary.pick(
                                counts,
                                &[
                                    "completed",
                                    "total",
                                    "failed",
                                    "cancelled",
                                    "skipped",
                                    "unknown",
                                ],
                            );
                        }
                        item
                    })
                    .collect();
                item["nodeStates"] = summary.list(states.len(), items);
            }
            item
        })
        .collect();
    let mut status_counts = Map::new();
    for status in
        ACTIVE_TASK_STATUSES
            .iter()
            .copied()
            .chain(["done", "failed", "cancelled", "interrupted"])
    {
        status_counts.insert(
            status.into(),
            json!(tasks
                .iter()
                .filter(|task| task.get("status").and_then(Value::as_str) == Some(status))
                .count()),
        );
    }
    let mut result = json!({ "statusCounts": status_counts });
    result["tasks"] = summary.list(tasks.len(), items);
    summary.finish(result)
}

pub(crate) fn summarize_settings(store: &Value) -> Value {
    let mut summary = Summary::default();
    let mut result = summary.pick(
        store,
        &[
            "themeMode",
            "themeAccent",
            "scaleFactor",
            "locale",
            "animationsEnabled",
            "developerMode",
            "defaultDevice",
            "defaultFormat",
            "downloadSource",
            "downloadMethod",
            "maxConcurrentSeparations",
            "clearInputAfterSubmit",
            "wavBitDepth",
            "flacBitDepth",
            "mp3BitRate",
            "m4aBitRate",
            "m4aCodec",
            "startupOnboardingSeen",
            "startupOnboardingSeenVersion",
            "proxyMode",
            "updateChannel",
            "updateSource",
            "pluginSource",
            "pluginDefaultsPolicy",
        ],
    );
    for key in ["modelDir", "outputDir"] {
        result[format!("{key}Configured")] = json!(store
            .get(key)
            .and_then(Value::as_str)
            .is_some_and(|value| !value.is_empty()));
    }
    result["customUpdateEndpointConfigured"] = json!(store
        .get("updateEndpointOverride")
        .and_then(Value::as_str)
        .is_some_and(|value| !value.is_empty()));
    summary.finish(result)
}

pub(crate) fn summarize_workflows(store: &Value) -> Value {
    let mut summary = Summary::default();
    let workflows = store
        .get("workflows")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or_default();
    let mut selected: Vec<&Value> = workflows.iter().collect();
    selected.sort_by_key(|workflow| {
        std::cmp::Reverse(
            workflow
                .get("updatedAt")
                .and_then(Value::as_i64)
                .unwrap_or(0),
        )
    });
    let items = selected
        .into_iter()
        .take(MAX_ITEMS)
        .map(|workflow| {
            let mut item = summary.pick(
                workflow,
                &[
                    "id",
                    "name",
                    "format",
                    "formatVersion",
                    "createdAt",
                    "updatedAt",
                ],
            );
            item["definition"] =
                summary.workflow(workflow.get("definition").unwrap_or(&Value::Null));
            item
        })
        .collect();
    let mut result = summary.pick(store, &["selectedWorkflowId"]);
    result["workflows"] = summary.list(workflows.len(), items);
    summary.finish(result)
}

/// Cache only bounded capability facts, never a raw environment or package response.
fn summarize_plugin_diagnostics(payload: &Value) -> Value {
    let mut summary = Summary::default();
    let mut result = summary.pick(payload, &["checkedAt"]);
    let runtime = &payload["runtime"];
    let mut environment = summary.pick(
        runtime,
        &[
            "identity",
            "pymssVersion",
            "managementSupported",
            "pluginApisSupported",
            "interpreterMatches",
        ],
    );
    for field in ["pythonPath", "configuredPythonPath", "bindingDir"] {
        environment[field] = summary.basename(&runtime[field]);
    }
    if let Some(error) = runtime["apiError"].as_str() {
        environment["apiError"] = summary.text(error, MAX_ERROR);
    }
    if let Some(error) = runtime["bindingError"].as_str() {
        environment["bindingError"] = summary.text(error, MAX_ERROR);
    }
    result["runtime"] = environment;
    let plugins = payload["plugins"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default();
    result["enabledCount"] = json!(plugins
        .iter()
        .filter(|plugin| plugin["enabled"] == true)
        .count());
    result["failedCount"] = json!(plugins
        .iter()
        .filter(|plugin| plugin["enabled"] == true
            && (plugin["loaded"] == false
                || plugin["error"]
                    .as_str()
                    .is_some_and(|error| !error.is_empty())))
        .count());
    let items = plugins
        .iter()
        .take(MAX_ITEMS)
        .map(|plugin| {
            let mut item = summary.pick(plugin, &["name", "version", "enabled", "loaded"]);
            if let Some(error) = plugin["error"].as_str() {
                item["error"] = summary.text(error, MAX_ERROR);
            }
            item
        })
        .collect();
    result["plugins"] = summary.list(plugins.len(), items);
    result["verification"] = summary.pick(&payload["verification"], &["performed"]);
    if let Some(error) = payload["verification"]["error"].as_str() {
        result["verification"]["error"] = summary.text(error, MAX_ERROR);
    }
    summary.finish(result)
}

pub(crate) fn summarize_window_state(payload: &Value) -> Value {
    let mut summary = Summary::default();
    let mut result = summary.pick(
        payload,
        &[
            "kind",
            "workflowId",
            "projectId",
            "updatedAt",
            "dirty",
            "saving",
            "revisionConflict",
            "recoveryBackupAvailable",
            "loading",
            "exporting",
            "exportCancelling",
            "exportPhase",
            "exportCompleted",
            "exportTotal",
            "hasLastExport",
            "playbackStatus",
            "recordingState",
            "pluginStateDirty",
            "pluginStateUnavailable",
        ],
    );
    for key in [
        "error",
        "playbackError",
        "recordingError",
        "pluginStateError",
    ] {
        if let Some(error) = payload[key].as_str() {
            result[key] = summary.text(error, MAX_ERROR);
        }
    }
    if let Some(path) = payload["lastExportPath"].as_str() {
        result["lastExportPath"] = summary.text(path, 4096);
    }
    if payload["rendering"].is_object() {
        result["rendering"] = summarize_rendering(&payload["rendering"]);
    }
    if let Some(changes) = payload["changes"].as_array() {
        result["changes"] = json!(changes
            .iter()
            .rev()
            .take(20)
            .rev()
            .map(summarize_render_change)
            .collect::<Vec<_>>());
    }
    summary.finish(result)
}

pub(crate) fn summarize_window_report(payload: &Value) -> Value {
    let mut summary = Summary::default();
    let mut result = summary.pick(
        payload,
        &[
            "kind",
            "observedAt",
            "updatedAt",
            "dirty",
            "saving",
            "revisionConflict",
            "recoveryBackupAvailable",
            "loading",
            "exporting",
            "exportCancelling",
            "exportPhase",
            "exportCompleted",
            "exportTotal",
            "hasLastExport",
            "playbackStatus",
            "recordingState",
            "pluginStateDirty",
            "pluginStateUnavailable",
        ],
    );
    for key in [
        "error",
        "playbackError",
        "recordingError",
        "pluginStateError",
    ] {
        result[format!("{key}Present")] =
            json!(payload[key].as_str().is_some_and(|value| !value.is_empty()));
    }
    if payload["rendering"].is_object() {
        result["rendering"] = summarize_rendering(&payload["rendering"]);
    }
    if let Some(changes) = payload["changes"].as_array() {
        result["changes"] = json!(changes
            .iter()
            .rev()
            .take(20)
            .rev()
            .map(summarize_render_change)
            .collect::<Vec<_>>());
    }
    summary.finish(result)
}

fn summarize_render_change(payload: &Value) -> Value {
    let mut summary = Summary::default();
    let mut result = summary.pick(
        payload,
        &[
            "at",
            "theme",
            "resolvedTheme",
            "animations",
            "scale",
            "locale",
            "resolvedLocale",
        ],
    );
    if let Some(route) = payload["routePath"].as_str() {
        result["routePath"] =
            summary.text(route.split(['?', '#']).next().unwrap_or_default(), MAX_TEXT);
    }
    summary.finish(result)
}

fn summarize_rendering(payload: &Value) -> Value {
    let mut summary = Summary::default();
    let mut result = summary.pick(
        payload,
        &[
            "checkedAt",
            "configuredTheme",
            "resolvedTheme",
            "configuredAnimations",
            "configuredScale",
            "configuredLocale",
            "resolvedLocale",
            "userAgent",
            "platform",
            "language",
            "documentLanguage",
            "visibility",
            "themeClass",
            "animationsDisabled",
            "devicePixelRatio",
            "viewportWidth",
            "viewportHeight",
            "prefersDark",
            "prefersReducedMotion",
            "surfaceColor",
            "textColor",
            "pageRoots",
            "visiblePageRoots",
            "modalRoots",
            "visibleModalRoots",
            "sampleLimit",
            "viewTransitionsAvailable",
        ],
    );
    if let Some(route) = payload["routePath"].as_str() {
        result["routePath"] =
            summary.text(route.split(['?', '#']).next().unwrap_or_default(), MAX_TEXT);
    }
    summary.finish(result)
}

fn summarize_network(payload: &Value) -> Value {
    let mut summary = Summary::default();
    let mut result = summary.pick(
        payload,
        &[
            "checkedAt",
            "proxyMode",
            "observedProxyMode",
            "timeoutSeconds",
            "readLimitBytes",
        ],
    );
    result["configuration"] = summary.pick(
        &payload["configuration"],
        &[
            "modelSource",
            "pluginSource",
            "proxyMode",
            "updateSource",
            "channel",
            "locale",
            "customUpdateEndpoint",
        ],
    );
    let checks = payload["checks"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default();
    let entries = checks
        .iter()
        .take(4)
        .map(|check| {
            let mut item = summary.pick(
                check,
                &[
                    "id",
                    "status",
                    "kind",
                    "source",
                    "sourcePreference",
                    "host",
                    "httpStatus",
                    "elapsedMs",
                    "errorCode",
                    "gitAvailable",
                    "catalogCount",
                    "outcome",
                ],
            );
            item["errorPresent"] = json!(check["error"]
                .as_str()
                .is_some_and(|value| !value.is_empty()));
            item
        })
        .collect();
    result["checks"] = summary.list(checks.len(), entries);
    summary.finish(result)
}

pub(crate) fn summarize_native_host(payload: &Value) -> Value {
    let mut summary = Summary::default();
    let mut result = summary.pick(payload, &["stopping"]);
    result["capabilities"] = summary.pick(
        &payload["capabilities"],
        &[
            "enabled",
            "platform",
            "nativeEditor",
            "nativeTransport",
            "recording",
            "scanControl",
            "catalogCache",
            "labBuild",
        ],
    );
    result["capabilities"]["formats"] = summary.scalars(&payload["capabilities"]["formats"]);
    result["session"] = summary.pick(&payload["session"], &["status", "hasProcess", "generation"]);
    result["renders"] = summary.pick(&payload["renders"], &["status", "activeCount"]);
    result["scan"] = summary.pick(
        &payload["scan"],
        &["status", "completed", "total", "elapsedMs", "canSkip"],
    );
    result["storage"] = summary.pick(&payload["storage"], &["status"]);
    result["storage"]["cache"] = summary.pick(
        &payload["storage"]["cache"],
        &[
            "status",
            "version",
            "entryCount",
            "descriptorCount",
            "failureCount",
        ],
    );
    result["storage"]["blacklist"] = summary.pick(
        &payload["storage"]["blacklist"],
        &["status", "count", "manualCount", "timeoutCount"],
    );
    summary.finish(result)
}

pub(crate) fn summarize_diagnostic_page(payload: &Value) -> Value {
    let mut summary = Summary::default();
    let mut result = summary.pick(payload, &["observedAt"]);
    let plugins = &payload["pluginDiagnostics"];
    result["pluginDiagnostics"] = summary.pick(plugins, &["loading"]);
    if let Some(error) = plugins["error"].as_str() {
        result["pluginDiagnostics"]["error"] = summary.text(error, MAX_ERROR);
    }
    result["pluginDiagnostics"]["snapshot"] = if plugins["snapshot"].is_object() {
        summarize_plugin_diagnostics(&plugins["snapshot"])
    } else {
        json!({"status": "not_observed"})
    };
    let update = &payload["applicationUpdate"];
    let mut update_summary = summary.pick(
        update,
        &[
            "status",
            "currentVersion",
            "latestVersion",
            "selectedSource",
            "lastCheckedAt",
            "downloadDownloadedBytes",
            "downloadTotalBytes",
            "downloadSpeedBytesPerSecond",
            "downloadProgressPercent",
            "restartRequired",
            "installFailed",
            "integrityChecking",
            "repairing",
        ],
    );
    for field in ["error", "integrityError"] {
        if let Some(error) = update[field].as_str() {
            update_summary[field] = summary.text(error, MAX_ERROR);
        }
    }
    update_summary["integrity"] = if update["integrity"].is_object() {
        let mut integrity = summary.pick(
            &update["integrity"],
            &["supported", "valid", "repairSupported"],
        );
        if let Some(detail) = update["integrity"]["detail"].as_str() {
            integrity["detail"] = summary.text(detail, MAX_ERROR);
        }
        integrity
    } else {
        Value::Null
    };
    let logs = update["downloadLogs"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or_default();
    let entries = logs
        .iter()
        .rev()
        .take(20)
        .rev()
        .map(|entry| {
            let mut item = summary.pick(entry, &["at"]);
            if let Some(message) = entry["message"].as_str() {
                item["message"] = summary.text(message, MAX_ERROR);
            }
            item
        })
        .collect();
    update_summary["downloadLogs"] = summary.list(logs.len(), entries);
    result["applicationUpdate"] = update_summary;
    if let Some(workflow) = payload.get("workflowResults") {
        let mut item = summary.pick(
            &workflow["workflow"],
            &["initialized", "count", "selectedWorkflowId", "isSaving"],
        );
        item["recoveryBackupPath"] = summary.basename(&workflow["workflow"]["recoveryBackupPath"]);
        item["lastMutationError"] =
            summary.pick(&workflow["workflow"]["lastMutationError"], &["code", "at"]);
        if let Some(error) = workflow["workflow"]["lastMutationError"]["message"].as_str() {
            item["lastMutationError"]["message"] = summary.text(error, MAX_ERROR);
        }
        result["workflowResults"]["workflow"] = item;
        result["workflowResults"]["results"] = summary.pick(
            &workflow["results"],
            &[
                "completedCount",
                "visibleCount",
                "hiddenCount",
                "omittedTasks",
                "omittedPaths",
            ],
        );
        let tasks = workflow["results"]["tasks"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or_default();
        let items = tasks
            .iter()
            .take(MAX_TASKS)
            .map(|task| {
                summary.pick(
                    task,
                    &[
                        "id",
                        "visible",
                        "resultHidden",
                        "outputCount",
                        "fileCount",
                        "nodeValueCount",
                        "reason",
                    ],
                )
            })
            .collect();
        result["workflowResults"]["results"]["tasks"] = summary.list(tasks.len(), items);
    }
    if let Some(outputs) = payload
        .get("outputChecks")
        .filter(|value| value.is_object())
    {
        result["outputChecks"] = summary.pick(outputs, &["checkedAt"]);
        let files = outputs["files"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or_default();
        let entries = files
            .iter()
            .take(MAX_ITEMS)
            .map(|file| {
                let mut item = summary.pick(file, &["state", "bytes"]);
                item["name"] = summary.basename(&file["path"]);
                if let Some(error) = file["error"].as_str() {
                    item["error"] = summary.text(error, MAX_ERROR);
                }
                item
            })
            .collect();
        result["outputChecks"]["files"] = summary.list(files.len(), entries);
    }
    if let Some(business) = payload
        .get("businessDiagnostics")
        .filter(|value| value.is_object())
    {
        let mut item = summary.pick(business, &["checkedAt", "requestId"]);
        item["windows"] = summary.pick(&business["windows"], &["status"]);
        item["windows"]["expectedCount"] =
            json!(collection_len(business["windows"].get("expected")));
        item["windows"]["missingCount"] = json!(collection_len(business["windows"].get("missing")));
        let states = business["windows"]["observed"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or_default();
        let observations = states
            .iter()
            .take(8)
            .map(|state| summarize_window_report(state))
            .collect();
        item["windows"]["observed"] = summary.list(states.len(), observations);
        result["businessDiagnostics"] = item;
    }
    if let Some(network) = payload
        .get("networkDiagnostics")
        .filter(|value| value.is_object())
    {
        result["networkDiagnostics"] = summarize_network(network);
    }
    if let Some(storage) = payload
        .get("storageDiagnostics")
        .filter(|value| value.is_object())
    {
        let mut item = summary.pick(storage, &["checkedAt", "writeProbe"]);
        let directories = storage["directories"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or_default();
        let entries = directories
            .iter()
            .take(7)
            .map(|directory| {
                let mut value = summary.pick(
                    directory,
                    &[
                        "id",
                        "state",
                        "availableBytes",
                        "readonlyAttribute",
                        "writeStatus",
                    ],
                );
                for key in ["error", "writeError", "cleanupError"] {
                    value[format!("{key}Present")] = json!(directory[key]
                        .as_str()
                        .is_some_and(|message| !message.is_empty()));
                }
                value
            })
            .collect();
        item["directories"] = summary.list(directories.len(), entries);
        result["storageDiagnostics"] = item;
    }
    if let Some(renderer) = payload
        .get("rendererDiagnostics")
        .filter(|value| value.is_object())
    {
        let mut item = summary.pick(renderer, &["checkedAt", "webviewVersion", "os", "arch"]);
        item["webviewErrorPresent"] = json!(renderer["webviewError"]
            .as_str()
            .is_some_and(|value| !value.is_empty()));
        item["missingWindowCount"] = json!(collection_len(renderer["windows"].get("missing")));
        let windows = renderer["windows"]["observed"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or_default();
        item["windows"] = summary.list(
            windows.len(),
            windows
                .iter()
                .take(8)
                .map(summarize_window_report)
                .collect(),
        );
        result["rendererDiagnostics"] = item;
    }
    summary.finish(result)
}

pub(crate) fn summarize_probe(command: &str, payload: &Value) -> Value {
    if command == "network_diagnostics" {
        return summarize_network(payload);
    }
    if command == "plugin_diagnostics" {
        return summarize_plugin_diagnostics(payload);
    }
    let mut summary = Summary::default();
    let mut result = summary.pick(
        payload,
        &[
            "pythonVersion",
            "platform",
            "machine",
            "workerVersion",
            "manifestVersion",
            "ready",
            "backend",
            "installedBackend",
            "pymssAvailable",
            "customModelsSupported",
            "pymssVersion",
            "pymssCoreVersion",
            "pymssGraphAvailable",
            "torchAvailable",
            "torchVersion",
            "torchBackend",
            "hipVersion",
            "cudaAvailable",
            "cudaDeviceCount",
            "mpsAvailable",
            "mlxAvailable",
            "acceleratorAvailable",
            "avAvailable",
            "librosaAvailable",
            "audioreadAvailable",
            "ffmpegAvailable",
            "ffprobeAvailable",
            "rollbackAvailable",
            "package",
            "requirement",
            "installed",
            "present",
            "version",
            "source",
            "canUninstall",
            "issue",
        ],
    );
    result["command"] = summary.text(command, MAX_TEXT);
    for field in [
        "pymssError",
        "torchError",
        "cudaAvailableError",
        "cudaDeviceCountError",
        "cudaDeviceNamesError",
    ] {
        if let Some(error) = payload.get(field).and_then(Value::as_str) {
            result[field] = summary.text(error, MAX_ERROR);
        }
    }
    if let Some(error) = payload.get("liveProbeError") {
        let mut item = summary.pick(error, &["code"]);
        if let Some(message) = error.get("message").and_then(Value::as_str) {
            item["message"] = summary.text(message, MAX_ERROR);
        }
        result["liveProbeError"] = item;
    }
    for field in ["packages", "packageVersions"] {
        if let Some(value) = payload.get(field) {
            result[field] = summary.pick(value, PACKAGE_FIELDS);
        }
    }
    if let Some(vendors) = payload.get("gpuVendors") {
        result["gpuVendors"] = summary.scalars(vendors);
    }
    if let Some(devices) = payload.get("cudaDevices").and_then(Value::as_array) {
        let items = devices
            .iter()
            .take(MAX_ITEMS)
            .map(|device| {
                summary.pick(
                    device,
                    &["id", "name", "totalMemoryBytes", "major", "minor"],
                )
            })
            .collect();
        result["cudaDevices"] = summary.list(devices.len(), items);
    }
    let environment_fields = [
        "backend",
        "source",
        "health",
        "manifestVersion",
        "installedAt",
        "updatedAt",
        "pythonVersion",
        "torchVersion",
        "torchBackend",
        "acceleratorAvailable",
        "pymssVersion",
        "pymssCoreVersion",
        "pymssGraphAvailable",
        "coreUpdateSupported",
    ];
    for field in ["installState", "activeOverlay"] {
        if let Some(state) = payload.get(field).filter(|value| value.is_object()) {
            let mut item = summary.pick(state, &environment_fields);
            for key in ["packages", "packageVersions"] {
                if let Some(packages) = state.get(key) {
                    item[key] = summary.pick(packages, PACKAGE_FIELDS);
                }
            }
            item["overlayConfigured"] = json!(state
                .get("overlayPath")
                .and_then(Value::as_str)
                .is_some_and(|value| !value.is_empty()));
            result[field] = item;
        }
    }
    if let Some(environments) = payload
        .get("installedEnvironments")
        .and_then(Value::as_array)
    {
        let items = environments
            .iter()
            .take(8)
            .map(|environment| {
                let mut item = summary.pick(environment, &environment_fields);
                for key in ["packages", "packageVersions"] {
                    if let Some(packages) = environment.get(key) {
                        item[key] = summary.pick(packages, PACKAGE_FIELDS);
                    }
                }
                item["overlayConfigured"] = json!(environment
                    .get("overlayPath")
                    .and_then(Value::as_str)
                    .is_some_and(|value| !value.is_empty()));
                item
            })
            .collect();
        result["installedEnvironments"] = summary.list(environments.len(), items);
    }
    summary.finish(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn system_diagnostics_reports_keep_health_without_credentials_paths_or_route_queries() {
        let page = summarize_diagnostic_page(&json!({
            "networkDiagnostics": {"observedProxyMode": "custom", "configuration": {
                "customUpdateEndpoint": true, "endpointOverride": "https://alice:private-secret@private.test"},
                "checks": [{"id": "models", "status": "timeout", "host": "example.test", "error": "private-secret"}]},
            "storageDiagnostics": {"writeProbe": true, "directories": [{"id": "data", "state": "directory",
                "path": "/private/directory", "probePath": "/private/probe", "writeStatus": "cleanup_failed", "cleanupError": "private-secret"}]},
            "rendererDiagnostics": {"webviewVersion": "123.4", "webviewError": "private-secret", "windows": {
                "missing": ["private-window"], "observed": [{"kind": "renderer", "windowLabel": "private-window",
                    "rendering": {"routePath": "/debug?private-secret#fragment", "themeClass": "mixed", "dom": "private-secret"},
                    "changes": (0..30).map(|_| json!({"routePath": "/models?private-secret", "theme": "dark"})).collect::<Vec<_>>() }]}}
        }));
        assert_eq!(
            page["networkDiagnostics"]["checks"]["items"][0]["status"],
            "timeout"
        );
        assert_eq!(page["networkDiagnostics"]["observedProxyMode"], "custom");
        assert_eq!(
            page["storageDiagnostics"]["directories"]["items"][0]["writeStatus"],
            "cleanup_failed"
        );
        assert_eq!(page["rendererDiagnostics"]["missingWindowCount"], 1);
        let rendering = &page["rendererDiagnostics"]["windows"]["items"][0];
        assert_eq!(rendering["rendering"]["routePath"], "/debug");
        assert_eq!(rendering["changes"].as_array().unwrap().len(), 20);
        assert!(!page.to_string().contains("private"));
    }

    #[test]
    fn business_reports_keep_fault_states_without_media_paths_or_host_identifiers() {
        let window = json!({"kind": "editor", "windowLabel": "editor-private", "projectId": "/private/project", "lastExportPath": "/private/export.wav",
            "error": "Cannot decode /private/audio.wav", "playbackError": "Device failed", "exporting": true,
            "graph": {"text": "private contents"}});
        let report = summarize_window_report(&window);
        assert_eq!(report["errorPresent"], true);
        assert_eq!(report["exporting"], true);
        assert!(!report.to_string().contains("private"));
        let host = summarize_native_host(
            &json!({"session": {"status": "active", "projectId": "/private/project", "hasProcess": true},
            "renders": {"status": "active", "activeCount": 1, "taskIds": ["private"]}}),
        );
        assert_eq!(host["renders"]["activeCount"], 1);
        assert!(!host.to_string().contains("private"));
        let page = summarize_diagnostic_page(
            &json!({"workflowResults": {"workflow": {"isSaving": true,
            "recoveryBackupPath": "/private/backup.json", "definition": "private graph"},
            "results": {"completedCount": 1, "tasks": [{"id": "task", "reason": "hidden"}]}},
            "businessDiagnostics": {"windows": {"expected": ["private"], "missing": ["private"], "observed": [window]}}}),
        );
        assert_eq!(page["workflowResults"]["workflow"]["isSaving"], true);
        assert_eq!(
            page["workflowResults"]["workflow"]["recoveryBackupPath"],
            "backup.json"
        );
        assert_eq!(page["businessDiagnostics"]["windows"]["missingCount"], 1);
        assert!(!page.to_string().contains("private"));
    }

    #[test]
    fn diagnostic_page_snapshots_keep_plugin_failures_and_redact_update_logs() {
        let snapshot = json!({
            "observedAt": "2026-11-03T00:00:00Z", "secret": "never-include",
            "pluginDiagnostics": {"loading": false, "error": "", "snapshot": {
                "checkedAt": "2026-11-03T00:00:00Z",
                "runtime": {"pythonPath": "/private/env/bin/python", "configuredPythonPath": "/other/bin/python",
                    "bindingDir": "/private/plugins/cuda", "identity": "cuda", "interpreterMatches": false,
                    "managementSupported": false, "pluginApisSupported": false, "apiError": "Missing APIs"},
                "plugins": [{"name": "example", "enabled": true, "loaded": false, "error": "Import failed", "private": "never-include"}],
                "verification": {"performed": true, "error": null}}},
            "applicationUpdate": {"status": "failed", "restartRequired": true,
                "error": "Checksum mismatch", "integrity": {"supported": true, "valid": false},
                "downloadLogs": [{"at": "now", "message": "https://user:pass@example.com/archive?token=hidden"}],
                "credentials": "never-include"},
        });
        let report = summarize_diagnostic_page(&snapshot);
        assert_eq!(
            report["pluginDiagnostics"]["snapshot"]["runtime"]["interpreterMatches"],
            false
        );
        assert_eq!(report["pluginDiagnostics"]["snapshot"]["failedCount"], 1);
        assert_eq!(report["applicationUpdate"]["status"], "failed");
        assert_eq!(report["applicationUpdate"]["integrity"]["valid"], false);
        let text = report.to_string();
        for forbidden in [
            "never-include",
            "/private/",
            "/other/",
            "user:pass",
            "hidden",
        ] {
            assert!(!text.contains(forbidden), "{forbidden}");
        }
    }

    #[test]
    fn diagnostic_page_snapshots_bound_collections_and_keep_unobserved_state() {
        let logs: Vec<_> = (0..30)
            .map(|i| json!({"at": i.to_string(), "message": "x".repeat(1000)}))
            .collect();
        let plugins: Vec<_> = (0..40)
            .map(|i| json!({"name": format!("plugin-{i}"), "enabled": true, "loaded": null}))
            .collect();
        let report = summarize_diagnostic_page(&json!({
            "pluginDiagnostics": {"snapshot": {"runtime": {}, "plugins": plugins}},
            "applicationUpdate": {"downloadLogs": logs},
        }));
        assert_eq!(
            report["pluginDiagnostics"]["snapshot"]["plugins"]["included"],
            MAX_ITEMS
        );
        assert_eq!(report["applicationUpdate"]["downloadLogs"]["included"], 20);
        assert_eq!(report["applicationUpdate"]["downloadLogs"]["omitted"], 10);
        assert_eq!(
            report["applicationUpdate"]["downloadLogs"]["items"][0]["at"],
            "10"
        );
        assert!(report["applicationUpdate"]["integrity"].is_null());
        let missing = summarize_diagnostic_page(&json!({}));
        assert_eq!(
            missing["pluginDiagnostics"]["snapshot"]["status"],
            "not_observed"
        );
    }

    #[test]
    fn requests_keep_batch_correlations_and_effective_inference_parameters() {
        let report = summarize_request(
            "infer_batch",
            &json!({
                "taskId": "batch", "jobId": "job", "model": "roformer", "device": "cuda", "deviceIds": [1, 2],
                "tasks": [{"taskId": "child", "input": "C:\\Users\\private\\song.wav"}],
                "inferenceParams": {"batch_size": 2, "chunk_size": 352800, "token": "sensitive"},
                "selectedStems": ["vocals"], "outputNaming": {"enabled": true, "template": "private_%filename%_%stem%"},
                "secret": "sensitive", "proxyUrl": "http://name:password@proxy:8080"
            }),
        );
        assert_eq!(report["feature"], "separation");
        assert_eq!(report["taskIds"][0], "child");
        assert_eq!(report["tasks"]["items"][0]["input"], "song.wav");
        assert_eq!(report["configuration"]["inferenceParams"]["batch_size"], 2);
        let text = report.to_string();
        assert!(!text.contains("sensitive"));
        assert!(!text.contains("private"));
        assert!(!text.contains("proxyUrl"));
    }

    #[test]
    fn native_graph_snapshot_retains_known_widgets_without_text_nodes() {
        let graph = json!({"nodes": [
            {"id": 1, "type": "mss_separate", "widgets_values": ["model.ckpt", "cuda", true, "modelscope", "1", true]},
            {"id": 2, "type": "StringConstant", "widgets_values": ["private text"]},
            {"id": 3, "type": "pymss_load_audio", "widgets_values": ["C:\\private\\input.wav"]}
        ], "links": [[1, 3, 0, 1, 0, "AUDIO"]]});
        let report = summarize_request(
            "infer_workflow",
            &json!({"workflowId": "w", "workbench": true, "nativeGraph": true, "workflow": graph}),
        );
        let workflow = &report["configuration"]["workflow"];
        assert_eq!(workflow["connectionCount"], 1);
        assert_eq!(
            workflow["nodes"]["items"][0]["parameters"]["device"],
            "cuda"
        );
        assert_eq!(workflow["nodes"]["items"][2]["inputConfigured"], true);
        assert!(!report.to_string().contains("private"));
        let snapshot = summarize_task_history(
            &json!({"tasks": [{"id": "t", "status": "failed", "runConfig": {
                "runMode": "workflow", "workbench": {"version": 1, "nativeGraph": true, "sourceDefinition": graph,
                    "bindings": {"primaryInput": "3", "inputs": {"3": {"files": ["D:/private/input.wav"]}}, "outputDirectory": "D:/private/out"}}
            }}]}),
        );
        assert_eq!(
            snapshot["tasks"]["items"][0]["runConfig"]["workbench"]["nativeGraph"],
            true
        );
        assert!(!snapshot.to_string().contains("private"));
    }

    #[test]
    fn save_node_snapshots_follow_legacy_and_current_widget_layouts() {
        for (values, expected_rate) in [
            (
                json!(["mp3", "private-folder", "48000", "FLOAT", "PCM_16", "192k"]),
                json!("48000"),
            ),
            (
                json!(["mp3", "2026", "48000", "FLOAT", "PCM_16", "192k"]),
                json!("48000"),
            ),
            (
                json!(["mp3", "48000", "FLOAT", "PCM_16", "192k", null]),
                json!("48000"),
            ),
            (
                json!(["mp3", 48000, "FLOAT", "PCM_16", "192k"]),
                json!(48000),
            ),
        ] {
            let report = summarize_request(
                "infer_workflow",
                &json!({"workflow": {
                    "nodes": [{"id": 1, "type": "pymss_save_audio", "widgets_values": values}]
                }}),
            );
            let parameters =
                &report["configuration"]["workflow"]["nodes"]["items"][0]["parameters"];
            assert_eq!(parameters["output_format"], "mp3");
            assert_eq!(parameters["sample_rate"], expected_rate);
            assert_eq!(parameters["wav_bit_depth"], "FLOAT");
            assert_eq!(parameters["flac_bit_depth"], "PCM_16");
            assert_eq!(parameters["mp3_bit_rate"], "192k");
            assert!(!report.to_string().contains("private-folder"));
        }
    }

    #[test]
    fn separator_snapshots_accept_aliases_and_legacy_custom_widgets() {
        for suffix in ["", "_list"] {
            for prefix in ["", "pymss_"] {
                for values in [
                    json!([
                        "custom.ckpt",
                        "bs_roformer",
                        "cpu",
                        true,
                        "modelscope",
                        "0,1",
                        true
                    ]),
                    json!(["custom.ckpt", "bs_roformer", "cpu", "0,1", true]),
                ] {
                    let report = summarize_request(
                        "infer_workflow",
                        &json!({"workflow": {
                            "nodes": [{"id": 1, "type": format!("{prefix}custom_mss_separate{suffix}"), "widgets_values": values}]
                        }}),
                    );
                    let parameters =
                        &report["configuration"]["workflow"]["nodes"]["items"][0]["parameters"];
                    assert_eq!(parameters["device"], "cpu");
                    assert_eq!(parameters["deviceIds"], "0,1");
                    assert_eq!(parameters["debug"], true);
                    assert!(parameters.get("download").is_none());
                }
            }
        }
    }

    #[test]
    fn simple_workflows_include_ensemble_weights_and_omit_templates() {
        let report = summarize_workflows(
            &json!({"selectedWorkflowId": "w", "workflows": [{"id": "w", "name": "Two stages", "definition": {
                "version": 1, "defaults": {"device": "cpu", "inference_params": {"overlap_size": 4}},
                "steps": [{"id": "s1", "model": "m1", "input": "input", "stems": ["vocals"], "save": {"vocals": "Default"}, "output_names": {"vocals": "private title"}}],
                "ensembles": [{"id": "e", "algorithm": "avg_wave", "inputs": [{"source": "s1.vocals", "weight": 0.5}], "output_stem": "vocals", "save": "private destination"}]
            }}]}),
        );
        let definition = &report["workflows"]["items"][0]["definition"];
        assert_eq!(definition["format"], "simple");
        assert_eq!(
            definition["ensembles"]["items"][0]["inputs"]["items"][0]["weight"],
            0.5
        );
        assert_eq!(definition["steps"]["items"][0]["saveCount"], 1);
        assert!(!report.to_string().contains("private"));
    }

    #[test]
    fn history_prefers_active_and_failed_and_bounds_nested_collections() {
        let mut tasks: Vec<Value> = (0..20)
            .map(|n| json!({"id": format!("done{n}"), "status": "done", "updatedAt": n}))
            .collect();
        tasks.push(json!({"id": "failed", "status": "failed", "updatedAt": -1, "error": "错".repeat(800),
            "nodeExecutionOrder": (0..30).map(|n| n.to_string()).collect::<Vec<_>>(),
            "nodeStates": {"last": {"status": "failed", "progress": 50, "message": "Node failed token=\"private content\""}}}));
        tasks.push(json!({"id": "active", "status": "separating", "updatedAt": -2}));
        let report = summarize_task_history(&json!({"tasks": tasks}));
        assert_eq!(report["tasks"]["included"], MAX_TASKS);
        assert_eq!(report["tasks"]["omitted"], 10);
        assert_eq!(report["tasks"]["items"][0]["id"], "active");
        assert_eq!(report["tasks"]["items"][1]["id"], "failed");
        assert_eq!(
            report["tasks"]["items"][1]["nodeExecutionOrder"]["omitted"],
            6
        );
        assert_eq!(report["_summary"]["truncated"], true);
        assert!(
            report["tasks"]["items"][1]["nodeStates"]["items"][0]["message"]
                .as_str()
                .unwrap()
                .contains("Node failed")
        );
        assert!(!report.to_string().contains("private content"));
    }

    #[test]
    fn model_preparation_tasks_remain_visible_when_history_is_full() {
        for status in ["downloading_model", "ensuring_model", "running"] {
            let mut tasks: Vec<_> = (0..MAX_TASKS)
                .map(|n| json!({"id": format!("failed{n}"), "status": "failed", "updatedAt": n}))
                .collect();
            tasks.push(json!({"id": "current", "status": status, "updatedAt": -1}));
            let report = summarize_task_history(&json!({"tasks": tasks}));
            assert_eq!(report["statusCounts"][status], 1);
            assert_eq!(report["statusCounts"]["failed"], MAX_TASKS);
            assert_eq!(report["tasks"]["items"][0]["id"], "current");
            assert_eq!(report["tasks"]["omitted"], 1);
        }
    }

    #[test]
    fn plugin_operations_exports_runtime_and_settings_use_explicit_fields() {
        for command in ["run_operation", "start_operation_task"] {
            let report = summarize_request(
                command,
                &json!({"requestId": "asr", "operation": "pymss.asr.tool",
                    "inputs": {"request": {"inputPath": "/private/audio.wav", "hotword": "private words", "text": "private text"}},
                    "params": {"language": "zh", "token": "private token"}, "outputDir": "/private/Client-Acme"}),
            );
            assert_eq!(report["feature"], "plugin-operations");
            assert_eq!(report["operation"], "pymss.asr.tool");
            assert_eq!(report["inputCount"], 1);
            assert_eq!(report["parameterCount"], 2);
            assert!(report.get("outputDir").is_none());
            assert!(!report.to_string().contains("private"));
            assert!(!report.to_string().contains("Client-Acme"));
        }
        let export = summarize_request(
            "export_editor_mix",
            &json!({"taskId": "mix", "format": "flac", "audioParams": {"sampleRate": 48000}, "project": {"id": "p", "sources": [{"path": "/private/file"}], "tracks": [{"id": "1", "effects": {"reverb": 0.3}, "clips": [{"duration": 3}]}]}}),
        );
        assert_eq!(export["project"]["clipCount"], 1);
        assert!(export["project"]["tracks"]["items"][0]
            .get("effects")
            .is_none());
        let runtime = summarize_request(
            "update_runtime_core",
            &json!({"backend": "rocm", "mode": "sync", "prepareOnly": true, "planId": "plan", "proxyUrl": "private"}),
        );
        assert_eq!(runtime["parameters"]["mode"], "sync");
        assert!(!runtime.to_string().contains("private"));
        let settings = summarize_settings(
            &json!({"defaultDevice": "cuda:1", "maxConcurrentSeparations": 2, "proxyMode": "custom", "proxyUrl": "private", "proxyBypass": "private", "modelDir": "/private/models", "updateEndpointOverride": "https://private"}),
        );
        assert_eq!(settings["proxyMode"], "custom");
        assert_eq!(settings["modelDirConfigured"], true);
        assert!(!settings.to_string().contains("private"));
        let model = summarize_request(
            "import_custom_model",
            &json!({"name":"custom-model","modelType":"mel_band_roformer", "weightsPath":"C:/private/model.ckpt"}),
        );
        assert_eq!(model["feature"], "models");
        assert_eq!(model["parameters"]["modelType"], "mel_band_roformer");
        assert_eq!(model["weightsPath"], "model.ckpt");
        assert!(!model.to_string().contains("C:/private"));
    }

    #[test]
    fn text_sanitization_removes_url_auth_queries_and_assignments() {
        let output = sanitize_text("错误 https://user:pass@example.com/file?token=private#secret password='private value' token=private Authorization: Bearer private");
        assert!(output.contains("https://example.com/file"));
        assert!(!output.contains("private"));
        assert!(!output.contains("user:pass"));
        let report = summarize_request(
            "download_model",
            &json!({"model": "x".repeat(500), "source": "https://user:pass@example.com/download?secret=private", "secret": "private"}),
        );
        assert_eq!(report["_summary"]["truncatedStrings"], 1);
        assert!(!report.to_string().contains("private"));
        let markdown = sanitize_text("# Report\n\n[source](https://user:pass@example.com/a?token=private)\n```text\nhttps://user:pass@example.com:invalid/a?secret=private\n```\n");
        assert!(markdown.starts_with("# Report\n\n"));
        assert!(markdown.contains("[source](https://example.com/a)"));
        assert!(markdown.contains("https://example.com:invalid/a"));
        assert!(!markdown.contains("private"));
        assert!(!markdown.contains("user:pass"));
    }

    #[test]
    fn text_sanitization_visits_every_url_in_json_strings() {
        let input = r#"{"urls":["https://alice:first@example.com/a?key=one#first","https://bob:second@example.net/b?X-Amz-Signature=second-signature#second"],"status":"failed"}"#;
        assert_eq!(
            sanitize_text(input),
            r#"{"urls":["https://example.com/a","https://example.net/b"],"status":"failed"}"#,
        );
    }

    #[test]
    fn text_sanitization_handles_apostrophes_inside_urls_and_wrappers() {
        for (input, expected) in [
            ("download failed https://alice:p'ass@example.com/file?signature=private", "download failed https://example.com/file"),
            ("https://example.com/file?signature='private-value'", "https://example.com/file"),
            ("'https://alice:p'ass@example.com/file?signature='private-value''", "'https://example.com/file'"),
            ("\"https://alice:p'ass@example.com/file?signature='private-value'\"", "\"https://example.com/file\""),
            ("[source](https://alice:p'ass@example.com:invalid/o'brien?signature='private-value')", "[source](https://example.com:invalid/o'brien)"),
            ("['https://alice:p'ass@example.com/a?signature='private-value'', 'https://bob:p'ass@example.net/b?signature=private']", "['https://example.com/a', 'https://example.net/b']"),
            ("https://alice:p'ass@example.com/o'brien", "https://example.com/o'brien"),
        ] {
            assert_eq!(sanitize_text(input), expected, "input: {input}");
        }
    }

    #[test]
    fn text_sanitization_consumes_escaped_quotes_inside_credentials() {
        for (input, expected) in [
            (
                r#"Request rejected: {"password":"prefix\"private-suffix","status":"failed"}"#,
                r#"Request rejected: {"password":"[redacted]","status":"failed"}"#,
            ),
            (
                r#"token='prefix\'private-suffix' status=failed"#,
                "token='[redacted]' status=failed",
            ),
            (
                r#"password="private\\" status=failed"#,
                "password=\"[redacted]\" status=failed",
            ),
            (
                r#"password="prefix\\\"private-suffix" status=failed"#,
                "password=\"[redacted]\" status=failed",
            ),
        ] {
            assert_eq!(sanitize_text(input), expected, "input: {input}");
        }
    }

    #[test]
    fn text_sanitization_treats_brackets_as_boundaries_only_for_wrapped_urls() {
        for (input, expected) in [
            (
                "https://alice:p)ass@example.com/file?signature=private",
                "https://example.com/file",
            ),
            (
                "https://alice:p]ass@example.com/file?signature=private",
                "https://example.com/file",
            ),
            (
                "https://example.com/file?signature=private)value",
                "https://example.com/file",
            ),
            (
                "https://example.com/file?signature=private]value",
                "https://example.com/file",
            ),
            (
                "[source](https://alice:p)ass@example.com/file?signature=private)",
                "[source](https://example.com/file)",
            ),
            (
                "[https://alice:p]ass@example.com/file?signature=private]",
                "[https://example.com/file]",
            ),
            (
                "['https://example.com/file?signature=private]value']",
                "['https://example.com/file']",
            ),
        ] {
            if input.starts_with("https://") {
                assert!(url::Url::parse(input).is_ok());
            }
            assert_eq!(sanitize_text(input), expected, "input: {input}");
        }
    }

    #[test]
    fn text_sanitization_preserves_unicode_and_newlines_around_multiple_urls() {
        let input = "下载失败：[甲](https://alice:first@example.com/a?signature=one)、[乙](https://bob:second@example.net/b?signature=two)、[丙](https://carol:third@example.org/c#private)；请重试。\n下一行保持完整。\n";
        assert_eq!(
            sanitize_text(input),
            "下载失败：[甲](https://example.com/a)、[乙](https://example.net/b)、[丙](https://example.org/c)；请重试。\n下一行保持完整。\n",
        );
        assert_eq!(
            sanitize_text("中文https://alice:first@example.com/a?signature=one\n"),
            "中文https://example.com/a\n",
        );
    }

    #[test]
    fn text_sanitization_preserves_link_suffixes_with_malformed_ports() {
        let input = "[first](https://alice:first@example.com:invalid/a?signature=one#private \"标题\")/[second](https://bob:second@example.net:99999/b?signature=two#private) 后缀\n";
        assert_eq!(
            sanitize_text(input),
            "[first](https://example.com:invalid/a \"标题\")/[second](https://example.net:99999/b) 后缀\n",
        );
    }

    #[test]
    fn text_sanitization_handles_adjacent_urls_and_ipv6_authorities() {
        assert_eq!(
            sanitize_text("https://alice:first@example.com/a?signature=one,https://bob:second@[::1]:invalid/b?signature=two;https://carol:third@[::1]/c#private"),
            "https://example.com/a,https://[::1]:invalid/b;https://[::1]/c",
        );
    }

    #[test]
    fn malformed_and_missing_stores_are_safe_and_batch_lists_are_bounded() {
        for store in [
            Value::Null,
            json!(42),
            json!({"tasks": "invalid", "workflows": false}),
        ] {
            assert_eq!(summarize_task_history(&store)["tasks"]["total"], 0);
            assert_eq!(summarize_workflows(&store)["workflows"]["total"], 0);
            assert!(summarize_settings(&store).is_object());
        }
        let tasks: Vec<_> = (0..30)
            .map(|i| json!({"taskId": format!("t{i}")}))
            .collect();
        let report = summarize_request("infer_workflow_batch", &json!({"tasks": tasks}));
        assert_eq!(report["taskIds"].as_array().unwrap().len(), MAX_ITEMS);
        assert_eq!(report["tasks"]["omitted"], 6);
        assert_eq!(report["_summary"]["truncated"], true);
    }

    #[test]
    fn probes_keep_capabilities_and_bound_environments_without_raw_paths_or_payloads() {
        let environments: Vec<_> = (0..10).map(|_| json!({"backend": "cuda", "source": "managed", "pythonPath": "C:/private/python.exe", "overlayPath": "C:/private/overlay", "packageVersions": {"pymss": "2.1.8", "secret-package": "private"}})).collect();
        let probe = summarize_probe(
            "runtime_info",
            &json!({"ready": false, "pymssGraphAvailable": true,
            "installedEnvironments": environments, "liveProbeError": {"code": "IMPORT_ERROR", "message": "password=private"},
            "environment": {"SECRET": "private"}, "activeRuntimeFile": "C:/private/active.json",
            "installState": {"backend": "cuda", "overlayPath": "C:/private/overlay", "pymssVersion": "2.1.8"}}),
        );
        assert_eq!(probe["installedEnvironments"]["included"], 8);
        assert_eq!(probe["installedEnvironments"]["omitted"], 2);
        assert_eq!(probe["installState"]["overlayConfigured"], true);
        assert_eq!(probe["pymssGraphAvailable"], true);
        assert!(!probe.to_string().contains("private"));
        assert!(!probe.to_string().contains("SECRET"));
    }
}
