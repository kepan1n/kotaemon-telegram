#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Stop old instances of this bot
pids=$(ps -eo pid,args | grep '\.venv/bin/python bot.py' | grep -v grep | awk '{print $1}' || true)
if [[ -n "${pids}" ]]; then
  echo "Stopping old bot pids: ${pids}"
  kill ${pids} || true
  sleep 1
  kill -9 ${pids} || true
fi

nohup ./.venv/bin/python bot.py >> bot.log 2>&1 < /dev/null &
echo "Started bot pid: $!"
