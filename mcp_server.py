#!/usr/bin/env python3
"""Minimal stdio MCP server exposing web-search's search.py as a `web_search` tool.

Why this exists
---------------
Some agent runtimes (e.g. Codex's ``exec_command`` sandbox) cannot launch
Playwright/Chromium, so running ``search.py`` directly inside the sandbox hangs
at the first browser channel.  MCP servers, however, are started by the host as
independent processes OUTSIDE that per-command sandbox -- so the browser channels
(Google/Bing/Baidu/Zhihu) work normally here and no channel has to be skipped.

The server shells out to ``search.py`` as a subprocess (rather than importing it)
so that (1) it reuses the exact, already-tested CLI behaviour, and (2) search.py's
stdout can never corrupt this process's JSON-RPC stdio stream.

Protocol
--------
JSON-RPC 2.0 over newline-delimited JSON on stdin/stdout (the MCP stdio
transport).  Only the Python standard library is used -- no third-party deps,
so the project's pinned venv/uv.lock stay untouched.
"""
import sys
import os
import json
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_INFO = {"name": "web-search", "version": "22.0.0"}
DEFAULT_PROTOCOL = "2024-11-05"


def _python_exe() -> str:
    """Prefer the project-local venv interpreter; fall back to the current one."""
    for rel in (("venv", "Scripts", "python.exe"), ("venv", "bin", "python")):
        cand = os.path.join(HERE, *rel)
        if os.path.exists(cand):
            return cand
    return sys.executable


TOOL = {
    "name": "web_search",
    "description": (
        "Multi-channel web research. Aggregates Google, Bing, Baidu, Zhihu, arXiv, "
        "Crossref, OpenAlex, GitHub, Hacker News, Stack Overflow, CSDN, V2EX and "
        "Juejin in one pass, deep-reads the top pages, and returns a merged, "
        "deduped, freshness-aware result set as JSON. Use for ANY web search or "
        "online research that needs current, corroborated, multi-source evidence. "
        "Start with max_iter=1; only set fresh=true for time-sensitive queries."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Base search query (always sent to Google/Bing/Baidu).",
            },
            "limit": {
                "type": "integer",
                "description": "Per-channel result target (default 8).",
                "default": 8,
            },
            "max_iter": {
                "type": "integer",
                "description": "Model-review rounds (default 1). Each extra round is a full live pass (~70-100s).",
                "default": 1,
            },
            "fresh": {
                "type": "boolean",
                "description": "Force live refresh. Only for time-sensitive queries (news/prices). Default false.",
                "default": False,
            },
            "summary": {
                "type": "boolean",
                "description": "Emit the compact review_packet instead of the full evidence set (default true).",
                "default": True,
            },
            "query_en": {
                "type": "string",
                "description": "English translation for English-indexed channels (optional).",
            },
            "query_zh": {
                "type": "string",
                "description": "Chinese translation for Chinese community channels (optional).",
            },
            "review_queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Targeted follow-up queries; needs max_iter >= 1 + len(review_queries).",
            },
        },
        "required": ["query"],
    },
}


def _run_search(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    cmd = [
        _python_exe(), os.path.join(HERE, "search.py"), query,
        "--limit", str(int(args.get("limit", 8))),
        "--max-iter", str(int(args.get("max_iter", 1))),
    ]
    if args.get("summary", True):
        cmd.append("--summary")
    if args.get("fresh"):
        cmd.append("--fresh")
    if args.get("query_en"):
        cmd += ["--query-en", str(args["query_en"])]
    if args.get("query_zh"):
        cmd += ["--query-zh", str(args["query_zh"])]
    for rq in (args.get("review_queries") or []):
        cmd += ["--review-query", str(rq)]

    env = dict(os.environ)
    # Chromium auto-discovers from the project-local browsers/ dir.
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(HERE, "browsers"))
    timeout = int(os.environ.get("WEB_SEARCH_MCP_TIMEOUT", "600"))
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=HERE, env=env, timeout=timeout,
    )
    out = proc.stdout or ""
    if not out.strip():
        raise RuntimeError(
            f"search.py produced no output (exit {proc.returncode}); "
            f"stderr: {(proc.stderr or '')[:800]}"
        )
    return out


# --------------------------------------------------------------------------
# JSON-RPC 2.0 / MCP plumbing
# --------------------------------------------------------------------------
def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(id_, result) -> None:
    _send({"jsonrpc": "2.0", "id": id_, "result": result})


def _error(id_, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


def _handle(msg: dict) -> None:
    if not isinstance(msg, dict):
        return
    method = msg.get("method")
    id_ = msg.get("id")
    is_request = "id" in msg and id_ is not None

    if method == "initialize":
        proto = (msg.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL
        _result(id_, {
            "protocolVersion": proto,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })
    elif method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        pass  # notifications: no reply
    elif method == "ping":
        _result(id_, {})
    elif method == "tools/list":
        _result(id_, {"tools": [TOOL]})
    elif method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name != "web_search":
            _error(id_, -32602, f"Unknown tool: {name}")
            return
        try:
            text = _run_search(arguments)
            _result(id_, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:  # noqa: BLE001 - report any failure to the client
            _result(id_, {"content": [{"type": "text", "text": f"web_search failed: {exc}"}],
                          "isError": True})
    else:
        if is_request:
            _error(id_, -32601, f"Method not found: {method}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, list):  # JSON-RPC batch
            for m in msg:
                _handle(m)
        else:
            _handle(msg)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
