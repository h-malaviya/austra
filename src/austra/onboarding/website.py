"""Pinned public-IP fetcher for a small administrator supplied website scope."""

import http.client
import ipaddress
import socket
import ssl
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_PAGES = 12
MAX_FULL_PAGES = 300
MAX_DISCOVERED_URLS = 1000
MAX_REDIRECTS = 3
FETCH_TIMEOUT_SECONDS = 8


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, port: int):
        super().__init__(
            host, port, timeout=FETCH_TIMEOUT_SECONDS, context=ssl.create_default_context()
        )
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host: str, address: str, port: int):
        super().__init__(host, port, timeout=FETCH_TIMEOUT_SECONDS)
        self.address = address

    def connect(self):
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


def public_target(url: str) -> tuple[str, str, int, str]:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("A public HTTP or HTTPS URL is required")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as error:
        raise ValueError("Invalid website port") from error
    if port not in {80, 443}:
        raise ValueError("Website port is not allowed")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise ValueError("Website must resolve to a public address")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except socket.gaierror as error:
        raise ValueError("Website address could not be resolved") from error
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise ValueError("Website must resolve only to public addresses")
    path = parsed.path or "/"
    target = urlunsplit((parsed.scheme, host, path, parsed.query, ""))
    return host, min(addresses), port, target


def fetch_page(url: str) -> tuple[str, bytes]:
    for _ in range(MAX_REDIRECTS + 1):
        host, address, port, safe_url = public_target(url)
        parsed = urlsplit(safe_url)
        connection = (_PinnedHTTPS if parsed.scheme == "https" else _PinnedHTTP)(
            host, address, port
        )
        try:
            connection.request(
                "GET",
                parsed.path + (f"?{parsed.query}" if parsed.query else ""),
                headers={
                    "Host": host,
                    "Accept": "text/html",
                    "Accept-Encoding": "identity",
                    "User-Agent": "AustraEvidenceBot/1.0",
                },
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Website redirect has no destination")
                url = urljoin(safe_url, location)
                continue
            if response.status != 200:
                raise ValueError(f"Website returned HTTP {response.status}")
            if not response.getheader("Content-Type", "").lower().split(";", 1)[0] in {
                "text/html",
                "application/xhtml+xml",
            }:
                raise ValueError("Website response is not HTML")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("Compressed website response is not accepted")
            size = response.getheader("Content-Length")
            if size and int(size) > MAX_PAGE_BYTES:
                raise ValueError("Website response is too large")
            data = response.read(MAX_PAGE_BYTES + 1)
            if len(data) > MAX_PAGE_BYTES:
                raise ValueError("Website response is too large")
            return safe_url, data
        finally:
            connection.close()
    raise ValueError("Too many website redirects")


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text: list[str] = []
        self.links: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.skip += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag in {"h1", "h2", "h3", "p", "li", "tr", "section"}:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.text.append(data.strip())


def parse_page(content: bytes) -> PageParser:
    parser = PageParser()
    parser.feed(content.decode("utf-8", errors="replace"))
    return parser


def same_site_host(first: str | None, second: str | None) -> bool:
    return bool(first and second and first.removeprefix("www.") == second.removeprefix("www."))


@dataclass(frozen=True)
class CrawlReport:
    pages: list[tuple[str, bytes]]
    failed_urls: list[str]
    truncated: bool
    page_limit: int


def crawl_report(root: str, page_limit: int = MAX_PAGES) -> CrawlReport:
    """Follow discoverable public HTML links across the supplied site."""
    root_host = urlsplit(root).hostname
    queue = deque([root])
    queued = {root}
    seen: set[str] = set()
    pages: list[tuple[str, bytes]] = []
    failed_urls: list[str] = []
    discovery_limited = False
    while queue and len(pages) < page_limit:
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        try:
            final_url, data = fetch_page(url)
        except (ValueError, OSError):
            if url == root:
                raise
            failed_urls.append(url)
            continue
        if not same_site_host(urlsplit(final_url).hostname, root_host):
            if url == root:
                raise ValueError("Website redirected outside the supplied host")
            failed_urls.append(url)
            continue
        if final_url in seen and final_url != url and any(page[0] == final_url for page in pages):
            continue
        seen.add(final_url)
        pages.append((final_url, data))
        for href in parse_page(data).links:
            candidate = urljoin(final_url, href)
            parsed = urlsplit(candidate)
            if not (
                same_site_host(parsed.hostname, root_host)
                and parsed.scheme in {"http", "https"}
                and not parsed.query
                and not parsed.path.lower().endswith(
                    (".pdf", ".jpg", ".jpeg", ".png", ".svg", ".webp", ".zip")
                )
            ):
                continue
            normalized = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))
            if normalized not in queued:
                if len(queued) >= MAX_DISCOVERED_URLS:
                    discovery_limited = True
                    continue
                queue.append(normalized)
                queued.add(normalized)
    return CrawlReport(pages, failed_urls, bool(queue) or discovery_limited, page_limit)


def crawl(root: str):
    yield from crawl_report(root).pages
