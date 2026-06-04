@echo off
title code-stick - OpenClaw Chat Interface
color 0B

echo.
echo ===========================================================
echo   code-stick + OpenClaw Chat Interface
echo   Launches OpenClaw alongside USB Ollama.
echo   NOTE: Start code-stick normally first to launch Ollama,
echo         OR use this script to start both together.
echo ===========================================================
echo.

set "USB_ROOT=%~dp0..\"
set "OLLAMA_API_KEY=ollama-local"
set "OLLAMA_HOST=127.0.0.1:11434"
set "OPENCLAW_STATE_DIR=%~dp0state"
set "OPENCLAW_CONFIG_PATH=%~dp0openclaw.json"

if not exist "%OPENCLAW_STATE_DIR%" mkdir "%OPENCLAW_STATE_DIR%"

:: Check if Ollama is already running; if not, start from USB
curl -s http://127.0.0.1:11434/api/tags >nul 2>&1
if errorlevel 1 (
    echo Ollama is not running. Starting from USB...
    set "OLLAMA_MODELS=%USB_ROOT%models"
    for %%d in (
        "%USB_ROOT%bin\ollama-windows-x64.exe"
        "%USB_ROOT%bin\ollama.exe"
        "%USB_ROOT%ollama\ollama.exe"
    ) do (
        if exist "%%~d" (
            start "" /B "%%~d" serve
            timeout /t 4 /nobreak >nul
            goto :OllamaStarted
        )
    )
    echo WARNING: Could not find Ollama binary. Run code-stick install first.
)
:OllamaStarted
echo Ollama available at http://127.0.0.1:11434

:: Check for OpenClaw
where openclaw >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: OpenClaw CLI not installed.
    echo Run: openclaw\setup-windows.ps1  (one-time setup)
    echo.
    pause
    exit /b 1
)

echo Starting OpenClaw Gateway...
start "" /B openclaw gateway start
timeout /t 3 /nobreak >nul

echo Opening OpenClaw dashboard...
start "" openclaw dashboard

echo.
echo ===========================================================
echo   OpenClaw chat: http://localhost:18789
echo   All models from USB Ollama auto-discovered
echo ===========================================================
echo.
echo Press any key to stop OpenClaw (Ollama keeps running).
pause >nul

openclaw gateway stop >nul 2>&1
echo OpenClaw stopped.
