mod commands;
mod config;
mod scheduler;
mod worker;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_process::init())
        .setup(|app| {
            #[cfg(desktop)]
            app.handle()
                .plugin(tauri_plugin_updater::Builder::new().build())?;
            scheduler::start(app.handle().clone());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::bootstrap,
            commands::save_config,
            commands::detect_environment,
            commands::list_groups,
            commands::run_task,
            commands::list_schedules,
            commands::save_schedules,
            commands::run_schedule_now,
            commands::list_pending_smart_sheet_syncs,
            commands::clear_pending_smart_sheet_syncs,
            commands::preview_smart_sheet,
            commands::sync_smart_sheet,
            commands::launch_key_extraction,
            commands::prepare_update_install,
            commands::cancel_update_install,
            commands::open_path,
            commands::open_documentation,
        ])
        .run(tauri::generate_context!())
        .expect("error while running WeCom Issue Radar");
}
