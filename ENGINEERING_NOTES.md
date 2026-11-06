# Engineering notes

## Architecture

```
python -m agent run
        │
        ▼
 worker.py  ── per-fax state machine, ascending received time ─────────────────────────────┐
   │ inventory session: login → Fax Management → Inbound Faxes → page through (Next)       │
   │ for each fax:                                                                         │
   │   ① session A  (portal.py, new browser process + login)                               │
   │        search inbox for exact ID → detail page (assert h1/Fax ID) → read status       │
   │        → click "Download Fax PDF" → validate %PDF + page count → close browser        │
   │   ② ocr.py      pypdfium2 @300dpi → Tesseract (page text + confidence + footer pass)  │
   │   ③ extract.py  title/role/category, IDs, patient, DOB, flags, footer "Page X of Y"   │
   │   ④ policy.py   pure decide(extraction, earlier faxes) → Decision (+ note, evidence)  │
   │   ⑤ session B  (new browser process, launched after ①, fresh login)                   │
   │        find same ID → status check → Route / Assign → 3 dropdowns by label → notes    │
   │        → Review Routing → assert modal summary == intended → checkpoint → OK          │
   │   ⑥ verify     re-open fax via inbox + read Routed Documents; both must match         │
   │ ledger.py: state.json (atomic) + events.jsonl after every transition                  │
   └── report.py: results.json / results.csv ──────────────────────────────────────────────┘
```

| Module | Responsibility | I/O |
|---|---|---|
| `sop.py` | Exact dropdown labels per category | none |
| `portal.py` | Page object; UI clicks only; detects login bounces and re-authenticates | browser |
| `ocr.py` | Rasterize + Tesseract | PDF |
| `extract.py`, `policy.py` | Classification and routing rules | **pure**; unit-tested on fixtures |
| `ledger.py` | Durable checkpoints | disk |
| `worker.py` | Orchestration, retries, reconciliation | all |

### Why deterministic OCR + rules, not an LLM

The scans are clean, typed and 150 dpi; Tesseract reads them at 90–95% mean word confidence. The documents all follow a banner + title layout. Rules keyed on the **title line** are therefore accurate and explainable: every decision lists the OCR lines it relied on.

The traps in the queue are body words that cross categories ("Renewal **authorization**", "**imaging** reports", "**No urgent** flag"). The rules handle them:

- The category comes from the title only.
- Identifier prefixes (RF/LAB/IMG/PA/RX/MR/EOB) are used only as a conflict check against the title.
- Negated urgency is ignored and recorded under `uncertainty`.

Everything the rules can't establish falls through to **Needs Review**, which is the SOP's safe default. An LLM/vision fallback could be added behind the same `Decision` interface. Due to the possibilities of hallucinations and to preserve accuracy, it would only ever downgrade a fax to review, never upgrade one, and it is deliberately left out to keep runs reproducible without API keys.

Small footer text is the weak spot: Tesseract reads "1" as "l". So the footer gets its own cropped pass with two segmentation modes, `l`/`I`/`|` are normalized to `1` inside the "Page X of Y" token, and the candidates are reconciled against the PDF page count. A footer that can't be read only adds an `uncertainty` entry. A footer that clearly disagrees (wrong fax ID, or Y ≠ pages received) forces review.

## Two-session invariant

- Every session is a **new Chromium process with its own context** (separate cookie jar) and its own login. Session A is closed before session B launches, so B's portal session is always created after the download. The portal enforces exactly this rule (`app.py:432-437`).
- The portal header shows the session id. The agent records the ids for A and B per fax and refuses to submit if they are equal.
- Fax identity is checked at every hop:
  - The inbox row is matched on the **exact Fax ID cell**, not on search hits, sender or time. FX-203 and FX-204 share both sender and timestamp.
  - The detail page `h1` and Fax ID field are asserted.
  - The route page `h1` is asserted.
  - The downloaded filename must be `FX-nnn.pdf`.
  - The OCR'd footer "Transmission FX-nnn" must match.

## State and idempotency

The per-fax states are `DISCOVERED → DOWNLOADED → EXTRACTED → DECIDED → SUBMIT_INTENT → SUBMITTED → DONE`, persisted with tmp + fsync + `os.replace` after every transition.

- **Not submitting blind.** `SUBMIT_INTENT` is written *before* clicking OK. On restart, a fax in `SUBMIT_INTENT` or `SUBMITTED` is reconciled against the portal: open the fax and read its status.
  - **Routed with our values:** verified; no second click.
  - **Routed with different values:** `failed` (conflict); the fax is left unchanged.
  - **Still New:** resubmit, bounded by `max_submit_attempts` (2) across runs.
- **Never trust the response.** Whatever comes back from OK (success redirect, 503, login bounce or timeout) is only logged. The final state comes from a fresh navigation to the fax detail page *and* the Routed Documents table, and both must match the intended values.
- **Status before route.** Status is read in session A, then again in session B right before opening the route form. If the fax is routed and we have no submit attempt on record, it is `already_routed` and nothing is changed. The portal also has its own guard against a second route.
- **Report once.** `DONE` records are skipped on rerun, and results are rebuilt from the ledger, so reruns produce the same report and take no portal actions. The E2E test asserts zero new downloads or routes.
- **Downloads are read-only.** Repeating one is harmless. A cached PDF is reused only if its sha256 still matches the checkpoint.

## Failure decisions

| Disruption | Detection | Response |
|---|---|---|
| Download 503 / HTML instead of PDF | A `/download` response with status ≥400 or a non-PDF content type, raced against the Playwright download event; `%PDF-` magic check | Screenshot, re-navigate to the fax via the UI, retry with backoff (3×). Not saved as a PDF. |
| Session expired (any page) | URL bounced to `/login` | Log in again in the same session B browser (still a session created after the download), then navigate back to the fax by ID |
| Session expired on submit | Submit lands on `/login` | Re-authenticate, re-read status (still New), resubmit once |
| Committed but 503 | Outcome `gateway_error` | Re-read the UI; it shows Routed with matching values, so verified with **one** click |
| Crash anywhere | Ledger state on restart | Resume from the checkpoint; reconcile any pending submit first |
| Confirmation modal disagrees with intent | Modal text compared before OK | Abort without submitting; the fax is retried from its checkpoint |
| Portal rejects with the separate-session rule | Outcome `two_session_blocked` | `failed`; no loop |
| Unexpected exception | per-fax try/except | Logged to `errors[]`, retried once from the checkpoint; then left resumable and reported `failed` |

### Classification safety rules (`policy.py`)

| Trigger | Conditions | Action |
|---|---|---|
| Integrity | Unreadable or low-confidence page; PDF pages ≠ portal pages; footer or cover page count says pages are missing; a page from another transmission; "incomplete/missing page" text | Review |
| Urgent / critical / STAT | Non-negated | Review, with the line quoted in the note |
| Mixed | More than one category; a cover sheet that says "multiple documents"; an attachment whose parent document is absent | Review |
| Unclear | No title; an ambiguous title; an unlabeled page; an ID prefix that contradicts the title | Review |
| Duplicate | Shares a source document ID with an **earlier-received** fax, **and** patient name, DOB, category and urgency all match, **and** there are no integrity issues | `skipped_duplicate` |
| Possible resend | Resend marker or shared ID without full confirmation | Review, never a guessed skip |

## Known gaps / trade-offs

- The classifier is tuned to this portal's layout (banner + title line). A real-world fax with a different layout would mostly fall to Needs Review. That is safe but gives a low automation rate. Production would need a trained layout/classification model or an LLM extractor behind the same evidence + review gate, plus a labeled evaluation set.
- Duplicate detection only covers faxes this agent has extracted. Originals routed before the agent existed, with no history, aren't in the registry. A resend that arrives *before* its original would route the resend and review the original. Both are conservative but imperfect.
- One A/B browser pair per fax (about 24 logins and roughly 50 s for 12 faxes) was chosen for a clear audit trail over throughput. Batching downloads in one session A and routes in one session B would also satisfy the portal.
- Session A logins are not explicitly signed out; they expire server-side.
- A fax finalized as `failed` (for example, the submit limit was exhausted or a conflict was found) is terminal for safety. A human resolves it, then deletes its entry from `state.json` to retry.
- Tesseract output can vary by engine version. The version is recorded in `results.json`, and the unit tests run on stored OCR fixtures so they are deterministic.
- Single worker process. Concurrent workers would need a cross-process lock on the ledger, for example SQLite `BEGIN IMMEDIATE` or a lease per fax.
