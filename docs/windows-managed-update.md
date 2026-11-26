# Windows Managed Update Design

## Status

Managed updates have shipped since the first release after 0.0.11. The Windows command-line
launcher `pymss.exe` joined the update payload in 0.1.3. From 0.1.4 on, `Pymss Studio.exe` and
`pymss.exe` are always updated together and the client no longer requires the installer for any
update.

## Goals

- Use one signed ZIP update payload for both Inno Setup installations and portable directories.
- Verify every application update with the Tauri updater signing key before it is staged.
- Update application-managed files only: `Pymss Studio.exe`, `pymss.exe`, `python/`, and `bin/`.
- Never replace `python-runtime/`, `runtime-envs/`, model files, outputs, editor projects, or settings.
- Keep stable and prerelease update channels separate.
- Every published client must always have a path to the latest version without reinstalling.

## Distribution Detection

The installer writes `pymss-studio.inno-install` at the application root. A directory that
contains `pymss-studio.portable` is portable. The marker is part of the distribution contract;
an unmanaged directory does not receive an in-app update.

| Marker | Distribution | Update asset |
| --- | --- | --- |
| `pymss-studio.inno-install` | Inno installation | `update.zip`, elevated when required |
| `pymss-studio.portable` | Portable directory | `update.zip` |
| Neither | Unmanaged | Manual release download |

## Payload

`Pymss-Studio-<version>-windows-x64-update.zip` contains exactly:

| Entry | Purpose |
| --- | --- |
| `Pymss Studio.exe` | Desktop application |
| `pymss.exe` | Command-line launcher. It resolves the runtime Studio currently uses, so it ships and updates together with `Pymss Studio.exe` |
| `python/` | Worker scripts and bundled resources |

`bin/` (aria2c, FFmpeg) stays in the installed package and is not part of the archive. Add it only
when a tool update has to be distributed, and only after checking the whitelist of every client
generation below. `scripts/generate-update-metadata.py` rejects an archive that lacks either
executable.

## Metadata

Metadata is published under the CI-managed `updater` release. The release repository
`pymss-project/pymss-studio-releases` on ModelScope and Hugging Face mirrors it under `updater/`,
next to one directory per version. CI uploads the version directory first, so `updater/` never
names an archive that is not available yet. Clients from 0.1.4 on read ModelScope at
`updater/<file>`. Clients 0.1.3 and earlier scan the legacy dataset `huanlinoto/Pymss-Studio-Dist`
for the newest version directory, so each release also uploads its update files there until that
step is removed. Each file has the regular Tauri static JSON shape. A client builds its endpoint from the
selected channel and a generation compiled into the client, so every file below has a fixed set of
readers that cannot change after release.

| File (`stable-` / `prerelease-`) | Read by | Written |
| --- | --- | --- |
| `…-v1-windows-x64-update.json` | Windows clients 0.1.1 and earlier | Never again. It stays on 0.1.2, the last payload those clients accept (their whitelist rejects `pymss.exe`). 0.1.2 reads v2. |
| `…-v2-windows-x64-update.json` | Windows clients 0.1.2 and later | Every Windows release |
| `…-v1-macos-arm64-update.json` | All macOS clients | Every macOS release |
| `…-v1-linux-x64-update.json` | All Linux clients | Every Linux release |

Fields added to the Tauri shape:

| Field | Meaning |
| --- | --- |
| `pymss_update_supported` | The release allows in-app installation |
| `pymss_requires_manual_install`, `pymss_update_message`, `pymss_manual_install_url` | Shown when the release must be installed from its release page |
| `pymss_windows_cli_update` | Always `true` on Windows. Read only by 0.1.2 and 0.1.3 (see Compatibility) |

The Rust update manager uses `tauri_plugin_updater::Update::download`, which verifies the minisign
signature before returning any bytes. It never calls `Update::install` because Tauri's Windows
installer path only supports Tauri NSIS/MSI bundles.

## Update Flow

1. Download and verify the signed ZIP update payload.
2. Save the verified ZIP temporarily, then have the helper extract it under `update/` in the
   application directory. This keeps replacements on the target volume, including when a
   portable installation is on another drive.
3. Start a temporary copy of the executable with `--apply-managed-update` and exit the old process.
4. The helper moves the current managed files to a sibling backup directory, replaces
   `Pymss Studio.exe`, `pymss.exe` and the managed directories present in the archive, then starts
   the new executable. Entries missing from the archive are left in place.
5. If the installation directory is protected, the helper restarts itself with UAC elevation.
6. If replacement or new executable startup fails, the helper restores every moved path from
   the backup and removes a `pymss.exe` that the failed update added. Protected installation
   directories use Explorer to start the new UI at the user's normal integrity level after
   elevation, and verify that the target executable remains running before deleting the backup.
7. The temporary helper schedules its own deletion after the update attempt completes.

For Inno installations, the elevated helper also updates the existing uninstall registry entry's
`DisplayVersion`. The compiled application version is always supplied by the downloaded update;
the `PYMSS_VERSION` build environment variable is not persisted on the user machine.

ZIP path traversal and unexpected top-level entries are rejected.

## Compatibility

| Installed version | Path to the latest release |
| --- | --- |
| 0.1.1 and earlier | v1 → 0.1.2 → v2 → latest |
| 0.1.2 or 0.1.3, portable | v2 → latest |
| 0.1.2 or 0.1.3, installed by the 0.1.2 installer or later | v2 → latest |
| 0.1.2 or 0.1.3, installed by an earlier installer and updated in the app | Run the Online installer once into the same folder, then v2 → latest. Extracting the update archive instead keeps the installation without the marker, so the next update asks again. |
| 0.1.4 and later | v2 → latest |
| macOS, Linux | v1 → latest |

0.1.2 and 0.1.3 refuse an update containing `pymss.exe` when an Inno installation lacks
`pymss-studio.cli-uninstall-v1`, which only the 0.1.2 installer and later write. The check is
compiled into those clients; `pymss_windows_cli_update` lets them report it before downloading.
The Online installer is enough for any variant: it contains no `runtime-envs`, excludes
`active-runtime.json`, has no `[InstallDelete]`, and repairs the existing environments' `pyvenv.cfg`,
so installed CPU, CUDA or ROCm runtimes stay active. Its application files are the same build the
update archive is made from.

0.1.4 removed the check: files added by in-app updates are not in an older uninstall log, which
applies to every new file, so uninstall completeness never blocks an update. The installer keeps
writing the marker for those two versions.

Rules for future changes:

- A client may handle new installation state itself after an update. It must never require a
  reinstall to continue updating.
- Before changing the archive's top-level entries, check the whitelist of every client generation
  that still reads a metadata file. If a generation cannot accept the change, leave that file on
  the last payload it can apply and move newer clients to a new generation.

## Release Gates

- Stable metadata is replaced only by a successful stable release.
- Users select either the stable or prerelease channel in Settings; stable is the default.
- The Online stage builds the single Windows update ZIP from `Pymss Studio.exe`, `pymss.exe` and
  `python/`. Bundled tools remain in the installed package and are not replaced by this archive.
- Prerelease releases advance only prerelease metadata. A stable release advances both channels so
  testers receive the finalized version without switching channels.
- Every update ZIP receives a `.sig` made with the release signing key.
- Before publishing a release that changes the payload or update logic, verify that each row of the
  Compatibility table can apply the update, start the updated app, and run `env_info` plus
  `list_models`. This release gate is not yet a workflow job.
- The release includes `SHA256SUMS` for all user-downloadable artifacts.
