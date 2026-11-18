#!/usr/bin/env bash
# Runs ON the exe.dev VM (deploy.sh calls it). Safe to re-run on every deploy.
set -euo pipefail

APP_DIR="$HOME/llmrun-mcp"
CONF_DIR="$HOME/.config/llmrun"
ENV_FILE="$CONF_DIR/env"
PORT="${LLMRUN_PORT:-8000}"

if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

cd "$APP_DIR"
uv sync --frozen --no-dev

mkdir -p "$CONF_DIR" "$HOME/.local/share/llmrun"
if [ ! -f "$ENV_FILE" ]; then
  umask 077
  TOKEN="$(uv run --no-dev llmrun-mcp new-token)"
  cat > "$ENV_FILE" <<CONF
LLMRUN_TOKEN=$TOKEN
LLMRUN_DB=$HOME/.local/share/llmrun/llmrun.db
LLMRUN_PORT=$PORT
CONF
  echo "NEW_TOKEN=$TOKEN"
fi

if [ -d /run/systemd/system ]; then
  sudo tee /etc/systemd/system/llmrun-mcp.service >/dev/null <<UNIT
[Unit]
Description=llmrun MCP server
After=network-online.target

[Service]
User=$USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/llmrun-mcp serve --host 0.0.0.0
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
  sudo systemctl enable llmrun-mcp >/dev/null
  sudo systemctl restart llmrun-mcp
  echo "service: systemd (logs: sudo journalctl -u llmrun-mcp -f)"
else
  # No systemd: run detached and restart on reboot via cron.
  pkill -f "llmrun-mcp serve" || true
  RUN="set -a; . $ENV_FILE; set +a; cd $APP_DIR; setsid nohup .venv/bin/llmrun-mcp serve --host 0.0.0.0 >> $HOME/llmrun.log 2>&1 &"
  bash -c "$RUN"
  if command -v crontab >/dev/null 2>&1; then
    ( crontab -l 2>/dev/null | grep -v 'llmrun-mcp serve' || true; echo "@reboot bash -c '$RUN'" ) | crontab -
  else
    echo "warning: no systemd or cron; rerun deploy after a VM reboot" >&2
  fi
  echo "service: nohup (logs: ~/llmrun.log)"
fi

for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "llmrun-mcp is up on port $PORT"; exit 0
  fi
  sleep 1
done
echo "llmrun-mcp did not answer on port $PORT; check the logs above" >&2
exit 1
