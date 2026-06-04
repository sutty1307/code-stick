#!/usr/bin/env bash
# code-stick - OpenClaw Setup (one-time, Mac/Linux)
set -e

echo ""
echo "============================================================"
echo "  OpenClaw Setup - One-Time Install (Mac / Linux)"
echo "============================================================"
echo ""

if command -v openclaw &>/dev/null; then
    echo "  Already installed: $(openclaw --version 2>&1 || echo unknown)"
    echo "  Run openclaw/start-with-openclaw-mac.sh or -linux.sh."
    exit 0
fi

if ! curl -fsSL --max-time 10 https://openclaw.ai -o /dev/null 2>/dev/null; then
    echo "  ERROR: No internet connection."
    exit 1
fi

curl -fsSL https://openclaw.ai/install.sh | bash

echo ""
if command -v openclaw &>/dev/null; then
    echo "  Done! Run openclaw/start-with-openclaw-mac.sh or -linux.sh."
else
    echo "  WARNING: Restart terminal and retry."
fi
echo ""
