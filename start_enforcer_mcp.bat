@echo off
title Enforcer MCP Server (HTTPS :8765 --reload)
cd /d "D:\Dev\mcp_enforcer"
set TMP=D:\Temp
set TEMP=D:\Temp
set PYTHONPATH=D:\Dev\mcp_enforcer;D:\Dev\act-tracker;D:\Dev\marvel_rag

echo Cleaning up any previous stuck process on port 8765...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8765" ^| findstr "LISTENING"') do (
    echo Killing old PID %%a...
    taskkill /F /T /PID %%a >nul 2>&1
)

echo ============================================================================
echo   Enforcer Archival MCP Server (HTTPS :8765/mcp + Uvicorn Hot-Reload)
echo ============================================================================
"D:\Dev\marvel_rag\.venv\Scripts\uvicorn.exe" server:app --host 0.0.0.0 --port 8765 --reload --reload-dir "D:\Dev\mcp_enforcer" --reload-dir "D:\Dev\act-tracker\src" --reload-dir "D:\Dev\marvel_rag\src" --ssl-certfile "D:\Dev\mcp_enforcer\certs\enforcer.crt" --ssl-keyfile "D:\Dev\mcp_enforcer\certs\enforcer.key"
pause
