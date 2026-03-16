#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

trim_whitespace() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

load_env_file_safely() {
  local env_file="$1"
  local raw_line=""
  local line=""
  local key=""
  local value=""
  local first_char=""
  local last_char=""

  while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
    line="$(trim_whitespace "$raw_line")"
    if [[ -z "$line" || "${line:0:1}" == "#" ]]; then
      continue
    fi
    if [[ "$line" == export\ * ]]; then
      line="$(trim_whitespace "${line#export }")"
    fi
    if [[ "$line" != *=* ]]; then
      continue
    fi

    key="$(trim_whitespace "${line%%=*}")"
    value="$(trim_whitespace "${line#*=}")"
    if [[ -z "$key" ]]; then
      continue
    fi

    if [[ ${#value} -ge 2 ]]; then
      first_char="${value:0:1}"
      last_char="${value: -1}"
      if [[ "$first_char" == "$last_char" && ( "$first_char" == "'" || "$first_char" == '"' ) ]]; then
        value="${value:1:${#value}-2}"
      fi
    fi

    export "$key=$value"
  done < "$env_file"
}

ENV_PATH="${ENV_PATH:-$ROOT_DIR/.env}"
if [[ -f "$ENV_PATH" ]]; then
  declare -A PRESERVED_ENV=()
  for key in \
    CONFIG_PATH \
    TELEGRAM_BOT_TOKEN \
    TELEGRAM_GROUP_ID \
    TELEGRAM_USER_ID \
    HERD_INVITE_TOKEN \
    HERD_ALIAS \
    HERD_SCOPE \
    HERD_CARGO \
    HERD_AGENT_TYPE \
    HERD_AGENT_COMMAND \
    HERD_AGENT_ARGS \
    HERD_AGENT_MODE \
    HERD_AGENT_PROMPT_MODE
  do
    if [[ -v "$key" ]]; then
      PRESERVED_ENV["$key"]="${!key}"
    fi
  done

  load_env_file_safely "$ENV_PATH"

  for key in "${!PRESERVED_ENV[@]}"; do
    export "$key=${PRESERVED_ENV[$key]}"
  done
fi

CONFIG_PATH="${CONFIG_PATH:-$ROOT_DIR/config.json}"
REQUIREMENTS_PATH="${REQUIREMENTS_PATH:-$ROOT_DIR/requirements.txt}"

if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python)"
else
  echo "Python not found. Install Python 3.10+ or create a .venv first." >&2
  exit 1
fi

ensure_env_file() {
  if [[ -f "$ENV_PATH" || ! -f "$ROOT_DIR/.env.example" ]]; then
    return
  fi

  mkdir -p "$(dirname "$ENV_PATH")"
  cp "$ROOT_DIR/.env.example" "$ENV_PATH"
  echo "[herd] Created $ENV_PATH from .env.example."
}

ensure_local_venv() {
  if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
    return
  fi

  echo "[herd] Creating local virtualenv in $ROOT_DIR/.venv..."
  "$PYTHON_BIN" -m venv "$ROOT_DIR/.venv"
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
}

python_requirements_ready() {
  "$PYTHON_BIN" -c 'import importlib.util, sys
modules = ("telegram", "schedule", "httpx")
missing = [name for name in modules if importlib.util.find_spec(name) is None]
raise SystemExit(0 if not missing else 1)'
}

install_python_requirements() {
  if [[ ! -f "$REQUIREMENTS_PATH" ]]; then
    echo "requirements.txt not found: $REQUIREMENTS_PATH" >&2
    exit 1
  fi

  echo "[herd] Installing Python dependencies from $REQUIREMENTS_PATH..."
  "$PYTHON_BIN" -m ensurepip --upgrade >/dev/null 2>&1 || true
  if ! "$PYTHON_BIN" -m pip install -r "$REQUIREMENTS_PATH"; then
    echo "[herd] Automatic dependency install failed. Try again or run:" >&2
    echo "  $PYTHON_BIN -m pip install -r $REQUIREMENTS_PATH" >&2
    exit 1
  fi
}

prepare_guided_runtime() {
  ensure_env_file
  ensure_local_venv
  if ! python_requirements_ready; then
    install_python_requirements
  fi
}

config_is_complete() {
  "$PYTHON_BIN" -c 'import os, sys
from pathlib import Path
from env_loader import load_env
import bridge
load_env()
config = bridge.load_config(Path(os.getenv("CONFIG_PATH", sys.argv[1])))
raise SystemExit(0 if bridge.is_config_complete(config) else 1)' "$CONFIG_PATH"
}

usage() {
  cat <<EOF
Usage: ./start.sh [mode]

Modes:
  guided      Recommended. Prepares the local runtime, opens setup on first run, and starts the unified app once configured.
  setup       Starts the interactive setup wizard.
  bridge      Starts only bridge.py.
  ui          Starts the bridge UI alongside the bot.
  gatekeeper  Starts only gatekeeper.py.
  cron        Starts only cron_manager.py.
  all         Starts the unified Herd runner with bridge, gatekeeper, and cron.
  demo        Starts bridge.py in demo mode.
  prepare     Creates .env/.venv when needed and installs Python dependencies.
  help        Shows this help.

Environment variables:
  ENV_PATH             Path to the .env file. Default: ./.env
  CONFIG_PATH          Path to the config JSON file. Default: ./config.json
  TELEGRAM_BOT_TOKEN   Overrides the token read from .env or config.json
EOF
}

read_token_from_config() {
  "$PYTHON_BIN" -c 'import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
if not path.exists():
    print("")
    raise SystemExit(0)
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    print("")
    raise SystemExit(0)
print(data.get("telegram_bot_token", ""))' "$CONFIG_PATH"
}

require_config() {
  if [[ ! -f "$CONFIG_PATH" ]]; then
    echo "Config file not found: $CONFIG_PATH" >&2
    exit 1
  fi
}

ensure_token() {
  local token="${TELEGRAM_BOT_TOKEN:-}"

  if [[ -z "$token" ]]; then
    require_config
    token="$(read_token_from_config)"
  fi

  if [[ -z "$token" ]]; then
    echo "Telegram bot token not found. Set TELEGRAM_BOT_TOKEN, add it to $ENV_PATH, or keep a legacy telegram_bot_token in $CONFIG_PATH." >&2
    exit 1
  fi

  export TELEGRAM_BOT_TOKEN="$token"
}

run_single() {
  exec "$PYTHON_BIN" "$@"
}

declare -a PIDS=()

start_background() {
  local name="$1"
  shift

  echo "[herd] Starting $name..."
  "$PYTHON_BIN" "$@" &
  local pid=$!
  PIDS+=("$pid")
  echo "[herd] $name started with PID $pid."
}

cleanup() {
  if [[ ${#PIDS[@]} -eq 0 ]]; then
    return
  fi

  echo
  echo "[herd] Stopping services..."
  kill "${PIDS[@]}" 2>/dev/null || true
  wait "${PIDS[@]}" 2>/dev/null || true
}

MODE="${1:-guided}"

if [[ $# -gt 0 ]]; then
  shift
fi

case "$MODE" in
  help|-h|--help)
    usage
    ;;
  guided)
    prepare_guided_runtime
    if config_is_complete; then
      echo "[herd] Configuration detected. Opening Herd dashboard and unified runner..."
      run_single herd_runner.py --ui "$@"
    else
      echo "[herd] No complete config found yet. Opening the setup wizard..."
      run_single bridge.py --setup "$@"
    fi
    ;;
  prepare)
    prepare_guided_runtime
    echo "[herd] Local runtime is ready."
    ;;
  setup)
    run_single bridge.py --setup "$@"
    ;;
  bridge)
    run_single bridge.py "$@"
    ;;
  ui)
    run_single bridge.py --ui "$@"
    ;;
  demo)
    run_single bridge.py --demo "$@"
    ;;
  gatekeeper)
    ensure_token
    run_single gatekeeper.py --token "$TELEGRAM_BOT_TOKEN" "$@"
    ;;
  cron)
    ensure_token
    run_single cron_manager.py --token "$TELEGRAM_BOT_TOKEN" "$@"
    ;;
  all)
    run_single herd_runner.py "$@"
    ;;
  *)
    echo "Unknown mode: $MODE" >&2
    usage >&2
    exit 1
    ;;
esac
