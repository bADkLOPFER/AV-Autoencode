#!/bin/bash
# Deployment script for Linux/macOS
echo "--- Starting Deployment ---"

# 1. Create/Update Virtual Environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# 2. Activate and Install
source venv/bin/activate
echo "Installing requirements..."
pip install --upgrade pip
pip install -r requirements.txt

echo "--- Deployment Complete ---"
