"""OCR: render every page of the downloaded image-only PDF and run Tesseract.

Two passes per page:
  * full page (psm 6 default layout) -> body text + mean word confidence
  * footer strip (bottom 10%) with psm 6 and psm 11 -> "Transmission FX-nnn Page X of Y"
    The footer is small type; Tesseract often reads "1" as "l". Multiple readings are
    kept and reconciled later in extract.py instead of trusting a single pass.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import pypdfium2 as pdfium
import pytesseract


@dataclass
class PageOCR:
    page: int            # 1-based
    text: str
    mean_conf: float     # mean Tesseract word confidence (0-100)
    word_count: int
    footer_readings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PageOCR":
        return cls(**d)


def pdf_page_count(pdf_path: Path) -> int:
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        return len(pdf)
    finally:
        pdf.close()


def ocr_pdf(pdf_path: Path, scale: float = 300 / 72) -> list[PageOCR]:
    pdf = pdfium.PdfDocument(str(pdf_path))
    pages: list[PageOCR] = []
    try:
        for i in range(len(pdf)):
            img = pdf[i].render(scale=scale).to_pil().convert("L")
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
            confs = [float(c) for c, t in zip(data["conf"], data["text"]) if t.strip() and float(c) >= 0]
            text = pytesseract.image_to_string(img)
            w, h = img.size
            strip = img.crop((0, int(h * 0.90), w, h))
            footers = [pytesseract.image_to_string(strip, config=cfg).strip() for cfg in ("--psm 6", "--psm 11")]
            pages.append(PageOCR(
                page=i + 1,
                text=text,
                mean_conf=round(sum(confs) / len(confs), 1) if confs else 0.0,
                word_count=len(confs),
                footer_readings=footers,
            ))
    finally:
        pdf.close()
    return pages
