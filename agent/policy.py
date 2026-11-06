"""Routing decision: a pure function of (extraction, earlier faxes) to Decision.

Rules (any review trigger wins over a normal SOP route; integrity problems also block
duplicate confirmation, because an incomplete/unreadable fax cannot be proven identical):

  integrity  : unreadable page, PDF page count != portal page count, footer says
               "Page X of Y" with Y != pages received, cover sheet page count mismatch,
               footer transmission ID != this fax ID, explicit "incomplete/missing page" text
  duplicate  : the fax shares a source document ID with an EARLIER-received fax AND
               patient name, DOB, category and urgency all match -> skip (do not route).
               Resend markers / shared IDs without full confirmation -> review.
  urgent     : explicit URGENT/CRITICAL/STAT line (negations like "No urgent flag" ignored)
  mixed      : >1 distinct document category across pages, cover says "multiple documents",
               or an attachment page whose parent document is not in the packet
  unclear    : no page with a recognizable title, an ambiguous title, an unlabeled page,
               or an identifier prefix that contradicts the title
  otherwise  : exactly one category -> SOP row
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from .extract import ID_PREFIX_CATEGORY, FaxExtraction, norm_name
from .sop import SOP, Category, Route

NOTE_LIMIT = 1000  # portal truncates notes at 1000 chars


@dataclass
class PriorFax:
    """What we know about an earlier-received fax (from our own ledger)."""
    fax_id: str
    received_at: str
    source_ids: set[str]
    categories: set[str]
    patient: Optional[str]
    dob: Optional[str]
    urgent: bool


@dataclass
class Decision:
    kind: str              # "route" | "skip_duplicate"
    category: Category     # category used for the SOP lookup (NEEDS_REVIEW if review)
    route: Optional[Route]
    classification: str    # what the document IS (referral / mixed:[..] / unclassified)
    review_reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    uncertainty: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    duplicate_of: Optional[str] = None
    note: str = ""

    @property
    def is_review(self) -> bool:
        return self.kind == "route" and self.category == Category.NEEDS_REVIEW

    def to_dict(self) -> dict:
        d = asdict(self)
        d["category"] = self.category.value
        d["route"] = self.route.as_dict() if self.route else None
        return d


def prior_from_extraction(ex: FaxExtraction, received_at: str) -> PriorFax:
    ids = {p.source_id for p in ex.pages if p.source_id and p.role == "primary"}
    return PriorFax(
        fax_id=ex.fax_id, received_at=received_at, source_ids=ids,
        categories={c.value for c in ex.primary_categories}, patient=ex.patient, dob=ex.dob,
        urgent=any(p.urgent_lines for p in ex.pages),
    )


def decide(ex: FaxExtraction, priors: list[PriorFax]) -> Decision:
    integrity: list[str] = []
    review: list[str] = []
    flags: list[str] = []
    uncertainty: list[str] = []
    evidence: list[dict] = []

    # evidence summary
    for p in ex.pages:
        if p.title:
            evidence.append({"page": p.page, "kind": "title", "text": p.title, "role": p.role,
                             "category": p.category.value if p.category else None})
        for i in p.identifiers:
            evidence.append({"page": p.page, "kind": "identifier", "text": i["line"]})

    # integrity
    unreadable = [p.page for p in ex.pages if not p.readable]
    if unreadable:
        integrity.append(f"unreadable/low-confidence OCR on page(s) {unreadable} "
                         f"(conf={[p.mean_conf for p in ex.pages if not p.readable]})")
        flags.append("unreadable")
    if ex.pdf_pages == 0:
        integrity.append("downloaded PDF has no pages")
    if ex.portal_pages is not None and ex.portal_pages != ex.pdf_pages:
        integrity.append(f"portal lists {ex.portal_pages} page(s) but PDF has {ex.pdf_pages}")
        flags.append("page_count_mismatch")
    for p in ex.pages:
        if p.footer_fax_id and p.footer_fax_id != ex.fax_id:
            integrity.append(f"page {p.page} footer says {p.footer_fax_id}, expected {ex.fax_id}")
            flags.append("foreign_page")
        if p.footer_page_candidates:
            expected = (p.page, ex.pdf_pages)
            if expected not in p.footer_page_candidates:
                totals = {t for _, t in p.footer_page_candidates}
                if len(totals) == 1 and next(iter(totals)) != ex.pdf_pages:
                    integrity.append(f"page {p.page} footer indicates {next(iter(totals))} total pages; "
                                     f"received {ex.pdf_pages}")
                    flags.append("missing_pages")
                else:
                    uncertainty.append(f"page {p.page} footer page-number OCR ambiguous: {p.footer_page_candidates}")
        else:
            uncertainty.append(f"page {p.page} footer page number not read")
        if p.cover_declared_pages is not None and p.cover_declared_pages != ex.pdf_pages:
            integrity.append(f"cover sheet declares {p.cover_declared_pages} pages; received {ex.pdf_pages}")
            flags.append("missing_pages")
        for ln in p.incomplete_lines:
            integrity.append(f'page {p.page} states: "{ln}"')
        if p.incomplete_lines:
            flags.append("incomplete")
            evidence.extend({"page": p.page, "kind": "flag", "text": ln} for ln in p.incomplete_lines)

    # urgent
    urgent_lines = [(p.page, ln) for p in ex.pages for ln in p.urgent_lines]
    if urgent_lines:
        flags.append("urgent_or_critical")
        review.append("explicit urgent/critical flag: " + "; ".join(f'p{pg} "{ln}"' for pg, ln in urgent_lines))
        evidence.extend({"page": pg, "kind": "flag", "text": ln} for pg, ln in urgent_lines)
    for p in ex.pages:
        for ln in p.negated_urgent_lines:
            uncertainty.append(f'p{p.page} negated urgency ignored: "{ln}"')

    # category / mixed / unclear
    primary = ex.primary_categories
    for p in ex.pages:
        if p.role == "ambiguous":
            review.append(f'page {p.page} title "{p.title}" matches several categories {p.title_matches}')
        elif p.role == "unknown":
            review.append(f"page {p.page} has no recognizable document heading (title={p.title!r})")
        elif p.role == "supporting":
            parent_ids = {q.source_id for q in ex.pages if q.role == "primary" and q.source_id}
            if p.category not in primary:
                review.append(f'page {p.page} "{p.title}" is an attachment without its parent document')
            elif p.related_id and parent_ids and p.related_id not in parent_ids:
                review.append(f"page {p.page} attachment references {p.related_id}, not {sorted(parent_ids)}")
        elif p.role == "primary" and p.source_id:
            prefix = p.source_id.split("-")[0]
            implied = ID_PREFIX_CATEGORY.get(prefix)
            if implied and implied != p.category:
                review.append(f"page {p.page} identifier {p.source_id} implies {implied.value} "
                              f"but title says {p.category.value}")
    mixed_hints = [(p.page, ln) for p in ex.pages for ln in p.mixed_hint_lines]
    if len(primary) > 1:
        flags.append("mixed_packet")
        review.append("packet contains multiple document categories: " + ", ".join(
            f"p{p.page}={p.category.value}" for p in ex.pages if p.role == "primary" and p.category))
    elif mixed_hints:
        flags.append("mixed_packet")
        review.append("cover sheet indicates multiple documents: " + "; ".join(f'"{ln}"' for _, ln in mixed_hints))
    if not primary:
        flags.append("unclassified")
        review.append("no document category could be established from the OCR text")

    if len(primary) == 1:
        classification = next(iter(primary)).value
    elif len(primary) > 1:
        classification = "mixed:[" + ",".join(sorted(c.value for c in primary)) + "]"
    else:
        classification = "unclassified"

    # duplicate / resend
    duplicate_of = None
    resend_lines = [(p.page, ln) for p in ex.pages for ln in p.resend_lines]
    my_ids = {p.source_id for p in ex.pages if p.source_id and p.role == "primary"}
    if ex.original_document_id:
        my_ids.add(ex.original_document_id)
    if resend_lines:
        flags.append("possible_resend")
        evidence.extend({"page": pg, "kind": "flag", "text": ln} for pg, ln in resend_lines)
    candidates = [pr for pr in priors if my_ids & pr.source_ids]
    if candidates:
        pr = candidates[0]  # priors are ordered earliest-received first
        checks = {
            "source_id": sorted(my_ids & pr.source_ids),
            "patient": bool(ex.patient and pr.patient and norm_name(ex.patient) == norm_name(pr.patient)),
            "dob": bool(ex.dob and pr.dob and ex.dob == pr.dob),
            "category": bool(primary and {c.value for c in primary} == pr.categories),
            "urgency": bool(urgent_lines) == pr.urgent,
        }
        confirmed = all(checks[k] for k in ("patient", "dob", "category", "urgency"))
        if confirmed and not integrity:
            duplicate_of = pr.fax_id
            if "possible_resend" in flags:
                flags.remove("possible_resend")
            flags.append("confirmed_resend")
            evidence.append({"page": None, "kind": "duplicate_check",
                             "text": f"matches earlier {pr.fax_id} ({pr.received_at}): {checks}"})
        else:
            review.append(f"shares source ID {checks['source_id']} with earlier {pr.fax_id} but identity not "
                          f"confirmed: {checks}" + ("; integrity issues present" if integrity else ""))
    elif resend_lines:
        review.append("marked as resend/retransmission but no earlier original with the same source "
                      f"document ID was found (ids={sorted(my_ids) or 'none'})")

    if duplicate_of:
        note = f"Skipped: confirmed resend of {duplicate_of} (source {', '.join(sorted(my_ids))})."
        return Decision(kind="skip_duplicate", category=next(iter(primary)), route=None,
                        classification=classification, flags=flags, uncertainty=uncertainty,
                        evidence=evidence, duplicate_of=duplicate_of, note=note)

    reasons = integrity + review
    if reasons:
        cat = Category.NEEDS_REVIEW
        detected = classification + (f" {ex.source_document_id}" if ex.source_document_id else "")
        note = ("NEEDS REVIEW - auto-routing withheld. Reasons: " + " | ".join(reasons)
                + f". Detected content: {detected}. Agent made no clinical interpretation.")
    else:
        cat = next(iter(primary))
        title_page = next(p for p in ex.pages if p.role == "primary")
        note = (f'Auto-routed as {SOP[cat].document_type}: title "{title_page.title}" (p{title_page.page})'
                + (f", source ID {ex.source_document_id}" if ex.source_document_id else "")
                + f", {ex.pdf_pages} page(s) OCR-verified.")
    if len(note) > NOTE_LIMIT:
        note = note[:NOTE_LIMIT - 3] + "..."
    return Decision(kind="route", category=cat, route=SOP[cat], classification=classification,
                    review_reasons=reasons, flags=flags, uncertainty=uncertainty, evidence=evidence,
                    note=note)
