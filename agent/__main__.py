"""CLI: python -m agent run|report"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .config import Config
from .report import write_reports
from .worker import Worker


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agent", description="Linda Mini fax routing agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="process the inbound fax queue (resumes from checkpoints)")
    run.add_argument("--base-url")
    run.add_argument("--state-dir")
    run.add_argument("--headed", action="store_true", help="show the browser windows")
    run.add_argument("--slow-mo", type=int, default=0, help="ms delay between browser actions (demo)")
    run.add_argument("--only", help="comma-separated fax IDs to process (debug)")
    run.add_argument("--fresh", action="store_true",
                     help="delete local checkpoints first (portal state is NOT touched; routed faxes "
                          "will then be reported as already_routed)")
    rep = sub.add_parser("report", help="regenerate results.json/csv from checkpoints")
    rep.add_argument("--state-dir")
    args = ap.parse_args(argv)

    cfg = Config()
    if getattr(args, "state_dir", None):
        cfg.state_dir = Path(args.state_dir)
    if args.cmd == "report":
        out = write_reports(cfg.state_dir)
        print(out["summary"])
        return 0

    if args.base_url:
        cfg.base_url = args.base_url
    cfg.headless = not args.headed
    cfg.slow_mo_ms = args.slow_mo
    if args.fresh and cfg.state_dir.exists():
        shutil.rmtree(cfg.state_dir)
    only = {x.strip() for x in args.only.split(",")} if args.only else None
    try:
        Worker(cfg).run(only)
    finally:
        out = write_reports(cfg.state_dir)
        print(f"results: {cfg.state_dir / 'results.json'}  summary: {out['summary']}", file=sys.stderr)
    return 0 if out["summary"].get("failed", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
