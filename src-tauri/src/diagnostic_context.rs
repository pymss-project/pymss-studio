use crate::log_record::LogRecord;
use serde_json::{json, Value};
use std::collections::{BTreeMap, VecDeque};

const OPERATION_LIMIT: usize = 24;
const ISSUE_LIMIT: usize = 16;
const EVENT_LIMIT: usize = 40;
const NAVIGATION_LIMIT: usize = 24;

#[derive(Default)]
pub(crate) struct DiagnosticContext {
    operations: VecDeque<Value>,
    issues: VecDeque<Value>,
    events: VecDeque<Value>,
    navigation: VecDeque<Value>,
    windows: BTreeMap<String, Value>,
    probes: BTreeMap<String, Value>,
    editor: Option<Value>,
    diagnostic_request: Option<String>,
    window_states: BTreeMap<String, Value>,
    levels: BTreeMap<String, u64>,
    omitted_operations: usize,
    omitted_issues: usize,
    omitted_events: usize,
    omitted_navigation: usize,
}

pub(crate) fn operation_feature(command: &str) -> Option<&'static str> {
    match command {
        "infer" | "infer_batch" | "separate" | "separate_batch" => Some("separation"),
        "infer_workflow" | "infer_workflow_batch" => Some("workflow"),
        "run_operation" => Some("plugin-operations"),
        "export_editor_mix" => Some("editor-export"),
        "install_runtime"
        | "activate_runtime"
        | "delete_runtime"
        | "update_runtime_core"
        | "manage_optional_package" => Some("runtime"),
        "download_model"
        | "delete_model"
        | "delete_tool_model"
        | "import_custom_model"
        | "inspect_custom_model"
        | "unregister_custom_model"
        | "relink_custom_model"
        | "remap_custom_model_paths"
        | "cleanup_model_residual_files" => Some("models"),
        _ => None,
    }
}

fn push_bounded(queue: &mut VecDeque<Value>, value: Value, limit: usize, omitted: &mut usize) {
    if queue.len() == limit {
        queue.pop_front();
        *omitted += 1;
    }
    queue.push_back(value);
}

fn record_summary(record: &LogRecord) -> Value {
    let mut context = serde_json::Map::new();
    for key in [
        "command",
        "operation",
        "action",
        "event",
        "code",
        "stage",
        "phase",
        "nodeType",
        "nodeIndex",
        "nodeCount",
        "nodeProgress",
        "overallFraction",
        "errorType",
        "done",
        "unit",
        "audioIndex",
        "audioCount",
        "progress",
        "completed",
        "total",
        "succeeded",
        "failedCount",
        "skippedCount",
        "warningCount",
        "outputCount",
        "model",
        "backend",
        "version",
        "status",
        "cancelled",
        "durationMs",
        "operationId",
        "source",
        "targetVersion",
        "currentVersion",
        "distribution",
        "selectedSource",
        "python",
        "worker",
    ] {
        if let Some(value) = record.context.get(key).filter(|value| !value.is_null()) {
            if value.is_string() || value.is_boolean() || value.is_number() {
                context.insert(key.to_string(), value.clone());
            }
        }
    }
    json!({
        "id": record.id, "timestamp": record.timestamp, "level": record.level,
        "source": record.source, "logger": record.logger, "event": record.event,
        "message": record.message.chars().take(1200).collect::<String>(),
        "messageTruncated": record.message.chars().count() > 1200,
        "taskId": record.task_id, "nodeId": record.node_id, "jobId": record.job_id,
        "requestId": record.request_id,
        "detail": record.detail.as_deref().map(|value| value.chars().take(4096).collect::<String>()),
        "detailTruncated": record.detail.as_ref().is_some_and(|value| value.chars().count() > 4096),
        "location": record.location, "context": context,
    })
}

fn matches_operation(operation: &Value, record: &LogRecord) -> bool {
    if let Some(id) = record.context.get("operationId").and_then(Value::as_str) {
        return operation["id"].as_str() == Some(id);
    }
    let command = record.context.get("command").and_then(Value::as_str);
    if command.is_some() && command != operation["command"].as_str() {
        return false;
    }
    if let Some(task_id) = record.task_id.as_deref() {
        return operation["taskId"].as_str() == Some(task_id)
            || operation["requestId"].as_str() == Some(task_id)
            || operation["request"]["taskIds"]
                .as_array()
                .is_some_and(|ids| ids.iter().any(|id| id.as_str() == Some(task_id)));
    }
    if let Some(request_id) = record.request_id.as_deref() {
        return operation["requestId"].as_str() == Some(request_id)
            || operation["taskId"].as_str() == Some(request_id);
    }
    if let Some(ids) = record.context.get("taskIds").and_then(Value::as_array) {
        return ids.iter().filter_map(Value::as_str).any(|id| {
            operation["taskId"].as_str() == Some(id)
                || operation["request"]["taskIds"]
                    .as_array()
                    .is_some_and(|tasks| tasks.iter().any(|task| task.as_str() == Some(id)))
        });
    }
    // An uncorrelated probe or SDK message must not update the most recent user operation.
    command.is_some() && operation["taskId"].is_null() && operation["requestId"].is_null()
}

impl DiagnosticContext {
    pub(crate) fn observe(&mut self, record: &LogRecord) {
        *self
            .levels
            .entry(record.level.as_deref().unwrap_or("unclassified").into())
            .or_default() += 1;
        if record.event == "worker.command" {
            let command = record
                .context
                .get("command")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let key = format!("workerInterpreter:{command}");
            if self.probes.len() < 32 || self.probes.contains_key(&key) {
                self.probes.insert(
                    key,
                    json!({"observedAt": record.timestamp,
                    "command": command, "python": record.context.get("python"),
                    "worker": record.context.get("worker")}),
                );
            }
        }
        if record.event == "ui.navigation" {
            let window = record
                .context
                .get("windowLabel")
                .and_then(Value::as_str)
                .unwrap_or("main");
            let visit = json!({"timestamp": record.timestamp, "window": window,
                "page": record.context.get("page"), "tool": record.context.get("tool")});
            if self.windows.len() < 8 || self.windows.contains_key(window) {
                self.windows.insert(window.to_string(), visit.clone());
            }
            push_bounded(
                &mut self.navigation,
                visit,
                NAVIGATION_LIMIT,
                &mut self.omitted_navigation,
            );
            return;
        }
        if record.event == "operation.request" {
            let command = record
                .context
                .get("command")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let operation = json!({
                "id": record.id, "feature": operation_feature(command), "command": command,
                "taskId": record.task_id, "requestId": record.request_id, "jobId": record.job_id,
                "startedAt": record.timestamp, "updatedAt": record.timestamp,
                "status": "requested", "request": record.context,
            });
            push_bounded(
                &mut self.operations,
                operation,
                OPERATION_LIMIT,
                &mut self.omitted_operations,
            );
            return;
        }
        if matches!(record.level.as_deref(), Some("warn" | "error" | "fatal")) {
            push_bounded(
                &mut self.issues,
                record_summary(record),
                ISSUE_LIMIT,
                &mut self.omitted_issues,
            );
        }
        if !matches!(record.level.as_deref(), Some("debug" | "trace")) {
            push_bounded(
                &mut self.events,
                record_summary(record),
                EVENT_LIMIT,
                &mut self.omitted_events,
            );
        }
        let Some(operation) = self
            .operations
            .iter_mut()
            .rev()
            .find(|operation| matches_operation(operation, record))
        else {
            return;
        };
        operation["updatedAt"] = record.timestamp.clone().into();
        operation["lastEvent"] = record.event.clone().into();
        operation["lastContext"] = record_summary(record)["context"].clone();
        if record.event == "worker.command" {
            operation["interpreter"] = json!({"python": record.context.get("python"),
                "worker": record.context.get("worker"), "observedAt": record.timestamp});
        }
        if let Some(node_id) = &record.node_id {
            operation["currentNodeId"] = node_id.clone().into();
        }
        if let Some(task_id) = &record.task_id {
            operation["lastTaskId"] = task_id.clone().into();
        }
        let execution = record
            .context
            .get("event")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let cancelled = record.event.ends_with("cancelled")
            || execution.ends_with("cancelled")
            || record
                .context
                .get("cancelled")
                .is_some_and(|value| value == true || value == "true");
        let failed = matches!(record.level.as_deref(), Some("error" | "fatal"))
            || execution.ends_with("failed")
            || record.event == "operation.failed";
        if failed {
            operation["status"] = "failed".into();
            operation["error"] = record_summary(record);
        } else if operation["status"] != "failed" {
            if cancelled {
                operation["status"] = "cancelled".into();
            } else if record.event == "worker.exit"
                || matches!(
                    record.event.as_str(),
                    "operation_result"
                        | "editor_mix_exported"
                        | "task_done"
                        | "task_finished"
                        | "runtime_installed"
                        | "runtime_core_updated"
                        | "model_downloaded"
                )
            {
                let batch_child_finished =
                    matches!(record.event.as_str(), "task_done" | "task_finished")
                        && operation["request"]["tasks"]["total"].as_u64().unwrap_or(0) > 1;
                operation["status"] = if batch_child_finished {
                    "running"
                } else if operation["status"] == "completed_with_errors"
                    || record
                        .context
                        .get("failedCount")
                        .and_then(Value::as_u64)
                        .unwrap_or(0)
                        > 0
                {
                    "completed_with_errors"
                } else {
                    "completed"
                }
                .into();
            } else if operation["status"] != "cancelled" && operation["status"] != "completed" {
                operation["status"] = "running".into();
            }
        }
        if let Some(duration) = record.context.get("durationMs") {
            operation["durationMs"] = duration.clone();
        }
    }

    pub(crate) fn operation_failed(&self, id: &str) -> bool {
        self.operations
            .iter()
            .any(|operation| operation["id"] == id && operation["status"] == "failed")
    }

    pub(crate) fn probe(&mut self, command: &str, summary: Value) {
        // The caller supplies only a fixed set of environment probe commands.
        self.probes.insert(command.to_string(), summary);
    }

    pub(crate) fn editor(&mut self, summary: Value) {
        self.editor = Some(summary);
    }

    pub(crate) fn begin_window_request(&mut self, request_id: &str) {
        self.diagnostic_request = Some(request_id.into());
        self.window_states.clear();
    }

    pub(crate) fn observe_window(&mut self, request_id: &str, label: &str, mut summary: Value) -> bool {
        if self.diagnostic_request.as_deref() != Some(request_id)
            || (self.window_states.len() >= 8 && !self.window_states.contains_key(label)) { return false; }
        summary["windowLabel"] = json!(label);
        summary["requestId"] = json!(request_id);
        self.window_states.insert(label.into(), summary);
        true
    }

    pub(crate) fn forget_window(&mut self, label: &str) {
        self.window_states.remove(label);
        self.windows.remove(label);
    }

    pub(crate) fn current_window_states(&self, request_id: &str, expected: &[String]) -> Value {
        let observed: Vec<_> = self.window_states.iter().filter(|(label, state)| expected.contains(label)
            && state["requestId"] == request_id).map(|(_, state)| state.clone()).collect();
        let missing: Vec<_> = expected.iter().filter(|label| !observed.iter().any(|state| state["windowLabel"] == label.as_str())).collect();
        json!({"status": if self.diagnostic_request.as_deref() == Some(request_id) { "observed" } else { "superseded" },
            "expected": expected, "observed": observed, "missing": missing})
    }

    pub(crate) fn snapshot(&self) -> Value {
        json!({
            "scope": "Current process only; persisted task history is reported separately",
            "recordCounts": self.levels,
            "windows": self.windows.values().collect::<Vec<_>>(),
            "navigation": self.navigation.iter().rev().collect::<Vec<_>>(),
            "operations": self.operations.iter().rev().collect::<Vec<_>>(),
            "issues": self.issues.iter().rev().collect::<Vec<_>>(),
            "events": self.events.iter().rev().collect::<Vec<_>>(),
            "environment": self.probes,
            "editor": self.editor,
            "windowStates": self.window_states.values().collect::<Vec<_>>(),
            "omitted": {"operations": self.omitted_operations, "issues": self.omitted_issues,
                "events": self.omitted_events, "navigation": self.omitted_navigation},
            "limits": {"operations": OPERATION_LIMIT, "issues": ISSUE_LIMIT,
                "events": EVENT_LIMIT, "navigation": NAVIGATION_LIMIT, "windows": 8},
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn diagnostic_window_requests_reject_stale_replies_and_track_missing_windows() {
        let mut context = DiagnosticContext::default();
        context.begin_window_request("first");
        assert!(context.observe_window("first", "editor-one", json!({"kind": "editor"})));
        context.begin_window_request("second");
        assert!(!context.observe_window("first", "editor-one", json!({"error": "stale"})));
        let expected = vec!["editor-one".to_string(), "editor-two".to_string()];
        let snapshot = context.current_window_states("second", &expected);
        assert_eq!(snapshot["missing"].as_array().unwrap().len(), 2);
        assert!(context.observe_window("second", "editor-one", json!({"kind": "editor", "exporting": true})));
        let snapshot = context.current_window_states("second", &expected);
        assert_eq!(snapshot["observed"][0]["exporting"], true);
        assert_eq!(snapshot["missing"], json!(["editor-two"]));
        context.forget_window("editor-one");
        assert!(context.current_window_states("second", &expected)["observed"].as_array().unwrap().is_empty());
    }

    #[test]
    fn window_observations_are_bounded_and_refresh_reclaims_the_capacity() {
        let mut context = DiagnosticContext::default();
        context.begin_window_request("first");
        for index in 0..8 { assert!(context.observe_window("first", &format!("editor-{index}"), json!({"kind": "editor"}))); }
        assert!(!context.observe_window("first", "editor-extra", json!({"kind": "editor"})));
        context.begin_window_request("second");
        assert!(context.observe_window("second", "editor-extra", json!({"kind": "editor"})));
    }

    fn request(id: &str, task: &str) -> LogRecord {
        let mut record = LogRecord::new(
            "studio",
            Some("info"),
            "studio",
            "operation.request",
            "Operation requested",
        );
        record.id = id.into();
        record.task_id = Some(task.into());
        record
            .context
            .insert("command".into(), "infer_workflow".into());
        record
    }

    #[test]
    fn simultaneous_workflows_keep_their_own_failure_and_node_context() {
        let mut context = DiagnosticContext::default();
        context.observe(&request("op-a", "task-a"));
        context.observe(&request("op-b", "task-b"));
        let mut error = LogRecord::new(
            "worker",
            Some("error"),
            "worker",
            "error",
            "Model load failed",
        );
        error.task_id = Some("task-a".into());
        error.node_id = Some("node-7".into());
        error
            .context
            .insert("command".into(), "infer_workflow".into());
        context.observe(&error);
        let mut exit = error.clone();
        exit.event = "worker.exit".into();
        exit.level = Some("info".into());
        context.observe(&exit);
        let snapshot = context.snapshot();
        assert_eq!(snapshot["operations"][0]["status"], "requested");
        assert_eq!(snapshot["operations"][1]["status"], "failed");
        assert_eq!(snapshot["operations"][1]["currentNodeId"], "node-7");
        assert_eq!(snapshot["issues"][0]["taskId"], "task-a");
    }

    #[test]
    fn batch_child_events_and_early_start_failures_are_correlated() {
        let mut context = DiagnosticContext::default();
        let mut batch = request("batch-op", "batch-parent");
        batch
            .context
            .insert("taskIds".into(), json!(["child-a", "child-b"]));
        context.observe(&batch);
        let mut event = LogRecord::new(
            "worker",
            Some("info"),
            "worker",
            "workflow_execution",
            "Node started",
        );
        event.task_id = Some("child-b".into());
        event.node_id = Some("step-2".into());
        event
            .context
            .insert("command".into(), "infer_workflow".into());
        event.context.insert("event".into(), "node_started".into());
        context.observe(&event);
        assert_eq!(
            context.snapshot()["operations"][0]["currentNodeId"],
            "step-2"
        );
        let mut error = LogRecord::new(
            "studio",
            Some("error"),
            "studio",
            "operation.failed",
            "No active environment",
        );
        error
            .context
            .insert("operationId".into(), "batch-op".into());
        context.observe(&error);
        assert!(context.operation_failed("batch-op"));
    }

    #[test]
    fn cancellation_stays_informational_and_recent_context_is_bounded() {
        let mut context = DiagnosticContext::default();
        for index in 0..OPERATION_LIMIT + 5 {
            context.observe(&request(&format!("op-{index}"), &format!("task-{index}")));
        }
        let mut cancelled = LogRecord::new(
            "worker",
            Some("info"),
            "worker",
            "task_cancelled",
            "Cancelled",
        );
        cancelled.task_id = Some(format!("task-{}", OPERATION_LIMIT + 4));
        context.observe(&cancelled);
        let snapshot = context.snapshot();
        assert_eq!(
            snapshot["operations"].as_array().unwrap().len(),
            OPERATION_LIMIT
        );
        assert_eq!(snapshot["omitted"]["operations"], 5);
        assert_eq!(snapshot["operations"][0]["status"], "cancelled");
        assert_eq!(snapshot["issues"], json!([]));
    }

    #[test]
    fn interpreter_context_survives_progress_and_probes_do_not_overwrite_task_status() {
        let mut context = DiagnosticContext::default();
        context.observe(&request("op-a", "task-a"));
        let mut prepared = LogRecord::new(
            "studio",
            Some("debug"),
            "studio",
            "worker.command",
            "worker.command",
        );
        prepared.task_id = Some("task-a".into());
        prepared
            .context
            .insert("command".into(), "infer_workflow".into());
        prepared
            .context
            .insert("python".into(), "runtime/cuda/python.exe".into());
        context.observe(&prepared);
        let mut progress = prepared.clone();
        progress.event = "task_progress".into();
        context.observe(&progress);
        let mut unrelated =
            LogRecord::new("worker", Some("error"), "worker", "error", "Probe failed");
        unrelated
            .context
            .insert("command".into(), "env_info".into());
        context.observe(&unrelated);
        assert_eq!(
            context.snapshot()["operations"][0]["interpreter"]["python"],
            "runtime/cuda/python.exe"
        );
        assert_eq!(context.snapshot()["operations"][0]["status"], "running");
    }

    #[test]
    fn partially_failed_results_remain_visible_after_a_successful_process_exit() {
        let mut context = DiagnosticContext::default();
        let mut operation = request("op-tools", "tools-1");
        operation
            .context
            .insert("command".into(), "run_operation".into());
        context.observe(&operation);
        assert_eq!(context.snapshot()["operations"][0]["feature"], "plugin-operations");
        let mut result = LogRecord::new(
            "worker",
            Some("warn"),
            "worker",
            "operation_result",
            "Some inputs failed",
        );
        result.task_id = Some("tools-1".into());
        result
            .context
            .insert("command".into(), "run_operation".into());
        result.context.insert("failedCount".into(), 1.into());
        context.observe(&result);
        assert_eq!(context.snapshot()["operations"][0]["status"], "completed_with_errors");
        result.event = "worker.exit".into();
        result.level = Some("info".into());
        result.context.remove("failedCount");
        context.observe(&result);
        assert_eq!(
            context.snapshot()["operations"][0]["status"],
            "completed_with_errors"
        );
    }

    #[test]
    fn large_batch_children_use_operation_identity_and_finish_only_when_the_worker_exits() {
        let mut context = DiagnosticContext::default();
        let mut batch = request("large-batch", "batch-parent");
        batch.context.insert("tasks".into(), json!({"total": 80}));
        batch.context.insert("taskIds".into(), json!(["child-1"]));
        context.observe(&batch);
        let mut event = LogRecord::new(
            "worker",
            Some("info"),
            "worker",
            "task_done",
            "File completed",
        );
        event.task_id = Some("child-1".into());
        event
            .context
            .insert("operationId".into(), "large-batch".into());
        context.observe(&event);
        assert_eq!(context.snapshot()["operations"][0]["status"], "running");
        event.task_id = Some("child-79".into());
        event.node_id = Some("late-node".into());
        event.event = "error".into();
        event.level = Some("error".into());
        context.observe(&event);
        event.event = "worker.exit".into();
        event.level = Some("info".into());
        context.observe(&event);
        let snapshot = context.snapshot();
        assert_eq!(snapshot["operations"][0]["status"], "failed");
        assert_eq!(snapshot["operations"][0]["lastTaskId"], "child-79");
        assert_eq!(snapshot["operations"][0]["currentNodeId"], "late-node");
    }
}
