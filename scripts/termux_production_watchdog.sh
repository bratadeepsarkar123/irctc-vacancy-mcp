#!/data/data/com.termux/files/usr/bin/sh
set -eu

APP_DIR="$HOME/irctc-vacancy-mcp"
cd "$APP_DIR"

termux-wake-lock 2>/dev/null || true

start_worker() {
  tmux has-session -t irctc-worker 2>/dev/null || tmux new -d -s irctc-worker ./run_worker.sh
}

start_connector() {
  tmux has-session -t irctc-connector 2>/dev/null || tmux new -d -s irctc-connector ./run_connector.sh
}

start_tunnel() {
  if ! tmux has-session -t irctc-tunnel 2>/dev/null; then
    rm -f cloudflared-connector.log public_url.txt
    tmux new -d -s irctc-tunnel 'cloudflared tunnel --protocol http2 --url http://127.0.0.1:8000 2>&1 | tee cloudflared-connector.log'
  fi
}

start_cloud_poller() {
  if grep -q '^export GCP_CONTROL_URL=' "$HOME/.irctc-worker.env" 2>/dev/null; then
    tmux has-session -t irctc-cloud-poller 2>/dev/null || tmux new -d -s irctc-cloud-poller './run_cloud_poller.sh'
  fi
}

while true; do
  start_worker
  start_connector
  start_cloud_poller
  start_tunnel
  url="$(tmux capture-pane -t irctc-tunnel -p -S -200 2>/dev/null | grep -Eo 'https://[-a-zA-Z0-9.]+\.trycloudflare\.com' | tail -n 1 || true)"
  if [ -n "$url" ]; then
    printf '%s\n' "$url" > public_url.txt
    python scripts/termux_register_worker.py "$url" >> register.log 2>&1 || true
  fi
  sleep 60
done
