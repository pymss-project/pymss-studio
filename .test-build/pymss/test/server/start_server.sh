#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${MODEL+x}" ]]; then
  MODEL="bs_roformer_voc_hyperacev2"
fi
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
DEVICE="${DEVICE:-auto}"
SOURCE="${SOURCE:-modelscope}"

args=(
  --extra server
  pymss
)

if [[ -n "${MODEL_DIR:-}" ]]; then
  args+=(--model-dir "$MODEL_DIR")
fi

args+=(
  serve
  --host "$HOST"
  --port "$PORT"
  --device "$DEVICE"
  --source "$SOURCE"
)

if [[ -n "$MODEL" ]]; then
  args+=("$MODEL")
fi

if [[ -n "${ENDPOINT:-}" ]]; then
  args+=(--endpoint "$ENDPOINT")
fi

if [[ -n "${API_KEY:-}" ]]; then
  args+=(--api-key "$API_KEY")
fi

exec uv run "${args[@]}" "$@"
