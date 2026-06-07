#!/data/data/com.termux/files/usr/bin/sh
set -eu

REPO_URL="https://github.com/bratadeepsarkar123/irctc-vacancy-mcp.git"
APP_DIR="$HOME/irctc-vacancy-mcp"
ENV_FILE="$HOME/.irctc-worker.env"
ASKPASS_FILE="$HOME/.irctc-git-askpass.sh"

echo "== IRCTC Termux worker bootstrap =="
echo "This installs code/deps only. Do not paste cookies into chat."

pkg update -y
pkg install -y python git tmux curl wget openssh
pkg install -y termux-api || true

clone_or_pull() {
  if [ -d "$APP_DIR/.git" ]; then
    echo "Updating existing repo at $APP_DIR"
    cd "$APP_DIR"
    if git pull --ff-only; then
      return 0
    fi
  else
    echo "Cloning repo to $APP_DIR"
    if git clone "$REPO_URL" "$APP_DIR"; then
      cd "$APP_DIR"
      return 0
    fi
  fi

  echo
  echo "GitHub auth is needed. Use your GitHub username and PAT."
  printf "GitHub username: " > /dev/tty
  IFS= read -r GH_USER < /dev/tty
  printf "GitHub PAT: " > /dev/tty
  stty -echo < /dev/tty
  IFS= read -r GH_PAT < /dev/tty
  stty echo < /dev/tty
  printf "\n" > /dev/tty

  cat > "$ASKPASS_FILE" <<'SH'
#!/data/data/com.termux/files/usr/bin/sh
case "$1" in
  *Username*) printf "%s" "$GH_USER" ;;
  *Password*) printf "%s" "$GH_PAT" ;;
  *) printf "" ;;
esac
SH
  chmod 700 "$ASKPASS_FILE"
  export GIT_ASKPASS="$ASKPASS_FILE"
  export GIT_TERMINAL_PROMPT=0
  export GH_USER GH_PAT

  if [ -d "$APP_DIR/.git" ]; then
    cd "$APP_DIR"
    git pull --ff-only
  else
    if [ -e "$APP_DIR" ]; then
      mv "$APP_DIR" "$APP_DIR.failed.$(date +%s)"
    fi
    git clone "$REPO_URL" "$APP_DIR"
    cd "$APP_DIR"
  fi

  git remote set-url origin "$REPO_URL"
  rm -f "$ASKPASS_FILE"
  unset GH_PAT GH_USER GIT_ASKPASS GIT_TERMINAL_PROMPT
}

clone_or_pull

echo "Installing Termux-safe worker dependencies..."
python -m pip install --no-cache-dir requests python-dotenv pycryptodome
python -m pip install --no-cache-dir curl-cffi || true

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

if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock || true
fi

if command -v termux-clipboard-get >/dev/null 2>&1; then
  if ! grep -q "^export IRCTC_COOKIE=" "$ENV_FILE" 2>/dev/null; then
    echo
    echo "If your IRCTC cookie is copied on Android clipboard, press Enter to save it."
    echo "If not, press Ctrl+C now, copy it, then rerun this command."
    IFS= read -r _unused < /dev/tty
    COOKIE="$(termux-clipboard-get || true)"
    if [ -n "$COOKIE" ]; then
      python - "$ENV_FILE" "$COOKIE" <<'PY'
import pathlib
import shlex
import sys
env_file = pathlib.Path(sys.argv[1])
cookie = sys.argv[2]
with env_file.open("a", encoding="utf-8") as f:
    f.write("export IRCTC_COOKIE=" + shlex.quote(cookie) + "\n")
PY
      echo "Cookie saved to $ENV_FILE"
    else
      echo "Clipboard was empty or Termux:API is unavailable. Add cookie later."
    fi
  fi
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
python scripts/termux_chart_worker.py
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

cat > "$APP_DIR/start_all.sh" <<'SH'
#!/data/data/com.termux/files/usr/bin/sh
set -eu
cd "$HOME/irctc-vacancy-mcp"
tmux has-session -t irctc-worker 2>/dev/null || tmux new -d -s irctc-worker ./run_worker.sh
echo "Worker tmux session is running."
echo "Check: ./check_worker.sh"
SH
chmod +x "$APP_DIR/start_all.sh"

echo
echo "Bootstrap done."
echo "Next:"
echo "1) Probe:"
echo "   cd ~/irctc-vacancy-mcp && ./run_probe.sh"
echo "2) Start worker:"
echo "   cd ~/irctc-vacancy-mcp && ./start_all.sh"
