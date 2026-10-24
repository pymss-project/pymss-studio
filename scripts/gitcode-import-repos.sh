#!/usr/bin/env bash
# 批量将 GitHub pymss-project 组织的所有仓库导入 GitCode（gitcode.com/pymss-project）
# 依赖: GITCODE_TOKEN 环境变量（已写入 ~/.zshrc）
# 用法: bash scripts/gitcode-import-repos.sh
set -euo pipefail

GH_ORG="pymss-project"
GC_ORG="pymss-project"
API="https://api.gitcode.com/api/v5"

: "${GITCODE_TOKEN:?请先 export GITCODE_TOKEN=<你的GitCode个人访问令牌>}"

curl -sL "https://api.github.com/orgs/${GH_ORG}/repos?per_page=100" | python3 -c "
import json,sys
for r in json.load(sys.stdin): print(r['name']+'\t'+(r['description'] or ''))" > .tmp/repo_list.tsv

while IFS=$'\t' read -r name desc; do
  echo "==> importing ${name}"
  python3 - "$name" "$desc" <<'PYEOF' > .tmp/payload.json
import json, sys, os
print(json.dumps({
  "access_token": os.environ["GITCODE_TOKEN"],
  "name": sys.argv[1], "path": sys.argv[1],
  "description": sys.argv[2],
  "public": 1, "auto_init": False,
  "import_url": f"https://github.com/pymss-project/{sys.argv[1]}.git"
}))
PYEOF
  curl -sS --max-time 55 -X POST "${API}/orgs/${GC_ORG}/repos" \
    -H "Content-Type: application/json" -d @.tmp/payload.json | python3 -c "
import json,sys
d=json.load(sys.stdin)
if d.get('id') or d.get('full_name'): print('    OK:', d.get('full_name'))
else: print('    FAILED:', json.dumps(d, ensure_ascii=False)[:400])"
  sleep 2
done < .tmp/repo_list.tsv
echo "ALL DONE"
