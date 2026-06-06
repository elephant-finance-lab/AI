"""AI-side gRPC server wrapper for the shared AI-BE proto contract."""
from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
import uuid
from concurrent import futures
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.integration.grpc.payloads import (
    build_ack_payload,
    build_health_payload,
    build_recommendations_payload,
    build_service_readiness_payload,
)
from src.integration.kafka.producer import KafkaEventProducer
from src.ops.paper_candidate_registry_validator import validate_paper_candidate_registry
from src.ops.service_readiness_status import build_service_status
from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.ticker_utils import pad_ticker

logger = get_logger("grpc_server")
_KST = ZoneInfo("Asia/Seoul")

_GENERATED_DIR = Path(__file__).resolve().parent / "generated"
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_SCRIPTS_DIR = _PROJECT_ROOT / "new" / "scripts"
_PRELIVE_GATE_MODULE_LOCK = threading.Lock()


class _PaperAutoSession:
    """동시 1세션만 허용하는 paper auto trading 세션 관리.

    BE에서 Start 요청 → 비동기 스레드 실행 → Stop 요청 또는 자동 종료.
    """

    def __init__(self, kafka: KafkaEventProducer) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._kafka = kafka
        self.session_id: str = ""
        self.request_id: str = ""
        self.bundle_id: str = ""
        self.running: bool = False
        self.completed_cycles: int = 0
        self.total_cycles: int = 0
        self.started_at: str = ""
        self.last_cycle_at: str = ""
        self.stop_requested: bool = False
        self.terminal_status: str = ""
        self.stop_reason: str = ""
        self.ended_at: str = ""
        self.report_path: str = ""
        self.last_error: str = ""
        self.orders_submitted: int = 0
        self.orders_filled: int = 0
        self.orders_rejected: int = 0
        self.last_cycle_status: str = ""

    def start(
        self,
        *,
        request_id: str,
        bundle_id: str,
        cycles: int,
        interval_sec: int,
        tickers: list[str],
        confirm_phrase: str,
        registry_dir: Path | str | None = None,
        root: Path | None = None,
    ) -> dict[str, Any]:
        """세션 시작. 이미 실행 중이면 거부."""
        with self._lock:
            if self.running:
                return {
                    "accepted": False,
                    "status": "ALREADY_RUNNING",
                    "reason": f"session {self.session_id} 실행 중",
                    "session_id": self.session_id,
                }

            self.session_id = uuid.uuid4().hex[:8]
            self.request_id = request_id
            self.bundle_id = bundle_id
            self.total_cycles = cycles
            self.completed_cycles = 0
            self.started_at = datetime.now(_KST).isoformat()
            self.last_cycle_at = ""
            self.stop_requested = False
            self.terminal_status = "RUNNING"
            self.stop_reason = ""
            self.ended_at = ""
            self.report_path = ""
            self.last_error = ""
            self.orders_submitted = 0
            self.orders_filled = 0
            self.orders_rejected = 0
            self.last_cycle_status = ""
            self.running = True
            self._stop_event.clear()

            self._thread = threading.Thread(
                target=self._run_loop,
                kwargs={
                    "bundle_id": bundle_id,
                    "cycles": cycles,
                    "interval_sec": interval_sec,
                    "tickers": tickers,
                    "confirm_phrase": confirm_phrase,
                    "registry_dir": registry_dir,
                    "root": root,
                },
                daemon=True,
            )

            try:
                self._kafka.emit(
                    "AUTO_TRADING_START_REQUESTED",
                    session_id=self.session_id,
                    request_id=self.request_id,
                    bundle_id=bundle_id,
                    payload={
                        "cycles": cycles,
                        "tickers": tickers,
                        "phase": "ACCEPTED_NOT_RUNNING_YET",
                        "worker_started": False,
                        "preflight_passed": False,
                    },
                )
            except Exception as e:
                self.running = False
                self.terminal_status = "FAILED"
                self.stop_reason = "KAFKA_EMIT_FAILED"
                self.ended_at = datetime.now(_KST).isoformat()
                self.last_error = str(e)
                return {
                    "accepted": False,
                    "status": "KAFKA_EMIT_FAILED",
                    "reason": str(e),
                    "session_id": "",
                }
            self._thread.start()
            return {
                "accepted": True,
                "status": "START_REQUESTED",
                "reason": "",
                "session_id": self.session_id,
            }

    def shutdown(self, *, timeout_sec: float = 5.0) -> None:
        """Stop an active worker thread before process shutdown."""
        thread: threading.Thread | None
        with self._lock:
            thread = self._thread
            if self.running:
                self._stop_event.set()
                self.stop_requested = True
                self.terminal_status = "STOPPING"
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, float(timeout_sec)))

    def stop(self, session_id: str = "") -> dict[str, Any]:
        """세션 중지 요청."""
        with self._lock:
            if not self.running:
                return {
                    "accepted": False,
                    "status": "NOT_RUNNING",
                    "reason": "실행 중인 세션 없음",
                }
            if session_id and session_id != self.session_id:
                return {
                    "accepted": False,
                    "status": "SESSION_MISMATCH",
                    "reason": f"요청 {session_id} != 실행 {self.session_id}",
                }
            self._stop_event.set()
            self.stop_requested = True
            self.terminal_status = "STOPPING"
            try:
                self._kafka.emit(
                    "AUTO_TRADING_STOP_REQUESTED",
                    session_id=self.session_id,
                    request_id=self.request_id,
                    bundle_id=self.bundle_id,
                    payload={"stop_reason": "USER_REQUESTED"},
                )
            except Exception as e:
                self.last_error = str(e)
            return {
                "accepted": True,
                "status": "STOPPING",
                "reason": "",
            }

    def status(self) -> dict[str, Any]:
        """현재 상태 반환. lock으로 백그라운드 스레드와 일관성 보장."""
        with self._lock:
            return {
                "running": self.running,
                "session_id": self.session_id,
                "request_id": self.request_id,
                "status": "RUNNING" if self.running else "IDLE",
                "completed_cycles": self.completed_cycles,
                "total_cycles": self.total_cycles,
                "started_at": self.started_at,
                "bundle_id": self.bundle_id,
                "last_cycle_at": self.last_cycle_at,
                "stop_requested": self.stop_requested,
                "terminal_status": self.terminal_status,
                "stop_reason": self.stop_reason,
                "ended_at": self.ended_at,
                "report_path": self.report_path,
                "last_error": self.last_error,
                "orders_submitted": self.orders_submitted,
                "orders_filled": self.orders_filled,
                "orders_rejected": self.orders_rejected,
                "last_cycle_status": self.last_cycle_status,
                "kafka": _kafka_status(self._kafka),
            }

    def _run_loop(
        self,
        *,
        bundle_id: str,
        cycles: int,
        interval_sec: int,
        tickers: list[str],
        confirm_phrase: str,
        registry_dir: Path | str | None = None,
        root: Path | None = None,
    ) -> None:
        """비동기 스레드에서 PaperAutoTrader.run()을 실행.

        _GrpcPaperAutoTrader(상속)로 run_once()를 override해서 매 cycle마다
        실시간 kafka 이벤트 발행. sleep_fn 주입으로 stop 요청 시 조기 종료.
        """
        try:
            stop_event = self._stop_event

            def interruptible_sleep(sec: float) -> None:
                if stop_event.wait(timeout=sec):
                    raise InterruptedError("stop requested via gRPC")

            trader = _make_grpc_paper_auto_trader(
                kafka=self._kafka,
                session_ref=self,
                required_bundle_id=bundle_id,
                registry_dir=registry_dir,
                sleep_fn=interruptible_sleep,
            )

            report = trader.run(
                tickers=tickers,
                cycles=cycles,
                interval_sec=interval_sec,
                confirm_phrase=confirm_phrase,
            )
            report = report if isinstance(report, dict) else {}
            stop_reason = (
                "USER_REQUESTED" if self._stop_event.is_set() else "COMPLETED"
            )
            terminal_status = (
                "STOPPED"
                if stop_reason == "USER_REQUESTED"
                else str(report.get("status") or "COMPLETED")
            )
            with self._lock:
                self.stop_requested = self.stop_requested or self._stop_event.is_set()
                self.terminal_status = terminal_status
                self.stop_reason = stop_reason
                self.ended_at = datetime.now(_KST).isoformat()
                self.report_path = str(
                    report.get("report_path_relative")
                    or report.get("report_path")
                    or ""
                )

            event_type = (
                "AUTO_TRADING_FAILED"
                if str(self.terminal_status).upper() == "FAIL"
                else "AUTO_TRADING_STOPPED"
            )
            self._kafka.emit(
                event_type,
                session_id=self.session_id,
                request_id=self.request_id,
                bundle_id=bundle_id,
                payload={
                    "completed_cycles": self.completed_cycles,
                    "stop_reason": (
                        "FAIL_CLOSED" if event_type == "AUTO_TRADING_FAILED"
                        else stop_reason
                    ),
                    "terminal_status": self.terminal_status,
                    "report_path": self.report_path,
                },
            )
            self._kafka.flush()
        except InterruptedError:
            with self._lock:
                self.stop_requested = True
                self.terminal_status = "STOPPED"
                self.stop_reason = "USER_REQUESTED"
                self.ended_at = datetime.now(_KST).isoformat()
            self._kafka.emit(
                "AUTO_TRADING_STOPPED",
                session_id=self.session_id,
                request_id=self.request_id,
                bundle_id=bundle_id,
                payload={
                    "completed_cycles": self.completed_cycles,
                    "stop_reason": "USER_REQUESTED",
                    "terminal_status": self.terminal_status,
                    "report_path": self.report_path,
                },
            )
            self._kafka.flush()
        except Exception as e:
            logger.error("[grpc] auto trading 비정상 종료: %s", e)
            with self._lock:
                self.terminal_status = "FAILED"
                self.stop_reason = "ERROR"
                self.ended_at = datetime.now(_KST).isoformat()
                self.last_error = str(e)
            self._kafka.emit(
                "AUTO_TRADING_FAILED",
                session_id=self.session_id,
                request_id=self.request_id,
                bundle_id=bundle_id,
                payload={
                    "error": str(e),
                    "terminal_status": self.terminal_status,
                    "report_path": self.report_path,
                },
            )
            self._kafka.flush()
        finally:
            with self._lock:
                self.running = False
                if not self.ended_at:
                    self.ended_at = datetime.now(_KST).isoformat()


def _dict_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def _event_ticker(value: Any) -> str | None:
    raw = str(value).strip() if value is not None else ""
    return pad_ticker(raw) if raw else None


def _first_not_none(*values: Any) -> Any:
    return next((value for value in values if value is not None), None)


def _decision_event_payload(
    *,
    cycle: int,
    status: Any,
    decision: dict[str, Any],
    order_deltas: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "cycle": cycle,
        "status": status,
        "decision_id": decision.get("decision_id"),
        "approved": decision.get("approved"),
        "reason_code": decision.get("reason_code"),
        "order_count": len(order_deltas),
    }


def _kafka_status(kafka: Any) -> dict[str, Any]:
    try:
        status = kafka.status()
    except AttributeError:
        status = {}
    except Exception as e:
        status = {"last_error": str(e)}
    status = status if isinstance(status, dict) else {}
    return {
        "connected": bool(status.get("connected", False)),
        "topic": str(status.get("topic", "")),
        "required": bool(status.get("required", False)),
        "last_error": str(status.get("last_error", "")),
    }


def _parse_hhmm(value: Any, *, default_hour: int, default_minute: int) -> tuple[int, int]:
    raw = str(value or "").strip()
    if not raw:
        return default_hour, default_minute
    try:
        hour_raw, minute_raw = raw.split(":", 1)
        hour = int(hour_raw)
        minute = int(minute_raw)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid HH:MM value {raw!r}") from e
    raise ValueError(f"invalid HH:MM value {raw!r}")


def _remaining_market_cycles(
    *,
    interval_sec: int,
    pa_cfg: dict[str, Any],
    now: datetime | None = None,
) -> int:
    """Return remaining one-shot cycles until configured KOSPI paper close.

    BE often sends cycles=0 to ask AI for the default. In operation, the safe
    default is "run until today's configured close", not a one-cycle smoke.
    """
    current = (now or datetime.now(_KST)).astimezone(_KST)
    open_hour, open_minute = _parse_hhmm(
        pa_cfg.get("market_open_time", "09:00"),
        default_hour=9,
        default_minute=0,
    )
    close_hour, close_minute = _parse_hhmm(
        pa_cfg.get("market_close_time", "15:30"),
        default_hour=15,
        default_minute=30,
    )
    open_dt = current.replace(
        hour=open_hour, minute=open_minute, second=0, microsecond=0,
    )
    close_dt = current.replace(
        hour=close_hour, minute=close_minute, second=0, microsecond=0,
    )
    start_dt = max(current, open_dt)
    remaining_sec = (close_dt - start_dt).total_seconds()
    if remaining_sec <= 0:
        return 0
    step = max(1, int(interval_sec or 60))
    return max(1, int(remaining_sec // step))


def _market_start_guard(
    *,
    pa_cfg: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(_KST)).astimezone(_KST)
    open_hour, open_minute = _parse_hhmm(
        pa_cfg.get("market_open_time", "09:00"),
        default_hour=9,
        default_minute=0,
    )
    close_hour, close_minute = _parse_hhmm(
        pa_cfg.get("market_close_time", "15:30"),
        default_hour=15,
        default_minute=30,
    )
    open_dt = current.replace(
        hour=open_hour, minute=open_minute, second=0, microsecond=0,
    )
    close_dt = current.replace(
        hour=close_hour, minute=close_minute, second=0, microsecond=0,
    )
    if current < open_dt:
        return {
            "status": "BLOCKED",
            "reason": "market_not_open",
            "market_open_at": open_dt.isoformat(),
        }
    if current >= close_dt:
        return {
            "status": "BLOCKED",
            "reason": "market_closed",
            "market_close_at": close_dt.isoformat(),
        }
    return {"status": "PASS"}


def _candidate_registry_dir(repo_root: Path, bundle_id: str) -> Path | None:
    if not bundle_id:
        return None
    path = repo_root / "artifacts" / "lgbm_paper_candidate" / bundle_id
    registry = path / "registry.json"
    return path if path.exists() and registry.exists() else None


def _load_prelive_gate_module() -> Any:
    module_name = "_elephant_grpc_prelive_gate"
    with _PRELIVE_GATE_MODULE_LOCK:
        if module_name in sys.modules:
            return sys.modules[module_name]
        spec = importlib.util.spec_from_file_location(
            module_name,
            _SCRIPTS_DIR / "prelive_gate.py",
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("prelive_gate module spec could not be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module


def _paper_rehearsal_gate_from_strict(prelive: dict[str, Any]) -> dict[str, Any]:
    stages = prelive.get("stages") if isinstance(prelive, dict) else {}
    stages = stages if isinstance(stages, dict) else {}
    required_stages = [
        "01_code_ssot",
        "02_real_data_readiness",
        "03_80_business_day_data",
        "04_lgbm_real_train",
        "06_paper_balance",
        "07_paper_reconciliation",
        "08_paper_probe_order",
        "09_ops_risk",
    ]
    blockers: list[str] = []
    for stage_name in required_stages:
        stage = stages.get(stage_name)
        if not isinstance(stage, dict) or stage.get("status") != "PASS":
            blockers.append(stage_name)

    strict_blockers = [
        str(blocker)
        for blocker in (prelive.get("blockers") or [])
        if str(blocker).strip()
    ]
    allowed_strict_blockers = {"05_backtest_real_candidate"}
    blockers.extend(sorted(set(strict_blockers) - allowed_strict_blockers))

    stage_01 = stages.get("01_code_ssot")
    if isinstance(stage_01, dict) and bool(stage_01.get("live_enabled")):
        blockers.append("live_enabled_true")

    return {
        "status": "PASS" if not blockers else "BLOCKED",
        "scope": "paper-rehearsal",
        "blockers": blockers,
        "allowed_strict_blockers": sorted(allowed_strict_blockers),
        "strict_prelive_status": prelive.get("status"),
        "strict_prelive_blockers": strict_blockers,
    }


def _paper_start_gate_from_service_readiness(
    *,
    bundle_id: str,
    repo_root: Path,
    candidate_registry_dir: Path,
) -> dict[str, Any]:
    registry_check = validate_paper_candidate_registry(
        repo_root=repo_root,
        bundle_id=bundle_id,
        registry_dir=candidate_registry_dir,
    )
    readiness = build_service_status(bundle_id=bundle_id, root=repo_root)
    contract = readiness.get("be_contract")
    contract = contract if isinstance(contract, dict) else {}
    blockers: list[str] = list(registry_check.get("blockers") or [])

    if readiness.get("status") != "PASS":
        blockers.append("service_readiness_not_pass")
    if readiness.get("deploy_quality") != "PASS":
        blockers.append("deploy_quality_not_pass")
    if readiness.get("broker_evidence") != "PASS":
        blockers.append("broker_evidence_not_pass")
    if readiness.get("read_only") is not True:
        blockers.append("readiness_not_read_only")
    if readiness.get("external_api_called") is not False:
        blockers.append("readiness_external_api_called")
    if not bool(contract.get("safe_to_enable_order_actions")):
        blockers.append("order_actions_not_enabled")
    if bool(readiness.get("live_trading_allowed")):
        blockers.append("live_trading_allowed_true")
    if bool(contract.get("safe_to_enable_live_actions")):
        blockers.append("live_actions_enabled")
    if bool(readiness.get("registry_mutated")):
        blockers.append("registry_mutated_true")

    return {
        "status": "PASS" if not blockers else "BLOCKED",
        "scope": "paper-start",
        "blockers": blockers,
        "registry": registry_check,
        "readiness_status": readiness.get("status"),
        "deploy_quality": readiness.get("deploy_quality"),
        "broker_evidence": readiness.get("broker_evidence"),
        "read_only": readiness.get("read_only"),
        "external_api_called": readiness.get("external_api_called"),
        "live_trading_allowed": readiness.get("live_trading_allowed"),
        "registry_mutated": readiness.get("registry_mutated"),
        "safe_to_enable_order_actions": bool(
            contract.get("safe_to_enable_order_actions")
        ),
        "safe_to_enable_live_actions": bool(
            contract.get("safe_to_enable_live_actions")
        ),
    }


def _default_prelive_end_date(prelive_gate: Any) -> str:
    gate_cfg = prelive_gate._final_dataset_gate_cfg()
    expected = prelive_gate._parse_dataset_date(gate_cfg.get("expected_end_date"))
    if expected is not None:
        return expected.strftime("%Y%m%d")
    return prelive_gate._previous_business_day().strftime("%Y%m%d")


def _default_prelive_business_days(prelive_gate: Any) -> int:
    gate_cfg = prelive_gate._final_dataset_gate_cfg()
    return int(gate_cfg.get("rehearsal_business_days") or 80)


def _normalize_start_tickers(raw_tickers: Any) -> tuple[list[str], list[str]]:
    tickers: list[str] = []
    invalid: list[str] = []
    for raw in list(raw_tickers or []):
        value = str(raw).strip()
        if not value:
            continue
        if not value.isdigit():
            invalid.append(value)
            continue
        tickers.append(pad_ticker(value))
    return tickers, invalid


def _load_active_start_tickers(max_tickers: int) -> list[str]:
    """Load the paper-auto default universe from the trade-universe SSOT."""
    cfg = config_load("universe_config.yaml") or {}
    tickers: list[str] = []
    for sector in (cfg.get("sectors") or {}).values():
        if not isinstance(sector, dict):
            continue
        for stock in sector.get("stocks", []) or []:
            if not isinstance(stock, dict):
                continue
            if stock.get("status") == "active" and stock.get("ticker"):
                tickers.append(pad_ticker(str(stock["ticker"])))
    return tickers[:max_tickers] if max_tickers > 0 else tickers


def _validate_paper_auto_start_args(
    *,
    bundle_id: str,
    cycles: int,
    interval_sec: int,
    tickers: list[str],
    invalid_tickers: list[str],
    confirm_phrase: str,
    required_confirm_phrase: str,
    max_tickers: int,
) -> dict[str, Any]:
    if not bundle_id:
        return {"status": "INVALID_ARGUMENT", "reason": "bundle_id_required"}
    if required_confirm_phrase and confirm_phrase != required_confirm_phrase:
        return {
            "status": "INVALID_ARGUMENT",
            "reason": "confirm_phrase_missing_or_mismatch",
            "required_phrase": required_confirm_phrase,
        }
    if cycles <= 0:
        return {"status": "INVALID_ARGUMENT", "reason": "cycles_must_be_positive"}
    if interval_sec < 60:
        return {
            "status": "INVALID_ARGUMENT",
            "reason": "interval_sec_must_be_at_least_60",
        }
    if invalid_tickers:
        return {
            "status": "INVALID_ARGUMENT",
            "reason": "ticker_must_be_numeric",
            "invalid_tickers": invalid_tickers,
        }
    if not tickers:
        return {"status": "INVALID_ARGUMENT", "reason": "tickers_required"}
    if max_tickers > 0 and len(tickers) > max_tickers:
        return {
            "status": "INVALID_ARGUMENT",
            "reason": "ticker_count_exceeds_max",
            "max_tickers": max_tickers,
            "ticker_count": len(tickers),
        }
    return {"status": "PASS", "tickers": tickers}


def _event_order_no(row: dict[str, Any]) -> Any:
    response = row.get("broker_response")
    response = response if isinstance(response, dict) else {}
    return (
        row.get("broker_order_id")
        or row.get("order_id")
        or row.get("order_no")
        or row.get("odno")
        or row.get("ODNO")
        or response.get("broker_order_id")
        or response.get("order_id")
        or response.get("order_no")
        or response.get("odno")
        or response.get("ODNO")
    )


def _paper_cycle_events(
    result: dict[str, Any],
    *,
    cycle: int,
) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    hot_result = result.get("hot_result")
    hot_result = hot_result if isinstance(hot_result, dict) else {}
    decision = hot_result.get("final_decision")
    decision = decision if isinstance(decision, dict) else {}
    order_deltas = _dict_rows(decision.get("order_deltas"))
    decision_payload = _decision_event_payload(
        cycle=cycle,
        status=result.get("status", ""),
        decision=decision,
        order_deltas=order_deltas,
    )

    execution = result.get("execution")
    execution = execution if isinstance(execution, dict) else {}
    report = execution.get("execution_report")
    report = report if isinstance(report, dict) else {}
    if not report:
        return decision_payload, []

    common = {
        "order_plan_id": report.get("order_plan_id"),
        "execution_mode": "paper",
        "kis_mode": "virtual",
        "paper_only": True,
    }
    events: list[tuple[str, dict[str, Any]]] = []
    submitted_rows = _dict_rows(report.get("fills"))
    for row in submitted_rows:
        response = row.get("broker_response")
        response = response if isinstance(response, dict) else {}
        order_no = _event_order_no(row)
        events.append((
            "PAPER_ORDER_SUBMITTED",
            {
                **common,
                "ticker": _event_ticker(row.get("ticker")),
                "side": row.get("side"),
                "quantity": row.get("qty"),
                "price": _first_not_none(row.get("price"), response.get("price")),
                "order_type": _first_not_none(
                    row.get("order_type"),
                    response.get("order_type"),
                ),
                "kis_order_no": order_no,
                "broker_order_id": order_no,
            },
        ))

    for row in _dict_rows(report.get("rejections")):
        response = row.get("broker_response")
        response = response if isinstance(response, dict) else {}
        failed_payload = {
            **common,
            "ticker": _event_ticker(row.get("ticker")),
            "side": row.get("side"),
            "quantity": row.get("qty"),
            "price": _first_not_none(row.get("price"), response.get("price")),
            "error_code": (
                row.get("error_code")
                or response.get("msg_cd")
                or response.get("rt_cd")
            ),
            "reason": row.get("reason"),
        }
        events.append(("PAPER_ORDER_REJECTED", dict(failed_payload)))
        events.append(("PAPER_ORDER_FAILED", failed_payload))

    confirmed_rows: list[dict[str, Any]] = []
    confirmed_order_ids: set[str] = set()

    def add_confirmed_row(row: dict[str, Any]) -> None:
        order_id = str(_event_order_no(row) or "").strip()
        if order_id and order_id in confirmed_order_ids:
            return
        if order_id:
            confirmed_order_ids.add(order_id)
        # Order id가 없으면 서로 다른 체결일 수 있으므로 임의로 합치지 않는다.
        confirmed_rows.append(row)

    history = result.get("order_history_verification")
    history = history if isinstance(history, dict) else {}
    for query in _dict_rows(history.get("queries")):
        for row in _dict_rows(query.get("matched_orders")):
            if str(row.get("status", "")).lower() in {"filled", "partial_filled"}:
                add_confirmed_row(row)
    if (
        str(history.get("status") or "").upper() == "FAIL"
        and submitted_rows
    ):
        events.append((
            "PAPER_ORDER_VERIFICATION_FAILED",
            {
                **common,
                "reason": history.get("reason") or "order_history_not_matched",
                "submitted_count": len(submitted_rows),
            },
        ))
    if not confirmed_rows:
        for row in submitted_rows:
            if str(row.get("broker_status", "")).lower() in {"filled", "partial_filled"}:
                add_confirmed_row(row)

    for row in confirmed_rows:
        order_no = _event_order_no(row)
        status = str(row.get("status", "") or row.get("broker_status", "")).lower()
        event_type = (
            "PAPER_ORDER_PARTIALLY_FILLED"
            if status == "partial_filled"
            else "PAPER_ORDER_FILLED"
        )
        events.append((
            event_type,
            {
                **common,
                "ticker": _event_ticker(row.get("ticker")),
                "side": row.get("side"),
                "filled_quantity": row.get("filled_qty", row.get("qty")),
                "filled_price": row.get("avg_fill_price"),
                "kis_order_no": order_no,
                "broker_order_id": order_no,
            },
        ))
    return decision_payload, events


def _make_grpc_paper_auto_trader(
    *,
    kafka: KafkaEventProducer,
    session_ref: _PaperAutoSession,
    **kwargs: Any,
) -> Any:
    """PaperAutoTrader를 상속해 run_once()마다 실시간 kafka 이벤트 발행.

    run() 내부에서 매 cycle마다 run_once()를 호출하므로,
    override한 run_once()에서 super() 호출 후 kafka emit → 실시간.
    PaperAutoTrader 원본 코드 수정 없음.

    lazy import로 circular import 회피.
    """
    from src.execution.paper_auto_trading import PaperAutoTrader
    from src.agents.hot.quant import QuantAgent
    from src.data.dual_source_runner import load_latest_scores
    from src.models.registry import ModelRegistry
    from src.orchestration.hot_runner import HotRunner

    class _GrpcPaperAutoTrader(PaperAutoTrader):

        def __init__(self, *, _kafka: KafkaEventProducer, _session: _PaperAutoSession, **kw: Any) -> None:
            registry_dir = kw.pop("registry_dir", None)
            if registry_dir and "hot_runner" not in kw:
                registry = ModelRegistry(artifacts_dir=Path(registry_dir))
                quant = QuantAgent(
                    registry=registry,
                    dual_source_loader=load_latest_scores,
                )
                kw["hot_runner"] = HotRunner(quant=quant)
            super().__init__(**kw)
            self._grpc_kafka = _kafka
            self._grpc_session = _session
            self._grpc_pre_submit_decision_cycles: set[int] = set()

        def _on_run_preflight_passed(self, report: dict[str, Any]) -> None:
            self._grpc_kafka.emit(
                "AUTO_TRADING_STARTED",
                session_id=self._grpc_session.session_id,
                request_id=self._grpc_session.request_id,
                bundle_id=self._grpc_session.bundle_id,
                payload={
                    "phase": "RUNNING",
                    "worker_started": True,
                    "preflight_passed": True,
                    "cycles": report.get("cycles"),
                    "tickers": report.get("tickers", []),
                },
            )
            if self._grpc_kafka.status().get("required"):
                self._grpc_kafka.flush()

        def _on_before_broker_submit(
            self,
            *,
            cycle_index: int,
            final_decision: dict[str, Any],
            hot_result: dict[str, Any],
            order_guard: dict[str, Any],
        ) -> None:
            del order_guard
            order_deltas = [
                dict(od)
                for od in list(final_decision.get("order_deltas", []))
                if isinstance(od, dict)
            ]
            quant_output = hot_result.get("quant_output")
            quant_output = quant_output if isinstance(quant_output, dict) else {}
            payload = _decision_event_payload(
                cycle=cycle_index + 1,
                status="PRE_SUBMIT",
                decision=final_decision,
                order_deltas=order_deltas,
            )
            self._grpc_kafka.emit(
                "DECISION_COMPLETED",
                session_id=self._grpc_session.session_id,
                request_id=self._grpc_session.request_id,
                bundle_id=self._grpc_session.bundle_id,
                payload={
                    **payload,
                    "phase": "PRE_BROKER_SUBMIT",
                    "quant_mode": quant_output.get("mode"),
                },
            )
            if self._grpc_kafka.status().get("required"):
                self._grpc_kafka.flush()
            self._grpc_pre_submit_decision_cycles.add(cycle_index + 1)

        def run_once(self, *, tickers: list[str], cycle_index: int = 0, risk_warnings: Any = None) -> dict[str, Any]:
            result = super().run_once(
                tickers=tickers,
                cycle_index=cycle_index,
                risk_warnings=risk_warnings,
            )

            decision_payload, order_events = _paper_cycle_events(
                result,
                cycle=cycle_index + 1,
            )
            submitted_count = sum(
                1 for event_type, _payload in order_events
                if event_type == "PAPER_ORDER_SUBMITTED"
            )
            filled_count = sum(
                1 for event_type, _payload in order_events
                if event_type in {"PAPER_ORDER_FILLED", "PAPER_ORDER_PARTIALLY_FILLED"}
            )
            rejected_count = sum(
                1 for event_type, _payload in order_events
                if event_type == "PAPER_ORDER_REJECTED"
            )
            with self._grpc_session._lock:
                self._grpc_session.completed_cycles = cycle_index + 1
                self._grpc_session.last_cycle_at = datetime.now(_KST).isoformat()
                self._grpc_session.last_cycle_status = str(result.get("status") or "")
                self._grpc_session.orders_submitted += submitted_count
                self._grpc_session.orders_filled += filled_count
                self._grpc_session.orders_rejected += rejected_count

            sid = self._grpc_session.session_id
            bid = self._grpc_session.bundle_id
            rid = self._grpc_session.request_id

            if cycle_index + 1 not in self._grpc_pre_submit_decision_cycles:
                self._grpc_kafka.emit(
                    "DECISION_COMPLETED",
                    session_id=sid,
                    request_id=rid,
                    bundle_id=bid,
                    payload=decision_payload,
                )

            for event_type, payload in order_events:
                self._grpc_kafka.emit(
                    event_type,
                    session_id=sid,
                    request_id=rid,
                    bundle_id=bid,
                    payload=payload,
                )

            return result

    return _GrpcPaperAutoTrader(_kafka=kafka, _session=session_ref, **kwargs)


def _load_grpc_modules() -> tuple[Any, Any, Any]:
    """Load grpc + generated modules with a clear setup error."""
    try:
        import grpc  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "grpcio is not installed. Install requirements.txt before running the AI gRPC server."
        ) from e

    if str(_GENERATED_DIR) not in sys.path:
        sys.path.insert(0, str(_GENERATED_DIR))
    try:
        pb2 = importlib.import_module("elephant_ai_bridge_pb2")
        pb2_grpc = importlib.import_module("elephant_ai_bridge_pb2_grpc")
    except ImportError as e:
        raise RuntimeError(
            "Generated gRPC stubs are missing. Run "
            "`PYTHONPATH=new python new/scripts/generate_ai_grpc_stubs.py` first."
        ) from e
    return grpc, pb2, pb2_grpc


def _make_servicer(
    grpc: Any,
    pb2: Any,
    pb2_grpc: Any,
    *,
    bundle_id: str,
    root: Path | None,
    session: _PaperAutoSession,
) -> Any:
    """Create a generated servicer instance bound to read-only payload builders."""

    class AiBeBridgeServicer(pb2_grpc.AiBeBridgeServiceServicer):  # type: ignore[misc, name-defined]
        def HealthCheck(self, request: Any, context: Any) -> Any:  # noqa: N802
            payload = build_health_payload(
                request_id=str(getattr(request, "request_id", "")),
                bundle_id=str(getattr(request, "bundle_id", "") or bundle_id),
                root=root,
            )
            return pb2.HealthCheckResponse(**payload)

        def GetServiceReadiness(self, request: Any, context: Any) -> Any:  # noqa: N802
            requested_bundle_id = str(getattr(request, "bundle_id", "") or bundle_id)
            if not requested_bundle_id:
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details("bundle_id is required")
                requested_bundle_id = ""
            payload = build_service_readiness_payload(
                request_id=str(getattr(request, "request_id", "")),
                bundle_id=requested_bundle_id,
                include_details=bool(getattr(request, "include_details", False)),
                root=root,
            )
            return pb2.ServiceReadinessResponse(**payload)

        def PublishPortfolioPatch(self, request: Any, context: Any) -> Any:  # noqa: N802
            return pb2.Ack(**_ack_from_request(request))

        def PublishFinalDecision(self, request: Any, context: Any) -> Any:  # noqa: N802
            return pb2.Ack(**_ack_from_request(request))

        def PublishExecutionFeedback(self, request: Any, context: Any) -> Any:  # noqa: N802
            if bool(getattr(request, "live_enabled", False)):
                return pb2.Ack(
                    **_ack_from_request(
                        request,
                        accepted=False,
                        status="REJECTED_LIVE_DISABLED",
                        reason="AI gRPC bridge rejects live_enabled=true by default",
                    )
                )
            return pb2.Ack(**_ack_from_request(request))

        def PublishInternalMessage(self, request: Any, context: Any) -> Any:  # noqa: N802
            return pb2.Ack(**_ack_from_request(request))

        def PublishAgentReport(self, request: Any, context: Any) -> Any:  # noqa: N802
            return pb2.Ack(**_ack_from_request(request))

        def GetRecommendations(self, request: Any, context: Any) -> Any:  # noqa: N802
            payload = build_recommendations_payload(
                request_id=str(getattr(request, "request_id", "")),
                bundle_id=str(getattr(request, "bundle_id", "") or bundle_id),
                asof=str(getattr(request, "asof", "")),
                tickers=list(getattr(request, "tickers", [])),
                top_k=int(getattr(request, "top_k", 0) or 0),
                include_diagnostics=bool(getattr(request, "include_diagnostics", False)),
                root=root,
            )
            items = [
                pb2.RecommendationItem(**item)
                for item in payload.pop("recommendations", [])
            ]
            return pb2.GetRecommendationsResponse(
                **payload,
                recommendations=items,
            )

        def StartPaperAutoTrading(self, request: Any, context: Any) -> Any:  # noqa: N802
            pa_cfg = config_load("risk_config.yaml", "paper_auto_trading") or {}
            if "default_max_cycles" not in pa_cfg:
                context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
                context.set_details("paper_auto_trading.default_max_cycles 설정 누락")
                return pb2.StartPaperAutoTradingResponse(
                    request_id=str(getattr(request, "request_id", "")),
                    accepted=False, status="CONFIG_MISSING",
                    reason="paper_auto_trading.default_max_cycles 설정 누락",
                )
            if "default_interval_sec" not in pa_cfg:
                context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
                context.set_details("paper_auto_trading.default_interval_sec 설정 누락")
                return pb2.StartPaperAutoTradingResponse(
                    request_id=str(getattr(request, "request_id", "")),
                    accepted=False, status="CONFIG_MISSING",
                    reason="paper_auto_trading.default_interval_sec 설정 누락",
                )
            try:
                default_cycles = int(pa_cfg["default_max_cycles"])
                default_interval = int(pa_cfg["default_interval_sec"])
                requested_interval = int(
                    getattr(request, "interval_sec", 0) or default_interval
                )
                raw_requested_cycles = int(getattr(request, "cycles", 0) or 0)
                max_tickers = int(pa_cfg.get("max_tickers", 0) or 0)
            except Exception as e:
                context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
                context.set_details(f"paper_auto_trading 설정값 변환 실패: {e}")
                return pb2.StartPaperAutoTradingResponse(
                    request_id=str(getattr(request, "request_id", "")),
                    accepted=False, status="CONFIG_INVALID",
                    reason=f"paper_auto_trading_config_invalid:{e}",
                )
            requested_bundle_id = str(getattr(request, "bundle_id", "") or bundle_id)
            requested_tickers, invalid_tickers = _normalize_start_tickers(
                getattr(request, "tickers", []),
            )
            ticker_source = "request"
            if not requested_tickers and not invalid_tickers:
                requested_tickers = _load_active_start_tickers(max_tickers)
                ticker_source = "universe_config"
            confirm_phrase = str(getattr(request, "confirm_phrase", ""))
            if raw_requested_cycles < 0:
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details("negative_cycles_not_allowed")
                return pb2.StartPaperAutoTradingResponse(
                    request_id=str(getattr(request, "request_id", "")),
                    accepted=False,
                    status="INVALID_ARGUMENT",
                    reason="negative_cycles_not_allowed",
                )
            requested_cycles_for_validation = (
                raw_requested_cycles if raw_requested_cycles > 0 else default_cycles
            )
            validation = _validate_paper_auto_start_args(
                bundle_id=requested_bundle_id,
                cycles=requested_cycles_for_validation,
                interval_sec=requested_interval,
                tickers=requested_tickers,
                invalid_tickers=invalid_tickers,
                confirm_phrase=confirm_phrase,
                required_confirm_phrase=str(pa_cfg.get("confirm_start_phrase", "")),
                max_tickers=max_tickers,
            )
            if validation["status"] != "PASS":
                context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
                context.set_details(str(validation["reason"]))
                return pb2.StartPaperAutoTradingResponse(
                    request_id=str(getattr(request, "request_id", "")),
                    accepted=False,
                    status=str(validation["status"]),
                    reason=str(validation["reason"]),
                )
            repo_root = root or _PROJECT_ROOT
            registry_dir = _candidate_registry_dir(repo_root, requested_bundle_id)
            if registry_dir is None:
                context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
                context.set_details("paper_candidate_registry_not_found")
                return pb2.StartPaperAutoTradingResponse(
                    request_id=str(getattr(request, "request_id", "")),
                    accepted=False,
                    status="MODEL_REGISTRY_NOT_READY",
                    reason="paper_candidate_registry_not_found",
                )
            try:
                market_guard = _market_start_guard(pa_cfg=pa_cfg)
            except ValueError as e:
                context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
                context.set_details(f"paper_auto_trading_market_time_invalid:{e}")
                return pb2.StartPaperAutoTradingResponse(
                    request_id=str(getattr(request, "request_id", "")),
                    accepted=False,
                    status="CONFIG_INVALID",
                    reason=f"paper_auto_trading_market_time_invalid:{e}",
                )
            if market_guard.get("status") != "PASS":
                context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
                context.set_details(str(market_guard.get("reason", "")))
                return pb2.StartPaperAutoTradingResponse(
                    request_id=str(getattr(request, "request_id", "")),
                    accepted=False,
                    status=str(market_guard.get("reason", "MARKET_NOT_OPEN")).upper(),
                    reason=str(market_guard.get("reason", "")),
                )
            try:
                remaining_cycles = _remaining_market_cycles(
                    interval_sec=requested_interval,
                    pa_cfg=pa_cfg,
                )
            except ValueError as e:
                context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
                context.set_details(f"paper_auto_trading_market_time_invalid:{e}")
                return pb2.StartPaperAutoTradingResponse(
                    request_id=str(getattr(request, "request_id", "")),
                    accepted=False,
                    status="CONFIG_INVALID",
                    reason=f"paper_auto_trading_market_time_invalid:{e}",
                )
            if raw_requested_cycles > 0:
                requested_cycles = min(raw_requested_cycles, remaining_cycles)
            else:
                requested_cycles = min(default_cycles, remaining_cycles)
            if requested_cycles <= 0:
                context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
                context.set_details("market_session_not_open_or_no_remaining_cycles")
                return pb2.StartPaperAutoTradingResponse(
                    request_id=str(getattr(request, "request_id", "")),
                    accepted=False,
                    status="MARKET_CLOSED",
                    reason="market_session_not_open_or_no_remaining_cycles",
                )
            try:
                paper_gate = _paper_start_gate_from_service_readiness(
                    bundle_id=requested_bundle_id,
                    repo_root=repo_root,
                    candidate_registry_dir=registry_dir,
                )
            except Exception as e:
                context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
                context.set_details(f"paper_start_gate_error:{e}")
                return pb2.StartPaperAutoTradingResponse(
                    request_id=str(getattr(request, "request_id", "")),
                    accepted=False,
                    status="PAPER_START_GATE_ERROR",
                    reason=f"paper_start_gate_error:{e}",
                )
            if paper_gate.get("status") != "PASS":
                reason = ",".join(paper_gate.get("blockers") or [])
                return pb2.StartPaperAutoTradingResponse(
                    request_id=str(getattr(request, "request_id", "")),
                    accepted=False,
                    status="PAPER_START_GATE_BLOCKED",
                    reason=reason,
                )
            result = session.start(
                request_id=str(getattr(request, "request_id", "")),
                bundle_id=requested_bundle_id,
                cycles=requested_cycles,
                interval_sec=requested_interval,
                tickers=list(validation["tickers"]),
                confirm_phrase=confirm_phrase,
                registry_dir=registry_dir,
                root=root,
            )
            if bool(result["accepted"]):
                logger.info(
                    "[grpc_server] StartPaperAutoTrading accepted: "
                    "ticker_source=%s ticker_count=%d",
                    ticker_source,
                    len(validation["tickers"]),
                )
            if not bool(result["accepted"]):
                context.set_code(grpc.StatusCode.FAILED_PRECONDITION)
                context.set_details(str(result.get("reason", "")))
            return pb2.StartPaperAutoTradingResponse(
                request_id=str(getattr(request, "request_id", "")),
                accepted=bool(result["accepted"]),
                status=str(result["status"]),
                reason=str(result.get("reason", "")),
                session_id=str(result.get("session_id", "")),
                started_at=session.started_at if result["accepted"] else "",
            )

        def StopPaperAutoTrading(self, request: Any, context: Any) -> Any:  # noqa: N802
            result = session.stop(
                session_id=str(getattr(request, "session_id", "")),
            )
            return pb2.StopPaperAutoTradingResponse(
                request_id=str(getattr(request, "request_id", "")),
                accepted=bool(result["accepted"]),
                status=str(result["status"]),
                reason=str(result.get("reason", "")),
                stopped_at=datetime.now(_KST).isoformat() if result["accepted"] else "",
            )

        def GetPaperAutoTradingStatus(self, request: Any, context: Any) -> Any:  # noqa: N802
            s = session.status()
            return pb2.PaperAutoTradingStatusResponse(
                request_id=str(getattr(request, "request_id", "")),
                running=bool(s["running"]),
                session_id=str(s["session_id"]),
                status=str(s["status"]),
                completed_cycles=int(s["completed_cycles"]),
                total_cycles=int(s["total_cycles"]),
                started_at=str(s["started_at"]),
                bundle_id=str(s["bundle_id"]),
                last_cycle_at=str(s["last_cycle_at"]),
                stop_requested=bool(s["stop_requested"]),
                terminal_status=str(s["terminal_status"]),
                stop_reason=str(s["stop_reason"]),
                ended_at=str(s["ended_at"]),
                report_path=str(s["report_path"]),
                kafka_connected=bool(s["kafka"]["connected"]),
                kafka_topic=str(s["kafka"]["topic"]),
                kafka_last_error=str(s["kafka"]["last_error"]),
                last_error=str(s["last_error"]),
                start_request_id=str(s["request_id"]),
                orders_submitted=int(s["orders_submitted"]),
                orders_filled=int(s["orders_filled"]),
                orders_rejected=int(s["orders_rejected"]),
                last_cycle_status=str(s["last_cycle_status"]),
                kafka_required=bool(s["kafka"]["required"]),
                kis_mode="virtual",
                live_trading_allowed=False,
                production_registry_mutated=False,
            )

    return AiBeBridgeServicer()


def _ack_from_request(
    request: Any,
    *,
    accepted: bool = True,
    status: str = "ACK_READ_ONLY",
    reason: str = "validated by AI gRPC bridge; no live trading or registry mutation",
) -> dict[str, Any]:
    return build_ack_payload(
        request_id=str(getattr(request, "request_id", "")),
        idempotency_key=str(getattr(request, "idempotency_key", "")),
        accepted=accepted,
        status=status,
        reason=reason,
    )


class _ManagedGrpcServer:
    """grpc.Server wrapper that also stops paper-auto and Kafka resources."""

    def __init__(
        self,
        server: Any,
        *,
        session: _PaperAutoSession,
        kafka: KafkaEventProducer,
    ) -> None:
        self._server = server
        self._session = session
        self._kafka = kafka

    def __getattr__(self, name: str) -> Any:
        return getattr(self._server, name)

    def stop(self, grace: float | None = None) -> Any:
        timeout = float(grace or 0)
        self._session.shutdown(timeout_sec=max(0.0, timeout))
        try:
            self._kafka.flush()
        finally:
            self._kafka.close()
        return self._server.stop(grace)

    @property
    def session(self) -> _PaperAutoSession:
        return self._session

    @property
    def kafka(self) -> KafkaEventProducer:
        return self._kafka


def serve(
    *,
    host: str,
    port: int,
    bundle_id: str,
    root: Path | None = None,
    max_workers: int = 4,
    kafka_required: bool = False,
) -> Any:
    """Start the AI gRPC server and return the grpc.Server instance."""
    grpc, pb2, pb2_grpc = _load_grpc_modules()
    kafka = KafkaEventProducer(required=kafka_required)
    session = _PaperAutoSession(kafka=kafka)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    pb2_grpc.add_AiBeBridgeServiceServicer_to_server(
        _make_servicer(
            grpc, pb2, pb2_grpc,
            bundle_id=bundle_id, root=root, session=session,
        ),
        server,
    )
    server.add_insecure_port(f"{host}:{int(port)}")
    server.start()
    return _ManagedGrpcServer(server, session=session, kafka=kafka)
