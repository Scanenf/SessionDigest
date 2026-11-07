#!/usr/bin/env sh
# 按需修改输出目录；日志追加到 nightly.log
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/session_digest.py" --nightly \
  --obsidian-dir "$HOME/vault/AI摘要" "$@" >> "$SCRIPT_DIR/nightly.log" 2>&1
