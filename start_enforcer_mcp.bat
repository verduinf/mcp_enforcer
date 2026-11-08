@echo off
cd /d "D:\Dev\mcp_enforcer"
set TMP=D:\Temp
set TEMP=D:\Temp
set PYTHONPATH=D:\Dev\mcp_enforcer;D:\Dev\act-tracker;D:\Dev\marvel_rag

echo [MCP_ENFORCER] Terminating any previous server instances on port 8765...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8765 ^| findstr LISTENING') do taskkill /f /t /pid %%a >nul 2>&1
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { ($_.CommandLine -like '*server:app*8765*' -or $_.CommandLine -like '*mcp_enforcer*server.py*') -and $_.ProcessId -ne $PID } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq Enforcer MCP Server (HTTPS :8765 --reload)*" >nul 2>&1
timeout /t 1 /nobreak >nul

title Enforcer MCP Server (HTTPS :8765 --reload)

echo ============================================================================
echo   Enforcer Archival MCP Server (HTTPS :8765/mcp + Uvicorn Hot-Reload)
echo ============================================================================
"D:\Dev\marvel_rag\.venv\Scripts\uvicorn.exe" server:app --host 0.0.0.0 --port 8765 --reload --reload-dir "D:\Dev\mcp_enforcer" --reload-dir "D:\Dev\act-tracker\src" --reload-dir "D:\Dev\marvel_rag\src" --ssl-certfile "D:\Dev\mcp_enforcer\certs\enforcer.crt" --ssl-keyfile "D:\Dev\mcp_enforcer\certs\enforcer.key"
pause
