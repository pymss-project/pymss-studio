use std::sync::{Condvar, Mutex};
use std::time::Duration;

pub(super) const CANCEL_COMPLETION_TIMEOUT: &str = "RUNTIME_CANCEL_TIMEOUT: Runtime maintenance did not finish recovery within its time limit. Restart the application before using the runtime.";

#[derive(Default)]
pub(crate) struct RuntimeMaintenanceCompletion {
    outcome: Mutex<Option<Result<(), String>>>,
    changed: Condvar,
}

impl RuntimeMaintenanceCompletion {
    pub(super) fn complete_after_release<R, A>(
        &self,
        runtime_lease: R,
        application_lease: A,
        result: Result<(), String>,
    ) -> Result<(), String> {
        drop(runtime_lease);
        drop(application_lease);
        self.complete(result)
    }

    // The supervisor calls this only after releasing runtime and application leases.
    // A timeout is permanent for this operation, even if the worker finishes later.
    pub(super) fn complete(&self, result: Result<(), String>) -> Result<(), String> {
        let mut outcome = self
            .outcome
            .lock()
            .map_err(|_| "Runtime completion lock poisoned".to_string())?;
        let result = outcome.get_or_insert(result).clone();
        self.changed.notify_all();
        result
    }

    pub(super) fn wait(&self, timeout: Duration, on_timeout: impl FnOnce()) -> Result<(), String> {
        let outcome = self
            .outcome
            .lock()
            .map_err(|_| "Runtime completion lock poisoned".to_string())?;
        let (mut outcome, _) = self
            .changed
            .wait_timeout_while(outcome, timeout, |outcome| outcome.is_none())
            .map_err(|_| "Runtime completion lock poisoned".to_string())?;
        if outcome.is_none() {
            on_timeout();
            *outcome = Some(Err(CANCEL_COMPLETION_TIMEOUT.to_string()));
            self.changed.notify_all();
        }
        outcome.as_ref().unwrap().clone()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{mpsc, Arc};

    #[test]
    fn cancellation_waits_until_the_supervisor_completes_and_preserves_failures() {
        for result in [
            Ok(()),
            Err("RUNTIME_RECOVERY_FAILED: Environment files remain occupied".into()),
        ] {
            let completion = Arc::new(RuntimeMaintenanceCompletion::default());
            let (started, receive_started) = mpsc::channel();
            let (finished, receive_finished) = mpsc::channel();
            let waiting = completion.clone();
            let waiter = std::thread::spawn(move || {
                started.send(()).unwrap();
                finished
                    .send(waiting.wait(Duration::from_secs(2), || {}))
                    .unwrap();
            });
            receive_started.recv().unwrap();
            assert!(receive_finished
                .recv_timeout(Duration::from_millis(30))
                .is_err());
            assert_eq!(completion.complete(result.clone()), result);
            assert_eq!(
                receive_finished
                    .recv_timeout(Duration::from_secs(1))
                    .unwrap(),
                result
            );
            waiter.join().unwrap();
        }
    }

    #[test]
    fn completion_timeout_cannot_be_replaced_by_late_success() {
        let completion = RuntimeMaintenanceCompletion::default();
        let failure = completion.wait(Duration::from_millis(1), || {});
        assert_eq!(failure, Err(CANCEL_COMPLETION_TIMEOUT.to_string()));
        assert_eq!(completion.complete(Ok(())), failure);
        assert_eq!(completion.wait(Duration::ZERO, || {}), failure);
    }
}
