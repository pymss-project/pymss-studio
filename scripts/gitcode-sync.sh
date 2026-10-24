#!/usr/bin/env bash
# GitHub pymss-project → GitCode 全量镜像同步（幂等）
# 设计要点：
#   - flock 单实例：与 gitcode-watch.sh 共用一把锁，绝不并发跑
#   - 每个网络操作带 timeout + 重试（NAS 跨境链路会偶发 TLS 中断）
#   - 镜像目录缺失/损坏时自动重建，失败则本轮跳过该仓库而不是带病推送
set -uo pipefail

GH_ORG="pymss-project"
GC_ORG="pymss-project"
GC_USER="${GITCODE_USER:-baicai-1145}"
API="https://api.gitcode.com/api/v5"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIRROR="$ROOT/.tmp/gitcode-mirror"
LOCK="/tmp/pymss-gitcode.lock"

if [ -n "${PYMSS_SYNC_LOG:-}" ]; then LOG="$PYMSS_SYNC_LOG"
elif [ "$(uname)" = "Darwin" ]; then LOG="$HOME/Library/Logs/pymss-gitcode-sync.log"
else LOG="${XDG_STATE_HOME:-$HOME/.local/state}/pymss-gitcode-sync.log"; fi
mkdir -p "$MIRROR" "$(dirname "$LOG")"

if [ -z "${GITCODE_TOKEN:-}" ] && [ -f "$HOME/.config/pymss/gitcode.env" ]; then
  # shellcheck disable=SC1091
  . "$HOME/.config/pymss/gitcode.env"
fi
if [ -z "${GITCODE_TOKEN:-}" ]; then
  GITCODE_TOKEN="$(grep -E '^export GITCODE_TOKEN=' "$HOME/.zshrc" 2>/dev/null | tail -1 | sed -E 's/.*="?([^"]+)"?/\1/')"
fi
[ -z "${GITCODE_TOKEN:-}" ] && { echo "[$(date '+%F %T')] FATAL: GITCODE_TOKEN 未找到" >>"$LOG"; exit 1; }

log() { echo "[$(date '+%F %T')] $*" >>"$LOG"; }
GC_URL="https://${GC_USER}:${GITCODE_TOKEN}@gitcode.com/${GC_ORG}"

exec 9>"$LOCK"
flock -n 9 || { log "另一同步实例在运行，本轮跳过"; exit 0; }

export GIT_TERMINAL_PROMPT=0 GIT_HTTP_LOW_SPEED_LIMIT=1000 GIT_HTTP_LOW_SPEED_TIME=60
# 代理自动探测：GitHub 在境内直连被阻断，若本机有本地代理则仅对 github.com 启用
# （GitCode 为境内站点，保持直连，避免绕路）
PROXY_URL="${PYMSS_GIT_PROXY:-http://127.0.0.1:7890}"
GH_PROXY_OPT=()
if [ -z "${PYMSS_NO_PROXY:-}" ] && (exec 3<>/dev/tcp/127.0.0.1/7890) 2>/dev/null; then
  exec 3>&- 2>/dev/null
  GH_PROXY_OPT=(-c "http.https://github.com/.proxy=${PROXY_URL}")
fi
GOPTS=(-c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 "${GH_PROXY_OPT[@]}")

retry() { # retry <次数> <初始退避秒> <命令...>
  local tries="$1" delay="$2" i=1; shift 2
  while [ "$i" -le "$tries" ]; do
    "$@" && return 0
    [ "$i" -lt "$tries" ] && { log "  第 $i 次失败，${delay}s 后重试"; sleep "$delay"; delay=$((delay * 2)); }
    i=$((i + 1))
  done
  return 1
}

mirror_ok() { [ -d "$1" ] && git -C "$1" rev-parse --git-dir >/dev/null 2>&1; }
clone_m()   { timeout 900 git "${GOPTS[@]}" clone --quiet --mirror "$1" "$2"; }
fetch_m()   { timeout 900 git -C "$1" "${GOPTS[@]}" remote update --prune; }
push_m()    { timeout 900 git -C "$1" "${GOPTS[@]}" push --quiet --force --prune "$2" \
                'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'; }

names=$(curl -sL --max-time 30 "https://api.github.com/orgs/${GH_ORG}/repos?per_page=100" \
  | python3 -c "import json,sys; [print(r['name']) for r in json.load(sys.stdin)]")
[ -z "$names" ] && names=$(ls "$MIRROR" 2>/dev/null | sed 's/\.git$//')
[ -z "$names" ] && { log "FATAL: 无法获取仓库列表"; exit 1; }

fail=0
for name in $names; do
  m="$MIRROR/${name}.git"
  if mirror_ok "$m"; then
    retry 3 10 fetch_m "$m" 2>>"$LOG" || log "[$name] fetch 失败（继续用本地镜像推送）"
  else
    rm -rf "$m"
    if retry 3 15 clone_m "https://github.com/${GH_ORG}/${name}.git" "$m" 2>>"$LOG"; then
      log "[$name] 镜像克隆完成"
    else
      log "[$name] CLONE FAILED，跳过"; fail=1; continue
    fi
  fi

  if retry 2 10 push_m "$m" "${GC_URL}/${name}.git" 2>>"$LOG"; then
    log "[$name] OK $(git -C "$m" rev-parse --short HEAD 2>/dev/null)"
  else
    log "[$name] PUSH FAILED"; fail=1
  fi
done

log "=== 同步结束 fail=${fail} ==="
[ -f "$LOG" ] && [ "$(wc -c <"$LOG")" -gt 2097152 ] && tail -c 500000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
exit "$fail"
