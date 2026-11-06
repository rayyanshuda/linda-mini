"""Unit tests: extraction + routing policy on OCR fixtures (no browser, no portal).

Fixtures in tests/fixtures/ocr/ are the agent's own Tesseract output for the PDFs it
downloaded through the portal. Mutated copies exercise exception paths."""
import copy
import json
from pathlib import Path

import pytest

from agent.extract import extract_fax, extract_page, parse_footer
from agent.ocr import PageOCR
from agent.policy import decide, prior_from_extraction
from agent.sop import SOP, Category

from .expected import EXPECTED, PAGES, RECEIVED, route_dict

FIX = Path(__file__).parent / "fixtures" / "ocr"


def load(fid):
    return [PageOCR.from_dict(p) for p in json.loads((FIX / f"{fid}.json").read_text())]


def run_queue(overrides=None):
    """Decide all faxes in received order, like the worker does."""
    overrides = overrides or {}
    priors, out = [], {}
    for fid in sorted(EXPECTED, key=lambda f: (RECEIVED[f], f)):
        pages = overrides.get(fid) or load(fid)
        ex = extract_fax(fid, pages, PAGES.get(fid, 1))
        out[fid] = (ex, decide(ex, priors))
        priors.append(prior_from_extraction(ex, RECEIVED[fid]))
    return out


@pytest.mark.parametrize("fid", sorted(EXPECTED))
def test_expected_decision(fid):
    ex, d = run_queue()[fid]
    action, key, classification, source = EXPECTED[fid]
    assert d.classification == classification
    assert ex.source_document_id == source
    if action == "skipped_duplicate":
        assert d.kind == "skip_duplicate" and d.duplicate_of == "FX-201"
    elif action == "already_routed":
        assert d.route.as_dict() == route_dict("imaging")  # agrees with existing staff route
    else:
        assert d.kind == "route" and d.route.as_dict() == route_dict(key)


def test_critical_flag_quoted_in_note():
    _, d = run_queue()["FX-202"]
    assert d.is_review and "CRITICAL RESULT - CALLING PROVIDER" in d.note
    assert "urgent_or_critical" in d.flags


def test_negated_urgency_is_not_a_flag():
    ex, d = run_queue()["FX-208"]
    assert not any(p.urgent_lines for p in ex.pages)
    assert any("No urgent flag" in u for u in d.uncertainty)
    assert d.category == Category.LAB


def test_authorization_word_traps():
    q = run_queue()
    assert q["FX-205"][1].category == Category.REFILL      # "Renewal authorization requested"
    assert q["FX-206"][1].category == Category.RECORDS     # p2 "AUTHORIZATION TO RELEASE", body mentions imaging


def test_same_sender_same_time_are_distinct():
    q = run_queue()
    assert q["FX-203"][1].category == Category.IMAGING
    assert q["FX-204"][1].category == Category.PRIOR_AUTH


def test_patient_and_dob_extracted():
    ex, _ = run_queue()["FX-201"]
    assert ex.patient == "Elena Hart" and ex.dob == "04/16/1987"


def test_resend_without_original_goes_to_review():
    ex = extract_fax("FX-210", load("FX-210"), 1)
    d = decide(ex, priors=[])
    assert d.is_review and any("resend" in r for r in d.review_reasons)


def test_resend_with_mismatched_dob_goes_to_review():
    pages = load("FX-210")
    pages[0].text = pages[0].text.replace("04/16/1987", "04/16/1988")
    _, d = run_queue({"FX-210": pages})["FX-210"]
    assert d.is_review and d.duplicate_of is None
    assert any("identity not confirmed" in r for r in d.review_reasons)


def test_missing_page_goes_to_review():
    pages = load("FX-201")[:1]  # cover only; portal says 2 pages
    ex = extract_fax("FX-201", pages, 2)
    d = decide(ex, [])
    assert d.is_review and "page_count_mismatch" in d.flags


def test_low_confidence_page_goes_to_review():
    pages = load("FX-203")
    pages[0].mean_conf = 31.0
    d = decide(extract_fax("FX-203", pages, 1), [])
    assert d.is_review and "unreadable" in d.flags


def test_foreign_page_goes_to_review():
    pages = load("FX-203") + load("FX-204")
    pages[1].page = 2
    d = decide(extract_fax("FX-203", pages, 2), [])
    assert d.is_review
    assert "foreign_page" in d.flags or "mixed_packet" in d.flags


def test_attachment_without_parent_goes_to_review():
    p2 = load("FX-206")[1]
    p2.page = 1
    d = decide(extract_fax("FX-206", [p2], 1), [])
    assert d.is_review


def test_footer_ocr_tolerance():
    fax, cands = parse_footer(["Transmission FX-207 Pagelofl", "Transmission FX-207 | Page 1 of 1"])
    assert fax == "FX-207" and (1, 1) in cands


def test_sop_values_exact():
    assert SOP[Category.NEEDS_REVIEW].as_dict() == route_dict("review")
    assert SOP[Category.REFILL].as_dict() == route_dict("refill")


def test_note_within_portal_limit():
    for _, (_, d) in run_queue().items():
        assert len(d.note) <= 1000
