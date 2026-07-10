import io
from collections.abc import Callable, Sequence
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin

from markitdown import MarkItDown
from markitdown._stream_info import StreamInfo

_md = MarkItDown()

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
) -> str:
    extension = ".pdf" if "pdf" in content_type.lower() else ".html"
    info = StreamInfo(extension=extension, mimetype=content_type, url=url)
    result = _md.convert_stream(io.BytesIO(content), stream_info=info)
    text = result.text_content or ""
    for text_filter in text_filters or ():
        text = text_filter(text)
    return text


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
        seen.add(absolute)
        out.append((text, absolute))
    return out
