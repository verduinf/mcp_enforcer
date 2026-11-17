import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(r"D:\Dev\mcp_enforcer")
LOG_DIR = Path(r"D:\Share\mcp_enforcer_logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

SUPERVISOR_LOG = LOG_DIR / "supervisor.log"
UVICORN_LOG = LOG_DIR / "uvicorn.log"
PID_FILE = LOG_DIR / "supervisor.pid"
HALTED_FILE = LOG_DIR / "supervisor.halted"

PYTHON_EXE = r"D:\Dev\marvel_rag\.venv\Scripts\python.exe"
UVICORN_EXE = r"D:\Dev\marvel_rag\.venv\Scripts\uvicorn.exe"

UVICORN_ARGS = [
    PYTHON_EXE,
    UVICORN_EXE,
    "server:app",
    "--host", "0.0.0.0",
    "--port", "8765",
    "--reload",
    "--reload-dir", r"D:\Dev\mcp_enforcer",
    "--reload-dir", r"D:\Dev\act-tracker\src",
    "--reload-dir", r"D:\Dev\marvel_rag\src",
    "--ssl-certfile", r"D:\Dev\mcp_enforcer\certs\enforcer.crt",
    "--ssl-keyfile", r"D:\Dev\mcp_enforcer\certs\enforcer.key",
]

RAPID_UPTIME_THRESHOLD_SEC = 60.0
MAX_RAPID_FAILURES = 5
MAX_LOG_BYTES = 10 * 1024 * 1024


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    with SUPERVISOR_LOG.open("a", encoding="utf-8") as f:
        f.write(line)
    if sys.stdout:
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except Exception:
            pass


def rotate_uvicorn_log() -> None:
    if UVICORN_LOG.exists() and UVICORN_LOG.stat().st_size > MAX_LOG_BYTES:
        backup = LOG_DIR / "uvicorn.log.1"
        if backup.exists():
            backup.unlink()
        UVICORN_LOG.rename(backup)


def cleanup_port_8765() -> None:
    ps = (
        "Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue "
        "| Select-Object -ExpandProperty OwningProcess -Unique "
        "| ForEach-Object { taskkill /F /T /PID $_ | Out-Null }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def main() -> int:
    if HALTED_FILE.exists():
        HALTED_FILE.unlink()

    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    log(f"Supervisor started (PID={os.getpid()})")

    env = os.environ.copy()
    env["TMP"] = r"D:\Temp"
    env["TEMP"] = r"D:\Temp"
    env["PYTHONPATH"] = r"D:\Dev\mcp_enforcer;D:\Dev\act-tracker;D:\Dev\marvel_rag"

    consecutive_failures = 0

    try:
        while True:
            rotate_uvicorn_log()
            cleanup_port_8765()
            started_at = time.monotonic()
            log("Starting uvicorn on :8765...")

            with UVICORN_LOG.open("a", encoding="utf-8") as ulog:
                proc = subprocess.Popen(
                    UVICORN_ARGS,
                    cwd=str(BASE_DIR),
                    env=env,
                    stdout=ulog,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                rc = proc.wait()

            uptime = time.monotonic() - started_at
            log(f"uvicorn exited with code {rc} after {uptime:.1f}s")

            if uptime >= RAPID_UPTIME_THRESHOLD_SEC:
                consecutive_failures = 0
                delay = 2
            else:
                consecutive_failures += 1
                if consecutive_failures >= MAX_RAPID_FAILURES:
                    msg = (
                        f"FATAL: {consecutive_failures} consecutive rapid failures "
                        f"(uptime < {RAPID_UPTIME_THRESHOLD_SEC}s). Halting supervisor."
                    )
                    log(msg)
                    HALTED_FILE.write_text(
                        f"{datetime.now().isoformat()} rc={rc}\n", encoding="utf-8"
                    )
                    return 1
                delay = min(60, 2 ** consecutive_failures)

            log(f"Restarting in {delay}s (consecutive_failures={consecutive_failures})...")
            time.sleep(delay)
    finally:
        if PID_FILE.exists() and PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
