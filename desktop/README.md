# Слопчик Desktop

Windows desktop shell built with Tauri 2. The desktop interface is served by the existing Render application at `/desktop/` and uses the same authenticated API, Telegram identity, and shared conversation state as the iOS app and Telegram bot.

## Build

On Windows with Rust and the Tauri prerequisites installed:

```powershell
cd desktop/src-tauri
cargo install tauri-cli --version "^2" --locked
cargo tauri build
```

The GitHub Actions workflow `.github/workflows/build-desktop.yml` builds NSIS and MSI installers and uploads them as an artifact.

## Important

The desktop client intentionally shares the existing backend. It does not create a second AI history or duplicate model/provider infrastructure. Authentication uses the one-time code issued by the Telegram bot with `/app`.
