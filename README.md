# Enforcer Streamable-HTTP MCP Server (`Aether -> Enforcer`)

Exposes **Enforcer's** local Marvel Knowledge Base (`marvel_kb.db` + `vectors.f16.npy`), Master Archive (`comics.db`), fast `.cbz` TOC / `.cbr` archive inspection on `S:\Agile Comic Tracker`, and optional local **Clio** subagent execution over TLS (`https://192.168.178.36:8765/mcp`).

> **Security Note:** All TLS certificates and private keys (`certs/`, `*.crt`, `*.key`, `*.pem`) are strictly excluded via `.gitignore` and never committed to git. The public certificate (`enforcer.crt`) is exported to `D:\Share\enforcer.crt` for importing on **Aether**.

---

## 1. Starting the Server on `Enforcer`

1. **(One-time) Generate the 100-Year TLS Certificate & Key:**
   ```powershell
   D:\Dev\marvel_rag\.venv\Scripts\python.exe D:\Dev\act-tracker\mcp_enforcer\generate_cert.py
   ```
   - Writes `mcp_enforcer/certs/enforcer.crt` & `mcp_enforcer/certs/enforcer.key` (gitignored).
   - Automatically copies `enforcer.crt` to `D:\Share\enforcer.crt`.

2. **Launch the HTTPS MCP Server on Port `8765`:**
   ```powershell
   D:\Dev\marvel_rag\.venv\Scripts\python.exe D:\Dev\act-tracker\mcp_enforcer\server.py
   ```
   - Endpoint: `https://192.168.178.36:8765/mcp`

---

## 2. Connecting from `Aether`

1. **Trust `enforcer.crt` on `Aether` (One-time PowerShell command on Aether):**
   Copy `D:\Share\enforcer.crt` to **Aether** and import it into your Windows CurrentUser Trusted Root CA store:
   ```powershell
   Import-Certificate -FilePath "C:\path\to\enforcer.crt" -CertStoreLocation Cert:\CurrentUser\Root
   ```

2. **Add to `~/.gemini/config/mcp_config.json` on `Aether`:**
   ```json
   {
     "mcpServers": {
       "enforcer-mcp": {
         "url": "https://192.168.178.36:8765/mcp"
       }
     }
   }
   ```

---

## 3. Exposed MCP Tools

- **`enforcer_ping()`** — Health check & mounted database/storage verification + RAM cache counts.
- **`marvel_resolve_volume(series_title, year=None, issue_number=None)`** — Resolves canonical `vN` volume numbers, year spans, and issue counts across `marvel_kb.db` (`volume_summaries`) and `comics.db`.
- **`marvel_resolve_tpb(search_title, year=None)`** — Queries the 18,743 `reprint_of_json` records in `marvel_kb.db` to return exact collected issues and contiguous vs. non-contiguous ranges.
- **`marvel_hybrid_search(query, top_k=5)`** — Runs hybrid FTS5 lexical + BGE-small dense vector search (`vectors.f16.npy`) on Enforcer.
- **`enforcer_inspect_archive(file_path)`** — Reads `.cbz` Central Directory TOC page counts (`< 2 ms`) and file size directly on `S:\Agile Comic Tracker` without SMB transfer.
- **`enforcer_list_staging(subfolder="Marvel", filter_substring="", limit=100)`** — Lists set folders and files in `S:\Agile Comic Tracker\<subfolder>`.
- **`enforcer_batch_audit_files(subfolder="Marvel", limit=50, offset=0, filter_substring="", exclude_omnibus_epic=True)`** — Zero-API-cost batch enrichment: walks `S:\Agile Comic Tracker\<subfolder>`, computes `.cbz` TOC page counts, runs `act-tracker`'s `parser.py` + `scrub_release_groups()`, and attaches `marvel_kb.db` volumes, `reprint_of_json` TPB contents, and `comics.db` runs for Clio on **Aether** (running on Google AI Pro).
- **`enforcer_sql_query(database="marvel_kb", sql_query="SELECT ...")`** — Executes read-only `SELECT` queries against Enforcer's `marvel_kb.db` or `comics.db`.
- **`enforcer_git_sync(repo="all")`** — Pulls latest commits from `origin/main` on Enforcer so Uvicorn `--reload` hot-reloads any `.py` changes.

