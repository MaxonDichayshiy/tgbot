#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
use tauri_plugin_updater::UpdaterExt;

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(|app| {
            let app_handle = app.handle().clone();

            // Check for signed updates after launch. The user chooses whether to install.
            tauri::async_runtime::spawn(async move {
                let updater = match app_handle.updater() {
                    Ok(updater) => updater,
                    Err(error) => {
                        eprintln!("Не удалось подготовить проверку обновлений: {error}");
                        return;
                    }
                };

                let update = match updater.check().await {
                    Ok(Some(update)) => update,
                    Ok(None) => return,
                    Err(error) => {
                        eprintln!("Не удалось проверить обновления: {error}");
                        return;
                    }
                };

                let version = update.version.clone();
                let install_handle = app_handle.clone();

                app_handle
                    .dialog()
                    .message(format!(
                        "Доступна новая версия Слопчика: {version}. Установить обновление сейчас?"
                    ))
                    .title("Обновление Слопчика")
                    .buttons(MessageDialogButtons::OkCancelCustom("Обновить", "Позже"))
                    .show(move |confirmed| {
                        if confirmed {
                            let restart_handle = install_handle.clone();
                            tauri::async_runtime::spawn(async move {
                                match update.download_and_install(|_, _| {}, || {}).await {
                                    Ok(()) => restart_handle.restart(),
                                    Err(error) => {
                                        eprintln!("Не удалось установить обновление: {error}");
                                    }
                                }
                            });
                        }
                    });

            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("Не удалось запустить Слопчик Desktop");
}
