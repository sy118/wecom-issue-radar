mod commands;
mod config;
mod scheduler;
mod worker;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
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
            commands::sync_smart_sheet,
            commands::launch_key_extraction,
            commands::open_path,
        ])
        .run(tauri::generate_context!())
        .expect("error while running WeCom Issue Radar");
}
