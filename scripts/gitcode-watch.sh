#!/usr/bin/env bash
# GitHub → GitCode 近实时同步：比对两端 refs 哈希，有差异才传输
# 结构：外层拿锁 + 刷新仓库列表 → xargs -P 并行调用自身 --one <repo>
#   （跨境 ls-remote 会抖动，串行一趟要几分钟，并行后几秒完成）
set -uo pipefail

GH_ORG="pymss-project"
GC_ORG="pymss-project"
GC_USER="${GITCODE_USER:-baicai-1145}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIRROR="$ROOT/.tmp/gitcode-mirror"
REPOS="$ROOT/repos.txt"
LOCK="/tmp/pymss-gitcode.lock"
PARALLEL="${PYMSS_SYNC_PARALLEL:-5}"

if [ -n "${PYMSS_SYNC_LOG:-}" ]; then LOG="$PYMSS_SYNC_LOG"
elif [ "$(uname)" = "Darwin" ]; then LOG="$HOME/Library/Logs/pymss-gitcode-sync.log"
else LOG="${XDG_STATE_HOME:-$HOME/.local/state}/pymss-gitcode-sync.log"; fi
mkdir -p "$MIRROR" "$(dirname "$LOG")" "$ROOT/.tmp"

if [ -z "${GITCODE_TOKEN:-}" ] && [ -f "$HOME/.config/pymss/gitcode.env" ]; then
  # shellcheck disable=SC1091
  . "$HOME/.config/pymss/gitcode.env"
fi
[ -z "${GITCODE_TOKEN:-}" ] && { echo "[$(date '+%F %T')] FATAL: no token" >>"$LOG"; exit 1; }
GC_URL="https://${GC_USER}:${GITCODE_TOKEN}@gitcode.com/${GC_ORG}"

log() { echo "[$(date '+%F %T')] $*" >>"$LOG"; }

# 同一错误 30 分钟内只记一次，避免每分钟刷屏
log_throttled() {
  local stamp="$ROOT/.tmp/.thr_$1"
  if [ ! -f "$stamp" ] || [ -n "$(find "$stamp" -mmin +30 2>/dev/null)" ]; then
    log "$2"; : >"$stamp"
  fi
}

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
refs() { timeout 90 git "${GOPTS[@]}" ls-remote --heads "$1" 2>/dev/null | awk '{print $2" "$1}' | sort; }
mirror_ok() { [ -d "$1" ] && git -C "$1" rev-parse --git-dir >/dev/null 2>&1; }

sync_one() {
  local name="$1" m="$MIRROR/$1.git" gh gc
  gh="$(refs "https://github.com/${GH_ORG}/${name}.git")"
  if [ -z "$gh" ]; then log_throttled "gh_$name" "[$name] GitHub refs 读取失败（网络），跳过"; return; fi
  gc="$(refs "https://gitcode.com/${GC_ORG}/${name}.git")"
  [ "$gh" = "$gc" ] && return

  log "[$name] 检测到差异 → 同步"
  if mirror_ok "$m"; then
    timeout 600 git -C "$m" "${GOPTS[@]}" remote update --prune >/dev/null 2>&1 \
      || { log_throttled "fetch_$name" "[$name] fetch 失败，本轮跳过"; return; }
  else
    rm -rf "$m"
    timeout 900 git "${GOPTS[@]}" clone --quiet --mirror "https://github.com/${GH_ORG}/${name}.git" "$m" 2>>"$LOG" \
      || { log_throttled "clone_$name" "[$name] CLONE FAILED，跳过"; return; }
  fi
  mirror_ok "$m" || { log_throttled "bad_$name" "[$name] 镜像不可用，跳过"; return; }

  if timeout 900 git -C "$m" "${GOPTS[@]}" push --quiet --force --prune "${GC_URL}/${name}.git" \
       'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*' 2>>"$LOG"; then
    log "[$name] 同步完成 $(git -C "$m" rev-parse --short HEAD 2>/dev/null)"
  else
    log_throttled "push_$name" "[$name] PUSH FAILED"
  fi
}

# ---- worker 模式（并行子进程，不再拿锁）----
if [ "${1:-}" = "--one" ]; then
  # 关键：关闭继承来的锁 fd。否则父进程被杀后，worker 的子孙（timeout/git/awk）
  # 会继续 pin 住 flock，导致后续每次运行都在 flock -n 处秒退（锁僵尸）
  exec 9>&- 2>/dev/null || true
  sync_one "${2:?repo name required}"
  exit 0
fi

# ---- 主模式 ----
exec 9>"$LOCK"
flock -n 9 || exit 0

# 仓库列表：24h 刷新一次（api.github.com 匿名限额 60/h）
if [ ! -s "$REPOS" ] || [ -n "$(find "$REPOS" -mmin +1440 2>/dev/null)" ]; then
  if curl -sL --max-time 30 "https://api.github.com/orgs/${GH_ORG}/repos?per_page=100" \
       | python3 -c "import json,sys; [print(r['name']) for r in json.load(sys.stdin)]" >"$REPOS.new" 2>/dev/null \
     && [ -s "$REPOS.new" ]; then
    mv "$REPOS.new" "$REPOS"
  else
    rm -f "$REPOS.new"
    log_throttled repos "仓库列表刷新失败，沿用旧列表"
  fi
fi
[ -s "$REPOS" ] || { log_throttled norepos "FATAL: repos.txt 为空"; exit 1; }

start=$(date +%s)
xargs -a "$REPOS" -P "$PARALLEL" -I{} "$ROOT/scripts/gitcode-watch.sh" --one {} 2>/dev/null
elapsed=$(( $(date +%s) - start ))

# 单趟耗时异常时记一笔，便于发现链路退化
if [ "$elapsed" -gt 120 ]; then log "本轮耗时 ${elapsed}s（链路偏慢）"; fi

[ -f "$LOG" ] && [ "$(wc -c <"$LOG")" -gt 2097152 ] && tail -c 500000 "$LOG" >"$LOG.tmp" && mv "$LOG.tmp" "$LOG"
exit 0
