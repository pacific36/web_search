# web-search Skill v22

Multi-channel web research that aggregates every enabled source instead of treating sources as fallbacks. No proxy support is included.

## Channels

- General web: Google, Bing, Baidu (all run for every query round)
- Community: Zhihu (through Sogou's zhihu vertical), CSDN, Stack Overflow, V2EX, Juejin, Hacker News
- Academic: arXiv, Crossref, OpenAlex; Semantic Scholar joins when `SEMANTIC_SCHOLAR_API_KEY` is set
- Code: GitHub
- Reddit joins when `REDDIT_ACCESS_TOKEN` is set (anonymous search endpoints are blocked)
- Direct discovery: official pages, canonical/next/feed links, PDFs, attachments, and bounded related-link traversal

Each browser engine owns an independent fingerprint/cooldown state. CAPTCHA, verification, 403, or 429 responses cool that engine/profile pair and retry with another profile while every other channel continues normally. Browser channels stay sequential and each one targets a distinct host (Google/Bing/Baidu/Sogou); API channels run concurrently alongside them.

## Usage

```bash
venv/Scripts/python.exe search.py "your query" --limit 15 --max-iter 3
venv/Scripts/python.exe search.py "latest product release" --fresh
venv/Scripts/python.exe search.py "your query" --plan-only
venv/Scripts/python.exe search.py "your query" --max-iter 1 --summary
venv/Scripts/python.exe search.py "your query" --max-iter 3 --summary --review-query "targeted gap one" --review-query "targeted gap two"
```

`--limit` is the per-channel target. `--fresh` forces live refresh while retaining stale data only as a failure supplement. `--plan-only` prints query expansion directions without network access. `--summary` emits a compact `review_packet`; repeated `--review-query` values inject caller-selected directions into subsequent all-channel rounds.

## Cross-language routing

English-indexed sources cannot match a CJK-only query and Chinese community sources rank CJK text far better, so language-affine channels can search a translated variant of the base query:

```bash
venv/Scripts/python.exe search.py "SQLite WAL 模式 并发写入 性能" \
  --query-en "SQLite WAL mode concurrent write performance"
```

English-indexed channels (Stack Overflow, Hacker News, arXiv, Crossref, OpenAlex, GitHub) then run the `--query-en` text; Chinese community channels (Baidu, Zhihu, CSDN, V2EX, Juejin) run `--query-zh` when supplied. Google/Bing always receive the original query. When no translation is given, English channels fall back to the query's Latin tokens (`SQLite WAL 模式 并发` reaches them as `SQLite WAL`), so mixed queries still get partial reach. The `smart_search()` / `search_all_engines_extended()` APIs accept the same variants via an `alt_queries={"en": ..., "zh": ...}` argument.

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

The output keeps per-channel status, original ranks, `found_by`, freshness, canonical/final URLs, resource lineage, link graph, filtered-ad reasons, coverage, and partial failures.
