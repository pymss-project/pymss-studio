//! Supply GTK3's unset font DPI before WebKitGTK captures its initial settings.

fn valid_dpi(value: f64) -> bool {
    // WebKitGTK converts DPI to an integer in units of 1/1024 DPI.
    (1.0..=f64::from(i32::MAX)).contains(&(value * 1024.0))
}

pub(crate) fn fallback_font_dpi(current: f64, configured: i32, scale: Option<&str>) -> Option<f64> {
    if valid_dpi(current) {
        return None;
    }
    let base = if configured > 0 {
        f64::from(configured) / 1024.0
    } else {
        96.0
    };
    let scaled = scale
        .and_then(|value| value.parse::<f64>().ok())
        .map(|value| base * value)
        .filter(|value| valid_dpi(*value));
    Some(scaled.unwrap_or(base))
}

#[cfg(target_os = "linux")]
struct FontDpiStartup {
    warning: Option<(&'static str, String)>,
}

#[cfg(target_os = "linux")]
fn ensure_valid_font_dpi() -> Option<(&'static str, String)> {
    use gtk::prelude::GtkSettingsExt;

    if !gtk::is_initialized_main_thread() {
        return Some((
            "render.font_dpi.unavailable",
            "GTK font DPI could not be checked on the main thread".into(),
        ));
    }
    let Some(screen) = gtk::gdk::Screen::default() else {
        return Some((
            "render.font_dpi.unavailable",
            "GTK has no default screen for font DPI validation".into(),
        ));
    };
    let current = screen.resolution();
    if valid_dpi(current) {
        return None;
    }
    let configured = gtk::Settings::for_screen(&screen)
        .map(|settings| settings.gtk_xft_dpi())
        .unwrap_or(-1);
    let scale = std::env::var("GDK_DPI_SCALE").ok();
    fallback_font_dpi(current, configured, scale.as_deref()).map(|dpi| {
        screen.set_resolution(dpi);
        ("render.font_dpi.fallback",
            format!("GTK font DPI was {current}; applied {dpi} DPI before creating WebViews (gtk-xft-dpi={configured})"))
    })
}

#[cfg(target_os = "linux")]
pub(crate) fn record_diagnostics(app: &tauri::AppHandle) {
    use tauri::Manager;

    // Session logs are initialized after window creation; keep the outcome until then.
    if let Some(state) = app.try_state::<FontDpiStartup>() {
        if let Some((event, message)) = &state.warning {
            crate::session_log::append(app, "WARN", event, vec![("message", message.clone())]);
        }
    }
}

#[cfg(target_os = "linux")]
pub(crate) fn init<R: tauri::Runtime>() -> tauri::plugin::TauriPlugin<R> {
    use tauri::Manager;

    // Plugin setup runs after Tao initializes GTK, before configured windows.
    tauri::plugin::Builder::new("linux-font-dpi")
        .setup(|app, _| {
            app.manage(FontDpiStartup {
                warning: ensure_valid_font_dpi(),
            });
            Ok(())
        })
        .build()
}
