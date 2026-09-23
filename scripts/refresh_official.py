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
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

import yaml

from search_web import search_web


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "data" / "official_sources.yml"
OUTPUT = ROOT / "data" / "conferences.js"
STATE = ROOT / "data" / "refresh_state.json"
MAX_RESPONSE_BYTES = 4_000_000
MAX_SITEMAPS = 6
MAX_CANDIDATES = 12
MAX_DEADLINE_PAGES = 4
MAX_ANNOUNCEMENT_PAGES = 4
MAX_HISTORY = 3
MAX_UNVERIFIED = 8
MAX_SEARCH_SOURCES = 4
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
    r"submissions?\s+deadline|abstracts?\s+(?:submission\s+)?(?:deadline|due)",
    re.I,
)
EXCLUDE = re.compile(
    r"workshop|tutorial|camera.ready|supplement|rebuttal|notification|demo|"
    r"doctoral|challenge|final\s+(?:paper|manuscript)|journal|special\s+track|"
    r"\b(?:site\s+)?opens?\b",
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
    r"registration|pricing|schedule|committees?|travel|accommodation|sponsors?|about|"
    r"venue|news|local ?information|hotels?|polic(?:y|ies)|author ?kits?|"
    r"reviewers?|nomination|surveys?|application forms?|volunteers?)\b",
    re.I,
)
LOCATION_PLACEHOLDER = re.compile(
    r"^(?:(?:venue|location)\s+)?(?:TB[ACD]|coming soon|unknown|not available|"
    r"information will be (?:posted|available|announced)(?: here| soon)?|"
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
    redirects: tuple[str, ...] = ()


@dataclass(frozen=True)
class Candidate:
    url: str
    discovered_from: str
    method: str
    provenance: tuple[dict, ...] = ()


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


class ContextLinkCollector(HTMLParser):
    """Keep local paragraph/list/table context with links such as 'website'."""

    BLOCKS = {"p", "li", "tr", "td", "dd", "article", "section", "div"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict] = []
        self.blocks: list[dict] = []
        self.anchor: dict | None = None
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style", "template"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag in self.BLOCKS:
            self.blocks.append({"tag": tag, "text": [], "links": []})
        if tag == "a" and dict(attrs).get("href"):
            self.anchor = {"href": dict(attrs)["href"], "label": [], "contexts": []}
            self.links.append(self.anchor)
            for block in self.blocks:
                block["links"].append(self.anchor)

    def handle_data(self, data):
        if self.hidden:
            return
        for block in self.blocks:
            block["text"].append(data)
        if self.anchor is not None:
            self.anchor["label"].append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style", "template"}:
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag == "a":
            self.anchor = None
        if tag in self.BLOCKS:
            index = next((i for i in range(len(self.blocks) - 1, -1, -1)
                          if self.blocks[i]["tag"] == tag), None)
            if index is not None:
                closing, self.blocks = self.blocks[index:], self.blocks[:index]
                for block in reversed(closing):
                    context = re.sub(r"\s+", " ", " ".join(block["text"])).strip()
                    if len(context) <= 600 and len(block["links"]) <= 4:
                        for link in block["links"]:
                            if not link.get("localized"):
                                link["contexts"].append(context)
                                # A separate paragraph cannot lend its identity
                                # to an unrelated link in a sibling paragraph.
                                if block["tag"] in {"p", "li", "tr", "dd"}:
                                    link["localized"] = True


class PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        super().__init__()
        self.redirects: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        assert_public_url(target)
        self.redirects.append(target)
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
    if safe_path(url) is None:
        raise RuntimeError("ambiguous URL path")
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
    redirects = PublicRedirectHandler()
    opener = urllib.request.build_opener(redirects)
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
            return Page(url, final_url, raw.decode(charset, "replace"), status, tuple(redirects.redirects))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        reason = str(exc.reason).replace("\n", " ")[:160]
        raise RuntimeError(f"network error: {reason}") from exc


def plain_text(document: str) -> str:
    document = re.sub(r"<!--.*?-->", " ", document, flags=re.S)
    document = re.sub(r"<(script|style|template)\b[^>]*>.*?</\1>", " ", document, flags=re.I | re.S)
    # Source-code wrapping is whitespace, not a visible line break (Hong\nKong).
    document = re.sub(r"\s+", " ", document)
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


def submission_portal(url: str) -> bool:
    """Operational submission services are evidence links, not edition homes."""
    host = normalized_host(url)
    if host == "docs.google.com" and urllib.parse.urlparse(url).path.startswith("/forms/"):
        return True
    return any(host == platform or host.endswith("." + platform) for platform in (
        "openreview.net", "easychair.org", "edas.info", "forms.gle",
        "cmt.research.microsoft.com", "cmt3.research.microsoft.com",
    ))


def trusted_host(url: str, source: dict) -> bool:
    if safe_path(url) is None:
        return False
    host = normalized_host(url)
    for trusted in source.get("trusted_hosts") or []:
        trusted = trusted.rstrip(".").lower().encode("idna").decode("ascii")
        if host == trusted or host.endswith(f".{trusted}"):
            return True
    return False


def same_site(url: str, reference: str) -> bool:
    """Only www canonicalization is inherited, never an entire parent domain."""
    return normalized_host(url).removeprefix("www.") == normalized_host(reference).removeprefix("www.")


def safe_path(url: str) -> str | None:
    """Reject paths whose server-side decoding can escape an inherited scope."""
    if "\\" in url or re.search(r"[\x00-\x1f\x7f]", url):
        return None
    path = urllib.parse.urlparse(url).path
    for _ in range(5):
        if ("\\" in path or re.search(r"%(?:2f|5c)|[\x00-\x1f\x7f]", path, re.I)
                or any(segment.split(";", 1)[0] in {".", ".."} for segment in path.split("/"))):
            return None
        try:
            decoded = urllib.parse.unquote(path, errors="strict")
        except UnicodeError:
            return None
        if decoded == path:
            return path.rstrip("/")
        path = decoded
    return None


def edition_scope(url: str, home_url: str) -> bool:
    if not same_site(url, home_url):
        return False
    path, target = safe_path(home_url), safe_path(url)
    if path is None or target is None:
        return False
    if re.search(r"/index\.html?$", path, re.I):
        path = path.rsplit("/", 1)[0]
    return not path or target == path or target.startswith(path + "/")


def official_history(prior: dict) -> list[dict]:
    """Preserve verified edition identities, independently of current-year facts."""
    records = [prior, *(prior.get("official_history") or [])]
    result, seen = [], set()
    for record in records:
        if (not isinstance(record, dict) or not record.get("official_url") or not record.get("verified_at")
                or submission_portal(record["official_url"])):
            continue
        if not isinstance(record.get("year"), int):
            continue
        key = (record["year"], candidate_key(record["official_url"]))
        if key in seen:
            continue
        seen.add(key)
        result.append({key: record.get(key) for key in (
            "year", "official_url", "verified_at", "discovered_from", "discovery_method", "provenance")})
    return sorted(result, key=lambda item: item["year"], reverse=True)[:MAX_HISTORY]


def context_links(document: str, base_url: str) -> list[tuple[str, str, list[str]]]:
    parser = ContextLinkCollector()
    try:
        parser.feed(document)
    except Exception:
        return []
    result = []
    for item in parser.links:
        url = urllib.parse.urljoin(base_url, html.unescape(item["href"]).strip())
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            continue
        url = urllib.parse.urlunparse(parsed._replace(fragment=""))
        result.append((url, re.sub(r"\s+", " ", " ".join(item["label"])).strip(), item["contexts"]))
    return result


def linked_homepages(page: Page, source: dict, year: int, method: str,
                     provenance: tuple[dict, ...] = ()) -> list[Candidate]:
    candidates = []
    for url, label, contexts in context_links(page.document, page.final_url):
        if safe_path(url) is None or submission_portal(url):
            continue
        evidence = label
        direct = probable_homepage_reference(url, label, source, year)
        external = not same_site(url, page.final_url)
        website_label = bool(re.search(r"\b(?:website|official site|homepage)\b", label, re.I))
        if external and not (identity_year_match(label, source, year) or website_label):
            direct = False
        if not direct:
            if NON_HOMEPAGE.search(urllib.parse.unquote(f"{urllib.parse.urlparse(url).path} {label}")):
                continue
            # Do not attach a next-year paragraph to a link explicitly labelled as
            # a previous edition (or vice versa).
            if any(int(value) != year for value in re.findall(r"(?<!\d)20\d{2}(?!\d)", f"{url} {label}")):
                continue
            evidence = next((context for context in contexts
                             if identity_year_match(context, source, year)
                             and not re.search(r"\b(?:workshop|tutorial|sponsor)\b", context, re.I)
                             and re.search(r"\b(?:website|official site|homepage)\b" if external else
                                           r"\b(?:website|official site|homepage|visit|here|conference)\b",
                                           f"{context} {label}", re.I)), "")
            if not evidence:
                continue
        proof = {"url": page.final_url, "method": method, "target_url": url,
                 "evidence": evidence[:600], "link_label": label[:300]}
        candidates.append(Candidate(url, page.final_url, method, (*provenance, proof)[-8:]))
    return candidates


def announcement_reference(url: str, label: str, seed: Page, source: dict, year: int) -> bool:
    if not (trusted_host(url, source) or edition_scope(url, seed.final_url)):
        return False
    reference = urllib.parse.unquote(f"{url} {label}")
    if re.search(r"\.(?:pdf|jpg|png|zip|ics)(?:$|[?#])|\b(?:workshop|tutorial|sponsor)\b", reference, re.I):
        return False
    return relevant_reference(url, label, source, year) or bool(re.search(
        r"\bnews\b|announcements?|future[-_ /]*(?:meetings?|editions?|conferences?)|"
        r"next[-_ /]*(?:edition|conference)|upcoming[-_ /]*(?:events?|conferences?)", reference, re.I))


def discover_announcements(seed: Page, source: dict, year: int, method: str,
                           provenance: tuple[dict, ...] = ()) -> tuple[list[Candidate], list[str]]:
    candidates = linked_homepages(seed, source, year, method, provenance)
    errors, seen = [], {candidate_key(seed.final_url)}
    count = 0
    for url, label in extract_links(seed.document, seed.final_url):
        key = candidate_key(url)
        if key in seen or not announcement_reference(url, label, seed, source, year):
            continue
        # A direct homepage reference is already queued for verification.
        if key in {candidate_key(item.url) for item in candidates}:
            continue
        seen.add(key)
        count += 1
        if count > MAX_ANNOUNCEMENT_PAGES:
            break
        try:
            page = fetch(url, timeout=15)
            if not (same_site(page.final_url, url) and
                    (trusted_host(page.final_url, source) or edition_scope(page.final_url, seed.final_url))):
                raise RuntimeError("announcement redirected outside its official source")
            if SOFT_ERROR.search(plain_text(page.document)):
                raise RuntimeError("announcement is an error page")
            proof = {"url": seed.final_url, "method": method, "target_url": url, "evidence": label[:600]}
            candidates.extend(linked_homepages(page, source, year, "official_announcement_link", (*provenance, proof)))
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    return candidates, errors


def formatted_candidate(source: dict, year: int) -> str:
    return source["url"].format(year=year, yy=str(year)[-2:])


def candidate_key(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))


def probable_homepage_reference(url: str, label: str, source: dict, year: int) -> bool:
    if safe_path(url) is None or submission_portal(url) or not relevant_reference(url, label, source, year):
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
    return identity_year_match(final_segment, source, year) or identity_year_match(label, source, year)


def clear_homepage_endorsement(candidate: Candidate, source: dict | None, year: int | None) -> bool:
    """An edition label alone also occurs on blog categories and account pages."""
    if candidate.method in {"configured_pattern", "last_verified", "previous_edition_update",
                            "previous_edition_redirect", "official_hub_redirect"}:
        return True
    proof = candidate.provenance[-1] if candidate.provenance else {}
    evidence = proof.get("evidence") or ""
    label = proof.get("link_label", evidence)
    website = r"\b(?:official (?:site|website)|(?:conference )?(?:website|homepage))\b"
    if re.search(website, label, re.I):
        return True
    # A local announcement may put 'official website' around a 'click here'
    # link. Its wording must not promote an unrelated navigation label.
    generic_link = bool(re.fullmatch(r"(?:click |visit |go )?here|link|this site", label.strip(), re.I))
    if (generic_link or label.strip() == candidate.url) and re.search(website, evidence, re.I):
        return True
    path = safe_path(candidate.url)
    if path is None or not source or not year:
        return False
    # Recognize plain edition roots such as /2027 or /Conferences/2027,
    # including older official editions used to bootstrap a new website.
    edition_paths = {str(year), f"conference {year}", f"conferences {year}"}
    for alias in aliases_for(source):
        name = normalize_words(alias)
        edition_paths.update({f"{name} {year}", f"{name}{year}", f"{name} {str(year)[-2:]}", f"{year} {name}"})
    root_home = path.lower() in {"", "/index.html", "/index.htm"} or normalize_words(path) in edition_paths
    if candidate.method in {"official_sitemap", "official_search_result"}:
        label = candidate.url
    return root_home and identity_year_match(label, source, year)


def unique_candidates(candidates: Iterable[Candidate], source: dict | None = None,
                      year: int | None = None) -> list[Candidate]:
    priority = {
        "last_verified": 2,
        "official_hub_link": 1,
        "previous_edition_link": 1,
        "previous_edition_update": 1,
        "previous_edition_redirect": 1,
        "official_hub_redirect": 1,
        "official_announcement_link": 1,
        "official_search_link": 1,
        # A bare organizer event listing is useful as a fallback, but must not
        # replace a working dedicated site without a fresh official link.
        "official_search_result": 6,
        "configured_pattern": 3,
        "official_sitemap": 4,
    }
    pattern = formatted_candidate(source, year) if source and year else None

    def ordering(item: Candidate) -> int:
        rank = priority.get(item.method, 9)
        if not clear_homepage_endorsement(item, source, year):
            # Keep weak leads for diagnostics, without letting a weak duplicate
            # displace a known canonical candidate and its stronger provenance.
            rank = 7
        if pattern and candidate_key(item.url) != candidate_key(pattern) and edition_scope(pattern, item.url):
            rank = max(rank, 4)  # Prefer the edition path over its yearless hub.
        listing = bool(pattern and not same_site(pattern, source["series_url"])
                       and same_site(item.url, source["series_url"]))
        # Dedicated verified sites beat organizer listings, but an arbitrary
        # external page must not get that preference merely for changing host.
        return max(rank, 5) if listing else rank

    seen: set[str] = set()
    result = []
    # Sort before deduplication so fresh corroboration replaces a cached record
    # of the same URL, including its stronger provenance.
    for candidate in sorted(candidates, key=ordering):
        key = candidate_key(candidate.url)
        if key not in seen and safe_path(candidate.url) is not None and not submission_portal(candidate.url):
            seen.add(key)
            result.append(candidate)
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
        if not trusted_host(page.final_url, source):
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
                candidates.append(Candidate(location, page.final_url, "official_sitemap",
                                            ({"url": page.final_url, "method": "official_sitemap", "target_url": location},)))
    return candidates


def discovery_candidates(source: dict, year: int, prior: dict) -> tuple[list[Candidate], list[str]]:
    candidates: list[Candidate] = []
    errors: list[str] = []
    if prior.get("year") == year and prior.get("official_url"):
        candidates.append(Candidate(prior["official_url"], prior.get("discovered_from") or source["series_url"], "last_verified",
                                    tuple(prior.get("provenance") or [])))
    pattern_candidate = formatted_candidate(source, year)
    if trusted_host(pattern_candidate, source):
        candidates.append(Candidate(pattern_candidate, source["series_url"], "configured_pattern",
                                    ({"url": source["series_url"], "method": "configured_pattern", "target_url": pattern_candidate},)))

    seeds = [(url, None) for url in [source["series_url"], *(source.get("discovery_urls") or [])]]
    seeds.extend((item["official_url"], item) for item in official_history(prior))
    seen = set()
    bootstrap_attempted = False
    for seed_url, history in seeds:
        if submission_portal(seed_url):
            errors.append(f"{seed_url}: submission portal is not an edition homepage")
            continue
        if safe_path(seed_url) is None:
            errors.append(f"{seed_url}: ambiguous official source path")
            continue
        if candidate_key(seed_url) in seen:
            continue
        seen.add(candidate_key(seed_url))
        try:
            seed_page = fetch(seed_url)
        except Exception as exc:
            errors.append(f"{seed_url}: {exc}")
            continue
        if submission_portal(seed_page.final_url):
            errors.append(f"{seed_url}: official source redirected to a submission portal")
            continue
        if any(safe_path(url) is None for url in [seed_page.final_url, *seed_page.redirects]):
            errors.append(f"{seed_url}: official source redirected through an ambiguous path")
            continue
        if not edition_scope(seed_page.final_url, seed_url):
            # A configured official source or a previously verified homepage
            # can announce migration with an HTTP redirect. Guessed URL patterns
            # and search hits do not get this authority.
            current, _ = validate_official_page(seed_page.document, source, year)
            if current:
                method = "previous_edition_redirect" if history else "official_hub_redirect"
                proof = {"url": seed_url, "method": method, "target_url": seed_page.final_url,
                         "redirects": list(seed_page.redirects) or [seed_page.final_url],
                         "evidence": page_title(seed_page.document) or page_primary_heading(seed_page.document)}
                candidates.append(Candidate(seed_page.final_url, seed_url, method,
                                            (*tuple((history or {}).get("provenance") or []), proof)[-8:]))
                continue
        if not trusted_host(seed_page.final_url, source) and not (
                history and same_site(seed_page.final_url, seed_url) and edition_scope(seed_page.final_url, seed_url)):
            errors.append(f"{seed_url}: official hub redirected to an untrusted host")
            continue
        if history:
            current, _ = validate_official_page(seed_page.document, source, year)
            if current and history["year"] != year:
                proof = {"url": seed_url, "method": "previous_edition_update", "target_url": seed_page.final_url,
                         "evidence": page_title(seed_page.document) or page_primary_heading(seed_page.document)}
                candidates.append(Candidate(seed_page.final_url, seed_url, "previous_edition_update",
                                            (*tuple(history.get("provenance") or []), proof)[-8:]))
            valid, reason = validate_official_page(seed_page.document, source, year if current else history["year"])
            if not valid:
                errors.append(f"{seed_url}: prior edition identity no longer verified: {reason}")
                continue
        method = "previous_edition_link" if history else "official_hub_link"
        provenance = tuple((history or {}).get("provenance") or [])
        discovered, announcement_errors = discover_announcements(seed_page, source, year, method, provenance)
        candidates.extend(discovered)
        errors.extend(announcement_errors)
        if not history:
            candidates.extend(discover_from_sitemaps(seed_page, source, year))
            if not bootstrap_attempted:
                previous_year = year - (2 if source["year_rule"] in {"even_next", "odd_next"} else 1)
                previous_candidates = linked_homepages(seed_page, source, previous_year, "official_hub_link")
                if previous_candidates:
                    bootstrap_attempted = True
                    previous = previous_candidates[0]
                    previous_page, error = verify_candidate(previous, source, previous_year)
                    if previous_page:
                        found, found_errors = discover_announcements(previous_page, source, year,
                                                                     "previous_edition_link", previous.provenance)
                        candidates.extend(found)
                        errors.extend(found_errors)
                    else:
                        errors.append(f"{previous.url}: previous edition bootstrap failed: {error}")
    return unique_candidates(candidates, source, year), errors


def search_discovery_candidates(source: dict, year: int, prior: dict | None = None) -> tuple[list[Candidate], list[str], list[dict], list[dict]]:
    """Search provides leads; only known organizers can corroborate a new host."""
    base_query = f"{source['title']} {year} official conference"
    history = official_history(prior or {})
    hosts = list(dict.fromkeys([normalized_host(source["series_url"]),
                               *(normalized_host(item["official_url"]) for item in history),
                               *(source.get("trusted_hosts") or [])]))[:3]
    queries = [base_query, f"{base_query} ({' OR '.join('site:' + host for host in hosts)})"]
    candidates, errors, attempts, unverified = [], [], [], []
    visited, fetched = set(), 0
    checked_history = {}
    for query in queries:
        try:
            urls, diagnostics = search_web(query, fetch)
        except Exception as exc:
            urls, diagnostics = [], [{"provider": "search", "status": "error", "error": str(exc)[:200]}]
        attempts.extend({**item, "query": query} for item in diagnostics)
        for item in diagnostics:
            if item.get("status") in {"error", "blocked", "unavailable"}:
                errors.append(f"search {item.get('provider', '')}: {item.get('error') or item.get('status')}")
        for url in urls[:MAX_UNVERIFIED]:
            key = candidate_key(url)
            if key in visited:
                continue
            visited.add(key)
            if submission_portal(url):
                continue
            inherited = next((item for item in history if edition_scope(url, item["official_url"])), None)
            proof = ()
            if not trusted_host(url, source) and inherited:
                history_url = inherited["official_url"]
                if history_url not in checked_history:
                    try:
                        old_page = fetch(history_url, timeout=15)
                        valid, reason = validate_official_page(old_page.document, source, inherited["year"])
                        if not valid or not edition_scope(old_page.final_url, history_url):
                            raise RuntimeError(reason if not valid else "previous edition moved outside its verified scope")
                        checked_history[history_url] = True
                    except Exception as exc:
                        checked_history[history_url] = False
                        errors.append(f"{history_url}: search history recheck: {exc}")
                if checked_history[history_url]:
                    proof = (*tuple(inherited.get("provenance") or []),
                             {"url": history_url, "method": "previous_edition_search", "target_url": url, "query": query})
                else:
                    inherited = None
            if not trusted_host(url, source) and not inherited:
                unverified.append({"url": url, "discovered_from": query,
                                   "reason": "search result has no verified official-source reference"})
                continue
            if fetched >= MAX_SEARCH_SOURCES:
                continue
            fetched += 1
            try:
                page = fetch(url, timeout=15)
                if submission_portal(page.final_url):
                    raise RuntimeError("search source redirected to a submission portal")
                if not trusted_host(page.final_url, source) and not (
                        inherited and same_site(page.final_url, url) and edition_scope(page.final_url, inherited["official_url"])):
                    raise RuntimeError("search source redirected to an untrusted host")
                if SOFT_ERROR.search(plain_text(page.document)):
                    raise RuntimeError("search source is an error page")
                if not proof:
                    proof = ({"url": page.final_url, "method": "trusted_organizer_search", "query": query},)
                valid, _ = validate_official_page(page.document, source, year)
                if valid:
                    candidates.append(Candidate(page.final_url, page.final_url, "official_search_result", proof))
                found, found_errors = discover_announcements(page, source, year, "official_search_link", proof)
                candidates.extend(found)
                errors.extend(found_errors)
            except Exception as exc:
                errors.append(f"{url}: {exc}")
    return unique_candidates(candidates, source, year), errors, attempts[:6], unverified[:MAX_UNVERIFIED]


def verify_candidate(candidate: Candidate, source: dict, year: int) -> tuple[Page | None, str]:
    if submission_portal(candidate.url):
        return None, "submission portal is not an edition homepage"
    if safe_path(candidate.url) is None:
        return None, "ambiguous homepage path"
    if not clear_homepage_endorsement(candidate, source, year):
        return None, "edition link does not establish a conference homepage"
    try:
        page = fetch(candidate.url)
    except Exception as exc:
        return None, str(exc)
    if submission_portal(page.final_url):
        return None, "homepage redirected to a submission portal"
    if any(safe_path(url) is None for url in [page.final_url, *page.redirects]):
        return None, "homepage redirected through an ambiguous path"
    if not same_site(page.final_url, candidate.url):
        return None, "homepage redirected to an uncorroborated host"
    if not edition_scope(page.final_url, candidate.url) and candidate_key(page.final_url) != candidate_key(candidate.url):
        return None, "homepage redirected outside the discovered edition path"
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


def fixed_timezone(name: str) -> timezone | None:
    if re.fullmatch(r"AoE|Anywhere on Earth", name, re.I):
        return timezone(timedelta(hours=-12))
    matched = re.fullmatch(r"(?:UTC|GMT)(?:([+-])(\d{1,2})(?::(\d{2}))?)?", name, re.I)
    if not matched:
        return None
    hours, minutes = int(matched[2] or 0), int(matched[3] or 0)
    if hours > 14 or minutes > 59:
        return None
    offset = timedelta(hours=hours, minutes=minutes)
    return timezone(-offset if matched[1] == "-" else offset)


def deadline_kind(label: str) -> str | None:
    label = label.replace("_", " ")
    if EXCLUDE.search(label) or not KEYWORDS.search(label):
        return None
    return "abstract_deadline" if re.search(r"abstract|registration", label, re.I) else "deadline"


def text_deadline(block: str, expected_year: int, default_timezone: str | None) -> tuple[str, str, str] | None:
    dates = list(DATE_RE.finditer(block))
    if len(dates) != 1:
        return None
    value = parse_date(dates[0])
    if int(value[:4]) not in {expected_year - 1, expected_year}:
        return None
    zone_match = re.search(r"Anywhere on Earth|\bAoE\b|\b(?:UTC|GMT)(?:\s*[+-]\s*\d{1,2}(?::\d{2})?)?", block, re.I)
    zone_name = re.sub(r"\s+", "", zone_match[0]) if zone_match else default_timezone
    if zone_match and re.fullmatch("Anywhere on Earth", zone_match[0], re.I):
        zone_name = "UTC-12"
    zone = fixed_timezone(zone_name or "")
    if zone is None:
        return None
    clock_text = block[:zone_match.start()] + block[zone_match.end():] if zone_match else block
    clock = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?\b", clock_text, re.I)
    if clock:
        hour, minute, second = int(clock[1]), int(clock[2]), int(clock[3] or 0)
        if clock[4]:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if clock[4].upper() == "PM" else 0)
        value = f"{value[:10]} {hour:02d}:{minute:02d}:{second:02d}"
    elif zone != timezone(timedelta(hours=-12)):
        # A bare UTC date does not specify whether the cutoff is morning or evening.
        return None
    try:
        instant = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=zone)
    except ValueError:
        return None
    canonical = instant.astimezone(timezone(timedelta(hours=-12)))
    return canonical.strftime("%Y-%m-%d %H:%M:%S"), zone_name, "page" if zone_match else "registry"


def extract_deadline_observations(document: str, expected_year: int, default_timezone: str | None = None) -> list[dict]:
    """Read literal dates, never execute scripts or infer a missing timezone."""
    document = re.sub(r"<!--.*?-->|<(?:del|s|strike)\b[^>]*>.*?</(?:del|s|strike)>", " ", document, flags=re.I | re.S)
    # Some official pages declare AoE once for the entire main-paper timetable.
    if re.search(r"(?:all\s+(?:submission\s+)?deadlines\b|deadlines\s+are\b)[^.\n<]{0,100}(?:anywhere on earth|\bAoE\b|UTC\s*-\s*12)", plain_text(document), re.I):
        default_timezone = "UTC-12"
        global_zone = True
    else:
        global_zone = False
    blocks = [match for tag in ("tr", "p", "li", "dt", "dd", "article")
              for match in re.finditer(rf"<{tag}\b[^>]*>(?P<body>.*?)</{tag}>", document, re.I | re.S)]
    headings = [(match.start(), plain_text(match[1])) for match in re.finditer(r"<h[1-6]\b[^>]*>(.*?)</h[1-6]>", document, re.I | re.S)]
    observations = []

    def add(field: str, value: str, method: str, evidence: str, round_number: int, **extra) -> None:
        observations.append({"field": field, "value": value, "method": method,
                             "evidence": re.sub(r"\s+", " ", evidence).strip()[:300],
                             "round": round_number, **extra})

    # Whitelisted date variables used by the official countdown template. Multiple
    # copies of a variable are harmless; non-paper variables never match.
    for match in re.finditer(r"\b(?:var|let|const)\s+((?:round_\d+_)?(?:paper_registration_deadline|abstract_deadline|paper_submission_deadline|submission_deadline|paper_deadline)(?:_\d+)?)\s*=\s*['\"](\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) UTC['\"]", document):
        name, raw = match[1], match[2]
        try:
            instant = datetime.strptime(raw, "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if instant.year not in {expected_year - 1, expected_year}:
            continue
        enclosing = min((block for block in blocks if block.start() <= match.start() < block.end()), key=lambda block: len(block[0]), default=None)
        if enclosing and EXCLUDE.search(plain_text(enclosing["body"])):
            continue
        heading = next((label for position, label in reversed(headings) if position < match.start()), "")
        if EXCLUDE.search(heading):
            continue
        round_match = re.search(r"round_(\d+)", name)
        add(deadline_kind(name), instant.astimezone(timezone(timedelta(hours=-12))).strftime("%Y-%m-%d %H:%M:%S"),
            "official_countdown", f"{name} = {raw} UTC", int(round_match[1]) if round_match else 0,
            source_timezone="UTC", timezone_source="page")

    for block in blocks:
        # Retain each row/paragraph's label and date together.
        value = re.sub(r"\s+", " ", plain_text(block["body"])).strip()
        field = deadline_kind(value)
        heading = next((label for position, label in reversed(headings) if position < block.start()), "")
        if not field or EXCLUDE.search(heading):
            continue
        parsed = text_deadline(value, expected_year, default_timezone)
        if not parsed:
            continue
        round_match = re.search(r"\bround\s+(\d+)", value, re.I)
        timestamp, zone, zone_source = parsed
        add(field, timestamp, "labelled_date", value, int(round_match[1]) if round_match else 0,
            source_timezone=zone, timezone_source="page" if global_zone else zone_source)
    return observations


def select_deadlines(observations: list[dict]) -> dict[str, dict]:
    """The existing UI has one cutoff pair: use the latest main-paper round."""
    papers = [item for item in observations if item["field"] == "deadline"]
    selected_round = max(papers, key=lambda item: item["value"])["round"] if papers else None
    selected = {}
    for field in ("deadline", "abstract_deadline"):
        choices = [item for item in observations if item["field"] == field and (selected_round is None or item["round"] == selected_round)]
        if choices:
            # A later official extension wins; exact countdown data breaks ties.
            selected[field] = max(choices, key=lambda item: (item["value"], item["method"] == "official_countdown"))
    return selected


def extract_deadlines(document: str, expected_year: int) -> dict[str, str]:
    return {field: item["value"] for field, item in select_deadlines(extract_deadline_observations(document, expected_year)).items()}


def deadline_reference(url: str, label: str, home: Page, source: dict, year: int) -> bool:
    if safe_path(url) is None or safe_path(home.final_url) is None:
        return False
    if normalized_host(url) != normalized_host(home.final_url):
        if not trusted_host(url, source) or not identity_year_match(f"{url} {label}", source, year):
            return False
    parsed = urllib.parse.urlparse(url)
    scoped = edition_scope(url, home.final_url)
    if not scoped and not identity_year_match(f"{url} {label}", source, year):
        return False
    reference = urllib.parse.unquote(f"{parsed.path} {parsed.query} {label}")
    if EXCLUDE.search(reference) or re.search(r"\.(?:pdf|ics|zip|jpg|png)$", parsed.path, re.I):
        return False
    years = re.findall(r"(?<!\d)20\d{2}(?!\d)", f"{parsed.hostname} {reference}")
    if any(int(value) != year for value in years):
        return False
    return bool(re.search(r"\bdates?\b|\bdeadlines?\b|important[-_ /]*dates|call[-_ /]*for[-_ /]*papers|\bcfp\b|main[-_ /]*technical[-_ /]*track", reference, re.I))


def deadline_pages(home: Page, source: dict, year: int) -> tuple[list[Page], list[str]]:
    pages, errors = [], []
    queue = extract_links(home.document, home.final_url)
    seen = {candidate_key(home.final_url)}
    attempted = 0
    while queue and attempted < MAX_DEADLINE_PAGES:
        url, label = queue.pop(0)
        key = candidate_key(url)
        if key in seen or not deadline_reference(url, label, home, source, year):
            continue
        seen.add(key)
        attempted += 1
        try:
            page = fetch(url)
            if not deadline_reference(page.final_url, label, home, source, year):
                raise RuntimeError("deadline page redirected outside the target edition")
            # Shared conference templates include navigation and hidden CSP
            # test headings; neither describes the deadline page's identity.
            headings = [value for value in page_headings(page.document).splitlines()
                        if normalize_words(value) not in {"main navigation", "csp test"}]
            identity = " ".join([page_title(page.document), *headings])
            declared_years = re.findall(r"(?<!\d)20\d{2}(?!\d)", identity)
            neutral_heading = set(normalize_words(re.sub(r"\b20\d{2}\b", "", identity)).split()) <= {
                "important", "key", "dates", "date", "deadline", "deadlines", "call", "for",
                "papers", "paper", "submission", "submissions", "main", "track", "cfp", "and",
            }
            wrong_identity = declared_years and (
                str(year) not in declared_years or (not neutral_heading and not identity_year_match(identity, source, year))
            )
            if SOFT_ERROR.search(plain_text(page.document)) or wrong_identity:
                raise RuntimeError("deadline page does not identify the target edition")
            # Yearless subpages inherit identity only on the verified edition host.
            if not declared_years and normalized_host(page.final_url) != normalized_host(home.final_url):
                raise RuntimeError("deadline page lacks target edition identity")
            pages.append(page)
            queue.extend(extract_links(page.document, page.final_url))
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    # A dedicated Dates/CFP page is preferred when duplicate facts match.
    return pages + [home], errors


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

    # Official conference templates mark the hero's city with a location icon.
    for badge in re.finditer(r"<i\b[^>]*class=['\"][^'\"]*\b(?:fa-map-marker-alt|fa-map-marker|fa-location-dot)\b[^'\"]*['\"][^>]*>.*?</div>", document, re.I | re.S):
        found = clean_location(plain_text(badge[0]))
        if found:
            return location_result(found, "location_badge", plain_text(badge[0]))

    lines = plain_lines(document)
    date_location = re.compile(
        rf"(?P<date>.{{0,85}}(?:{MONTH_PATTERN}).{{0,55}}\b{expected_year}\b)"
        rf"\s*(?:\||•|·|,|—)\s*(?P<location>[^|;]{{2,100}})$",
        re.I,
    )
    label_location = re.compile(r"^(?:location|venue|where)\s*[:|·–—-]\s*(.+)$", re.I)
    held_in = re.compile(
        r"\b(?:(?:will be held|is held|join us)\b.{0,45}?\bin|will be\s+in)\s+"
        rf"(?P<location>[^.!;]{{2,100}}?)(?=\s+(?:from|on|for|during|{MONTH_PATTERN})\b|[.!;]|$)",
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
        if identity_year_match(line, source, expected_year) and re.search(r"\bwill be held\b", line, re.I):
            # Official multi-site announcements can have a separate date range
            # before each city; preserve both rather than truncating the first.
            dated_places = re.findall(
                rf"\b{expected_year}\b\s+in\s+(.{{2,100}}?)(?=\s+and\s+from\b|[.!;]|$)",
                line, re.I,
            )
            places = [clean_location(value) for value in dated_places]
            if places and all(places):
                combined = clean_location("; ".join(dict.fromkeys(places)))
                if combined:
                    return location_result(combined, "official_future_meeting", line)
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
    if previous and not clean_location(previous.get("display", "")):
        previous = None
    if pending and not clean_location(pending.get("display", "")):
        pending = None
    if not extracted:
        return previous, pending

    observed = {
        **extracted,
        "source_url": source_url,
        "verified_at": now.isoformat(),
    }
    if not previous or previous.get("display") == observed["display"]:
        return observed, None

    # Repair a demonstrated old HTML-newline truncation immediately; genuine
    # location changes still require the existing two-observation confirmation.
    if (previous.get("method") == observed.get("method") == "held_in_sentence"
            and previous.get("source_url") == source_url
            and observed["display"].startswith(previous.get("display", "") + " ")
            and observed.get("evidence", "").startswith(previous.get("evidence", "") + " ")):
        return observed, None

    count = int(pending.get("observations", 0)) + 1 if pending and pending.get("display") == observed["display"] else 1
    proposed = {**observed, "observations": count}
    if count >= 2:
        return observed, None
    return previous, proposed


def refresh_year(source: dict, prior: dict, now: datetime) -> int:
    if prior.get("year") == now.year:
        cutoff = (prior.get("deadlines") or {}).get("deadline")
        zone = fixed_timezone(prior.get("timezone") or "UTC-12")
        try:
            instant = datetime.strptime(cutoff, "%Y-%m-%d %H:%M:%S").replace(tzinfo=zone) if cutoff and zone else None
        except ValueError:
            instant = None
        # January must not skip an edition whose submissions are still open,
        # or whose paper deadline has not yet been published/parsed.
        if instant is None or instant >= now:
            return now.year
    return edition_year(source["year_rule"], now.year)


def merge_deadlines(source: dict, prior: dict, year: int, pages: list[Page], now: datetime) -> tuple[dict, dict, str]:
    known = dict((source.get("known") or {}).get(year, {}))
    deadlines, evidence = {}, {}
    abstract_pair_deadline = None
    target_zone = timezone(timedelta(hours=-12))
    for fallback, status, zone_name in (
        (known, "reviewed_fallback", known.get("timezone") or "UTC-12"),
        (prior.get("deadlines") or {}, "retained", prior.get("timezone") or "UTC-12"),
    ):
        zone = fixed_timezone(zone_name)
        fallback_paper = None
        for field in ("deadline", "abstract_deadline"):
            if not zone or not fallback.get(field):
                continue
            try:
                instant = datetime.strptime(fallback[field], "%Y-%m-%d %H:%M:%S").replace(tzinfo=zone)
            except ValueError:
                continue
            deadlines[field] = instant.astimezone(target_zone).strftime("%Y-%m-%d %H:%M:%S")
            if field == "deadline":
                fallback_paper = deadlines[field]
            else:
                abstract_pair_deadline = fallback_paper
            evidence[field] = {**((prior.get("deadline_evidence") or {}).get(field, {}) if status == "retained" else {}), "status": status}
    observations = []
    for page in pages:
        for item in extract_deadline_observations(page.document, year, known.get("timezone")):
            observations.append({**item, "source_url": page.final_url, "verified_at": now.isoformat(), "status": "verified"})
    selected = select_deadlines(observations)
    paper = selected.get("deadline") or {**evidence.get("deadline", {}), "value": deadlines.get("deadline")}
    if paper.get("round") and "abstract_deadline" not in selected:
        abstract_round = evidence.get("abstract_deadline", {}).get("round")
        same_round = abstract_round == paper["round"]
        same_pair = abstract_round is None and abstract_pair_deadline == paper["value"]
        if not same_round and not same_pair:
            deadlines.pop("abstract_deadline", None)
            evidence.pop("abstract_deadline", None)
    for field, item in selected.items():
        deadlines[field] = item["value"]
        evidence[field] = item
    statuses = {item["status"] for item in evidence.values()}
    if not statuses:
        status = "not_detected"
    elif statuses == {"verified"}:
        status = "verified"
    elif "verified" in statuses:
        status = "partial"
    elif "retained" in statuses:
        status = "retained"
    else:
        status = "reviewed_fallback"
    return deadlines, evidence, status


def refresh_source(source: dict, prior: dict, now: datetime) -> tuple[dict, dict]:
    year = refresh_year(source, prior, now)
    same_prior = prior if prior.get("year") == year else {}
    if submission_portal(same_prior.get("official_url") or ""):
        # A rejected homepage must not reappear through failure retention. Its
        # independently stored deadline evidence remains usable.
        same_prior = {**same_prior, "official_url": None, "verified_at": None,
                      "discovered_from": None, "discovery_method": None, "provenance": []}
    candidates, errors = discovery_candidates(source, year, prior)
    # Each scheduled check searches even when the cached homepage still works:
    # organizers can announce a replacement before taking the old site down.
    search_candidates, search_errors, search_attempts, unverified = search_discovery_candidates(source, year, prior)
    errors.extend(search_errors)
    candidates = unique_candidates([*candidates, *search_candidates], source, year)
    verified_page: Page | None = None
    verified_candidate: Candidate | None = None
    attempted = set()
    for candidate in candidates:
        attempted.add(candidate_key(candidate.url))
        page, error = verify_candidate(candidate, source, year)
        if page:
            verified_page, verified_candidate = page, candidate
            break
        errors.append(f"{candidate.url}: {error}")
        if candidate.method not in {"configured_pattern", "last_verified"}:
            unverified.append({"url": candidate.url, "discovered_from": candidate.discovered_from, "reason": error})

    pages = []
    if verified_page:
        pages, deadline_errors = deadline_pages(verified_page, source, year)
        errors.extend(deadline_errors)
    deadlines, deadline_evidence, deadline_status = merge_deadlines(source, same_prior, year, pages, now)
    timezone_name = "UTC-12"
    primary_evidence = deadline_evidence.get("deadline") or deadline_evidence.get("abstract_deadline") or {}
    deadline_source_url = primary_evidence.get("source_url")

    if verified_page and verified_candidate:
        official_url = verified_page.final_url
        discovered_from = verified_candidate.discovered_from
        discovery_method = verified_candidate.method
        provenance = list(verified_candidate.provenance)
        verified_at = now.isoformat()
        status = "verified"
        extracted_location = extract_location(verified_page.document, year, source)
    else:
        official_url = same_prior.get("official_url")
        discovered_from = same_prior.get("discovered_from")
        discovery_method = same_prior.get("discovery_method")
        provenance = same_prior.get("provenance") or []
        verified_at = same_prior.get("verified_at")
        status = "retained" if official_url else "awaiting_official_page"
        extracted_location = None

    search_statuses = {item.get("status") for item in search_attempts}
    search_failed = bool(search_statuses & {"error", "blocked", "unavailable"})
    search_completed = bool(search_statuses & {"ok", "success", "empty", "irrelevant_results", "completed"})
    search_status = ("not_needed" if not search_attempts else "partial" if search_failed and search_completed
                     else "unavailable" if search_failed else "completed")
    discovery_status = ("verified" if verified_page else "retained" if official_url
                        else "unverified_candidates" if unverified
                        else "search_unavailable" if search_status == "unavailable" else "not_found")
    history_record = {"year": year, "official_url": official_url, "verified_at": verified_at,
                      "discovered_from": discovered_from, "discovery_method": discovery_method,
                      "provenance": provenance, "official_history": official_history(prior)}
    history = official_history(history_record)
    unverified = list({candidate_key(item["url"]): item for item in unverified
                       if not official_url or candidate_key(item["url"]) != candidate_key(official_url)}.values())[:MAX_UNVERIFIED]

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
    location, pending_location = apply_location_stability(
        extracted_location, same_prior, now, location_source_url or source["series_url"]
    )

    display_url = official_url or source["series_url"]
    timeline = [deadlines] if any(key.endswith("deadline") for key in deadlines) else []
    place = location.get("display") if isinstance(location, dict) else "TBD"
    conf = {
        "year": year,
        "id": f"{source['slug']}{str(year)[-2:]}",
        "link": display_url,
        "link_kind": "edition" if official_url else "series",
        "official_page_announced": bool(official_url),
        "discovery_status": discovery_status,
        "timeline": timeline,
        "timezone": timezone_name,
        "deadline_source_url": deadline_source_url,
        "deadline_status": deadline_status,
        "date": "TBD",
        "place": place,
        "place_status": (
            "verified"
            if place != "TBD"
            else "not_detected"
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
        "discovery_status": discovery_status,
        "discovery_details": {"checked_at": now.isoformat(), "search_status": search_status,
                              "candidate_count": len(attempted)},
        "search_attempts": search_attempts,
        "unverified_candidates": unverified[:MAX_UNVERIFIED],
        "official_history": history,
        "provenance": provenance,
        "verified_at": verified_at,
        "deadlines": deadlines,
        "deadline_evidence": deadline_evidence,
        "deadline_status": deadline_status,
        "deadline_pages": [page.final_url for page in pages],
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
