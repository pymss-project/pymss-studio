use crate::error::{AppError, AppResult};
use serde::Serialize;
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Mutex, MutexGuard};
use tauri::{AppHandle, Emitter, Manager};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum WorkflowEditorKind {
    Simple,
    Advanced,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkflowEditor {
    pub label: String,
    pub workflow_id: String,
    pub kind: WorkflowEditorKind,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct RegistrySnapshot {
    pub revision: u64,
    pub editors: Vec<WorkflowEditor>,
}

#[derive(Default)]
pub struct EditorRegistry {
    revision: u64,
    editors: BTreeMap<String, WorkflowEditor>,
    used_labels: BTreeSet<String>,
}

impl EditorRegistry {
    pub fn snapshot(&self) -> RegistrySnapshot {
        RegistrySnapshot {
            revision: self.revision,
            editors: self.editors.values().cloned().collect(),
        }
    }

    pub fn editor_for_request(
        &self,
        workflow_id: &str,
        kind: WorkflowEditorKind,
    ) -> Option<WorkflowEditor> {
        self.editors
            .values()
            .find(|editor| {
                if workflow_id.is_empty() {
                    editor.workflow_id.is_empty() && editor.kind == kind
                } else {
                    editor.workflow_id == workflow_id
                }
            })
            .cloned()
    }

    pub fn next_available_label(&self, base: &str) -> String {
        if !self.used_labels.contains(base) {
            return base.to_string();
        }
        let mut suffix = 1u64;
        loop {
            let candidate = format!("{base}-{}-{suffix}", self.revision.saturating_add(1));
            if !self.used_labels.contains(&candidate) {
                return candidate;
            }
            suffix += 1;
        }
    }

    pub fn reserve(
        &mut self,
        label: &str,
        workflow_id: &str,
        kind: WorkflowEditorKind,
    ) -> AppResult<RegistrySnapshot> {
        self.check_write_owner(label, workflow_id)?;
        // A new editor can auto-save before WebviewWindowBuilder returns.
        // Never replace the identity it acquired while loading its page.
        if !self.editors.contains_key(label) {
            if self.used_labels.contains(label) {
                return Err(AppError::Worker(
                    "workflow editor label was already used".into(),
                ));
            }
            // Keep labels spent after release. Tauri removes a destroyed
            // window from its manager before dispatching our close listener;
            // that late event must never match a replacement window.
            self.used_labels.insert(label.to_string());
            self.editors.insert(
                label.to_string(),
                WorkflowEditor {
                    label: label.to_string(),
                    workflow_id: workflow_id.to_string(),
                    kind,
                },
            );
            self.revision += 1;
        }
        Ok(self.snapshot())
    }

    pub fn release(&mut self, label: &str) -> Option<RegistrySnapshot> {
        self.editors.remove(label).map(|_| {
            self.revision += 1;
            self.snapshot()
        })
    }

    fn check_write_owner(&self, caller_label: &str, workflow_id: &str) -> AppResult<()> {
        if !workflow_id.is_empty()
            && self
                .editors
                .values()
                .any(|editor| editor.workflow_id == workflow_id && editor.label != caller_label)
        {
            return Err(AppError::Worker("WORKFLOW_EDITOR_IN_USE".into()));
        }
        Ok(())
    }

    pub fn mutate(
        &mut self,
        caller_label: &str,
        payload: &Value,
        operation: impl FnOnce() -> AppResult<Value>,
    ) -> AppResult<(Value, Option<RegistrySnapshot>)> {
        let action = payload.get("action").and_then(Value::as_str).unwrap_or("");
        let workflow_id = match action {
            "upsert" => payload
                .get("entry")
                .and_then(|entry| entry.get("id"))
                .and_then(Value::as_str)
                .unwrap_or(""),
            "delete" => payload
                .get("workflowId")
                .and_then(Value::as_str)
                .unwrap_or(""),
            // Publication metadata never changes the graph or its revision.
            _ => "",
        };
        self.check_write_owner(caller_label, workflow_id)?;
        let response = operation()?;
        let mut changed = false;
        if response.get("conflict").is_none() {
            if let Some(editor) = self.editors.get_mut(caller_label) {
                if action == "upsert"
                    && !workflow_id.is_empty()
                    && editor.workflow_id != workflow_id
                {
                    editor.workflow_id = workflow_id.to_string();
                    changed = true;
                } else if action == "delete"
                    && editor.workflow_id == workflow_id
                    && !workflow_id.is_empty()
                {
                    editor.workflow_id.clear();
                    changed = true;
                }
            }
        }
        let snapshot = changed.then(|| {
            self.revision += 1;
            self.snapshot()
        });
        Ok((response, snapshot))
    }
}

#[derive(Default)]
pub struct WorkflowEditors {
    // Window construction dispatches to the GUI thread. Only the async gate
    // spans build(); registry locks cover identity checks and storage writes.
    pub creation_gate: tauri::async_runtime::Mutex<()>,
    registry: Mutex<EditorRegistry>,
}

impl WorkflowEditors {
    pub fn registry(&self) -> MutexGuard<'_, EditorRegistry> {
        self.registry
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

pub fn emit_snapshot(app: &AppHandle, snapshot: &RegistrySnapshot) {
    let _ = app.emit_to("main", "pymss://workflow-editors-changed", snapshot);
}

pub fn release_window(app: &AppHandle, label: &str) {
    let snapshot = app.state::<WorkflowEditors>().registry().release(label);
    if let Some(snapshot) = snapshot {
        emit_snapshot(app, &snapshot);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn save(registry: &mut EditorRegistry, label: &str, id: &str) {
        registry
            .mutate(
                label,
                &json!({ "action": "upsert", "entry": { "id": id } }),
                || Ok(json!({ "state": {} })),
            )
            .unwrap();
    }

    #[test]
    fn first_save_binds_formal_id_without_renaming_the_native_window() {
        let mut registry = EditorRegistry::default();
        registry
            .reserve("simple-new", "", WorkflowEditorKind::Simple)
            .unwrap();
        save(&mut registry, "simple-new", "workflow-a");
        let owner = registry
            .editor_for_request("workflow-a", WorkflowEditorKind::Advanced)
            .unwrap();
        assert_eq!(owner.label, "simple-new");
        assert_eq!(owner.kind, WorkflowEditorKind::Simple);
        assert_eq!(registry.snapshot().revision, 2);
        registry
            .reserve("simple-new", "", WorkflowEditorKind::Simple)
            .unwrap();
        assert_eq!(registry.snapshot().editors[0].workflow_id, "workflow-a");
    }

    #[test]
    fn publication_metadata_can_change_while_another_window_owns_the_graph() {
        let mut registry = EditorRegistry::default();
        registry.reserve("simple-a", "a", WorkflowEditorKind::Simple).unwrap();
        let before = registry.snapshot();
        for action in ["metadata", "notes"] {
            let (_, changed) = registry.mutate("main", &json!({ "action": action, "workflowId": "a" }),
                || Ok(json!({ "state": {} }))).unwrap();
            assert!(changed.is_none());
        }
        assert_eq!(registry.snapshot().revision, before.revision);
        assert_eq!(registry.snapshot().editors[0].label, "simple-a");
        assert!(registry.mutate("main", &json!({ "action": "upsert", "entry": { "id": "a" } }),
            || panic!("graph ownership must be enforced")).is_err());
    }

    #[test]
    fn formal_id_has_one_owner_across_editor_kinds() {
        let mut registry = EditorRegistry::default();
        registry
            .reserve("simple-a", "a", WorkflowEditorKind::Simple)
            .unwrap();
        assert!(registry
            .reserve("advanced-a", "a", WorkflowEditorKind::Advanced)
            .is_err());
        assert_eq!(
            registry
                .editor_for_request("a", WorkflowEditorKind::Advanced)
                .unwrap()
                .label,
            "simple-a"
        );
        assert_eq!(registry.snapshot().editors.len(), 1);
    }

    #[test]
    fn saving_a_copy_releases_old_id_but_does_not_reuse_its_physical_label() {
        let mut registry = EditorRegistry::default();
        registry
            .reserve("simple-a", "a", WorkflowEditorKind::Simple)
            .unwrap();
        save(&mut registry, "simple-a", "b");
        assert!(registry
            .editor_for_request("a", WorkflowEditorKind::Simple)
            .is_none());
        let new_label = registry.next_available_label("simple-a");
        assert_ne!(new_label, "simple-a");
        registry
            .reserve(&new_label, "a", WorkflowEditorKind::Simple)
            .unwrap();
        assert_eq!(
            registry
                .editor_for_request("b", WorkflowEditorKind::Advanced)
                .unwrap()
                .label,
            "simple-a"
        );
        assert_eq!(
            registry
                .editor_for_request("a", WorkflowEditorKind::Advanced)
                .unwrap()
                .label,
            new_label
        );
    }

    #[test]
    fn saved_new_window_does_not_capture_the_next_new_workflow_request() {
        let mut registry = EditorRegistry::default();
        registry
            .reserve("simple-new", "", WorkflowEditorKind::Simple)
            .unwrap();
        save(&mut registry, "simple-new", "a");
        assert!(registry
            .editor_for_request("", WorkflowEditorKind::Simple)
            .is_none());
        let second_label = registry.next_available_label("simple-new");
        registry
            .reserve(&second_label, "", WorkflowEditorKind::Simple)
            .unwrap();
        assert_eq!(
            registry
                .editor_for_request("", WorkflowEditorKind::Simple)
                .unwrap()
                .label,
            second_label
        );
    }

    #[test]
    fn closing_one_editor_keeps_other_owners_and_duplicate_close_is_a_noop() {
        let mut registry = EditorRegistry::default();
        registry
            .reserve("simple-a", "a", WorkflowEditorKind::Simple)
            .unwrap();
        registry
            .reserve("advanced-b", "b", WorkflowEditorKind::Advanced)
            .unwrap();
        let closed = registry.release("simple-a").unwrap();
        assert_eq!(closed.editors.len(), 1);
        assert_eq!(closed.editors[0].workflow_id, "b");
        assert!(registry.release("simple-a").is_none());
        assert_eq!(registry.snapshot().revision, closed.revision);
    }

    #[test]
    fn delayed_destroyed_event_cannot_release_a_reopened_workflow() {
        let mut registry = EditorRegistry::default();
        registry
            .reserve("simple-a", "a", WorkflowEditorKind::Simple)
            .unwrap();
        // The open helper discovers the manager has removed the old window
        // before its Destroyed listener runs, so it releases the stale owner.
        registry.release("simple-a").unwrap();
        let replacement_label = registry.next_available_label("simple-a");
        assert_ne!(replacement_label, "simple-a");
        registry
            .reserve(&replacement_label, "a", WorkflowEditorKind::Simple)
            .unwrap();
        let revision = registry.snapshot().revision;
        assert!(registry.release("simple-a").is_none());
        assert_eq!(registry.snapshot().revision, revision);
        assert_eq!(
            registry
                .editor_for_request("a", WorkflowEditorKind::Advanced)
                .unwrap()
                .label,
            replacement_label
        );
    }

    #[test]
    fn released_labels_cannot_be_reserved_again_even_after_a_failed_open() {
        let mut registry = EditorRegistry::default();
        registry
            .reserve("simple-new", "", WorkflowEditorKind::Simple)
            .unwrap();
        registry.release("simple-new").unwrap();
        assert!(registry
            .reserve("simple-new", "", WorkflowEditorKind::Simple)
            .is_err());
        assert!(registry.snapshot().editors.is_empty());
        assert_ne!(registry.next_available_label("simple-new"), "simple-new");
    }

    #[test]
    fn force_cannot_override_another_editor_or_bypass_delete_protection() {
        let mut registry = EditorRegistry::default();
        registry
            .reserve("simple-a", "a", WorkflowEditorKind::Simple)
            .unwrap();
        for payload in [
            json!({ "action": "upsert", "force": true, "entry": { "id": "a" } }),
            json!({ "action": "delete", "workflowId": "a" }),
        ] {
            let denied = registry.mutate("main", &payload, || {
                panic!("unauthorized write reached storage")
            });
            assert!(denied
                .unwrap_err()
                .to_string()
                .contains("WORKFLOW_EDITOR_IN_USE"));
        }
        registry
            .mutate(
                "main",
                &json!({ "action": "select", "workflowId": "a" }),
                || Ok(json!({ "state": {} })),
            )
            .unwrap();
        save(&mut registry, "simple-a", "a");
        assert_eq!(registry.snapshot().revision, 1);
    }

    #[test]
    fn saving_a_copy_cannot_claim_another_open_workflow() {
        let mut registry = EditorRegistry::default();
        registry
            .reserve("simple-a", "a", WorkflowEditorKind::Simple)
            .unwrap();
        registry
            .reserve("advanced-b", "b", WorkflowEditorKind::Advanced)
            .unwrap();
        let payload = json!({ "action": "upsert", "force": true, "entry": { "id": "b" } });
        assert!(registry
            .mutate("simple-a", &payload, || {
                panic!("copy must not overwrite another editor's workflow")
            })
            .is_err());
        assert_eq!(
            registry
                .editor_for_request("a", WorkflowEditorKind::Advanced)
                .unwrap()
                .label,
            "simple-a"
        );
        assert_eq!(registry.snapshot().revision, 2);
    }

    #[test]
    fn failed_or_conflicting_saves_leave_the_previous_owner_unchanged() {
        let mut registry = EditorRegistry::default();
        registry
            .reserve("simple-a", "a", WorkflowEditorKind::Simple)
            .unwrap();
        let payload = json!({ "action": "upsert", "entry": { "id": "b" } });
        assert!(registry
            .mutate("simple-a", &payload, || Err(AppError::Worker(
                "disk write failed".into()
            )))
            .is_err());
        registry
            .mutate("simple-a", &payload, || {
                Ok(json!({ "state": {}, "conflict": {} }))
            })
            .unwrap();
        assert_eq!(registry.snapshot().editors[0].workflow_id, "a");
        assert_eq!(registry.snapshot().revision, 1);
    }
}
