"""Bounded RSS/Atom collection. No article HTML, JavaScript or redirects are executed."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import socket
import ssl
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import urlsplit

from defusedxml import ElementTree

from .contracts import SourceDocument
from .policy import canonicalize_url


class CollectionError(ValueError):
    pass


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.values.append(data)


def plain_text(value):
    parser = TextOnly()
    parser.feed(value)
    return " ".join(" ".join(parser.values).split())


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # Connect to the validated IP while preserving TLS SNI/certificate host validation.
        sock = socket.create_connection((self.address, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def public_addresses(host):
    addresses = sorted(
        {record[4][0] for record in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    )
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise CollectionError("non_public_dns")
    return addresses


class SafeFetcher:
    def __init__(self, max_bytes=1_000_000, deadline=None):
        self.max_bytes = max_bytes
        self.deadline = deadline or time.monotonic() + 60

    def fetch(self, url, allowed_hosts, mime="feed"):
        try:
            normalized = canonicalize_url(url, allowed_hosts)
            parsed = urlsplit(normalized)
            addresses = public_addresses(parsed.hostname)
            remaining = min(12, self.deadline - time.monotonic())
            if remaining <= 0:
                raise CollectionError("collection_deadline")
            connection = PinnedHTTPS(parsed.hostname, addresses[0], remaining)
            try:
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
                connection.request(
                    "GET",
                    path,
                    headers={
                        "User-Agent": "ArchDeckResearch/1.0",
                        "Accept-Encoding": "identity",
                        "Accept": (
                            "application/rss+xml,application/atom+xml,"
                            "application/xml,text/xml,text/plain"
                        ),
                    },
                )
                response = connection.getresponse()
                if response.status != 200:
                    raise CollectionError(f"source_http_{response.status}")
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise CollectionError("compressed_content_not_allowed")
                content_type = response.getheader("Content-Type", "").split(";", 1)[0].lower()
                accepted = (
                    {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"}
                    if mime == "feed"
                    else {"text/plain"}
                )
                if content_type not in accepted:
                    raise CollectionError("unsupported_source_mime")
                body = bytearray()
                while True:
                    if time.monotonic() > self.deadline:
                        raise CollectionError("collection_deadline")
                    chunk = response.read(min(65536, self.max_bytes + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > self.max_bytes:
                        raise CollectionError("source_size_limit")
                return bytes(body)
            finally:
                connection.close()
        except CollectionError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise CollectionError("source_unavailable") from None

    def feed(self, url, allowed_hosts):
        parsed = urlsplit(canonicalize_url(url, allowed_hosts))
        robots = self.fetch(f"https://{parsed.hostname}/robots.txt", allowed_hosts, mime="robots")
        rules = robotparser.RobotFileParser()
        rules.parse(robots.decode("utf-8", errors="replace").splitlines())
        if not rules.can_fetch("ArchDeckResearch", url):
            raise CollectionError("robots_disallowed")
        delay = rules.crawl_delay("ArchDeckResearch") or 0
        if delay > 0:
            raise CollectionError("crawl_delay_requires_scheduled_fetch")
        return self.fetch(url, allowed_hosts)


def parse_feed(body, host, observed_at, limit, reuse_allowed):
    root = ElementTree.fromstring(body)
    documents, rejected = [], []
    atom = "{http://www.w3.org/2005/Atom}"
    entries = root.findall("./channel/item") + root.findall(f"{atom}entry")
    for entry in entries[:100]:
        if len(documents) >= limit:
            break
        title = entry.findtext("title") or entry.findtext(f"{atom}title") or ""
        url = entry.findtext("link") or ""
        if not url:
            link = entry.find(f"{atom}link")
            url = link.get("href", "") if link is not None else ""
        published = (
            entry.findtext("pubDate")
            or entry.findtext(f"{atom}published")
            or entry.findtext(f"{atom}updated")
        )
        excerpt = (
            entry.findtext("description")
            or entry.findtext(f"{atom}summary")
            or entry.findtext(f"{atom}content")
            or ""
        )
        try:
            canonical = canonicalize_url(url, {host})
            if not published:
                raise ValueError("missing_publication_date")
            try:
                date = parsedate_to_datetime(published)
            except (TypeError, ValueError):
                date = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if date.tzinfo is None:
                raise ValueError("timezone_missing")
            documents.append(
                SourceDocument(
                    source_id=hashlib.sha256(canonical.encode()).hexdigest()[:32],
                    url=canonical,
                    title=plain_text(title)[:300],
                    excerpt=plain_text(excerpt)[:2000],
                    published_at=date.astimezone(UTC),
                    observed_at=observed_at,
                    reuse_allowed=reuse_allowed,
                )
            )
        except ValueError:
            rejected.append(
                {"reason": "invalid_or_undated_entry", "title": plain_text(title)[:150]}
            )
    return documents, rejected
