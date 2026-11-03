//! Per-scan control. User cancellation never resets or cancels the app-wide process lease.
use super::{blacklist, ProcessControl};
use serde::Serialize;
use std::collections::HashSet;
use std::sync::{Arc, Mutex};
use std::time::Instant;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Decision {
    Continue,
    Skip,
    Cancel,
}

#[derive(Clone, Copy)]
pub(super) struct Ticket {
    attempt: u64,
    pub started: Instant,
}

struct Current {
    ticket: Ticket,
    path: String,
    name: String,
    skipped: bool,
    process: Option<Arc<ProcessControl>>,
}

struct Active {
    id: String,
    running: bool,
    cancelled: bool,
    collecting: bool,
    attempt: u64,
    completed: usize,
    total: usize,
    current: Option<Current>,
    invalidations: HashSet<String>,
}

#[derive(Default)]
struct Inner {
    stopping: bool,
    active: Option<Active>,
}

#[derive(Default)]
pub(super) struct ScanControl(Mutex<Inner>);

impl ScanControl {
    pub fn diagnostic_snapshot(&self) -> serde_json::Value {
        let inner = match self.0.try_lock() {
            Ok(inner) => inner,
            Err(error) => return super::diagnostics::lock_failure(error),
        };
        let Some(active) = inner.active.as_ref() else { return serde_json::json!({"status": "idle"}); };
        serde_json::json!({"status": if !active.running { if active.cancelled { "cancelled" } else { "finished" } }
            else if active.collecting { "collecting" } else { "scanning" },
            "scanId": active.id, "completed": active.completed, "total": active.total,
            "currentName": active.current.as_ref().map(|item| item.name.as_str()),
            "elapsedMs": active.current.as_ref().map_or(0, |item| item.ticket.started.elapsed().as_millis()),
            "canSkip": active.running && !active.cancelled && active.current.as_ref().is_some_and(|item| !item.skipped)})
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(super) struct Progress {
    scan_id: String,
    phase: &'static str,
    current_path: Option<String>,
    current_name: Option<String>,
    elapsed_ms: u128,
    completed: usize,
    total: usize,
    can_skip: bool,
}

pub(super) struct ScanRun {
    control: Arc<ScanControl>,
    id: String,
}

fn decision(active: &Active) -> Decision {
    if active.cancelled {
        Decision::Cancel
    } else if active
        .current
        .as_ref()
        .is_some_and(|current| current.skipped)
    {
        Decision::Skip
    } else {
        Decision::Continue
    }
}

impl ScanControl {
    pub fn start(self: &Arc<Self>, id: String) -> Result<ScanRun, String> {
        if id.is_empty()
            || id.len() > 128
            || !id
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"-_.:".contains(&c))
        {
            return Err("The plugin scan identifier is invalid".into());
        }
        let mut inner = self.0.lock().map_err(|_| "Plugin scan state lock failed")?;
        if inner.stopping {
            return Err("Plugin scanning was cancelled".into());
        }
        if inner.active.as_ref().is_some_and(|run| run.running) {
            return Err("Another plugin scan is already running".into());
        }
        if inner.active.as_ref().is_some_and(|run| run.id == id) {
            return Err("The plugin scan identifier was already used".into());
        }
        inner.active = Some(Active {
            id: id.clone(),
            running: true,
            cancelled: false,
            collecting: true,
            attempt: 0,
            completed: 0,
            total: 0,
            current: None,
            invalidations: HashSet::new(),
        });
        Ok(ScanRun {
            control: self.clone(),
            id,
        })
    }

    pub fn status(&self, id: &str) -> Option<Progress> {
        let inner = self.0.lock().unwrap_or_else(|e| e.into_inner());
        let active = inner.active.as_ref().filter(|run| run.id == id)?;
        Some(Progress {
            scan_id: active.id.clone(),
            phase: if active.cancelled {
                "cancelled"
            } else if !active.running {
                "finished"
            } else if active.collecting {
                "collecting"
            } else {
                "scanning"
            },
            current_path: active.current.as_ref().map(|item| item.path.clone()),
            current_name: active.current.as_ref().map(|item| item.name.clone()),
            elapsed_ms: active
                .current
                .as_ref()
                .map_or(0, |item| item.ticket.started.elapsed().as_millis()),
            completed: active.completed,
            total: active.total,
            can_skip: active.running
                && !active.cancelled
                && active.current.as_ref().is_some_and(|item| !item.skipped),
        })
    }

    /// Claim the displayed item under the same lock as completion, then signal outside it.
    pub fn skip(&self, id: &str, path: &str) -> bool {
        let process = {
            let mut inner = self.0.lock().unwrap_or_else(|e| e.into_inner());
            let Some(active) = inner
                .active
                .as_mut()
                .filter(|run| run.id == id && run.running && !run.cancelled)
            else {
                return false;
            };
            let Some(current) = active
                .current
                .as_mut()
                .filter(|item| blacklist::path_matches(&item.path, path) && !item.skipped)
            else {
                return false;
            };
            current.skipped = true;
            current.process.clone()
        };
        if let Some(process) = process {
            process.stop();
        }
        true
    }

    pub fn cancel(&self, id: &str) -> bool {
        let process = {
            let mut inner = self.0.lock().unwrap_or_else(|e| e.into_inner());
            let Some(active) = inner
                .active
                .as_mut()
                .filter(|run| run.id == id && run.running)
            else {
                return false;
            };
            active.cancelled = true;
            active
                .current
                .as_ref()
                .and_then(|item| item.process.clone())
        };
        if let Some(process) = process {
            process.stop();
        }
        true
    }

    pub fn shutdown(&self) {
        let id = {
            let mut inner = self.0.lock().unwrap_or_else(|e| e.into_inner());
            inner.stopping = true;
            inner.active.as_ref().map(|run| run.id.clone())
        };
        if let Some(id) = id {
            self.cancel(&id);
        }
    }

    pub fn invalidate(&self, path: &str) {
        let mut inner = self.0.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(active) = inner.active.as_mut().filter(|run| run.running) {
            active.invalidations.insert(blacklist::path_key(path));
        }
    }
}

impl ScanRun {
    pub fn set_total(&self, total: usize) {
        let mut inner = self.control.0.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(active) = inner.active.as_mut().filter(|run| run.id == self.id) {
            active.collecting = false;
            active.total = total;
        }
    }

    pub fn begin_item(&self, path: &str) -> Ticket {
        let mut inner = self.control.0.lock().unwrap_or_else(|e| e.into_inner());
        let active = inner
            .active
            .as_mut()
            .expect("an owned scan remains registered");
        assert_eq!(active.id, self.id);
        active.attempt += 1;
        let ticket = Ticket {
            attempt: active.attempt,
            started: Instant::now(),
        };
        let name = if path == "au:system" {
            "Audio Units"
        } else {
            path.rsplit(['/', '\\']).next().unwrap_or(path)
        };
        active.current = Some(Current {
            ticket,
            path: path.into(),
            name: name.into(),
            skipped: false,
            process: None,
        });
        ticket
    }

    pub fn decision(&self, ticket: Option<Ticket>) -> Decision {
        let inner = self.control.0.lock().unwrap_or_else(|e| e.into_inner());
        let Some(active) = inner
            .active
            .as_ref()
            .filter(|run| run.id == self.id && run.running)
        else {
            return Decision::Cancel;
        };
        if ticket.is_some_and(|ticket| {
            active
                .current
                .as_ref()
                .is_none_or(|item| item.ticket.attempt != ticket.attempt)
        }) {
            return Decision::Cancel;
        }
        decision(active)
    }

    pub fn attach(&self, ticket: Ticket, process: Arc<ProcessControl>) -> Decision {
        let mut inner = self.control.0.lock().unwrap_or_else(|e| e.into_inner());
        let Some(active) = inner
            .active
            .as_mut()
            .filter(|run| run.id == self.id && run.running)
        else {
            return Decision::Cancel;
        };
        let Some(item) = active
            .current
            .as_mut()
            .filter(|item| item.ticket.attempt == ticket.attempt)
        else {
            return Decision::Cancel;
        };
        item.process = Some(process);
        decision(active)
    }

    /// A skip arriving before completion wins even if a successful reply was already queued.
    pub fn complete_item(&self, ticket: Ticket) -> Decision {
        let mut inner = self.control.0.lock().unwrap_or_else(|e| e.into_inner());
        let Some(active) = inner.active.as_mut().filter(|run| run.id == self.id) else {
            return Decision::Cancel;
        };
        if active
            .current
            .as_ref()
            .is_none_or(|item| item.ticket.attempt != ticket.attempt)
        {
            return Decision::Cancel;
        }
        let result = decision(active);
        active.current = None;
        if result != Decision::Cancel {
            active.completed += 1;
        }
        result
    }

    pub fn invalidations(&self) -> HashSet<String> {
        self.control
            .0
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .active
            .as_ref()
            .filter(|run| run.id == self.id)
            .map(|run| run.invalidations.clone())
            .unwrap_or_default()
    }

    pub fn is_invalidated(&self, path: &str) -> bool {
        let inner = self.control.0.lock().unwrap_or_else(|e| e.into_inner());
        inner
            .active
            .as_ref()
            .filter(|run| run.id == self.id)
            .is_some_and(|run| run.invalidations.contains(&blacklist::path_key(path)))
    }
}

impl Drop for ScanRun {
    fn drop(&mut self) {
        let mut inner = self.control.0.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(active) = inner.active.as_mut().filter(|run| run.id == self.id) {
            active.current = None;
            active.running = false;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn stale_skip_cannot_affect_the_next_plugin_or_another_scan() {
        let control = Arc::new(ScanControl::default());
        let run = control.start("first".into()).unwrap();
        run.set_total(2);
        let first = run.begin_item("a.vst3");
        assert!(control.skip("first", "a.vst3"));
        assert_eq!(run.complete_item(first), Decision::Skip);
        let second = run.begin_item("b.vst3");
        assert!(!control.skip("first", "a.vst3"));
        assert!(!control.skip("other", "b.vst3"));
        assert_eq!(run.complete_item(second), Decision::Continue);
        assert_eq!(control.status("first").unwrap().completed, 2);
        drop(run);
        let next = control.start("second".into()).unwrap();
        next.begin_item("a.vst3");
        assert!(!control.skip("first", "a.vst3"));
    }

    #[test]
    fn cancel_is_per_run_while_shutdown_is_permanent() {
        let control = Arc::new(ScanControl::default());
        let first = control.start("first".into()).unwrap();
        assert!(control.cancel("first"));
        assert!(control.start("second".into()).is_err());
        assert_eq!(first.decision(None), Decision::Cancel);
        drop(first);
        let second = control.start("second".into()).unwrap();
        assert_eq!(second.decision(None), Decision::Continue);
        control.shutdown();
        assert_eq!(second.decision(None), Decision::Cancel);
        drop(second);
        assert!(control.start("third".into()).is_err());
    }

    #[test]
    fn skip_can_claim_an_item_before_the_process_is_published() {
        let control = Arc::new(ScanControl::default());
        let run = control.start("first".into()).unwrap();
        let ticket = run.begin_item("a.vst3");
        assert!(control.skip("first", "a.vst3"));
        assert!(!control.status("first").unwrap().can_skip);
        assert_eq!(run.decision(Some(ticket)), Decision::Skip);
        assert_eq!(run.complete_item(ticket), Decision::Skip);
        assert!(!control.skip("first", "a.vst3"));
    }
}
