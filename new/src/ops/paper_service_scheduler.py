"""Paper service schedule runner.

This module turns ``paper_service_schedule.yaml`` into a deploy-friendly,
paper-safe scheduler plan. It is intentionally conservative:

* live/real order paths are never enabled here;
* user-facing paper-auto start requires explicit selected tickers;
* default execution is dry-run/report-only unless the CLI passes ``--execute``.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.ops.paper_service_bundle import load_schedule
from src.utils.safe_cast import safe_bool, safe_int
from src.utils.ticker_utils import is_valid_ticker, pad_ticker
from src.utils.trading_calendar import is_kospi_trading_day, previous_kospi_trading_day

_KST = ZoneInfo("Asia/Seoul")
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_OUTPUT_DIR = Path("artifacts/reports/paper_service_scheduler")
_SCHEDULE_GROUPS = ("preopen", "market", "postmarket", "mode_b", "closeout")


@dataclass(frozen=True)
class ScheduleTask:
    """Normalized schedule task."""

    group: str
    task_id: str
    required: bool
    demo_blocker: bool
    time_text: str | None = None
    window_text: str | None = None
    repeat_sec: int | None = None


@dataclass(frozen=True)
class CommandSpec:
    """Subprocess command to execute for a task."""

    command: list[str]
    env: dict[str, str] | None = None


def _repo_relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _parse_clock(value: str) -> time:
    parts = str(value or "").strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid clock value: {value}")
    hour = safe_int(parts[0], default=-1, min_value=0, max_value=23)
    minute = safe_int(parts[1], default=-1, min_value=0, max_value=59)
    if hour < 0 or minute < 0:
        raise ValueError(f"invalid clock value: {value}")
    return time(hour=hour, minute=minute)


def _seconds_of_day(value: time) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


def _task_from_raw(group: str, raw: dict[str, Any]) -> ScheduleTask:
    return ScheduleTask(
        group=group,
        task_id=str(raw.get("id") or "").strip(),
        required=safe_bool(raw.get("required"), default=False),
        demo_blocker=safe_bool(raw.get("demo_blocker"), default=bool(raw.get("required"))),
        time_text=str(raw.get("time") or "").strip() or None,
        window_text=str(raw.get("window") or "").strip() or None,
        repeat_sec=safe_int(raw.get("repeat_sec"), default=0, min_value=0) or None,
    )


def load_tasks(
    *,
    repo_root: Path | None = None,
    schedule_path: Path | None = None,
) -> list[ScheduleTask]:
    root = repo_root or _REPO_ROOT
    schedule = load_schedule(repo_root=root, schedule_path=schedule_path)
    tasks: list[ScheduleTask] = []
    for group in _SCHEDULE_GROUPS:
        items = schedule.get(group)
        if not isinstance(items, list):
            continue
        for raw in items:
            if not isinstance(raw, dict):
                continue
            task = _task_from_raw(group, raw)
            if task.task_id:
                tasks.append(task)
    return tasks


def _window_bounds(window_text: str) -> tuple[time, time]:
    start_text, end_text = str(window_text).split("-", maxsplit=1)
    return _parse_clock(start_text), _parse_clock(end_text)


def due_state(
    task: ScheduleTask,
    *,
    now: datetime,
    grace_sec: int = 75,
) -> dict[str, Any]:
    current = now.astimezone(_KST)
    now_sec = _seconds_of_day(current.time())
    if task.time_text:
        target_sec = _seconds_of_day(_parse_clock(task.time_text))
        delta = now_sec - target_sec
        due = 0 <= delta < grace_sec
        return {
            "due": due,
            "kind": "time",
            "scheduled": task.time_text,
            "delta_sec": delta,
            "grace_sec": grace_sec,
        }
    if task.window_text:
        start, end = _window_bounds(task.window_text)
        start_sec = _seconds_of_day(start)
        end_sec = _seconds_of_day(end)
        in_window = start_sec <= now_sec <= end_sec
        repeat = int(task.repeat_sec or grace_sec)
        offset = max(0, now_sec - start_sec)
        due = in_window and (offset % repeat) < grace_sec
        return {
            "due": due,
            "kind": "window",
            "window": task.window_text,
            "repeat_sec": repeat,
            "offset_sec": offset,
            "grace_sec": grace_sec,
            "in_window": in_window,
        }
    return {
        "due": False,
        "kind": "manual",
        "reason": "task_has_no_time_or_window",
    }


def due_tasks(
    *,
    repo_root: Path | None = None,
    schedule_path: Path | None = None,
    now: datetime | None = None,
    grace_sec: int = 75,
) -> list[dict[str, Any]]:
    current = (now or datetime.now(_KST)).astimezone(_KST)
    result: list[dict[str, Any]] = []
    for task in load_tasks(repo_root=repo_root, schedule_path=schedule_path):
        state = due_state(task, now=current, grace_sec=grace_sec)
        if bool(state.get("due")):
            result.append({"task": task, "due_state": state})
    return result


def _normalize_selected_tickers(tickers_arg: str) -> tuple[list[str], list[str]]:
    tickers: list[str] = []
    invalid: list[str] = []
    for item in str(tickers_arg or "").split(","):
        raw = item.strip()
        if not raw:
            continue
        ticker = pad_ticker(raw)
        if not is_valid_ticker(ticker):
            invalid.append(raw)
        else:
            tickers.append(ticker)
    return list(dict.fromkeys(tickers)), invalid


def _script(root: Path, name: str) -> str:
    return str(root / "new" / "scripts" / name)


def _yyyymmdd(value: date) -> str:
    return value.strftime("%Y%m%d")


def _max_selected_tickers(schedule: dict[str, Any]) -> int:
    policy = schedule.get("execution_policy") if isinstance(schedule.get("execution_policy"), dict) else {}
    return safe_int(policy.get("max_selected_tickers"), default=10, min_value=1)


def command_for_task(
    task_id: str,
    *,
    repo_root: Path | None = None,
    bundle_id: str,
    selected_tickers: str = "",
    max_tickers: int = 30,
    cycles: int = 10,
    interval_sec: float = 60.0,
    generated_date: str | None = None,
) -> dict[str, Any]:
    root = repo_root or _REPO_ROOT
    schedule = load_schedule(repo_root=root)
    python = sys.executable
    today = datetime.now(_KST).date()
    target_date = generated_date or _yyyymmdd(today)
    selected, invalid = _normalize_selected_tickers(selected_tickers)
    registry_dir = f"artifacts/lgbm_paper_candidate/{bundle_id}"

    def spec(command: list[str], *, env: dict[str, str] | None = None) -> dict[str, Any]:
        return {
            "status": "PASS",
            "command_spec": CommandSpec(command=command, env=env),
            "blockers": [],
            "warnings": [],
        }

    if task_id in {"infra_health", "bundle_freeze", "recommendation_cache_check"}:
        return spec([
            python,
            _script(root, "deploy_paper_service_bundle.py"),
            "--bundle-id",
            bundle_id,
            "--mode",
            "paper-service-30t",
            "--max-tickers",
            str(max_tickers),
            "--tickers",
            "",
            "--allow-readiness-partial",
            "--write-report",
        ])
    if task_id == "trading_day_check":
        return {"status": "INTERNAL", "action": "trading_day_check"}
    if task_id == "kis_virtual_preflight":
        return spec([
            python,
            _script(root, "paper_trading_smoke.py"),
            "--action",
            "balance",
        ])
    if task_id in {
        "service_readiness",
        "paper_auto_monitor",
        "next_day_readiness",
        "be_fe_status_cache_update",
    }:
        return spec([
            python,
            _script(root, "service_readiness_status.py"),
            "--bundle-id",
            bundle_id,
        ])
    if task_id in {"first_market_data_collection", "recommendation_refresh"}:
        return spec([
            python,
            _script(root, "deploy_paper_service_bundle.py"),
            "--bundle-id",
            bundle_id,
            "--mode",
            "paper-service-30t",
            "--max-tickers",
            str(max_tickers),
            "--tickers",
            "",
            "--allow-readiness-partial",
            "--write-report",
        ])
    if task_id == "paper_auto_start":
        max_selected = _max_selected_tickers(schedule)
        blockers: list[str] = []
        if invalid:
            blockers.append("selected_ticker_invalid_format")
        if not selected:
            blockers.append("selected_tickers_required_for_paper_auto_start")
        if len(selected) > max_selected:
            blockers.append("selected_ticker_count_exceeds_limit")
        if blockers:
            return {
                "status": "BLOCKED",
                "blockers": blockers,
                "warnings": [],
                "selected_tickers": selected,
                "invalid_tickers": invalid,
                "max_selected_tickers": max_selected,
            }
        return spec([
            python,
            _script(root, "paper_auto_trade.py"),
            "--bundle-id",
            bundle_id,
            "--registry-dir",
            registry_dir,
            "--tickers",
            ",".join(selected),
            "--max-tickers",
            str(max_tickers),
            "--cycles",
            str(cycles),
            "--interval-sec",
            str(interval_sec),
            "--confirm-phrase",
            "PAPER_AUTO_OK",
            "--prelive-scope",
            "paper-rehearsal",
            "--track-id",
            "PAPER_SERVICE_SELECTED",
        ])
    if task_id in {"paper_auto_stop_prepare", "paper_auto_stop"}:
        return {
            "status": "MANUAL",
            "blockers": [],
            "warnings": ["paper_auto_stop_is_be_or_operator_session_action"],
        }
    if task_id == "paper_reconciliation":
        return spec([
            python,
            _script(root, "paper_trading_smoke.py"),
            "--action",
            "order-history",
            "--side",
            "all",
            "--execution-filter",
            "all",
        ])
    if task_id == "daily_paper_report":
        return spec([
            python,
            _script(root, "summarize_paper_auto_day.py"),
            "--generated-date",
            target_date,
            "--bundle-id",
            bundle_id,
        ])
    if task_id == "post_close_data_update":
        return spec([
            python,
            _script(root, "post_close_data_update.py"),
            "--bundle-id",
            bundle_id,
            "--max-tickers",
            str(max_tickers),
            "--run-prelive",
        ], env={"ELEPHANT_MODE": "mode_b"})
    if task_id in {
        "dual_source_history_update",
        "feature_backfill",
        "lgbm_retrain",
        "backtest",
        "service_policy_replay",
        "deploy_dry_run",
    }:
        return {
            "status": "COVERED",
            "covered_by": "post_close_data_update",
            "blockers": [],
            "warnings": ["run post_close_data_update once instead of individual substage"],
        }
    if task_id == "daily_closeout":
        return spec([
            python,
            _script(root, "deploy_paper_service_bundle.py"),
            "--bundle-id",
            bundle_id,
            "--mode",
            "paper-service-30t",
            "--max-tickers",
            str(max_tickers),
            "--tickers",
            "",
            "--allow-readiness-partial",
            "--write-report",
        ])
    return {
        "status": "UNKNOWN",
        "blockers": ["unknown_schedule_task"],
        "warnings": [],
    }


def _run_command(
    command_spec: CommandSpec,
    *,
    cwd: Path,
    timeout_sec: int,
) -> dict[str, Any]:
    env = None
    if command_spec.env:
        import os

        env = dict(os.environ)
        env.update(command_spec.env)
    try:
        completed = subprocess.run(
            command_spec.command,
            cwd=str(cwd),
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "status": "BLOCKED",
            "reason": "task_timeout",
            "timeout_sec": timeout_sec,
            "stdout_tail": (e.stdout or "")[-2000:] if isinstance(e.stdout, str) else "",
            "stderr_tail": (e.stderr or "")[-2000:] if isinstance(e.stderr, str) else "",
        }
    return {
        "status": "PASS" if completed.returncode == 0 else "BLOCKED",
        "returncode": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }


Runner = Callable[[CommandSpec, Path, int], dict[str, Any]]


def _execute_internal(task_id: str, *, now: datetime) -> dict[str, Any]:
    if task_id == "trading_day_check":
        current_date = now.astimezone(_KST).date()
        trading_day = is_kospi_trading_day(current_date)
        return {
            "status": "PASS" if trading_day else "BLOCKED",
            "is_kospi_trading_day": trading_day,
            "date": current_date.isoformat(),
            "previous_trading_day": previous_kospi_trading_day(current_date).isoformat(),
        }
    return {"status": "UNKNOWN", "reason": "unknown_internal_action"}


def run_tasks(
    *,
    task_ids: list[str],
    repo_root: Path | None = None,
    bundle_id: str,
    selected_tickers: str = "",
    max_tickers: int = 30,
    cycles: int = 10,
    interval_sec: float = 60.0,
    generated_date: str | None = None,
    execute: bool = False,
    timeout_sec: int = 300,
    now: datetime | None = None,
    runner: Runner | None = None,
) -> list[dict[str, Any]]:
    root = repo_root or _REPO_ROOT
    current = (now or datetime.now(_KST)).astimezone(_KST)
    command_runner = runner or _run_command
    results: list[dict[str, Any]] = []
    for task_id in task_ids:
        command_plan = command_for_task(
            task_id,
            repo_root=root,
            bundle_id=bundle_id,
            selected_tickers=selected_tickers,
            max_tickers=max_tickers,
            cycles=cycles,
            interval_sec=interval_sec,
            generated_date=generated_date,
        )
        command_spec = command_plan.pop("command_spec", None)
        task_result: dict[str, Any] = {
            "task_id": task_id,
            "plan_status": command_plan.get("status"),
            "blockers": command_plan.get("blockers", []),
            "warnings": command_plan.get("warnings", []),
        }
        if command_spec is not None:
            task_result["command"] = command_spec.command
            task_result["env_overrides"] = sorted((command_spec.env or {}).keys())
            if execute:
                task_result["execution"] = command_runner(command_spec, root, timeout_sec)
            else:
                task_result["execution"] = {"status": "DRY_RUN"}
        elif command_plan.get("status") == "INTERNAL":
            task_result["execution"] = _execute_internal(task_id, now=current)
        else:
            task_result["details"] = command_plan
            task_result["execution"] = {"status": command_plan.get("status")}
        results.append(task_result)
    return results


def build_scheduler_report(
    *,
    repo_root: Path | None = None,
    schedule_path: Path | None = None,
    bundle_id: str | None = None,
    selected_tickers: str = "",
    task_ids: list[str] | None = None,
    run_due: bool = False,
    now: datetime | None = None,
    grace_sec: int = 75,
    execute: bool = False,
    max_tickers: int = 30,
    cycles: int = 10,
    interval_sec: float = 60.0,
    generated_date: str | None = None,
    timeout_sec: int = 300,
    runner: Runner | None = None,
) -> dict[str, Any]:
    root = repo_root or _REPO_ROOT
    current = (now or datetime.now(_KST)).astimezone(_KST)
    schedule = load_schedule(repo_root=root, schedule_path=schedule_path)
    selected_bundle = str(
        bundle_id
        or ((schedule.get("bundle") or {}).get("primary") if isinstance(schedule.get("bundle"), dict) else "")
        or "BUNDLE-UNKNOWN"
    )
    all_tasks = load_tasks(repo_root=root, schedule_path=schedule_path)
    due_items = due_tasks(
        repo_root=root,
        schedule_path=schedule_path,
        now=current,
        grace_sec=grace_sec,
    )
    due_ids = [item["task"].task_id for item in due_items]
    selected_ids = list(task_ids or [])
    if run_due:
        selected_ids.extend(task_id for task_id in due_ids if task_id not in selected_ids)

    results: list[dict[str, Any]] = []
    if selected_ids:
        results = run_tasks(
            task_ids=selected_ids,
            repo_root=root,
            bundle_id=selected_bundle,
            selected_tickers=selected_tickers,
            max_tickers=max_tickers,
            cycles=cycles,
            interval_sec=interval_sec,
            generated_date=generated_date,
            execute=execute,
            timeout_sec=timeout_sec,
            now=current,
            runner=runner,
        )

    hard_blockers = [
        f"{item['task_id']}:{blocker}"
        for item in results
        for blocker in item.get("blockers", [])
    ]
    failed = [
        item["task_id"]
        for item in results
        if ((item.get("execution") or {}).get("status") in {"BLOCKED", "FAIL", "FAILED"})
    ]
    status = "PASS" if not hard_blockers and not failed else "BLOCKED"
    return {
        "schema_version": "1.0.0",
        "action": "paper_service_scheduler",
        "status": status,
        "generated_at": current.isoformat(),
        "bundle_id": selected_bundle,
        "execute": bool(execute),
        "schedule": {
            "path": schedule.get("_schedule_path"),
            "hash": schedule.get("_schedule_hash"),
            "timezone": schedule.get("timezone"),
            "mode": schedule.get("mode"),
        },
        "safety": {
            "allow_live_order": False,
            "allow_real_order": False,
            "registry_mutation_allowed": False,
            "selected_tickers_required_for_paper_auto_start": True,
        },
        "task_count": len(all_tasks),
        "tasks": [
            {
                "group": task.group,
                "id": task.task_id,
                "time": task.time_text,
                "window": task.window_text,
                "repeat_sec": task.repeat_sec,
                "required": task.required,
            }
            for task in all_tasks
        ],
        "due_task_ids": due_ids,
        "selected_task_ids": selected_ids,
        "results": results,
        "blockers": hard_blockers + [f"{task_id}:execution_blocked" for task_id in failed],
    }


def write_scheduler_report(
    report: dict[str, Any],
    *,
    repo_root: Path | None = None,
    output_dir: Path | None = None,
) -> Path:
    root = repo_root or _REPO_ROOT
    report_dir = output_dir or root / _DEFAULT_OUTPUT_DIR
    if not report_dir.is_absolute():
        report_dir = root / report_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    path = report_dir / f"paper_service_scheduler_{ts}.json"
    report["report_path"] = str(path)
    report["report_path_relative"] = _repo_relative(path, root)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path
