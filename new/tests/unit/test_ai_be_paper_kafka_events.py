"""Tests for the paper-auto Kafka event contract consumed by BE."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

import src.integration.grpc.server as grpc_server
from src.integration.grpc.server import (
    _PaperAutoSession,
    _load_active_start_tickers,
    _normalize_start_tickers,
    _paper_cycle_events,
    _validate_paper_auto_start_args,
)
from src.integration.kafka.producer import KafkaEventProducer


class _RecordingKafka:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.flushed = False

    def emit(self, event_type: str, **kwargs) -> None:
        self.events.append((event_type, kwargs))

    def flush(self) -> None:
        self.flushed = True

    def status(self) -> dict:
        return {
            "connected": False,
            "topic": "test-topic",
            "last_error": "no broker in unit test",
        }


def _write_paper_candidate_registry(tmp_path, bundle_id: str = "BUNDLE-1"):
    registry_dir = tmp_path / "artifacts" / "lgbm_paper_candidate" / bundle_id
    registry_dir.mkdir(parents=True)
    (registry_dir / "v1.pkl").write_bytes(b"pickle-placeholder")
    (registry_dir / "v1_metadata.json").write_text(
        json.dumps(
            {
                "bundle_id": bundle_id,
                "feature_cols": ["feat_1m_close_robust_z"],
                "feature_manifest": {"feature_cols": ["feat_1m_close_robust_z"]},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (registry_dir / "registry.json").write_text(
        (
            "{"
            '"schema_version":"1.0.0",'
            '"active_version":"v1",'
            '"paper_only_registry":true,'
            '"live_trading_allowed":false,'
            '"versions":[{'
            '"version":"v1",'
            f'"bundle_id":"{bundle_id}",'
            '"model_path":"v1.pkl",'
            '"metadata_path":"v1_metadata.json",'
            '"live_trading_allowed":false,'
            '"feature_cols":["feat_1m_close_robust_z"],'
            '"feature_manifest":{"feature_cols":["feat_1m_close_robust_z"]}'
            "}]"
            "}"
        ),
        encoding="utf-8",
    )
    return registry_dir


def test_kafka_envelope_contains_ids_and_nested_payload() -> None:
    class _Future:
        def add_errback(self, _callback) -> None:
            return None

    class _Producer:
        value = None

        def send(self, _topic, *, key, value):
            assert key == "AI-SESSION"
            self.value = value
            return _Future()

    kafka_sender = _Producer()
    producer = KafkaEventProducer.__new__(KafkaEventProducer)
    producer._topic = "test-topic"
    producer._producer = kafka_sender
    producer._required = False
    producer._sequence_by_session = {}

    producer.emit(
        "AUTO_TRADING_STARTED",
        session_id="AI-SESSION",
        request_id="BE-REQ-001",
        bundle_id="BUNDLE-1",
        payload={"cycles": 3},
    )

    uuid.UUID(kafka_sender.value["event_id"])
    assert kafka_sender.value["session_id"] == "AI-SESSION"
    assert kafka_sender.value["request_id"] == "BE-REQ-001"
    assert kafka_sender.value["bundle_id"] == "BUNDLE-1"
    assert kafka_sender.value["event_key"] == "AI-SESSION"
    assert kafka_sender.value["sequence_no"] == 1
    assert kafka_sender.value["timestamp"]
    assert kafka_sender.value["payload"] == {"cycles": 3}
    assert "cycles" not in {
        key for key in kafka_sender.value
        if key != "payload"
    }


def test_kafka_required_emit_defers_delivery_wait_until_flush() -> None:
    class _Future:
        get_called = False

        def add_errback(self, _callback) -> None:
            return None

        def get(self, timeout=None):
            self.get_called = True
            assert timeout == 10

    class _Producer:
        def __init__(self) -> None:
            self.future = _Future()
            self.flushed = False

        def send(self, *_args, **_kwargs):
            return self.future

        def flush(self) -> None:
            self.flushed = True

    kafka_sender = _Producer()
    producer = KafkaEventProducer.__new__(KafkaEventProducer)
    producer._topic = "test-topic"
    producer._producer = kafka_sender
    producer._required = True
    producer._sequence_by_session = {}
    producer._last_error = ""
    producer._delivery_timeout_sec = 10

    producer.emit("AUTO_TRADING_STARTED", session_id="AI-SESSION")

    assert kafka_sender.future.get_called is False
    producer.flush()
    assert kafka_sender.flushed is True
    assert kafka_sender.future.get_called is True


def test_kafka_status_reports_readiness_without_secret_values() -> None:
    producer = KafkaEventProducer.__new__(KafkaEventProducer)
    producer._topic = "paper-topic"
    producer._bootstrap_servers = "localhost:9092"
    producer._producer = None
    producer._last_error = "connection refused"

    status = producer.status()

    assert status == {
        "connected": False,
        "topic": "paper-topic",
        "bootstrap_servers_set": True,
        "required": False,
        "last_error": "connection refused",
    }


def test_kafka_required_mode_raises_when_dependency_missing(monkeypatch) -> None:
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "kafka":
            raise ImportError("missing kafka")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    try:
        KafkaEventProducer(required=True)
    except RuntimeError as e:
        assert "kafka-python" in str(e)
    else:
        raise AssertionError("required Kafka must fail startup when unavailable")


def test_kafka_required_emit_failure_raises() -> None:
    class _Producer:
        def send(self, *_args, **_kwargs):
            raise RuntimeError("broker down")

    producer = KafkaEventProducer.__new__(KafkaEventProducer)
    producer._topic = "test-topic"
    producer._producer = _Producer()
    producer._required = True
    producer._last_error = ""
    producer._sequence_by_session = {}

    try:
        producer.emit(
            "AUTO_TRADING_STARTED",
            session_id="AI-SESSION",
            request_id="REQ-1",
            bundle_id="BUNDLE-1",
        )
    except RuntimeError as e:
        assert "Kafka required emit failed" in str(e)
    else:
        raise AssertionError("required Kafka must fail closed on emit failure")
    assert producer._last_error == "broker down"


def test_start_validation_rejects_bad_request_before_thread_start() -> None:
    tickers, invalid = _normalize_start_tickers(["005930", "BAD"])

    result = _validate_paper_auto_start_args(
        bundle_id="BUNDLE-1",
        cycles=1,
        interval_sec=60,
        tickers=tickers,
        invalid_tickers=invalid,
        confirm_phrase="WRONG",
        required_confirm_phrase="PAPER_AUTO_OK",
        max_tickers=30,
    )

    assert result["status"] == "INVALID_ARGUMENT"
    assert result["reason"] == "confirm_phrase_missing_or_mismatch"


def test_start_validation_normalizes_tickers_and_accepts_valid_request() -> None:
    tickers, invalid = _normalize_start_tickers(["5930", "000660"])

    result = _validate_paper_auto_start_args(
        bundle_id="BUNDLE-1",
        cycles=1,
        interval_sec=60,
        tickers=tickers,
        invalid_tickers=invalid,
        confirm_phrase="PAPER_AUTO_OK",
        required_confirm_phrase="PAPER_AUTO_OK",
        max_tickers=30,
    )

    assert result == {"status": "PASS", "tickers": ["005930", "000660"]}


def test_start_validation_rejects_sub_minute_interval() -> None:
    tickers, invalid = _normalize_start_tickers(["5930", "000660"])

    result = _validate_paper_auto_start_args(
        bundle_id="BUNDLE-1",
        cycles=1,
        interval_sec=10,
        tickers=tickers,
        invalid_tickers=invalid,
        confirm_phrase="PAPER_AUTO_OK",
        required_confirm_phrase="PAPER_AUTO_OK",
        max_tickers=30,
    )

    assert result["status"] == "INVALID_ARGUMENT"
    assert result["reason"] == "interval_sec_must_be_at_least_60"


def test_load_active_start_tickers_uses_universe_ssot(monkeypatch) -> None:
    monkeypatch.setattr(
        grpc_server,
        "config_load",
        lambda *_args, **_kwargs: {
            "sectors": {
                "반도체": {
                    "stocks": [
                        {"ticker": "5930", "status": "active"},
                        {"ticker": "000660", "status": "active"},
                        {"ticker": "035420", "status": "pending"},
                    ],
                },
                "조선": {
                    "stocks": [
                        {"ticker": "329180", "status": "active"},
                    ],
                },
            }
        },
    )

    assert _load_active_start_tickers(30) == ["005930", "000660", "329180"]
    assert _load_active_start_tickers(2) == ["005930", "000660"]


def test_start_rpc_rejects_bad_confirm_before_session_start(monkeypatch) -> None:
    kafka = _RecordingKafka()
    session = _PaperAutoSession(kafka=kafka)

    class _Grpc:
        class StatusCode:
            INVALID_ARGUMENT = "INVALID_ARGUMENT"
            FAILED_PRECONDITION = "FAILED_PRECONDITION"

    class _Pb2Grpc:
        class AiBeBridgeServiceServicer:
            pass

    class _Pb2:
        class StartPaperAutoTradingResponse:
            def __init__(self, **kwargs) -> None:
                self.__dict__.update(kwargs)

    class _Context:
        code = None
        details = ""

        def set_code(self, code) -> None:
            self.code = code

        def set_details(self, details: str) -> None:
            self.details = details

    class _Request:
        request_id = "BE-REQ-001"
        bundle_id = "BUNDLE-1"
        cycles = 1
        interval_sec = 60
        tickers = ["005930"]
        confirm_phrase = "WRONG"

    def fail_if_called(**_kwargs):
        raise AssertionError("session.start must not run for invalid Start RPC")

    monkeypatch.setattr(
        grpc_server,
        "config_load",
        lambda *_args, **_kwargs: {
            "default_max_cycles": 1,
            "default_interval_sec": 60,
            "confirm_start_phrase": "PAPER_AUTO_OK",
            "max_tickers": 30,
        },
    )
    monkeypatch.setattr(session, "start", fail_if_called)

    servicer = grpc_server._make_servicer(
        _Grpc,
        _Pb2,
        _Pb2Grpc,
        bundle_id="BUNDLE-1",
        root=None,
        session=session,
    )
    context = _Context()

    response = servicer.StartPaperAutoTrading(_Request(), context)

    assert response.accepted is False
    assert response.status == "INVALID_ARGUMENT"
    assert response.reason == "confirm_phrase_missing_or_mismatch"
    assert context.code == _Grpc.StatusCode.INVALID_ARGUMENT
    assert context.details == "confirm_phrase_missing_or_mismatch"
    assert kafka.events == []


def test_start_rpc_requires_paper_candidate_registry_before_session_start(
    monkeypatch,
    tmp_path,
) -> None:
    kafka = _RecordingKafka()
    session = _PaperAutoSession(kafka=kafka)

    class _Grpc:
        class StatusCode:
            INVALID_ARGUMENT = "INVALID_ARGUMENT"
            FAILED_PRECONDITION = "FAILED_PRECONDITION"

    class _Pb2Grpc:
        class AiBeBridgeServiceServicer:
            pass

    class _Pb2:
        class StartPaperAutoTradingResponse:
            def __init__(self, **kwargs) -> None:
                self.__dict__.update(kwargs)

    class _Context:
        code = None
        details = ""

        def set_code(self, code) -> None:
            self.code = code

        def set_details(self, details: str) -> None:
            self.details = details

    class _Request:
        request_id = "BE-REQ-001"
        bundle_id = "BUNDLE-MISSING"
        cycles = 1
        interval_sec = 60
        tickers = ["005930"]
        confirm_phrase = "PAPER_AUTO_OK"

    monkeypatch.setattr(
        grpc_server,
        "config_load",
        lambda *_args, **_kwargs: {
            "default_max_cycles": 1,
            "default_interval_sec": 60,
            "confirm_start_phrase": "PAPER_AUTO_OK",
            "max_tickers": 30,
            "require_prelive_pass": False,
        },
    )
    monkeypatch.setattr(
        grpc_server,
        "_market_start_guard",
        lambda **_kwargs: {"status": "PASS"},
    )
    monkeypatch.setattr(session, "start", lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("session.start must not run without candidate registry")
    ))

    servicer = grpc_server._make_servicer(
        _Grpc,
        _Pb2,
        _Pb2Grpc,
        bundle_id="BUNDLE-MISSING",
        root=tmp_path,
        session=session,
    )

    response = servicer.StartPaperAutoTrading(_Request(), _Context())

    assert response.accepted is False
    assert response.status == "MODEL_REGISTRY_NOT_READY"
    assert response.reason == "paper_candidate_registry_not_found"


def test_start_rpc_passes_candidate_registry_and_allows_paper_gate(
    monkeypatch,
    tmp_path,
) -> None:
    registry_dir = _write_paper_candidate_registry(tmp_path)
    kafka = _RecordingKafka()
    session = _PaperAutoSession(kafka=kafka)
    calls: dict[str, object] = {}

    class _Grpc:
        class StatusCode:
            INVALID_ARGUMENT = "INVALID_ARGUMENT"
            FAILED_PRECONDITION = "FAILED_PRECONDITION"

    class _Pb2Grpc:
        class AiBeBridgeServiceServicer:
            pass

    class _Pb2:
        class StartPaperAutoTradingResponse:
            def __init__(self, **kwargs) -> None:
                self.__dict__.update(kwargs)

    class _Context:
        code = None
        details = ""

        def set_code(self, code) -> None:
            self.code = code

        def set_details(self, details: str) -> None:
            self.details = details

    class _Request:
        request_id = "BE-REQ-001"
        bundle_id = "BUNDLE-1"
        cycles = 1
        interval_sec = 60
        tickers = ["005930"]
        confirm_phrase = "PAPER_AUTO_OK"

    monkeypatch.setattr(
        grpc_server,
        "config_load",
        lambda *_args, **_kwargs: {
            "default_max_cycles": 1,
            "default_interval_sec": 60,
            "confirm_start_phrase": "PAPER_AUTO_OK",
            "max_tickers": 30,
            "require_prelive_pass": True,
        },
    )
    monkeypatch.setattr(
        grpc_server,
        "_market_start_guard",
        lambda **_kwargs: {"status": "PASS"},
    )
    monkeypatch.setattr(
        grpc_server,
        "_remaining_market_cycles",
        lambda **_kwargs: 1,
    )

    def mock_load_prelive_gate_module():
        raise AssertionError("Start RPC must not rebuild strict prelive gate")

    monkeypatch.setattr(
        grpc_server,
        "_load_prelive_gate_module",
        mock_load_prelive_gate_module,
    )
    monkeypatch.setattr(
        grpc_server,
        "build_service_status",
        lambda **_kwargs: {
            "status": "PASS",
            "deploy_quality": "PASS",
            "broker_evidence": "PASS",
            "read_only": True,
            "external_api_called": False,
            "live_trading_allowed": False,
            "registry_mutated": False,
            "be_contract": {
                "safe_to_enable_order_actions": True,
                "safe_to_enable_live_actions": False,
            },
        },
    )

    def fake_start(**kwargs):
        calls.update(kwargs)
        return {
            "accepted": True,
            "status": "START_REQUESTED",
            "session_id": "AI-SESSION",
        }

    monkeypatch.setattr(session, "start", fake_start)

    servicer = grpc_server._make_servicer(
        _Grpc,
        _Pb2,
        _Pb2Grpc,
        bundle_id="BUNDLE-1",
        root=tmp_path,
        session=session,
    )

    response = servicer.StartPaperAutoTrading(_Request(), _Context())

    assert response.accepted is True
    assert response.status == "START_REQUESTED"
    assert calls["registry_dir"] == registry_dir


@pytest.mark.parametrize(
    ("readiness_overrides", "contract_overrides", "expected_reason"),
    [
        (
            {"broker_evidence": "BLOCKED"},
            {"safe_to_enable_order_actions": False},
            "broker_evidence_not_pass,order_actions_not_enabled",
        ),
        ({"status": "PARTIAL"}, {}, "service_readiness_not_pass"),
        ({"live_trading_allowed": True}, {}, "live_trading_allowed_true"),
        ({"registry_mutated": True}, {}, "registry_mutated_true"),
        ({}, {"safe_to_enable_live_actions": True}, "live_actions_enabled"),
    ],
)
@pytest.mark.parametrize("require_prelive_pass", [True, False])
def test_start_rpc_blocks_on_service_readiness_gate_without_grpc_error(
    monkeypatch,
    tmp_path,
    readiness_overrides,
    contract_overrides,
    expected_reason,
    require_prelive_pass,
) -> None:
    registry_dir = _write_paper_candidate_registry(tmp_path)
    kafka = _RecordingKafka()
    session = _PaperAutoSession(kafka=kafka)

    class _Grpc:
        class StatusCode:
            INVALID_ARGUMENT = "INVALID_ARGUMENT"
            FAILED_PRECONDITION = "FAILED_PRECONDITION"

    class _Pb2Grpc:
        class AiBeBridgeServiceServicer:
            pass

    class _Pb2:
        class StartPaperAutoTradingResponse:
            def __init__(self, **kwargs) -> None:
                self.__dict__.update(kwargs)

    class _Context:
        code = None
        details = ""

        def set_code(self, code) -> None:
            self.code = code

        def set_details(self, details: str) -> None:
            self.details = details

    class _Request:
        request_id = "BE-REQ-BLOCKED"
        bundle_id = "BUNDLE-1"
        cycles = 1
        interval_sec = 60
        tickers = ["005930"]
        confirm_phrase = "PAPER_AUTO_OK"

    monkeypatch.setattr(
        grpc_server,
        "config_load",
        lambda *_args, **_kwargs: {
            "default_max_cycles": 1,
            "default_interval_sec": 60,
            "confirm_start_phrase": "PAPER_AUTO_OK",
            "max_tickers": 30,
            "require_prelive_pass": require_prelive_pass,
        },
    )
    monkeypatch.setattr(
        grpc_server,
        "_market_start_guard",
        lambda **_kwargs: {"status": "PASS"},
    )
    monkeypatch.setattr(
        grpc_server,
        "_remaining_market_cycles",
        lambda **_kwargs: 1,
    )
    readiness = {
        "status": "PASS",
        "deploy_quality": "PASS",
        "broker_evidence": "PASS",
        "read_only": True,
        "external_api_called": False,
        "live_trading_allowed": False,
        "registry_mutated": False,
        "be_contract": {
            "safe_to_enable_order_actions": True,
            "safe_to_enable_live_actions": False,
        },
    }
    readiness.update(readiness_overrides)
    readiness["be_contract"].update(contract_overrides)
    monkeypatch.setattr(
        grpc_server,
        "build_service_status",
        lambda **_kwargs: readiness,
    )

    def mock_start(**_kwargs):
        raise AssertionError(
            "session.start must not run when paper start gate is blocked"
        )

    monkeypatch.setattr(session, "start", mock_start)

    servicer = grpc_server._make_servicer(
        _Grpc,
        _Pb2,
        _Pb2Grpc,
        bundle_id="BUNDLE-1",
        root=tmp_path,
        session=session,
    )
    context = _Context()

    response = servicer.StartPaperAutoTrading(_Request(), context)

    assert response.accepted is False
    assert response.status == "PAPER_START_GATE_BLOCKED"
    assert response.reason == expected_reason
    assert context.code is None
    assert context.details == ""
    assert kafka.events == []


@pytest.mark.parametrize(
    ("readiness_overrides", "contract_overrides", "expected_blocker"),
    [
        ({"status": "PARTIAL"}, {}, "service_readiness_not_pass"),
        ({"live_trading_allowed": True}, {}, "live_trading_allowed_true"),
        ({"registry_mutated": True}, {}, "registry_mutated_true"),
        ({}, {"safe_to_enable_live_actions": True}, "live_actions_enabled"),
    ],
)
def test_paper_start_gate_blocks_safety_invariants(
    monkeypatch,
    tmp_path,
    readiness_overrides,
    contract_overrides,
    expected_blocker,
) -> None:
    readiness = {
        "status": "PASS",
        "deploy_quality": "PASS",
        "broker_evidence": "PASS",
        "read_only": True,
        "external_api_called": False,
        "live_trading_allowed": False,
        "registry_mutated": False,
        "be_contract": {
            "safe_to_enable_order_actions": True,
            "safe_to_enable_live_actions": False,
        },
    }
    readiness.update(readiness_overrides)
    readiness["be_contract"].update(contract_overrides)
    monkeypatch.setattr(
        grpc_server,
        "validate_paper_candidate_registry",
        lambda **_kwargs: {"status": "PASS", "blockers": [], "warnings": []},
    )
    monkeypatch.setattr(
        grpc_server,
        "build_service_status",
        lambda **_kwargs: readiness,
    )

    gate = grpc_server._paper_start_gate_from_service_readiness(
        bundle_id="BUNDLE-1",
        repo_root=tmp_path,
        candidate_registry_dir=tmp_path / "registry",
    )

    assert gate["status"] == "BLOCKED"
    assert expected_blocker in gate["blockers"]


def test_paper_start_gate_blocks_invalid_candidate_registry(
    monkeypatch,
    tmp_path,
) -> None:
    registry_dir = tmp_path / "artifacts" / "lgbm_paper_candidate" / "BUNDLE-1"
    registry_dir.mkdir(parents=True)
    (registry_dir / "registry.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        grpc_server,
        "build_service_status",
        lambda **_kwargs: {
            "status": "PASS",
            "deploy_quality": "PASS",
            "broker_evidence": "PASS",
            "read_only": True,
            "external_api_called": False,
            "live_trading_allowed": False,
            "registry_mutated": False,
            "be_contract": {
                "safe_to_enable_order_actions": True,
                "safe_to_enable_live_actions": False,
            },
        },
    )

    gate = grpc_server._paper_start_gate_from_service_readiness(
        bundle_id="BUNDLE-1",
        repo_root=tmp_path,
        candidate_registry_dir=registry_dir,
    )

    assert gate["status"] == "BLOCKED"
    assert "paper_candidate_registry_not_paper_only" in gate["blockers"]
    assert "paper_candidate_registry_active_version_missing" in gate["blockers"]


def test_paper_candidate_registry_blocks_artifact_paths_outside_allowed_roots(tmp_path) -> None:
    registry_dir = _write_paper_candidate_registry(tmp_path)
    outside_model = tmp_path.parent / f"outside-model-{uuid.uuid4()}.pkl"
    outside_model.write_bytes(b"pickle-placeholder")
    registry_path = registry_dir / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["versions"][0]["model_path"] = str(outside_model)
    registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")

    result = grpc_server.validate_paper_candidate_registry(
        repo_root=tmp_path,
        bundle_id="BUNDLE-1",
        registry_dir=registry_dir,
    )

    assert result["status"] == "BLOCKED"
    assert "paper_candidate_model_path_outside_allowed_roots" in result["blockers"]


def test_paper_candidate_registry_reports_path_os_error(monkeypatch, tmp_path) -> None:
    registry_dir = _write_paper_candidate_registry(tmp_path)
    original_exists = Path.exists

    def fake_exists(path):
        if path.name == "v1.pkl":
            raise OSError("permission denied")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", fake_exists)

    result = grpc_server.validate_paper_candidate_registry(
        repo_root=tmp_path,
        bundle_id="BUNDLE-1",
        registry_dir=registry_dir,
    )

    assert result["status"] == "BLOCKED"
    assert "paper_candidate_model_path_os_error" in result["blockers"]
    assert "paper_candidate_model_file_missing" not in result["blockers"]


def test_paper_candidate_registry_blocks_invalid_metadata_json(tmp_path) -> None:
    registry_dir = _write_paper_candidate_registry(tmp_path)
    (registry_dir / "v1_metadata.json").write_text("{", encoding="utf-8")

    result = grpc_server.validate_paper_candidate_registry(
        repo_root=tmp_path,
        bundle_id="BUNDLE-1",
        registry_dir=registry_dir,
    )

    assert result["status"] == "BLOCKED"
    assert "paper_candidate_metadata_invalid_json" in result["blockers"]


def test_paper_candidate_registry_blocks_metadata_bundle_mismatch(tmp_path) -> None:
    registry_dir = _write_paper_candidate_registry(tmp_path)
    metadata_path = registry_dir / "v1_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["bundle_id"] = "BUNDLE-OTHER"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    result = grpc_server.validate_paper_candidate_registry(
        repo_root=tmp_path,
        bundle_id="BUNDLE-1",
        registry_dir=registry_dir,
    )

    assert result["status"] == "BLOCKED"
    assert "paper_candidate_metadata_bundle_mismatch" in result["blockers"]


def test_paper_candidate_registry_blocks_feature_column_mismatch(tmp_path) -> None:
    registry_dir = _write_paper_candidate_registry(tmp_path)
    metadata_path = registry_dir / "v1_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["feature_cols"] = ["other_feature"]
    metadata["feature_manifest"] = {"feature_cols": ["other_feature"]}
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    result = grpc_server.validate_paper_candidate_registry(
        repo_root=tmp_path,
        bundle_id="BUNDLE-1",
        registry_dir=registry_dir,
    )

    assert result["status"] == "BLOCKED"
    assert "paper_candidate_metadata_feature_cols_mismatch" in result["blockers"]
    assert "paper_candidate_metadata_feature_manifest_mismatch" in result["blockers"]


def test_start_rpc_defaults_empty_tickers_to_active_universe(
    monkeypatch,
    tmp_path,
) -> None:
    registry_dir = _write_paper_candidate_registry(tmp_path)
    kafka = _RecordingKafka()
    session = _PaperAutoSession(kafka=kafka)
    calls: dict[str, object] = {}

    class _Grpc:
        class StatusCode:
            INVALID_ARGUMENT = "INVALID_ARGUMENT"
            FAILED_PRECONDITION = "FAILED_PRECONDITION"

    class _Pb2Grpc:
        class AiBeBridgeServiceServicer:
            pass

    class _Pb2:
        class StartPaperAutoTradingResponse:
            def __init__(self, **kwargs) -> None:
                self.__dict__.update(kwargs)

    class _Context:
        code = None
        details = ""

        def set_code(self, code) -> None:
            self.code = code

        def set_details(self, details: str) -> None:
            self.details = details

    class _Request:
        request_id = "BE-REQ-EMPTY-TICKERS"
        bundle_id = "BUNDLE-1"
        cycles = 1
        interval_sec = 60
        tickers: list[str] = []
        confirm_phrase = "PAPER_AUTO_OK"

    def fake_config_load(path, *_args, **_kwargs):
        if path == "universe_config.yaml":
            return {
                "sectors": {
                    "반도체": {
                        "stocks": [
                            {"ticker": "005930", "status": "active"},
                            {"ticker": "000660", "status": "active"},
                            {"ticker": "042700", "status": "active"},
                            {"ticker": "035420", "status": "pending"},
                        ],
                    }
                }
            }
        return {
            "default_max_cycles": 1,
            "default_interval_sec": 60,
            "confirm_start_phrase": "PAPER_AUTO_OK",
            "max_tickers": 3,
            "require_prelive_pass": False,
        }

    monkeypatch.setattr(grpc_server, "config_load", fake_config_load)
    monkeypatch.setattr(
        grpc_server,
        "_market_start_guard",
        lambda **_kwargs: {"status": "PASS"},
    )
    monkeypatch.setattr(
        grpc_server,
        "_remaining_market_cycles",
        lambda **_kwargs: 1,
    )
    monkeypatch.setattr(
        grpc_server,
        "build_service_status",
        lambda **_kwargs: {
            "status": "PASS",
            "deploy_quality": "PASS",
            "broker_evidence": "PASS",
            "read_only": True,
            "external_api_called": False,
            "live_trading_allowed": False,
            "registry_mutated": False,
            "be_contract": {
                "safe_to_enable_order_actions": True,
                "safe_to_enable_live_actions": False,
            },
        },
    )

    def fake_start(**kwargs):
        calls.update(kwargs)
        return {
            "accepted": True,
            "status": "START_REQUESTED",
            "session_id": "AI-SESSION",
        }

    monkeypatch.setattr(session, "start", fake_start)

    servicer = grpc_server._make_servicer(
        _Grpc,
        _Pb2,
        _Pb2Grpc,
        bundle_id="BUNDLE-1",
        root=tmp_path,
        session=session,
    )

    response = servicer.StartPaperAutoTrading(_Request(), _Context())

    assert response.accepted is True
    assert response.status == "START_REQUESTED"
    assert calls["tickers"] == ["005930", "000660", "042700"]
    assert calls["registry_dir"] == registry_dir


def test_paper_cycle_maps_submitted_and_failed_without_false_filled_event() -> None:
    result = {
        "status": "PASS",
        "hot_result": {
            "final_decision": {
                "decision_id": "DEC-1",
                "approved": True,
                "reason_code": "PASS",
                "order_deltas": [{"ticker": "005930"}, {"ticker": "000660"}],
            },
        },
        "execution": {
            "execution_report": {
                "order_plan_id": "OP-1",
                "fills": [{
                    "ticker": "5930",
                    "side": "buy",
                    "qty": 1,
                    "price": None,
                    "order_type": None,
                    "broker_status": "submitted",
                    "broker_order_id": "OD-1",
                    "broker_response": {
                        "price": 72500.0,
                        "order_type": "00",
                    },
                }],
                "rejections": [{
                    "ticker": 660,
                    "side": "buy",
                    "qty": 2,
                    "price": None,
                    "reason": "broker_rt_cd_1",
                    "broker_response": {"msg_cd": "EGW00001", "price": 190000.0},
                }],
            },
        },
    }

    decision, events = _paper_cycle_events(result, cycle=1)

    assert decision["decision_id"] == "DEC-1"
    assert decision["approved"] is True
    assert decision["reason_code"] == "PASS"
    assert decision["order_count"] == 2
    assert [event_type for event_type, _payload in events] == [
        "PAPER_ORDER_SUBMITTED",
        "PAPER_ORDER_REJECTED",
        "PAPER_ORDER_FAILED",
    ]
    submitted = events[0][1]
    assert submitted["ticker"] == "005930"
    assert submitted["quantity"] == 1
    assert submitted["price"] == 72500.0
    assert submitted["order_type"] == "00"
    assert submitted["broker_order_id"] == "OD-1"
    assert submitted["paper_only"] is True
    assert submitted["execution_mode"] == "paper"
    assert submitted["kis_mode"] == "virtual"
    rejected = events[1][1]
    assert rejected["ticker"] == "000660"
    assert rejected["error_code"] == "EGW00001"
    failed = events[2][1]
    assert failed["ticker"] == "000660"
    assert failed["error_code"] == "EGW00001"
    assert failed["reason"] == "broker_rt_cd_1"
    assert failed["price"] == 190000.0


def test_paper_cycle_emits_filled_when_order_history_confirms_fill() -> None:
    result = {
        "execution": {
            "execution_report": {
                "order_plan_id": "OP-1",
                "fills": [{
                    "ticker": "005930",
                    "side": "buy",
                    "qty": 1,
                    "broker_status": "submitted",
                    "broker_order_id": "OD-1",
                }],
            },
        },
        "order_history_verification": {
            "queries": [{
                "matched_orders": [{
                    "ticker": "5930",
                    "side": "buy",
                    "status": "filled",
                    "filled_qty": 1,
                    "avg_fill_price": 72450.0,
                    "broker_response": {"ODNO": "OD-1"},
                }],
            }, {
                "matched_orders": [{
                    "ticker": "5930",
                    "side": "buy",
                    "status": "filled",
                    "filled_qty": 1,
                    "avg_fill_price": 72450.0,
                    "broker_response": {"ODNO": "OD-1"},
                }],
            }],
        },
    }

    _decision, events = _paper_cycle_events(result, cycle=1)

    assert [event_type for event_type, _payload in events] == [
        "PAPER_ORDER_SUBMITTED",
        "PAPER_ORDER_FILLED",
    ]
    filled = events[1][1]
    assert filled["ticker"] == "005930"
    assert filled["filled_quantity"] == 1
    assert filled["filled_price"] == 72450.0
    assert filled["broker_order_id"] == "OD-1"


def test_start_event_carries_start_request_id(monkeypatch) -> None:
    kafka = _RecordingKafka()
    session = _PaperAutoSession(kafka=kafka)
    seen_events_at_thread_start: list[str] = []

    class _Thread:
        def start(self) -> None:
            seen_events_at_thread_start.extend(
                event_type for event_type, _payload in kafka.events
            )
            return None

    monkeypatch.setattr(
        grpc_server.threading,
        "Thread",
        lambda **_kwargs: _Thread(),
    )

    session.start(
        request_id="BE-REQ-001",
        bundle_id="BUNDLE-1",
        cycles=3,
        interval_sec=10,
        tickers=["005930"],
        confirm_phrase="PAPER_ORDER_OK",
    )

    assert kafka.events[0][0] == "AUTO_TRADING_START_REQUESTED"
    assert kafka.events[0][1]["request_id"] == "BE-REQ-001"
    assert seen_events_at_thread_start == ["AUTO_TRADING_START_REQUESTED"]


def test_start_event_precedes_immediate_thread_failure(monkeypatch) -> None:
    kafka = _RecordingKafka()
    session = _PaperAutoSession(kafka=kafka)
    captured: dict[str, object] = {}

    class _Thread:
        def __init__(self, *, target, kwargs, **_unused) -> None:
            captured["target"] = target
            captured["kwargs"] = kwargs

        def start(self) -> None:
            captured["events_at_start"] = [
                event_type for event_type, _payload in kafka.events
            ]
            return None

    class _BrokenTrader:
        def run(self, **_kwargs) -> None:
            raise RuntimeError("paper-auto init failed")

    monkeypatch.setattr(grpc_server.threading, "Thread", _Thread)
    monkeypatch.setattr(
        grpc_server,
        "_make_grpc_paper_auto_trader",
        lambda **_kwargs: _BrokenTrader(),
    )

    session.start(
        request_id="BE-REQ-001",
        bundle_id="BUNDLE-1",
        cycles=1,
        interval_sec=0,
        tickers=["005930"],
        confirm_phrase="PAPER_ORDER_OK",
    )

    assert [event_type for event_type, _payload in kafka.events] == [
        "AUTO_TRADING_START_REQUESTED",
    ]
    assert captured["events_at_start"] == ["AUTO_TRADING_START_REQUESTED"]

    target = captured["target"]
    kwargs = captured["kwargs"]
    assert callable(target)
    assert isinstance(kwargs, dict)
    target(**kwargs)

    assert [event_type for event_type, _payload in kafka.events] == [
        "AUTO_TRADING_START_REQUESTED",
        "AUTO_TRADING_FAILED",
    ]
    assert kafka.events[1][1]["request_id"] == "BE-REQ-001"
    assert kafka.flushed is True
    assert session.status()["last_error"] == "paper-auto init failed"


def test_started_event_is_emitted_only_after_run_preflight(tmp_path) -> None:
    kafka = _RecordingKafka()
    session = _PaperAutoSession(kafka=kafka)
    session.session_id = "AI-SESSION"
    session.request_id = "BE-REQ-001"
    session.bundle_id = "BUNDLE-1"

    trader = grpc_server._make_grpc_paper_auto_trader(
        kafka=kafka,
        session_ref=session,
        kis_client=object(),
        hot_runner=object(),
        report_dir=tmp_path,
    )

    assert kafka.events == []

    trader._on_run_preflight_passed({
        "cycles": 3,
        "tickers": ["005930"],
    })

    assert [event_type for event_type, _payload in kafka.events] == [
        "AUTO_TRADING_STARTED",
    ]
    payload = kafka.events[0][1]["payload"]
    assert payload["phase"] == "RUNNING"
    assert payload["worker_started"] is True
    assert payload["preflight_passed"] is True


def test_decision_event_is_published_before_broker_submit(tmp_path) -> None:
    kafka = _RecordingKafka()
    session = _PaperAutoSession(kafka=kafka)
    session.session_id = "AI-SESSION"
    session.request_id = "BE-REQ-001"
    session.bundle_id = "BUNDLE-1"
    trader = grpc_server._make_grpc_paper_auto_trader(
        kafka=kafka,
        session_ref=session,
        kis_client=object(),
        hot_runner=object(),
        report_dir=tmp_path,
    )

    trader._on_before_broker_submit(
        cycle_index=0,
        final_decision={
            "decision_id": "DEC-1",
            "approved": True,
            "reason_code": "PASS",
            "order_deltas": [{"ticker": "005930"}],
        },
        hot_result={"quant_output": {"mode": "active"}},
        order_guard={"status": "PASS"},
    )

    assert [event_type for event_type, _payload in kafka.events] == [
        "DECISION_COMPLETED",
    ]
    payload = kafka.events[0][1]["payload"]
    assert payload["phase"] == "PRE_BROKER_SUBMIT"
    assert payload["decision_id"] == "DEC-1"
    assert payload["order_count"] == 1


def test_user_stop_is_published_as_stopped_with_start_request_id(monkeypatch) -> None:
    kafka = _RecordingKafka()
    session = _PaperAutoSession(kafka=kafka)
    session.session_id = "AI-SESSION"
    session.request_id = "BE-REQ-001"
    session.bundle_id = "BUNDLE-1"
    session.running = True
    session._stop_event.set()

    class _Trader:
        def run(self, **_kwargs) -> None:
            return None

    monkeypatch.setattr(
        grpc_server,
        "_make_grpc_paper_auto_trader",
        lambda **_kwargs: _Trader(),
    )

    session._run_loop(
        bundle_id="BUNDLE-1",
        cycles=3,
        interval_sec=10,
        tickers=["005930"],
        confirm_phrase="PAPER_ORDER_OK",
        root=None,
    )

    assert [event_type for event_type, _payload in kafka.events] == [
        "AUTO_TRADING_STOPPED",
    ]
    assert kafka.events[0][1]["request_id"] == "BE-REQ-001"
    assert kafka.events[0][1]["payload"]["stop_reason"] == "USER_REQUESTED"
    assert kafka.events[0][1]["payload"]["terminal_status"] == "STOPPED"
    assert kafka.flushed is True
    assert session.running is False
    assert session.status()["terminal_status"] == "STOPPED"
    assert session.status()["stop_requested"] is True


def test_session_status_exposes_terminal_report_path_and_kafka_state(monkeypatch) -> None:
    kafka = _RecordingKafka()
    session = _PaperAutoSession(kafka=kafka)
    session.session_id = "AI-SESSION"
    session.request_id = "BE-REQ-001"
    session.bundle_id = "BUNDLE-1"
    session.running = True

    class _Trader:
        def run(self, **_kwargs) -> dict:
            return {
                "status": "PASS",
                "report_path_relative": "artifacts/reports/paper_auto_trading/run.json",
            }

    monkeypatch.setattr(
        grpc_server,
        "_make_grpc_paper_auto_trader",
        lambda **_kwargs: _Trader(),
    )

    session._run_loop(
        bundle_id="BUNDLE-1",
        cycles=3,
        interval_sec=10,
        tickers=["005930"],
        confirm_phrase="PAPER_AUTO_OK",
        root=None,
    )

    status = session.status()

    assert status["running"] is False
    assert status["terminal_status"] == "PASS"
    assert status["stop_reason"] == "COMPLETED"
    assert status["ended_at"]
    assert status["report_path"] == "artifacts/reports/paper_auto_trading/run.json"
    assert status["kafka"] == {
        "connected": False,
        "topic": "test-topic",
        "required": False,
        "last_error": "no broker in unit test",
    }


def test_failed_paper_report_emits_failed_not_stopped(monkeypatch) -> None:
    kafka = _RecordingKafka()
    session = _PaperAutoSession(kafka=kafka)
    session.session_id = "AI-SESSION"
    session.request_id = "BE-REQ-001"
    session.bundle_id = "BUNDLE-1"
    session.running = True

    class _Trader:
        def run(self, **_kwargs) -> dict:
            return {
                "status": "FAIL",
                "report_path_relative": "artifacts/reports/paper_auto_trading/fail.json",
            }

    monkeypatch.setattr(
        grpc_server,
        "_make_grpc_paper_auto_trader",
        lambda **_kwargs: _Trader(),
    )

    session._run_loop(
        bundle_id="BUNDLE-1",
        cycles=1,
        interval_sec=0,
        tickers=["005930"],
        confirm_phrase="PAPER_AUTO_OK",
        root=None,
    )

    assert [event_type for event_type, _payload in kafka.events] == [
        "AUTO_TRADING_FAILED",
    ]
    assert kafka.events[0][1]["payload"]["stop_reason"] == "FAIL_CLOSED"
    assert session.status()["terminal_status"] == "FAIL"


def test_remaining_market_cycles_supports_full_day_default() -> None:
    now = grpc_server.datetime(2026, 5, 27, 8, 30, tzinfo=grpc_server._KST)

    cycles = grpc_server._remaining_market_cycles(
        interval_sec=60,
        pa_cfg={
            "market_open_time": "09:00",
            "market_close_time": "15:30",
        },
        now=now,
    )

    assert cycles == 390


def test_market_start_guard_blocks_preopen_start() -> None:
    now = grpc_server.datetime(2026, 5, 27, 8, 30, tzinfo=grpc_server._KST)

    guard = grpc_server._market_start_guard(
        pa_cfg={
            "market_open_time": "09:00",
            "market_close_time": "15:30",
        },
        now=now,
    )

    assert guard["status"] == "BLOCKED"
    assert guard["reason"] == "market_not_open"


def test_remaining_market_cycles_blocks_after_close() -> None:
    now = grpc_server.datetime(2026, 5, 27, 15, 31, tzinfo=grpc_server._KST)

    cycles = grpc_server._remaining_market_cycles(
        interval_sec=60,
        pa_cfg={
            "market_open_time": "09:00",
            "market_close_time": "15:30",
        },
        now=now,
    )

    assert cycles == 0


def test_market_time_config_rejects_malformed_value() -> None:
    try:
        grpc_server._remaining_market_cycles(
            interval_sec=60,
            pa_cfg={
                "market_open_time": "bad-time",
                "market_close_time": "15:30",
            },
            now=grpc_server.datetime(2026, 5, 27, 8, 30, tzinfo=grpc_server._KST),
        )
    except ValueError as e:
        assert "invalid HH:MM" in str(e)
    else:
        raise AssertionError("malformed market time must fail closed")
