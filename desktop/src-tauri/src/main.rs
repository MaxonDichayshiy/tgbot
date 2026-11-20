#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::time::Duration;
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
use tauri_plugin_updater::UpdaterExt;

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .setup(|app| {
            let app_handle = app.handle().clone();

            // Check immediately after launch, then repeat every six hours.
            // A native prompt is shown only when a signed update is available.
            tauri::async_runtime::spawn(async move {
                loop {
                    let updater = match app_handle.updater() {
                        Ok(updater) => updater,
                        Err(error) => {
                            eprintln!("Не удалось подготовить проверку обновлений: {error}");
                            tokio::time::sleep(Duration::from_secs(6 * 60 * 60)).await;
                            continue;
                        }
                    };

                    match updater.check().await {
                        Ok(Some(update)) => {
                            let version = update.version.clone();
                            let install_handle = app_handle.clone();

                            app_handle
                                .dialog()
                                .message(format!(
                                    "Доступна новая версия Слопчика: {version}. Установить обновление сейчас?"
                                ))
                                .title("Обновление Слопчика")
                                .buttons(MessageDialogButtons::OkCancelCustom(
                                    "Обновить".to_string(),
                                    "Позже".to_string(),
                                ))
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
                        }
                        Ok(None) => {}
                        Err(error) => {
                            // A temporary network failure should not interrupt the app.
                            eprintln!("Не удалось проверить обновления: {error}");
                        }
                    }

                    tokio::time::sleep(Duration::from_secs(6 * 60 * 60)).await;
                }
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("Не удалось запустить Слопчик Desktop");
}
