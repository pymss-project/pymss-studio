#!/usr/bin/env bash
# 建空仓（GitCode 后端连不上 GitHub，import_url 导入会失败，改为本机中转推送）
# 用法: GITCODE_TOKEN=xxx bash scripts/gitcode-create-and-push.sh
set -uo pipefail

GH_ORG="pymss-project"
GC_ORG="pymss-project"
API="https://api.gitcode.com/api/v5"
GC_USER="${GITCODE_USER:-baicai-1145}"

: "${GITCODE_TOKEN:?请先 export GITCODE_TOKEN}"

MIRROR_TMP=".tmp/gitcode-mirror"
mkdir -p "$MIRROR_TMP"

# 凭据走 credential helper，避免 token 出现在 URL/日志里
CRED_HELPER='!f() { echo "username=${GC_USER}"; echo "password=${GITCODE_TOKEN}"; }; f'

curl -sL "https://api.github.com/orgs/${GH_ORG}/repos?per_page=100" | python3 -c "
import json,sys
for r in json.load(sys.stdin): print(r['name']+'\t'+(r['description'] or ''))" > .tmp/repo_list.tsv

while IFS=$'\t' read -r name desc; do
  echo "==> [${name}] creating repo"
  python3 - "$name" "$desc" <<'PYEOF' > .tmp/payload.json
import json, sys, os
print(json.dumps({
  "access_token": os.environ["GITCODE_TOKEN"],
  "name": sys.argv[1], "path": sys.argv[1],
  "description": sys.argv[2],
  "public": 1, "auto_init": False,
}))
PYEOF
  curl -sS --max-time 55 -X POST "${API}/orgs/${GC_ORG}/repos" \
    -H "Content-Type: application/json" -d @.tmp/payload.json | python3 -c "
import json,sys
d=json.load(sys.stdin)
if d.get('id') or d.get('full_name'): print('    created OK')
elif 'already' in json.dumps(d) or d.get('error_code')==400: print('    exists/notice:', json.dumps(d, ensure_ascii=False)[:200])
else: print('    FAILED:', json.dumps(d, ensure_ascii=False)[:300])"

  echo "==> [${name}] cloning from GitHub"
  rm -rf "${MIRROR_TMP:?}/${name}.git"
  if ! git clone --quiet --mirror "https://github.com/${GH_ORG}/${name}.git" "${MIRROR_TMP}/${name}.git"; then
    echo "    CLONE FAILED, skip"; continue
  fi

  echo "==> [${name}] pushing mirror to GitCode"
  git -C "${MIRROR_TMP}/${name}.git" \
    -c "credential.helper=${CRED_HELPER}" \
    push --force "https://gitcode.com/${GC_ORG}/${name}.git" \
    'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*' \
    && echo "    pushed OK" || echo "    PUSH FAILED"
done < .tmp/repo_list.tsv
echo "ALL DONE"
