#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_SRC="${PROJECT_DIR}/systemd/receipt-watcher.service"
USER_SYSTEMD_DIR="${HOME}/.config/systemd/user"
SERVICE_DEST="${USER_SYSTEMD_DIR}/receipt-watcher.service"

if [[ ! -f "${PROJECT_DIR}/.env" ]] || [[ ! -f "${PROJECT_DIR}/token.json" ]]; then
    echo "Warning: .env or token.json is missing."
    echo "Run '.venv/bin/python main.py --setup' first so the watcher can authenticate."
fi

mkdir -p "${USER_SYSTEMD_DIR}"
sed "s|/home/tudor/Documents/monthly-budget-automation|${PROJECT_DIR}|g" "${SERVICE_SRC}" > "${SERVICE_DEST}"

systemctl --user daemon-reload
systemctl --user enable --now receipt-watcher.service

echo "Service installed and started!"
echo "Check status: systemctl --user status receipt-watcher.service"
echo "View logs:    journalctl --user -u receipt-watcher.service -f"
