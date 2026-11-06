"""Build results.json / results.csv from the ledger (one row per fax, idempotent)."""
from __future__ import annotations

import csv
import json
import subprocess
from collections import Counter
from pathlib import Path

from . import ledger as L


def _tesseract_version() -> str:
    try:
        return subprocess.run(["tesseract", "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    except Exception:  # noqa: BLE001
        return "unknown"


def fax_result(rec: dict) -> dict:
    ex = rec.get("extraction") or {}
    dec = rec.get("decision") or {}
    final = rec.get("final") or {}
    pages = ex.get("pages", [])
    primary = [p for p in pages if p.get("role") == "primary"]
    patient = next((p["patient"] for p in pages if p.get("patient") and p.get("role") != "cover"), None) or \
        next((p["patient"] for p in pages if p.get("patient")), None)
    if rec["state"] == L.DONE:
        action = final["action"]
    elif rec["state"] == L.DISCOVERED and not rec.get("errors"):
        action = "not_processed"   # e.g. excluded by --only; nothing was attempted
    else:
        action = "failed"          # started but didn't reach a verified final state (resumable)
    return {
        "fax_id": rec["fax_id"],
        "received_at": rec.get("received_at"),
        "sender": rec.get("sender"),
        "portal_pages": rec.get("portal_pages"),
        "pdf_pages": rec.get("pdf_pages"),
        "pdf_sha256": (rec.get("download") or {}).get("sha256"),
        "ocr_mean_confidence": (rec.get("ocr") or {}).get("mean_conf"),
        "classification": dec.get("classification"),
        "page_titles": [{"page": p["page"], "title": p["title"], "role": p["role"], "category": p["category"]}
                        for p in pages],
        "source_document_id": next((p["source_id"] for p in primary if p.get("source_id")), None),
        "original_document_id": next((p["original_document_id"] for p in pages if p.get("original_document_id")), None),
        "identifiers": [dict(i, page=p["page"]) for p in pages for i in p.get("identifiers", [])],
        "patient_name": patient,
        "patient_dob": next((p["dob"] for p in pages if p.get("dob")), None),
        "flags": dec.get("flags", []),
        "review_reasons": dec.get("review_reasons", []),
        "uncertainty": dec.get("uncertainty", []),
        "evidence": dec.get("evidence", []),
        "action": action,
        "duplicate_of": dec.get("duplicate_of"),
        "selected": final.get("selected"),
        "existing_assignment": rec.get("existing_assignment"),
        "verification": rec.get("verification"),
        "session_a_ids": (rec.get("session_a") or {}).get("session_ids"),
        "session_b_ids": [sid for b in rec.get("sessions_b", []) for sid in b.get("session_ids", [])],
        "submit_attempts": [{"n": a["n"], "session_id": a["session_id"],
                             "outcome": (a.get("outcome") or {}).get("kind", "unknown (crash before checkpoint)")}
                            for a in rec.get("submit_attempts", [])],
        "errors": rec.get("errors", []),
        "recovery": rec.get("recovery", []),
        "state": rec["state"],
        "failure": final.get("failure") or (None if rec["state"] == L.DONE or action == "not_processed"
                                             else f"incomplete: stopped in state {rec['state']}"),
    }


def write_reports(state_dir: Path) -> dict:
    led = L.Ledger(state_dir)
    results = [fax_result(r) for r in led.records()]
    summary = Counter(r["action"] for r in results)
    out = {
        "generated_at": L.now_iso(),
        "ocr_engine": _tesseract_version(),
        "runs": led.data.get("runs", []),
        "summary": dict(sorted(summary.items())),
        "faxes": results,
    }
    (Path(state_dir) / "results.json").write_text(json.dumps(out, indent=2, default=str))
    cols = ["fax_id", "received_at", "sender", "classification", "source_document_id", "patient_name",
            "patient_dob", "flags", "action", "duplicate_of", "assigned_to", "document_type", "folder",
            "verification", "session_a_ids", "session_b_ids", "submit_attempts", "errors", "recovery"]
    with open(Path(state_dir) / "results.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in results:
            sel = r["selected"] or {}
            w.writerow({
                **{k: r[k] for k in ("fax_id", "received_at", "sender", "classification", "source_document_id",
                                     "patient_name", "patient_dob", "action", "duplicate_of")},
                "flags": ";".join(r["flags"]),
                "assigned_to": sel.get("assigned_to", ""), "document_type": sel.get("document_type", ""),
                "folder": sel.get("folder", ""),
                "verification": (r["verification"] or {}).get("status", ""),
                "session_a_ids": ";".join(r["session_a_ids"] or []),
                "session_b_ids": ";".join(r["session_b_ids"]),
                "submit_attempts": ";".join(f"{a['n']}:{a['outcome']}" for a in r["submit_attempts"]),
                "errors": len(r["errors"]), "recovery": len(r["recovery"]),
            })
    return out
