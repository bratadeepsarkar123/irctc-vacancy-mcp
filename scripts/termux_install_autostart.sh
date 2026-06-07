#!/data/data/com.termux/files/usr/bin/sh
set -eu

APP_DIR="$HOME/irctc-vacancy-mcp"
BOOT_DIR="$HOME/.termux/boot"

mkdir -p "$BOOT_DIR"
cat > "$BOOT_DIR/irctc-start.sh" <<'SH'
#!/data/data/com.termux/files/usr/bin/sh
cd "$HOME/irctc-vacancy-mcp"
tmux has-session -t irctc-watchdog 2>/dev/null || tmux new -d -s irctc-watchdog ./scripts/termux_production_watchdog.sh
SH
chmod 700 "$BOOT_DIR/irctc-start.sh"

cd "$APP_DIR"
chmod 700 scripts/termux_production_watchdog.sh
tmux has-session -t irctc-watchdog 2>/dev/null || tmux new -d -s irctc-watchdog ./scripts/termux_production_watchdog.sh

echo "IRCTC watchdog started."
echo "If Termux:Boot is installed, it will also start after device reboot."
echo "Check current public URL with: cat ~/irctc-vacancy-mcp/public_url.txt"
