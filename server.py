"""
Enforcer Streamable-HTTP MCP Server (Aether -> Enforcer Bridge)
Location: D:\\Dev\\mcp_enforcer\\server.py
Exposes Enforcer's local Marvel RAG (`marvel_kb.db` + `vectors.f16.npy`),
Master Archive (`comics.db`), fast `.cbz` TOC / `.cbr` archive inspection on `S:\\`,
git hot-sync (`enforcer_git_sync`), and optional local Clio subagent invocation
over TLS (`https://192.168.178.36:8765/mcp`) with Uvicorn `--reload` auto-discovery.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import zipfile
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

MCP_ROOT = r"D:\Dev\mcp_enforcer"
ACT_ROOT = r"D:\Dev\act-tracker"
RAG_ROOT = r"D:\Dev\marvel_rag"
for p in (MCP_ROOT, ACT_ROOT, RAG_ROOT):
    if p not in sys.path and os.path.exists(p):
        sys.path.insert(0, p)

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


@mcp.tool()
def marvel_resolve_volume(
    series_title: str,
    year: int | None = None,
    issue_number: int | None = None,
) -> dict[str, Any]:
    """
    Resolve canonical Marvel volume (vN), publication year bounds, issue count,
    and master archive (comics.db) continuity for a series title on Enforcer.
    """
    _ensure_kb_cache()
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
def marvel_resolve_tpb(search_title: str, year: int | None = None) -> dict[str, Any]:
    """
    Query `issue_continuity.reprint_of_json` (18,743 collected editions) in `marvel_kb.db`
    to find exact collected issues, contiguous vs. non-contiguous ranges, and parent volumes.
    """
    _ensure_kb_cache()
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
async def ask_clio_on_enforcer(task_prompt: str) -> str:
    """
    Spawn an autonomous read-only Clio subagent locally on Enforcer via `google.antigravity` SDK.
    """
    from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig

    sys_prompt = "You are Clio, Muse of History and Archival Curator. Strictly read-only."
    if os.path.exists(CLIO_SKILL_PATH):
        with open(CLIO_SKILL_PATH, "r", encoding="utf-8") as f:
            sys_prompt = f.read()

    config = LocalAgentConfig(
        system_instructions=sys_prompt,
        capabilities=CapabilitiesConfig(),
    )
    chunks: list[str] = []
    async with Agent(config) as clio:
        response = await clio.chat(task_prompt)
        async for token in response:
            chunks.append(token)
    return "".join(chunks)


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
    """Ensures MCP clients sending only `Accept: application/json` (or no Accept header) never get 406 Not Acceptable."""

    def __init__(self, inner_app: Any) -> None:
        self.inner_app = inner_app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            headers = list(scope.get("headers", []))
            new_headers = [(k, v) for k, v in headers if k.lower() != b"accept"]
            new_headers.append((b"accept", b"application/json, text/event-stream"))
            scope = dict(scope, headers=new_headers)
        await self.inner_app(scope, receive, send)


app = _AcceptHeaderNormalizer(_mcp_starlette)


def main() -> None:
    host = _HOST
    port = int(os.environ.get("ENFORCER_MCP_PORT", "8765"))
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
