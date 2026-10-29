#![cfg_attr(windows, windows_subsystem = "windows")]

mod commands;
#[cfg(any(target_os = "macos", test))]
mod app_quit;
mod automatic_report;
mod diagnostic_context;
mod diagnostic_report;
mod error;
mod log_record;
mod model_dir_migration;
mod plugin_views;
#[cfg(any(target_os = "linux", test))]
mod appimage_environment;
#[cfg(any(target_os = "macos", test))]
mod macos_update;
#[cfg(any(target_os = "linux", test))]
mod linux_update;
#[cfg(any(target_os = "macos", target_os = "linux", test))]
mod update_startup;
#[cfg(any(target_os = "macos", target_os = "linux", test))]
mod update_stream;
mod python;
mod rba;
mod session_log;
mod single_instance;
mod state;
mod storage;
mod terminal;
mod update_manager;
mod update_sources;
mod workflow_editors;

use state::AppState;
use tauri::{Emitter, Manager};


fn main() {
    if let Some(code) = commands::plugin_host::run_capabilities_cli() {
        std::process::exit(code);
    }
    #[cfg(feature = "native-plugins")]
    if let Some(code) = pymss_audio_host::run_cli() {
        std::process::exit(code);
    }
    commands::plugin_host::configure_lab_environment();
    terminal::attach_parent();
    if session_log::configure_startup_logging(std::env::args_os().skip(1)) {
        session_log::early("DEBUG", "app.debug", "Verbose logging enabled by --debug");
    }
    #[cfg(not(feature = "vst-lab"))]
    if let Some(code) = update_manager::run_helper_from_args() {
        std::process::exit(code);
    }
    #[cfg(not(feature = "vst-lab"))]
    if let Some(code) = update_manager::recover_interrupted_update_from_startup() {
        std::process::exit(code);
    }
    // Managed-update/helper modes return above. Normal launches acquire a
    // process-wide lock so a second launch cannot create another WebView
    // process and operate on the same runtime environment concurrently.
    let _single_instance = match single_instance::acquire_or_focus() {
        Ok(Some(guard)) => Some(guard),
        Ok(None) => return,
        Err(error) => {
            // A mutex failure is rare (for example, a restricted user
            // session). Keep the application usable, while making the loss
            // of single-instance enforcement visible in diagnostics.
            crate::session_log::early("WARN", "app.diagnostic", &format!("Unable to create the single-instance mutex: {error}"));
            None
        }
    };

    let builder = tauri::Builder::default()
        .register_uri_scheme_protocol("pymss-plugin", |ctx, request| {
            plugin_views::serve_view(ctx.app_handle(), &request)
        })
        .on_page_load(|webview, _payload| {
            let window = webview.window();
            if !cfg!(target_os = "macos") {
                let _ = window.set_decorations(false);
            }
            let _ = window.show();
            let _ = window.set_focus();
        })
        .on_window_event(|window, event| {
            if matches!(event, tauri::WindowEvent::Destroyed)
                && (window.label().starts_with("workflow-node-editor")
                    || window.label().starts_with("workflow-simple-editor"))
            {
                workflow_editors::release_window(window.app_handle(), window.label());
                let event = if window.label().starts_with("workflow-simple-editor") {
                    "pymss://workflow-simple-editor-closed"
                } else {
                    "pymss://workflow-node-editor-closed"
                };
                let _ = window.app_handle().emit_to("main", event, serde_json::json!({ "label": window.label() }));
            }
        })
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_store::Builder::new().build())
        .manage(commands::plugin_host::PluginHostState::default())
        .manage(AppState::new())
        .manage(workflow_editors::WorkflowEditors::default());

    #[cfg(all(desktop, not(feature = "vst-lab")))]
    let builder = builder.plugin(tauri_plugin_updater::Builder::new().build());

    #[cfg(target_os = "macos")]
    let builder = builder
        .menu(app_quit::menu)
        .on_menu_event(app_quit::on_menu_event);

    builder
        .setup(|app| {
            let _ = session_log::init_session_log(app.handle());
            python::worker::recover_audio_output_temps(app.handle());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::plugin_host::plugin_host_capabilities,
            commands::plugin_host::plugin_host_scan,
            commands::plugin_host::plugin_host_scan_report,
            commands::plugin_host::plugin_host_cached_scan_report,
            commands::plugin_host::plugin_host_scan_status,
            commands::plugin_host::plugin_host_skip_scan,
            commands::plugin_host::plugin_host_cancel_scan,
            commands::plugin_host::plugin_host_scan_blacklist,
            commands::plugin_host::plugin_host_remove_scan_blacklist,
            commands::plugin_host::plugin_host_request,
            commands::plugin_host::plugin_host_render,
            commands::plugin_host::plugin_host_cancel_render,
            commands::plugin_host::plugin_host_cleanup_render,
            commands::app_cmd::cancel_task,
            commands::app_cmd::write_frontend_log,
            commands::app_cmd::close_current_window,
            commands::app_cmd::minimize_current_window,
            commands::app_cmd::toggle_maximize_current_window,
            commands::app_cmd::is_current_window_maximized,
            commands::app_cmd::start_drag_current_window,
            commands::app_cmd::create_blank_editor_project,
            commands::app_cmd::create_editor_project_from_task,
            commands::app_cmd::delete_model,
            commands::app_cmd::delete_tool_model,
            commands::app_cmd::start_model_delete,
            commands::app_cmd::delete_editor_project,
            commands::app_cmd::delete_editor_projects_for_tasks,
            commands::app_cmd::list_open_editor_projects_for_tasks,
            commands::app_cmd::list_orphaned_editor_projects,
            commands::app_cmd::delete_orphaned_editor_projects,
            commands::app_cmd::download_model,
            commands::app_cmd::editor_project_exists,
            commands::app_cmd::export_editor_mix,
            commands::app_cmd::start_editor_mix_export,
            commands::app_cmd::cancel_editor_mix_export,
            commands::tempo_key::start_tempo_key_analysis,
            commands::app_cmd::start_operation_task,
            commands::app_cmd::list_operations,
            commands::app_cmd::list_nodes,
            commands::app_cmd::plugin_marketplace,
            commands::app_cmd::debug_plugin_source,
            commands::app_cmd::debug_save_plugin_source,
            commands::app_cmd::load_plugin_tool_settings,
            commands::app_cmd::save_plugin_tool_settings,
            commands::app_cmd::manage_plugin,
            commands::app_cmd::set_plugin_view_root,
            commands::app_cmd::describe_operation,
            commands::app_cmd::get_app_paths,
            commands::app_cmd::get_env_info,
            commands::app_cmd::generate_waveform_peaks,
            commands::app_cmd::begin_editor_recording,
            commands::app_cmd::append_editor_recording_chunk,
            commands::app_cmd::finish_editor_recording,
            commands::app_cmd::cancel_editor_recording,
            commands::app_cmd::discard_editor_recording,
            commands::app_cmd::get_build_info,
            commands::app_cmd::get_audio_metadata,
            commands::app_cmd::get_model_info,
            commands::app_cmd::get_model_storage_summary,
            commands::app_cmd::import_editor_assets,
            commands::app_cmd::list_audio_files,
            commands::app_cmd::list_editor_projects,
            commands::app_cmd::list_models,
            commands::app_cmd::debug_catalog_info,
            commands::app_cmd::debug_catalog_save,
            commands::app_cmd::debug_catalog_reset,
            commands::app_cmd::debug_model_config,
            commands::app_cmd::debug_log_clear,
            commands::app_cmd::debug_log_create_report,
            commands::app_cmd::debug_log_info,
            commands::app_cmd::debug_editor_playback,
            commands::app_cmd::editor_preview_chunk,
            commands::app_cmd::debug_log_read,
            commands::app_cmd::check_managed_update,
            commands::app_cmd::start_managed_update,
            commands::app_cmd::confirm_managed_update_startup,
            commands::app_cmd::get_managed_update_state,
            commands::app_cmd::debug_runtime_override_active,
            commands::app_cmd::debug_runtime_pointers,
            commands::app_cmd::debug_runtime_restore_file,
            commands::app_cmd::debug_runtime_write_file,
            commands::app_cmd::inspect_custom_model,
            commands::app_cmd::start_custom_model_import,
            commands::app_cmd::unregister_custom_model,
            commands::app_cmd::relink_custom_model,
            commands::app_cmd::remap_custom_model_paths,
            commands::app_cmd::pick_model_weights_file,
            commands::app_cmd::pick_model_config_file,
            commands::app_cmd::load_app_store,
            commands::app_cmd::get_workflow_editors,
            commands::app_cmd::mutate_workflow_store,
            commands::app_cmd::load_editor_project,
            commands::app_cmd::relink_editor_sources,
            commands::app_cmd::open_editor_window,
            commands::app_cmd::open_workflow_node_editor_window,
            commands::app_cmd::open_workflow_simple_editor_window,
            commands::app_cmd::pick_media_files,
            commands::app_cmd::pick_audio_files,
            commands::app_cmd::pick_single_audio_file,
            commands::app_cmd::pick_input_folder,
            commands::app_cmd::pick_output_folder,
            commands::app_cmd::save_text_file_dialog,
            commands::app_cmd::prepare_model_dir_change,
            commands::app_cmd::reveal_path,
            commands::app_cmd::move_paths_to_trash,
            commands::app_cmd::respond_model_dir_migration_conflict,
            commands::app_cmd::confirm_model_dir_migration_switch,
            commands::app_cmd::cancel_model_dir_migration,
            commands::app_cmd::cleanup_model_residual_files,
            commands::app_cmd::start_cleanup_model_residual_files,
            commands::app_cmd::save_app_store,
            commands::app_cmd::save_editor_project,
            commands::app_cmd::scan_media_paths,
            commands::app_cmd::scan_audio_paths,
            commands::app_cmd::scan_audio_paths_with_options,
            commands::app_cmd::scan_editor_assets,
            commands::app_cmd::start_env_check,
            commands::app_cmd::start_model_dir_migration,
            commands::app_cmd::start_model_download,
            commands::app_cmd::start_separation,
            commands::app_cmd::start_workflow_inference,
            commands::app_cmd::test_proxy_connection,
            commands::app_cmd::update_proxy_settings,
            commands::app_cmd::runtime_info,
            commands::app_cmd::runtime_env_sizes,
            commands::app_cmd::runtime_core_versions,
            commands::app_cmd::optional_runtime_package_status,
            commands::app_cmd::manage_optional_runtime_package,
            commands::app_cmd::start_runtime_install,
            commands::app_cmd::start_runtime_core_update,
            commands::app_cmd::cancel_runtime_install,
            commands::app_cmd::cancel_runtime_core_update,
            commands::app_cmd::activate_runtime,
            commands::app_cmd::delete_runtime,
            commands::app_cmd::worker_health,
        ])
        .build(tauri::generate_context!())
        .expect("error while building Pymss Studio")
        .run(|app, event| {
            if matches!(event, tauri::RunEvent::Exit) {
                app.state::<commands::plugin_host::PluginHostState>().shutdown();
                session_log::finish_automatic_reports(app);
            }
        });
}
