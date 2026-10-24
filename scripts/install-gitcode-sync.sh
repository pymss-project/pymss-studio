#!/usr/bin/env bash
# 安装 launchd 定时任务：每 30 分钟把 GitHub 同步到 GitCode
# 用法: bash scripts/install-gitcode-sync.sh [卸载: --uninstall]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.pymss.gitcode-sync"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  rm -f "$PLIST"
  echo "已卸载 ${LABEL}"
  exit 0
fi

chmod +x "${ROOT}/scripts/gitcode-sync.sh"
mkdir -p "$HOME/Library/LaunchAgents"

cat >"$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${ROOT}/scripts/gitcode-sync.sh</string>
  </array>
  <key>StartInterval</key><integer>1800</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>${HOME}/Library/Logs/pymss-gitcode-sync.err.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLIST_EOF

plutil -lint "$PLIST"
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/${LABEL}"
echo "已安装并启动 ${LABEL}（每 30 分钟同步一次，睡眠期间自动跳过、唤醒后补跑）"
echo "日志: ~/Library/Logs/pymss-gitcode-sync.log"
echo "卸载: bash scripts/install-gitcode-sync.sh --uninstall"
