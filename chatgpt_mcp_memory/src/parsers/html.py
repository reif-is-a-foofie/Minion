"""HTML parser. Uses trafilatura when available (strips nav/footer/boilerplate),
falls back to a very small html.parser stripper otherwise."""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from . import ParsedChunk, ParseResult
from ._common import chunk_text


class _StripTags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._buf: list[str] = []
        self._skip = 0

    _SKIP_TAGS = {"script", "style", "noscript"}

    def handle_starttag(self, tag, attrs):  # type: ignore[override]
        if tag in self._SKIP_TAGS:
            self._skip += 1

    def handle_endtag(self, tag):  # type: ignore[override]
        if tag in self._SKIP_TAGS and self._skip:
            self._skip -= 1

    def handle_data(self, data):  # type: ignore[override]
        if self._skip:
            return
        self._buf.append(data)

    def text(self) -> str:
        return "\n".join(self._buf)


def _extract_with_trafilatura(raw: str) -> str | None:
    try:
        import trafilatura  # type: ignore
    except Exception:
        return None
    extracted = trafilatura.extract(raw, include_comments=False, include_tables=True)
    return extracted or None


def parse(path: Path) -> ParseResult:
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = _extract_with_trafilatura(raw)
    parser_name = "trafilatura"
    if not text:
        parser_name = "html.parser"
        stripper = _StripTags()
        stripper.feed(raw)
        text = stripper.text()

    chunks = [
        ParsedChunk(text=t, role=None, meta={"seq": i})
        for i, t in enumerate(chunk_text(text))
    ]
    return ParseResult(
        chunks=chunks,
        source_meta={"extractor": parser_name},
        kind="html",
        parser=parser_name,
    )
