@echo off
title Enforcer MCP Server (HTTPS :8765 --reload)
cd /d "D:\Dev\mcp_enforcer"
set TMP=D:\Temp
set TEMP=D:\Temp
set PYTHONPATH=D:\Dev\mcp_enforcer;D:\Dev\act-tracker;D:\Dev\marvel_rag

echo ============================================================================
echo   Enforcer Archival MCP Server (HTTPS :8765/mcp + Uvicorn Hot-Reload)
echo ============================================================================
"D:\Dev\marvel_rag\.venv\Scripts\python.exe" "D:\Dev\mcp_enforcer\server.py"
pause
