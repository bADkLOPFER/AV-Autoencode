#!/bin/bash
# Start script for Linux/macOS
echo "--- Starting Application ---"

if [ ! -d "venv" ]; then
    echo "[ERROR] Virtual environment 'venv' not found. Run deployment first!"
    exit 1
fi

source venv/bin/activate
exec python -m uvicorn src.server:app --host 0.0.0.0 --port 8000
