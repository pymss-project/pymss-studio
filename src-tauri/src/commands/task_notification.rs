use crate::error::{AppError, AppResult};
#[cfg(windows)]
use tauri::Manager;

#[cfg(target_os = "macos")]
pub(crate) mod macos;
#[cfg(windows)]
mod windows;

pub(crate) fn supported() -> bool {
    #[cfg(windows)]
    {
        // Development builds use the identity provided by the Windows notification library.
        return tauri::is_dev()
            || matches!(
                crate::update_manager::current_distribution(),
                Some(crate::update_manager::DistributionKind::Inno)
            );
    }

    #[cfg(target_os = "macos")]
    return macos::supported();

    #[cfg(not(any(windows, target_os = "macos")))]
    cfg!(target_os = "linux")
}

fn require_notification_window(window: &tauri::WebviewWindow) -> AppResult<()> {
    if window.label() != "main" || !supported() {
        return Err(AppError::Worker(
            "System notifications are unavailable.".into(),
        ));
    }
    Ok(())
}

#[tauri::command]
pub async fn prepare_task_notifications(window: tauri::WebviewWindow) -> AppResult<()> {
    require_notification_window(&window)?;
    #[cfg(windows)]
    if !tauri::is_dev() {
        let app_id = window.config().identifier.clone();
        tauri::async_runtime::spawn_blocking(move || windows::ensure_registered(&app_id))
            .await
            .map_err(|error| AppError::Worker(error.to_string()))??;
    }
    #[cfg(target_os = "macos")]
    macos::authorize().await?;
    Ok(())
}

#[tauri::command]
pub async fn show_task_notification(
    window: tauri::WebviewWindow,
    title: String,
    body: String,
) -> AppResult<()> {
    require_notification_window(&window)?;
    #[cfg(target_os = "macos")]
    return macos::send(&title, &body).await;

    #[cfg(windows)]
    let app_id = window.config().identifier.clone();
    #[cfg(any(windows, target_os = "linux"))]
    tauri::async_runtime::spawn_blocking(move || {
        let mut notification = notify_rust::Notification::new();
        notification.summary(&title).body(&body).auto_icon();
        #[cfg(windows)]
        {
            if !tauri::is_dev() {
                windows::ensure_registered(&app_id)?;
                notification.app_id(&app_id);
            }
            notification.sound_name("Default");
        }
        notification
            .show()
            .map(|_| ())
            .map_err(|error| AppError::Worker(error.to_string()))
    })
    .await
    .map_err(|error| AppError::Worker(error.to_string()))?
}
