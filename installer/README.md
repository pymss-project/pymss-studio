# Windows Installer (MoInstaller migration)

The Windows setup.exe is built with [MoInstaller](./moinstaller) (vendored Rust
toolchain, replacing Inno Setup).

| File | Purpose |
| --- | --- |
| `installer.toml.in` | Installer manifest template. CI replaces `__PYMSS_VERSION__` and `__STAGE_NAME__`, then `mo build` consumes the result. |
| `setup.rhai` | Install-time logic ported from the legacy `pymss-studio.iss` `[Code]` section. |
| `moinstaller/` | Vendored MoInstaller source (git subtree of the upstream repo). |
| `tests-fixture/` | Local/CI smoke test: fake staged package + full install/upgrade/uninstall assertions. |

## Build (local)

```pwsh
# 1. build the mo CLI inside the vendored subtree (two passes: the embedded
#    setup template is fixed on the second pass)
cargo build --release --manifest-path installer/moinstaller/Cargo.toml
cargo build --release --manifest-path installer/moinstaller/Cargo.toml

# 2. stage a package under installer/stage/<stage-name>/ (release pipeline output)
#    and render the manifest
$tpl = Get-Content installer/installer.toml.in -Raw
Set-Content installer/installer.toml ($tpl -replace '__PYMSS_VERSION__','1.2.3' -replace '__STAGE_NAME__','Pymss-Studio-1.2.3-windows-x64-cuda')

# 3. build
installer/moinstaller/target/release/mo.exe build installer/installer.toml --out release/setup.exe
```

## Smoke test

```pwsh
./installer/tests-fixture/make-fixture.ps1   # fake staged package
./installer/tests-fixture/smoke.ps1           # build + install + upgrade + uninstall
```

`smoke.ps1` runs without elevation: the fixture manifest installs into a temp
directory under the current user and uses an HKCU uninstall key.

## Mapping from the Inno Setup script

| Inno (`pymss-studio.iss`) | MoInstaller |
| --- | --- |
| `AppId {{6A208087-...}` | `app.id` (same GUID, so upgrades update the same control-panel entry) |
| `[Files]` + `Check: ShouldInstallBundledRuntimeEnvs` | single `[[files]]` rule; `before_file` in `setup.rhai` decides per file |
| `Excludes: active-runtime.json` | `before_file` returns false for it |
| `[Dirs] users-modify` | `[[hooks]]` running `icacls` (SID `*S-1-5-32-545`) |
| `[Icons]` + unchecked `desktopicon` task | `[[shortcuts]]` + optional component with `default = false` |
| `[Run]` VC++ runtime bootstrap | `after_install` in `setup.rhai` (`ctx.reg_read` + `ctx.run`) |
| `[Code] RepairVenvConfig` / `CleanupInstallTree` | `after_install` in `setup.rhai` (`ctx.write_text` / `ctx.delete_dir`) |
| `UninstallDisplayIcon` | `uninstall.display_icon` |
| `pymss-studio.inno-install` marker | kept (same filename) so upgrades over Inno installs are detected |
| `/SILENT`, `/VERYSILENT`, `/DIR=` | supported as-is |
| Inno uninstall removes the whole app dir | `uninstall.remove_app_dir = true` |

### Known behavior differences

* Shortcuts are created per-user (Inno created them for all users when running
  elevated); the silent `/TASKS=desktopicon` switch has no equivalent — the
  desktop icon component follows its `default = false` state in silent mode.
* `CloseApplications` is not supported: files locked by a running app make the
  installer fail (and roll back) instead of prompting to close the app.
* The setup.exe icon is the generic MoInstaller icon.
* The installer UI is bilingual (Chinese/English, auto-detected) instead of the
  Inno language selection page.
