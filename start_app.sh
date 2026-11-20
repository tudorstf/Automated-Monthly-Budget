#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${PROJECT_DIR}"

if [[ ! -d "${PROJECT_DIR}/.venv" ]] || [[ ! -x "${PROJECT_DIR}/.venv/bin/python" ]] || ! "${PROJECT_DIR}/.venv/bin/python" -c "import fastapi, uvicorn, openpyxl, google.genai" >/dev/null 2>&1; then
    if [[ ! -x "${PROJECT_DIR}/.venv/bin/pip" ]]; then
        echo "Creating virtual environment in ${PROJECT_DIR}/.venv..."
        python3 -m venv "${PROJECT_DIR}/.venv"
    else
        echo "Installing missing dependencies into ${PROJECT_DIR}/.venv..."
    fi
    "${PROJECT_DIR}/.venv/bin/pip" install --upgrade pip
    "${PROJECT_DIR}/.venv/bin/pip" install -r "${PROJECT_DIR}/requirements.txt"
fi

exec "${PROJECT_DIR}/.venv/bin/python" "${PROJECT_DIR}/app.py" --open-browser "$@"
