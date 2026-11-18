use super::{blacklist, PluginHostState};
use serde_json::{json, Value};
use std::io::Read;
use std::path::Path;
use std::sync::atomic::Ordering;

pub(super) fn lock_failure<T>(error: std::sync::TryLockError<T>) -> Value {
    match error {
        std::sync::TryLockError::WouldBlock => json!({"status": "busy"}),
        std::sync::TryLockError::Poisoned(_) => json!({"status": "error", "error": "Host state lock is poisoned"}),
    }
}

fn cache_summary(root: &Path) -> Result<Value, String> {
    let file = match std::fs::File::open(root.join("plugin-cache.json")) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(json!({"status": "not_observed"})),
        Err(error) => return Err(error.to_string()),
    };
    let mut bytes = Vec::new();
    file.take(super::MAX_CACHE_BYTES + 1).read_to_end(&mut bytes).map_err(|error| error.to_string())?;
    if bytes.len() as u64 > super::MAX_CACHE_BYTES { return Err("Plugin scan cache exceeds its read limit".into()); }
    let data: Value = serde_json::from_slice(&bytes).map_err(|error| error.to_string())?;
    if data["version"] != super::SCAN_CACHE_VERSION { return Err("Unsupported plugin scan cache version".into()); }
    let entries = data["entries"].as_array().ok_or("Invalid plugin scan cache entries")?;
    Ok(json!({"status": "observed", "version": data["version"], "entryCount": entries.len(),
        "descriptorCount": entries.iter().map(|entry| entry["plugins"].as_array().map_or(0, Vec::len)).sum::<usize>(),
        "failureCount": entries.iter().filter(|entry| entry["error"].as_str().is_some_and(|error| !error.is_empty())).count()}))
}

/// Never start a host, ask it to respond, or wait for an operation's mutex.
pub(crate) fn snapshot(root: &Path, state: &PluginHostState) -> Value {
    let mut capabilities = super::plugin_host_capabilities();
    if let Some(object) = capabilities.as_object_mut() { object.remove("defaultDirectories"); }
    let session = match state.sessions.try_lock() {
        Ok(sessions) => {
            let mut active = 0;
            let mut busy = 0;
            let mut failed = false;
            for session in sessions.windows.values().chain(&sessions.closing) {
                match session.session.try_lock() {
                    Ok(session) => active += usize::from(session.process.is_some()),
                    Err(std::sync::TryLockError::WouldBlock) => busy += 1,
                    Err(std::sync::TryLockError::Poisoned(_)) => failed = true,
                }
            }
            if failed {
                json!({"status": "error", "error": "Host state lock is poisoned"})
            } else {
                let status = if busy != 0 { "busy" } else if active != 0 { "active" } else { "idle" };
                let mut snapshot = json!({"status": status, "activeCount": active, "busyCount": busy});
                if active != 0 || busy == 0 {
                    snapshot["hasProcess"] = json!(active != 0);
                }
                snapshot
            }
        }
        Err(error) => lock_failure(error),
    };
    let renders = match state.renders.try_lock() {
        Ok(renders) => json!({"status": if renders.is_empty() { "idle" } else { "active" },
            "activeCount": renders.len()}),
        Err(error) => lock_failure(error),
    };
    let storage = match state.scan_storage.try_lock() {
        Ok(_guard) => {
            let cache = cache_summary(root).unwrap_or_else(|error| json!({"status": "error", "error": error}));
            let blacklist = match blacklist::load(root) {
                Ok(entries) => json!({"status": "observed", "count": entries.len(),
                    "manualCount": entries.iter().filter(|entry| entry.reason == blacklist::BlacklistReason::Manual).count(),
                    "timeoutCount": entries.iter().filter(|entry| entry.reason == blacklist::BlacklistReason::Timeout).count()}),
                Err(error) => json!({"status": "error", "error": error}),
            };
            json!({"status": if cache["status"] == "error" || blacklist["status"] == "error" { "error" } else { "observed" }, "cache": cache, "blacklist": blacklist})
        }
        Err(error) => lock_failure(error),
    };
    json!({"capabilities": capabilities, "stopping": state.stopping.load(Ordering::Acquire),
        "session": session, "renders": renders, "scan": state.scan_control.diagnostic_snapshot(), "storage": storage})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn locked_host_sections_are_reported_busy_without_waiting() {
        let root = tempfile::tempdir().unwrap();
        let state = PluginHostState::default();
        let session = state.live_session("editor-one", || true).unwrap();
        let _session = session.session.lock().unwrap();
        let _renders = state.renders.lock().unwrap();
        let _storage = state.scan_storage.lock().unwrap();
        let snapshot = snapshot(root.path(), &state);
        for section in ["session", "renders", "storage"] { assert_eq!(snapshot[section]["status"], "busy"); }
        assert_eq!(snapshot["session"]["busyCount"], 1);
        assert!(snapshot["session"].get("hasProcess").is_none());
    }

    #[test]
    fn native_diagnostics_do_not_create_storage_or_start_a_host() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("absent");
        let state = PluginHostState::default();
        let snapshot = snapshot(&path, &state);
        assert!(!path.exists());
        assert_eq!(snapshot["session"]["hasProcess"], false);
        assert_eq!(snapshot["storage"]["cache"]["status"], "not_observed");
        assert_eq!(snapshot["renders"]["activeCount"], 0);
        assert!(snapshot["capabilities"].get("defaultDirectories").is_none());
    }

    #[test]
    fn cache_statistics_and_parse_errors_do_not_modify_saved_data() {
        let root = tempfile::tempdir().unwrap();
        let cache = root.path().join("plugin-cache.json");
        let data = json!({"version": super::super::SCAN_CACHE_VERSION, "entries": [
            {"plugins": [{}, {}], "error": null}, {"plugins": [], "error": "Previous scan failed"}]});
        std::fs::write(&cache, data.to_string()).unwrap();
        let before = std::fs::read(&cache).unwrap();
        let state = PluginHostState::default();
        let observed = snapshot(root.path(), &state);
        assert_eq!(observed["storage"]["cache"]["descriptorCount"], 2);
        assert_eq!(observed["storage"]["cache"]["failureCount"], 1);
        assert_eq!(std::fs::read(&cache).unwrap(), before);
        std::fs::write(&cache, "broken JSON").unwrap();
        let observed = snapshot(root.path(), &state);
        assert_eq!(observed["storage"]["status"], "error");
        assert_eq!(std::fs::read_to_string(cache).unwrap(), "broken JSON");
    }
}
