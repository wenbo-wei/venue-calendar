#!/usr/bin/env python3
"""Discover and refresh conference editions from verifiable official sources.

The registry contains stable official series hubs and URL patterns, not asserted
future homepages. A candidate becomes an edition homepage only after a successful
HTTP response whose visible content identifies both the venue and target year.
"""

from __future__ import annotations

import gzip
import html
import ipaddress
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

import yaml


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "data" / "official_sources.yml"
OUTPUT = ROOT / "data" / "conferences.js"
STATE = ROOT / "data" / "refresh_state.json"
MAX_RESPONSE_BYTES = 4_000_000
MAX_SITEMAPS = 6
MAX_CANDIDATES = 12
USER_AGENT = "VenueCalendar/2.0 (+https://github.com/wenbo-wei/venue-calendar)"

MONTH_PATTERN = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?"
)
DATE_RE = re.compile(
    rf"(?:(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    rf"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    rf"Dec(?:ember)?)\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+['’]?(\d{{2,4}})|"
    rf"(\d{{1,2}})\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    rf"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    rf"Nov(?:ember)?|Dec(?:ember)?)\s+['’]?(\d{{2,4}}))",
    re.I,
)
MONTHS = {
    name.lower(): number
    for number, names in enumerate(
        [
            (),
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ]
    )
    for name in names
}
KEYWORDS = re.compile(
    r"(?:full\s+)?papers?\s+(?:submission|registration|deadline|due)|"
    r"submission\s+deadline|abstracts?\s+(?:deadline|due)",
    re.I,
)
EXCLUDE = re.compile(
    r"workshop|tutorial|camera.ready|supplement|rebuttal|notification|demo|"
    r"doctoral|challenge|\b(?:site\s+)?opens?\b",
    re.I,
)
SOFT_ERROR = re.compile(
    r"\b(?:404|403)\b.{0,40}\b(?:not found|error|forbidden)\b|"
    r"\bpage (?:was |is )?not found\b|\bsite (?:can.t be reached|unavailable)\b|"
    r"\berr_(?:name_not_resolved|connection|timed_out)\b|"
    r"\bdomain (?:is )?for sale\b|\bthis site has been suspended\b",
    re.I | re.S,
)
NON_HOMEPAGE = re.compile(
    r"\b(?:call for papers|cfp|important dates?|deadlines?|accepted papers?|"
    r"proceedings|workshops?|tutorials?|submission instructions?|program|"
    r"registration|schedule|committees?|travel|accommodation|sponsors?|about|"
    r"venue|news|local ?information|hotels?)\b",
    re.I,
)
LOCATION_PLACEHOLDER = re.compile(
    r"^(?:TB[ACD]|coming soon|unknown|not available|"
    r"(?:location\s+)?not (?:yet )?(?:announced|confirmed|available)|"
    r"to be (?:announced|confirmed|determined|decided))[\s.!-]*$",
    re.I,
)
LOCATION_REJECT = re.compile(
    rf"\b(?:{MONTH_PATTERN}|deadline|submission|call for|workshop|"
    r"tutorial|program|committee|registration|calendar|select year|"
    r"announcements?|welcome|days?|hours?|minutes?|seconds?|"
    r"to be announced|not announced|TBD|TBA|home|schedule|overview|"
    r"important dates?|key dates?|privacy|terms|contact|accessibility|"
    r"copyright|cookies?|sponsors?|code of conduct|future meetings?)\b",
    re.I,
)


@dataclass(frozen=True)
class Page:
    requested_url: str
    final_url: str
    document: str
    status: int


@dataclass(frozen=True)
class Candidate:
    url: str
    discovered_from: str
    method: str


class LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = dict(attrs)
        self._href = values.get("href")
        self._label = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._label.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._label)))
            self._href = None
            self._label = []


class PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        assert_public_url(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


def edition_year(rule: str, now_year: int) -> int:
    candidate = now_year + 1
    if rule == "even_next" and candidate % 2:
        candidate += 1
    if rule == "odd_next" and candidate % 2 == 0:
        candidate += 1
    return candidate


def assert_public_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise RuntimeError("unsafe URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("invalid URL port") from exc
    if port not in {None, 80, 443}:
        raise RuntimeError("non-web port rejected")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(parsed.hostname, port or 443, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise RuntimeError(f"DNS error: {str(exc)[:120]}") from exc
    if not addresses:
        raise RuntimeError("DNS returned no addresses")
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_global:
                raise RuntimeError("private or non-global address rejected")
        except ValueError as exc:
            raise RuntimeError("invalid resolved address") from exc


def fetch(url: str, timeout: int = 25) -> Page:
    assert_public_url(url)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/xml"},
    )
    opener = urllib.request.build_opener(PublicRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.getcode()
            if status < 200 or status >= 300:
                raise RuntimeError(f"HTTP {status}")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError("official response too large")
            final_url = response.geturl()
            if response.headers.get("Content-Encoding", "").lower() == "gzip" or final_url.lower().split("?", 1)[0].endswith(".gz"):
                try:
                    raw = gzip.decompress(raw)
                except (OSError, EOFError) as exc:
                    raise RuntimeError("invalid gzip response") from exc
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise RuntimeError("decompressed response too large")
            charset = response.headers.get_content_charset() or "utf-8"
            return Page(url, final_url, raw.decode(charset, "replace"), status)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = str(exc.reason).replace("\n", " ")[:160]
        raise RuntimeError(f"network error: {reason}") from exc


def plain_text(document: str) -> str:
    document = re.sub(r"<(script|style|template)\b[^>]*>.*?</\1>", " ", document, flags=re.I | re.S)
    document = re.sub(
        r"</?(?:p|div|li|tr|td|th|h\d|section|header|footer|main|article|br)\b[^>]*>",
        "\n",
        document,
        flags=re.I,
    )
    text = html.unescape(re.sub(r"<[^>]+>", " ", document)).replace("\xa0", " ")
    return "\n".join(re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip())


def plain_lines(document: str) -> list[str]:
    return [line for line in plain_text(document).splitlines() if line]


def page_title(document: str) -> str:
    match = re.search(r"<title\b[^>]*>(.*?)</title>", document, re.I | re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))).strip() if match else ""


def page_headings(document: str) -> str:
    headings = re.findall(r"<h[1-2]\b[^>]*>(.*?)</h[1-2]>", document, re.I | re.S)
    return "\n".join(
        re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()
        for value in headings[:20]
    )


def page_primary_heading(document: str) -> str:
    for tag in ("h1", "h2"):
        match = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", document, re.I | re.S)
        if match:
            return re.sub(
                r"\s+",
                " ",
                html.unescape(re.sub(r"<[^>]+>", " ", match.group(1))),
            ).strip()
    return ""


def normalize_words(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def aliases_for(source: dict) -> list[str]:
    return list(dict.fromkeys([source["title"], *(source.get("aliases") or []), source["slug"]]))


def identity_year_match(value: str, source: dict, year: int) -> bool:
    normalized = normalize_words(value)
    full_year = str(year)
    short_year = str(year)[-2:]
    for alias in aliases_for(source):
        identity = normalize_words(alias)
        if not identity:
            continue
        escaped = re.escape(identity)
        if re.search(rf"\b{escaped}\b.{{0,80}}\b{full_year}\b", normalized):
            return True
        if re.search(rf"\b{full_year}\b.{{0,80}}\b{escaped}\b", normalized):
            return True
        if re.search(rf"\b{escaped}\s*{short_year}\b", normalized):
            return True
    return False


def validate_official_page(document: str, source: dict, year: int) -> tuple[bool, str]:
    visible = plain_text(document)
    title = page_title(document)
    headings = page_headings(document)
    sample = f"{title}\n{headings}\n{visible[:200_000]}"
    if SOFT_ERROR.search(sample):
        return False, "soft error page"
    if NON_HOMEPAGE.search(f"{title}\n{page_primary_heading(document)}"):
        return False, "edition subpage rather than conference homepage"
    if not identity_year_match(f"{title}\n{headings}", source, year):
        return False, "page title or main heading does not identify the target edition"
    if len(visible) < 20:
        return False, "page is only a placeholder"
    return True, "verified"


def extract_links(document: str, base_url: str) -> list[tuple[str, str]]:
    collector = LinkCollector()
    try:
        collector.feed(document)
    except Exception:
        return []
    links = []
    for href, label in collector.links:
        absolute = urllib.parse.urljoin(base_url, html.unescape(href).strip())
        parsed = urllib.parse.urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            continue
        clean = urllib.parse.urlunparse(parsed._replace(fragment=""))
        links.append((clean, re.sub(r"\s+", " ", label).strip()))
    return links


def relevant_reference(url: str, label: str, source: dict, year: int) -> bool:
    decoded = urllib.parse.unquote(f"{url} {label}")
    return identity_year_match(decoded, source, year)


def normalized_host(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").rstrip(".").lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def trusted_host(url: str, source: dict) -> bool:
    host = normalized_host(url)
    for trusted in source.get("trusted_hosts") or []:
        trusted = trusted.rstrip(".").lower().encode("idna").decode("ascii")
        if host == trusted or host.endswith(f".{trusted}"):
            return True
    return False


def formatted_candidate(source: dict, year: int) -> str:
    return source["url"].format(year=year, yy=str(year)[-2:])


def candidate_key(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))


def probable_homepage_reference(url: str, label: str, source: dict, year: int) -> bool:
    if not relevant_reference(url, label, source, year):
        return False
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path).strip("/")
    normalized_path = normalize_words(path)
    normalized_label = normalize_words(label)
    if NON_HOMEPAGE.search(f"{normalized_path}\n{normalized_label}"):
        return False
    if source.get("url") and candidate_key(url) == candidate_key(formatted_candidate(source, year)):
        return True
    if not path or path.lower() in {"index.html", "index.htm"}:
        return True
    final_segment = path.rsplit("/", 1)[-1]
    if final_segment.lower() in {"index.html", "index.htm"}:
        return True
    if re.search(
        r"\b(?:official (?:site|website)|conference (?:site|website)|homepage)\b",
        label,
        re.I,
    ):
        return True
    return identity_year_match(final_segment, source, year)


def unique_candidates(candidates: Iterable[Candidate]) -> list[Candidate]:
    seen: set[str] = set()
    result = []
    for candidate in candidates:
        key = candidate_key(candidate.url)
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    priority = {
        "last_verified": 0,
        "official_hub_link": 1,
        "configured_pattern": 2,
        "official_sitemap": 3,
    }
    result.sort(key=lambda item: priority.get(item.method, 9))
    return result[:MAX_CANDIDATES]


def sitemap_urls(seed_page: Page) -> list[str]:
    parsed = urllib.parse.urlparse(seed_page.final_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    found = [f"{origin}/sitemap.xml"]
    try:
        robots = fetch(f"{origin}/robots.txt", timeout=12)
        found.extend(
            match.group(1).strip()
            for match in re.finditer(r"(?im)^\s*Sitemap:\s*(https?://\S+)", robots.document)
        )
    except Exception:
        pass
    return list(dict.fromkeys(found))


def discover_from_sitemaps(seed_page: Page, source: dict, year: int) -> list[Candidate]:
    queue = [(url, 0) for url in sitemap_urls(seed_page)]
    seen: set[str] = set()
    candidates: list[Candidate] = []
    while queue and len(seen) < MAX_SITEMAPS:
        sitemap_url, depth = queue.pop(0)
        if sitemap_url in seen:
            continue
        seen.add(sitemap_url)
        if not trusted_host(sitemap_url, source):
            continue
        try:
            page = fetch(sitemap_url, timeout=15)
        except Exception:
            continue
        locations = [
            html.unescape(value.strip())
            for value in re.findall(r"<loc\b[^>]*>(.*?)</loc>", page.document, re.I | re.S)
        ]
        for location in locations:
            if location.lower().split("?", 1)[0].endswith((".xml", ".xml.gz")) and depth == 0:
                if len(queue) + len(seen) < MAX_SITEMAPS:
                    queue.append((location, 1))
            elif probable_homepage_reference(location, "", source, year):
                candidates.append(Candidate(location, sitemap_url, "official_sitemap"))
    return candidates


def discovery_candidates(source: dict, year: int, prior: dict) -> tuple[list[Candidate], list[str]]:
    candidates: list[Candidate] = []
    errors: list[str] = []
    if prior.get("year") == year and prior.get("official_url"):
        candidates.append(Candidate(prior["official_url"], prior.get("discovered_from") or source["series_url"], "last_verified"))
    pattern_candidate = formatted_candidate(source, year)
    if trusted_host(pattern_candidate, source):
        candidates.append(Candidate(pattern_candidate, source["series_url"], "configured_pattern"))

    for seed_url in [source["series_url"], *(source.get("discovery_urls") or [])]:
        try:
            seed_page = fetch(seed_url)
        except Exception as exc:
            errors.append(f"{seed_url}: {exc}")
            continue
        if not trusted_host(seed_page.final_url, source):
            errors.append(f"{seed_url}: official hub redirected to an untrusted host")
            continue
        for url, label in extract_links(seed_page.document, seed_page.final_url):
            if probable_homepage_reference(url, label, source, year):
                candidates.append(Candidate(url, seed_page.final_url, "official_hub_link"))
        candidates.extend(discover_from_sitemaps(seed_page, source, year))
    return unique_candidates(candidates), errors


def verify_candidate(candidate: Candidate, source: dict, year: int) -> tuple[Page | None, str]:
    try:
        page = fetch(candidate.url)
    except Exception as exc:
        return None, str(exc)
    valid, reason = validate_official_page(page.document, source, year)
    if valid:
        return page, ""
    return None, reason


def parse_date(match: re.Match[str]) -> str:
    if match.group(1):
        month, day, year = match.group(1), match.group(2), match.group(3)
    else:
        day, month, year = match.group(4), match.group(5), match.group(6)
    year_number = int(year)
    if year_number < 100:
        year_number += 2000
    month_number = MONTHS[month.lower()]
    return f"{year_number:04d}-{month_number:02d}-{int(day):02d} 23:59:59"


def extract_deadlines(document: str, expected_year: int) -> dict[str, str]:
    """Extract only main-paper dates with a close, unambiguous label."""
    blocks = []
    for match in re.finditer(
        r"<(?P<tag>p|li|tr|dt|dd)\b[^>]*>(?P<body>.*?)</(?P=tag)>",
        document,
        re.I | re.S,
    ):
        value = html.unescape(re.sub(r"<[^>]+>", " ", match.group("body"))).replace("\xa0", " ")
        value = re.sub(r"\s+", " ", value).strip()
        if value:
            blocks.append(value)
    # Plain-text sources are supported only when label and date share one line.
    blocks.extend(line for line in plain_lines(document) if KEYWORDS.search(line) and DATE_RE.search(line))

    found: dict[str, str] = {}
    for block in blocks:
        if not KEYWORDS.search(block) or EXCLUDE.search(block):
            continue
        dates = list(DATE_RE.finditer(block))
        if not dates:
            continue
        value = parse_date(dates[0])
        if int(value[:4]) not in {expected_year - 1, expected_year}:
            continue
        if re.search(r"abstract|registration", block, re.I):
            found.setdefault("abstract_deadline", value)
        elif re.search(r"paper|submission", block, re.I):
            found.setdefault("deadline", value)
    return found


def jsonld_nodes(document: str) -> Iterable[dict]:
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", document, re.I | re.S)
    for attrs, payload in scripts:
        if not re.search(r"\btype\s*=\s*['\"]application/ld\+json['\"]", attrs, re.I):
            continue
        try:
            root = json.loads(html.unescape(payload).strip())
        except (json.JSONDecodeError, TypeError):
            continue
        stack = list(root if isinstance(root, list) else [root])
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                yield item
                stack.extend(value for value in item.values() if isinstance(value, (dict, list)))
            elif isinstance(item, list):
                stack.extend(item)


def schema_type(node: dict) -> set[str]:
    value = node.get("@type", [])
    values = value if isinstance(value, list) else [value]
    return {str(item).lower() for item in values}


def scalar_name(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("name") or "")
    return ""


def schema_location(value: object) -> str | None:
    if isinstance(value, str):
        return clean_location(value)
    if isinstance(value, list):
        for item in value:
            found = schema_location(item)
            if found:
                return found
        return None
    if not isinstance(value, dict):
        return None
    parts = [scalar_name(value.get("name"))]
    address = value.get("address")
    if isinstance(address, str):
        parts.append(address)
    elif isinstance(address, dict):
        parts.extend(
            scalar_name(address.get(key))
            for key in ("addressLocality", "addressRegion", "addressCountry")
        )
    compact = []
    for part in parts:
        part = re.sub(r"\s+", " ", part).strip(" ,")
        if part and part.casefold() not in {item.casefold() for item in compact}:
            compact.append(part)
    return clean_location(", ".join(compact))


def clean_location(value: str) -> str | None:
    candidate = re.sub(r"\s+", " ", html.unescape(value)).strip(" \t\r\n|,;·–—-")
    candidate = re.sub(r"^(?:at|in)\s+", "", candidate, flags=re.I)
    candidate = re.split(r"\s+and dive\b", candidate, maxsplit=1, flags=re.I)[0]
    if LOCATION_PLACEHOLDER.fullmatch(candidate):
        return None
    if not 1 < len(candidate) <= 100:
        return None
    if len(candidate.split()) > 14 or not re.search(r"[^\W\d_]", candidate, re.UNICODE):
        return None
    if re.search(r"\b20\d{2}\b", candidate) or LOCATION_REJECT.search(candidate):
        return None
    if any(mark in candidate for mark in ("http://", "https://", "@")):
        return None
    return candidate


def location_result(display: str, method: str, evidence: str) -> dict:
    return {
        "display": display,
        "method": method,
        "confidence": "high",
        "precision": "locality",
        "evidence": re.sub(r"\s+", " ", evidence).strip()[:240],
    }


def extract_location(document: str, expected_year: int, source: dict | None = None) -> dict | None:
    for node in jsonld_nodes(document):
        if "event" not in schema_type(node):
            continue
        if source:
            event_identity = " ".join(
                str(node.get(key) or "")
                for key in ("name", "description", "url", "startDate", "endDate")
            )
            if not identity_year_match(event_identity, source, expected_year):
                continue
        found = schema_location(node.get("location"))
        if found:
            return location_result(found, "schema_org_event", json.dumps(node.get("location"), ensure_ascii=False))

    lines = plain_lines(document)
    date_location = re.compile(
        rf"(?P<date>.{{0,85}}(?:{MONTH_PATTERN}).{{0,55}}\b{expected_year}\b)"
        rf"\s*(?:\||•|·|,|—)\s*(?P<location>[^|;]{{2,100}})$",
        re.I,
    )
    label_location = re.compile(r"^(?:location|venue|where)\s*[:|·–—-]\s*(.+)$", re.I)
    held_in = re.compile(
        r"\b(?:(?:will be held|is held|join us)\b.{0,45}?\bin|will be\s+in)\s+"
        r"(?P<location>[^.!;]{2,100}?)(?=\s+(?:from|on|for|during)\b|[.!;]|$)",
        re.I,
    )

    for index, line in enumerate(lines):
        labelled = label_location.search(line)
        if labelled:
            found = clean_location(labelled.group(1))
            if found:
                return location_result(found, "labelled_location", line)
        if re.fullmatch(r"(?:location|venue|where)\s*:?", line, re.I) and index + 1 < len(lines):
            found = clean_location(lines[index + 1])
            if found:
                return location_result(found, "labelled_location", f"{line}: {lines[index + 1]}")

    for line in lines:
        matched = date_location.search(line)
        if matched:
            found = clean_location(matched.group("location"))
            if found:
                return location_result(found, "date_location_line", line)

    for line in lines:
        if str(expected_year) not in line:
            continue
        matched = held_in.search(line)
        if matched:
            found = clean_location(matched.group("location"))
            if found:
                return location_result(found, "held_in_sentence", line)

    if source and source.get("location_layout") == "heading_before_date":
        headings = list(
            re.finditer(
            r"<h(?P<level>[1-6])\b[^>]*>(?P<body>.*?)</h(?P=level)>",
            document,
            re.I | re.S,
            )
        )
        for location_heading, date_heading in zip(headings, headings[1:]):
            if location_heading.group("level") != "2" or date_heading.group("level") != "4":
                continue
            date_text = re.sub(
                r"\s+",
                " ",
                html.unescape(re.sub(r"<[^>]+>", " ", date_heading.group("body"))),
            ).strip()
            if str(expected_year) not in date_text or not re.search(MONTH_PATTERN, date_text, re.I):
                continue
            location_text = re.sub(
                r"\s+",
                " ",
                html.unescape(re.sub(r"<[^>]+>", " ", location_heading.group("body"))),
            ).strip()
            found = clean_location(location_text)
            if found:
                return location_result(
                    found,
                    "heading_before_event_date",
                    f"{location_text} | {date_text}",
                )
    return None


def extract_series_location(document: str, source: dict, expected_year: int) -> dict | None:
    """Extract a target-year location from a venue's explicit official future-meetings page."""
    lines = plain_lines(document)
    year_separator = re.compile(
        rf"\b{expected_year}\b\s*(?:--+|:|\||·|–|—)\s*(?P<location>.{{2,100}})$",
        re.I,
    )
    announced_in = re.compile(
        rf"\b{expected_year}\b.{{0,55}}\b(?:will be(?: held)?|is held|takes place)\s+in\s+"
        r"(?P<location>[^.!;]{2,100}?)(?=[.!;]|$)",
        re.I,
    )
    for line in lines:
        matched = announced_in.search(line)
        if matched and identity_year_match(line, source, expected_year):
            found = clean_location(matched.group("location"))
            if found:
                return location_result(found, "official_future_meeting", line)

    for index, line in enumerate(lines):
        matched = year_separator.search(line)
        if matched:
            found = clean_location(matched.group("location"))
            if found:
                return location_result(found, "official_future_meeting", line)
    return None


def load_previous_state() -> dict:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"refusing to refresh from invalid state file: {exc}") from exc


def atomic_write(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def apply_location_stability(extracted: dict | None, prior: dict, now: datetime, source_url: str) -> tuple[dict | None, dict | None]:
    previous = prior.get("location") if isinstance(prior.get("location"), dict) else None
    pending = prior.get("pending_location") if isinstance(prior.get("pending_location"), dict) else None
    if not extracted:
        return previous, pending

    observed = {
        **extracted,
        "source_url": source_url,
        "verified_at": now.isoformat(),
    }
    if not previous or previous.get("display") == observed["display"]:
        return observed, None

    count = int(pending.get("observations", 0)) + 1 if pending and pending.get("display") == observed["display"] else 1
    proposed = {**observed, "observations": count}
    if count >= 2:
        return observed, None
    return previous, proposed


def refresh_source(source: dict, prior: dict, now: datetime) -> tuple[dict, dict]:
    year = edition_year(source["year_rule"], now.year)
    same_prior = prior if prior.get("year") == year else {}
    candidates, errors = discovery_candidates(source, year, same_prior)
    verified_page: Page | None = None
    verified_candidate: Candidate | None = None
    for candidate in candidates:
        page, error = verify_candidate(candidate, source, year)
        if page:
            verified_page, verified_candidate = page, candidate
            break
        errors.append(f"{candidate.url}: {error}")

    known = dict((source.get("known") or {}).get(year, {}))
    prior_deadlines = dict(same_prior.get("deadlines") or {})
    extracted_deadlines = {}
    if verified_page:
        extracted_deadlines = extract_deadlines(verified_page.document, year)
    # Explicitly reviewed values remain authoritative. For other fields, a newly
    # extracted value can correct the prior snapshot.
    deadlines = {**prior_deadlines, **extracted_deadlines, **known}
    timezone_name = deadlines.pop("timezone", None) or same_prior.get("timezone") or "UTC-12"

    if verified_page and verified_candidate:
        official_url = verified_page.final_url
        discovered_from = verified_candidate.discovered_from
        discovery_method = verified_candidate.method
        verified_at = now.isoformat()
        status = "verified"
        extracted_location = extract_location(verified_page.document, year, source)
    else:
        official_url = same_prior.get("official_url")
        discovered_from = same_prior.get("discovered_from")
        discovery_method = same_prior.get("discovery_method")
        verified_at = same_prior.get("verified_at")
        status = "retained" if official_url else "awaiting_official_page"
        extracted_location = None

    location_source_url = official_url
    if not extracted_location:
        for location_source in source.get("location_sources") or []:
            try:
                location_page = fetch(location_source["url"])
                if not trusted_host(location_page.final_url, source):
                    raise RuntimeError("location source redirected to an untrusted host")
                extracted_location = extract_series_location(location_page.document, source, year)
            except Exception as exc:
                errors.append(f"{location_source['url']}: {exc}")
                continue
            if extracted_location:
                extracted_location["precision"] = location_source.get("precision") or "locality"
                location_source_url = location_page.final_url
                break
    if extracted_location and location_source_url:
        location, pending_location = apply_location_stability(
            extracted_location, same_prior, now, location_source_url
        )
    else:
        location = same_prior.get("location")
        pending_location = same_prior.get("pending_location")

    display_url = official_url or source["series_url"]
    timeline = [deadlines] if any(key.endswith("deadline") for key in deadlines) else []
    place = location.get("display") if isinstance(location, dict) else "TBD"
    conf = {
        "year": year,
        "id": f"{source['slug']}{str(year)[-2:]}",
        "link": display_url,
        "link_kind": "edition" if official_url else "series",
        "official_page_announced": bool(official_url),
        "timeline": timeline,
        "timezone": timezone_name,
        "date": "TBD",
        "place": place,
        "place_status": (
            "verified"
            if place != "TBD"
            else ("not_detected" if official_url else "not_announced")
        ),
        "location_source_url": location.get("source_url") if isinstance(location, dict) else None,
    }
    venue = {
        "title": source["title"],
        "description": source["description"],
        "rank": {"ccf": source["rank"]},
        "confs": [conf],
        "latest_link": display_url,
        "official_url": official_url,
        "series_url": source["series_url"],
        "next_year": year,
        "source_category": "official",
        "source_slug": source["slug"],
    }
    state = {
        "url": display_url,
        "official_url": official_url,
        "series_url": source["series_url"],
        "year": year,
        "status": status,
        "error": "; ".join(errors[:8]) or None,
        "discovered_from": discovered_from,
        "discovery_method": discovery_method,
        "verified_at": verified_at,
        "deadlines": deadlines,
        "timezone": timezone_name,
        "location": location,
        "pending_location": pending_location,
    }
    return venue, state


def semantic_projection(state: dict) -> dict:
    projected = {}
    for slug, venue in (state.get("venues") or {}).items():
        location = venue.get("location") if isinstance(venue.get("location"), dict) else {}
        pending = venue.get("pending_location") if isinstance(venue.get("pending_location"), dict) else {}
        projected[slug] = {
            "year": venue.get("year"),
            "official_url": venue.get("official_url"),
            "deadlines": venue.get("deadlines") or {},
            "timezone": venue.get("timezone"),
            "location": location.get("display"),
            "pending_location": pending.get("display"),
            "pending_observations": pending.get("observations"),
        }
    return projected


def main() -> int:
    now = datetime.now(timezone.utc)
    previous = load_previous_state()
    refresh_state = {"checked_at": now.isoformat(), "changed": False, "venues": {}}
    venues = []
    for source in yaml.safe_load(SOURCES.read_text(encoding="utf-8")):
        prior = (previous.get("venues") or {}).get(source["slug"], {})
        venue, state = refresh_source(source, prior, now)
        venues.append(venue)
        refresh_state["venues"][source["slug"]] = state

    refresh_state["changed"] = semantic_projection(previous) != semantic_projection(refresh_state)
    payload = {
        "updated_at": now.isoformat(),
        "upstream": "verified official conference websites",
        "venues": sorted(venues, key=lambda item: item["title"].lower()),
    }
    atomic_write(
        OUTPUT,
        "window.AI_CONFERENCES = " + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n",
    )
    atomic_write(
        STATE,
        json.dumps(refresh_state, ensure_ascii=False, indent=2) + "\n",
    )
    verified = sum(state["status"] == "verified" for state in refresh_state["venues"].values())
    located = sum(bool(state.get("location")) for state in refresh_state["venues"].values())
    print(
        f"Checked {len(venues)} venues; verified {verified} edition pages and "
        f"{located} locations; semantic changes={str(refresh_state['changed']).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
