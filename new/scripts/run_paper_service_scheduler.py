#!/usr/bin/env python
"""Run due tasks from the paper service schedule.

Default mode is dry-run/report-only. Use ``--execute`` only in a prepared
paper-safe environment where credentials have already been sourced by the
caller. This script never reads or prints ``.env`` contents.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.ops.paper_service_scheduler import (  # noqa: E402
    build_scheduler_report,
    write_scheduler_report,
)


def _parse_now(raw: str) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_KST)
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", default="")
    parser.add_argument("--task-id", action="append", default=[])
    parser.add_argument("--run-due", action="store_true")
    parser.add_argument("--selected-tickers", default="")
    parser.add_argument("--max-tickers", type=int, default=30)
    parser.add_argument("--cycles", type=int, default=10)
    parser.add_argument("--interval-sec", type=float, default=60.0)
    parser.add_argument("--generated-date", default="")
    parser.add_argument("--now", default="", help="ISO timestamp override for tests/runbooks")
    parser.add_argument("--grace-sec", type=int, default=75)
    parser.add_argument("--timeout-sec", type=int, default=300)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args(argv)

    report = build_scheduler_report(
        repo_root=ROOT,
        bundle_id=str(args.bundle_id or "") or None,
        selected_tickers=str(args.selected_tickers or ""),
        task_ids=[str(item) for item in args.task_id],
        run_due=bool(args.run_due),
        now=_parse_now(str(args.now or "")),
        grace_sec=int(args.grace_sec),
        execute=bool(args.execute),
        max_tickers=int(args.max_tickers),
        cycles=int(args.cycles),
        interval_sec=float(args.interval_sec),
        generated_date=str(args.generated_date or "") or None,
        timeout_sec=int(args.timeout_sec),
    )
    if bool(args.write_report):
        output_dir = Path(str(args.output_dir)) if str(args.output_dir or "").strip() else None
        write_scheduler_report(report, repo_root=ROOT, output_dir=output_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
