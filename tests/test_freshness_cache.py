import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from freshness_cache import (
    FRESH,
    MISS,
    STALE,
    FreshnessCache,
    classify_cache_policy,
    make_cache_key,
)


class FakeClock:
    def __init__(self, value=1_000.0):
        self.value = float(value)
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def advance(self, seconds):
        with self.lock:
            self.value += seconds


class PolicyTests(unittest.TestCase):
    def test_time_sensitive_query_terms(self):
        ordinary = classify_cache_policy("python tutorial", "serp")
        self.assertEqual(ordinary.category, "serp")
        for query in (
            "latest Python release",
            "AI news today",
            "黄金价格",
            "今日新闻",
            "目前汇率",
        ):
            with self.subTest(query=query):
                policy = classify_cache_policy(query, "serp")
                self.assertEqual(policy.category, "time_sensitive")
                self.assertLess(policy.success_ttl, ordinary.success_ttl)

    def test_resource_categories(self):
        self.assertEqual(
            classify_cache_policy("topic", "community").category, "community"
        )
        self.assertEqual(
            classify_cache_policy(
                "topic", "auto", url="https://www.reddit.com/r/python/"
            ).category,
            "community",
        )
        self.assertEqual(
            classify_cache_policy("topic", "webpage").category, "webpage"
        )
        self.assertEqual(
            classify_cache_policy(
                "topic", "auto", url="https://example.test/report.docx"
            ).category,
            "document",
        )
        self.assertEqual(
            classify_cache_policy(
                "latest report", "auto", url="https://example.test/report.pdf"
            ).category,
            "pdf",
        )

    def test_stability_changes_only_success_ttl(self):
        base = classify_cache_policy("manual", "document")
        stable = classify_cache_policy("manual", "document", unchanged_count=4)
        changed = classify_cache_policy("manual", "document", changed_recently=True)
        self.assertGreater(stable.success_ttl, base.success_ttl)
        self.assertLess(changed.success_ttl, base.success_ttl)
        self.assertEqual(stable.failure_ttl, base.failure_ttl)
        self.assertEqual(stable.stale_ttl, base.stale_ttl)

    def test_cache_key_is_stable_and_order_sensitive(self):
        self.assertEqual(make_cache_key("a", {"x": 1}), make_cache_key("a", {"x": 1}))
        self.assertNotEqual(make_cache_key("a", "b"), make_cache_key("b", "a"))


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "cache.sqlite3"
        self.clock = FakeClock()

    def make_cache(self, **kwargs):
        return FreshnessCache(self.db_path, clock=self.clock, **kwargs)

    def test_fresh_stale_miss_and_metadata(self):
        with self.make_cache() as cache:
            cache.set(
                "google",
                "query",
                {"results": [1, 2]},
                ttl=10,
                stale_ttl=20,
                etag='"abc"',
                last_modified="Sun, 12 Jul 2026 09:00:00 GMT",
                content_hash="content-v1",
            )
            fresh = cache.get("google", "query")
            self.assertEqual(fresh.state, FRESH)
            self.assertTrue(fresh.hit)
            self.assertEqual(fresh.value, {"results": [1, 2]})
            self.assertEqual(fresh.etag, '"abc"')
            self.assertEqual(fresh.last_modified, "Sun, 12 Jul 2026 09:00:00 GMT")
            self.assertEqual(fresh.content_hash, "content-v1")
            self.assertEqual(fresh.expires_at, 1_010.0)
            self.assertEqual(fresh.stale_until, 1_030.0)

            self.clock.advance(11)
            stale = cache.get("google", "query")
            self.assertEqual(stale.state, STALE)
            self.assertEqual(stale.value, {"results": [1, 2]})

            self.clock.advance(20)
            self.assertEqual(cache.get("google", "query").state, MISS)

    def test_failure_ttl_is_independent_and_never_stale(self):
        with self.make_cache() as cache:
            cache.set_failure("reddit", "query", "HTTP 403", ttl=5)
            failure = cache.get("reddit", "query")
            self.assertEqual(failure.state, FRESH)
            self.assertTrue(failure.is_failure)
            self.assertEqual(failure.error, "HTTP 403")
            self.assertIsNone(failure.content_hash)

            self.clock.advance(6)
            self.assertEqual(cache.get("reddit", "query").state, MISS)

    def test_namespace_isolation_and_persistence(self):
        cache = self.make_cache()
        cache.set("google", "same-key", ["g"], ttl=100, stale_ttl=50)
        cache.set("bing", "same-key", ["b"], ttl=100, stale_ttl=50)
        cache.close()

        reopened = self.make_cache()
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get("google", "same-key").value, ["g"])
        self.assertEqual(reopened.get("bing", "same-key").value, ["b"])

        with closing(sqlite3.connect(self.db_path)) as connection:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.casefold(), "wal")

    def test_memory_layer_is_true_lru(self):
        with self.make_cache(memory_max_entries=2, memory_max_bytes=10_000) as cache:
            cache.set("n", "a", "a", ttl=100)
            cache.set("n", "b", "b", ttl=100)
            cache.get("n", "a")
            cache.set("n", "c", "c", ttl=100)
            self.assertEqual(list(cache._memory), [("n", "a"), ("n", "c")])

    def test_memory_eviction_preserves_pending_disk_touch(self):
        with self.make_cache(memory_max_entries=2, memory_max_bytes=10_000) as cache:
            cache.set("n", "a", "a", ttl=100)
            self.clock.advance(1)
            cache.set("n", "b", "b", ttl=100)
            self.clock.advance(9)

            self.assertEqual(cache.get("n", "a").state, FRESH)
            touched_at = self.clock.value
            entry_b = cache._memory[("n", "b")]

            # Force A out of memory without performing a cache write.  Its
            # already-recorded access still has to reach SQLite so disk LRU
            # does not later treat a recently used entry as cold.
            cache.memory_max_entries = 1
            with cache._lock:
                cache._memory_put(entry_b)
                cache._flush_touches()
                row = cache._ensure_open().execute(
                    "SELECT last_accessed FROM freshness_cache_v1 "
                    "WHERE namespace=? AND cache_key=?",
                    ("n", "a"),
                ).fetchone()

            self.assertNotIn(("n", "a"), cache._memory)
            self.assertIsNotNone(row)
            self.assertEqual(touched_at, float(row["last_accessed"]))

    def test_stats_identifies_memory_and_sqlite_wal_backends(self):
        with self.make_cache() as persistent:
            self.assertEqual("sqlite-wal", persistent.stats()["backend"])

        with FreshnessCache(":memory:", clock=self.clock) as memory_only:
            self.assertEqual("memory", memory_only.stats()["backend"])

    def test_disk_lru_and_entry_bound(self):
        cache = self.make_cache(
            memory_max_entries=0,
            memory_max_bytes=0,
            disk_max_entries=2,
            disk_max_bytes=100_000,
        )
        cache.set("n", "a", "a", ttl=100)
        self.clock.advance(1)
        cache.set("n", "b", "b", ttl=100)
        self.clock.advance(1)
        self.assertEqual(cache.get("n", "a").state, FRESH)
        self.clock.advance(1)
        cache.set("n", "c", "c", ttl=100)
        cache.close()

        reopened = self.make_cache(memory_max_entries=0, memory_max_bytes=0)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get("n", "a").state, FRESH)
        self.assertEqual(reopened.get("n", "b").state, MISS)
        self.assertEqual(reopened.get("n", "c").state, FRESH)

    def test_disk_byte_bound(self):
        with self.make_cache(
            memory_max_entries=0,
            memory_max_bytes=0,
            disk_max_entries=100,
            disk_max_bytes=300,
        ) as cache:
            cache.set("n", "a", "x" * 150, ttl=100)
            self.clock.advance(1)
            cache.set("n", "b", "y" * 150, ttl=100)
            stats = cache.stats()
            self.assertLessEqual(stats["disk_bytes"], 300)
            self.assertLessEqual(stats["disk_entries"], 1)

    def test_bad_row_is_discarded_without_raising(self):
        cache = self.make_cache(memory_max_entries=0, memory_max_bytes=0)
        cache.set("n", "bad", {"valid": True}, ttl=100)
        cache.close()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE freshness_cache_v1 SET payload=?, encoding='json' "
                "WHERE namespace='n' AND cache_key='bad'",
                (b"{broken",),
            )
            connection.commit()

        with self.make_cache(memory_max_entries=0, memory_max_bytes=0) as reopened:
            self.assertEqual(reopened.get("n", "bad").state, MISS)
            self.assertEqual(reopened.stats()["disk_entries"], 0)

    def test_corrupt_database_degrades_to_memory(self):
        self.db_path.write_bytes(b"this is not a sqlite database")
        with self.make_cache() as cache:
            self.assertTrue(cache.degraded)
            self.assertTrue(cache.last_error)
            cache.set("n", "key", "usable", ttl=100)
            self.assertEqual(cache.get("n", "key").value, "usable")

    def test_thread_safe_reads_and_writes(self):
        cache = self.make_cache(
            memory_max_entries=128,
            disk_max_entries=1_000,
            disk_max_bytes=10_000_000,
        )
        self.addCleanup(cache.close)
        errors = []

        def worker(worker_id):
            try:
                for item in range(40):
                    key = f"{worker_id}-{item}"
                    cache.set("threads", key, {"item": item}, ttl=100)
                    result = cache.get("threads", key)
                    if result.state != FRESH or result.value != {"item": item}:
                        raise AssertionError(result)
            except Exception as exc:  # captured so failures cross thread boundary
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(item,)) for item in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(cache.stats()["disk_entries"], 240)


if __name__ == "__main__":
    unittest.main()
