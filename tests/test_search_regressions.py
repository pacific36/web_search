"""Offline regression tests for the public search helpers.

These tests intentionally exercise behavior rather than implementation details.  In
particular, they must never open a browser or make a real network request.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import search
from freshness_cache import CacheResult, FRESH, MISS, STALE


class _FakeCache:
    """Minimal deterministic cache double shared by the offline tests."""

    def __init__(self, hits=None) -> None:
        self.hits = dict(hits or {})
        self.get_calls = []
        self.set_calls = []
        self.failure_calls = []

    def get(self, namespace, key):
        self.get_calls.append((namespace, key))
        return self.hits.get(
            (namespace, key),
            self.hits.get(namespace, CacheResult(state=MISS)),
        )

    def set(self, namespace, key, value, **kwargs) -> None:
        self.set_calls.append(
            {
                "namespace": namespace,
                "key": key,
                "value": value,
                **kwargs,
            }
        )

    def set_failure(self, namespace, key, error, **kwargs) -> None:
        self.failure_calls.append(
            {
                "namespace": namespace,
                "key": key,
                "error": error,
                **kwargs,
            }
        )

    def stats(self) -> dict:
        return {
            "backend": "fake",
            "reads": len(self.get_calls),
            "writes": len(self.set_calls),
        }


class AdTextRegressionTests(unittest.TestCase):
    def test_normal_words_containing_ad_are_not_ads(self) -> None:
        normal_texts = (
            "Download the complete technical report",
            "Adobe publishes a new image format",
            "Product roadmap and release notes",
        )

        for text in normal_texts:
            with self.subTest(text=text):
                self.assertFalse(search.is_ad_text(text))

    def test_explicit_ad_labels_are_detected(self) -> None:
        ad_texts = (
            "广告",
            "推广：限时优惠",
            "Sponsored result",
            "Promoted by Example Corp",
        )

        for text in ad_texts:
            with self.subTest(text=text):
                self.assertTrue(search.is_ad_text(text))


class UrlNormalizationRegressionTests(unittest.TestCase):
    def test_semantic_query_parameters_are_preserved_and_tracking_is_removed(self) -> None:
        normalized = search.normalize_url(
            "https://Example.com/watch/?v=abc123&id=42&page=3"
            "&utm_source=newsletter&utm_medium=email&gclid=tracking"
        )

        self.assertIn("v=abc123", normalized)
        self.assertIn("id=42", normalized)
        self.assertIn("page=3", normalized)
        self.assertNotIn("utm_source", normalized.lower())
        self.assertNotIn("utm_medium", normalized.lower())
        self.assertNotIn("gclid", normalized.lower())

    def test_tracking_values_do_not_change_identity_but_semantic_values_do(self) -> None:
        first = search.normalize_url(
            "https://example.com/watch?v=one&page=2&utm_source=google"
        )
        second = search.normalize_url(
            "https://example.com/watch?page=2&utm_source=bing&v=one"
        )
        different_video = search.normalize_url(
            "https://example.com/watch?v=two&page=2&utm_source=google"
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, different_video)

    def test_scheme_and_www_are_preserved_as_distinct_origins(self) -> None:
        http_www = search.normalize_url("http://www.example.com/report")
        https_www = search.normalize_url("https://www.example.com/report")
        https_apex = search.normalize_url("https://example.com/report")

        self.assertEqual("http://www.example.com/report", http_www)
        self.assertEqual("https://www.example.com/report", https_www)
        self.assertEqual("https://example.com/report", https_apex)
        self.assertEqual(3, len({http_www, https_www, https_apex}))


class ResultMergeRegressionTests(unittest.TestCase):
    def test_same_title_on_different_domains_is_not_merged(self) -> None:
        raw = [
            {
                "engine": "Google",
                "title": "Project documentation",
                "url": "https://docs.example.com/project",
                "snippet": "Official project documentation.",
            },
            {
                "engine": "Bing",
                "title": "Project documentation",
                "url": "https://community.example.net/project",
                "snippet": "An independent community guide.",
            },
        ]

        merged = search.merge_search_results(copy.deepcopy(raw))

        self.assertEqual(2, len(merged))
        self.assertEqual(
            {"docs.example.com", "community.example.net"},
            {urllib.request.urlparse(item["url"]).netloc for item in merged},
        )

    def test_same_canonical_url_is_merged_and_all_engines_are_retained(self) -> None:
        canonical = "https://example.com/reports/complete"
        raw = [
            {
                "engine": "Google",
                "title": "Complete report",
                "url": "https://example.com/report?utm_source=google",
                "canonical_url": canonical,
                "snippet": "Short summary.",
            },
            {
                "engine": "Bing",
                "title": "The complete report",
                "url": "https://example.com/report/index.html?utm_source=bing",
                "canonical_url": canonical,
                "snippet": "A longer and more useful summary of the report.",
            },
        ]

        merged = search.merge_search_results(copy.deepcopy(raw))

        self.assertEqual(1, len(merged))
        self.assertEqual({"Google", "Bing"}, set(merged[0]["engines"]))

    def test_later_specialized_result_keeps_type_metadata_and_open_pdf(self) -> None:
        canonical = "https://example.org/papers/complete-study"
        raw = [
            {
                "engine": "Google",
                "title": "Complete study",
                "url": canonical,
                "snippet": "General web result.",
                "type": "organic",
                "metadata": {"language": "en"},
            },
            {
                "engine": "OpenAlex",
                "title": "Complete study",
                "url": canonical,
                "snippet": "Indexed scholarly record.",
                "type": "academic",
                "metadata": {"doi": "10.1234/example", "cited_by_count": 17},
                "open_access_pdf": "https://example.org/papers/complete-study.pdf",
            },
        ]

        merged = search.merge_search_results(copy.deepcopy(raw))

        self.assertEqual(1, len(merged))
        self.assertEqual("academic", merged[0]["type"])
        self.assertEqual("en", merged[0]["metadata"]["language"])
        self.assertEqual("10.1234/example", merged[0]["metadata"]["doi"])
        self.assertEqual(17, merged[0]["metadata"]["cited_by_count"])
        self.assertEqual(
            "https://example.org/papers/complete-study.pdf",
            merged[0]["open_access_pdf"],
        )


class SemanticScholarRegressionTests(unittest.TestCase):
    @staticmethod
    def _response(payload: dict) -> mock.MagicMock:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = json.dumps(payload).encode("utf-8")
        return response

    def test_successful_response_returns_results_error_tuple(self) -> None:
        payload = {
            "data": [
                {
                    "title": "A Useful Paper",
                    "abstract": "Useful evidence from the paper.",
                    "year": 2026,
                    "citationCount": 7,
                    "url": "https://www.semanticscholar.org/paper/useful",
                    "externalIds": {"DOI": "10.1000/useful"},
                }
            ]
        }

        with mock.patch.object(
            search, "_guarded_urlopen", return_value=self._response(payload)
        ) as mocked_urlopen:
            returned = search.search_semantic_scholar("useful paper", limit=1)

        self.assertIsInstance(returned, tuple)
        self.assertEqual(2, len(returned))
        results, error = returned
        self.assertIsNone(error)
        self.assertEqual(1, len(results))
        self.assertEqual("A Useful Paper", results[0]["title"])
        mocked_urlopen.assert_called_once()

    def test_successful_empty_response_still_returns_tuple(self) -> None:
        with mock.patch.object(
            search, "_guarded_urlopen", return_value=self._response({"data": []})
        ):
            returned = search.search_semantic_scholar("no matches", limit=1)

        self.assertEqual(([], None), returned)


class QueryExpansionRegressionTests(unittest.TestCase):
    def test_expansions_are_unique_and_cover_broad_search_directions(self) -> None:
        expansions = search.generate_expansion_queries("量子计算", vendor=None)
        normalized = [item.strip().casefold() for item in expansions]

        self.assertEqual(len(normalized), len(set(normalized)))
        self.assertTrue(
            any(any("\u4e00" <= char <= "\u9fff" for char in item) for item in expansions),
            "expected at least one Chinese-oriented expansion",
        )
        self.assertTrue(
            any(
                token in item.casefold()
                for item in expansions
                for token in ("release", "official", "documentation", "benchmark")
            ),
            "expected at least one English-oriented expansion",
        )
        self.assertTrue(
            any("official" in item.casefold() or "官方" in item for item in expansions),
            "expected an official-source expansion",
        )
        self.assertTrue(
            any("filetype:" in item.casefold() for item in expansions),
            "expected a filetype resource expansion",
        )


class SufficiencyRegressionTests(unittest.TestCase):
    CATEGORIES = (
        "official_open_source",
        "official_commercial_api",
        "official_blog_docs",
        "community_results",
    )

    @classmethod
    def _result(cls, rows: list[dict]) -> dict:
        result = {category: [] for category in cls.CATEGORIES}
        for index, row in enumerate(rows):
            result[cls.CATEGORIES[index % len(cls.CATEGORIES)]].append(row)
        return result

    def test_duplicate_urls_do_not_count_as_independent_sources(self) -> None:
        content = "quantum computing evidence and analysis " * 45
        rows = [
            {
                "url": f"https://example.com/report?utm_source=channel{index}",
                "content": content + f" section {index}",
            }
            for index in range(3)
        ]

        sufficient, _reason = search.assess_sufficiency(
            self._result(rows), "quantum computing"
        )

        self.assertFalse(sufficient)

    def test_duplicate_bodies_on_different_urls_do_not_fake_coverage(self) -> None:
        duplicated_content = "quantum computing evidence and analysis " * 45
        rows = [
            {
                "url": f"https://source{index}.example/report",
                "content": duplicated_content,
            }
            for index in range(3)
        ]

        sufficient, _reason = search.assess_sufficiency(
            self._result(rows), "quantum computing"
        )

        self.assertFalse(sufficient)


class ChannelCacheRegressionTests(unittest.TestCase):
    @staticmethod
    def _row(engine: str, suffix: str) -> dict:
        return {
            "engine": engine,
            "title": f"Result {suffix}",
            "url": f"https://{engine.casefold()}.example/{suffix}",
            "snippet": f"{engine} cached result",
        }

    def test_same_query_uses_independent_per_channel_cache_namespaces(self) -> None:
        cache = _FakeCache(
            {
                "channel:google": CacheResult(
                    state=FRESH, value=[self._row("Google", "cached")]
                ),
                "channel:bing": CacheResult(
                    state=FRESH, value=[self._row("Bing", "cached")]
                ),
            }
        )
        google_live = mock.Mock(side_effect=AssertionError("fresh cache must win"))
        bing_live = mock.Mock(side_effect=AssertionError("fresh cache must win"))

        with mock.patch.object(search, "_get_persistent_cache", return_value=cache), \
                mock.patch.object(search, "_force_fresh", False):
            google, google_error, google_state = search._run_channel_cached(
                "Google", google_live, "same query", 10
            )
            bing, bing_error, bing_state = search._run_channel_cached(
                "Bing", bing_live, "same query", 10
            )

        self.assertEqual("Google", google[0]["engine"])
        self.assertEqual("Bing", bing[0]["engine"])
        self.assertIsNone(google_error)
        self.assertIsNone(bing_error)
        self.assertEqual(("fresh", "fresh"), (google_state, bing_state))
        self.assertEqual(
            ["channel:google", "channel:bing"],
            [namespace for namespace, _key in cache.get_calls],
        )
        google_live.assert_not_called()
        bing_live.assert_not_called()

    def test_fresh_hit_does_not_call_live_channel(self) -> None:
        cached = self._row("OpenAlex", "fresh")
        cache = _FakeCache(
            {"channel:openalex": CacheResult(state=FRESH, value=[cached])}
        )
        live = mock.Mock(side_effect=AssertionError("network channel was called"))

        with mock.patch.object(search, "_get_persistent_cache", return_value=cache), \
                mock.patch.object(search, "_force_fresh", False):
            results, error, state = search._run_channel_cached(
                "OpenAlex", live, "stable research topic", 5
            )

        self.assertEqual("fresh", state)
        self.assertIsNone(error)
        self.assertEqual("fresh", results[0]["cache_state"])
        live.assert_not_called()
        self.assertEqual([], cache.set_calls)

    def test_stale_results_are_merged_with_successful_live_results(self) -> None:
        stale = self._row("Google", "older")
        live = self._row("Google", "newer")
        cache = _FakeCache(
            {"channel:google": CacheResult(state=STALE, value=[stale])}
        )
        provider = mock.Mock(return_value=([live], None))

        with mock.patch.object(search, "_get_persistent_cache", return_value=cache), \
                mock.patch.object(search, "_force_fresh", False):
            results, error, state = search._run_channel_cached(
                "Google", provider, "stable research topic", 10
            )

        self.assertEqual("live+stale", state)
        self.assertIsNone(error)
        self.assertEqual(
            {stale["url"], live["url"]},
            {row["url"] for row in results},
        )
        self.assertEqual({"stale", "live"}, {row["cache_state"] for row in results})
        provider.assert_called_once_with("stable research topic", 10)
        self.assertEqual(1, len(cache.set_calls))

    def test_time_sensitive_query_is_written_with_short_ttl(self) -> None:
        query = "最新 AI 模型价格"
        cache = _FakeCache()
        provider = mock.Mock(return_value=([self._row("Google", "latest")], None))

        with mock.patch.object(search, "_get_persistent_cache", return_value=cache), \
                mock.patch.object(search, "_force_fresh", False):
            _results, error, state = search._run_channel_cached(
                "Google", provider, query, 10
            )

        time_policy = search.classify_cache_policy(query, "serp")
        stable_policy = search.classify_cache_policy("stable research topic", "serp")
        self.assertIsNone(error)
        self.assertEqual("live", state)
        self.assertLess(time_policy.success_ttl, stable_policy.success_ttl)
        self.assertEqual(time_policy.success_ttl, cache.set_calls[0]["ttl"])
        self.assertLessEqual(cache.set_calls[0]["ttl"], 5 * 60)

    def test_partial_success_error_survives_a_fresh_cache_round_trip(self) -> None:
        row = self._row("Baidu", "partial")
        warning = "Baidu HTTP 429 after collecting partial results"
        first_cache = _FakeCache()
        provider = mock.Mock(return_value=([row], warning))

        with mock.patch.object(search, "_get_persistent_cache", return_value=first_cache), \
                mock.patch.object(search, "_force_fresh", False):
            first_results, first_error, first_state = search._run_channel_cached(
                "Baidu", provider, "partial cache topic", 5
            )

        self.assertEqual("live", first_state)
        self.assertEqual(warning, first_error)
        self.assertEqual(row["url"], first_results[0]["url"])
        self.assertEqual(1, len(first_cache.set_calls))

        written_value = first_cache.set_calls[0]["value"]
        second_cache = _FakeCache(
            {"channel:baidu": CacheResult(state=FRESH, value=written_value)}
        )
        should_not_run = mock.Mock(
            side_effect=AssertionError("fresh partial-success cache must win")
        )
        with mock.patch.object(search, "_get_persistent_cache", return_value=second_cache), \
                mock.patch.object(search, "_force_fresh", False):
            cached_results, cached_error, cached_state = search._run_channel_cached(
                "Baidu", should_not_run, "partial cache topic", 5
            )

        self.assertEqual("fresh", cached_state)
        self.assertEqual(warning, cached_error)
        self.assertEqual(row["url"], cached_results[0]["url"])
        should_not_run.assert_not_called()

    def test_successful_empty_result_is_cached_and_skips_next_live_call(self) -> None:
        first_cache = _FakeCache()
        provider = mock.Mock(return_value=([], None))

        with mock.patch.object(search, "_get_persistent_cache", return_value=first_cache), \
                mock.patch.object(search, "_force_fresh", False):
            first_results, first_error, first_state = search._run_channel_cached(
                "GitHub", provider, "no matching repositories", 5
            )

        self.assertEqual([], first_results)
        self.assertIsNone(first_error)
        self.assertEqual("live", first_state)
        self.assertEqual(1, len(first_cache.set_calls))

        written_value = first_cache.set_calls[0]["value"]
        second_cache = _FakeCache(
            {"channel:github": CacheResult(state=FRESH, value=written_value)}
        )
        should_not_run = mock.Mock(
            side_effect=AssertionError("fresh successful-empty cache must win")
        )
        with mock.patch.object(search, "_get_persistent_cache", return_value=second_cache), \
                mock.patch.object(search, "_force_fresh", False):
            cached_results, cached_error, cached_state = search._run_channel_cached(
                "GitHub", should_not_run, "no matching repositories", 5
            )

        self.assertEqual([], cached_results)
        self.assertIsNone(cached_error)
        self.assertEqual("fresh", cached_state)
        should_not_run.assert_not_called()

    def test_force_fresh_success_excludes_old_rows_but_failure_falls_back(self) -> None:
        old = self._row("Google", "old-cached")
        new = self._row("Google", "new-live")
        hit = CacheResult(state=FRESH, value=[old])

        success_cache = _FakeCache({"channel:google": hit})
        successful_provider = mock.Mock(return_value=([new], None))
        with mock.patch.object(
            search, "_get_persistent_cache", return_value=success_cache
        ), mock.patch.object(search, "_force_fresh", True):
            results, error, state = search._run_channel_cached(
                "Google", successful_provider, "forced refresh topic", 5
            )

        self.assertIsNone(error)
        self.assertEqual("live", state)
        self.assertEqual([new["url"]], [row["url"] for row in results])
        self.assertNotIn(old["url"], {row["url"] for row in results})

        failure_cache = _FakeCache({"channel:google": hit})
        failed_provider = mock.Mock(return_value=([], "Google HTTP 503"))
        with mock.patch.object(
            search, "_get_persistent_cache", return_value=failure_cache
        ), mock.patch.object(search, "_force_fresh", True):
            fallback, fallback_error, fallback_state = search._run_channel_cached(
                "Google", failed_provider, "forced refresh topic", 5
            )

        self.assertEqual("Google HTTP 503", fallback_error)
        self.assertEqual("stale", fallback_state)
        self.assertEqual([old["url"]], [row["url"] for row in fallback])


class AdditionalAcademicChannelRegressionTests(unittest.TestCase):
    @staticmethod
    def _response(payload: dict) -> mock.MagicMock:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = json.dumps(payload).encode("utf-8")
        return response

    def test_crossref_valid_json_returns_parsed_tuple(self) -> None:
        payload = {
            "message": {
                "items": [
                    {
                        "DOI": "10.1000/crossref-test",
                        "title": ["Crossref Paper"],
                        "abstract": "<jats:p>Evidence from Crossref.</jats:p>",
                        "published": {"date-parts": [[2026, 7, 1]]},
                        "URL": "https://doi.org/10.1000/crossref-test",
                        "type": "journal-article",
                        "is-referenced-by-count": 12,
                    }
                ]
            }
        }

        with mock.patch.object(
            search, "_guarded_urlopen", return_value=self._response(payload)
        ):
            returned = search.search_crossref("test paper", limit=3)

        self.assertIsInstance(returned, tuple)
        self.assertEqual(2, len(returned))
        results, error = returned
        self.assertIsNone(error)
        self.assertEqual("Crossref Paper", results[0]["title"])
        self.assertEqual("10.1000/crossref-test", results[0]["metadata"]["doi"])

    def test_openalex_valid_json_returns_parsed_tuple(self) -> None:
        payload = {
            "results": [
                {
                    "id": "https://openalex.org/W123",
                    "display_name": "OpenAlex Paper",
                    "doi": "https://doi.org/10.1000/openalex-test",
                    "publication_year": 2026,
                    "cited_by_count": 8,
                    "authorships": [
                        {"author": {"display_name": "Example Author"}}
                    ],
                    "primary_location": {
                        "landing_page_url": "https://example.org/paper",
                        "source": {"display_name": "Example Journal"},
                    },
                    "best_oa_location": {
                        "pdf_url": "https://example.org/paper.pdf"
                    },
                }
            ]
        }

        with mock.patch.object(
            search, "_guarded_urlopen", return_value=self._response(payload)
        ):
            returned = search.search_openalex("test paper", limit=3)

        self.assertIsInstance(returned, tuple)
        self.assertEqual(2, len(returned))
        results, error = returned
        self.assertIsNone(error)
        self.assertEqual("OpenAlex Paper", results[0]["title"])
        self.assertEqual(
            "https://example.org/paper.pdf",
            results[0]["metadata"]["open_access_pdf"],
        )

    def test_valid_empty_academic_payloads_still_return_tuples(self) -> None:
        cases = (
            (search.search_crossref, {"message": {"items": []}}),
            (search.search_openalex, {"results": []}),
        )

        for function, payload in cases:
            with self.subTest(channel=function.__name__), mock.patch.object(
                search, "_guarded_urlopen", return_value=self._response(payload)
            ):
                self.assertEqual(([], None), function("no matches", limit=2))


class HtmlAdRegressionTests(unittest.TestCase):
    def test_read_more_aria_label_is_not_an_ad(self) -> None:
        html = '<a href="/article" aria-label="Read more">Read more</a>'
        self.assertFalse(search.is_ad(html, "Read more"))

    def test_ordinary_commercial_wording_is_not_an_ad(self) -> None:
        html = "<article><h2>Commercial aviation outlook</h2></article>"
        text = "Commercial viability and market analysis for aircraft."
        self.assertFalse(search.is_ad(html, text))

    def test_explicit_advertisement_role_is_detected(self) -> None:
        html = '<aside role="advertisement">Limited offer</aside>'
        self.assertTrue(search.is_ad(html, "Limited offer"))


class FingerprintStateRegressionTests(unittest.TestCase):
    def test_cooldown_is_isolated_by_engine(self) -> None:
        with mock.patch.dict(search._fp_cooldown, {}, clear=True), \
                mock.patch.dict(search._fp_failures, {}, clear=True), \
                mock.patch.dict(search._fp_last_used, {}, clear=True), \
                mock.patch.object(search._time, "time", return_value=1_000.0), \
                mock.patch.object(search.random, "uniform", return_value=0.0), \
                mock.patch.object(search.random, "choice", side_effect=lambda rows: min(rows)):
            search._cooldown_fingerprint(0, "google", base_seconds=30)

            google_index, _google_fingerprint = search._get_fingerprint("google")
            bing_index, _bing_fingerprint = search._get_fingerprint("bing")

            self.assertIn(("google", 0), search._fp_cooldown)
            self.assertNotIn(("bing", 0), search._fp_cooldown)
            self.assertNotEqual(0, google_index)
            self.assertEqual(0, bing_index)

    def test_success_clears_only_that_engine_failure_state(self) -> None:
        with mock.patch.dict(search._fp_cooldown, {}, clear=True), \
                mock.patch.dict(search._fp_failures, {}, clear=True), \
                mock.patch.object(search._time, "time", return_value=1_000.0), \
                mock.patch.object(search.random, "uniform", return_value=0.0):
            search._cooldown_fingerprint(2, "google", base_seconds=30)
            search._cooldown_fingerprint(2, "bing", base_seconds=30)
            search._mark_fingerprint_success(2, "google")

            self.assertNotIn(("google", 2), search._fp_failures)
            self.assertNotIn(("google", 2), search._fp_cooldown)
            self.assertEqual(1, search._fp_failures[("bing", 2)])
            self.assertIn(("bing", 2), search._fp_cooldown)


class ExtractionMetadataRegressionTests(unittest.TestCase):
    def test_fresh_cached_document_exposes_metadata_and_discovered_links(self) -> None:
        requested_url = "https://example.com/article?utm_source=test"
        document = {
            "content": "Complete offline article body with useful evidence.",
            "requested_url": requested_url,
            "final_url": "https://example.com/article",
            "canonical_url": "https://example.com/canonical-article",
            "title": "Offline article",
            "content_type": "text/html; charset=utf-8",
            "content_chars": 51,
            "content_hash": "offline-content-hash",
            "truncated": False,
            "links": [
                {
                    "url": "https://example.com/report.pdf",
                    "kind": "document",
                    "anchor": "Full report",
                    "rel": [],
                    "mime_type": "application/pdf",
                },
                {
                    "url": "https://example.com/canonical-article",
                    "kind": "canonical",
                    "anchor": "",
                    "rel": ["canonical"],
                    "mime_type": "",
                },
            ],
            "cache_state": "live",
        }
        cache = _FakeCache(
            {"content": CacheResult(state=FRESH, value=document)}
        )

        with mock.patch.dict(search._extraction_metadata, {}, clear=True), \
                mock.patch.object(search, "_get_persistent_cache", return_value=cache), \
                mock.patch.object(search, "_force_fresh", False):
            content = search.playwright_extract_content(
                requested_url, max_chars=500, query="stable topic"
            )
            metadata = search.get_extraction_metadata(
                "https://example.com/article?utm_medium=equivalent"
            )

        self.assertEqual(document["content"], content)
        self.assertNotIn("content", metadata)
        self.assertEqual("fresh", metadata["cache_state"])
        self.assertEqual(document["canonical_url"], metadata["canonical_url"])
        self.assertEqual(document["links"], metadata["links"])
        self.assertEqual("application/pdf", metadata["links"][0]["mime_type"])

    def test_old_long_ttl_hit_is_revalidated_under_current_time_sensitive_policy(self) -> None:
        url = "https://example.com/architecture-report"
        stable_policy = search.classify_cache_policy(
            "architecture reference", "auto", url=url
        )
        current_query = "latest architecture report today"
        current_policy = search.classify_cache_policy(
            current_query, "auto", url=url
        )
        self.assertLess(current_policy.success_ttl, stable_policy.success_ttl)

        now = search._time.time()
        created_at = now - current_policy.success_ttl - 5
        document = {
            "content": "Previously cached architecture report.",
            "requested_url": url,
            "final_url": url,
            "canonical_url": url,
            "content_type": "text/html",
            "content_hash": "stable-document-hash",
            "links": [],
        }
        cache = _FakeCache(
            {
                "content": CacheResult(
                    state=FRESH,
                    value=document,
                    created_at=created_at,
                    expires_at=created_at + stable_policy.success_ttl,
                    stale_until=created_at
                    + stable_policy.success_ttl
                    + stable_policy.stale_ttl,
                    etag='"stable-etag"',
                    content_hash="stable-document-hash",
                )
            }
        )
        revalidate = mock.Mock(return_value=True)

        with mock.patch.dict(search._extraction_metadata, {}, clear=True), \
                mock.patch.object(search, "_get_persistent_cache", return_value=cache), \
                mock.patch.object(search, "_force_fresh", False), \
                mock.patch.object(search, "_is_safe_fetch_url", return_value=True), \
                mock.patch.object(
                    search, "_revalidate_cached_resource", revalidate
                ):
            content = search.playwright_extract_content(
                url, max_chars=500, query=current_query
            )

        self.assertEqual(document["content"], content)
        revalidate.assert_called_once()
        self.assertEqual(
            "time_sensitive", revalidate.call_args.args[-1].category
        )


class ResourceRevalidationRegressionTests(unittest.TestCase):
    def test_changed_etag_wins_over_unchanged_last_modified(self) -> None:
        url = "https://example.com/versioned-report"
        last_modified = "Sun, 12 Jul 2026 09:00:00 GMT"
        hit = CacheResult(
            state=STALE,
            value={"content": "cached report"},
            etag='"version-one"',
            last_modified=last_modified,
            content_hash="cached-hash",
        )
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.headers = {
            "ETag": '"version-two"',
            "Last-Modified": last_modified,
        }
        cache = _FakeCache()
        policy = search.classify_cache_policy("stable report", "webpage", url=url)

        with mock.patch.object(search, "_guarded_urlopen", return_value=response):
            unchanged = search._revalidate_cached_resource(
                url,
                hit,
                cache,
                "offline-resource-key",
                policy,
            )

        self.assertFalse(unchanged)
        self.assertEqual([], cache.set_calls)


class ContentHttpStatusRegressionTests(unittest.TestCase):
    def test_non_captcha_error_status_is_never_cached_as_success(self) -> None:
        body_text = "Useful-looking error body evidence. " * 30
        html = "<html><body>" + body_text + "</body></html>"
        fake_trafilatura = types.SimpleNamespace(
            extract=lambda _html, **_kwargs: body_text
        )

        for status in (403, 429, 500, 503):
            with self.subTest(status=status):
                url = f"https://status-{status}.example/report"
                cache = _FakeCache()
                response = mock.MagicMock(name=f"response_{status}")
                response.status = status
                response.headers = {"content-type": "text/html"}
                page = mock.MagicMock(name=f"page_{status}")
                page.goto.return_value = response
                page.content.return_value = html
                page.url = url
                page.title.return_value = "Error response disguised as content"
                context = mock.MagicMock(name=f"context_{status}")
                context.new_page.return_value = page
                browser = mock.MagicMock(name=f"browser_{status}")
                manager = mock.MagicMock(name=f"playwright_manager_{status}")
                manager.__enter__.return_value = types.SimpleNamespace()

                with mock.patch.dict(sys.modules, {"trafilatura": fake_trafilatura}), \
                        mock.patch.dict(search._extraction_metadata, {}, clear=True), \
                        mock.patch.object(search, "_get_persistent_cache", return_value=cache), \
                        mock.patch.object(search, "_force_fresh", False), \
                        mock.patch.object(
                            search,
                            "_new_stealth_browser",
                            return_value=(browser, context, 0),
                        ), \
                        mock.patch.object(search, "_is_captcha_page", return_value=False), \
                        mock.patch.object(search, "_element_text", return_value=body_text), \
                        mock.patch.object(search, "_cooldown_fingerprint"), \
                        mock.patch.object(search, "_mark_fingerprint_success"), \
                        mock.patch(
                            "playwright.sync_api.sync_playwright",
                            return_value=manager,
                        ):
                    content = search.playwright_extract_content(
                        url, max_chars=5_000, query="stable report"
                    )

                self.assertIsNone(content)
                self.assertEqual([], cache.set_calls)


class PrivateUrlGuardRegressionTests(unittest.TestCase):
    @staticmethod
    def _playwright_manager():
        manager = mock.MagicMock(name="private_url_playwright_manager")
        manager.__enter__.return_value = types.SimpleNamespace()
        return manager

    @staticmethod
    def _public_dns(*args, **_kwargs):
        del _kwargs
        host = str(args[0]) if args else "public.example"
        address = host if host in {"127.0.0.1", "192.168.1.50"} else "93.184.216.34"
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, 443),
            )
        ]

    @staticmethod
    def _response(data: bytes, final_url: str, headers=None):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = data
        response.geturl.return_value = final_url
        response.headers = dict(headers or {})
        return response

    def test_literal_private_url_is_rejected_before_browser_and_override_allows_it(self) -> None:
        private_url = "http://127.0.0.1/internal-admin"
        blocked_cache = _FakeCache()
        blocked_factory = mock.Mock(name="blocked_private_browser_factory")
        blocked_manager = self._playwright_manager()
        blocked_playwright = mock.Mock(return_value=blocked_manager)

        with mock.patch.dict(
            os.environ, {"WEB_SEARCH_ALLOW_PRIVATE_URLS": "0"}, clear=False
        ), mock.patch.object(
            search, "_get_persistent_cache", return_value=blocked_cache
        ), mock.patch.object(
            search, "_force_fresh", False
        ), mock.patch.object(
            search, "_new_stealth_browser", blocked_factory
        ), mock.patch(
            "playwright.sync_api.sync_playwright", blocked_playwright
        ):
            blocked = search.playwright_extract_content(
                private_url, max_chars=500, query="security review"
            )

        self.assertIsNone(blocked)
        blocked_playwright.assert_not_called()
        blocked_factory.assert_not_called()

        allowed_cache = _FakeCache()
        allowed_manager = self._playwright_manager()
        allowed_playwright = mock.Mock(return_value=allowed_manager)
        allowed_factory = mock.Mock(
            side_effect=RuntimeError("offline stop after private-url opt-in")
        )
        with mock.patch.dict(
            os.environ, {"WEB_SEARCH_ALLOW_PRIVATE_URLS": "1"}, clear=False
        ), mock.patch.object(
            search, "_get_persistent_cache", return_value=allowed_cache
        ), mock.patch.object(
            search, "_force_fresh", False
        ), mock.patch.object(
            search, "_new_stealth_browser", allowed_factory
        ), mock.patch.object(
            search, "_cooldown_fingerprint"
        ), mock.patch(
            "playwright.sync_api.sync_playwright", allowed_playwright
        ):
            search.playwright_extract_content(
                private_url, max_chars=500, query="local development"
            )

        allowed_playwright.assert_called()
        allowed_factory.assert_called()

    def test_hostname_resolving_to_private_ip_is_rejected_before_browser(self) -> None:
        cache = _FakeCache()
        browser_factory = mock.Mock(name="dns_private_browser_factory")
        manager = self._playwright_manager()
        playwright_entry = mock.Mock(return_value=manager)
        private_dns = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.23.45.67", 443),
            )
        ]

        with mock.patch.dict(
            os.environ, {"WEB_SEARCH_ALLOW_PRIVATE_URLS": "0"}, clear=False
        ), mock.patch("socket.getaddrinfo", return_value=private_dns), \
                mock.patch.object(search, "_get_persistent_cache", return_value=cache), \
                mock.patch.object(search, "_force_fresh", False), \
                mock.patch.object(search, "_new_stealth_browser", browser_factory), \
                mock.patch(
                    "playwright.sync_api.sync_playwright", playwright_entry
                ):
            content = search.playwright_extract_content(
                "https://public-name.invalid/report",
                max_chars=500,
                query="security review",
            )

        self.assertIsNone(content)
        playwright_entry.assert_not_called()
        browser_factory.assert_not_called()

    def test_dns_public_decision_is_not_cached_across_rebinding(self) -> None:
        public_dns = self._public_dns("rebind.example", 443)
        private_dns = [
            (
                socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                ("127.0.0.1", 443),
            )
        ]
        with mock.patch.dict(
            os.environ, {"WEB_SEARCH_ALLOW_PRIVATE_URLS": "0"}, clear=False
        ), mock.patch(
            "socket.getaddrinfo", side_effect=[public_dns, private_dns]
        ) as resolver:
            self.assertTrue(
                search._is_safe_fetch_url("https://rebind.example/resource")
            )
            self.assertFalse(
                search._is_safe_fetch_url("https://rebind.example/resource")
            )

        self.assertEqual(2, resolver.call_count)

    def test_pdf_redirect_to_private_ip_is_not_downloaded_or_cached(self) -> None:
        public_pdf = "https://public.example/report.pdf"
        private_final = "http://127.0.0.1/internal/report.pdf"
        pdf_bytes = b"%PDF-1.7\nredirected private payload"
        fake_pypdf = types.SimpleNamespace(
            PdfReader=mock.Mock(
                return_value=types.SimpleNamespace(
                    pages=[types.SimpleNamespace(extract_text=lambda: "private PDF text")]
                )
            )
        )
        cache = _FakeCache()

        with mock.patch.dict(
            os.environ, {"WEB_SEARCH_ALLOW_PRIVATE_URLS": "0"}, clear=False
        ), mock.patch.dict(sys.modules, {"pypdf": fake_pypdf}), \
                mock.patch("socket.getaddrinfo", side_effect=self._public_dns), \
                mock.patch.object(
                    search,
                    "_guarded_urlopen",
                    side_effect=search.urllib.error.URLError(
                        f"blocked redirect to {private_final}"
                    ),
                ) as guarded_open, \
                mock.patch.object(search, "_get_persistent_cache", return_value=cache), \
                mock.patch.object(search, "_force_fresh", False), \
                mock.patch.dict(search._extraction_metadata, {}, clear=True):
            content = search.playwright_extract_content(
                public_pdf, max_chars=5_000, query="security review"
            )

        self.assertIsNone(content)
        self.assertEqual([], cache.set_calls)
        self.assertEqual({}, search.get_extraction_metadata(public_pdf))
        guarded_open.assert_called_once()

    def test_head_revalidation_redirect_to_private_ip_is_rejected(self) -> None:
        public_url = "https://public.example/versioned-report"
        private_final = "http://192.168.1.50/internal-version"
        hit = CacheResult(
            state=STALE,
            value={"content": "cached public report"},
            etag='"version-one"',
            last_modified="Sun, 12 Jul 2026 09:00:00 GMT",
            content_hash="public-report-hash",
        )
        cache = _FakeCache()
        policy = search.classify_cache_policy(
            "security review", "webpage", url=public_url
        )

        with mock.patch.dict(
            os.environ, {"WEB_SEARCH_ALLOW_PRIVATE_URLS": "0"}, clear=False
        ), mock.patch("socket.getaddrinfo", side_effect=self._public_dns), \
                mock.patch.object(
                    search,
                    "_guarded_urlopen",
                    side_effect=search.urllib.error.URLError(
                        f"blocked redirect to {private_final}"
                    ),
                ) as guarded_open:
            unchanged = search._revalidate_cached_resource(
                public_url, hit, cache, "private-redirect-head", policy
            )

        self.assertFalse(unchanged)
        self.assertEqual([], cache.set_calls)
        guarded_open.assert_called_once()

    def test_guarded_opener_rejects_a_private_final_redirect(self) -> None:
        public_url = "https://public.example/start"
        private_final = "http://127.0.0.1/internal-target"
        response = self._response(
            b"", public_url, {"Location": private_final}
        )
        response.status = 302
        response.reason = "Found"
        request = urllib.request.Request(public_url)

        with mock.patch.dict(
            os.environ, {"WEB_SEARCH_ALLOW_PRIVATE_URLS": "0"}, clear=False
        ), mock.patch.object(
            search, "_pinned_open_once", return_value=response
        ) as pinned_open:
            with self.assertRaises(search.urllib.error.URLError):
                search._guarded_urlopen(request, timeout=5)

        response.close.assert_called_once()
        pinned_open.assert_called_once()


class AllChannelOrchestrationRegressionTests(unittest.TestCase):
    # Keys must stay in providers-list order: the first BROWSER_CHANNEL_COUNT
    # entries are the sequential browser group, the rest the parallel API group.
    # SemanticScholar and Reddit are credential-gated and absent by default.
    BROWSER_CHANNEL_COUNT = 4
    CHANNEL_FUNCTIONS = {
        "playwright_google_search": "Google",
        "playwright_bing_search": "Bing",
        "playwright_baidu_search": "Baidu",
        "search_zhihu": "Zhihu",
        "search_arxiv": "arXiv",
        "search_crossref": "Crossref",
        "search_openalex": "OpenAlex",
        "search_github": "GitHub",
        "search_hackernews": "HackerNews",
        "search_csdn": "CSDN",
        "search_stackoverflow": "StackOverflow",
        "search_v2ex": "V2EX",
        "search_juejin": "Juejin",
    }
    CREDENTIAL_CHANNELS = {
        "search_semantic_scholar": ("SemanticScholar", "SEMANTIC_SCHOLAR_API_KEY"),
        "search_reddit": ("Reddit", "REDDIT_ACCESS_TOKEN"),
    }

    def setUp(self) -> None:
        patcher = mock.patch.dict(
            os.environ, {}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for _, env_name in self.CREDENTIAL_CHANNELS.values():
            os.environ.pop(env_name, None)

    def test_all_channels_run_even_when_one_fails(self) -> None:
        providers = {}
        for attribute, channel in self.CHANNEL_FUNCTIONS.items():
            if channel == "Crossref":
                providers[attribute] = mock.Mock(
                    name=attribute, side_effect=RuntimeError("simulated channel failure")
                )
            else:
                providers[attribute] = mock.Mock(
                    name=attribute,
                    return_value=(
                        [
                            {
                                "engine": channel,
                                "title": f"{channel} independent result",
                                "url": f"https://{channel.casefold()}.example/result",
                                "snippet": f"Result returned by {channel}",
                                "type": "community",
                            }
                        ],
                        None,
                    ),
                )
        cache = _FakeCache()

        with mock.patch.multiple(search, **providers), \
                mock.patch.object(search, "_get_persistent_cache", return_value=cache), \
                mock.patch.object(search, "_force_fresh", False):
            combined, statuses = search.search_all_engines_extended(
                "broad coverage query", limit=4
            )

        self.assertEqual(set(self.CHANNEL_FUNCTIONS.values()), set(statuses))
        self.assertTrue(statuses["Crossref"].startswith("❌"))
        for attribute, channel in self.CHANNEL_FUNCTIONS.items():
            providers[attribute].assert_called_once()
            if channel != "Crossref":
                self.assertTrue(statuses[channel].startswith("✅"), statuses[channel])
        self.assertEqual(len(self.CHANNEL_FUNCTIONS) - 1, len(combined))
        self.assertNotIn("Crossref", {row.get("engine") for row in combined})

    def test_one_search_keeps_api_channels_parallel_and_all_channels_enabled(self) -> None:
        state_lock = threading.Lock()
        active_api_channels = 0
        maximum_parallel_api_channels = 0
        providers = {}

        def make_provider(channel, is_api):
            def provider(_query, _limit):
                nonlocal active_api_channels, maximum_parallel_api_channels
                if is_api:
                    with state_lock:
                        active_api_channels += 1
                        maximum_parallel_api_channels = max(
                            maximum_parallel_api_channels, active_api_channels
                        )
                    time.sleep(0.05)
                    with state_lock:
                        active_api_channels -= 1
                return (
                    [
                        {
                            "engine": channel,
                            "title": f"{channel} concurrency result",
                            "url": f"https://{channel.casefold()}.example/concurrency",
                            "snippet": "single-session all-channel evidence",
                            "type": "organic",
                        }
                    ],
                    None,
                )

            return mock.Mock(name=f"parallel_{channel}", side_effect=provider)

        for index, (attribute, channel) in enumerate(self.CHANNEL_FUNCTIONS.items()):
            providers[attribute] = make_provider(
                channel, is_api=index >= self.BROWSER_CHANNEL_COUNT
            )

        cache = _FakeCache()
        with mock.patch.multiple(search, **providers), \
                mock.patch.object(search, "_get_persistent_cache", return_value=cache), \
                mock.patch.object(search, "_force_fresh", False):
            combined, statuses = search.search_all_engines_extended(
                "parallel channel contract", limit=2
            )

        self.assertEqual(set(self.CHANNEL_FUNCTIONS.values()), set(statuses))
        self.assertEqual(len(self.CHANNEL_FUNCTIONS), len(combined))
        self.assertGreaterEqual(maximum_parallel_api_channels, 2)
        for provider in providers.values():
            provider.assert_called_once()

    def test_language_affine_channels_receive_routed_queries(self) -> None:
        providers = {}
        for attribute, channel in self.CHANNEL_FUNCTIONS.items():
            providers[attribute] = mock.Mock(name=attribute, return_value=([], None))
        cache = _FakeCache()
        alt_queries = {"en": "Rust tokio runtime tuning", "zh": "Rust tokio 运行时 调优"}

        with mock.patch.multiple(search, **providers), \
                mock.patch.object(search, "_get_persistent_cache", return_value=cache), \
                mock.patch.object(search, "_force_fresh", True):
            search.search_all_engines_extended(
                "Rust tokio 异步运行时 性能调优", limit=3, alt_queries=alt_queries
            )

        self.assertEqual(
            providers["playwright_google_search"].call_args[0][0],
            "Rust tokio 异步运行时 性能调优",
        )
        self.assertEqual(
            providers["search_stackoverflow"].call_args[0][0],
            "Rust tokio runtime tuning",
        )
        self.assertEqual(
            providers["search_zhihu"].call_args[0][0],
            "Rust tokio 运行时 调优",
        )

    def test_english_channels_fall_back_to_latin_tokens_without_translation(self) -> None:
        self.assertEqual(
            search._effective_channel_query("StackOverflow", "Rust tokio 异步运行时 性能调优"),
            "Rust tokio",
        )
        self.assertEqual(
            search._effective_channel_query("Google", "Rust tokio 异步运行时 性能调优"),
            "Rust tokio 异步运行时 性能调优",
        )
        self.assertEqual(
            search._effective_channel_query("Baidu", "Rust tokio 异步运行时 性能调优"),
            "Rust tokio 异步运行时 性能调优",
        )
        # A pure-CJK query has no usable Latin subset and stays unchanged.
        self.assertEqual(
            search._effective_channel_query("StackOverflow", "机械键盘 推荐"),
            "机械键盘 推荐",
        )
        # Digit-only leftovers (e.g. a bare year) must not become the query.
        self.assertEqual(
            search._effective_channel_query("StackOverflow", "客制化键盘 轴体 对比 2026"),
            "客制化键盘 轴体 对比 2026",
        )

    def test_credential_gated_channels_join_when_keys_present(self) -> None:
        providers = {}
        all_functions = dict(self.CHANNEL_FUNCTIONS)
        for attribute, (channel, _env) in self.CREDENTIAL_CHANNELS.items():
            all_functions[attribute] = channel
        for attribute, channel in all_functions.items():
            providers[attribute] = mock.Mock(
                name=attribute,
                return_value=(
                    [
                        {
                            "engine": channel,
                            "title": f"{channel} gated result",
                            "url": f"https://{channel.casefold()}.example/gated",
                            "snippet": f"Result returned by {channel}",
                            "type": "community",
                        }
                    ],
                    None,
                ),
            )
        cache = _FakeCache()
        env_overrides = {env: "token" for _, env in self.CREDENTIAL_CHANNELS.values()}

        with mock.patch.multiple(search, **providers), \
                mock.patch.dict(os.environ, env_overrides), \
                mock.patch.object(search, "_get_persistent_cache", return_value=cache), \
                mock.patch.object(search, "_force_fresh", False):
            combined, statuses = search.search_all_engines_extended(
                "credential gated coverage", limit=4
            )

        self.assertEqual(set(all_functions.values()), set(statuses))
        self.assertEqual(len(all_functions), len(combined))
        for provider in providers.values():
            provider.assert_called_once()


class SmartSearchIntegrationRegressionTests(unittest.TestCase):
    def test_metadata_links_and_graph_are_integrated_under_attempt_budget(self) -> None:
        rows = [
            {
                "engine": "Google",
                "source": "Google",
                "found_by": ["Google"],
                "engines": ["Google"],
                "title": f"Integration result {index}",
                "url": f"https://source{index}.example/article",
                "snippet": "integration topic evidence",
                "type": "community",
                "cache_state": "live",
            }
            for index in range(3)
        ]
        channel_search = mock.Mock(
            return_value=(copy.deepcopy(rows), {"Google": "✅ 3 results [live]"})
        )

        def extract_content(url, max_chars=50_000, query=""):
            return (f"integration topic content extracted from {url}. " * 8)[:max_chars]

        def extraction_metadata(url):
            index = url.split("source", 1)[1].split(".", 1)[0]
            links = []
            if index == "0":
                links = [
                    {
                        "url": "https://assets.example/full-report.pdf",
                        "kind": "pdf",
                        "anchor": "Full report PDF",
                        "rel": [],
                        "mime_type": "application/pdf",
                    },
                    {
                        "url": "https://related.example/supporting-analysis",
                        "kind": "link",
                        "anchor": "Supporting analysis",
                        "rel": [],
                        "mime_type": "text/html",
                    },
                ]
            return {
                "final_url": f"https://source{index}.example/article",
                "canonical_url": f"https://canonical.example/article-{index}",
                "title": f"Canonical article {index}",
                "content_type": "text/html",
                "content_hash": f"hash-{index}",
                "content_chars": 400,
                "truncated": False,
                "cache_state": "live",
                "links": links,
            }

        extractor = mock.Mock(side_effect=extract_content)
        metadata = mock.Mock(side_effect=extraction_metadata)
        cache = _FakeCache()
        environment = {
            "WEB_SEARCH_DEEP_PAGES_PER_ROUND": "2",
            "WEB_SEARCH_DEEP_ATTEMPTS_PER_ROUND": "2",
            "WEB_SEARCH_MIN_QUERY_ROUNDS": "1",
            "WEB_SEARCH_LINK_DEPTH": "2",
            "WEB_SEARCH_LINK_MAX_NODES": "20",
            "WEB_SEARCH_LINK_MAX_EDGES": "30",
            "WEB_SEARCH_LINKS_PER_DOMAIN": "5",
        }

        with mock.patch.dict(os.environ, environment, clear=False), \
                mock.patch.object(search, "search_all_engines_extended", channel_search), \
                mock.patch.object(search, "playwright_extract_content", extractor), \
                mock.patch.object(search, "get_extraction_metadata", metadata), \
                mock.patch.object(search, "assess_sufficiency", return_value=(False, "continue")), \
                mock.patch.object(search, "_get_persistent_cache", return_value=cache):
            result = search.smart_search(
                "integration topic", limit=3, max_iterations=1, fresh=True
            )

        self.assertEqual(2, extractor.call_count)
        self.assertEqual(2, result["total_pages_attempted"])
        self.assertLessEqual(
            result["total_pages_attempted"],
            int(environment["WEB_SEARCH_DEEP_ATTEMPTS_PER_ROUND"]),
        )
        self.assertTrue(
            any(
                row.get("canonical_url") == "https://canonical.example/article-0"
                for row in result["combined_results"]
            )
        )
        self.assertTrue(
            any(
                resource["url"] == "https://assets.example/full-report.pdf"
                for resource in result["resources"]
            )
        )
        self.assertTrue(
            any(
                link["url"] == "https://related.example/supporting-analysis"
                for link in result["related_links"]
            )
        )
        self.assertTrue(result["link_graph"]["nodes"])
        self.assertTrue(result["link_graph"]["edges"])
        self.assertEqual(3, result["coverage"]["unique_results"])
        self.assertEqual(2, result["coverage"]["pages_attempted"])
        self.assertEqual(1, result["coverage"]["channels"])


class SmartSearchConcurrencyRegressionTests(unittest.TestCase):
    def test_public_sessions_serialize_do_not_cross_freshness_and_restore_global(self) -> None:
        state_lock = threading.Lock()
        first_entered = threading.Event()
        release_first = threading.Event()
        active_sessions = 0
        maximum_active_sessions = 0
        observations = {}
        errors = []

        def isolated_impl(query, limit=15, max_iterations=3, fresh=False,
                          review_queries=None, alt_queries=None):
            del limit, max_iterations, review_queries, alt_queries
            nonlocal active_sessions, maximum_active_sessions
            search._force_fresh = bool(fresh)
            with state_lock:
                active_sessions += 1
                maximum_active_sessions = max(maximum_active_sessions, active_sessions)
            try:
                if query == "fresh-session":
                    first_entered.set()
                    release_first.wait(timeout=2)
                observed = search._force_fresh
                observations[query] = observed
                return {"query": query, "cache": {"force_fresh": observed}}
            finally:
                with state_lock:
                    active_sessions -= 1

        def worker(query, fresh):
            try:
                search.smart_search(
                    query, limit=1, max_iterations=1, fresh=fresh
                )
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(search, "_force_fresh", True), \
                mock.patch.object(search, "_smart_search_impl", side_effect=isolated_impl):
            fresh_thread = threading.Thread(
                target=worker, args=("fresh-session", True), daemon=True
            )
            normal_thread = threading.Thread(
                target=worker, args=("normal-session", False), daemon=True
            )
            fresh_thread.start()
            self.assertTrue(first_entered.wait(timeout=1))
            normal_thread.start()
            time.sleep(0.05)
            release_first.set()
            fresh_thread.join(timeout=2)
            normal_thread.join(timeout=2)

            self.assertFalse(fresh_thread.is_alive())
            self.assertFalse(normal_thread.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(1, maximum_active_sessions)
            self.assertIs(observations["fresh-session"], True)
            self.assertIs(observations["normal-session"], False)
            self.assertIs(search._force_fresh, True)


class ModelReviewRegressionTests(unittest.TestCase):
    @staticmethod
    def _round_result(query: str) -> list[dict]:
        slug = query.casefold().replace(" ", "-")
        return [
            {
                "engine": "Google",
                "source": "Google",
                "found_by": ["Google"],
                "engines": ["Google"],
                "title": f"Evidence for {query}",
                "url": f"https://evidence.example/{slug}",
                "snippet": f"Targeted evidence for {query}",
                "type": "organic",
                "rank": 1,
                "cache_state": "live",
            }
        ]

    def test_review_queries_run_first_in_order_through_all_channel_aggregator(self) -> None:
        original = "base investigation"
        review_queries = [
            "targeted primary-source chronology",
            "independent contradiction evidence",
        ]

        def aggregate(query, limit, vendor, alt_queries=None):
            return self._round_result(query), {"Google": "✅ 1 results [live]"}

        all_channel_aggregator = mock.Mock(side_effect=aggregate)
        cache = _FakeCache()
        environment = {
            "WEB_SEARCH_DEEP_PAGES_PER_ROUND": "0",
            "WEB_SEARCH_DEEP_ATTEMPTS_PER_ROUND": "0",
            "WEB_SEARCH_MIN_QUERY_ROUNDS": "3",
        }

        with mock.patch.dict(os.environ, environment, clear=False), \
                mock.patch.object(
                    search,
                    "search_all_engines_extended",
                    all_channel_aggregator,
                ), \
                mock.patch.object(
                    search,
                    "generate_expansion_queries",
                    return_value=["automatic fallback expansion"],
                ), \
                mock.patch.object(
                    search,
                    "assess_sufficiency",
                    return_value=(False, "continue model-guided review"),
                ), \
                mock.patch.object(search, "_get_persistent_cache", return_value=cache):
            result = search.smart_search(
                original,
                limit=3,
                max_iterations=3,
                review_queries=review_queries,
            )

        expected_queries = [original, *review_queries]
        self.assertEqual(expected_queries, result["queries_tried"])
        self.assertEqual(review_queries, result["model_review"]["requested_queries"])
        self.assertEqual(review_queries, result["model_review"]["applied_queries"])
        self.assertEqual(
            [mock.call(query, 3, None, alt_queries=None) for query in expected_queries],
            all_channel_aggregator.call_args_list,
        )
        self.assertNotIn("automatic fallback expansion", result["queries_tried"])

    def test_review_packet_exposes_bounded_evidence_gap_and_decision_contract(self) -> None:
        result = {
            "query": "review contract topic",
            "sufficient": False,
            "coverage": {
                "queries": 2,
                "channels": 4,
                "unique_results": 2,
                "independent_domains": 2,
                "resources": 1,
                "related_links": 0,
                "pages_read": 1,
                "pages_attempted": 2,
            },
            "engine_status_summary": {
                "Google": ["✅ 2 results [live]"],
                "SemanticScholar": ["⚠️ 1 partial results [live]; HTTP 429"],
                "Reddit": ["❌ [failure] HTTP 403"],
            },
            "combined_results": [
                {
                    "title": "Corroborated web evidence",
                    "url": "https://one.example/evidence",
                    "snippet": "Useful concise evidence.",
                    "content": "full page content must not enter the review packet",
                    "type": "organic",
                    "found_by": ["Google", "Bing"],
                    "cache_state": "live",
                    "relevance": 0.9,
                    "discovered_at": "2026-07-12T09:00:00Z",
                    "validated_at": "2026-07-12T09:01:00Z",
                },
                {
                    "title": "Academic evidence",
                    "canonical_url": "https://two.example/paper",
                    "snippet": "A supporting study.",
                    "type": "academic",
                    "found_by": ["OpenAlex"],
                    "cache_state": "fresh",
                    "relevance": 0.7,
                    "metadata": {"publication_year": 2025},
                },
            ],
            "resources": [{"url": "https://two.example/paper.pdf", "kind": "pdf"}],
            "query_plan": [f"follow-up {index}" for index in range(12)],
        }

        packet = search.build_model_review_packet(result, top_k=1)

        required_keys = {
            "query",
            "sufficient",
            "coverage",
            "type_counts",
            "corroborated_results",
            "failed_channels",
            "warning_channels",
            "gaps",
            "top_evidence",
            "suggested_queries",
            "decision_contract",
        }
        self.assertTrue(required_keys.issubset(packet))
        self.assertEqual(["Reddit"], packet["failed_channels"])
        self.assertEqual(["SemanticScholar"], packet["warning_channels"])
        self.assertEqual({"academic": 1, "organic": 1}, packet["type_counts"])
        self.assertEqual(1, packet["corroborated_results"])
        self.assertEqual(1, len(packet["top_evidence"]))
        self.assertNotIn("content", packet["top_evidence"][0])
        self.assertLessEqual(len(packet["suggested_queries"]), 8)
        self.assertEqual(
            {"stop", "queries", "focus", "reason"},
            set(packet["decision_contract"]),
        )

    def test_compact_output_contract_excludes_full_result_bodies(self) -> None:
        packet = {"query": "compact topic", "gaps": ["missing evidence"]}
        resources = [
            {"url": f"https://resources.example/{index}.pdf", "kind": "pdf"}
            for index in range(25)
        ]
        result = {
            "query": "compact topic",
            "detected_vendor": None,
            "sufficient": False,
            "queries_tried": ["compact topic"],
            "model_review": {"requested_queries": [], "applied_queries": []},
            "coverage": {"unique_results": 99},
            "channels": {"Google": {"unique_results": 10}},
            "filtered_summary": {"total": 2},
            "review_packet": packet,
            "resources": resources,
            "search_log": [{"iteration": 1}],
            "combined_results": [{"content": "very large page body"}],
            "related_links": [{"url": "https://example.com/large-link-set"}],
        }

        compact = search.compact_search_output(result)

        self.assertEqual(
            {
                "query",
                "detected_vendor",
                "alt_queries",
                "sufficient",
                "queries_tried",
                "model_review",
                "coverage",
                "channels",
                "filtered_summary",
                "review_packet",
                "resources",
                "search_log",
            },
            set(compact),
        )
        self.assertEqual(packet, compact["review_packet"])
        self.assertEqual(20, len(compact["resources"]))
        self.assertNotIn("combined_results", compact)
        self.assertNotIn("related_links", compact)


class CommandLineRegressionTests(unittest.TestCase):
    def test_help_exits_without_search_or_cache_side_effects(self) -> None:
        script = Path(search.__file__).resolve()
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "help-must-not-create-cache.sqlite3"
            environment = os.environ.copy()
            environment["WEB_SEARCH_CACHE_PATH"] = str(cache_path)
            environment["WEB_SEARCH_CHROMIUM_EXECUTABLE"] = str(
                Path(temp_dir) / "missing-browser.exe"
            )
            completed = subprocess.run(
                [sys.executable, str(script), "--help"],
                cwd=str(script.parent),
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("Usage: search.py", completed.stdout)
            self.assertIn("--review-query", completed.stdout)
            self.assertNotIn('"query": "--help"', completed.stdout)
            self.assertFalse(cache_path.exists())


class PdfExtractionRegressionTests(unittest.TestCase):
    class _Page:
        def __init__(self, text: str = "", error: Exception | None = None) -> None:
            self._text = text
            self._error = error

        def extract_text(self) -> str:
            if self._error is not None:
                raise self._error
            return self._text

    @staticmethod
    def _response(data: bytes, final_url: str) -> mock.MagicMock:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        response.read.return_value = data
        response.geturl.return_value = final_url
        response.headers = {
            "Content-Length": str(len(data)),
            "Content-Type": "application/pdf",
            "ETag": '"pdf-etag"',
        }
        return response

    def test_pdf_text_pages_hash_links_and_truncation_metadata(self) -> None:
        url = "https://example.com/report.pdf"
        final_url = "https://cdn.example.com/report-v2.pdf"
        pdf_bytes = b"%PDF-1.7\nfully mocked binary payload"
        pages = [
            self._Page("First page with detailed findings."),
            self._Page(
                "Second page links to https://data.example.org/dataset.csv for data."
            ),
            self._Page("Third page contains additional conclusions."),
        ]
        reader = types.SimpleNamespace(pages=pages)
        pdf_reader = mock.Mock(return_value=reader)
        fake_pypdf = types.SimpleNamespace(PdfReader=pdf_reader)
        cache = _FakeCache()

        with mock.patch.dict(sys.modules, {"pypdf": fake_pypdf}), \
                mock.patch.object(
                    search, "_guarded_urlopen",
                    return_value=self._response(pdf_bytes, final_url),
                ), \
                mock.patch.dict(search._extraction_metadata, {}, clear=True):
            text = search._extract_pdf_content(
                url,
                max_chars=55,
                query="stable report",
                persistent=cache,
                content_key="offline-pdf-key",
                stale_text=None,
            )
            metadata = search.get_extraction_metadata(url)

        self.assertEqual(55, len(text))
        self.assertTrue(text.startswith("First page with detailed findings."))
        self.assertEqual(3, metadata["pages_extracted"])
        self.assertEqual(3, metadata["pages_total"])
        self.assertTrue(metadata["truncated"])
        self.assertEqual(hashlib.sha256(pdf_bytes).hexdigest(), metadata["content_hash"])
        self.assertEqual("application/pdf", metadata["content_type"])
        self.assertEqual(final_url, metadata["final_url"])
        self.assertTrue(
            any(
                link["url"] == "https://data.example.org/dataset.csv"
                for link in metadata["links"]
            )
        )
        self.assertEqual(metadata["content_hash"], cache.set_calls[0]["content_hash"])
        pdf_reader.assert_called_once()
        self.assertFalse(pdf_reader.call_args.kwargs["strict"])

    def test_one_broken_pdf_page_does_not_discard_other_page_text(self) -> None:
        url = "https://example.com/partially-readable.pdf"
        pdf_bytes = b"%PDF-1.7\npartially readable mocked payload"
        pages = [
            self._Page("Readable first-page evidence."),
            self._Page(error=RuntimeError("damaged page content stream")),
            self._Page("Readable third-page conclusions."),
        ]
        fake_pypdf = types.SimpleNamespace(
            PdfReader=mock.Mock(return_value=types.SimpleNamespace(pages=pages))
        )
        cache = _FakeCache()

        with mock.patch.dict(sys.modules, {"pypdf": fake_pypdf}), \
                mock.patch.object(
                    search, "_guarded_urlopen",
                    return_value=self._response(pdf_bytes, url),
                ), \
                mock.patch.dict(search._extraction_metadata, {}, clear=True):
            text = search._extract_pdf_content(
                url,
                max_chars=5_000,
                query="stable report",
                persistent=cache,
                content_key="partial-pdf-key",
                stale_text=None,
            )
            metadata = search.get_extraction_metadata(url)

        self.assertIsNotNone(text)
        self.assertIn("Readable first-page evidence.", text)
        self.assertIn("Readable third-page conclusions.", text)
        self.assertEqual(2, metadata["pages_extracted"])
        self.assertEqual(3, metadata["pages_total"])
        self.assertEqual(1, len(cache.set_calls))

    def test_extensionless_pdf_final_url_keeps_original_metadata_alias(self) -> None:
        requested_url = "https://download.example.com/resource?id=report-42"
        final_url = "https://cdn.example.com/reports/report-42.pdf"
        pdf_bytes = b"%PDF-1.7\nextensionless download payload"
        fake_pypdf = types.SimpleNamespace(
            PdfReader=mock.Mock(
                return_value=types.SimpleNamespace(
                    pages=[self._Page("Complete report text from the PDF document.")]
                )
            )
        )
        navigation_response = mock.MagicMock(name="pdf_navigation_response")
        navigation_response.status = 200
        navigation_response.headers = {
            "Content-Type": "application/pdf",
            "Content-Disposition": 'inline; filename="report-42.pdf"',
        }
        page = mock.MagicMock(name="extensionless_pdf_page")
        page.goto.return_value = navigation_response
        page.url = final_url
        context = mock.MagicMock(name="extensionless_pdf_context")
        context.new_page.return_value = page
        browser = mock.MagicMock(name="extensionless_pdf_browser")
        manager = mock.MagicMock(name="extensionless_pdf_playwright")
        manager.__enter__.return_value = types.SimpleNamespace()
        cache = _FakeCache()

        with mock.patch.dict(sys.modules, {"pypdf": fake_pypdf}), \
                mock.patch.dict(search._extraction_metadata, {}, clear=True), \
                mock.patch.object(search, "_get_persistent_cache", return_value=cache), \
                mock.patch.object(search, "_force_fresh", False), \
                mock.patch.object(search, "_is_safe_fetch_url", return_value=True), \
                mock.patch.object(
                    search,
                    "_new_stealth_browser",
                    return_value=(browser, context, 0),
                ), \
                mock.patch.object(search, "_mark_fingerprint_success"), \
                mock.patch(
                    "playwright.sync_api.sync_playwright", return_value=manager
                ), \
                mock.patch.object(
                    search, "_guarded_urlopen",
                    return_value=self._response(pdf_bytes, final_url),
                ):
            content = search.playwright_extract_content(
                requested_url, max_chars=5_000, query="stable report"
            )
            original_metadata = search.get_extraction_metadata(requested_url)

        self.assertIn("Complete report text", content)
        self.assertEqual(final_url, original_metadata["final_url"])
        self.assertEqual("application/pdf", original_metadata["content_type"])
        self.assertEqual(1, original_metadata["pages_total"])


class BrowserFactoryRegressionTests(unittest.TestCase):
    def test_local_executable_route_and_three_item_contract(self) -> None:
        context = mock.MagicMock(name="browser_context")
        browser = mock.MagicMock(name="browser")
        browser.version = "145.0.7632.6"
        browser.new_context.return_value = context
        chromium = mock.MagicMock(name="chromium")
        chromium.launch.return_value = browser
        playwright = types.SimpleNamespace(chromium=chromium)
        local_executable = (
            Path(search.__file__).resolve().parent
            / "browsers"
            / "chromium-9999"
            / "chrome-win64"
            / "chrome.exe"
        )

        with mock.patch.dict(
            os.environ,
            {
                "WEB_SEARCH_CHROMIUM_EXECUTABLE": "",
                "WEB_SEARCH_LOAD_HEAVY_ASSETS": "0",
            },
            clear=False,
        ), mock.patch.object(Path, "glob", return_value=[local_executable]):
            returned = search._new_browser_with_fingerprint(
                playwright, fp_idx=0, engine="google"
            )

        self.assertIsInstance(returned, tuple)
        self.assertEqual(3, len(returned))
        returned_browser, returned_context, fingerprint_id = returned
        self.assertIs(browser, returned_browser)
        self.assertIs(context, returned_context)
        self.assertEqual(0, fingerprint_id)
        self.assertEqual(
            str(local_executable),
            chromium.launch.call_args.kwargs["executable_path"],
        )
        context.route.assert_called_once()
        route_pattern, route_handler = context.route.call_args.args
        self.assertEqual("**/*", route_pattern)
        self.assertTrue(callable(route_handler))
        context.add_init_script.assert_called_once()


if __name__ == "__main__":
    unittest.main()
