"""Seed the frontier from a site's own sitemap (opt-in, ``AWE_USE_SITEMAP``).

Best-first search is only as good as the links it is offered, and its weakest
spot is a page nothing links to prominently: page 12 of a listing sits behind a
paginator no scorer ranks highly, so a ten-fetch budget spent on the top of the
navigation never reaches it. A sitemap is the site telling us what it has, in one
request, before the crawl starts.

What this module does **not** do is bypass the traversal. Discovered URLs are
handed to the same link scorer, gated by the same crawl boundary, and (with
``respect_robots`` on) checked against the same policy as any other link. They
enter the frontier; they do not skip it. The alternative -- pushing a thousand
unscored URLs at a fixed score -- would drown the ranking that is the whole
navigation policy.

Discovery follows the usual order: ``robots.txt`` ``Sitemap:`` lines first, since
that is where a site declares them, then ``/sitemap.xml`` as the conventional
fallback. Index documents are followed one level at a time until
``AWE_SITEMAP_MAX_DOCUMENTS`` is spent.

**The XML here is written by somebody else.** Three bounds, all deliberate:

* ``AWE_SITEMAP_MAX_BYTES`` caps a body before it is parsed, so "serve a 2 GB
  file" costs one truncated read.
* A body declaring a DTD or an entity is refused unparsed. ``ElementTree``
  expands internal general entities, so the billion-laughs construction really
  does work against it -- and a sitemap has no legitimate reason to carry one.
  Refusing the construct outright is a check that cannot be reasoned around,
  which a parser configuration would have to be re-verified on every upgrade.
* Only ``http``/``https`` URLs are returned, so a ``file://`` or ``javascript:``
  ``<loc>`` never reaches the fetch path.

A sitemap that cannot be read is not an error: the crawl proceeds from its seeds
exactly as it did before, with a log line saying nothing was obtained.
"""

from __future__ import annotations

import gzip
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable
from functools import partial
from urllib.parse import urlsplit, urlunsplit

import httpx

from . import fetch as fetch_module
from . import logsink
from .frontier import domain_of

# Same reasoning as robots.py's: a sitemap gates work the crawl is waiting on, so
# a slow origin must not hold a whole run for the page client's full timeout.
SITEMAP_TIMEOUT = httpx.Timeout(15.0, connect=10.0)

# `Sitemap: <url>` in robots.txt. The directive is case-insensitive and is not
# scoped to a User-agent group, so it is read from the whole file.
_SITEMAP_LINE = re.compile(r"^\s*sitemap\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)

# Refused before parsing -- see the module docstring on entity expansion.
_DOCTYPE_OR_ENTITY = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def _localname(tag: str) -> str:
    """`{http://...}urlset` -> `urlset`.

    Sitemaps are namespaced, inconsistently: the 0.84, 0.90 and Google extension
    namespaces all appear in the wild, and some publishers emit none at all.
    Matching on the local name accepts all of them without a namespace table to
    keep current.
    """
    return tag.rpartition("}")[2].lower()


def _origin(url: str) -> str:
    """``scheme://host[:port]`` for an http(s) URL, else ""."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _is_http_url(url: str) -> bool:
    try:
        return urlsplit(url).scheme in ("http", "https")
    except ValueError:
        return False


def _get(
    url: str,
    user_agent: str,
    max_bytes: int,
    delay: float = 0.0,
    max_per_domain: int = 0,
) -> bytes | None:
    """Fetch `url` and return at most `max_bytes` of body, or None on any failure.

    Deliberately the crawl's own client and User-Agent -- a sitemap read under a
    different identity than the pages is the same divergence robots.py refuses --
    and deliberately not `fetch.fetch`, whose job is to classify a *page*: a
    sitemap is neither HTML nor PDF, so that path would report it `skipped`.
    """
    # Through the same pace gate as a page fetch. These are the *first* requests
    # a crawl makes, and an opt-in sitemap pass that opened with half a dozen
    # unspaced hits would undo the politeness the rest of the crawl now has -- at
    # exactly the moment an origin is deciding what we are.
    try:
        with (
            fetch_module.paced(url, delay, max_per_domain),
            fetch_module.get_client().stream(
                "GET",
                url,
                timeout=SITEMAP_TIMEOUT,
                headers={"User-Agent": user_agent} if user_agent else None,
            ) as response,
        ):
            if not 200 <= response.status_code < 300:
                logsink.emit(f"    [sitemap] {url} returned {response.status_code}")
                return None
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= max_bytes:
                    logsink.emit(
                        f"    [sitemap] {url} exceeded {max_bytes} bytes — "
                        f"reading the first {max_bytes} only"
                    )
                    break
            return b"".join(chunks)[:max_bytes]
    except Exception as e:  # noqa: BLE001 - an unobtainable sitemap is not an error
        logsink.emit(f"    [sitemap] {url} unavailable ({type(e).__name__}: {e})")
        return None


def _decompress(url: str, body: bytes) -> bytes:
    """Gunzip a `.gz` sitemap. Sniffed on the magic bytes, not the extension:
    plenty of origins serve `sitemap.xml` gzipped and plenty serve `sitemap.xml.gz`
    already decoded by the transport."""
    if not body.startswith(b"\x1f\x8b"):
        return body
    try:
        return gzip.decompress(body)
    except Exception as e:  # noqa: BLE001
        logsink.emit(f"    [sitemap] {url} is not readable gzip ({type(e).__name__})")
        return b""


def parse(url: str, body: bytes) -> tuple[list[str], list[str]]:
    """Parse one sitemap document into ``(page urls, child sitemap urls)``.

    A ``<sitemapindex>`` yields children; a ``<urlset>`` yields pages. Both are
    read from whichever element type is present rather than from the root's name,
    so a document that mislabels its root still parses usefully.
    """
    body = _decompress(url, body)
    if not body:
        return [], []
    if _DOCTYPE_OR_ENTITY.search(body):
        logsink.emit(
            f"    [sitemap] {url} declares a DTD or an entity — refusing to parse it"
        )
        return [], []
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as e:
        logsink.emit(f"    [sitemap] {url} is not parseable XML ({e})")
        return [], []

    pages: list[str] = []
    children: list[str] = []
    # Driven by the *entry* elements rather than by every `<loc>` in the tree:
    # whether a location is a page or a child sitemap is decided by its parent,
    # and ElementTree elements carry no parent pointer. Reading `<url>`/`<sitemap>`
    # and taking their own `<loc>` gets the answer in one pass instead of relating
    # each `<loc>` back to an ancestor.
    for entry in root.iter():
        name = _localname(entry.tag)
        if name not in ("url", "sitemap"):
            continue
        target = children if name == "sitemap" else pages
        for child in entry:
            if _localname(child.tag) != "loc":
                continue
            location = (child.text or "").strip()
            if location and _is_http_url(location):
                target.append(location)
    return pages, children


def discover(
    seed_url: str,
    *,
    user_agent: str = "",
    max_documents: int = 5,
    max_urls: int = 200,
    max_bytes: int = 10_000_000,
    delay: float = 0.0,
    max_per_domain: int = 0,
    allows: Callable[[str], bool] | None = None,
) -> list[str]:
    """URLs advertised by `seed_url`'s origin, capped at `max_urls`.

    `allows` is an optional ``callable(url) -> bool`` -- the crawl's robots policy
    when one is in force -- consulted before each sitemap *document* is fetched.
    Sitemaps are pages of the site like any other, and a crawl that honours
    robots.txt for its content while reading whatever it likes at ``/sitemap.xml``
    is honouring it selectively.

    Sitemap *documents* are restricted to `seed_url`'s own registrable domain --
    see the guard below on why a site naming its own sitemap locations is not a
    reason to fetch whatever address it names.

    Returns ``[]`` and logs when nothing is obtainable: a site with no sitemap is
    the common case, not a failure.
    """
    origin = _origin(seed_url)
    if not origin:
        return []

    # Which *documents* this pass is willing to fetch. Both sources of sitemap
    # locations -- the `Sitemap:` lines in the origin's robots.txt and the `<loc>`
    # entries of a sitemap index -- are written by the site being crawled, so
    # without this an origin could name any address it liked and have the client
    # request it: another tenant's host, an internal service, a cloud instance's
    # link-local metadata endpoint. The crawl boundary does not cover this, because
    # it gates links entering the *frontier* and these are documents fetched before
    # that. Restricting them to the seed's own registrable domain matches the
    # sitemaps protocol, which scopes a sitemap to its own host anyway, and uses the
    # same `frontier.domain_of` key as every other host comparison in the library.
    home = domain_of(origin)

    def in_scope(url: str) -> bool:
        if _is_http_url(url) and domain_of(url) == home:
            return True
        logsink.emit(f"    [sitemap] ignoring {url} — outside {home or origin}")
        return False

    fetch_document = partial(
        _get,
        user_agent=user_agent,
        max_bytes=max_bytes,
        delay=delay,
        max_per_domain=max_per_domain,
    )

    queue: list[str] = []
    # Deliberately re-fetched even when `respect_robots` already read this origin's
    # policy: `RobotsPolicy` caches a parser, not the body, and this pass has to
    # work with robots off. One extra request per origin, only under an opt-in
    # flag, is the cheaper side of that trade.
    robots_body = fetch_document(f"{origin}/robots.txt")
    if robots_body:
        declared = _SITEMAP_LINE.findall(robots_body.decode("utf-8", errors="replace"))
        queue.extend(url for url in declared if in_scope(url))
        if queue:
            logsink.emit(
                f"    [sitemap] robots.txt at {origin} declares {len(queue)} sitemap(s)"
            )
    if not queue:
        queue.append(f"{origin}/sitemap.xml")

    pages: list[str] = []
    seen_documents: set[str] = set()
    seen_pages: set[str] = set()
    while queue and len(seen_documents) < max_documents and len(pages) < max_urls:
        document = queue.pop(0)
        if document in seen_documents:
            continue
        seen_documents.add(document)
        if allows is not None and not allows(document):
            logsink.emit(f"    [sitemap] {document} disallowed by robots.txt — skipped")
            continue
        body = fetch_document(document)
        if not body:
            continue
        found, children = parse(document, body)
        queue.extend(url for url in children if in_scope(url))
        for url in found:
            if url in seen_pages:
                continue
            seen_pages.add(url)
            pages.append(url)
            if len(pages) >= max_urls:
                break

    if pages:
        logsink.emit(
            f"    [sitemap] {origin} advertises {len(pages)} URL(s) "
            f"(from {len(seen_documents)} document(s), capped at {max_urls})"
        )
    else:
        logsink.emit(f"    [sitemap] no usable sitemap for {origin}")
    return pages
