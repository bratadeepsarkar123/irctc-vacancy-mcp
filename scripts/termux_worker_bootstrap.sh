#!/data/data/com.termux/files/usr/bin/sh
set -eu

REPO_URL="https://github.com/bratadeepsarkar123/irctc-vacancy-mcp.git"
APP_DIR="$HOME/irctc-vacancy-mcp"
ENV_FILE="$HOME/.irctc-worker.env"

echo "== IRCTC Termux worker bootstrap =="
echo "This installs code/deps only. Do not paste cookies into chat."

pkg update -y
pkg install -y python git tmux curl wget openssh termux-api

if [ -d "$APP_DIR/.git" ]; then
  echo "Updating existing repo at $APP_DIR"
  cd "$APP_DIR"
  git pull --ff-only
else
  echo "Cloning repo to $APP_DIR"
  git clone "$REPO_URL" "$APP_DIR"
  cd "$APP_DIR"
fi

python -m pip install --upgrade pip
if ! python -m pip install -r requirements.txt; then
  echo "Full requirements install failed. Installing Termux worker minimum set..."
  python -m pip install fastapi "uvicorn[standard]" pydantic requests python-dotenv pycryptodome mcp websocket-client
  python -m pip install curl-cffi || true
fi

if [ ! -f "$ENV_FILE" ]; then
  SECRET="$(python - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)"
  {
    echo "export IRCTC_WORKER_SECRET='$SECRET'"
    echo "# Add the cookie locally. Best: copy it on Android, then run:"
    echo "# printf \"export IRCTC_COOKIE='%s'\\n\" \"\$(termux-clipboard-get)\" >> $ENV_FILE"
  } > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

cat > "$APP_DIR/run_probe.sh" <<'SH'
#!/data/data/com.termux/files/usr/bin/sh
set -eu
. "$HOME/.irctc-worker.env"
python src/deployment_probe.py --include-chart-composition
SH
chmod +x "$APP_DIR/run_probe.sh"

cat > "$APP_DIR/run_worker.sh" <<'SH'
#!/data/data/com.termux/files/usr/bin/sh
set -eu
. "$HOME/.irctc-worker.env"
python -m uvicorn src.chart_worker:app --host 127.0.0.1 --port 8001
SH
chmod +x "$APP_DIR/run_worker.sh"

cat > "$APP_DIR/check_worker.sh" <<'SH'
#!/data/data/com.termux/files/usr/bin/sh
set -eu
. "$HOME/.irctc-worker.env"
curl -sS -H "X-Worker-Secret: $IRCTC_WORKER_SECRET" http://127.0.0.1:8001/chart/health
echo
SH
chmod +x "$APP_DIR/check_worker.sh"

echo
echo "Bootstrap done."
echo "Next:"
echo "1) Copy IRCTC cookie on the tablet, then run:"
echo "   printf \"export IRCTC_COOKIE='%s'\\n\" \"\$(termux-clipboard-get)\" >> ~/.irctc-worker.env"
echo "2) Probe:"
echo "   cd ~/irctc-vacancy-mcp && ./run_probe.sh"
echo "3) Start worker:"
echo "   cd ~/irctc-vacancy-mcp && tmux new -s irctc-worker ./run_worker.sh"
