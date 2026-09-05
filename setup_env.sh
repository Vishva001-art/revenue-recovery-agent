#!/usr/bin/env bash
# Sets up a Python virtual environment for the AI Revenue Recovery Agent.
set -e

echo "Creating virtual environment..."
python3 -m venv venv

echo "Activating virtual environment..."
# shellcheck disable=SC1091
. venv/bin/activate

echo "Upgrading pip..."
pip install --upgrade pip

echo "Installing dependencies..."
pip install -r requirements.txt

echo ""
echo "Setup complete."
echo "Activate the environment with:  source venv/bin/activate  (Linux/Mac)  or  venv\\Scripts\\activate (Windows)"
echo "Then run:"
echo "  python generate_data.py     # create 200 synthetic transactions"
echo "  uvicorn main:app --reload   # start the API + dashboard"
