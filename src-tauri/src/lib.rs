mod commands;
mod config;
mod reply_runtime;
mod scheduler;
mod worker;

use tauri::Manager;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_process::init())
        .setup(|app| {
            #[cfg(desktop)]
            app.handle()
                .plugin(tauri_plugin_updater::Builder::new().build())?;
            app.manage(reply_runtime::ReplyRuntime::start(app.handle().clone()));
            scheduler::start(app.handle().clone());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::bootstrap,
            commands::save_config,
            commands::export_config_backup,
            commands::import_config_backup,
            commands::detect_environment,
            commands::list_groups,
            commands::run_task,
            commands::list_schedules,
            commands::save_schedules,
            commands::run_schedule_now,
            commands::list_schedule_execution_history,
            commands::list_pending_smart_sheet_syncs,
            commands::clear_pending_smart_sheet_syncs,
            commands::preview_smart_sheet,
            commands::sync_smart_sheet,
            commands::launch_key_extraction,
            commands::reply_runtime_execute,
            commands::reply_runtime_query,
            commands::prepare_update_install,
            commands::cancel_update_install,
            commands::open_path,
            commands::open_agent_log_directory,
            commands::open_documentation,
        ])
        .build(tauri::generate_context!())
        .expect("error while building WeCom Issue Radar");

    app.run(|app_handle, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            if let Some(runtime) = app_handle.try_state::<reply_runtime::ReplyRuntime>() {
                runtime.shutdown();
            }
        }
    });
}
