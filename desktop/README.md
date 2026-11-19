# Слопчик Desktop

Windows desktop shell built with Tauri 2. The desktop interface is served by the existing Render application at `/desktop/` and uses the same authenticated API, Telegram identity, and shared conversation state as the iOS app and Telegram bot.

## Build

On Windows with Rust 1.90+ and the Tauri prerequisites installed:

```powershell
cd desktop/src-tauri
cargo install tauri-cli --version "^2" --locked
cargo tauri build
```

The regular CI build produces the NSIS `.exe` installer. The Windows icon is generated from the original assets in `Brand book/icons/` before packaging. The standard icon is the official lime variant.\n\nTo build locally with a selected official icon, install Pillow and run the generator before `cargo tauri build`:\n\n```powershell\npython -m pip install Pillow\npython desktop/scripts/generate_windows_icon.py lime\ncd desktop/src-tauri\ncargo tauri build\n```\n\nSupported variants: `lime`, `lime-invert`, `purple`, `orange`, `pink`, `blue`, `teal`, `rainbow`. The GitHub Actions workflow `Build Slopchik Desktop` lets you choose the Windows installer icon when started manually.\n\nThe in-app icon picker changes the brand mark shown inside the interface. The Windows shortcut and Start menu icon are fixed at installer build time.

## Automatic updates

The desktop app checks GitHub Releases on startup. If a newer signed release exists, it asks before downloading and installing it. Updates are verified by Tauri's updater signature before installation.

### One-time signing setup

Tauri requires a stable signing keypair. Keep the private key private and back it up securely; never commit or send it to anyone.

1. In `desktop/src-tauri`, run:

   ```powershell
   cargo tauri signer generate -w "$env:USERPROFILE\.tauri\slopchik.key"
   ```

2. The matching public key is already configured in `tauri.conf.json`; do not replace it unless you intentionally rotate the signing keypair. Keep the public and private keys matched.
3. In GitHub repository Settings → Secrets and variables → Actions, create:
   - `TAURI_SIGNING_PRIVATE_KEY`: the full contents of `slopchik.key`
   - `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`: the password used when generating the key (leave empty only if no password was set).
4. Keep the private key and its backup safe. Losing it prevents signing future updates for existing installations.

### Publishing an update

Update the version in both `tauri.conf.json` and `Cargo.toml`, then push a matching tag such as `v0.1.1`. The release workflow builds the signed NSIS updater bundle and publishes `latest.json` plus the installer assets to GitHub Releases.

The public key and private key must remain a matched pair for the lifetime of the installed updater.
