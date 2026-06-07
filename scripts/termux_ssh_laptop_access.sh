#!/data/data/com.termux/files/usr/bin/sh
set -eu

echo "== Termux SSH access setup =="
echo "This lets your laptop control this tablet over the same Wi-Fi."

pkg update -y
pkg install -y openssh iproute2

if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock || true
fi

echo
echo "Set a Termux SSH password now. You will type this password from the laptop."
passwd < /dev/tty > /dev/tty

mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

sshd || true

USER_NAME="$(whoami)"
IP_ADDR="$(ip route get 8.8.8.8 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1)"

if [ -z "$IP_ADDR" ]; then
  IP_ADDR="$(ip -4 addr show wlan0 2>/dev/null | sed -n 's/.*inet \([0-9.]*\).*/\1/p' | head -n 1)"
fi

echo
echo "=============================================="
echo "Type this on your laptop PowerShell:"
echo
echo "ssh -p 8022 $USER_NAME@$IP_ADDR"
echo
echo "If this fails, connect both devices to the same hotspot/Wi-Fi and rerun this script."
echo "=============================================="
