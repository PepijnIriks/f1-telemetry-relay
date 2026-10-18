#!/usr/bin/env bash
# Starts everything in one go (Linux/macOS). Usage: ./start.sh   or   ./start.sh simulate
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  . .venv/bin/activate
  pip install --quiet -r requirements.txt
else
  . .venv/bin/activate
fi
if [ "$1" = "simulate" ] || [ "$1" = "simulatie" ]; then
  python src/simulate.py &
  SIM=$!
  trap 'kill $SIM 2>/dev/null' EXIT
fi
python src/relay.py
