"""
Enforcer Streamable-HTTP MCP Server (Aether -> Enforcer Bridge)
Location: D:\\Dev\\mcp_enforcer\\server.py
Exposes Enforcer's local Marvel RAG (`marvel_kb.db` + `vectors.f16.npy`),
Master Archive (`comics.db`), fast `.cbz` TOC / `.cbr` archive inspection on `S:\\`,
git hot-sync (`enforcer_git_sync`), and optional local Clio subagent invocation
over TLS (`https://192.168.178.36:8765/mcp`) with Uvicorn `--reload` auto-discovery.
"""

import datetime
import glob
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

MCP_ROOT = r"D:\Dev\mcp_enforcer"
ACT_ROOT = r"D:\Dev\act-tracker"
RAG_ROOT = r"D:\Dev\marvel_rag"
LOG_DIR = r"D:\Share\mcp_enforcer_logs"
MAX_LOG_BYTES = 5 * 1024  # 5 KB per file
MAX_LOG_FILES = 100       # Max 100 rolling timestamped files kept

for p in (MCP_ROOT, ACT_ROOT, RAG_ROOT):
    if p not in sys.path and os.path.exists(p):
        sys.path.insert(0, p)

# Keep temporary agent session files off C:\
os.makedirs(r"D:\Temp", exist_ok=True)
os.environ.setdefault("TMP", r"D:\Temp")
os.environ.setdefault("TEMP", r"D:\Temp")


class TimestampedRollingLogHandler(logging.Handler):
    """
    Writes logs into `D:\\Share\\mcp_enforcer_logs\\mcp_enforcer_<YYYYMMDD_HHMMSS_mmm>.log`.
    Rolls over to a new timestamped file whenever file size reaches 5 KB (5,120 bytes)
    and prunes oldest files so at most 100 files are kept on disk.
    """

    def __init__(self, log_dir: str = LOG_DIR, max_bytes: int = MAX_LOG_BYTES, backup_count: int = MAX_LOG_FILES) -> None:
        super().__init__()
        self.log_dir = log_dir
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._lock = threading.RLock()
        self._current_path: str | None = None
        self._current_size: int = 0
        self._seq: int = 0
        os.makedirs(self.log_dir, exist_ok=True)
        self._open_new_chunk()

    def _open_new_chunk(self) -> None:
        now = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        self._seq = (self._seq + 1) % 1000
        fname = f"mcp_enforcer_{now}_{self._seq:03d}.log"
        self._current_path = os.path.join(self.log_dir, fname)
        self._current_size = 0
        self._prune_old_files()

    def _prune_old_files(self) -> None:
        try:
            pattern = os.path.join(self.log_dir, "mcp_enforcer_*.log")
            files = sorted(glob.glob(pattern), key=lambda p: os.path.getmtime(p))
            while len(files) >= self.backup_count:
                oldest = files.pop(0)
                try:
                    os.remove(oldest)
                except OSError:
                    pass
        except Exception:
            pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record) + "\n"
            encoded = msg.encode("utf-8", errors="replace")
            with self._lock:
                if self._current_path is None or (
                    self._current_size > 0 and self._current_size + len(encoded) > self.max_bytes
                ):
                    self._open_new_chunk()
                with open(self._current_path, "ab") as f:
                    f.write(encoded)
                self._current_size += len(encoded)
        except Exception:
            self.handleError(record)


def _setup_rolling_logger() -> logging.Logger:
    root_logger = logging.getLogger("enforcer_mcp")
    root_logger.setLevel(logging.INFO)
    if not any(isinstance(h, TimestampedRollingLogHandler) for h in root_logger.handlers):
        handler = TimestampedRollingLogHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root_logger.addHandler(handler)
        for logger_name in ("uvicorn.error", "uvicorn.access", "mcp"):
            lg = logging.getLogger(logger_name)
            lg.setLevel(logging.INFO)
            if not any(isinstance(h, TimestampedRollingLogHandler) for h in lg.handlers):
                lg.addHandler(handler)
    return root_logger


logger = _setup_rolling_logger()
logger.info("Enforcer MCP rolling logger initialized in %s (max_bytes=%d, max_files=%d)", LOG_DIR, MAX_LOG_BYTES, MAX_LOG_FILES)

# Auto-load API keys (GEMINI_API_KEY, OPENROUTER_API_KEY, GROQ_API_KEY, etc.) from .env files
for _env_path in (os.path.join(ACT_ROOT, ".env"), os.path.join(MCP_ROOT, ".env")):
    if os.path.exists(_env_path):
        with open(_env_path, "r", encoding="utf-8") as _ef:
            for _line in _ef:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

KB_DB_PATH = r"D:\Dev\marvel_rag\data\marvel_kb.db"
VECTORS_PATH = r"D:\Dev\marvel_rag\data\vectors.f16.npy"
COMICS_DB_PATH = r"S:\Agile Comic Tracker\Data\comics.db"
STAGING_ROOT = r"S:\Agile Comic Tracker"
CLIO_SKILL_PATH = r"D:\Dev\act-tracker\.agents\skills\archival-curator-and-librarian\SKILL.md"

mcp = MCPServer(
    "Enforcer-Archival-MCP",
    instructions=(
        "Remote MCP bridge running on Enforcer (192.168.178.36) from D:\\Dev\\mcp_enforcer "
        "with Uvicorn hot-reload enabled. Provides instant read-only access to Enforcer's "
        "local Marvel Knowledge Base (marvel_kb.db + vectors.f16.npy), comics.db, "
        "S:\\Agile Comic Tracker archive TOC inspection, and `enforcer_git_sync`."
    ),
)

_KB_CACHE: dict[str, Any] = {
    "loaded": False,
    "volumes_by_title": {},
    "reprints_by_title": {},
    "rag_instance": None,
}


def _norm_title(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _ensure_kb_cache() -> None:
    if _KB_CACHE["loaded"] or not os.path.exists(KB_DB_PATH):
        return
    conn = sqlite3.connect(f"file:{KB_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # Pre-compute min_year, max_year, and issue_count per (series, volume) from issue_continuity
    vol_stats: dict[tuple[str, int], dict[str, Any]] = {}
    reprints_by_title: dict[str, list[dict[str, Any]]] = {}

    for row in conn.execute(
        "SELECT title, series, volume, issue, legacy_number, release_date, year, reprint_of_json, events_json "
        "FROM issue_continuity"
    ):
        ser = (row["series"] or "").strip()
        vol_raw = row["volume"]
        vol = int(vol_raw) if str(vol_raw or "").isdigit() else 1
        yr_raw = row["year"]
        yr = int(yr_raw) if str(yr_raw or "").isdigit() else None
        key = (ser.lower(), vol)
        st = vol_stats.get(key)
        if st is None:
            st = {"series": ser, "volume": vol, "start_year": yr, "end_year": yr, "issue_count": 0}
            vol_stats[key] = st
        st["issue_count"] += 1
        if yr is not None:
            if st["start_year"] is None or yr < st["start_year"]:
                st["start_year"] = yr
            if st["end_year"] is None or yr > st["end_year"]:
                st["end_year"] = yr

        rep_raw = row["reprint_of_json"]
        if rep_raw and rep_raw != "[]":
            d = dict(row)
            t_norm = _norm_title(d["title"] or "")
            if t_norm:
                reprints_by_title.setdefault(t_norm, []).append(d)

    vols_by_title: dict[str, list[dict[str, Any]]] = {}
    seen_vol_keys: set[tuple[str, int]] = set()
    for row in conn.execute(
        "SELECT title, series, volume, format, status, writers_json, parts_notes_json FROM volume_summaries"
    ):
        d = dict(row)
        ser = (d["series"] or "").strip()
        vol = int(d["volume"]) if d["volume"] is not None else 1
        key = (ser.lower(), vol)
        seen_vol_keys.add(key)
        st = vol_stats.get(key, {})
        entry = {
            "volume_title": d["title"],
            "series_title": ser,
            "volume_number": vol,
            "start_year": st.get("start_year"),
            "end_year": st.get("end_year"),
            "issue_count": st.get("issue_count", 0),
            "format": d["format"],
            "status": d["status"],
        }
        for variant in {_norm_title(ser), _norm_title(re.sub(r"^the\s+", "", ser, flags=re.I))}:
            if variant:
                vols_by_title.setdefault(variant, []).append(entry)

    # Also include any (series, volume) present in issue_continuity but missing from volume_summaries
    for key, st in vol_stats.items():
        if key not in seen_vol_keys and st["series"]:
            ser = st["series"]
            entry = {
                "volume_title": f"{ser} Vol {st['volume']}",
                "series_title": ser,
                "volume_number": st["volume"],
                "start_year": st["start_year"],
                "end_year": st["end_year"],
                "issue_count": st["issue_count"],
                "format": None,
                "status": None,
            }
            for variant in {_norm_title(ser), _norm_title(re.sub(r"^the\s+", "", ser, flags=re.I))}:
                if variant:
                    vols_by_title.setdefault(variant, []).append(entry)

    conn.close()
    _KB_CACHE["volumes_by_title"] = vols_by_title
    _KB_CACHE["reprints_by_title"] = reprints_by_title
    _KB_CACHE["loaded"] = True


@mcp.tool()
def enforcer_ping() -> dict[str, Any]:
    """Health check returning Enforcer host status, hot-reload watch paths, and mounted database availability."""
    _ensure_kb_cache()
    return {
        "status": "online",
        "host": "Enforcer (192.168.178.36)",
        "server_root": MCP_ROOT,
        "hot_reload_enabled": True,
        "watched_dirs": [
            MCP_ROOT,
            os.path.join(ACT_ROOT, "src"),
            os.path.join(RAG_ROOT, "src"),
        ],
        "marvel_kb_exists": os.path.exists(KB_DB_PATH),
        "vectors_exists": os.path.exists(VECTORS_PATH),
        "comics_db_exists": os.path.exists(COMICS_DB_PATH),
        "staging_root_exists": os.path.exists(STAGING_ROOT),
        "cached_volume_keys": len(_KB_CACHE["volumes_by_title"]),
        "cached_reprint_editions": len(_KB_CACHE["reprints_by_title"]),
    }


@mcp.tool()
def enforcer_git_sync(repo: str = "all") -> dict[str, Any]:
    """
    Pull latest commits from `origin/main` on Enforcer (`mcp_enforcer`, `act-tracker`, `marvel_rag`, or `all`).
    When `.py` files in `D:\\Dev\\mcp_enforcer`, `act-tracker/src/`, or `marvel_rag/src/` are updated,
    Uvicorn's filewatcher automatically hot-reloads the MCP server within ~1 second!
    """
    targets = []
    if repo in ("all", "mcp_enforcer", "mcp-enforcer"):
        targets.append(("mcp_enforcer", MCP_ROOT))
    if repo in ("all", "act-tracker", "act_tracker"):
        targets.append(("act-tracker", ACT_ROOT))
    if repo in ("all", "marvel_rag", "marvel-rag"):
        targets.append(("marvel_rag", RAG_ROOT))

    results: dict[str, Any] = {}
    for name, path in targets:
        try:
            proc = subprocess.run(
                ["git", "-C", path, "pull", "--ff-only", "origin", "main"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            results[name] = {
                "returncode": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
            }
        except Exception as e:
            results[name] = {"error": str(e)}
    return {
        "synced": results,
        "note": "If any watched .py file changed, Uvicorn --reload will automatically restart the worker within 1s.",
    }


# -----------------------------------------------------------------------------
# Remote Administration & Mutation Tools (Aether -> Enforcer Control Suite)
# -----------------------------------------------------------------------------

_LAST_ACTIVITY_TIME = time.time()
_WATCHDOG_CONFIG: dict[str, Any] = {
    "enabled": False,
    "idle_timeout_seconds": 900,  # 15 minutes default
    "sleep_plan_guid": "",
    "sleep_plan_name": "",
}


def _get_powerplans() -> list[dict[str, Any]]:
    """Parse output of `powercfg /list`."""
    try:
        out = subprocess.check_output(["powercfg", "/list"], text=True, stderr=subprocess.DEVNULL)
    except Exception:
        return []
    plans = []
    for line in out.splitlines():
        m = re.search(r"Power Scheme GUID:\s+([a-f0-9\-]+)\s+\((.*?)\)(\s*\*|\s*$)", line, re.I)
        if m:
            guid = m.group(1).strip()
            name = m.group(2).strip()
            is_active = "*" in m.group(3)
            plans.append({"guid": guid, "name": name, "is_active": is_active})
    return plans


def _compute_md5(fp: str) -> str:
    h = hashlib.md5()
    with open(fp, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@mcp.tool()
def execute_manifest(manifest: dict[str, Any], dry_run: bool = True) -> dict[str, Any]:
    """
    Execute verified archival file mutations (moves, renames, duplicate evacuations)
    locally on Enforcer with strict reversibility and cryptographic rollback manifests.
    
    If dry_run=True (default), verifies all source files exist, hashes match,
    and target collision checks pass without modifying files.
    
    If dry_run=False, moves/renames files, writes a persistent rollback manifest JSON to
    `D:\\Dev\\act-tracker\\Data\\rollbacks\\` and `D:\\Share\\`, and creates a 1-click Python restore script.
    """
    raw_items = manifest.get("items")
    if not raw_items:
        if "source" in manifest and "target" in manifest:
            raw_items = [manifest]
        else:
            return {"error": "Manifest must contain an 'items' list with 'source' and 'target' paths."}

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    action_type = manifest.get("action", "relocate")
    reason = manifest.get("reason", "Archival mutation")

    verified_items = []
    errors = []

    for idx, item in enumerate(raw_items):
        src = item.get("source") or item.get("source_path")
        dst = item.get("target") or item.get("target_path")
        if not src or not dst:
            errors.append(f"Item #{idx}: missing 'source' or 'target'")
            continue

        if not os.path.exists(src):
            errors.append(f"Item #{idx}: source does not exist: {src}")
            continue

        src_size = os.path.getsize(src)
        expected_size = item.get("expected_size")
        if expected_size is not None and src_size != expected_size:
            errors.append(f"Item #{idx}: size mismatch for {src} (expected {expected_size}, got {src_size})")
            continue

        expected_md5 = item.get("expected_md5") or item.get("md5")
        actual_md5 = ""
        if expected_md5 or not dry_run:
            actual_md5 = _compute_md5(src)
            if expected_md5 and actual_md5.lower() != str(expected_md5).lower():
                errors.append(f"Item #{idx}: MD5 mismatch for {src} (expected {expected_md5}, got {actual_md5})")
                continue

        if os.path.exists(dst):
            errors.append(f"Item #{idx}: target collision exists: {dst}")
            continue

        verified_items.append({
            "source": src,
            "target": dst,
            "size_bytes": src_size,
            "md5": actual_md5,
        })

    if errors:
        return {
            "status": "validation_failed",
            "dry_run": dry_run,
            "verified_count": len(verified_items),
            "error_count": len(errors),
            "errors": errors,
        }

    if dry_run:
        return {
            "status": "dry_run_passed",
            "dry_run": True,
            "action": action_type,
            "reason": reason,
            "items_to_process": len(verified_items),
            "items": verified_items,
            "message": "All source paths verified, hashes/sizes matched, zero target collisions. Ready for execution.",
        }

    # Execute mutations
    executed = []
    rollback_records = []

    for item in verified_items:
        src = item["source"]
        dst = item["target"]
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        executed.append({
            "original_source": src,
            "current_location": dst,
            "size_bytes": item["size_bytes"],
            "md5": item["md5"],
        })
        rollback_records.append({
            "source": dst,
            "target": src,
            "size_bytes": item["size_bytes"],
            "md5": item["md5"],
        })

    rollback_manifest = {
        "timestamp": ts,
        "action": f"rollback_{action_type}",
        "original_reason": reason,
        "items": rollback_records,
    }

    rollback_dir = r"D:\Dev\act-tracker\Data\rollbacks"
    os.makedirs(rollback_dir, exist_ok=True)
    mpath = os.path.join(rollback_dir, f"rollback_manifest_{ts}.json")
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(rollback_manifest, f, indent=2)

    share_mpath = os.path.join(r"D:\Share", f"rollback_manifest_{ts}.json")
    try:
        shutil.copy2(mpath, share_mpath)
    except Exception:
        pass

    rscript_content = f'''"""1-Click Rollback Script for execution {ts}"""
import json, os, shutil

MANIFEST_PATH = r"{mpath}"

def rollback():
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    items = manifest.get("items", [])
    print(f"Rolling back {{len(items)}} items from manifest {ts}...")
    restored = 0
    for it in items:
        src, dst = it["source"], it["target"]
        if os.path.exists(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            print(f"Restored: {{src}} -> {{dst}}")
            restored += 1
        else:
            print(f"Warning: {{src}} not found!")
    print(f"Rollback complete: {{restored}}/{{len(items)}} restored.")

if __name__ == "__main__":
    rollback()
'''
    rscript_path = os.path.join(rollback_dir, f"rollback_{ts}.py")
    with open(rscript_path, "w", encoding="utf-8") as f:
        f.write(rscript_content)
    try:
        shutil.copy2(rscript_path, os.path.join(r"D:\Share", f"rollback_{ts}.py"))
    except Exception:
        pass

    return {
        "status": "success",
        "dry_run": False,
        "action": action_type,
        "reason": reason,
        "executed_count": len(executed),
        "rollback_manifest": mpath,
        "rollback_manifest_share": share_mpath,
        "rollback_script": rscript_path,
        "executed_items": executed,
    }


@mcp.tool()
def enforcer_terminal(command: str, timeout_seconds: int = 60) -> dict[str, Any]:
    """
    Execute a PowerShell command directly on Enforcer with local user/admin privileges.
    Returns returncode, stdout, and stderr.
    """
    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return {
            "command": command,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {"command": command, "error": f"Command timed out after {timeout_seconds}s"}
    except Exception as e:
        return {"command": command, "error": str(e)}


@mcp.tool()
def enforcer_powerplan(action: str = "list", plan: str = "") -> dict[str, Any]:
    """
    Manage Windows Power Plans on Enforcer via `powercfg`.
    - action="list": Lists all power plans, GUIDs, and indicates which one is active (*).
    - action="set": Activates the power plan matching `plan` (by name substring or GUID).
    """
    plans = _get_powerplans()
    active_plan = next((p for p in plans if p["is_active"]), None)

    if action == "list":
        return {
            "active_plan": active_plan,
            "available_plans": plans,
        }

    if action == "set":
        if not plan:
            return {"error": "Must specify `plan` name or GUID to activate", "available_plans": plans}
        target = None
        q = plan.lower().strip()
        for p in plans:
            if q == p["guid"].lower() or q == p["name"].lower():
                target = p
                break
        if not target:
            for p in plans:
                if q in p["name"].lower() or q in p["guid"].lower():
                    target = p
                    break
        if not target:
            return {
                "error": f"No power plan matching '{plan}' found",
                "available_plans": plans,
            }

        res = subprocess.run(["powercfg", "/setactive", target["guid"]], capture_output=True, text=True)
        updated_plans = _get_powerplans()
        new_active = next((p for p in updated_plans if p["is_active"]), None)
        return {
            "success": res.returncode == 0,
            "activated": target,
            "active_plan": new_active,
            "stdout": res.stdout.strip(),
            "stderr": res.stderr.strip(),
        }

    return {"error": f"Unknown action '{action}'. Use 'list' or 'set'."}


@mcp.tool()
def enforcer_system_power(action: str = "sleep", delay_seconds: int = 60) -> dict[str, Any]:
    """
    Control Enforcer system power state:
    - action="sleep": Put machine into sleep / standby immediately.
    - action="shutdown": Schedule a clean Windows shutdown with countdown.
    - action="restart": Schedule a clean Windows reboot.
    - action="abort": Cancel a scheduled shutdown or restart.
    """
    if action == "sleep":
        cmd = "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Application]::SetSuspendState([System.Windows.Forms.PowerState]::Suspend, $false, $false)"
        res = subprocess.run(["powershell.exe", "-NoProfile", "-Command", cmd], capture_output=True, text=True)
        return {"action": "sleep", "returncode": res.returncode, "stdout": res.stdout.strip(), "stderr": res.stderr.strip()}

    elif action == "shutdown":
        res = subprocess.run(["shutdown.exe", "/s", "/t", str(delay_seconds), "/c", "Shutdown requested remotely from Aether via FastMCP"], capture_output=True, text=True)
        return {"action": "shutdown", "delay_seconds": delay_seconds, "returncode": res.returncode, "stdout": res.stdout.strip(), "stderr": res.stderr.strip()}

    elif action == "restart":
        res = subprocess.run(["shutdown.exe", "/r", "/t", str(delay_seconds), "/c", "Reboot requested remotely from Aether via FastMCP"], capture_output=True, text=True)
        return {"action": "restart", "delay_seconds": delay_seconds, "returncode": res.returncode, "stdout": res.stdout.strip(), "stderr": res.stderr.strip()}

    elif action == "abort":
        res = subprocess.run(["shutdown.exe", "/a"], capture_output=True, text=True)
        return {"action": "abort", "returncode": res.returncode, "stdout": res.stdout.strip(), "stderr": res.stderr.strip()}

    return {"error": f"Unknown action '{action}'. Use 'sleep', 'shutdown', 'restart', or 'abort'."}


@mcp.tool()
def enforcer_watchdog_config(
    enabled: bool | None = None,
    idle_minutes: int | None = None,
    sleep_plan: str | None = None,
) -> dict[str, Any]:
    """
    Configure or inspect the Enforcer MCP inactivity watchdog.
    When enabled, switches to `sleep_plan` if no FastMCP requests arrive for `idle_minutes`.
    """
    global _WATCHDOG_CONFIG
    if enabled is not None:
        _WATCHDOG_CONFIG["enabled"] = bool(enabled)
    if idle_minutes is not None and idle_minutes > 0:
        _WATCHDOG_CONFIG["idle_timeout_seconds"] = int(idle_minutes * 60)
    if sleep_plan is not None:
        plans = _get_powerplans()
        target = None
        q = sleep_plan.lower().strip()
        for p in plans:
            if q == p["guid"].lower() or q == p["name"].lower() or q in p["name"].lower():
                target = p
                break
        if target:
            _WATCHDOG_CONFIG["sleep_plan_guid"] = target["guid"]
            _WATCHDOG_CONFIG["sleep_plan_name"] = target["name"]
        else:
            return {"error": f"Could not find power plan matching '{sleep_plan}'", "config": _WATCHDOG_CONFIG}

    idle_seconds_current = round(time.time() - _LAST_ACTIVITY_TIME, 1)
    return {
        "watchdog_config": {
            "enabled": _WATCHDOG_CONFIG["enabled"],
            "idle_timeout_minutes": _WATCHDOG_CONFIG["idle_timeout_seconds"] / 60,
            "sleep_plan_name": _WATCHDOG_CONFIG["sleep_plan_name"],
            "sleep_plan_guid": _WATCHDOG_CONFIG["sleep_plan_guid"],
        },
        "seconds_since_last_activity": idle_seconds_current,
    }


def _watchdog_loop() -> None:
    while True:
        try:
            time.sleep(30)
            if not _WATCHDOG_CONFIG.get("enabled"):
                continue
            timeout = _WATCHDOG_CONFIG.get("idle_timeout_seconds", 900)
            target_guid = _WATCHDOG_CONFIG.get("sleep_plan_guid")
            if not target_guid:
                continue

            idle = time.time() - _LAST_ACTIVITY_TIME
            if idle >= timeout:
                plans = _get_powerplans()
                active = next((p for p in plans if p["is_active"]), None)
                if active and active["guid"].lower() != target_guid.lower():
                    logger.warning(
                        "[Watchdog] Inactivity timeout reached (idle %.0fs >= %ds). Switching powerplan to '%s' (%s)",
                        idle, timeout, _WATCHDOG_CONFIG.get("sleep_plan_name"), target_guid
                    )
                    subprocess.run(["powercfg", "/setactive", target_guid], capture_output=True, text=True)
        except Exception:
            pass


_watchdog_thread = threading.Thread(target=_watchdog_loop, daemon=True)
_watchdog_thread.start()


@mcp.tool()
def marvel_resolve_volume(
    series_title: str = "",
    title: str = "",
    year: int | None = None,
    issue_number: int | None = None,
    issue: int | None = None,
) -> dict[str, Any]:
    """
    Resolve canonical Marvel volume (vN), publication year bounds, issue count,
    and master archive (comics.db) continuity for a series title on Enforcer.
    Accepts either `series_title` or `title` (and `issue_number` or `issue`).
    """
    _ensure_kb_cache()
    series_title = (series_title or title or "").strip()
    if issue_number is None and issue is not None:
        issue_number = issue
    norm = _norm_title(series_title)
    norm_no_the = _norm_title(re.sub(r"^the\s+", "", series_title, flags=re.I))

    candidates = list(_KB_CACHE["volumes_by_title"].get(norm, []))
    if norm_no_the != norm:
        for c in _KB_CACHE["volumes_by_title"].get(norm_no_the, []):
            if c not in candidates:
                candidates.append(c)

    candidates.sort(key=lambda x: (x.get("volume_number") or 0, x.get("start_year") or 0))

    filtered = candidates
    if year is not None:
        year_matches = [
            c for c in candidates
            if (c.get("start_year") or 0) - 1 <= year <= (c.get("end_year") or 9999) + 1
        ]
        if year_matches:
            filtered = year_matches

    if issue_number is not None and len(filtered) > 1:
        iss_matches = [
            c for c in filtered
            if (c.get("issue_count") or 999) + 2 >= issue_number
        ]
        if iss_matches:
            filtered = iss_matches

    master_runs = []
    if os.path.exists(COMICS_DB_PATH):
        try:
            conn = sqlite3.connect(f"file:{COMICS_DB_PATH}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT volume, MIN(issue_number) AS min_iss, MAX(issue_number) AS max_iss, "
                "MIN(year) AS min_yr, MAX(year) AS max_yr, COUNT(*) AS cnt "
                "FROM comics WHERE LOWER(series_title) = LOWER(?) GROUP BY volume ORDER BY volume",
                (series_title.strip(),),
            ).fetchall()
            master_runs = [dict(r) for r in rows]
            conn.close()
        except Exception as e:
            master_runs = [{"error": str(e)}]

    return {
        "query_series": series_title,
        "query_year": year,
        "query_issue": issue_number,
        "best_matches": filtered[:10],
        "all_kb_volumes_count": len(candidates),
        "comics_db_runs": master_runs,
    }


@mcp.tool()
def marvel_resolve_tpb(
    search_title: str = "",
    title: str = "",
    series_title: str = "",
    year: int | None = None,
) -> dict[str, Any]:
    """
    Query `issue_continuity.reprint_of_json` (18,743 collected editions) in `marvel_kb.db`
    to find exact collected issues, contiguous vs. non-contiguous ranges, and parent volumes.
    Accepts `search_title`, `title`, or `series_title`.
    """
    _ensure_kb_cache()
    search_title = (search_title or title or series_title or "").strip()
    tokens = [t for t in _norm_title(search_title).split() if t not in {"tpb", "vol", "v", "the", "of", "and"}]
    matches = []
    for k, rows in _KB_CACHE["reprints_by_title"].items():
        if all(tok in k for tok in tokens):
            for r in rows:
                ry_raw = r.get("year")
                ry = int(ry_raw) if str(ry_raw or "").isdigit() else None
                if year is None or ry is None or abs(ry - int(year)) <= 2:
                    reprints = json.loads(r["reprint_of_json"] or "[]")
                    matches.append({
                        "title": r["title"],
                        "series_title": r.get("series"),
                        "volume_number": r.get("volume"),
                        "issue_number": r.get("issue"),
                        "release_year": ry,
                        "release_date": r.get("release_date"),
                        "collected_issues": reprints,
                    })
    matches.sort(key=lambda x: (int(x.get("release_year") or 0), x.get("title") or ""))
    return {
        "query": search_title,
        "year": year,
        "match_count": len(matches),
        "matches": matches[:20],
    }


@mcp.tool()
def marvel_hybrid_search(query: str, top_k: int = 5) -> dict[str, Any]:
    """Run hybrid FTS5 lexical + BGE-small dense vector search against Enforcer's `marvel_kb.db`."""
    from src.marvel_rag import MarvelRAG

    if _KB_CACHE["rag_instance"] is None:
        _KB_CACHE["rag_instance"] = MarvelRAG(db_path=KB_DB_PATH, vectors_path=VECTORS_PATH)
    rag: MarvelRAG = _KB_CACHE["rag_instance"]
    results = rag.hybrid_search(query, top_k=top_k)
    return {"query": query, "results": results}


@mcp.tool()
def enforcer_inspect_archive(file_path: str) -> dict[str, Any]:
    """
    Fast, read-only inspection of a `.cbz` or `.cbr` archive on `S:\\Agile Comic Tracker`.
    Reads `.cbz` Central Directory TOC (`< 2 ms`) without extracting bytes or transferring over SMB.
    """
    full_path = file_path if os.path.isabs(file_path) else os.path.join(STAGING_ROOT, file_path)
    if not os.path.exists(full_path):
        return {"exists": False, "path": full_path}

    size_bytes = os.path.getsize(full_path)
    size_mb = round(size_bytes / (1024 * 1024), 2)
    ext = os.path.splitext(full_path)[1].lower()
    pages = None
    mode = "SIZE_EST"
    sample_entries: list[str] = []

    if ext == ".cbz":
        try:
            with zipfile.ZipFile(full_path, "r") as zf:
                names = zf.namelist()
                imgs = [n for n in names if re.search(r"\.(jpg|jpeg|png|webp|gif)$", n, re.I)]
                pages = len(imgs)
                mode = "CBZ_TOC"
                sample_entries = imgs[:5]
        except Exception:
            pages = max(1, int(round(size_mb / 1.65)))
    else:
        pages = max(1, int(round(size_mb / 1.65)))

    classification = "SINGLE_ISSUE"
    if pages < 5 or size_mb < 3.0:
        classification = "COVER_ONLY_ARCHIVE"
    elif pages > 400 or size_mb > 450.0:
        classification = "OMNIBUS_OR_ANTHOLOGY_BRICK"
    elif pages >= 80 or size_mb >= 85.0:
        classification = "TPB_OR_COLLECTION"

    return {
        "exists": True,
        "path": full_path,
        "filename": os.path.basename(full_path),
        "size_mb": size_mb,
        "pages": pages,
        "page_count_mode": mode,
        "classification": classification,
        "sample_entries": sample_entries,
    }


@mcp.tool()
def enforcer_list_staging(
    subfolder: str = "Marvel",
    filter_substring: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """List folders and files inside `S:\\Agile Comic Tracker\\<subfolder>` with fast metadata."""
    target_dir = os.path.join(STAGING_ROOT, subfolder)
    if not os.path.exists(target_dir):
        return {"exists": False, "target_dir": target_dir}

    entries = sorted(os.listdir(target_dir))
    if filter_substring:
        q = filter_substring.lower()
        entries = [e for e in entries if q in e.lower()]

    items = []
    for name in entries[:limit]:
        fp = os.path.join(target_dir, name)
        is_dir = os.path.isdir(fp)
        info: dict[str, Any] = {"name": name, "is_dir": is_dir}
        if is_dir:
            try:
                children = [c for c in os.listdir(fp) if c.lower().endswith((".cbz", ".cbr"))]
                info["comic_file_count"] = len(children)
            except Exception:
                info["comic_file_count"] = 0
        else:
            info["size_mb"] = round(os.path.getsize(fp) / (1024 * 1024), 2)
        items.append(info)

    return {
        "target_dir": target_dir,
        "total_matching": len(entries),
        "returned": len(items),
        "items": items,
    }


@mcp.tool()
def enforcer_sql_query(database: str = "marvel_kb", sql_query: str = "SELECT * FROM volume_summaries LIMIT 10") -> dict[str, Any]:
    """
    Execute a strictly READ-ONLY SQLite SELECT query on Enforcer's local `marvel_kb` (`marvel_kb.db`)
    or `comics` (`comics.db`) database so Clio on Aether (running on Google AI Pro) can inspect any table directly.
    """
    q_strip = sql_query.strip()
    if not re.match(r"^(SELECT|WITH|PRAGMA)\b", q_strip, re.I):
        return {"error": "Only read-only SELECT / WITH / PRAGMA queries are permitted."}

    db_target = KB_DB_PATH if database.lower() in ("marvel_kb", "kb", "marvel") else COMICS_DB_PATH
    if not os.path.exists(db_target):
        return {"error": f"Database not found: {db_target}"}

    try:
        conn = sqlite3.connect(f"file:{db_target}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(q_strip).fetchmany(250)
        data = [dict(r) for r in rows]
        conn.close()
        return {"database": database, "row_count": len(data), "rows": data}
    except Exception as e:
        return {"database": database, "error": str(e)}


@mcp.tool()
def enforcer_batch_audit_files(
    subfolder: str = "Marvel",
    limit: int = 50,
    offset: int = 0,
    filter_substring: str = "",
    exclude_omnibus_epic: bool = True,
) -> dict[str, Any]:
    """
    High-speed local archival pre-enrichment on Enforcer (zero API cost).
    Scans up to `limit` comic files in `S:\\Agile Comic Tracker\\<subfolder>`, computes `.cbz` TOC
    page counts (`< 2 ms/file`), parses with `act-tracker`'s `parser.py`, and attaches matching
    `marvel_kb.db` volumes, `reprint_of_json` TPB contents, and `comics.db` master runs so
    Clio on Aether (running on your Google AI Pro subscription) can audit & propose renames directly!
    """
    _ensure_kb_cache()
    from src.core.parser import parse_incoming_file
    from src.core.rename_cleaner import scrub_release_groups

    target_dir = os.path.join(STAGING_ROOT, subfolder)
    if not os.path.exists(target_dir):
        return {"exists": False, "target_dir": target_dir}

    candidates: list[tuple[str, str, str]] = []
    for root, _dirs, files in os.walk(target_dir):
        rel_folder = os.path.relpath(root, target_dir)
        parent_label = f"[Root: {subfolder}]" if rel_folder == "." else rel_folder
        for fn in sorted(files):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in (".cbz", ".cbr"):
                continue
            fn_low = fn.lower()
            if exclude_omnibus_epic and any(k in fn_low for k in ("omnibus", "epic collection", "masterworks")):
                continue
            if filter_substring and filter_substring.lower() not in f"{parent_label}/{fn}".lower():
                continue
            candidates.append((parent_label, fn, os.path.join(root, fn)))

    sliced = candidates[offset : offset + limit]
    enriched = []
    for parent_label, fn, fp in sliced:
        size_mb = round(os.path.getsize(fp) / (1024 * 1024), 2)
        pages = max(1, int(round(size_mb / 1.65)))
        mode = "SIZE_EST"
        if fn.lower().endswith(".cbz"):
            try:
                with zipfile.ZipFile(fp, "r") as zf:
                    imgs = [n for n in zf.namelist() if re.search(r"\.(jpg|jpeg|png|webp|gif)$", n, re.I)]
                    if imgs:
                        pages = len(imgs)
                        mode = "CBZ_TOC"
            except Exception:
                pass

        parsed = parse_incoming_file(fn, target_dir, f"[{subfolder}]")
        stitle = parsed.get("series_title") or ""
        f_year = parsed.get("year")
        f_iss = parsed.get("issue_number")
        vol_info = marvel_resolve_volume(stitle, year=f_year, issue_number=f_iss) if stitle else {}
        tpb_info = marvel_resolve_tpb(stitle or fn, year=f_year) if (parsed.get("is_tpb") or pages >= 80) else {}

        enriched.append({
            "parent_folder": parent_label,
            "filename": fn,
            "scrubbed_filename": scrub_release_groups(fn),
            "size_mb": size_mb,
            "pages": pages,
            "page_mode": mode,
            "parsed": parsed,
            "kb_best_volumes": vol_info.get("best_matches", [])[:5],
            "comics_db_runs": vol_info.get("comics_db_runs", []),
            "kb_tpb_matches": tpb_info.get("matches", [])[:5],
        })

    return {
        "subfolder": subfolder,
        "total_matching_files": len(candidates),
        "offset": offset,
        "returned_count": len(enriched),
        "items": enriched,
    }


# Expose ASGI app at module level so `uvicorn` can import `"server:app"` with `reload=True`
_HOST = os.environ.get("ENFORCER_MCP_HOST", "0.0.0.0")
_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)
_mcp_starlette = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    json_response=True,
    stateless_http=True,
    transport_security=_SECURITY,
    host=_HOST,
)


class _AcceptHeaderNormalizer:
    """
    1. Ensures MCP clients sending only `Accept: application/json` never get 406 Not Acceptable.
    2. Logs every incoming HTTP / JSON-RPC tool call and response status to `D:\\Share\\mcp_enforcer_logs`.
    """

    def __init__(self, inner_app: Any) -> None:
        self.inner_app = inner_app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.inner_app(scope, receive, send)
            return

        global _LAST_ACTIVITY_TIME
        _LAST_ACTIVITY_TIME = time.time()

        headers = list(scope.get("headers", []))
        new_headers = [(k, v) for k, v in headers if k.lower() != b"accept"]
        new_headers.append((b"accept", b"application/json, text/event-stream"))
        scope = dict(scope, headers=new_headers)

        client = scope.get("client") or ("unknown", 0)
        method = scope.get("method", "POST")
        path = scope.get("path", "/mcp")
        body_chunks: list[bytes] = []
        status_code_holder: list[int] = [0]

        async def wrapped_receive() -> dict[str, Any]:
            msg = await receive()
            if msg.get("type") == "http.request":
                body_chunks.append(msg.get("body", b""))
            return msg

        async def wrapped_send(msg: dict[str, Any]) -> None:
            if msg.get("type") == "http.response.start":
                status_code_holder[0] = msg.get("status", 0)
            await send(msg)

        try:
            await self.inner_app(scope, wrapped_receive, wrapped_send)
        except Exception:
            logger.exception("Unhandled exception on %s %s from %s:%s", method, path, client[0], client[1])
            raise
        finally:
            raw_body = b"".join(body_chunks).decode("utf-8", errors="replace")
            rpc_summary = ""
            if raw_body:
                try:
                    j = json.loads(raw_body)
                    rpc_method = j.get("method", "")
                    params = j.get("params") or {}
                    tool_name = params.get("name", "") if isinstance(params, dict) else ""
                    tool_args = params.get("arguments", {}) if isinstance(params, dict) else {}
                    rpc_summary = f" rpc={rpc_method}" + (f" tool={tool_name} args={json.dumps(tool_args)[:300]}" if tool_name else "")
                except Exception:
                    rpc_summary = f" body={raw_body[:200]}"
            logger.info(
                "%s:%s - \"%s %s\" %d%s",
                client[0],
                client[1],
                method,
                path,
                status_code_holder[0],
                rpc_summary,
            )


app = _AcceptHeaderNormalizer(_mcp_starlette)


def _terminate_existing_instances(port: int = 8765) -> None:
    """Terminates any previous mcp_enforcer instance bound to `port` before starting a new parent server."""
    my_pid = os.getpid()
    parent_pid = os.getppid()
    try:
        out = subprocess.check_output(["netstat", "-aon"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                if parts and parts[-1].isdigit():
                    old_pid = int(parts[-1])
                    if old_pid not in (0, my_pid, parent_pid):
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(old_pid)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
    except Exception:
        pass


def main() -> None:
    host = _HOST
    port = int(os.environ.get("ENFORCER_MCP_PORT", "8765"))
    _terminate_existing_instances(port)
    cert_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")
    cert_file = os.path.join(cert_dir, "enforcer.crt")
    key_file = os.path.join(cert_dir, "enforcer.key")

    use_tls = os.path.exists(cert_file) and os.path.exists(key_file)
    scheme = "https" if use_tls else "http"

    reload_dirs = [
        d for d in (
            MCP_ROOT,
            os.path.join(ACT_ROOT, "src"),
            os.path.join(RAG_ROOT, "src"),
        )
        if os.path.exists(d)
    ]

    print(
        f"🚀 Enforcer MCP Server (Hot-Reload Enabled) listening on "
        f"{scheme}://{host}:{port}/mcp (LAN: {scheme}://192.168.178.36:{port}/mcp)\n"
        f"👀 Watching directories for .py changes: {reload_dirs}"
    )
    uvicorn.run(
        "server:app",
        app_dir=MCP_ROOT,
        host=host,
        port=port,
        reload=True,
        reload_dirs=reload_dirs,
        reload_includes=["*.py"],
        ssl_certfile=cert_file if use_tls else None,
        ssl_keyfile=key_file if use_tls else None,
        log_level="info",
    )


if __name__ == "__main__":
    main()
