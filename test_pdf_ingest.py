import tempfile
import unittest
from pathlib import Path

import pymupdf as fitz

import pdf_ingest


class PdfIngest(unittest.TestCase):
    def test_extracts_text_table_image_and_chart_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            pdf_path = folder / "sample_study.pdf"
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((72, 72), "Ferro A5 substrate loss study")
            # A drawn chart: several strokes, not an embedded image.
            shape = page.new_shape()
            shape.draw_line((72, 200), (300, 200))
            shape.draw_line((72, 200), (72, 400))
            shape.draw_line((72, 380), (120, 300))
            shape.draw_line((120, 300), (180, 340))
            shape.draw_line((180, 340), (240, 240))
            shape.draw_line((240, 240), (300, 260))
            shape.finish(color=(0.8, 0.1, 0.1), width=1.5)
            shape.commit()
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 80, 80), 0)
            pix.set_rect(pix.irect, (10, 120, 40))
            page.insert_image(fitz.Rect(360, 72, 460, 172), pixmap=pix)
            # Table via a simple text grid plus find_tables needs lines.
            rows = [["Material", "Loss"], ["Ferro A5", "0.4"], ["FR4", "1.2"]]
            y = 460
            shape = page.new_shape()
            for r, row in enumerate(rows):
                for c, cell in enumerate(row):
                    x = 72 + c * 120
                    rect = fitz.Rect(x, y + r * 24, x + 120, y + (r + 1) * 24)
                    shape.draw_rect(rect)
                    page.insert_textbox(rect, cell, fontsize=10)
            shape.finish(color=(0, 0, 0), width=0.6)
            shape.commit()
            doc.save(pdf_path)
            doc.close()

            md = pdf_ingest.convert_pdf(pdf_path)
            text = md.read_text(encoding="utf-8")
            self.assertIn("Ferro A5 substrate loss study", text)
            self.assertIn("source_pdf: sample_study.pdf", text)
            self.assertIn("| Ferro A5 | 0.4 |", text)
            pngs = list(folder.glob("sample_study-p*.png"))
            self.assertTrue(any(p.name == "sample_study-p1.png" for p in pngs))
            self.assertTrue(any("![[" in line and line.strip().endswith("]]") for line in text.splitlines()))
            self.assertTrue(pdf_ingest.markdown_is_current(pdf_path, md))


if __name__ == "__main__":
    unittest.main()
