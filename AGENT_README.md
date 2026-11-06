# Linda Mini Fax Routing Agent

A browser agent that works the Linda Mini inbound fax queue end to end. For each fax it:

1. Downloads the scanned PDF in browser **session A**.
2. OCRs every page with **Tesseract**.
3. Classifies the fax with **deterministic, evidence-backed rules** and applies the routing SOP.
4. Routes the fax in a **separately launched and separately logged-in browser session B**.
5. Verifies the saved result in the UI.
6. Checkpoints progress so the run can resume after a crash without repeating actions.

The portal (`app.py`) is untouched. The agent lives in `agent/`. Architecture and design decisions are in [ENGINEERING_NOTES.md](ENGINEERING_NOTES.md).

## Requirements

| Dependency | Why | Install |
|---|---|---|
| Python 3.11+ | runtime (tested on 3.14) | — |
| Tesseract 5.x | OCR engine (local, no network) | `brew install tesseract` / `apt-get install tesseract-ocr` |
| Playwright + Chromium | browser automation | `pip install -r requirements-agent.txt && python -m playwright install chromium` |
| pytesseract, pypdfium2, pillow | OCR binding and PDF rasterization (pip wheels, no poppler needed) | in `requirements-agent.txt` |
| pytest | tests | in `requirements-agent.txt` |

No LLM, cloud OCR or API key is used.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-agent.txt
python -m playwright install chromium
tesseract --version          # must be on PATH
```

## Run

```bash
# terminal 1 – the portal (fresh state)
python reset_sandbox.py
uvicorn app:app --host 127.0.0.1 --port 8000

# terminal 2 – the agent
python -m agent run                      # headless; add --headed --slow-mo 300 to watch
```

The agent writes these files to `artifacts/`:

| File | Contents |
|---|---|
| `results.json` / `results.csv` | One record per fax (see below) |
| `state.json` | Durable checkpoint ledger |
| `events.jsonl` | Append-only audit trail of every step |
| `downloads/FX-*.pdf` | PDFs as downloaded through the portal |
| `ocr/FX-*.json` | Per-page OCR text and confidence |
| `screenshots/` | Captured only when something goes wrong |

Re-running `python -m agent run` resumes from the checkpoints. Faxes that are already final are not touched.

### Options

| Flag / env | Default | Meaning |
|---|---|---|
| `--base-url` / `LINDA_BASE_URL` | `http://127.0.0.1:8000` | portal URL |
| `LINDA_USER` / `LINDA_PASS` | training creds `agent.demo` / `demo-password-123` | login. Credentials are only typed into the login form; they are never logged or written to results. |
| `--state-dir` / `AGENT_STATE_DIR` | `artifacts` | checkpoint and results directory |
| `--headed`, `--slow-mo MS` | off | watch the browsers |
| `--only FX-201,FX-210` | all | debug subset (processed in received order) |
| `--fresh` | off | delete local checkpoints first. Portal state is not reset, so faxes the agent routed before will now report `already_routed`. |
| `python -m agent report` | | rebuild `results.json`/`results.csv` from checkpoints |
| `AGENT_CRASH_AT=<point>:<FAX>` | — | **test only**: hard-kill at `after_download`, `before_submit_click` or `after_submit_click` |

Exit code is `0` when no fax ended `failed`.

## Tests and demo

```bash
pytest -m "not e2e"   # 28 unit tests: extraction + policy + ledger, < 1 s
pytest -m e2e         # 7 end-to-end scenarios with real portal + browsers, ~2 min
scripts/demo.sh       # replays every scenario and writes sample_results/<scenario>/
```

The E2E tests and the demo start their **own** portal per scenario, using a temp DB (`LINDA_DB_PATH`), a free port and the chosen `LINDA_FAULT_MODE`. They never touch your `portal.sqlite3`.

| Scenario | What it proves |
|---|---|
| normal + rerun | All 12 outcomes are exact. Session A ≠ session B for every routed fax. Exactly one route per fax. A second run makes zero downloads and zero routes. |
| `download_503_once` | The HTML 503 page is detected (not saved as a PDF); the agent re-navigates and retries. |
| `session_expire_on_route_once` | The agent re-authenticates inside session B, re-checks status (still New), and resubmits once. |
| `route_commit_then_503_once` | One submit click. The 503 is treated as an unknown outcome; the UI shows Routed with the right values, so the route is verified with no retry. |
| crash `after_submit_click` / `before_submit_click` / `after_download` | Restart reconciles from the UI. No duplicate route; a cached PDF is reused after its hash is checked. |

The tests read `/__admin/state` as an independent oracle: they use it to assert "one route per fax" and "the routing session differs from the download session". The agent itself doesn't call any admin endpoint, the DB, or `data/faxes`.

## Expected outcome on the supplied queue

| Fax | Result | Why |
|---|---|---|
| FX-201 | routed: Referral | cover sheet + "NEW PATIENT REFERRAL REQUEST" RF-3001 |
| FX-202 | needs_review | "CRITICAL RESULT - CALLING PROVIDER" (flag quoted in the note) |
| FX-203 | routed: Imaging Report | "DIAGNOSTIC IMAGING REPORT" IMG-4003 (same sender and time as FX-204) |
| FX-204 | routed: Prior Authorization | "PRIOR AUTHORIZATION REQUEST" PA-4004 |
| FX-205 | routed: Refill Request | title wins over the body word "authorization" |
| FX-206 | routed: Records Request | page 2 is the release authorization *for* MR-4006, so this is not a mixed packet |
| FX-207 | routed: EOB | EOB-4007 |
| FX-208 | routed: Lab Result | "No urgent flag provided" is recognized as a negation |
| FX-209 | needs_review | incomplete, no readable category |
| FX-210 | skipped_duplicate | resend of RF-3001; patient, DOB, category and urgency match the earlier FX-201 |
| FX-211 | already_routed | routed by staff before the run; left untouched |
| FX-212 | needs_review | mixed packet: lab (p2) + radiology (p3) |

The agent's results for these runs are in `sample_results/normal/results.json`. Fault scenarios are in their own folders under `sample_results/`.

## Result record (per fax)

| Group | Fields |
|---|---|
| Fax metadata | `fax_id`, `received_at`, `sender`, `portal_pages`, `pdf_pages`, `pdf_sha256`, `ocr_mean_confidence` |
| Extraction | `classification`, `page_titles[]`, `source_document_id`, `original_document_id`, `identifiers[]`, `patient_name`, `patient_dob` |
| Decision evidence | `flags[]`, `review_reasons[]`, `uncertainty[]`, `evidence[]` (page + OCR line) |
| Outcome | `action` (`routed`, `needs_review`, `skipped_duplicate`, `already_routed`, `failed`, or `not_processed` for faxes excluded by `--only`), `duplicate_of`, `selected` (`assigned_to`, `document_type`, `folder`, `notes`), `existing_assignment` |
| Verification | `verification` (status, method, observed values from the fax detail page **and** the Routed Documents table) |
| Session and recovery trail | `session_a_ids[]`, `session_b_ids[]` (portal session ids read from the page header), `submit_attempts[]` (outcome per click), `errors[]`, `recovery[]` |
