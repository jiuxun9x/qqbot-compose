#!/usr/bin/env bash
# Intentionally stops this Compose project's currently running services briefly.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
[[ -f .env && -d data ]] || { echo '找不到现有配置，停止备份。' >&2; exit 1; }
mkdir -p backups
chmod 700 backups
running=$(docker compose ps --services --status running)
restore_running() {
  if [[ -n "$running" ]]; then
    while IFS= read -r service; do docker compose start "$service"; done <<< "$running"
  fi
}
trap restore_running EXIT
if [[ -n "$running" ]]; then docker compose stop; fi
archive="backups/qqbot-$(date +%Y%m%d-%H%M%S)-$$.tar.gz"
tar -czf "$archive" .env data compose.yaml images.lock.json plugins system-prompt.txt
chmod 600 "$archive"
echo "备份完成：$archive（含私密数据，只保存在你控制的存储中）"
