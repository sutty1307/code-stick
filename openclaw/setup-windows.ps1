# code-stick - OpenClaw Setup (one-time, Windows)
$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  OpenClaw Setup - One-Time Install (Windows)" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

if (Get-Command openclaw -ErrorAction SilentlyContinue) {
    Write-Host "  Already installed: $(& openclaw --version 2>&1)" -ForegroundColor Green
    Write-Host "  Run openclaw\start-with-openclaw-windows.bat to launch."
    $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown") | Out-Null
    exit 0
}

try { $null = Invoke-WebRequest -Uri "https://openclaw.ai" -UseBasicParsing -TimeoutSec 10 }
catch { Write-Host "  ERROR: No internet." -ForegroundColor Red; $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown") | Out-Null; exit 1 }

try { iwr -useb https://openclaw.ai/install.ps1 | iex }
catch { Write-Host "  ERROR: $($_)" -ForegroundColor Red; $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown") | Out-Null; exit 1 }

Write-Host ""
if (Get-Command openclaw -ErrorAction SilentlyContinue) {
    Write-Host "  Done! Run openclaw\start-with-openclaw-windows.bat." -ForegroundColor Green
} else {
    Write-Host "  WARNING: Restart PowerShell and retry." -ForegroundColor Yellow
}
$Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown") | Out-Null
