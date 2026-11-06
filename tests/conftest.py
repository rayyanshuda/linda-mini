"""E2E fixtures: launch an isolated Linda Mini portal (temp DB, chosen fault mode, free port)
and run the agent as a real subprocess (`python -m agent run`).

`/__admin/state` is read ONLY here, as an independent test oracle (to prove one route per
fax and that routing sessions differ from download sessions). The agent never calls it."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Portal:
    def __init__(self, tmp: Path, fault: str = "none"):
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ, LINDA_DB_PATH=str(tmp / "portal.sqlite3"), LINDA_FAULT_MODE=fault)
        self.log = open(tmp / "portal.log", "w")
        self.proc = subprocess.Popen([PY, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port",
                                      str(self.port)], cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                urllib.request.urlopen(self.url + "/login", timeout=1)
                return
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        raise RuntimeError("portal did not start")

    def oracle(self) -> dict:
        with urllib.request.urlopen(self.url + "/__admin/state?token=local-evaluator-only") as r:
            return json.load(r)

    def stop(self):
        self.proc.terminate()
        self.proc.wait(10)
        self.log.close()


class AgentRun:
    def __init__(self, portal: Portal, state_dir: Path):
        self.portal, self.state_dir = portal, state_dir

    def run(self, *extra: str, env: dict | None = None) -> subprocess.CompletedProcess:
        cmd = [PY, "-m", "agent", "run", "--base-url", self.portal.url, "--state-dir", str(self.state_dir), *extra]
        cp = subprocess.run(cmd, cwd=ROOT, env=dict(os.environ, **(env or {})), capture_output=True, text=True,
                            timeout=600)
        (self.state_dir.parent / f"agent-{int(time.time()*1000)}.log").write_text(cp.stderr)
        return cp

    def results(self) -> dict:
        return {r["fax_id"]: r for r in json.loads((self.state_dir / "results.json").read_text())["faxes"]}

    def state(self) -> dict:
        return json.loads((self.state_dir / "state.json").read_text())["faxes"]


@pytest.fixture
def make_env(tmp_path):
    portals = []

    def _make(fault: str = "none"):
        p = Portal(tmp_path, fault)
        portals.append(p)
        return p, AgentRun(p, tmp_path / "state")

    yield _make
    for p in portals:
        p.stop()


def audit_counts(oracle: dict) -> Counter:
    return Counter((e["event"], e["fax_id"]) for e in oracle["audit_events"])
