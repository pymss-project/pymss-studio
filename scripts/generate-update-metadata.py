"""Generate platform-specific managed update channels from signed release assets."""

from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def generate(artifacts: Path, environment: dict[str, str]) -> list[str]:
    tag = environment["RELEASE_TAG"]
    version = tag.removeprefix("v")
    prerelease = environment.get("RELEASE_PRERELEASE", "false").lower() == "true"
    channels = ("prerelease",) if prerelease else ("stable", "prerelease")
    generated = []
    for slug, target, suffix, capability in (
        ("windows-x64", "windows-x86_64", "zip", "RELEASE_AUTO_UPDATE_SUPPORTED"),
        ("macos-arm64", "darwin-aarch64", "app.tar.gz", "RELEASE_MACOS_AUTO_UPDATE_SUPPORTED"),
        ("linux-x64", "linux-x86_64", "AppImage", "RELEASE_LINUX_AUTO_UPDATE_SUPPORTED"),
    ):
        asset = f"Pymss-Studio-{version}-{slug}{'' if slug == 'linux-x64' else '-update'}.{suffix}"
        if not (artifacts / asset).is_file():
            continue
        signature = (artifacts / f"{asset}.sig").read_text(encoding="utf-8").strip()
        if not signature:
            raise ValueError(f"Missing update signature: {asset}")
        supported = environment.get(capability, "false").lower() == "true"
        message = "" if supported else (
            environment.get("RELEASE_UPDATE_MESSAGE", "") if slug == "windows-x64" else ""
        ) or ("" if supported else "This release requires manual installation from its release page.")
        extra = {}
        if slug == "macos-arm64":
            sidecar = json.loads((artifacts / f"{asset}.json").read_text(encoding="utf-8"))
            if sidecar.get("version") != version:
                raise ValueError("The macOS update bundle version does not match the release")
            size = sidecar.get("pymss_unpacked_size")
            if type(size) is not int or not 0 < size <= 16 * 1024**3 or not sidecar.get("pymss_runtime_contract"):
                raise ValueError("Invalid macOS update runtime metadata")
            extra = {key: sidecar[key] for key in ("pymss_unpacked_size", "pymss_runtime_contract")}
        generations = ("v1",)
        if slug == "windows-x64":
            # Derive the channel from the actual signed payload. Full CLI updates
            # leave the last v1 bridge published for clients skipping versions.
            with zipfile.ZipFile(artifacts / asset) as archive:
                includes_cli = "pymss.exe" in archive.namelist()
            extra["pymss_windows_cli_update"] = includes_cli
            generations = ("v2",) if includes_cli else ("v1", "v2")
        for channel in channels:
            data = {
                "version": version,
                "notes": "See the GitHub release notes for details.",
                "pub_date": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "prerelease": prerelease,
                "channel": channel,
                "pymss_update_supported": supported,
                "pymss_requires_manual_install": not supported,
                "pymss_update_message": message,
                "pymss_manual_install_url": f"https://github.com/pymss-project/pymss-studio/releases/tag/{tag}",
                "platforms": {
                    target: {
                        "signature": signature,
                        "url": f"https://github.com/pymss-project/pymss-studio/releases/download/{tag}/{asset}",
                    }
                },
                **extra,
            }
            for generation in generations:
                name = f"{channel}-{generation}-{slug}-update.json"
                (artifacts / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                generated.append(name)
    if not generated:
        raise ValueError("No signed managed update archives were found")
    (artifacts / "managed-update-metadata.txt").write_text("\n".join(generated) + "\n", encoding="utf-8")
    return generated


if __name__ == "__main__":
    print("\n".join(generate(Path("artifacts"), dict(os.environ))))
