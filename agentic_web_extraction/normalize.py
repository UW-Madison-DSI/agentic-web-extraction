import io
from collections.abc import Callable, Sequence
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup
from markitdown import MarkItDown
from markitdown._stream_info import StreamInfo

_md = MarkItDown()

# Always parsed with the stdlib parser, never lxml-if-available: which parser bs4
# picks changes the markup it emits, which changes the normalized markdown, which
# changes the content hash every cache key is built on. A cache that silently
# misses because a wheel is present on one machine and not another is worse than
# a marginally slower parse.
_PARSER = "html.parser"

# Elements that carry no reader-visible text. Dropped whenever DOM filtering runs.
INVISIBLE_TAGS = ("script", "style", "noscript", "template", "head")

# Site chrome: dropped only when it is NOT inside a `main` or `article`. An
# article's own `<header>` is where its title and byline live -- exactly the
# concrete values a target schema asks for -- so the naive "remove every header"
# rule loses data on precisely the pages worth extracting from.
CHROME_TAGS = ("header", "footer", "nav", "aside")
_CONTENT_ANCESTORS = ("main", "article")


def strip_boilerplate(
    html: str | bytes, *, main_content_only: bool, from_encoding: str | None = None
) -> str:
    """Remove non-content elements from `html` before the Markdown conversion.

    Site-agnostic by construction: it names only standard HTML sectioning
    elements, so it is not the site-specific text munging this module refuses to
    carry (that stays with the caller, in `text_filters`).

    This governs the *extraction input* only. Link discovery reads the unfiltered
    HTML (see `Extractor._process_page`), so filtering a navigation block out of
    the markdown never hides the links inside it from the scorer.
    """
    # Bytes are handed over undecoded on purpose: BeautifulSoup consults the
    # document's own `<meta charset>` (and a BOM, and the byte pattern) to decide
    # the encoding, which pre-decoding here would throw away -- a windows-1251
    # page whose charset is declared only in the markup would arrive as
    # replacement characters. `from_encoding` supplies the HTTP header's charset
    # as a hint when there is one; bs4 still overrides it if the document
    # disagrees, which is the right precedence for a mislabelled response.
    soup = BeautifulSoup(html, _PARSER, from_encoding=from_encoding)
    doomed = list(soup.find_all(INVISIBLE_TAGS))
    if main_content_only:
        for element in soup.find_all(CHROME_TAGS):
            if not element.find_parents(_CONTENT_ANCESTORS):
                doomed.append(element)
    for element in doomed:
        element.decompose()
    return str(soup)


# A text filter is a pure `str -> str` transform applied to the normalized
# markdown. The library ships none: it is deliberately site-agnostic and does
# not know about any particular website. Callers that need to strip volatile,
# per-response tokens (rotating anti-bot tokens, per-render timestamps,
# shuffled recommendation strips) so a page's content hash stays stable across
# fetches pass their own filters via `Extractor(text_filters=...)`. See
# examples/strippers.py for a ready-made set keyed to specific real-world sites.
TextFilter = Callable[[str], str]


def to_markdown(
    content: bytes,
    content_type: str,
    url: str | None = None,
    text_filters: Sequence[TextFilter] | None = None,
    *,
    main_content_only: bool = False,
) -> str:
    is_pdf = "pdf" in content_type.lower()
    extension = ".pdf" if is_pdf else ".html"
    if not is_pdf and main_content_only:
        # Re-encoded as UTF-8 after the parse, and the mimetype is restated to
        # match: bs4 has decoded the bytes using the document's own declaration,
        # so handing markitdown the original charset would have it decode UTF-8 as
        # something else.
        content = strip_boilerplate(
            content,
            main_content_only=True,
            from_encoding=_declared_charset(content_type),
        ).encode("utf-8")
        content_type = "text/html; charset=utf-8"
    info = StreamInfo(extension=extension, mimetype=content_type, url=url)
    result = _md.convert_stream(io.BytesIO(content), stream_info=info)
    text = result.text_content or ""
    for text_filter in text_filters or ():
        text = text_filter(text)
    return text


def _declared_charset(content_type: str) -> str | None:
    """The `charset=` from a Content-Type header, or None.

    A *hint* for the parser, not a decision: plenty of responses declare one
    charset in the header and another in the markup, and the document is the more
    reliable of the two.
    """
    for part in content_type.split(";")[1:]:
        name, _, value = part.strip().partition("=")
        if name.strip().lower() == "charset":
            return value.strip().strip("\"'") or None
    return None


# URL path extensions whose responses `fetch._classify` could never accept as
# content: it admits HTML and PDF and nothing else, so a link to a `.zip` is
# fetched, classified `skipped`, and dropped -- after the link scorer has already
# been billed for ranking it. Dropping them here is therefore free of any change
# in outcome; it only removes work that was always wasted.
#
# `.pdf` is deliberately absent, and deliberately NOT gated on `follow_pdf`: this
# filter runs in the worker, whose output is what the PAGE cache stores, so
# anything that varies with configuration would be baked into a cached entry and
# replayed under a different configuration later. A pure function of the URL is
# the only kind of filter that is safe here (the same reasoning that keeps the
# crawl boundary at `frontier.push`). With `follow_pdf` off, `fetch` still skips
# the PDF cheaply.
NON_CONTENT_EXTENSIONS = frozenset(
    {
        # images
        "apng",
        "avif",
        "bmp",
        "gif",
        "heic",
        "ico",
        "jpeg",
        "jpg",
        "png",
        "svg",
        "tif",
        "tiff",
        "webp",
        # audio / video
        "aac",
        "avi",
        "flac",
        "flv",
        "m4a",
        "m4v",
        "mkv",
        "mov",
        "mp3",
        "mp4",
        "mpeg",
        "mpg",
        "oga",
        "ogg",
        "ogv",
        "opus",
        "wav",
        "webm",
        "wmv",
        # archives and disk images
        "7z",
        "bz2",
        "dmg",
        "gz",
        "iso",
        "rar",
        "tar",
        "tgz",
        "xz",
        "zip",
        "zst",
        # executables and packages
        "apk",
        "bat",
        "deb",
        "dll",
        "exe",
        "jar",
        "msi",
        "pkg",
        "rpm",
        "whl",
        # fonts
        "eot",
        "otf",
        "ttf",
        "woff",
        "woff2",
        # web assets
        "css",
        "js",
        "map",
        "mjs",
        "wasm",
        # data and office formats (readable by nothing in the fetch path)
        "csv",
        "doc",
        "docx",
        "epub",
        "json",
        "odp",
        "ods",
        "odt",
        "ppt",
        "pptx",
        "rtf",
        "tsv",
        "xls",
        "xlsx",
        "xml",
        "yaml",
        "yml",
    }
)


def is_probably_content_url(url: str) -> bool:
    """False when `url`'s path ends in an extension no fetch could read as a page.

    Extension-only, and a deny list rather than an allow list: `.php`, `.aspx`,
    `.do` and every extensionless URL are pages, and an allow list would have to
    know all of them.
    """
    try:
        path = urlsplit(url).path
    except ValueError:
        return True  # unparseable is not evidence; let the fetch decide
    _, dot, extension = path.rpartition(".")
    if not dot or "/" in extension:
        return True
    return extension.lower() not in NON_CONTENT_EXTENSIONS


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._capture_href: str | None = None
        self._buffer: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self._capture_href = value
                self._buffer = []
                return

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._capture_href is not None:
            text = " ".join("".join(self._buffer).split()).strip()
            self.links.append((text, self._capture_href))
            self._capture_href = None
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capture_href is not None:
            self._buffer.append(data)


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Absolute, deduplicated `(anchor text, url)` pairs, in document order.

    Links whose extension no fetch could read as a page are dropped (see
    `is_probably_content_url`). Everything else is returned: the per-page cap is
    applied by the caller, *after* it has filtered out links the crawl has already
    seen -- capping here would spend the whole allowance on the site-wide
    navigation that appears at the top of every page, and starve the frontier of
    the links that make each page different.
    """
    parser = _LinkParser()
    parser.feed(html)
    parser.close()
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for text, href in parser.links:
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute, _ = urldefrag(urljoin(base_url, href))
        if not absolute.startswith(("http://", "https://")):
            continue
        if absolute in seen:
            continue
        if not is_probably_content_url(absolute):
            continue
        seen.add(absolute)
        out.append((text, absolute))
    return out
