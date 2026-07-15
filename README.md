# web-search Skill v22

Multi-channel web research that aggregates every enabled source instead of treating sources as fallbacks. No proxy support is included.

## Channels

- General web: Google, Bing, Baidu (all run for every query round)
- Community: Zhihu (through Sogou's zhihu vertical), CSDN, Stack Overflow, V2EX, Juejin, Hacker News
- Academic: arXiv, Crossref, OpenAlex, DBLP (CS bibliography), PubMed (biomedical, via NCBI E-utilities); Semantic Scholar joins when `SEMANTIC_SCHOLAR_API_KEY` is set
- Reference: Wikipedia (routes to the Chinese wiki automatically for CJK queries)
- Code: GitHub
- Reddit joins when `REDDIT_ACCESS_TOKEN` is set (anonymous search endpoints are blocked)
- Direct discovery: official pages, canonical/next/feed links, PDFs, attachments, and bounded related-link traversal

DBLP, PubMed, and Wikipedia are free, keyless APIs; `WEB_SEARCH_CONTACT` (an email) is optional but recommended for Wikipedia and PubMed -- it identifies this tool as a good-citizen client under their respective robot/rate-limit policies instead of a generic anonymous one. `NCBI_API_KEY` is optional and raises PubMed's rate ceiling the same way `GITHUB_TOKEN`/`OPENALEX_API_KEY` do for their channels.

Each browser engine owns an independent fingerprint/cooldown state. CAPTCHA, verification, 403, or 429 responses cool that engine/profile pair and retry with another profile while every other channel continues normally. Browser channels stay sequential and each one targets a distinct host (Google/Bing/Baidu/Sogou); API channels run concurrently alongside them.

## Stealth and anti-detection (no proxy)

Browser SERP channels are hardened to survive risk-control on a plain residential IP, with no proxy layer. The target failure mode is the *soft challenge* (CAPTCHA / 429 / "unusual traffic" interstitial) that shared consumer IPs actually receive — not the permanent IP ban providers avoid, since a residential IP is shared by many real users. So the aim is resilience and quick recovery, driven by fingerprint + behavior + rate discipline rather than IP rotation.

Every browser context receives, through a single injected init script kept internally consistent with its rotated fingerprint:

- `navigator.webdriver` removed, and Chromium's default `--enable-automation` switch stripped
- `navigator.languages`/`language` matched to the profile locale, plus a descending-`q` `Accept-Language` header
- realistic `navigator.plugins`/`mimeTypes` (headless otherwise reports zero), `hardwareConcurrency`, `deviceMemory`, and `window.chrome.runtime`
- WebGL `UNMASKED_VENDOR`/`RENDERER` spoofed to the profile's GPU, so `--disable-gpu` headless does not leak `SwiftShader`
- a `permissions.query` patch and subtle, per-context-stable canvas-readback noise against hash-based canvas fingerprinting

On top of that static environment, each channel performs **best-effort behavioral humanization** (jittered dwell times, mouse drift, natural scroll) that is fully guarded — it can never be what kills a channel — and **escalates with each retry**: the existing per-engine fingerprint cooldown doubles as an adaptive ladder, so a profile that just hit a CAPTCHA is retried on a fresh fingerprint with more patient, more human interaction.

**Optional CDP-leak fix (patchright).** Installing [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) (`uv add patchright` or `pip install patchright`) transparently upgrades every browser channel: it is a drop-in Playwright fork that closes the CDP `Runtime.enable` leak Cloudflare/DataDome/Akamai fingerprint. The code auto-detects and prefers it, falling back to vanilla Playwright; set `WEB_SEARCH_DISABLE_PATCHRIGHT=1` to force vanilla.

**Deliberately not done: API-channel TLS/JA3 impersonation.** Browser TLS is already Chromium's. For the plain-HTTP API/content path (`urllib`), matching a browser's JA3/JA4 via `curl_cffi` was deferred on purpose: that same path fetches *discovered, untrusted* URLs and relies on `_guarded_urlopen`'s DNS-pinning SSRF protection (rejecting private IPs and redirects to them), which a naive `curl_cffi` swap would silently drop. The open JSON APIs it would cover rarely block on TLS, so the trade-off is not worth it until the impersonated path can keep the same SSRF guard.

**No proxy layer** is intentional: on a shared consumer IP the levers above are the correct and sufficient strategy; a proxy only matters for high-volume same-host concurrency, which this tool does not do.

## Usage

```bash
venv/Scripts/python.exe search.py "your query" --limit 15 --max-iter 3
venv/Scripts/python.exe search.py "latest product release" --fresh
venv/Scripts/python.exe search.py "your query" --plan-only
venv/Scripts/python.exe search.py "your query" --max-iter 1 --summary
venv/Scripts/python.exe search.py "your query" --max-iter 3 --summary --review-query "targeted gap one" --review-query "targeted gap two"
```

`--limit` is the per-channel target. `--fresh` forces live refresh while retaining stale data only as a failure supplement. `--plan-only` prints query expansion directions without network access. `--summary` emits a compact `review_packet`; repeated `--review-query` values inject caller-selected directions into subsequent all-channel rounds.

## CLI output schema

The CLI prints structured evidence JSON, not prose. There is no `synthesis` /
`summary` / `answer` field anywhere in the output -- writing a concise,
readable synthesis for the human is the calling agent's job (see "Report
evidence" in `SKILL.md`), not something `search.py` generates itself.

With `--summary`, top-level keys are `query`, `detected_vendor`, `alt_queries`,
`sufficient`, `queries_tried`, `model_review`, `coverage`, `channels`,
`filtered_summary`, `cache`, `review_packet`, `resources`, `search_log`. The
evidence to read and synthesize from is `review_packet.top_evidence`
(title/url/snippet/relevance/found_by/cache_state for the top-ranked results,
`top_k=12` by default). `filtered_summary` is easy to mistake for a content
summary because of its name -- it is actually just ad/spam **filter counters**
(`{"total": N, "reasons": {...}}`), not a synthesis of the results.
`cache.degraded`/`cache.last_error` flag whether SQLite lock contention forced
this run onto an in-memory (non-persistent) cache.

When `review_packet.sufficient` is false, check `review_packet.fallback_hint`:
for the first review round it nudges toward `--review-query` rounds (this
tool's own remediation path); once still insufficient after that, it
explicitly says to stop iterating within this tool and supplement with other
web-search/browsing/domain-specific tools available in the calling
environment, rather than reporting the query as unanswerable.

Without `--summary`, the full result also includes `combined_results` (the
complete deduped/merged set, not just the top 12) plus the per-category rows
(`official_open_source`, `academic_results`, `community_results`, ...).

Since the JSON can be large, don't `| tail -N` it -- that lands on unrelated
tail structure (`resources`, `search_log`) rather than the evidence. Redirect
to a file (`cmd > out.json 2>&1`, stdout redirected before stderr is copied to
it) and pull fields by key instead:

```bash
python -c "import json; d=json.load(open('out.json', encoding='utf-8')); \
[print(f\"- {e['title']}\n  {e['url']}\n  {e['snippet']}\n\") for e in d['review_packet']['top_evidence']]"
```

## Cross-language routing

English-indexed sources cannot match a CJK-only query and Chinese community sources rank CJK text far better, so language-affine channels can search a translated variant of the base query:

```bash
venv/Scripts/python.exe search.py "SQLite WAL 模式 并发写入 性能" \
  --query-en "SQLite WAL mode concurrent write performance"
```

English-indexed channels (Stack Overflow, Hacker News, arXiv, Crossref, OpenAlex, DBLP, PubMed, GitHub) then run the `--query-en` text; Chinese community channels (Baidu, Zhihu, CSDN, V2EX, Juejin) run `--query-zh` when supplied. Google/Bing always receive the original query. Wikipedia has no fixed affinity -- it always receives the base query and picks `en.wikipedia.org` vs `zh.wikipedia.org` itself by detecting CJK characters in that text. When no translation is given, English channels fall back to the query's Latin tokens (`SQLite WAL 模式 并发` reaches them as `SQLite WAL`), so mixed queries still get partial reach. The `smart_search()` / `search_all_engines_extended()` APIs accept the same variants via an `alt_queries={"en": ..., "zh": ...}` argument.

## Review loop

Run one summarized round first, inspect coverage, freshness, source diversity, corroboration, contradictions, resources, and failures, then select up to three targeted queries. Rerun with the accumulated `--review-query` arguments; cached earlier rounds make this inexpensive while every new direction is still broadcast to all channels. Stop when the evidence is sufficient or a pass produces no material information gain.

## Setup

PowerShell with `uv` (recommended):

```powershell
$env:UV_PROJECT_ENVIRONMENT="venv"
uv sync --frozen
$env:PLAYWRIGHT_BROWSERS_PATH="$PWD/browsers"
venv/Scripts/python.exe -m playwright install chromium
```

Unix with `uv`:

```bash
UV_PROJECT_ENVIRONMENT=venv uv sync --frozen
PLAYWRIGHT_BROWSERS_PATH="$PWD/browsers" venv/bin/python -m playwright install chromium
```

`requirements.txt` remains a pinned compatibility fallback when `uv` is unavailable.

The search code automatically discovers a Chromium executable under the project-local `browsers/` directory, so runtime does not depend on the host browser installation.

To register the skill with an agent runtime, point it at `SKILL.md` (for example copy this directory, or symlink `SKILL.md` into the runtime's skills directory and keep the repo as the working directory).

## MCP server (browser channels in sandboxed hosts)

Agent runtimes whose command sandbox cannot launch Chromium (e.g. Codex's
`exec_command`, where running `search.py` directly hangs at the first browser
channel) can reach the full skill -- browser SERP channels included -- through
the bundled stdio MCP server, `mcp_server.py`. MCP servers are started by the
host as their own process, outside the per-command sandbox, so Playwright runs
normally and no channel is skipped.

It exposes one tool, `web_search` (arguments: `query`, `limit`, `max_iter`,
`fresh`, `summary`, `query_en`, `query_zh`, `review_queries`), and shells out to
`search.py`. Only the Python standard library is used, so the pinned venv is
untouched.

Register it with the host. For the Codex CLI, add to `~/.codex/config.toml`
(use single-quoted TOML literals so Windows backslashes are not treated as
escapes):

```toml
[mcp_servers.web_search]
command = 'D:\tools\web_search\venv\Scripts\python.exe'
args = ['D:\tools\web_search\mcp_server.py']
# optional tuning / long searches:
# env = { WEB_SEARCH_DEEP_WORKERS = "8", WEB_SEARCH_MCP_TIMEOUT = "600" }
```

After restarting the host, the `web_search` tool is auto-discovered via
`tools/list` -- no per-query setup. If a host instead runs `search.py` directly
inside a no-Chromium sandbox, use `WEB_SEARCH_SKIP_BROWSER=1` (API channels only).

## Stability and performance

- Freshness-aware memory LRU + SQLite WAL cache
- Short TTL for latest/news/price queries; longer TTL for stable documents and PDFs
- Fresh/stale/failure entries separated per channel and query
- Failed page fetches are cached (default 30 min) so hostile or broken pages are not re-attempted every round
- Bounded response, content, graph, page-attempt, and disk-cache budgets
- Retry-After handling and transient HTTP retries; a session circuit breaker skips rate-limited APIs
- Deep page reads run in parallel waves with per-host serialization, so per-site request rates match sequential crawling
- Result pages are parsed as soon as their result containers render instead of after fixed delays
- Lightweight API channels run concurrently while browser engines remain sequential
- Separate `smart_search` calls are session-isolated and serialized; channels inside each call remain concurrent
- Images, media, fonts, ads, and tracking requests are blocked during text retrieval by default
- Untrusted discovery rejects localhost/private IP literals, private DNS answers, and redirects to them

Useful environment variables:

- `WEB_SEARCH_CACHE_PATH`, `WEB_SEARCH_FRESH=1`
- `WEB_SEARCH_SKIP_BROWSER=1` disables the browser SERP channels (Google/Bing/Baidu/Zhihu) and routes deep reads through plain HTTP + trafilatura, for sandboxes that cannot launch Chromium (e.g. Codex's `exec_command`). Only the API channels (arXiv/Crossref/OpenAlex/GitHub/Hacker News/CSDN/Stack Overflow/V2EX/Juejin) return live results; browser channels still serve any stale cache. JS-heavy pages (CSDN/Juejin/some Stack Overflow) may not extract without a browser.
- `WEB_SEARCH_FINGERPRINTS_JSON`
- `WEB_SEARCH_DEEP_PAGES_PER_ROUND`, `WEB_SEARCH_DEEP_ATTEMPTS_PER_ROUND`
- `WEB_SEARCH_DEEP_WORKERS` (parallel deep reads, default 4), `WEB_SEARCH_CONTENT_FAILURE_TTL` (seconds, default 1800)
- `WEB_SEARCH_MAX_CONTENT_CHARS`, `WEB_SEARCH_LINK_DEPTH`
- `SEMANTIC_SCHOLAR_API_KEY` / `REDDIT_ACCESS_TOKEN` enable those channels; `OPENALEX_API_KEY`, `GITHUB_TOKEN`, `STACKEXCHANGE_API_KEY` raise rate limits
- `WEB_SEARCH_CONTACT` for API etiquette headers
- `WEB_SEARCH_ALLOW_PRIVATE_URLS=1` only for explicitly trusted local-network research
- `WEB_SEARCH_INSECURE_TLS=1` **disables TLS certificate verification entirely** (`ssl.CERT_NONE`, no hostname check) for all HTTP fetches. This is a real MITM exposure -- only ever set it against a fully trusted network (e.g. a controlled test fixture), never for normal research use.
- Cache sizing: `WEB_SEARCH_MEMORY_CACHE_ENTRIES` (512), `WEB_SEARCH_MEMORY_CACHE_BYTES` (32MB), `WEB_SEARCH_DISK_CACHE_ENTRIES` (20000), `WEB_SEARCH_DISK_CACHE_BYTES` (512MB)
- Fetch/extraction limits: `WEB_SEARCH_MAX_RESOURCE_BYTES` (25MB, per PDF/page download), `WEB_SEARCH_MAX_PDF_CHARS` (500000), `WEB_SEARCH_MAX_STORED_CONTENT_CHARS` (500000), `WEB_SEARCH_MIN_HOST_INTERVAL` (0.2s pacing between requests to the same host)
- Discovery/expansion: `WEB_SEARCH_MAX_QUERY_EXPANSIONS` (24), `WEB_SEARCH_LINK_MAX_NODES` (200), `WEB_SEARCH_LINK_MAX_EDGES` (500), `WEB_SEARCH_LINKS_PER_DOMAIN` (20), `WEB_SEARCH_VERTICAL_LINK_MIN_RELEVANCE` (0.35), `WEB_SEARCH_MIN_QUERY_ROUNDS` (2)
- `WEB_SEARCH_CHROMIUM_EXECUTABLE` overrides Chromium auto-discovery; `WEB_SEARCH_LOAD_HEAVY_ASSETS=1` stops the default blocking of images/media/fonts during browser fetches
- `WEB_SEARCH_HEADFUL=1` runs a visible (non-headless) browser for the hardest targets (needs a display); `WEB_SEARCH_DISABLE_PATCHRIGHT=1` forces vanilla Playwright even when patchright is installed (see "Stealth and anti-detection")

The output keeps per-channel status, original ranks, `found_by`, freshness, canonical/final URLs, resource lineage, link graph, filtered-ad reasons, coverage, and partial failures.
