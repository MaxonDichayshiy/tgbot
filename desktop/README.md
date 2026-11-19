# Слопчик Desktop

Windows desktop shell built with Tauri 2. The desktop interface is served by the existing Render application at `/desktop/` and uses the same authenticated API, Telegram identity, and shared conversation state as the iOS app and Telegram bot.

## Build

On Windows with Rust 1.90+ and the Tauri prerequisites installed:

```powershell
cd desktop/src-tauri
cargo install tauri-cli --version "^2" --locked
cargo tauri build
```

The regular CI build produces the NSIS `.exe` installer. The `.ico` file is only the Windows application icon.

## Automatic updates

The desktop app checks GitHub Releases on startup. If a newer signed release exists, it asks before downloading and installing it. Updates are verified by Tauri's updater signature before installation.

### One-time signing setup

Tauri requires a stable signing keypair. Keep the private key private and back it up securely; never commit or send it to anyone.

1. In `desktop/src-tauri`, run:

   ```powershell
   cargo tauri signer generate -w "$env:USERPROFILE\.tauri\slopchik.key"
   ```

2. Copy the contents of `slopchik.key.pub` into `plugins.updater.pubkey` in `tauri.conf.json`, replacing `PUBLIC_KEY_PENDING`.
3. In GitHub repository Settings → Secrets and variables → Actions, create:
   - `TAURI_SIGNING_PRIVATE_KEY`: the full contents of `slopchik.key`
   - `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`: the password used when generating the key (leave empty only if no password was set).
4. Keep the private key and its backup safe. Losing it prevents signing future updates for existing installations.

### Publishing an update

Update the version in both `tauri.conf.json` and `Cargo.toml`, then push a matching tag such as `v0.1.1`. The release workflow builds the signed NSIS updater bundle and publishes `latest.json` plus the installer assets to GitHub Releases.

The public key and private key must remain a matched pair for the lifetime of the installed updater.
