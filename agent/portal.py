"""Playwright page-object for the Linda Mini portal. UI-only: every action is a visible
click / fill / select on the portal pages. No direct HTTP client, DB access or admin
endpoints are used.

Each PortalSession owns its OWN browser process + context (separate cookie jar) and
performs its own login, so "session A" and "session B" are distinct authenticated
browser sessions. The portal shows "Session xxxxxxxx" in the header; Read it to
prove A != B in the results.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from playwright.sync_api import Browser, Page, Playwright
from playwright.sync_api import TimeoutError as PWTimeout

from .config import Config


class PortalError(Exception):
    pass


class FaxNotFound(PortalError):
    pass


class DownloadFailed(PortalError):
    pass


@dataclass
class FaxRow:
    fax_id: str
    received_at: str
    sender: str
    pages: int
    status: str            # "New" | "Routed"


@dataclass
class FaxDetail:
    fax_id: str
    received_at: str
    sender: str
    pages: int
    status: str
    routed: bool
    assignment: dict = field(default_factory=dict)   # Assigned To / Document Type / Document Folder
    flash: str = ""


@dataclass
class SubmitOutcome:
    kind: str           # success | already | gateway_error | login_redirect | two_session_blocked | invalid | timeout | unknown
    url: str
    status_text: str


class PortalSession:
    def __init__(self, pw: Playwright, cfg: Config, label: str, log: Callable[[str], None]):
        self.cfg, self.label, self.log = cfg, label, log
        self.browser: Browser = pw.chromium.launch(headless=cfg.headless, slow_mo=cfg.slow_mo_ms)
        self.context = self.browser.new_context(accept_downloads=True)
        self.context.set_default_timeout(cfg.nav_timeout_ms)
        self.page: Page = self.context.new_page()
        self.session_ids: list[str] = []
        self.created_at = time.time()
        self.logins = 0

    # lifecycle
    def close(self) -> None:
        try:
            self.context.close()
        finally:
            self.browser.close()

    def __enter__(self) -> "PortalSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def session_id(self) -> Optional[str]:
        return self.session_ids[-1] if self.session_ids else None

    def screenshot(self, name: str) -> Optional[str]:
        try:
            self.cfg.screenshots_dir.mkdir(parents=True, exist_ok=True)
            path = self.cfg.screenshots_dir / f"{name}.png"
            self.page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:  # screenshots are diagnostic only
            return None

    # auth
    def on_login_page(self) -> bool:
        return urlpath(self.page.url).startswith("/login")

    def login(self) -> str:
        """Open the portal and sign in. Returns the session id shown in the header."""
        last_err: Optional[Exception] = None
        for attempt in range(1, self.cfg.max_login_attempts + 1):
            try:
                if not self.on_login_page():
                    self.page.goto(self.cfg.base_url.rstrip("/") + "/")
                if self.on_login_page():
                    self.page.fill("#username", self.cfg.username)
                    self.page.fill("#password", self.cfg.password)
                    self.page.get_by_role("button", name="Sign in").click()
                    self.page.wait_for_load_state("domcontentloaded")
                if self.on_login_page():
                    raise PortalError("still on login page after submitting credentials")
                sid = self.read_session_id()
                self.session_ids.append(sid)
                self.logins += 1
                self.log(f"[{self.label}] logged in, portal session {sid}")
                return sid
            except Exception as e:  # noqa: BLE001 - retry any transient login failure
                last_err = e
                self.log(f"[{self.label}] login attempt {attempt} failed: {e}")
                time.sleep(0.5 * attempt)
        raise PortalError(f"login failed: {last_err}")

    def read_session_id(self) -> str:
        txt = self.page.locator("header .userbar").inner_text(timeout=5000)
        m = re.search(r"Session\s+(\S+)", txt)
        if not m:
            raise PortalError("session id not visible in header")
        return m.group(1)

    def ensure_logged_in(self) -> bool:
        """Re-authenticate if the portal bounced us to /login. Returns True if re-login happened."""
        if self.on_login_page():
            self.log(f"[{self.label}] session expired / logged out -> re-authenticating")
            self.login()
            return True
        return False

    # navigation
    def goto_inbound(self) -> None:
        """Dashboard -> Fax Management -> Inbound Faxes, using visible navigation."""
        self.ensure_logged_in()
        self.page.locator("aside.side").get_by_role("link", name="Fax Management", exact=True).click()
        self.ensure_logged_in()
        self.page.get_by_role("link", name="Go to Inbound Faxes").click()
        self.ensure_logged_in()
        self._expect_h1("Inbound Faxes")

    def _expect_h1(self, text: str) -> None:
        h1 = self.page.locator("main h1").first.inner_text(timeout=self.cfg.nav_timeout_ms).strip()
        if h1 != text:
            raise PortalError(f"expected page heading {text!r}, got {h1!r} at {self.page.url}")

    def _read_inbox_rows(self) -> list[FaxRow]:
        rows: list[FaxRow] = []
        for tr in self.page.locator("table[aria-label='Inbound faxes'] tbody tr").all():
            cells = [c.strip() for c in tr.locator("td").all_inner_texts()]
            if len(cells) < 5:
                continue
            rows.append(FaxRow(fax_id=cells[0], received_at=cells[1], sender=cells[2],
                               pages=int(cells[3]), status=cells[4]))
        return rows

    def list_all_faxes(self) -> list[FaxRow]:
        """Walk every inbox page via the Next button."""
        self.goto_inbound()
        seen: dict[str, FaxRow] = {}
        for _ in range(100):
            for r in self._read_inbox_rows():
                seen[r.fax_id] = r
            nxt = self.page.locator(".pagination").get_by_role("link", name="Next")
            if nxt.count() == 0:
                break
            nxt.click()
            self.ensure_logged_in()
        return list(seen.values())

    def open_fax(self, fax_id: str) -> FaxDetail:
        """Locate the exact fax ID in the inbox (search, then exact-cell match; fall back to
        paging) and open its detail page. Asserts the detail page is for the same ID."""
        self.goto_inbound()
        self.page.fill("#search", fax_id)
        self.page.get_by_role("button", name="Filter").click()
        self.ensure_logged_in()
        if not self._click_exact_row(fax_id):
            self.log(f"[{self.label}] {fax_id} not found via search, paging through inbox")
            self.page.locator("main").get_by_role("link", name="Clear").click()
            found = False
            for _ in range(100):
                if self._click_exact_row(fax_id):
                    found = True
                    break
                nxt = self.page.locator(".pagination").get_by_role("link", name="Next")
                if nxt.count() == 0:
                    break
                nxt.click()
            if not found:
                raise FaxNotFound(fax_id)
        self.ensure_logged_in()
        detail = self.read_detail()
        if detail.fax_id != fax_id:
            raise PortalError(f"opened {detail.fax_id} but intended {fax_id}")
        return detail

    def _click_exact_row(self, fax_id: str) -> bool:
        table = self.page.locator("table[aria-label='Inbound faxes'] tbody")
        link = table.locator("tr td:first-child a").filter(has_text=re.compile(rf"^\s*{re.escape(fax_id)}\s*$"))
        if link.count() != 1:
            return False
        link.click()
        return True

    # detail page
    def read_detail(self) -> FaxDetail:
        self.page.wait_for_load_state("domcontentloaded")
        h1 = self.page.locator("main h1").first.inner_text().strip()
        m = re.match(r"Fax\s+(\S+)$", h1)
        if not m:
            raise PortalError(f"not on a fax detail page (h1={h1!r}, url={self.page.url})")
        info = self._kv(self.page.locator("div.card", has=self.page.locator("h2", has_text="Transmission information")))
        routing_card = self.page.locator("div.card", has=self.page.locator("h2", has_text="Document routing"))
        routed = routing_card.get_by_text("Routing completed").count() > 0
        assignment = self._kv(routing_card) if routed else {}
        flash = ""
        alerts = self.page.locator("main > [role=alert]")
        if alerts.count():
            flash = alerts.first.inner_text().strip()
        return FaxDetail(fax_id=info.get("Fax ID", m.group(1)), received_at=info.get("Received", ""),
                         sender=info.get("Sender", ""), pages=int(info.get("Pages", "0") or 0),
                         status=info.get("Status", ""), routed=routed or info.get("Status") == "Routed",
                         assignment=assignment, flash=flash)

    @staticmethod
    def _kv(scope) -> dict:
        out = {}
        dl = scope.locator("dl.kv")
        if dl.count() == 0:
            return out
        dts = dl.first.locator("dt").all_inner_texts()
        dds = dl.first.locator("dd").all_inner_texts()
        for k, v in zip(dts, dds):
            out[k.strip()] = v.strip()
        return out

    # download
    def download_pdf(self, fax_id: str, dest: Path) -> dict:
        """Click 'Download Fax PDF' on the (already open) detail page. Validates that the
        payload is really a PDF; an HTML error page (e.g. 503) raises DownloadFailed."""
        # Race: a real PDF arrives as a Playwright "download" event; an error (503 HTML page,
        # login redirect) arrives as a normal navigation response. Whichever comes first wins.
        downloads: list = []
        bad: list = []

        def on_response(r):
            if "/download" not in r.url and "/login" not in r.url:
                return
            ctype = (r.headers.get("content-type") or "").lower()
            if r.status >= 400 or "/login" in r.url or (r.status < 300 and "pdf" not in ctype):
                bad.append(f"HTTP {r.status} {ctype or '?'} {urlpath(r.url)}")

        def on_download(d):
            downloads.append(d)

        self.page.on("download", on_download)
        self.page.on("response", on_response)
        try:
            self.page.locator("#download-fax").click()
            deadline = time.time() + self.cfg.download_timeout_ms / 1000
            while not downloads and not bad and time.time() < deadline:
                self.page.wait_for_timeout(100)
        finally:
            self.page.remove_listener("download", on_download)
            self.page.remove_listener("response", on_response)
        if not downloads:
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=5000)
                body = "login page" if self.on_login_page() else self.page.locator("body").inner_text(timeout=3000)
            except Exception:  # noqa: BLE001
                body = ""
            why = bad[-1] if bad else "timed out waiting for download"
            raise DownloadFailed(f"no PDF download ({why}); page shows: {' '.join(body.split())[:160]}")
        download = downloads[0]
        if download.failure():
            raise DownloadFailed(f"browser reported download failure: {download.failure()}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        download.save_as(str(tmp))
        data = tmp.read_bytes()
        if not data.startswith(b"%PDF-"):
            tmp.unlink(missing_ok=True)
            raise DownloadFailed(f"downloaded file is not a PDF (starts with {data[:16]!r})")
        tmp.replace(dest)
        return {"path": str(dest), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "suggested_filename": download.suggested_filename}

    # routing
    def open_route_form(self, fax_id: str) -> None:
        btn = self.page.get_by_role("link", name="Route / Assign Fax")
        if btn.count() == 0:
            raise PortalError("Route / Assign Fax not available (fax may already be routed)")
        btn.click()
        self.ensure_logged_in()
        self._expect_h1(f"Route fax {fax_id}")

    def fill_and_review(self, assigned_to: str, document_type: str, folder: str, notes: str) -> dict:
        """Populate the three dropdowns by visible label + notes, click Review Routing,
        and return the confirmation modal's summary (to be checked before OK)."""
        self.page.select_option("#assignee", label=assigned_to)
        self.page.select_option("#doc_type", label=document_type)
        self.page.select_option("#folder", label=folder)
        self.page.fill("#notes", notes)
        self.page.get_by_role("button", name="Review Routing").click()
        modal = self.page.locator("#confirmation.show")
        modal.wait_for(state="visible", timeout=5000)
        return {
            "assigned_to": self.page.locator("#summary-assignee").inner_text().strip(),
            "document_type": self.page.locator("#summary-type").inner_text().strip(),
            "folder": self.page.locator("#summary-folder").inner_text().strip(),
        }

    def confirm_submit(self) -> SubmitOutcome:
        """Click OK - Submit Routing and classify whatever page comes back. The caller
        must NOT treat this as confirmation; it always re-reads the persisted UI state."""
        try:
            with self.page.expect_navigation(timeout=self.cfg.nav_timeout_ms):
                self.page.locator("#confirm-ok").click()
        except PWTimeout:
            return SubmitOutcome("timeout", self.page.url, "")
        url = self.page.url
        try:
            text = " ".join(self.page.locator("body").inner_text(timeout=5000).split())
        except Exception:  # noqa: BLE001
            text = ""
        if self.on_login_page():
            kind = "login_redirect"
        elif "recorded successfully" in text:
            kind = "success"
        elif "already routed" in text:
            kind = "already"
        elif "503" in text or "Gateway error" in text or "unavailable" in text.lower():
            kind = "gateway_error"
        elif "Separate session required" in text or "separate login session" in text:
            kind = "two_session_blocked"
        elif "Choose valid values" in text or "Invalid form token" in text:
            kind = "invalid"
        else:
            kind = "unknown"
        return SubmitOutcome(kind, url, text[:300])

    # verification
    def read_work_queue(self) -> dict[str, dict]:
        self.ensure_logged_in()
        self.page.locator("aside.side").get_by_role("link", name="Routed Documents").click()
        self.ensure_logged_in()
        self._expect_h1("Routed Documents")
        out = {}
        for tr in self.page.locator("table[aria-label='Routed documents'] tbody tr").all():
            cells = [c.strip() for c in tr.locator("td").all_inner_texts()]
            if len(cells) >= 5:
                out[cells[0]] = {"document_type": cells[1], "assigned_to": cells[2], "folder": cells[3],
                                 "status": cells[4]}
        return out


def urlpath(url: str) -> str:
    m = re.match(r"^[a-z]+://[^/]+(/[^?#]*)?", url)
    return (m.group(1) or "/") if m else url
