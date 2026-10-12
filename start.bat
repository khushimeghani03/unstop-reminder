@echo off
rem Starts the reminder server in the background (no window) and opens the app.
cd /d "%~dp0"
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8765/api/data -TimeoutSec 2 | Out-Null } catch { Start-Process pythonw -ArgumentList 'server.py' -WorkingDirectory '%~dp0' -WindowStyle Hidden; Start-Sleep 2 }"
start "" http://127.0.0.1:8765
