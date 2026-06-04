#!/usr/bin/env bash
# code-stick + OpenClaw Chat Interface (Linux)
# Launches OpenClaw pointed at the USB Ollama already running from code-stick.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
USB_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo ""
echo "==========================================================="
echo "  code-stick + OpenClaw Chat Interface (Linux)"
echo "==========================================================="
echo ""

export OLLAMA_API_KEY="ollama-local"
export OLLAMA_HOST="127.0.0.1:11434"
export OPENCLAW_STATE_DIR="$SCRIPT_DIR/state"
export OPENCLAW_CONFIG_PATH="$SCRIPT_DIR/openclaw.json"

mkdir -p "$OPENCLAW_STATE_DIR"

if ! curl -s http://127.0.0.1:11434/api/tags &>/dev/null; then
    echo "Ollama not running. Starting from USB..."
    export OLLAMA_MODELS="$USB_ROOT/models"
    OLLAMA_BIN=""
    for candidate in \
        "$USB_ROOT/bin/ollama-linux-x64" \
        "$USB_ROOT/bin/ollama-linux-arm64" \
        "$USB_ROOT/bin/ollama" \
        "$USB_ROOT/ollama/ollama"; do
        if [ -f "$candidate" ]; then
            OLLAMA_BIN="$candidate"
            break
        fi
    done
    if [ -n "$OLLAMA_BIN" ]; then
        chmod +x "$OLLAMA_BIN"
        "$OLLAMA_BIN" serve &
        OLLAMA_PID=$!
        sleep 4
    else
        echo "WARNING: Ollama binary not found. Run 'code-stick install' first."
    fi
fi

if ! command -v openclaw &>/dev/null; then
    echo ""
    echo "ERROR: OpenClaw not installed. Run:"
    echo "  bash '$SCRIPT_DIR/setup-unix.sh'"
    kill "${OLLAMA_PID:-}" 2>/dev/null || true
    exit 1
fi

echo "Starting OpenClaw Gateway..."
openclaw gateway start &
GATEWAY_PID=$!
sleep 3

echo "Opening OpenClaw dashboard..."
openclaw dashboard

echo ""
echo "  ONLINE: http://localhost:18789"
echo ""
echo "Press ENTER to stop OpenClaw..."
read -r

openclaw gateway stop 2>/dev/null || true
echo "OpenClaw stopped."
