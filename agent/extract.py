"""Structured extraction from OCR text. Pure functions, no I/O.

Every extracted value carries evidence (page number + the OCR line it came from) so
the routing decision can be audited. Nothing is inferred: if a field is not visibly
present in the OCR text, then it stays None.

Classification is driven by the document title line (the heading under the
"FAX TRANSMISSION / SCANNED DOCUMENT" banner). Body text is NOT used to
pick a category because the bodies contain cross-category words (e.g. a refill
request says "Renewal authorization requested"; a records request lists "imaging
reports"). Identifier prefixes are used only as a corroboration/conflict check.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional

from .ocr import PageOCR
from .sop import Category

# titles
# (regex on the upper-cased title line, category, role)
# role: "primary" = a document in its own right
# "supporting" = an attachment that belongs to another document (only valid if that document is present in the packet)
# "cover" = fax cover sheet (no category of its own)
TITLE_RULES: list[tuple[re.Pattern, Optional[Category], str]] = [
    (re.compile(r"\bCOVER\b"), None, "cover"),
    (re.compile(r"AUTHORI[ZS]ATION\s+(TO|FOR)\s+RELEASE|RELEASE\s+OF\s+INFORMATION"), Category.RECORDS, "supporting"),
    (re.compile(r"\bREFERRAL\b|CONSULT(ATION)?\s+REQUEST"), Category.REFERRAL, "primary"),
    (re.compile(r"\bLAB(ORATORY)?\s+(RESULTS?|REPORT)\b"), Category.LAB, "primary"),
    (re.compile(r"\bIMAGING\b|\bRADIOLOGY\b"), Category.IMAGING, "primary"),
    (re.compile(r"PRIOR\s+AUTHORI[ZS]ATION"), Category.PRIOR_AUTH, "primary"),
    (re.compile(r"\bREFILL\b|PRESCRIPTION\s+RENEWAL"), Category.REFILL, "primary"),
    (re.compile(r"MEDICAL\s+RECORDS|RECORDS\s+REQUEST|REQUEST\s+FOR\s+(MEDICAL\s+)?RECORDS|RECORDS\s+RELEASE"), Category.RECORDS, "primary"),
    (re.compile(r"EXPLANATION\s+OF\s+BENEFITS|\bEOB\b|REMITTANCE"), Category.EOB, "primary"),
]

# Identifier prefix -> category it implies (corroboration only).
ID_PREFIX_CATEGORY = {
    "RF": Category.REFERRAL, "LAB": Category.LAB, "IMG": Category.IMAGING, "PA": Category.PRIOR_AUTH,
    "RX": Category.REFILL, "MR": Category.RECORDS, "EOB": Category.EOB,
}
ID_RE = re.compile(r"\b([A-Z]{1,4})-(\d{3,6}[A-Z]?)\b")
# "Label: ID" lines. Labels that do NOT identify this page's own source document:
NON_SOURCE_LABELS = re.compile(r"related|claim|transmission|original|member|account|callback|phone", re.I)

URGENT_RE = re.compile(r"\b(URGENT|CRITICAL|STAT|EMERGENT|EMERGENCY)\b", re.I)
NEGATED_URGENT_RE = re.compile(r"\b(no|not|non|without)\b[\w\s-]{0,25}\b(urgent|critical|stat|emergent)\b", re.I)
RESEND_RE = re.compile(r"\bRE-?SEND\b|\bRESENT\b|RETRANSMI|\bDUPLICATE\b|ORIGINAL\s+DOCUMENT\s+ID", re.I)
INCOMPLETE_RE = re.compile(
    r"\bincomplete\b|missing\s+(continuation\s+)?pages?|pages?\s+(are\s+)?missing|\billegible\b|\bunreadable\b"
    r"|no\s+readable|cut\s+off|truncated", re.I)
MIXED_HINT_RE = re.compile(r"multiple\s+(documents|enclosures)|different\s+purposes|mixed\s+packet", re.I)

PATIENT_RE = re.compile(r"^\s*(Patient|Member|Patient\s+name)\s*:\s*(.+?)\s*$", re.I)
DOB_RE = re.compile(r"^\s*(DOB|Date\s+of\s+Birth)\s*:\s*([0-9OoIl]{1,2}/[0-9OoIl]{1,2}/[0-9OoIl]{4})", re.I)
LABELED_ID_RE = re.compile(r"^\s*([A-Za-z][A-Za-z .#]*?)\s*[:#]\s*([A-Z]{1,4}-\d{3,6}[A-Z]?)\b")
FOOTER_FAX_RE = re.compile(r"Transmission\s+(FX-\d+)", re.I)
FOOTER_PAGE_RE = re.compile(r"Page\s*([0-9lI|]{1,3})\s*of\s*([0-9lI|]{1,3})", re.I)
COVER_PAGECOUNT_RE = re.compile(r"Number\s+of\s+pages[^:]*:\s*(\d+)", re.I)


@dataclass
class Evidence:
    page: int
    text: str
    kind: str     # title | identifier | patient | dob | flag | footer | ...

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PageExtraction:
    page: int
    title: Optional[str]
    role: str    # cover | primary | supporting | unknown
    category: Optional[Category]
    title_matches: list[str] = field(default_factory=list)   # all categories the title matched
    identifiers: list[dict] = field(default_factory=list)    # {label, id, line}
    source_id: Optional[str] = None
    original_document_id: Optional[str] = None
    related_id: Optional[str] = None
    patient: Optional[str] = None
    dob: Optional[str] = None
    urgent_lines: list[str] = field(default_factory=list)
    negated_urgent_lines: list[str] = field(default_factory=list)
    resend_lines: list[str] = field(default_factory=list)
    incomplete_lines: list[str] = field(default_factory=list)
    mixed_hint_lines: list[str] = field(default_factory=list)
    footer_fax_id: Optional[str] = None
    footer_page_candidates: list[tuple[int, int]] = field(default_factory=list)
    cover_declared_pages: Optional[int] = None
    mean_conf: float = 0.0
    char_count: int = 0
    readable: bool = True

    def to_dict(self) -> dict:
        d = asdict(self)
        d["category"] = self.category.value if self.category else None
        return d


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _norm_digits(s: str) -> str:
    return s.replace("l", "1").replace("I", "1").replace("|", "1").replace("O", "0").replace("o", "0")


def find_title(lines: list[str]) -> Optional[str]:
    """Heading line directly after the 'FAX TRANSMISSION' banner; fallback to first
    upper-case line that matches a title rule."""
    for i, ln in enumerate(lines):
        if "FAX TRANSMISSION" in ln.upper() and i + 1 < len(lines):
            return lines[i + 1]
    for ln in lines[:8]:
        if ln.isupper() and any(rx.search(ln.upper()) for rx, _, _ in TITLE_RULES):
            return ln
    return None


def classify_title(title: Optional[str]) -> tuple[str, Optional[Category], list[str]]:
    if not title:
        return "unknown", None, []
    up = title.upper()
    hits = [(cat, role) for rx, cat, role in TITLE_RULES if rx.search(up)]
    if not hits:
        return "unknown", None, []
    if any(role == "cover" for _, role in hits):
        return "cover", None, ["cover"]
    cats = {cat for cat, _ in hits}
    names = sorted(c.value for c in cats)
    if len(cats) > 1:
        return "ambiguous", None, names
    cat, role = hits[0]
    return role, cat, names


def parse_footer(readings: list[str]) -> tuple[Optional[str], list[tuple[int, int]]]:
    fax_id = None
    cands: list[tuple[int, int]] = []
    for r in readings:
        flat = " ".join(r.split())
        m = FOOTER_FAX_RE.search(flat)
        if m and not fax_id:
            fax_id = m.group(1).upper()
        for pm in FOOTER_PAGE_RE.finditer(flat):
            try:
                cands.append((int(_norm_digits(pm.group(1))), int(_norm_digits(pm.group(2)))))
            except ValueError:
                pass
    return fax_id, cands


def extract_page(p: PageOCR, min_conf: float = 60.0, min_chars: int = 40) -> PageExtraction:
    lines = _lines(p.text)
    title = find_title(lines)
    role, category, matches = classify_title(title)
    ex = PageExtraction(page=p.page, title=title, role=role, category=category, title_matches=matches,
                        mean_conf=p.mean_conf, char_count=len(p.text.strip()))
    ex.readable = p.mean_conf >= min_conf and ex.char_count >= min_chars

    for ln in lines:
        m = LABELED_ID_RE.match(ln)
        if m:
            label, ident = m.group(1).strip(), m.group(2)
            ex.identifiers.append({"label": label, "id": ident, "line": ln})
            low = label.lower()
            if "original" in low:
                ex.original_document_id = ex.original_document_id or ident
            elif "related" in low:
                ex.related_id = ex.related_id or ident
            elif not NON_SOURCE_LABELS.search(label) and ex.source_id is None:
                ex.source_id = ident
        pm = PATIENT_RE.match(ln)
        if pm and ex.patient is None:
            ex.patient = pm.group(2)
        dm = DOB_RE.match(ln)
        if dm and ex.dob is None:
            ex.dob = _norm_digits(dm.group(2))
        if URGENT_RE.search(ln):
            (ex.negated_urgent_lines if NEGATED_URGENT_RE.search(ln) else ex.urgent_lines).append(ln)
        if RESEND_RE.search(ln):
            ex.resend_lines.append(ln)
        if INCOMPLETE_RE.search(ln):
            ex.incomplete_lines.append(ln)
        if MIXED_HINT_RE.search(ln):
            ex.mixed_hint_lines.append(ln)
        cm = COVER_PAGECOUNT_RE.search(ln)
        if cm:
            ex.cover_declared_pages = int(cm.group(1))

    ex.footer_fax_id, ex.footer_page_candidates = parse_footer(p.footer_readings + [lines[-1] if lines else ""])
    return ex


@dataclass
class FaxExtraction:
    fax_id: str
    pages: list[PageExtraction]
    pdf_pages: int
    portal_pages: Optional[int]

    # derived views used by policy + report 
    @property
    def primary_categories(self) -> set[Category]:
        return {p.category for p in self.pages if p.role == "primary" and p.category}

    @property
    def source_document_id(self) -> Optional[str]:
        for p in self.pages:
            if p.role in ("primary",) and p.source_id:
                return p.source_id
        return None

    @property
    def original_document_id(self) -> Optional[str]:
        return next((p.original_document_id for p in self.pages if p.original_document_id), None)

    @property
    def patient(self) -> Optional[str]:
        return next((p.patient for p in self.pages if p.patient and p.role != "cover"), None) or \
            next((p.patient for p in self.pages if p.patient), None)

    @property
    def dob(self) -> Optional[str]:
        return next((p.dob for p in self.pages if p.dob), None)

    @property
    def patients(self) -> set[str]:
        return {norm_name(p.patient) for p in self.pages if p.patient}

    @property
    def all_identifiers(self) -> list[dict]:
        return [dict(i, page=p.page) for p in self.pages for i in p.identifiers]

    def to_dict(self) -> dict:
        return {
            "fax_id": self.fax_id,
            "pdf_pages": self.pdf_pages,
            "portal_pages": self.portal_pages,
            "pages": [p.to_dict() for p in self.pages],
        }


def norm_name(name: Optional[str]) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


def extract_fax(fax_id: str, ocr_pages: list[PageOCR], portal_pages: Optional[int],
                min_conf: float = 60.0, min_chars: int = 40) -> FaxExtraction:
    pages = [extract_page(p, min_conf, min_chars) for p in ocr_pages]
    return FaxExtraction(fax_id=fax_id, pages=pages, pdf_pages=len(ocr_pages), portal_pages=portal_pages)
