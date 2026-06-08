#!/usr/bin/env bash
# EconGraph dev launcher for Mac / Linux
# Usage: ./start.sh
# Starts the backend (port 8101) and frontend (port 5180) in separate terminals.

set -e
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Check econgraph.db ───────────────────────────────────────────────────────
if [ ! -f "$REPO_DIR/econgraph.db" ]; then
  echo ""
  echo "  ✗ econgraph.db not found."
  echo "    Download it from https://github.com/kvmrbabu90/economy_network/releases/latest"
  echo "    and place it in: $REPO_DIR"
  echo ""
  exit 1
fi

# ── Kill stale listeners ─────────────────────────────────────────────────────
echo "Stopping any processes on ports 8101 and 5180..."
lsof -ti :8101 | xargs kill -9 2>/dev/null || true
lsof -ti :5180 | xargs kill -9 2>/dev/null || true
sleep 1

# ── Start backend ────────────────────────────────────────────────────────────
echo "Starting backend  → http://localhost:8101"
if [[ "$OSTYPE" == "darwin"* ]]; then
  osascript -e "tell app \"Terminal\" to do script \"cd '$REPO_DIR' && python -m uvicorn api.main:app --host '::' --port 8101 --reload\"" &
else
  # Linux: try common terminal emulators
  if command -v gnome-terminal &>/dev/null; then
    gnome-terminal -- bash -c "cd '$REPO_DIR' && python -m uvicorn api.main:app --host '::' --port 8101 --reload; exec bash" &
  elif command -v xterm &>/dev/null; then
    xterm -e "cd '$REPO_DIR' && python -m uvicorn api.main:app --host '::' --port 8101 --reload; bash" &
  else
    # Fallback: background process, logs to backend.log
    cd "$REPO_DIR"
    python -m uvicorn api.main:app --host '::' --port 8101 --reload > "$REPO_DIR/backend.log" 2>&1 &
    echo "  (no terminal emulator found — backend running in background, logs → backend.log)"
  fi
fi

sleep 2

# ── Start frontend ───────────────────────────────────────────────────────────
echo "Starting frontend → http://localhost:5180"
if [[ "$OSTYPE" == "darwin"* ]]; then
  osascript -e "tell app \"Terminal\" to do script \"cd '$REPO_DIR/web' && npm run dev -- --port 5180\"" &
else
  if command -v gnome-terminal &>/dev/null; then
    gnome-terminal -- bash -c "cd '$REPO_DIR/web' && npm run dev -- --port 5180; exec bash" &
  elif command -v xterm &>/dev/null; then
    xterm -e "cd '$REPO_DIR/web' && npm run dev -- --port 5180; bash" &
  else
    cd "$REPO_DIR/web"
    npm run dev -- --port 5180 > "$REPO_DIR/frontend.log" 2>&1 &
    echo "  (no terminal emulator found — frontend running in background, logs → frontend.log)"
  fi
fi

echo ""
echo "  Backend  : http://localhost:8101"
echo "  Frontend : http://localhost:5180"
echo ""
echo "Open http://localhost:5180 in your browser."
