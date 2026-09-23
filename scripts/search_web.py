"""Bounded public search for candidate URLs, never evidence of official status."""

from __future__ import annotations

import html
import ipaddress
import re
import urllib.parse
from html.parser import HTMLParser


MAX_RESULTS = 8
SEARCH_TIMEOUT = 12


def web_url(value: str) -> str | None:
    value = html.unescape(value).strip()
    if len(value) > 4096:
        return None
    try:
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        if parsed.port not in {None, 80, 443}:
            return None
        host = parsed.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith(".localhost"):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        return urllib.parse.urlunparse(parsed._replace(fragment=""))
    except ValueError:
        return None


class SearchLinks(HTMLParser):
    def __init__(self, provider="duckduckgo") -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[tuple[str, str]] = []
        self.href: str | None = None
        self.label: list[str] = []
        self.provider = provider
        self.div_depth = 0
        self.snippet_depth = None
        self.snippet_captured = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if tag == "div":
            self.div_depth += 1
            if self.provider == "brave" and "snippet" in classes and values.get("data-type") == "web":
                self.snippet_depth = self.div_depth
                self.snippet_captured = False
        if tag != "a":
            return
        result_link = (self.snippet_depth is not None and not self.snippet_captured) if self.provider == "brave" else any(name in classes for name in ("result-link", "result__a"))
        self.href = values.get("href") if result_link else None
        self.label = []

    def handle_data(self, data):
        if self.href is not None:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            self.results.append((self.href, " ".join(self.label)))
            self.href = None
            self.snippet_captured = True
        if tag == "div":
            if self.div_depth == self.snippet_depth:
                self.snippet_depth = None
            self.div_depth -= 1


def duckduckgo_results(document: str) -> list[tuple[str, str]]:
    if re.search(r"challenge-form|anomaly-modal|id=['\"]captcha|verify you are human", document, re.I):
        raise PermissionError("search provider requested human verification")
    parser = SearchLinks()
    parser.feed(document)
    results = []
    for href, title in parser.results:
        absolute = urllib.parse.urljoin("https://duckduckgo.com/", href)
        parsed = urllib.parse.urlparse(absolute)
        if parsed.hostname == "duckduckgo.com" or (parsed.hostname or "").endswith(".duckduckgo.com"):
            target = urllib.parse.parse_qs(parsed.query).get("uddg", [])
            if not target:
                continue
            absolute = target[0]
        url = web_url(absolute)
        if url:
            results.append((url, title))
    if not parser.results and not re.search(r"no (?:more )?results(?: found)?|no-results", document, re.I):
        raise ValueError("unrecognized search result markup")
    return results


def brave_results(document: str) -> list[tuple[str, str]]:
    # Normal result pages include the word captcha in their JS translations.
    if re.search(r"<title[^>]*>[^<]*(?:captcha|verify|verification)|<form\b[^>]*(?:captcha|challenge)", document, re.I):
        raise PermissionError("search provider requested human verification")
    parser = SearchLinks("brave")
    parser.feed(document)
    if not parser.results and not re.search(r"no results found|couldn.t find any results", document, re.I):
        raise ValueError("unrecognized search result markup")
    return [(url, title) for href, title in parser.results if (url := web_url(href))]


def relevant_result(query: str, url: str, title: str) -> bool:
    # The caller starts queries with the venue's name and edition year. Search
    # engines sometimes return unrelated cached feeds with HTTP 200; reject them.
    match = re.match(r"(.+?)\s+(20\d{2})\b", query)
    if not match:
        return False
    compact = lambda text: re.sub(r"[^a-z0-9]", "", text.lower())
    evidence = urllib.parse.unquote(f"{title} {url}")
    return compact(match[1]) in compact(evidence) and bool(re.search(rf"(?<!\d){match[2]}(?!\d)", evidence))


def search_web(query: str, fetch) -> tuple[list[str], list[dict]]:
    """Try public search endpoints once each; callers corroborate every URL."""
    providers = [
        ("duckduckgo_lite", "duckduckgo.com", "https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": query}), duckduckgo_results),
        ("brave", "brave.com", "https://search.brave.com/search?" + urllib.parse.urlencode({"q": query, "source": "web"}), brave_results),
    ]
    attempts = []
    for name, host, request_url, parse in providers:
        attempt = {"provider": name, "url": request_url}
        try:
            page = fetch(request_url, timeout=SEARCH_TIMEOUT)
            final_host = (urllib.parse.urlparse(page.final_url).hostname or "").lower()
            if final_host != host and not final_host.endswith("." + host):
                raise ValueError("search redirected outside its provider")
            results = parse(page.document)
            urls = list(dict.fromkeys(url for url, title in results if relevant_result(query, url, title)))[:MAX_RESULTS]
            attempt.update(status="ok" if urls else ("irrelevant_results" if results else "empty"), result_count=len(urls))
            attempts.append(attempt)
            if urls:
                return urls, attempts
        except PermissionError as exc:
            attempts.append({**attempt, "status": "blocked", "error": str(exc)})
        except Exception as exc:
            attempts.append({**attempt, "status": "error", "error": str(exc).replace("\n", " ")[:200]})
    return [], attempts
