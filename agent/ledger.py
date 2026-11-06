"""per-fax checkpoint store.

* state.json  - current record per fax; rewritten atomically (tmp file + fsync + os.replace)
                after every state transition, so a crash leaves either the old or the new
                version on disk, not a torn file.
* events.jsonl - append-only audit trail of every step/attempt (fsync'd per line).

State machine (per fax):
    DISCOVERED -> DOWNLOADED -> EXTRACTED -> DECIDED -> SUBMIT_INTENT -> SUBMITTED -> DONE
                                         \\-> DONE (skipped_duplicate / already_routed)
SUBMIT_INTENT is written *before* clicking "OK - Submit Routing". On restart, any fax in
SUBMIT_INTENT/SUBMITTED is reconciled against the portal UI before anything else is done,
so a crash between click and checkpoint can never cause a blind second submit.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

DISCOVERED = "DISCOVERED"
DOWNLOADED = "DOWNLOADED"
EXTRACTED = "EXTRACTED"
DECIDED = "DECIDED"
SUBMIT_INTENT = "SUBMIT_INTENT"
SUBMITTED = "SUBMITTED"
DONE = "DONE"

STATE_ORDER = [DISCOVERED, DOWNLOADED, EXTRACTED, DECIDED, SUBMIT_INTENT, SUBMITTED, DONE]


def now_iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts or time.time())) + f".{int(((ts or time.time()) % 1) * 1000):03d}Z"


def new_record(fax_id: str) -> dict:
    return {
        "fax_id": fax_id,
        "state": DISCOVERED,
        "received_at": None,
        "sender": None,
        "portal_pages": None,
        "inbox_status": None,
        "status_at_download": None,
        "existing_assignment": None,
        "download": None,
        "session_a": None,
        "sessions_b": [],
        "pdf_pages": None,
        "ocr": None,
        "extraction": None,
        "decision": None,
        "submit_attempts": [],
        "verification": None,
        "final": None,
        "errors": [],
        "recovery": [],
        "updated_at": None,
    }


class Ledger:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state.json"
        self.events_path = self.dir / "events.jsonl"
        self.data: dict[str, Any] = {"version": 1, "runs": [], "faxes": {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text())

    # persistence
    def save(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".state.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=2, sort_keys=False, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def event(self, fax_id: str | None, event: str, **data: Any) -> None:
        line = json.dumps({"at": now_iso(), "fax_id": fax_id, "event": event, **data}, default=str)
        with open(self.events_path, "a") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    # records
    def get(self, fax_id: str) -> dict:
        faxes = self.data["faxes"]
        if fax_id not in faxes:
            faxes[fax_id] = new_record(fax_id)
        return faxes[fax_id]

    def has(self, fax_id: str) -> bool:
        return fax_id in self.data["faxes"]

    def records(self) -> Iterator[dict]:
        return iter(sorted(self.data["faxes"].values(), key=lambda r: (r.get("received_at") or "", r["fax_id"])))

    def transition(self, rec: dict, state: str, **fields: Any) -> None:
        """Set fields + new state, persist, and log the transition."""
        rec.update(fields)
        prev = rec["state"]
        rec["state"] = state
        rec["updated_at"] = now_iso()
        self.save()
        self.event(rec["fax_id"], "state", frm=prev, to=state)

    def update(self, rec: dict, **fields: Any) -> None:
        rec.update(fields)
        rec["updated_at"] = now_iso()
        self.save()

    def add_error(self, rec: dict, step: str, error: str, **extra: Any) -> None:
        rec["errors"].append({"at": now_iso(), "step": step, "error": error, **extra})
        self.save()
        self.event(rec["fax_id"], "error", step=step, error=error, **extra)

    def add_recovery(self, rec: dict, what: str) -> None:
        rec["recovery"].append({"at": now_iso(), "what": what})
        self.save()
        self.event(rec["fax_id"], "recovery", what=what)
