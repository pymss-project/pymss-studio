//! Native plugin dialogs and menus can hold the helper UI thread inside a nested event loop.
//! Only the live session retains such a request; scanners and temporary exporters stay bounded.
use std::time::{Duration, Instant};

pub(super) const PENDING_PREFIX: &str = "PLUGIN_HOST_DIALOG_PENDING:";

pub(super) struct Pending {
    pub id: u64,
    pub operation: String,
    timeout: Duration,
    deadline: Option<Instant>,
}

impl Pending {
    pub fn new(id: u64, operation: String, started: Instant, timeout: Duration) -> Self {
        Self {
            id,
            operation,
            timeout,
            deadline: Some(started + timeout),
        }
    }

    pub fn suspend(&mut self) {
        self.deadline = None;
    }

    pub fn is_suspended(&self) -> bool {
        self.deadline.is_none()
    }

    pub fn remaining(&mut self) -> Duration {
        // Once the user has dismissed the dialog, SDK completion gets the same bounded budget
        // as the original request. Further retries cannot reset this deadline without a dialog.
        let now = Instant::now();
        self.deadline
            .get_or_insert(now + self.timeout)
            .saturating_duration_since(now)
    }
}

pub(super) fn message(title: &str) -> String {
    let dialog = if title.is_empty() {
        "the open plugin menu or dialog".into()
    } else {
        format!("the plugin dialog \"{title}\"")
    };
    format!(
        "{PENDING_PREFIX} Complete or cancel {dialog}, then retry. The audio host remains open."
    )
}

#[cfg(not(any(windows, target_os = "macos")))]
pub(super) fn owned_dialog(_pid: u32) -> Option<String> {
    None
}

#[cfg(target_os = "macos")]
pub(super) fn owned_dialog(pid: u32) -> Option<String> {
    use core_foundation::{
        base::{CFType, TCFType},
        dictionary::CFDictionary,
        number::CFNumber,
    };
    use core_graphics::window::{
        copy_window_info, kCGNullWindowID, kCGWindowLayer, kCGWindowListExcludeDesktopElements,
        kCGWindowListOptionOnScreenOnly, kCGWindowOwnerPID,
    };

    // The SDK's kCGPopUpMenuWindowLevel. Normal plugin editors use the floating level.
    const POP_UP_MENU_LEVEL: i32 = 101;
    let pid = i32::try_from(pid).ok()?;
    let windows = copy_window_info(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
        kCGNullWindowID,
    )?;
    // Only use owner and level metadata. Window titles and screen contents are unnecessary.
    // SAFETY: the keys are Core Graphics constants. The array returned by Quartz owns
    // each dictionary and its CF values for the duration of these lookups.
    let found = unsafe {
        windows.iter().any(|window| {
            let window = CFType::wrap_under_get_rule(*window);
            let Some(properties) = window.downcast::<CFDictionary>() else {
                return false;
            };
            let number = |key| {
                let value = properties.find(key)?;
                CFType::wrap_under_get_rule(*value)
                    .downcast::<CFNumber>()?
                    .to_i32()
            };
            number(kCGWindowOwnerPID.cast()) == Some(pid)
                && number(kCGWindowLayer.cast()) == Some(POP_UP_MENU_LEVEL)
        })
    };
    found.then(String::new)
}

#[cfg(windows)]
pub(super) fn owned_dialog(pid: u32) -> Option<String> {
    use windows_sys::Win32::Foundation::{HWND, LPARAM};
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        EnumWindows, GetAncestor, GetClassNameW, GetWindowTextW, GetWindowThreadProcessId,
        IsIconic, IsWindowVisible, GA_ROOTOWNER,
    };

    struct Search {
        pid: u32,
        title: Option<String>,
    }
    unsafe extern "system" fn visit(window: HWND, data: LPARAM) -> i32 {
        // SAFETY: EnumWindows invokes this callback synchronously with our live stack context.
        let search = unsafe { &mut *(data as *mut Search) };
        let mut pid = 0;
        unsafe {
            GetWindowThreadProcessId(window, &mut pid);
        }
        if pid != search.pid {
            return 1;
        }
        // Windows hides the complete owned-popup chain while the app editor is minimized.
        // A hidden standard modal can still block the helper UI thread until restoration.
        if unsafe {
            IsWindowVisible(window) == 0 && IsIconic(GetAncestor(window, GA_ROOTOWNER)) == 0
        } {
            return 1;
        }
        let mut class = [0u16; 64];
        let count = unsafe { GetClassNameW(window, class.as_mut_ptr(), class.len() as i32) };
        if count <= 0 || String::from_utf16_lossy(&class[..count as usize]) != "#32770" {
            return 1;
        }
        let mut title = [0u16; 512];
        let count = unsafe { GetWindowTextW(window, title.as_mut_ptr(), title.len() as i32) }.max(0)
            as usize;
        search.title = Some(String::from_utf16_lossy(&title[..count]));
        0
    }
    let mut search = Search { pid, title: None };
    // The caller still owns this unreaped child, so its PID cannot have been reused.
    unsafe {
        EnumWindows(Some(visit), (&mut search as *mut Search) as LPARAM);
    }
    search.title
}
