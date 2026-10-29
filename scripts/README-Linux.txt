Pymss Studio for Linux x86_64

Requires glibc 2.35 or newer (Ubuntu 22.04, Debian 12 or a newer compatible system).
ARM Linux is not included in this release.

1. Make the AppImage executable: chmod +x Pymss-Studio-*-linux-x64.AppImage
2. Run the AppImage and complete the first-time setup.
3. Install CPU, NVIDIA CUDA or AMD ROCm in Settings > Runtime Environment.

Python and audio tools are bundled. A system Python installation is not required.
Desktop integration uses the system xdg-open command (provided by xdg-utils).
Runtime environments and user data are stored under ~/.pymss-studio by default.
The first runtime installation requires a network connection and additional disk space.
GPU environments require compatible host drivers and supported hardware. The Linux
ROCm environment uses PyTorch 2.10 / ROCm 7.1; consult AMD's compatibility tables
before installing. GPU support depends on the driver, distribution and GPU model.

If FUSE is unavailable, run: ./Pymss-Studio-<version>-linux-x64.AppImage --appimage-extract-and-run

Check for updates in Settings. Releases enabled for automatic updates can be
downloaded, verified and installed in the application. Keep the AppImage in a
directory owned and writable by your user. Save your work and close additional
editor windows before updating. If the new version cannot start, the previous
AppImage is restored. Settings and runtime environments remain in ~/.pymss-studio.

For read-only locations or releases requiring manual installation, download the
new AppImage and replace the previous file after closing the application.
