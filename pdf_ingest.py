"""Turn an inbox PDF into a Markdown note plus figure files.

Text and tables come from the PDF text layer. Embedded images are saved as
files. Pages that contain drawings or figures are also rendered, so vector
charts that are not embedded images still land in the note.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Sequence

import pymupdf as fitz

MAX_RENDERED_PAGES = 80
MIN_IMAGE_PX = 64
RENDER_DPI = 110


def _clean(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _table_markdown(rows: Sequence[Sequence]) -> str:
    cleaned = []
    for row in rows:
        cells = [re.sub(r"\s+", " ", str(cell or "")).strip().replace("|", "\\|") for cell in row]
        if any(cells):
            cleaned.append(cells)
    if not cleaned:
        return ""
    width = max(len(r) for r in cleaned)
    cleaned = [r + [""] * (width - len(r)) for r in cleaned]
    header = cleaned[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in cleaned[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _rects_overlap(a: fitz.Rect, b: fitz.Rect) -> bool:
    return a.intersects(b)


def _save_pixmap(pix: fitz.Pixmap, dest: Path) -> None:
    if pix.n - pix.alpha > 3:
        pix = fitz.Pixmap(fitz.csRGB, pix)
    dest.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(dest))


def convert_pdf(pdf_path: Path, md_path: Optional[Path] = None) -> Path:
    """Write a Markdown note beside the PDF and return that note's path."""
    pdf_path = Path(pdf_path)
    md_path = Path(md_path) if md_path else pdf_path.with_suffix(".md")
    stem = md_path.stem
    doc = fitz.open(pdf_path)
    parts: List[str] = [
        "---",
        "tags: [pdf-ingest, research-intake]",
        f"source_pdf: {pdf_path.name}",
        f"pages: {doc.page_count}",
        "---",
        "",
        f"# {stem}",
        "",
        f"Source PDF: `{pdf_path.name}`",
        "",
    ]
    rendered = 0
    try:
        for index, page in enumerate(doc, 1):
            tables = []
            try:
                found = page.find_tables()
                tables = list(found.tables) if found is not None else []
            except Exception:
                tables = []
            table_rects = []
            table_md = []
            for table in tables:
                try:
                    table_rects.append(fitz.Rect(table.bbox))
                    md = _table_markdown(table.extract() or [])
                except Exception:
                    continue
                if md:
                    table_md.append(md)

            blocks = []
            try:
                for block in page.get_text("blocks"):
                    if len(block) < 5:
                        continue
                    rect = fitz.Rect(block[:4])
                    text = _clean(str(block[4]))
                    if not text:
                        continue
                    if any(_rects_overlap(rect, tre) for tre in table_rects):
                        continue
                    blocks.append(text)
            except Exception:
                raw = _clean(page.get_text("text") or "")
                if raw:
                    blocks.append(raw)

            image_names = []
            seen_xref = set()
            for img_i, img in enumerate(page.get_images(full=True), 1):
                xref = img[0]
                if xref in seen_xref:
                    continue
                seen_xref.add(xref)
                try:
                    pix = fitz.Pixmap(doc, xref)
                except Exception:
                    continue
                if pix.width < MIN_IMAGE_PX or pix.height < MIN_IMAGE_PX:
                    continue
                name = f"{stem}-p{index}-img{img_i}.png"
                try:
                    _save_pixmap(pix, md_path.parent / name)
                except Exception:
                    continue
                image_names.append(name)

            drawings = []
            try:
                drawings = page.get_drawings()
            except Exception:
                drawings = []
            page_name = None
            if rendered < MAX_RENDERED_PAGES and (image_names or len(drawings) >= 4):
                try:
                    pix = page.get_pixmap(dpi=RENDER_DPI, alpha=False)
                    page_name = f"{stem}-p{index}.png"
                    _save_pixmap(pix, md_path.parent / page_name)
                    rendered += 1
                except Exception:
                    page_name = None

            section = [f"## Page {index}", ""]
            if blocks:
                section.append("\n\n".join(blocks))
                section.append("")
            for md in table_md:
                section.append(md)
                section.append("")
            for name in image_names:
                section.append(f"![[{name}]]")
                section.append("")
            if page_name and page_name not in image_names:
                section.append(f"![[{page_name}]]")
                section.append("")
            if len(section) > 2:
                parts.extend(section)
    finally:
        doc.close()

    md_path.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    return md_path


def markdown_is_current(pdf_path: Path, md_path: Optional[Path] = None) -> bool:
    md_path = Path(md_path) if md_path else pdf_path.with_suffix(".md")
    if not md_path.exists():
        return False
    try:
        return md_path.stat().st_mtime >= pdf_path.stat().st_mtime
    except OSError:
        return False
