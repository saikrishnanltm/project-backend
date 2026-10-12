"""
Document chunking for uploaded prose files (Markdown, text, HTML, PDF...).

chunker.py is built for code: it splits on functions/classes. Prose has no
such units, so this module splits on what prose does have -- headings and
paragraphs -- and keeps each chunk citable:

  * Markdown: chunks never cross a heading; the heading path
    ("Setup > Docker") is stored as the chunk's qualified_name and
    prepended to the chunk text so it helps retrieval.
  * Plain text / HTML / CSV / JSON: split on blank-line paragraphs.
  * PDF: one page at a time (chunks never cross pages); the page number
    is used for the citation's start/end "line" fields.

Paragraphs are packed greedily up to settings.max_chunk_chars. A single
paragraph longer than that is split on sentence boundaries, then hard-cut
with a small overlap as a last resort.
"""
from __future__ import annotations

import io
import os
import re
from html.parser import HTMLParser

from app.chunker import Chunk, _make_id
from app.config import settings

SUPPORTED_EXTENSIONS = {
    ".md", ".markdown", ".txt", ".rst", ".html", ".htm", ".pdf", ".csv", ".json",
}
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
OVERLAP_CHARS = 150


class UnsupportedDocument(ValueError):
    """Unsupported type, or a file we couldn't extract any text from."""


# ---------------------------------------------------------------- extraction

class _HTMLText(HTMLParser):
    _SKIP = {"script", "style", "noscript", "head"}
    _BLOCK = {"p", "div", "br", "li", "tr", "section", "article", "h1", "h2",
              "h3", "h4", "h5", "h6", "pre", "blockquote", "table", "ul", "ol"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n\n" if tag in {"p", "div", "section", "article", "table"} or tag[0] == "h" else "\n")
            if tag[0] == "h" and len(tag) == 2 and tag[1].isdigit():
                self.parts.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_markdownish(html: str) -> str:
    p = _HTMLText()
    p.feed(html)
    text = "".join(p.parts)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_pdf_pages(data: bytes) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover
        raise UnsupportedDocument("PDF support needs the 'pypdf' package installed.") from e
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise UnsupportedDocument("Encrypted PDFs aren't supported.")
        return [(page.extract_text() or "") for page in reader.pages]
    except UnsupportedDocument:
        raise
    except Exception as e:
        raise UnsupportedDocument(f"Couldn't read this PDF: {e}") from e


# ------------------------------------------------------------------ packing

def _split_long(par: str, limit: int) -> list[str]:
    """Split one oversized paragraph: sentences first, hard cut with overlap if needed."""
    pieces, cur = [], ""
    for sent in _SENTENCE_RE.split(par):
        if len(sent) > limit:  # a "sentence" with no punctuation (log line, minified data)
            if cur:
                pieces.append(cur)
                cur = ""
            step = max(limit - OVERLAP_CHARS, 1)
            pieces.extend(sent[i:i + limit] for i in range(0, len(sent), step))
        elif cur and len(cur) + 1 + len(sent) > limit:
            pieces.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip() if cur else sent
    if cur:
        pieces.append(cur)
    return pieces


def _blocks(lines: list[str], base_line: int = 1, markdown: bool = False):
    """Yields (text, start_line, end_line, heading_path) paragraph blocks.
    A new heading (markdown) or a blank line ends the current block."""
    path: list[tuple[int, str]] = []
    cur: list[str] = []
    cur_start = base_line
    in_fence = False

    def flush(end_idx):
        nonlocal cur, cur_start
        if cur and "".join(cur).strip():
            yield ("\n".join(cur).strip("\n"), cur_start, end_idx, [t for _, t in path])
        cur = []

    for i, line in enumerate(lines):
        lineno = base_line + i
        if markdown and _FENCE_RE.match(line):
            in_fence = not in_fence
        m = _HEADING_RE.match(line) if markdown and not in_fence else None
        if m:
            yield from flush(lineno - 1)
            level = len(m.group(1))
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, m.group(2)))
            cur_start = lineno + 1
            continue
        if not line.strip() and not in_fence:
            yield from flush(lineno - 1)
            cur_start = lineno + 1
            continue
        if not cur:
            cur_start = lineno
        cur.append(line)
    yield from flush(base_line + len(lines) - 1)


def _pack(file_path, blocks, kind, label_prefix=""):
    """Greedy-pack consecutive blocks that share a heading path into chunks."""
    limit = max(settings.max_chunk_chars, 200)
    chunks: list[Chunk] = []
    group: list[tuple[str, int, int]] = []
    group_path: list[str] = []
    n = 0

    def emit(parts, path):
        nonlocal n
        text = "\n\n".join(t for t, _, _ in parts)
        start, end = parts[0][1], parts[-1][2]
        heading = " > ".join(path)
        name = heading or f"{os.path.basename(file_path)}#part{n}"
        if label_prefix:
            name = f"{label_prefix} {name}" if heading else label_prefix
        body = f"{heading}\n\n{text}" if heading else text
        chunks.append(Chunk(
            id=_make_id(file_path, f"{name}#{n}", start),
            file_path=file_path, kind=kind, qualified_name=name,
            start_line=start, end_line=end, code=body,
            metadata={"heading_path": heading},
        ))
        n += 1

    def flush():
        nonlocal group
        if group:
            emit(group, group_path)
        group = []

    for text, start, end, path in blocks:
        if path != group_path:
            flush()
            group_path = path
        pieces = _split_long(text, limit) if len(text) > limit else [text]
        for piece in pieces:
            size = sum(len(t) + 2 for t, _, _ in group)
            if group and size + len(piece) > limit:
                flush()
            group.append((piece, start, end))
    flush()
    return chunks


def chunk_document(file_path: str, data: bytes) -> list[Chunk]:
    """Raw uploaded bytes -> citable chunks. Raises UnsupportedDocument."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedDocument(
            f"'{file_path}': unsupported file type '{ext or '(none)'}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    if ext == ".pdf":
        chunks: list[Chunk] = []
        for page_no, page_text in enumerate(extract_pdf_pages(data), start=1):
            if not page_text.strip():
                continue
            lines = page_text.splitlines()
            page_chunks = _pack(file_path, _blocks(lines), "pdf_section", label_prefix=f"p.{page_no}")
            for c in page_chunks:
                c.start_line = c.end_line = page_no   # citations show the page number
                c.id = _make_id(file_path, c.qualified_name + f"#{len(chunks)}", page_no)
                chunks.append(c)
        if not chunks:
            raise UnsupportedDocument(
                f"'{file_path}' has no extractable text (is it a scanned PDF? OCR isn't supported)."
            )
        return chunks

    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in text[:1000]:
        raise UnsupportedDocument(f"'{file_path}' looks like a binary file, not text.")
    markdown = ext in {".md", ".markdown"}
    kind = "doc_section" if markdown else "doc_text"
    if ext in {".html", ".htm"}:
        text, markdown, kind = html_to_markdownish(text), True, "doc_section"
    if not text.strip():
        raise UnsupportedDocument(f"'{file_path}' is empty.")

    chunks = _pack(file_path, _blocks(text.split("\n"), markdown=markdown), kind)
    if not chunks:
        raise UnsupportedDocument(f"'{file_path}' has no extractable text.")
    return chunks
