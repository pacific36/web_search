"""Query and link discovery primitives for the web-search skill.

This module deliberately performs no network I/O.  It turns search results and
downloaded HTML into bounded, explainable discovery work which the caller can
schedule through its existing fetchers.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
import ipaddress
import re
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import unquote, unquote_to_bytes, urljoin, urlsplit, urlunsplit


# Parameters in this list identify a click or marketing campaign, rather than a
# resource.  Ambiguous names such as ``id``, ``ref``, ``source`` and ``page`` are
# intentionally absent.
TRACKING_PARAMETERS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "gbraid",
        "wbraid",
        "twclid",
        "ttclid",
        "yclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "mkt_tok",
        "_hsenc",
        "_hsmi",
        "vero_conv",
        "vero_id",
        "oly_anon_id",
        "oly_enc_id",
        "rb_clickid",
        "s_cid",
        "spm",
    }
)


def _is_tracking_parameter(name: str) -> bool:
    folded = name.casefold()
    return folded.startswith("utm_") or folded in TRACKING_PARAMETERS


def _filter_tracking_query(query: str) -> str:
    """Remove tracking fields without rewriting the rest of a query string.

    Re-encoding a query can invalidate signed download URLs and can collapse
    malformed-but-distinct byte sequences.  We therefore inspect only each
    parameter name and retain every accepted field byte-for-byte.
    """

    retained: list[str] = []
    for field in query.split("&"):
        raw_name = field.partition("=")[0]
        try:
            name = unquote_to_bytes(raw_name.replace("+", " ")).decode("ascii")
        except (UnicodeDecodeError, ValueError):
            retained.append(field)
            continue
        if not _is_tracking_parameter(name):
            retained.append(field)
    return "&".join(retained)


def _canonical_hostname(hostname: str) -> str:
    """Case-normalise a host without unsafe IDNA2003 folding.

    Python's stdlib IDNA codec implements the older IDNA2003 mapping, where
    distinct modern domains such as ``faß.de`` and ``fass.de`` collide.  Keeping
    non-ASCII hosts in Unicode is conservative: it can miss a Unicode/punycode
    deduplication, but never merges two different sites.
    """

    # ``casefold`` would itself turn ß into ``ss`` and recreate the IDNA2003
    # collision we are avoiding; DNS case normalisation only needs lower().
    return hostname.rstrip(".").lower()


def canonicalize_url(url: str, base_url: Optional[str] = None) -> str:
    """Return a stable HTTP(S) URL while preserving resource semantics.

    Relative URLs are resolved when *base_url* is supplied.  Hosts and schemes
    are case-insensitive and are therefore lower-cased, default ports and page
    fragments are removed, and only explicitly-known tracking parameters are
    discarded.  Query parameter order, duplicates, blank values, path casing,
    and all non-tracking parameters are preserved.

    An empty string is returned for malformed or non-web URLs.
    """

    if not isinstance(url, str):
        return ""
    candidate = url.strip()
    if not candidate:
        return ""
    if base_url:
        candidate = urljoin(base_url, candidate)
    elif candidate.startswith("//"):
        candidate = "https:" + candidate
    elif "://" not in candidate and re.match(
        r"^[A-Za-z][A-Za-z0-9+.-]*:(?!\d)", candidate
    ):
        # Do not reinterpret mailto:, data:, javascript:, magnet:, etc. as a
        # hostname merely because their payload contains a dot.
        return ""
    elif "://" not in candidate and re.match(
        r"^(?:localhost|\[[0-9a-fA-F:]+\]|[^/?#]+\.[^/?#]+)(?::\d+)?(?:[/?#]|$)",
        candidate,
    ):
        candidate = "https://" + candidate

    try:
        parts = urlsplit(candidate)
        scheme = parts.scheme.casefold()
        if scheme not in {"http", "https"} or not parts.hostname:
            return ""
        if "\\" in parts.netloc:
            # urllib and browsers disagree on authority backslashes.  Reject an
            # ambiguous destination instead of assigning it to the wrong host.
            return ""

        hostname = _canonical_hostname(parts.hostname)

        # urlsplit validates the numeric port only when .port is accessed.
        port = parts.port
        if ":" in hostname and not hostname.startswith("["):
            host_for_netloc = f"[{hostname}]"
        else:
            host_for_netloc = hostname
        if port is not None and not (
            (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        ):
            host_for_netloc += f":{port}"

        # Credentials are not part of a resource identity and retaining them in
        # logs or graph nodes would be unsafe.
        query = _filter_tracking_query(parts.query)
        return urlunsplit((scheme, host_for_netloc, parts.path or "/", query, ""))
    except (UnicodeError, ValueError):
        return ""


def _normalise_host(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        unbracketed = value[1:-1] if value.startswith("[") and value.endswith("]") else value
        try:
            return ipaddress.ip_address(unbracketed).compressed.casefold()
        except ValueError:
            pass
        if "://" in value or value.startswith("//"):
            parts = urlsplit(value if "://" in value else "https:" + value)
            host = parts.hostname or ""
        else:
            # A dummy scheme makes urlsplit correctly separate ports and IPv6.
            parts = urlsplit("//" + value)
            host = parts.hostname or ""
        return _canonical_hostname(host)
    except (UnicodeError, ValueError):
        return ""


def host_matches_domain(host_or_url: str, domain_or_url: str) -> bool:
    """Return whether a host is exactly *domain* or one of its subdomains.

    Label boundaries are honoured, so ``notexample.com`` never matches
    ``example.com``.  IP addresses only match exactly.
    """

    host = _normalise_host(host_or_url)
    domain = _normalise_host(domain_or_url.lstrip("*."))
    if not host or not domain:
        return False
    try:
        ipaddress.ip_address(domain)
        return host == domain
    except ValueError:
        return host == domain or host.endswith("." + domain)


def is_public_http_url(url: str, *, allow_private: bool = False) -> bool:
    """Reject literal/local network destinations before they enter a fetch graph.

    Domain-name DNS resolution is intentionally left to the network caller so
    this discovery module remains free of network I/O.
    """
    canonical = canonicalize_url(url)
    if not canonical:
        return False
    if allow_private:
        return True
    host = (urlsplit(canonical).hostname or "").casefold().rstrip(".")
    if not host:
        return False
    if (
        host == "localhost"
        or host.endswith(".localhost")
        or host.endswith(".local")
        or host.endswith(".internal")
        or host.endswith(".lan")
        or host == "home.arpa"
        or host.endswith(".home.arpa")
    ):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


ATTACHMENT_EXTENSIONS = frozenset(
    {
        ".7z",
        ".csv",
        ".doc",
        ".docx",
        ".epub",
        ".gz",
        ".json",
        ".ods",
        ".odt",
        ".ppt",
        ".pptx",
        ".rar",
        ".rtf",
        ".tar",
        ".tar.bz2",
        ".tar.gz",
        ".tgz",
        ".tsv",
        ".txt",
        ".xls",
        ".xlsx",
        ".xml",
        ".zip",
    }
)
FEED_MIME_TYPES = frozenset(
    {"application/atom+xml", "application/feed+json", "application/rss+xml"}
)
ATTACHMENT_MIME_TYPES = frozenset(
    {
        "application/epub+zip",
        "application/gzip",
        "application/json",
        "application/msword",
        "application/rtf",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.oasis.opendocument.presentation",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/x-7z-compressed",
        "application/x-rar-compressed",
        "application/x-tar",
        "application/xml",
        "application/zip",
        "text/csv",
        "text/tab-separated-values",
        "text/plain",
        "text/xml",
    }
)


@dataclass(frozen=True, slots=True)
class DiscoveredLink:
    """A resolved link and the evidence used to classify it."""

    url: str
    kind: str = "link"  # link, canonical, next, feed, pdf, attachment
    anchor: str = ""
    rel: tuple[str, ...] = ()
    mime_type: str = ""


@dataclass(slots=True)
class _RawLink:
    href: str
    tag: str
    rel: tuple[str, ...]
    mime_type: str
    download: bool
    anchor_parts: list[str] = field(default_factory=list)


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.base_href = ""
        self.links: list[_RawLink] = []
        self._anchor_stack: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self._handle_tag(tag, attrs, self_closing=False)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, Optional[str]]]
    ) -> None:
        self._handle_tag(tag, attrs, self_closing=True)

    def _handle_tag(
        self,
        tag: str,
        attrs: list[tuple[str, Optional[str]]],
        *,
        self_closing: bool,
    ) -> None:
        tag = tag.casefold()
        values = {key.casefold(): (value or "") for key, value in attrs}
        if tag == "base" and values.get("href") and not self.base_href:
            self.base_href = values["href"].strip()
            return
        if tag not in {"a", "area", "link"}:
            return
        href = values.get("href", "").strip()
        if not href:
            return
        rel = tuple(
            dict.fromkeys(part.casefold() for part in values.get("rel", "").split() if part)
        )
        mime_type = values.get("type", "").split(";", 1)[0].strip().casefold()
        if tag == "link":
            path = unquote(urlsplit(href).path).casefold()
            metadata_rel = bool({"canonical", "feed", "next"}.intersection(rel))
            feed_alternate = "alternate" in rel and (
                mime_type in FEED_MIME_TYPES or path.endswith((".atom", ".rss"))
            )
            downloadable = (
                mime_type == "application/pdf"
                or mime_type in ATTACHMENT_MIME_TYPES
                or path.endswith(".pdf")
                or any(path.endswith(extension) for extension in ATTACHMENT_EXTENSIONS)
            )
            if not (metadata_rel or feed_alternate or downloadable):
                return
        item = _RawLink(
            href=href,
            tag=tag,
            rel=rel,
            mime_type=mime_type,
            download="download" in values,
        )
        self.links.append(item)
        if tag == "a" and not self_closing:
            self._anchor_stack.append(len(self.links) - 1)

    def handle_data(self, data: str) -> None:
        if self._anchor_stack and data.strip():
            self.links[self._anchor_stack[-1]].anchor_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._anchor_stack:
            self._anchor_stack.pop()


def _link_kind(item: _RawLink, url: str) -> str:
    rel = set(item.rel)
    if "canonical" in rel:
        return "canonical"
    if "next" in rel:
        return "next"
    if item.mime_type in FEED_MIME_TYPES or "feed" in rel:
        return "feed"
    path = unquote(urlsplit(url).path).casefold()
    if path.endswith(".pdf") or item.mime_type == "application/pdf":
        return "pdf"
    if (
        item.download
        or item.mime_type in ATTACHMENT_MIME_TYPES
        or any(path.endswith(extension) for extension in ATTACHMENT_EXTENSIONS)
    ):
        return "attachment"
    return "link"


_KIND_PRIORITY = {"link": 0, "attachment": 1, "pdf": 2, "feed": 3, "next": 4, "canonical": 5}


def extract_html_links(html: str, base_url: str) -> list[DiscoveredLink]:
    """Extract and resolve useful links from an HTML document.

    Link elements (canonical, next and RSS/Atom/JSON feeds), anchors, PDFs and
    common downloadable attachments are classified.  Duplicate destinations are
    coalesced while retaining the strongest classification, anchor and ``rel``
    evidence.
    """

    parser = _LinkParser()
    try:
        parser.feed(html or "")
        parser.close()
    except (TypeError, ValueError):
        return []

    effective_base = canonicalize_url(parser.base_href, base_url) if parser.base_href else base_url
    if not canonicalize_url(effective_base):
        effective_base = base_url

    by_url: dict[str, DiscoveredLink] = {}
    for item in parser.links:
        if item.href.startswith("#"):
            continue
        resolved = canonicalize_url(item.href, effective_base)
        if not resolved:
            continue
        anchor = re.sub(r"\s+", " ", " ".join(item.anchor_parts)).strip()
        kind = _link_kind(item, resolved)
        link = DiscoveredLink(resolved, kind, anchor, item.rel, item.mime_type)
        previous = by_url.get(resolved)
        if previous is None:
            by_url[resolved] = link
            continue
        strongest = link if _KIND_PRIORITY[kind] > _KIND_PRIORITY[previous.kind] else previous
        merged_rel = tuple(dict.fromkeys((*previous.rel, *link.rel)))
        by_url[resolved] = DiscoveredLink(
            resolved,
            strongest.kind,
            previous.anchor or link.anchor,
            merged_rel,
            strongest.mime_type or previous.mime_type or link.mime_type,
        )
    return list(by_url.values())


# Short alias for callers which already use an ``extract_links`` convention.
extract_links = extract_html_links


_TOKEN_RE = re.compile(
    r"[A-Za-z0-9]+(?:[._+#-][A-Za-z0-9]+)*|[\u3400-\u4dbf\u4e00-\u9fff]+"
)
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "how",
        "in",
        "is",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "what",
        "with",
        "与",
        "以及",
        "如何",
        "是什么",
        "的",
    }
)


def tokenize_query(text: str) -> list[str]:
    """Tokenize mixed English/Chinese text without external dictionaries.

    Chinese runs are retained and additionally represented by overlapping
    bigrams.  This makes a query such as ``人工智能`` match the longer phrase
    ``人工智能技术`` without reducing everything to noisy single characters.
    """

    tokens: list[str] = []
    seen: set[str] = set()
    for match in _TOKEN_RE.finditer(text or ""):
        token = match.group(0).casefold()
        candidates = [token]
        if re.fullmatch(r"[\u3400-\u4dbf\u4e00-\u9fff]+", token) and len(token) > 2:
            candidates.extend(token[index : index + 2] for index in range(len(token) - 1))
        for candidate in candidates:
            if candidate in _STOP_WORDS or candidate in seen:
                continue
            seen.add(candidate)
            tokens.append(candidate)
    return tokens


def relevance_score(
    query: str,
    text: str = "",
    *,
    title: str = "",
    snippet: str = "",
    anchor: str = "",
    url: str = "",
) -> float:
    """Return a deterministic lexical relevance score in the range 0..1."""

    query_tokens = tokenize_query(query)
    if not query_tokens:
        return 0.0
    primary = " ".join(part for part in (title, anchor, text, snippet) if part)
    primary_tokens = set(tokenize_query(primary))
    url_tokens = set(tokenize_query(unquote(url)))
    total = len(query_tokens)
    primary_coverage = sum(token in primary_tokens for token in query_tokens) / total
    url_coverage = sum(token in url_tokens for token in query_tokens) / total

    compact_query = re.sub(r"\s+", " ", query).strip().casefold()
    compact_primary = re.sub(r"\s+", " ", primary).strip().casefold()
    phrase_bonus = 0.18 if compact_query and compact_query in compact_primary else 0.0
    title_tokens = set(tokenize_query(title))
    title_coverage = sum(token in title_tokens for token in query_tokens) / total
    score = 0.66 * primary_coverage + 0.18 * url_coverage + 0.16 * title_coverage + phrase_bonus
    return round(min(1.0, max(0.0, score)), 6)


@dataclass(frozen=True, slots=True)
class QueryExpansion:
    query: str
    reason: str
    score: float


def _normalise_query(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _result_value(result: Any, *names: str, default: Any = "") -> Any:
    if isinstance(result, Mapping):
        for name in names:
            if name in result:
                return result[name]
        return default
    for name in names:
        if hasattr(result, name):
            return getattr(result, name)
    return default


_BILINGUAL_REPLACEMENTS = (
    ("最新", "latest"),
    ("当前", "current"),
    ("官方", "official"),
    ("文档", "documentation"),
    ("教程", "tutorial"),
    ("论文", "paper"),
    ("研究", "research"),
    ("源码", "source code"),
    ("下载", "download"),
    ("价格", "price"),
)


def _bilingual_variants(query: str) -> list[str]:
    variants: list[str] = []
    folded = query.casefold()
    for chinese, english in _BILINGUAL_REPLACEMENTS:
        if chinese in query:
            variants.append(_normalise_query(query.replace(chinese, f" {english} ")))
        if re.search(rf"\b{re.escape(english)}\b", folded):
            variants.append(re.sub(rf"\b{re.escape(english)}\b", chinese, query, flags=re.I))
    return variants


def _quality_domains(
    results: Sequence[Any], explicit_domains: Iterable[str]
) -> list[str]:
    domains: list[str] = []
    for value in explicit_domains:
        host = _normalise_host(value)
        if host and host not in domains:
            domains.append(host)
    for result in results:
        quality = _result_value(result, "quality", "quality_score", default=0)
        official = bool(_result_value(result, "official", "is_official", default=False))
        try:
            high_quality = official or float(quality) >= 0.75
        except (TypeError, ValueError):
            high_quality = official
        if not high_quality:
            continue
        host = _normalise_host(str(_result_value(result, "url", "link", default="")))
        if host and host not in domains:
            domains.append(host)
    return domains


def _salient_result_terms(query: str, results: Sequence[Any], limit: int = 5) -> list[str]:
    query_tokens = set(tokenize_query(query))
    counts: Counter[str] = Counter()
    for result in results:
        title = str(_result_value(result, "title", "name", default=""))
        snippet = str(
            _result_value(result, "snippet", "abstract", "summary", "description", default="")
        )
        combined = f"{title} {snippet}"
        weight = 2 if relevance_score(query, title=title, snippet=snippet) >= 0.45 else 1
        for token in tokenize_query(combined):
            if token in query_tokens or token in _STOP_WORDS:
                continue
            if token.isascii() and len(token) < 3:
                continue
            counts[token] += weight
    return [term for term, _ in counts.most_common(limit)]


def generate_query_expansions(
    original_query: str,
    existing_results: Sequence[Any] = (),
    quality_domains: Iterable[str] = (),
    *,
    filetypes: Sequence[str] = ("pdf", "docx", "xlsx", "pptx", "csv"),
    max_queries: int = 24,
) -> list[QueryExpansion]:
    """Build bounded, deduplicated query variants with explicit reasons.

    The generator does not pretend to translate arbitrary language.  It emits
    useful Chinese and English research-intent variants, translates a small set
    of unambiguous intent terms, mines related terms from existing result text,
    and targets caller-approved or high-quality domains.
    """

    original = _normalise_query(original_query)
    if not original or max_queries <= 0:
        return []
    expansions: list[QueryExpansion] = []
    seen = {original.casefold()}

    def add(query: str, reason: str, score: float) -> None:
        candidate = _normalise_query(query)
        key = candidate.casefold()
        if candidate and key not in seen and len(expansions) < max_queries:
            seen.add(key)
            expansions.append(QueryExpansion(candidate, reason, round(score, 3)))

    escaped = original.replace('"', " ")
    add(f'"{_normalise_query(escaped)}"', "exact", 1.0)
    for variant in _bilingual_variants(original)[:4]:
        add(variant, "bilingual", 0.94)
    add(f"{original} 官方", "official_zh", 0.92)
    add(f"{original} official", "official_en", 0.92)
    add(f"{original} 官方 文档", "docs_zh", 0.9)
    add(f"{original} official documentation", "docs_en", 0.9)

    # Preserve breadth under a finite query budget: a long domain allow-list
    # must not crowd out resource-type and result-driven expansions.
    for domain in _quality_domains(existing_results, quality_domains)[:5]:
        add(f"{original} site:{domain}", "quality_domain", 0.88)
    for filetype in filetypes:
        cleaned = re.sub(r"[^a-z0-9]", "", str(filetype).casefold())
        if cleaned:
            add(f"{original} filetype:{cleaned}", f"filetype_{cleaned}", 0.84)
    for term in _salient_result_terms(original, existing_results):
        add(f"{original} {term}", "related_term", 0.72)
    return expansions


# More concise alias for orchestration code.
expand_queries = generate_query_expansions


@dataclass(slots=True)
class LinkNode:
    url: str
    depth: int
    title: str = ""
    snippet: str = ""
    relevance: float = 0.0
    visited: bool = False
    scheduled: bool = False
    reasons: set[str] = field(default_factory=set)


@dataclass(slots=True)
class LinkEdge:
    source: str
    target: str
    reason: str
    anchor: str = ""
    rel: tuple[str, ...] = ()
    cyclic: bool = False
    reasons: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.reason:
            self.reasons.add(self.reason)


class LinkGraph:
    """A bounded, cycle-safe discovery graph with relevance-based scheduling.

    Backlinks are retained as evidence, including edges that close a graph
    cycle.  Fetch loops are prevented at the node scheduler: every canonical
    node can be scheduled once until explicitly released.
    """

    def __init__(
        self,
        query: str,
        *,
        max_depth: int = 2,
        max_nodes: int = 200,
        max_edges: int = 500,
        per_domain_limit: int = 20,
        min_relevance: float = 0.05,
        allow_private: bool = False,
    ) -> None:
        if max_depth < 0 or max_nodes < 1 or max_edges < 1 or per_domain_limit < 1:
            raise ValueError("graph bounds must be positive (max_depth may be zero)")
        self.query = query
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.max_edges = max_edges
        self.per_domain_limit = per_domain_limit
        self.min_relevance = min_relevance
        self.allow_private = bool(allow_private)
        self.nodes: dict[str, LinkNode] = {}
        self.edges: list[LinkEdge] = []
        self._edge_keys: set[tuple[str, str]] = set()
        self._edges_by_pair: dict[tuple[str, str], LinkEdge] = {}
        self._adjacency: dict[str, set[str]] = {}
        self._domain_counts: Counter[str] = Counter()

    def add_node(
        self,
        url: str,
        *,
        depth: int = 0,
        title: str = "",
        snippet: str = "",
        relevance: Optional[float] = None,
        reason: str = "discovered",
    ) -> Optional[LinkNode]:
        canonical = canonicalize_url(url)
        if (not canonical or not is_public_http_url(canonical, allow_private=self.allow_private)
                or depth < 0 or depth > self.max_depth):
            return None
        score = (
            relevance_score(self.query, title=title, snippet=snippet, url=canonical)
            if relevance is None
            else min(1.0, max(0.0, float(relevance)))
        )
        existing = self.nodes.get(canonical)
        if existing:
            if depth < existing.depth:
                existing.depth = depth
                self._propagate_depth(existing.url)
            existing.title = existing.title or title
            existing.snippet = existing.snippet or snippet
            existing.relevance = max(existing.relevance, score)
            if reason:
                existing.reasons.add(reason)
            return existing
        if len(self.nodes) >= self.max_nodes:
            return None
        host = urlsplit(canonical).hostname or ""
        if self._domain_counts[host] >= self.per_domain_limit:
            return None
        node = LinkNode(canonical, depth, title, snippet, score)
        if reason:
            node.reasons.add(reason)
        self.nodes[canonical] = node
        self._domain_counts[host] += 1
        self._adjacency.setdefault(canonical, set())
        return node

    def _propagate_depth(self, start_url: str) -> None:
        """Propagate a newly discovered shorter path through existing edges."""

        pending = deque([start_url])
        while pending:
            source_url = pending.popleft()
            source = self.nodes[source_url]
            for target_url in self._adjacency.get(source_url, ()):
                target = self.nodes[target_url]
                proposed = source.depth + 1
                if proposed < target.depth:
                    target.depth = proposed
                    pending.append(target_url)

    def add_seed(
        self,
        url: str,
        *,
        title: str = "",
        snippet: str = "",
        relevance: Optional[float] = None,
    ) -> Optional[LinkNode]:
        # A seed is explicit work supplied by a search channel.  It must remain
        # schedulable even when its bare URL contains none of the query terms.
        seed_relevance = 1.0 if relevance is None else relevance
        return self.add_node(
            url,
            depth=0,
            title=title,
            snippet=snippet,
            relevance=seed_relevance,
            reason="seed",
        )

    def _would_create_cycle(self, source: str, target: str) -> bool:
        if source == target:
            return True
        pending = [target]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == source:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(self._adjacency.get(current, ()))
        return False

    def add_link(
        self,
        source_url: str,
        target_url: str,
        *,
        reason: str = "link",
        anchor: str = "",
        rel: Iterable[str] = (),
        title: str = "",
        snippet: str = "",
        relevance: Optional[float] = None,
    ) -> bool:
        """Add an edge and target node, rejecting duplicates, cycles and overflow."""

        source_key = canonicalize_url(source_url)
        source = self.nodes.get(source_key)
        if source is None:
            return False
        target_key = canonicalize_url(target_url, source.url)
        if not target_key:
            return False
        edge_reason = str(reason or "link")
        clean_anchor = re.sub(r"\s+", " ", str(anchor or "")).strip()
        if isinstance(rel, str):
            rel_values: Iterable[Any] = rel.split()
        else:
            try:
                rel_values = tuple(rel or ())
            except TypeError:
                rel_values = ()
        clean_rel = tuple(
            dict.fromkeys(str(value).casefold() for value in rel_values if value)
        )
        edge_key = (source.url, target_key)
        if edge_key in self._edge_keys:
            edge = self._edges_by_pair[edge_key]
            edge.reasons.add(edge_reason)
            edge.anchor = edge.anchor or clean_anchor
            edge.rel = tuple(dict.fromkeys((*edge.rel, *clean_rel)))
            target = self.nodes.get(target_key)
            if target is not None:
                target.reasons.add(edge_reason)
            return False
        if len(self.edges) >= self.max_edges:
            return False
        cyclic = self._would_create_cycle(source.url, target_key)

        target_depth = source.depth + 1
        existing_target = self.nodes.get(target_key)
        if target_depth > self.max_depth and existing_target is None:
            return False
        node_depth = (
            min(existing_target.depth, target_depth)
            if existing_target is not None
            else target_depth
        )
        if relevance is None:
            relevance = relevance_score(
                self.query,
                title=title,
                snippet=snippet,
                anchor=anchor,
                url=target_key,
            )
            reason_floor = {
                "canonical": 0.2,
                "next": 0.25,
                "feed": 0.18,
                "pdf": 0.08,
                "attachment": 0.08,
            }.get(edge_reason, 0.0)
            relevance = max(reason_floor, relevance)
        target = self.add_node(
            target_key,
            depth=node_depth,
            title=title,
            snippet=snippet,
            relevance=relevance,
            reason=edge_reason,
        )
        if target is None:
            return False

        edge = LinkEdge(
            source.url,
            target.url,
            edge_reason,
            clean_anchor,
            clean_rel,
            cyclic,
        )
        self.edges.append(edge)
        self._edge_keys.add(edge_key)
        self._edges_by_pair[edge_key] = edge
        self._adjacency[source.url].add(target.url)
        return True

    def add_discovered_links(
        self, source_url: str, links: Iterable[DiscoveredLink | Mapping[str, Any]]
    ) -> int:
        """Add links returned by :func:`extract_html_links`."""

        added = 0
        for link in links:
            if isinstance(link, Mapping):
                url = str(link.get("url", ""))
                kind = str(link.get("kind", "link"))
                anchor = str(link.get("anchor", ""))
                rel = link.get("rel", ())
                if isinstance(rel, str):
                    rel = rel.split()
                elif rel is None:
                    rel = ()
            else:
                url, kind, anchor, rel = link.url, link.kind, link.anchor, link.rel
            if self.add_link(source_url, url, reason=kind, anchor=anchor, rel=rel):
                added += 1
        return added

    def select_next_batch(
        self,
        limit: int = 10,
        *,
        min_relevance: Optional[float] = None,
        mark_scheduled: bool = True,
    ) -> list[LinkNode]:
        """Choose the best unseen nodes, favouring relevance then shallow depth."""

        if limit <= 0:
            return []
        threshold = self.min_relevance if min_relevance is None else min_relevance
        candidates = [
            node
            for node in self.nodes.values()
            if not node.visited and not node.scheduled and node.relevance >= threshold
        ]
        candidates.sort(key=lambda node: (-node.relevance, node.depth, node.url))
        selected = candidates[:limit]
        if mark_scheduled:
            for node in selected:
                node.scheduled = True
        return selected

    # Friendly synonym for schedulers.
    next_batch = select_next_batch

    def mark_visited(self, url: str) -> bool:
        node = self.nodes.get(canonicalize_url(url))
        if node is None:
            return False
        node.visited = True
        node.scheduled = False
        return True

    def release(self, url: str) -> bool:
        """Return a scheduled but unfetched node to the candidate pool."""

        node = self.nodes.get(canonicalize_url(url))
        if node is None or node.visited:
            return False
        node.scheduled = False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "bounds": {
                "max_depth": self.max_depth,
                "max_nodes": self.max_nodes,
                "max_edges": self.max_edges,
                "per_domain_limit": self.per_domain_limit,
                "allow_private": self.allow_private,
            },
            "nodes": [
                {
                    "url": node.url,
                    "depth": node.depth,
                    "title": node.title,
                    "snippet": node.snippet,
                    "relevance": node.relevance,
                    "visited": node.visited,
                    "scheduled": node.scheduled,
                    "reasons": sorted(node.reasons),
                }
                for node in self.nodes.values()
            ],
            "edges": [
                {
                    "source": edge.source,
                    "target": edge.target,
                    "reason": edge.reason,
                    "reasons": sorted(edge.reasons),
                    "anchor": edge.anchor,
                    "rel": list(edge.rel),
                    "cyclic": edge.cyclic,
                }
                for edge in self.edges
            ],
        }


__all__ = [
    "ATTACHMENT_EXTENSIONS",
    "ATTACHMENT_MIME_TYPES",
    "DiscoveredLink",
    "LinkEdge",
    "LinkGraph",
    "LinkNode",
    "QueryExpansion",
    "TRACKING_PARAMETERS",
    "canonicalize_url",
    "expand_queries",
    "extract_html_links",
    "extract_links",
    "generate_query_expansions",
    "host_matches_domain",
    "is_public_http_url",
    "relevance_score",
    "tokenize_query",
]
