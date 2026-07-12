"""Small, freshness-aware cache for search and resource retrieval.

The public API is intentionally narrow::

    policy = classify_cache_policy(query, "serp")
    cache.set("google", key, results,
              ttl=policy.success_ttl, stale_ttl=policy.stale_ttl)
    hit = cache.get("google", key)  # hit.state: fresh/stale/miss
    cache.set_failure("google", key, "timeout", ttl=policy.failure_ttl)

Values may be JSON-compatible objects, strings, or bytes.  The process-local
layer is a real LRU.  SQLite supplies a bounded, WAL-backed persistent layer.
If the database is unreadable, the cache degrades to an in-memory database
instead of interrupting retrieval.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple, Union
from urllib.parse import urlsplit


FRESH = "fresh"
STALE = "stale"
MISS = "miss"


@dataclass(frozen=True)
class CachePolicy:
    """TTL durations, in seconds, for one resource class.

    ``stale_ttl`` is the grace period *after* ``success_ttl``.  Failures use a
    separate, deliberately short TTL and are never served stale.
    """

    category: str
    success_ttl: int
    stale_ttl: int
    failure_ttl: int


@dataclass(frozen=True)
class CacheResult:
    """The value and freshness metadata returned by :meth:`FreshnessCache.get`."""

    state: str
    value: Any = None
    is_failure: bool = False
    error: Optional[str] = None
    created_at: Optional[float] = None
    expires_at: Optional[float] = None
    stale_until: Optional[float] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    content_hash: Optional[str] = None

    @property
    def hit(self) -> bool:
        return self.state != MISS

    @property
    def fresh(self) -> bool:
        return self.state == FRESH

    @property
    def stale(self) -> bool:
        return self.state == STALE


@dataclass(frozen=True)
class _StoredEntry:
    namespace: str
    key: str
    value: Any
    payload: bytes
    encoding: str
    created_at: float
    expires_at: float
    stale_until: float
    etag: Optional[str]
    last_modified: Optional[str]
    content_hash: Optional[str]
    is_failure: bool
    error: Optional[str]
    size_bytes: int
    last_accessed: float


_TIME_SENSITIVE_ENGLISH = re.compile(
    r"(?:\b(?:latest|today|tonight|now|current|currently|breaking|news|"
    r"price|prices|pricing|quote|quotes|realtime|real[ -]?time|recent|"
    r"recently|this\s+(?:week|month|year)|as\s+of)\b)",
    re.IGNORECASE,
)
_TIME_SENSITIVE_CJK = (
    "最新",
    "今日",
    "今天",
    "当前",
    "目前",
    "实时",
    "即时",
    "新闻",
    "价格",
    "报价",
    "行情",
    "现价",
    "刚刚",
    "本周",
    "本月",
    "今年",
    "近期",
    "最近",
    "截至",
)
_COMMUNITY_HOSTS = (
    "reddit.com",
    "news.ycombinator.com",
    "stackoverflow.com",
    "stackexchange.com",
    "quora.com",
    "zhihu.com",
    "v2ex.com",
)
_DOCUMENT_EXTENSIONS = {
    ".doc",
    ".docx",
    ".odt",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".csv",
    ".rtf",
    ".epub",
}

_BASE_POLICIES: Mapping[str, CachePolicy] = {
    # Very recent queries are refreshed aggressively; stale data remains useful
    # as an explicitly marked supplement when live retrieval fails.
    "time_sensitive": CachePolicy("time_sensitive", 180, 900, 30),
    "serp": CachePolicy("serp", 1_800, 21_600, 60),
    "community": CachePolicy("community", 900, 21_600, 60),
    "webpage": CachePolicy("webpage", 43_200, 259_200, 90),
    "document": CachePolicy("document", 172_800, 604_800, 120),
    "pdf": CachePolicy("pdf", 604_800, 2_592_000, 120),
}

_STABLE_TTL_CAPS = {
    "webpage": 259_200,
    "document": 604_800,
    "pdf": 2_592_000,
}


def _has_time_intent(query: str) -> bool:
    normalized = query.casefold().strip()
    if not normalized:
        return False
    if _TIME_SENSITIVE_ENGLISH.search(normalized):
        return True
    if any(token in normalized for token in _TIME_SENSITIVE_CJK):
        return True
    # A query naming the current year normally asks for a current snapshot.
    current_year = str(datetime.now(timezone.utc).year)
    return bool(re.search(rf"(?<!\d){re.escape(current_year)}(?!\d)", normalized))


def _infer_category(resource_type: str, url: str, content_type: str) -> str:
    resource = (resource_type or "auto").casefold().replace("-", "_").strip()
    aliases = {
        "search": "serp",
        "search_results": "serp",
        "search_result": "serp",
        "forum": "community",
        "social": "community",
        "page": "webpage",
        "html": "webpage",
        "web": "webpage",
        "doc": "document",
        "docs": "document",
        "office": "document",
        "application/pdf": "pdf",
    }
    resource = aliases.get(resource, resource)
    if resource in _BASE_POLICIES:
        return resource

    mime = (content_type or "").casefold().split(";", 1)[0].strip()
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith("application/") and any(
        marker in mime
        for marker in ("word", "excel", "powerpoint", "officedocument", "rtf", "epub")
    ):
        return "document"

    parsed = urlsplit(url or "")
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    extension = Path(path).suffix
    if extension == ".pdf":
        return "pdf"
    if extension in _DOCUMENT_EXTENSIONS:
        return "document"
    if any(host == item or host.endswith("." + item) for item in _COMMUNITY_HOSTS):
        return "community"
    if host == "github.com" and any(part in path for part in ("/issues", "/discussions")):
        return "community"
    return "webpage"


def classify_cache_policy(
    query: str = "",
    resource_type: str = "serp",
    *,
    url: str = "",
    content_type: str = "",
    unchanged_count: int = 0,
    changed_recently: bool = False,
) -> CachePolicy:
    """Return a freshness policy for a query/resource combination.

    Time-sensitive intent overrides SERP, community, and generic webpage
    policies, but not version-like documents or PDFs.  For stable fetched
    content, ``unchanged_count`` gradually extends only the fresh period, up to
    a conservative category cap.  ``changed_recently`` shortens it instead.
    """

    category = _infer_category(resource_type, url, content_type)
    if _has_time_intent(query) and category in {"serp", "community", "webpage"}:
        category = "time_sensitive"

    base = _BASE_POLICIES[category]
    success_ttl = base.success_ttl
    if category in _STABLE_TTL_CAPS:
        if changed_recently:
            success_ttl = max(60, success_ttl // 2)
        elif unchanged_count > 0:
            multiplier = 1.0 + min(int(unchanged_count), 8) * 0.25
            success_ttl = min(
                _STABLE_TTL_CAPS[category], int(round(success_ttl * multiplier))
            )

    return CachePolicy(category, success_ttl, base.stale_ttl, base.failure_ttl)


# A short, discoverable alias for callers that think in TTLs rather than policy.
classify_ttl = classify_cache_policy


def make_cache_key(*parts: Any) -> str:
    """Build a deterministic compact key from query parameters."""

    encoded = json.dumps(
        parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class FreshnessCache:
    """Two-level freshness cache with a true in-process LRU and SQLite WAL.

    ``memory_max_*`` and ``disk_max_*`` are independent bounds.  A zero bound
    disables storage in that level.  Disk access timestamps are batched until
    the next write/prune so memory hits stay cheap while disk eviction still
    follows recent use.
    """

    _TABLE = "freshness_cache_v1"

    def __init__(
        self,
        path: Union[str, os.PathLike[str]] = "freshness_cache.sqlite3",
        *,
        memory_max_entries: int = 512,
        memory_max_bytes: int = 16 * 1024 * 1024,
        disk_max_entries: int = 20_000,
        disk_max_bytes: int = 512 * 1024 * 1024,
        clock: Callable[[], float] = time.time,
    ) -> None:
        for name, value in (
            ("memory_max_entries", memory_max_entries),
            ("memory_max_bytes", memory_max_bytes),
            ("disk_max_entries", disk_max_entries),
            ("disk_max_bytes", disk_max_bytes),
        ):
            if int(value) < 0:
                raise ValueError(f"{name} must be >= 0")

        self.path = os.fspath(path)
        self.memory_max_entries = int(memory_max_entries)
        self.memory_max_bytes = int(memory_max_bytes)
        self.disk_max_entries = int(disk_max_entries)
        self.disk_max_bytes = int(disk_max_bytes)
        self._clock = clock
        self._lock = threading.RLock()
        self._memory: "OrderedDict[Tuple[str, str], _StoredEntry]" = OrderedDict()
        self._memory_bytes = 0
        self._pending_touches: Dict[Tuple[str, str], float] = {}
        self._conn: Optional[sqlite3.Connection] = None
        self.degraded = False
        self.last_error: Optional[str] = None

        try:
            self._conn = self._connect(self.path)
        except (OSError, sqlite3.Error) as exc:
            self.last_error = str(exc)
            self.degraded = True
            self._conn = self._connect(":memory:")

    @staticmethod
    def _validate_identity(namespace: str, key: str) -> Tuple[str, str]:
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("namespace must be a non-empty string")
        if not isinstance(key, str) or not key:
            raise ValueError("key must be a non-empty string")
        return namespace, key

    @classmethod
    def _initialize_connection(cls, conn: sqlite3.Connection, persistent: bool) -> None:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        if persistent:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {cls._TABLE} (
                namespace TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                payload BLOB NOT NULL,
                encoding TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                stale_until REAL NOT NULL,
                etag TEXT,
                last_modified TEXT,
                content_hash TEXT,
                is_failure INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                size_bytes INTEGER NOT NULL,
                last_accessed REAL NOT NULL,
                PRIMARY KEY (namespace, cache_key)
            ) WITHOUT ROWID
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {cls._TABLE}_expiry "
            f"ON {cls._TABLE} (stale_until)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {cls._TABLE}_lru "
            f"ON {cls._TABLE} (last_accessed, created_at)"
        )
        conn.commit()

    @classmethod
    def _connect(cls, path: str) -> sqlite3.Connection:
        persistent = path != ":memory:"
        if persistent:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
        try:
            cls._initialize_connection(conn, persistent)
        except Exception:
            conn.close()
            raise
        return conn

    def _ensure_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("cache is closed")
        return self._conn

    def _degrade(self, exc: BaseException) -> None:
        """Switch to a healthy in-memory database after a SQLite failure."""

        self.last_error = str(exc)
        self.degraded = True
        old = self._conn
        self._conn = None
        if old is not None:
            try:
                old.close()
            except sqlite3.Error:
                pass
        self._pending_touches.clear()
        self._conn = self._connect(":memory:")

    @staticmethod
    def _serialize(value: Any) -> Tuple[bytes, str]:
        if isinstance(value, bytes):
            return value, "bytes"
        if isinstance(value, str):
            return value.encode("utf-8"), "utf8"
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise TypeError(
                "cache values must be JSON-compatible, str, or bytes"
            ) from exc
        return encoded, "json"

    @staticmethod
    def _deserialize(payload: bytes, encoding: str) -> Any:
        if encoding == "bytes":
            return payload
        if encoding == "utf8":
            return payload.decode("utf-8")
        if encoding == "json":
            return json.loads(payload.decode("utf-8"))
        if encoding == "none":
            return None
        raise ValueError(f"unknown cache encoding: {encoding}")

    @staticmethod
    def _estimate_size(
        namespace: str,
        key: str,
        payload: bytes,
        etag: Optional[str],
        last_modified: Optional[str],
        content_hash: Optional[str],
        error: Optional[str],
    ) -> int:
        strings: Iterable[Optional[str]] = (
            namespace,
            key,
            etag,
            last_modified,
            content_hash,
            error,
        )
        return len(payload) + sum(len(item.encode("utf-8")) for item in strings if item)

    @staticmethod
    def _state(entry: _StoredEntry, now: float) -> str:
        if now < entry.expires_at:
            return FRESH
        if not entry.is_failure and now < entry.stale_until:
            return STALE
        return MISS

    @staticmethod
    def _public_result(entry: _StoredEntry, state: str) -> CacheResult:
        return CacheResult(
            state=state,
            value=entry.value,
            is_failure=entry.is_failure,
            error=entry.error,
            created_at=entry.created_at,
            expires_at=entry.expires_at,
            stale_until=entry.stale_until,
            etag=entry.etag,
            last_modified=entry.last_modified,
            content_hash=entry.content_hash,
        )

    @staticmethod
    def _miss() -> CacheResult:
        return CacheResult(MISS)

    def _memory_remove(self, identity: Tuple[str, str]) -> None:
        entry = self._memory.pop(identity, None)
        if entry is not None:
            self._memory_bytes -= entry.size_bytes
        self._pending_touches.pop(identity, None)

    def _memory_put(self, entry: _StoredEntry) -> None:
        identity = (entry.namespace, entry.key)
        self._memory_remove(identity)
        if self.memory_max_entries == 0 or self.memory_max_bytes == 0:
            return
        if entry.size_bytes > self.memory_max_bytes:
            return
        self._memory[identity] = entry
        self._memory_bytes += entry.size_bytes
        self._memory.move_to_end(identity)
        while (
            len(self._memory) > self.memory_max_entries
            or self._memory_bytes > self.memory_max_bytes
        ):
            old_identity, old_entry = self._memory.popitem(last=False)
            self._memory_bytes -= old_entry.size_bytes

    def _upsert(self, entry: _StoredEntry) -> None:
        conn = self._ensure_open()
        conn.execute(
            f"""
            INSERT INTO {self._TABLE} (
                namespace, cache_key, payload, encoding, created_at, expires_at,
                stale_until, etag, last_modified, content_hash, is_failure,
                error, size_bytes, last_accessed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(namespace, cache_key) DO UPDATE SET
                payload=excluded.payload,
                encoding=excluded.encoding,
                created_at=excluded.created_at,
                expires_at=excluded.expires_at,
                stale_until=excluded.stale_until,
                etag=excluded.etag,
                last_modified=excluded.last_modified,
                content_hash=excluded.content_hash,
                is_failure=excluded.is_failure,
                error=excluded.error,
                size_bytes=excluded.size_bytes,
                last_accessed=excluded.last_accessed
            """,
            (
                entry.namespace,
                entry.key,
                sqlite3.Binary(entry.payload),
                entry.encoding,
                entry.created_at,
                entry.expires_at,
                entry.stale_until,
                entry.etag,
                entry.last_modified,
                entry.content_hash,
                int(entry.is_failure),
                entry.error,
                entry.size_bytes,
                entry.last_accessed,
            ),
        )
        conn.commit()
        self._pending_touches.pop((entry.namespace, entry.key), None)

    def _flush_touches(self) -> None:
        if not self._pending_touches:
            return
        conn = self._ensure_open()
        rows = [
            (timestamp, namespace, key)
            for (namespace, key), timestamp in self._pending_touches.items()
        ]
        conn.executemany(
            f"UPDATE {self._TABLE} SET last_accessed=? "
            "WHERE namespace=? AND cache_key=?",
            rows,
        )
        conn.commit()
        self._pending_touches.clear()

    def _evict_disk(self, now: float) -> None:
        conn = self._ensure_open()
        self._flush_touches()

        expired = conn.execute(
            f"SELECT namespace, cache_key FROM {self._TABLE} WHERE stale_until<=?",
            (now,),
        ).fetchall()
        if expired:
            conn.execute(f"DELETE FROM {self._TABLE} WHERE stale_until<=?", (now,))
            for row in expired:
                self._memory_remove((row["namespace"], row["cache_key"]))

        aggregate = conn.execute(
            f"SELECT COUNT(*) AS entries, COALESCE(SUM(size_bytes), 0) AS bytes "
            f"FROM {self._TABLE}"
        ).fetchone()
        count = int(aggregate["entries"])
        total = int(aggregate["bytes"])
        if count <= self.disk_max_entries and total <= self.disk_max_bytes:
            conn.commit()
            return

        candidates = conn.execute(
            f"SELECT namespace, cache_key, size_bytes FROM {self._TABLE} "
            "ORDER BY last_accessed ASC, created_at ASC"
        ).fetchall()
        victims = []
        for row in candidates:
            if count <= self.disk_max_entries and total <= self.disk_max_bytes:
                break
            identity = (row["namespace"], row["cache_key"])
            victims.append(identity)
            count -= 1
            total -= int(row["size_bytes"])

        if victims:
            conn.executemany(
                f"DELETE FROM {self._TABLE} WHERE namespace=? AND cache_key=?",
                victims,
            )
            for identity in victims:
                self._memory_remove(identity)
        conn.commit()

    def _write(self, entry: _StoredEntry) -> None:
        self._memory_put(entry)
        try:
            self._upsert(entry)
            self._evict_disk(entry.created_at)
        except sqlite3.Error as exc:
            self._degrade(exc)
            self._upsert(entry)
            self._evict_disk(entry.created_at)

    def set(
        self,
        namespace: str,
        key: str,
        value: Any,
        *,
        ttl: Union[int, float],
        stale_ttl: Union[int, float] = 0,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        content_hash: Optional[str] = None,
    ) -> None:
        """Store a successful value with fresh and stale durations."""

        namespace, key = self._validate_identity(namespace, key)
        ttl = float(ttl)
        stale_ttl = float(stale_ttl)
        if ttl < 0 or stale_ttl < 0:
            raise ValueError("ttl and stale_ttl must be >= 0")
        payload, encoding = self._serialize(value)
        if content_hash is None:
            content_hash = hashlib.sha256(payload).hexdigest()
        # Store the serialized form's value in memory too.  This prevents caller
        # mutation from changing a cached dict/list and keeps memory and disk
        # round-trips identical (for example, JSON arrays are always lists).
        stored_value = self._deserialize(payload, encoding)
        now = float(self._clock())
        size_bytes = self._estimate_size(
            namespace, key, payload, etag, last_modified, content_hash, None
        )
        entry = _StoredEntry(
            namespace=namespace,
            key=key,
            value=stored_value,
            payload=payload,
            encoding=encoding,
            created_at=now,
            expires_at=now + ttl,
            stale_until=now + ttl + stale_ttl,
            etag=etag,
            last_modified=last_modified,
            content_hash=content_hash,
            is_failure=False,
            error=None,
            size_bytes=size_bytes,
            last_accessed=now,
        )
        with self._lock:
            self._ensure_open()
            self._write(entry)

    def set_failure(
        self,
        namespace: str,
        key: str,
        error: str,
        *,
        ttl: Union[int, float],
    ) -> None:
        """Store a short negative-cache entry; failures never have stale grace."""

        namespace, key = self._validate_identity(namespace, key)
        ttl = float(ttl)
        if ttl < 0:
            raise ValueError("ttl must be >= 0")
        error = str(error)
        now = float(self._clock())
        payload = b""
        size_bytes = self._estimate_size(
            namespace, key, payload, None, None, None, error
        )
        entry = _StoredEntry(
            namespace=namespace,
            key=key,
            value=None,
            payload=payload,
            encoding="none",
            created_at=now,
            expires_at=now + ttl,
            stale_until=now + ttl,
            etag=None,
            last_modified=None,
            content_hash=None,
            is_failure=True,
            error=error,
            size_bytes=size_bytes,
            last_accessed=now,
        )
        with self._lock:
            self._ensure_open()
            self._write(entry)

    def _delete_disk_identity(self, identity: Tuple[str, str],
                              only_expired_at: Optional[float] = None) -> None:
        conn = self._ensure_open()
        if only_expired_at is None:
            conn.execute(
                f"DELETE FROM {self._TABLE} WHERE namespace=? AND cache_key=?", identity
            )
        else:
            # Another process sharing the cache file may have refreshed this
            # key; only rows that are expired on disk may be dropped.
            conn.execute(
                f"DELETE FROM {self._TABLE} "
                "WHERE namespace=? AND cache_key=? AND stale_until<=?",
                (identity[0], identity[1], float(only_expired_at)),
            )
        conn.commit()

    def _entry_from_row(self, row: sqlite3.Row) -> _StoredEntry:
        payload = bytes(row["payload"])
        encoding = str(row["encoding"])
        value = self._deserialize(payload, encoding)
        return _StoredEntry(
            namespace=str(row["namespace"]),
            key=str(row["cache_key"]),
            value=value,
            payload=payload,
            encoding=encoding,
            created_at=float(row["created_at"]),
            expires_at=float(row["expires_at"]),
            stale_until=float(row["stale_until"]),
            etag=row["etag"],
            last_modified=row["last_modified"],
            content_hash=row["content_hash"],
            is_failure=bool(row["is_failure"]),
            error=row["error"],
            size_bytes=int(row["size_bytes"]),
            last_accessed=float(row["last_accessed"]),
        )

    def get(self, namespace: str, key: str) -> CacheResult:
        """Return a ``fresh``, ``stale``, or ``miss`` result."""

        identity = self._validate_identity(namespace, key)
        now = float(self._clock())
        with self._lock:
            self._ensure_open()
            memory_entry = self._memory.get(identity)
            if memory_entry is not None:
                state = self._state(memory_entry, now)
                if state == MISS:
                    self._memory_remove(identity)
                    try:
                        self._delete_disk_identity(identity, only_expired_at=now)
                    except sqlite3.Error as exc:
                        self._degrade(exc)
                        return self._miss()
                    # Fall through to the disk read to pick up a refresh
                    # written by another process since this copy expired.
                else:
                    self._memory.move_to_end(identity)
                    self._pending_touches[identity] = now
                    return self._public_result(memory_entry, state)

            try:
                row = self._ensure_open().execute(
                    f"SELECT * FROM {self._TABLE} "
                    "WHERE namespace=? AND cache_key=?",
                    identity,
                ).fetchone()
            except sqlite3.Error as exc:
                self._degrade(exc)
                return self._miss()
            if row is None:
                return self._miss()

            try:
                entry = self._entry_from_row(row)
            except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
                # A damaged row must not poison retrieval or the rest of the DB.
                try:
                    self._delete_disk_identity(identity)
                except sqlite3.Error as exc:
                    self._degrade(exc)
                return self._miss()

            state = self._state(entry, now)
            if state == MISS:
                try:
                    self._delete_disk_identity(identity, only_expired_at=now)
                except sqlite3.Error as exc:
                    self._degrade(exc)
                return self._miss()

            self._memory_put(entry)
            self._pending_touches[identity] = now
            if len(self._pending_touches) >= 256:
                try:
                    self._flush_touches()
                except sqlite3.Error as exc:
                    self._degrade(exc)
            return self._public_result(entry, state)

    def delete(self, namespace: str, key: str) -> None:
        identity = self._validate_identity(namespace, key)
        with self._lock:
            self._ensure_open()
            self._memory_remove(identity)
            try:
                self._delete_disk_identity(identity)
            except sqlite3.Error as exc:
                self._degrade(exc)

    def clear(self, namespace: Optional[str] = None) -> None:
        """Clear one namespace, or all namespaces when omitted."""

        if namespace is not None and (not isinstance(namespace, str) or not namespace):
            raise ValueError("namespace must be a non-empty string")
        with self._lock:
            conn = self._ensure_open()
            if namespace is None:
                self._memory.clear()
                self._memory_bytes = 0
                self._pending_touches.clear()
            else:
                for identity in [item for item in self._memory if item[0] == namespace]:
                    self._memory_remove(identity)
                for identity in [
                    item for item in self._pending_touches if item[0] == namespace
                ]:
                    self._pending_touches.pop(identity, None)
            try:
                if namespace is None:
                    conn.execute(f"DELETE FROM {self._TABLE}")
                else:
                    conn.execute(
                        f"DELETE FROM {self._TABLE} WHERE namespace=?", (namespace,)
                    )
                conn.commit()
            except sqlite3.Error as exc:
                self._degrade(exc)

    def prune(self) -> None:
        """Remove expired entries and enforce byte/count bounds immediately."""

        with self._lock:
            self._ensure_open()
            try:
                self._evict_disk(float(self._clock()))
            except sqlite3.Error as exc:
                self._degrade(exc)

    def stats(self) -> Dict[str, Any]:
        """Return lightweight capacity/debugging information."""

        with self._lock:
            self._ensure_open()
            try:
                row = self._ensure_open().execute(
                    f"SELECT COUNT(*) AS entries, "
                    f"COALESCE(SUM(size_bytes), 0) AS bytes FROM {self._TABLE}"
                ).fetchone()
            except sqlite3.Error as exc:
                self._degrade(exc)
                row = self._ensure_open().execute(
                    f"SELECT COUNT(*) AS entries, "
                    f"COALESCE(SUM(size_bytes), 0) AS bytes FROM {self._TABLE}"
                ).fetchone()
            backend = "memory" if self.path == ":memory:" or self.degraded else "sqlite-wal"
            return {
                "backend": backend,
                "memory_entries": len(self._memory),
                "memory_bytes": self._memory_bytes,
                "disk_entries": int(row["entries"]),
                "disk_bytes": int(row["bytes"]),
                "degraded": self.degraded,
                "last_error": self.last_error,
            }

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                self._flush_touches()
            except sqlite3.Error:
                pass
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self) -> "FreshnessCache":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


__all__ = [
    "FRESH",
    "STALE",
    "MISS",
    "CachePolicy",
    "CacheResult",
    "FreshnessCache",
    "classify_cache_policy",
    "classify_ttl",
    "make_cache_key",
]
