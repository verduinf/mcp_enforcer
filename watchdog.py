import ctypes
import json
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(r"D:\Dev\mcp_enforcer")
LOG_DIR = Path(r"D:\Share\mcp_enforcer_logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

WATCHDOG_LOG = LOG_DIR / "watchdog.log"
PID_FILE = LOG_DIR / "supervisor.pid"
HALTED_FILE = LOG_DIR / "supervisor.halted"
SERVER_PY = BASE_DIR / "server.py"
PYTHONW_EXE = r"D:\Dev\marvel_rag\.venv\Scripts\pythonw.exe"
SUPERVISOR_PY = BASE_DIR / "supervisor.py"

PROBE_URL = "https://127.0.0.1:8765/mcp"
PROBE_TIMEOUT_SEC = 10
RETRY_DELAY_SEC = 5


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    with WATCHDOG_LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def is_pid_alive(pid: int) -> bool:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == STILL_ACTIVE
        return False
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def supervisor_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        return is_pid_alive(pid)
    except Exception:
        return False


def probe_mcp() -> tuple[bool, str]:
    ctx = ssl._create_unverified_context()
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }).encode("utf-8")
    req = urllib.request.Request(
        PROBE_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=PROBE_TIMEOUT_SEC) as resp:
            if resp.status != 200:
                return False, f"HTTP {resp.status}"
            data = json.loads(resp.read().decode("utf-8"))
            if "result" in data:
                return True, "OK"
            return False, f"Unexpected JSON: {str(data)[:120]}"
    except Exception as exc:
        return False, repr(exc)


def kill_port_8765() -> None:
    ps = (
        "Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue "
        "| Select-Object -ExpandProperty OwningProcess -Unique "
        "| ForEach-Object { taskkill /F /T /PID $_ | Out-Null }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_supervisor() -> None:
    res = subprocess.run(
        ["schtasks", "/Run", "/TN", "EnforcerMcpServer"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if res.returncode == 0:
        log("Started EnforcerMcpServer scheduled task.")
        return

    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(
        [PYTHONW_EXE, str(SUPERVISOR_PY)],
        cwd=str(BASE_DIR),
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log(f"Started supervisor.py directly (schtasks rc={res.returncode}).")


def main() -> int:
    if HALTED_FILE.exists():
        if not SERVER_PY.exists() or HALTED_FILE.stat().st_mtime >= SERVER_PY.stat().st_mtime:
            return 0
        HALTED_FILE.unlink(missing_ok=True)
        log("server.py updated since halt; cleared supervisor.halted.")

    ok, detail = probe_mcp()
    if ok:
        if "--verbose" in sys.argv:
            print("OK")
        return 0

    time.sleep(RETRY_DELAY_SEC)
    ok2, detail2 = probe_mcp()
    if ok2:
        if "--verbose" in sys.argv:
            print("OK (after retry)")
        return 0

    log(f"Probe failed twice ({detail} | {detail2}); recovering :8765...")
    kill_port_8765()
    time.sleep(1)

    if supervisor_running():
        log("Killed wedged :8765 process; active supervisor will respawn uvicorn.")
    else:
        log("Supervisor not running; launching supervisor...")
        start_supervisor()
    return 0


if __name__ == "__main__":
    sys.exit(main())
