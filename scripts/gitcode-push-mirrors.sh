#!/usr/bin/env bash
# 将本机已克隆的镜像仓库推送到 GitCode（绕开 GitCode 服务器连不上 GitHub 的问题）
# 前置: scripts/gitcode-create-and-push.sh 已建好空仓并完成 clone
# 用法: GITCODE_TOKEN=xxx GITCODE_USER=xxx bash scripts/gitcode-push-mirrors.sh
set -uo pipefail

GH_ORG="pymss-project"
GC_ORG="pymss-project"
MIRROR_TMP=".tmp/gitcode-mirror"

: "${GITCODE_TOKEN:?请先 export GITCODE_TOKEN}"
: "${GITCODE_USER:?请先 export GITCODE_USER}"

# URL 内嵌认证（credential helper 的函数引号在 macOS sh 下不可靠，改为直传）
GC_URL="https://${GITCODE_USER}:${GITCODE_TOKEN}@gitcode.com/${GC_ORG}"

curl -sL "https://api.github.com/orgs/${GH_ORG}/repos?per_page=100" | python3 -c "
import json,sys
for r in json.load(sys.stdin): print(r['name'])" > .tmp/repo_names.txt

while read -r name; do
  m="${MIRROR_TMP}/${name}.git"
  if [ ! -d "$m" ]; then
    echo "==> [${name}] mirror missing, cloning from GitHub"
    git clone --quiet --mirror "https://github.com/${GH_ORG}/${name}.git" "$m" \
      || { echo "    CLONE FAILED, skip"; continue; }
  else
    echo "==> [${name}] updating existing mirror"
    git -C "$m" remote update --prune >/dev/null 2>&1 || true
  fi
  echo "==> [${name}] pushing to GitCode"
  git -C "$m" push --force "${GC_URL}/${name}.git" \
    'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*' \
    && echo "    pushed OK" || echo "    PUSH FAILED"
done < .tmp/repo_names.txt
echo "ALL DONE"
