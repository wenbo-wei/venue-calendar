"""Public-search parsing and failure isolation without network requests."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import search_web as search


def page(document, host="lite.duckduckgo.com"):
    return SimpleNamespace(document=document, final_url=f"https://{host}/search")


class SearchTests(unittest.TestCase):
    def test_lite_redirect_decoding_and_nested_labels(self):
        markup = '<a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fnew-venue.example%2F&amp;rut=x"><b>ECAI</b> 2027</a>'
        self.assertEqual(search.duckduckgo_results(markup), [("https://new-venue.example/", "ECAI  2027")])

    def test_captcha_is_not_an_empty_search(self):
        with self.assertRaises(PermissionError):
            search.duckduckgo_results('<form id="challenge-form"></form>')

    def test_parser_failure_is_not_an_empty_search(self):
        with self.assertRaises(ValueError):
            search.duckduckgo_results('<html><h1>Service unavailable</h1></html>')
        self.assertEqual(search.duckduckgo_results('<h1>No results found</h1>'), [])

    def test_irrelevant_fallback_results_are_reported_without_urls(self):
        fetch = Mock(side_effect=[page('<form id="challenge-form"></form>'), page('<div class="snippet" data-type="web"><a href="https://other.example/2027">Currency today</a></div>', "search.brave.com")])
        urls, attempts = search.search_web("ECAI 2027 official conference", fetch)
        self.assertEqual(urls, [])
        self.assertEqual([item["status"] for item in attempts], ["blocked", "irrelevant_results"])
        self.assertEqual(fetch.call_count, 2)

    def test_brave_fallback_reads_results_and_ignores_script_translation(self):
        markup = '<script>const labels={captcha:"captcha"};</script><a href="https://irrelevant.example">ECAI 2027</a><div class="snippet" data-type="web"><div><a href="https://ecai2027.org/"><h3>ECAI 2027</h3></a></div><a href="https://sitelink.example">ignored</a></div>'
        fetch = Mock(side_effect=[RuntimeError("HTTP 429"), page(markup, "search.brave.com")])
        urls, attempts = search.search_web("ECAI 2027 official conference", fetch)
        self.assertEqual(urls, ["https://ecai2027.org/"])
        self.assertEqual(attempts[-1]["status"], "ok")

    def test_success_deduplicates_and_does_not_call_fallback(self):
        markup = '<a class="result-link" href="https://ecai2027.org/">ECAI 2027</a>' * 2
        fetch = Mock(return_value=page(markup))
        urls, attempts = search.search_web("ECAI 2027 official conference", fetch)
        self.assertEqual(urls, ["https://ecai2027.org/"])
        self.assertEqual(attempts[0]["status"], "ok")
        fetch.assert_called_once()

    def test_network_failure_does_not_escape_search(self):
        urls, attempts = search.search_web("ICME 2027 official conference", Mock(side_effect=RuntimeError("HTTP 429")))
        self.assertEqual(urls, [])
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(item["status"] == "error" for item in attempts))

    def test_cross_provider_redirect_is_rejected(self):
        fetch = Mock(side_effect=[page('<a class="result-link" href="https://ecai2027.org/">ECAI 2027</a>', "attacker.example"), RuntimeError("offline")])
        urls, attempts = search.search_web("ECAI 2027 official conference", fetch)
        self.assertEqual(urls, [])
        self.assertIn("outside", attempts[0]["error"])

    def test_unsafe_or_old_edition_results_are_rejected(self):
        for url in ("file:///etc/passwd", "http://127.0.0.1/", "http://localhost/", "https://user:pass@ecai2027.org/", "https://ecai2027.org:9000/"):
            self.assertIsNone(search.web_url(url))
        self.assertFalse(search.relevant_result("ICME 2027 official conference", "https://www.ieeeicme.org/2026/", "ICME 2026"))
        self.assertTrue(search.relevant_result("ACM MM 2027 official conference", "https://2027.acmmm.org/", "ACM Multimedia"))

    def test_result_budget(self):
        markup = ''.join(f'<a class="result-link" href="https://example.org/ecai2027/{i}">ECAI 2027</a>' for i in range(30))
        urls, _ = search.search_web("ECAI 2027 official conference", Mock(return_value=page(markup)))
        self.assertEqual(len(urls), search.MAX_RESULTS)


if __name__ == "__main__":
    unittest.main()
