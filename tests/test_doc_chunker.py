import sys

sys.path.insert(0, ".")
import pytest  # noqa: E402

from app.doc_chunker import UnsupportedDocument, chunk_document  # noqa: E402

MD = b"""# Guide

Intro paragraph.

## Setup

Install it.

```bash
# not a heading
pip install thing
```

## Usage

Run it.
"""


def test_markdown_splits_on_headings_with_paths_and_lines():
    chunks = chunk_document("guide.md", MD)
    names = [c.qualified_name for c in chunks]
    assert names == ["Guide", "Guide > Setup", "Guide > Usage"]
    setup = chunks[1]
    assert "pip install thing" in setup.code and "# not a heading" in setup.code
    assert setup.start_line == 7 and setup.end_line == 12
    assert setup.code.startswith("Guide > Setup")


def test_long_paragraph_is_split_under_limit():
    from app.config import settings
    text = ("Sentence number one is here. " * 200).encode()
    chunks = chunk_document("a.txt", text)
    assert len(chunks) > 1
    assert all(len(c.code) <= settings.max_chunk_chars + 5 for c in chunks)


def test_unbroken_blob_is_hard_split():
    chunks = chunk_document("blob.txt", b"x" * 5000)
    assert len(chunks) >= 3


def test_html_headings_become_paths():
    html = b"<html><head><style>p{}</style></head><body><h1>Title</h1><p>Hello there world.</p><h2>Sub</h2><p>More text.</p></body></html>"
    names = [c.qualified_name for c in chunk_document("p.html", html)]
    assert names == ["Title", "Title > Sub"]


def test_pdf_pages_cited_by_page_number():
    pytest.importorskip("pypdf")
    from pypdf import PdfWriter
    # Build a tiny 2-page PDF with text via reportlab if present, else skip.
    reportlab = pytest.importorskip("reportlab.pdfgen.canvas")
    import io
    buf = io.BytesIO()
    c = reportlab.Canvas(buf)
    c.drawString(72, 720, "First page text about alpha.")
    c.showPage()
    c.drawString(72, 720, "Second page text about beta.")
    c.save()
    chunks = chunk_document("r.pdf", buf.getvalue())
    assert [(x.start_line, x.end_line) for x in chunks] == [(1, 1), (2, 2)]


def test_ids_are_unique():
    chunks = chunk_document("guide.md", MD * 3)
    assert len({c.id for c in chunks}) == len(chunks)


def test_rejects_unsupported_and_binary_and_empty():
    with pytest.raises(UnsupportedDocument):
        chunk_document("x.exe", b"MZ")
    with pytest.raises(UnsupportedDocument):
        chunk_document("x.txt", b"ab\x00cd")
    with pytest.raises(UnsupportedDocument):
        chunk_document("x.md", b"   \n")
