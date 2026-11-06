"""Runtime configuration (env-overridable)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    base_url: str = field(default_factory=lambda: os.getenv("LINDA_BASE_URL", "http://127.0.0.1:8000"))
    # Training credentials from the exercise README; override via env for anything else.
    username: str = field(default_factory=lambda: os.getenv("LINDA_USER", "agent.demo"))
    password: str = field(default_factory=lambda: os.getenv("LINDA_PASS", "demo-password-123"))
    state_dir: Path = field(default_factory=lambda: Path(os.getenv("AGENT_STATE_DIR", "artifacts")))
    headless: bool = True
    slow_mo_ms: int = 0
    nav_timeout_ms: int = 15_000
    download_timeout_ms: int = 15_000
    max_download_attempts: int = 3
    max_submit_attempts: int = 2
    max_login_attempts: int = 3
    ocr_scale: float = 300 / 72          # render PDFs at 300 dpi
    min_ocr_confidence: float = 60.0     # mean word confidence below this is an unreliable page
    min_page_chars: int = 40             # less text than this is an unreadable page
    # Fault injection for tests: "<point>:<FAX-ID>", e.g. "after_submit_click:FX-203".
    crash_at: str = field(default_factory=lambda: os.getenv("AGENT_CRASH_AT", ""))

    @property
    def downloads_dir(self) -> Path:
        return self.state_dir / "downloads"

    @property
    def ocr_dir(self) -> Path:
        return self.state_dir / "ocr"

    @property
    def screenshots_dir(self) -> Path:
        return self.state_dir / "screenshots"
