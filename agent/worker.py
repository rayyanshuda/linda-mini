"""Orchestrator: per-fax state machine over the whole inbox.

For each fax (ascending received time, so an original is always seen before a resend):
  1. Session A  - new browser, login, UI-navigate to the fax ID, read status, download PDF.
  2. OCR + extraction of every page of the downloaded PDF.
  3. Decision via the SOP policy (pure function).
  4. Session B  - NEW browser launched after the download, fresh login, UI-navigate to the
                  same fax ID, re-check status, fill dropdowns, Review Routing, verify the
                  confirmation modal, checkpoint SUBMIT_INTENT, click OK.
  5. Verify     - regardless of what the submit returned, re-read the fax detail page AND
                  the Routed Documents table; only a UI match should count as success.
Checkpoints are persisted after every step; see ledger.py.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from playwright.sync_api import sync_playwright

from . import ledger as L
from .config import Config
from .extract import FaxExtraction, PageExtraction, extract_fax
from .ocr import PageOCR, ocr_pdf, pdf_page_count
from .policy import Decision, PriorFax, decide, prior_from_extraction
from .portal import DownloadFailed, PortalError, PortalSession
from .sop import Category, Route

UI_KEYS = {"Assigned To": "assigned_to", "Document Type": "document_type", "Document Folder": "folder"}


class CrashInjected(SystemExit):
    pass


class Worker:
    def __init__(self, cfg: Config, log: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.log = log or (lambda m: print(time.strftime("%H:%M:%S"), m, file=sys.stderr, flush=True))
        self.ledger = L.Ledger(cfg.state_dir)

    # start point
    def run(self, only: Optional[set[str]] = None) -> dict:
        run_info = {"started_at": L.now_iso(), "base_url": self.cfg.base_url, "only": sorted(only) if only else None}
        self.ledger.data["runs"].append(run_info)
        self.ledger.save()
        self.ledger.event(None, "run_started", **run_info)
        with sync_playwright() as pw:
            self._inventory(pw)
            for rec in list(self.ledger.records()):
                if only and rec["fax_id"] not in only:
                    continue
                if rec["state"] == L.DONE:
                    self.log(f"{rec['fax_id']}: already final ({rec['final']['action']}), skipping")
                    continue
                self._process_with_retry(pw, rec)
        run_info["finished_at"] = L.now_iso()
        self.ledger.save()
        self.ledger.event(None, "run_finished")
        return run_info

    def _process_with_retry(self, pw, rec: dict, attempts: int = 2) -> None:
        for n in range(1, attempts + 1):
            try:
                self._process(pw, rec)
                return
            except CrashInjected:
                raise
            except Exception as e:  # noqa: BLE001 - record and retry from last checkpoint
                self.ledger.add_error(rec, f"process[{rec['state']}]", f"{type(e).__name__}: {e}", attempt=n)
                self.log(f"{rec['fax_id']}: error in state {rec['state']}: {e}")
                if n < attempts:
                    self.ledger.add_recovery(rec, f"retrying fax from checkpoint {rec['state']} after error")
                    time.sleep(1.0)
        self.log(f"{rec['fax_id']}: left non-final in state {rec['state']} (reported as failed; resumable)")

    # inventory
    def _inventory(self, pw) -> None:
        with PortalSession(pw, self.cfg, "inventory", self.log) as s:
            s.login()
            rows = s.list_all_faxes()
        self.log(f"inventory: {len(rows)} faxes in inbox")
        self.ledger.event(None, "inventory", count=len(rows), fax_ids=[r.fax_id for r in rows])
        for r in rows:
            rec = self.ledger.get(r.fax_id)
            rec.update(received_at=r.received_at, sender=r.sender, portal_pages=r.pages, inbox_status=r.status)
        self.ledger.save()

    # per-fax
    def _process(self, pw, rec: dict) -> None:
        fid = rec["fax_id"]
        if rec["state"] in (L.SUBMIT_INTENT, L.SUBMITTED):
            self.log(f"{fid}: resuming from {rec['state']} -> reconciling with portal UI before any action")
            self.ledger.add_recovery(rec, f"resumed in {rec['state']}; reconciling via UI before any retry")

        if rec["state"] == L.DISCOVERED or (rec["state"] in (L.DOWNLOADED, L.EXTRACTED) and not self._pdf_ok(rec)):
            self._session_a_download(pw, rec)
            self._maybe_crash("after_download", fid)

        if rec["state"] == L.DOWNLOADED:
            self._extract(rec)

        if rec["state"] == L.EXTRACTED:
            self._decide(rec)
            if rec["state"] == L.DONE:
                return

        if rec["state"] in (L.DECIDED, L.SUBMIT_INTENT, L.SUBMITTED):
            self._session_b_route(pw, rec)

    # step 1: session A
    def _session_a_download(self, pw, rec: dict) -> None:
        fid = rec["fax_id"]
        dest = self.cfg.downloads_dir / f"{fid}.pdf"
        with PortalSession(pw, self.cfg, f"A:{fid}", self.log) as s:
            s.login()
            info = None
            for attempt in range(1, self.cfg.max_download_attempts + 1):
                detail = s.open_fax(fid)
                self.ledger.update(rec, status_at_download=detail.status, portal_pages=detail.pages,
                                   existing_assignment=_ui_assignment(detail.assignment) if detail.routed else None)
                try:
                    info = s.download_pdf(fid, dest)
                    break
                except DownloadFailed as e:
                    self.ledger.add_error(rec, "download", str(e), attempt=attempt, session=s.session_id)
                    s.screenshot(f"{fid}-download-failed-{attempt}")
                    if s.ensure_logged_in():
                        self.ledger.add_recovery(rec, "session A expired during download; re-authenticated")
                    if attempt < self.cfg.max_download_attempts:
                        self.ledger.add_recovery(rec, f"download attempt {attempt} failed; re-navigating via UI and retrying")
                        time.sleep(0.5 * 2 ** (attempt - 1))
            if info is None:
                raise DownloadFailed(f"download failed after {self.cfg.max_download_attempts} attempts")
            if info["suggested_filename"] != f"{fid}.pdf":
                raise PortalError(f"downloaded file name {info['suggested_filename']} does not match {fid}")
            pages = pdf_page_count(dest)
            session_a = {"session_ids": list(s.session_ids), "browser_started_at": L.now_iso(s.created_at),
                         "download_completed_at": L.now_iso()}
        self.ledger.transition(rec, L.DOWNLOADED, download=info, pdf_pages=pages, session_a=session_a)
        self.log(f"{fid}: downloaded {info['bytes']} bytes, {pages} page(s), sha256={info['sha256'][:12]} "
                 f"(session A {session_a['session_ids']}, status {rec['status_at_download']})")

    def _pdf_ok(self, rec: dict) -> bool:
        d = rec.get("download")
        if not d or not Path(d["path"]).exists():
            return False
        import hashlib
        return hashlib.sha256(Path(d["path"]).read_bytes()).hexdigest() == d["sha256"]

    # step 2: OCR + extract
    def _extract(self, rec: dict) -> None:
        fid = rec["fax_id"]
        pages = ocr_pdf(Path(rec["download"]["path"]), self.cfg.ocr_scale)
        self.cfg.ocr_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.ocr_dir / f"{fid}.json").write_text(json.dumps([p.to_dict() for p in pages], indent=2))
        ex = extract_fax(fid, pages, rec["portal_pages"], self.cfg.min_ocr_confidence, self.cfg.min_page_chars)
        self.ledger.transition(rec, L.EXTRACTED, ocr={"engine": "tesseract", "pages": len(pages),
                                                      "mean_conf": [p.mean_conf for p in pages],
                                                      "text_path": str(self.cfg.ocr_dir / f"{fid}.json")},
                               extraction=ex.to_dict())
        self.log(f"{fid}: OCR {len(pages)} page(s), conf={[p.mean_conf for p in pages]}, "
                 f"titles={[p.title for p in ex.pages]}")

    # step 3: decide
    def _decide(self, rec: dict) -> None:
        fid = rec["fax_id"]
        ex = _load_extraction(rec)
        priors = self._priors_before(rec)
        d = decide(ex, priors)
        self.ledger.transition(rec, L.DECIDED, decision=d.to_dict())
        self.ledger.event(fid, "decision", kind=d.kind, category=d.category.value, reasons=d.review_reasons,
                          duplicate_of=d.duplicate_of)
        self.log(f"{fid}: decision={d.kind} category={d.category.value} classification={d.classification}"
                 + (f" duplicate_of={d.duplicate_of}" if d.duplicate_of else "")
                 + (f" reasons={d.review_reasons}" if d.review_reasons else ""))

        if rec["status_at_download"] == "Routed":
            # Already routed before touching it: don't modify. just record what the UI shows.
            existing = rec["existing_assignment"] or {}
            agrees = bool(d.route and existing == d.route.as_dict())
            self._finalize(rec, "already_routed", selected=None,
                           verification={"status": "verified_existing_route", "method": "fax detail page (session A)",
                                         "observed": existing, "agrees_with_agent_classification": agrees})
            return
        if d.kind == "skip_duplicate":
            self._finalize(rec, "skipped_duplicate", selected=None,
                           verification={"status": "not_submitted", "method": "no portal action by design",
                                         "duplicate_of": d.duplicate_of})

    def _priors_before(self, rec: dict) -> list[PriorFax]:
        key = (rec["received_at"] or "", rec["fax_id"])
        priors = []
        for r in self.ledger.records():
            if (r["received_at"] or "", r["fax_id"]) >= key or not r.get("extraction"):
                continue
            priors.append(prior_from_extraction(_load_extraction(r), r["received_at"]))
        return priors

    # step 4/5: session B
    def _session_b_route(self, pw, rec: dict) -> None:
        fid = rec["fax_id"]
        dec = rec["decision"]
        intended = dec["route"]
        session_a_ids = set((rec.get("session_a") or {}).get("session_ids", []))
        with PortalSession(pw, self.cfg, f"B:{fid}", self.log) as s:
            s.login()
            b_info = {"session_ids": s.session_ids, "browser_started_at": L.now_iso(s.created_at)}
            rec["sessions_b"].append(b_info)
            self.ledger.save()
            if s.session_id in session_a_ids:
                raise PortalError("session B has the same portal session id as session A; refusing to route")

            while True:
                detail = s.open_fax(fid)
                if detail.routed:
                    self._handle_routed(s, rec, intended)
                    return
                n_submits = len(rec["submit_attempts"])
                if n_submits >= self.cfg.max_submit_attempts:
                    self._finalize(rec, "failed", selected=intended,
                                   verification={"status": "not_routed", "method": "fax detail page",
                                                 "observed": detail.status},
                                   failure=f"not routed after {n_submits} submit attempt(s)")
                    return
                if n_submits:
                    self.ledger.add_recovery(rec, f"UI shows fax still New after {n_submits} submit(s); re-submitting")

                s.open_route_form(fid)
                summary = s.fill_and_review(intended["assigned_to"], intended["document_type"],
                                            intended["folder"], dec["note"])
                if summary != intended:
                    raise PortalError(f"confirmation modal shows {summary}, intended {intended}; not submitting")
                attempt = {"n": n_submits + 1, "intent_at": L.now_iso(), "session_id": s.session_id,
                           "modal_summary": summary, "outcome": None}
                rec["submit_attempts"].append(attempt)
                self.ledger.transition(rec, L.SUBMIT_INTENT)
                self._maybe_crash("before_submit_click", fid)

                outcome = s.confirm_submit()
                self._maybe_crash("after_submit_click", fid)
                attempt["outcome"] = {"kind": outcome.kind, "url": outcome.url, "page_text": outcome.status_text}
                self.ledger.transition(rec, L.SUBMITTED)
                self.ledger.event(fid, "submit_outcome", kind=outcome.kind, url=outcome.url)
                self.log(f"{fid}: submit attempt {attempt['n']} -> {outcome.kind} ({outcome.url})")
                if outcome.kind != "success":
                    s.screenshot(f"{fid}-submit-{attempt['n']}-{outcome.kind}")
                    self.ledger.add_error(rec, "submit", f"submit response: {outcome.kind}",
                                          page_text=outcome.status_text[:200])
                if outcome.kind == "login_redirect":
                    s.login()
                    b_info["session_ids"] = s.session_ids
                    self.ledger.add_recovery(rec, "session expired during submit; re-authenticated in session B "
                                                  "and re-checking fax status before any retry")
                elif outcome.kind in ("gateway_error", "timeout", "unknown"):
                    self.ledger.add_recovery(rec, f"submit outcome uncertain ({outcome.kind}); re-reading persisted "
                                                  "UI state before deciding whether to retry")
                elif outcome.kind == "two_session_blocked":
                    self._finalize(rec, "failed", selected=intended,
                                   verification={"status": "not_routed", "method": "portal rejection page"},
                                   failure="portal rejected route: separate-session rule")
                    return
                # Loop: re-open the fax from the inbox and read the persisted state.

    def _handle_routed(self, s: PortalSession, rec: dict, intended: dict) -> None:
        fid = rec["fax_id"]
        v = self._verify(s, fid, intended)
        we_submitted = bool(rec["submit_attempts"])
        if we_submitted and v["match"]:
            action = "needs_review" if rec["decision"]["category"] == Category.NEEDS_REVIEW.value else "routed"
            last = rec["submit_attempts"][-1].get("outcome")
            if last is None or last["kind"] != "success":
                self.ledger.add_recovery(rec, "route confirmed from persisted UI state (no blind resubmit)")
            self._finalize(rec, action, selected=intended, verification=v)
        elif we_submitted:
            self._finalize(rec, "failed", selected=intended, verification=v,
                           failure="fax is routed but UI values differ from intended; left unchanged")
        else:
            # Routed by someone else between session A and session B: do not modify.
            self._finalize(rec, "already_routed", selected=None, verification=dict(v, status="verified_existing_route"))

    def _verify(self, s: PortalSession, fid: str, intended: dict) -> dict:
        detail = s.open_fax(fid)
        observed = _ui_assignment(detail.assignment) if detail.routed else None
        queue = s.read_work_queue().get(fid)
        queue_vals = {k: queue[k] for k in ("assigned_to", "document_type", "folder")} if queue else None
        match = bool(detail.routed and observed == intended and queue_vals == intended)
        v = {"status": "verified" if match else ("mismatch" if detail.routed else "not_routed"),
             "match": match, "method": "fax detail page + Routed Documents table (fresh UI navigation)",
             "detail_status": detail.status, "observed_detail": observed, "observed_work_queue": queue_vals,
             "verified_at": L.now_iso(), "session_id": s.session_id}
        self.ledger.event(fid, "verify", **v)
        return v

    # helpers
    def _finalize(self, rec: dict, action: str, selected: Optional[dict], verification: dict,
                  failure: Optional[str] = None) -> None:
        final = {"action": action, "selected": selected, "finalized_at": L.now_iso()}
        if selected is not None and rec.get("decision"):
            final["selected"] = dict(selected, notes=rec["decision"]["note"])
        if failure:
            final["failure"] = failure
        self.ledger.transition(rec, L.DONE, final=final, verification=verification)
        self.log(f"{rec['fax_id']}: FINAL {action} verification={verification.get('status')}"
                 + (f" ({failure})" if failure else ""))

    def _maybe_crash(self, point: str, fid: str) -> None:
        if self.cfg.crash_at and self.cfg.crash_at == f"{point}:{fid}":
            self.ledger.event(fid, "crash_injected", point=point)
            self.log(f"!!! injected crash at {point} for {fid}")
            os._exit(97)  # hard kill: no cleanup, no checkpoint after this point


def _ui_assignment(kv: dict) -> dict:
    return {UI_KEYS[k]: v for k, v in kv.items() if k in UI_KEYS}


def _load_extraction(rec: dict) -> FaxExtraction:
    e = rec["extraction"]
    pages = []
    for p in e["pages"]:
        p = dict(p)
        p["category"] = Category(p["category"]) if p["category"] else None
        p["footer_page_candidates"] = [tuple(x) for x in p["footer_page_candidates"]]
        pages.append(PageExtraction(**p))
    return FaxExtraction(fax_id=e["fax_id"], pages=pages, pdf_pages=e["pdf_pages"], portal_pages=e["portal_pages"])
