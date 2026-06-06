#!/usr/bin/env python
"""Build a read-only paper service bundle manifest for BE daily ops."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "new"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from src.ops.paper_service_bundle import (  # noqa: E402
    build_paper_service_bundle_report,
    write_report,
)
from src.utils.safe_cast import safe_bool, safe_int  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--mode", default="selected-paper-service")
    parser.add_argument("--max-tickers", default=30)
    parser.add_argument("--tickers", default="")
    parser.add_argument("--be-base-url", default="")
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--output-dir", default="")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Dangerous opt-in for review only. Default keeps the manifest paper-safe.",
    )
    parser.add_argument(
        "--allow-readiness-partial",
        action="store_true",
        help="For demo planning only: keep manifest non-mutating even if readiness is not PASS.",
    )
    args = parser.parse_args(argv)

    report = build_paper_service_bundle_report(
        repo_root=ROOT,
        bundle_id=str(args.bundle_id),
        mode=str(args.mode),
        max_tickers=safe_int(args.max_tickers, default=30, min_value=1),
        tickers_arg=str(args.tickers),
        be_base_url=str(args.be_base_url or "") or None,
        no_live=not safe_bool(args.live, default=False),
        allow_readiness_partial=safe_bool(args.allow_readiness_partial, default=False),
    )
    if bool(args.write_report):
        output_dir = Path(args.output_dir) if args.output_dir else None
        write_report(report, repo_root=ROOT, output_dir=output_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
