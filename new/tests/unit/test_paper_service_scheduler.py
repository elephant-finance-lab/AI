from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.ops import paper_service_scheduler
from tests.unit.test_paper_service_bundle import _write_repo

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import run_paper_service_scheduler  # noqa: E402

_KST = ZoneInfo("Asia/Seoul")


def test_due_tasks_selects_0830_service_readiness(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)

    due = paper_service_scheduler.due_tasks(
        repo_root=root,
        now=datetime(2026, 6, 5, 8, 30, 10, tzinfo=_KST),
        grace_sec=75,
    )

    assert [item["task"].task_id for item in due] == ["service_readiness"]


def test_selected_paper_auto_start_blocks_without_selected_tickers(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)

    report = paper_service_scheduler.build_scheduler_report(
        repo_root=root,
        bundle_id="BUNDLE-TEST",
        task_ids=["paper_auto_start"],
        execute=False,
        now=datetime(2026, 6, 5, 9, 5, 0, tzinfo=_KST),
    )

    assert report["status"] == "BLOCKED"
    assert "paper_auto_start:selected_tickers_required_for_paper_auto_start" in report["blockers"]
    assert report["results"][0]["execution"]["status"] == "BLOCKED"


def test_selected_paper_auto_start_dry_run_builds_command(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)

    report = paper_service_scheduler.build_scheduler_report(
        repo_root=root,
        bundle_id="BUNDLE-TEST",
        task_ids=["paper_auto_start"],
        selected_tickers="000001",
        execute=False,
        now=datetime(2026, 6, 5, 9, 5, 0, tzinfo=_KST),
    )

    assert report["status"] == "PASS"
    command = report["results"][0]["command"]
    assert "paper_auto_trade.py" in command[1]
    assert "--tickers" in command
    assert "000001" in command
    assert report["results"][0]["execution"]["status"] == "DRY_RUN"


def test_selected_paper_auto_start_uses_schedule_max_selected_tickers(
    tmp_path: Path,
) -> None:
    root = _write_repo(tmp_path)
    schedule_path = root / "new" / "config" / "paper_service_schedule.yaml"
    schedule_path.write_text(
        schedule_path.read_text(encoding="utf-8").replace(
            "  max_selected_tickers: 10",
            "  max_selected_tickers: 1",
        ),
        encoding="utf-8",
    )

    report = paper_service_scheduler.build_scheduler_report(
        repo_root=root,
        bundle_id="BUNDLE-TEST",
        task_ids=["paper_auto_start"],
        selected_tickers="000001,000002",
        execute=False,
        now=datetime(2026, 6, 5, 9, 5, 0, tzinfo=_KST),
    )

    assert report["status"] == "BLOCKED"
    assert "paper_auto_start:selected_ticker_count_exceeds_limit" in report["blockers"]
    assert report["results"][0]["details"]["max_selected_tickers"] == 1


def test_parse_now_attaches_kst_to_naive_timestamp() -> None:
    parsed = run_paper_service_scheduler._parse_now("2026-06-05T08:30:10")  # noqa: SLF001

    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.astimezone(_KST).hour == 8
    assert parsed.astimezone(_KST).minute == 30


def test_execute_uses_injected_runner(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    calls: list[list[str]] = []

    def fake_runner(command_spec, cwd, timeout_sec):
        calls.append(command_spec.command)
        return {"status": "PASS", "returncode": 0}

    report = paper_service_scheduler.build_scheduler_report(
        repo_root=root,
        bundle_id="BUNDLE-TEST",
        task_ids=["service_readiness"],
        execute=True,
        runner=fake_runner,
    )

    assert report["status"] == "PASS"
    assert len(calls) == 1
    assert "service_readiness_status.py" in calls[0][1]
    assert report["results"][0]["execution"]["status"] == "PASS"


def test_trading_day_check_is_internal_action(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)

    report = paper_service_scheduler.build_scheduler_report(
        repo_root=root,
        bundle_id="BUNDLE-TEST",
        task_ids=["trading_day_check"],
        now=datetime(2026, 6, 5, 8, 5, 0, tzinfo=_KST),
    )

    assert report["status"] == "PASS"
    assert report["results"][0]["execution"]["is_kospi_trading_day"] is True
