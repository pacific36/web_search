import os
import unittest
from unittest import mock

from discovery import (
    LinkGraph,
    canonicalize_url,
    extract_html_links,
    generate_query_expansions,
    host_matches_domain,
    relevance_score,
    tokenize_query,
)


class URLTests(unittest.TestCase):
    def test_canonicalize_removes_only_known_tracking(self):
        value = canonicalize_url(
            "HTTPS://Example.COM:443/Docs/Case?utm_source=x&id=AbC&ref=keep&v=1&fbclid=y#part"
        )
        self.assertEqual(value, "https://example.com/Docs/Case?id=AbC&ref=keep&v=1")

    def test_canonicalize_preserves_signed_and_non_utf8_query_bytes(self):
        value = canonicalize_url(
            "https://Example.com?foo&sig=A%2fb+Z&bad=%FF&utm%5Fsource=x"
        )
        self.assertEqual(value, "https://example.com/?foo&sig=A%2fb+Z&bad=%FF")
        self.assertNotEqual(
            canonicalize_url("https://faß.de/"), canonicalize_url("https://fass.de/")
        )
        self.assertEqual(canonicalize_url(r"https://good.test\@evil.test/x"), "")

    def test_canonicalize_resolves_relative_and_keeps_duplicates(self):
        value = canonicalize_url(
            "../File.PDF?a=1&a=2&utm_medium=email", "https://Example.com/a/b/"
        )
        self.assertEqual(value, "https://example.com/a/File.PDF?a=1&a=2")
        self.assertEqual(canonicalize_url("mailto:person@example.com"), "")

    def test_host_matching_uses_domain_boundaries(self):
        self.assertTrue(host_matches_domain("https://docs.example.com/a", "example.com"))
        self.assertTrue(host_matches_domain("example.com:443", "example.com"))
        self.assertFalse(host_matches_domain("notexample.com", "example.com"))
        self.assertFalse(host_matches_domain("example.com.evil.test", "example.com"))
        self.assertTrue(host_matches_domain("::1", "::1"))


class HTMLExtractionTests(unittest.TestCase):
    def test_extracts_metadata_links_anchors_and_attachments(self):
        html = """
        <html><head>
          <base href="https://cdn.example.com/root/">
          <link rel="canonical" href="https://Example.com/Article?utm_source=x&id=7">
          <link rel="next" href="page2.html">
          <link rel="alternate" type="application/rss+xml" href="feed.xml">
          <link rel="stylesheet" href="assets/site.css">
          <link rel="icon" href="assets/favicon.ico">
        </head><body>
          <a href="guide.html" rel="nofollow">Complete <b>search guide</b></a>
          <a href="files/REPORT.PDF?download=1">Report PDF</a>
          <a href="files/data.XLSX" download>Data sheet</a>
          <a href="#section">same page</a>
          <a href="javascript:alert(1)">bad</a>
        </body></html>
        """
        links = extract_html_links(html, "https://origin.example/start")
        by_kind = {link.kind: link for link in links}
        self.assertEqual(by_kind["canonical"].url, "https://example.com/Article?id=7")
        self.assertEqual(by_kind["next"].url, "https://cdn.example.com/root/page2.html")
        self.assertEqual(by_kind["feed"].url, "https://cdn.example.com/root/feed.xml")
        self.assertEqual(by_kind["pdf"].anchor, "Report PDF")
        self.assertEqual(by_kind["attachment"].url, "https://cdn.example.com/root/files/data.XLSX")
        normal = next(link for link in links if link.url.endswith("guide.html"))
        self.assertEqual(normal.anchor, "Complete search guide")
        self.assertEqual(normal.rel, ("nofollow",))
        self.assertEqual(len(links), 6)


class RelevanceAndExpansionTests(unittest.TestCase):
    def test_mixed_language_tokenization_and_relevance(self):
        tokens = tokenize_query("OpenAI 人工智能搜索教程")
        self.assertIn("openai", tokens)
        self.assertIn("人工", tokens)
        self.assertIn("搜索", tokens)
        relevant = relevance_score(
            "OpenAI 人工智能", title="OpenAI 人工智能技术完整文档"
        )
        unrelated = relevance_score("OpenAI 人工智能", title="家庭烹饪与园艺")
        self.assertGreater(relevant, unrelated)
        self.assertGreaterEqual(relevant, 0.6)

    def test_query_expansion_is_explainable_bilingual_and_deduplicated(self):
        results = [
            {
                "title": "OpenAI Responses API migration guide",
                "snippet": "Official SDK reference and examples",
                "url": "https://platform.openai.com/docs/",
                "official": True,
            },
            {
                "title": "Responses API SDK examples",
                "snippet": "Python SDK examples",
                "url": "https://example.net/post",
            },
        ]
        expansions = generate_query_expansions(
            "OpenAI API 最新文档",
            results,
            quality_domains=["docs.python.org", "https://docs.python.org/3/"],
            max_queries=30,
        )
        queries = [item.query for item in expansions]
        reasons = {item.reason for item in expansions}
        self.assertEqual(len(queries), len({query.casefold() for query in queries}))
        self.assertIn("bilingual", reasons)
        self.assertIn("official_zh", reasons)
        self.assertIn("official_en", reasons)
        self.assertIn("docs_zh", reasons)
        self.assertIn("docs_en", reasons)
        self.assertIn("quality_domain", reasons)
        self.assertIn("filetype_pdf", reasons)
        self.assertTrue(any("site:platform.openai.com" in query for query in queries))
        self.assertTrue(any("site:docs.python.org" in query for query in queries))
        self.assertTrue(any(item.reason == "related_term" for item in expansions))

    def test_many_domains_do_not_crowd_out_resource_and_related_queries(self):
        expansions = generate_query_expansions(
            "distributed systems",
            [{"title": "Distributed systems consensus benchmark", "snippet": "Raft latency"}],
            quality_domains=[f"docs{index}.example.com" for index in range(30)],
        )
        reasons = {item.reason for item in expansions}
        self.assertIn("filetype_pdf", reasons)
        self.assertIn("related_term", reasons)


class LinkGraphTests(unittest.TestCase):
    def test_seed_and_pagination_remain_schedulable_without_query_in_url(self):
        graph = LinkGraph("unrelated query")
        seed = graph.add_seed("https://example.com/start")
        self.assertEqual(graph.next_batch(1)[0].url, seed.url)
        graph.mark_visited(seed.url)
        self.assertTrue(
            graph.add_discovered_links(
                seed.url,
                [{"url": "/page/2", "kind": "next", "rel": "next nofollow"}],
            )
        )
        selected = graph.next_batch(1)[0]
        self.assertEqual(selected.url, "https://example.com/page/2")
        self.assertEqual(graph.edges[-1].rel, ("next", "nofollow"))

    def test_graph_is_bounded_cycle_safe_and_ranked(self):
        graph = LinkGraph(
            "alpha research",
            max_depth=2,
            max_nodes=5,
            max_edges=6,
            per_domain_limit=3,
            min_relevance=0,
        )
        seed = graph.add_seed("https://example.com/start", relevance=0.5)
        self.assertIsNotNone(seed)
        self.assertTrue(
            graph.add_link(
                seed.url,
                "/alpha.pdf?utm_source=x",
                reason="pdf",
                anchor="alpha research report",
            )
        )
        self.assertTrue(
            graph.add_link(
                seed.url,
                "https://other.test/unrelated",
                reason="link",
                anchor="miscellaneous",
            )
        )
        self.assertFalse(graph.add_link(seed.url, "/alpha.pdf", reason="pdf"))
        self.assertFalse(graph.add_link(seed.url, "/alpha.pdf", reason="canonical"))
        pdf_edge = next(edge for edge in graph.edges if edge.target.endswith("alpha.pdf"))
        self.assertEqual(pdf_edge.reasons, {"pdf", "canonical"})

        pdf_url = "https://example.com/alpha.pdf"
        self.assertTrue(
            graph.add_link(
                pdf_url,
                "https://third.test/alpha-data",
                reason="citation",
                anchor="alpha research data",
            )
        )
        # A backlink is retained for provenance, but it does not create another
        # schedulable node or a fetch loop.
        self.assertTrue(
            graph.add_link(
                "https://third.test/alpha-data",
                seed.url,
                reason="backlink",
            )
        )
        self.assertTrue(graph.edges[-1].cyclic)
        # A child of a depth-two node exceeds the graph depth bound.
        self.assertFalse(
            graph.add_link(
                "https://third.test/alpha-data",
                "https://fourth.test/deeper",
                reason="link",
            )
        )

        batch = graph.select_next_batch(2)
        self.assertEqual(len(batch), 2)
        self.assertGreaterEqual(batch[0].relevance, batch[1].relevance)
        self.assertTrue(all(node.scheduled for node in batch))
        self.assertTrue(graph.mark_visited(batch[0].url))
        self.assertFalse(batch[0].scheduled)
        self.assertTrue(graph.release(batch[1].url))
        exported = graph.to_dict()
        self.assertEqual(len(exported["nodes"]), 4)
        self.assertEqual(len(exported["edges"]), 4)
        self.assertTrue(all(edge["reason"] for edge in exported["edges"]))
        self.assertEqual(sum(edge["cyclic"] for edge in exported["edges"]), 1)

    def test_shorter_rediscovery_propagates_depth_to_descendants(self):
        graph = LinkGraph("topic", max_depth=3, min_relevance=0)
        a = graph.add_seed("https://a.test/", relevance=1)
        graph.add_link(a.url, "https://b.test/", relevance=1)
        graph.add_link("https://b.test/", "https://c.test/", relevance=1)
        graph.add_link("https://c.test/", "https://d.test/", relevance=1)
        x = graph.add_seed("https://x.test/", relevance=1)
        self.assertTrue(graph.add_link(x.url, "https://c.test/", relevance=1))
        self.assertEqual(graph.nodes["https://c.test/"].depth, 1)
        self.assertEqual(graph.nodes["https://d.test/"].depth, 2)

        graph.add_seed("https://b.test/", relevance=1)
        self.assertEqual(graph.nodes["https://b.test/"].depth, 0)

    def test_domain_limit_prevents_one_site_from_flooding_graph(self):
        graph = LinkGraph("topic", per_domain_limit=2, min_relevance=0)
        seed = graph.add_seed("https://example.com/", relevance=1)
        self.assertTrue(graph.add_link(seed.url, "/one", relevance=1))
        self.assertFalse(graph.add_link(seed.url, "/two", relevance=1))


class PrivateNetworkLinkGraphTests(unittest.TestCase):
    PRIVATE_URLS = (
        "http://localhost/admin",
        "http://127.0.0.1/metrics",
        "http://10.10.0.5/internal",
        "http://172.16.20.4/private",
        "http://192.168.1.9/router",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/debug",
    )

    def test_private_and_loopback_seeds_are_rejected_by_default(self):
        with mock.patch.dict(
            os.environ, {"WEB_SEARCH_ALLOW_PRIVATE_URLS": "0"}, clear=False
        ):
            for url in self.PRIVATE_URLS:
                with self.subTest(url=url):
                    graph = LinkGraph("security review", min_relevance=0)
                    self.assertIsNone(graph.add_seed(url, relevance=1))
                    self.assertEqual({}, graph.nodes)

    def test_private_urls_can_be_explicitly_allowed(self):
        for url in self.PRIVATE_URLS:
            with self.subTest(url=url):
                graph = LinkGraph(
                    "local development", min_relevance=0, allow_private=True
                )
                self.assertIsNotNone(graph.add_seed(url, relevance=1))


if __name__ == "__main__":
    unittest.main()
