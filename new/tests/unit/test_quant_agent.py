"""S1-1 QuantAgent unit tests.

커버:
  - 초기화 (passive mode vs with model)
  - on_bar (BarBuffer 저장, 필드 검증)
  - score_cross_section (active/passive/warmup 3 경로, latency SLA)
  - compute_features (feat_ prefix 4 피처, warmup 부족 시 None)
  - detect_anomalies (intraday drop z-score)
  - latency_percentiles
  - report C5 (허용 3종 vs invalid)
  - reload_model
"""
from __future__ import annotations

import pickle
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from src.agents.hot.quant import QuantAgent
from src.data.bar_buffer import BarBuffer
from src.models.registry import ModelRegistry


# ====================================================================== #
# Fixtures
# ====================================================================== #


class MockBooster:
    """LightGBM Booster 대체. predict(X) 호출 시 주입된 scores 반환."""

    def __init__(self, scores=None):
        self._scores = scores
        self.predict_calls = 0
        self.last_X = None

    def predict(self, X):
        self.predict_calls += 1
        self.last_X = np.asarray(X, dtype=float)
        n = len(X)
        if self._scores is None:
            return np.linspace(0.1, 0.9, n).astype(float)
        if callable(self._scores):
            return self._scores(X)
        return np.asarray(self._scores[:n], dtype=float)


class MockTradeClassifier:
    """Trade/no-trade classifier sidecar 대체."""

    def __init__(self, probs=None):
        self._probs = probs
        self.predict_calls = 0
        self.last_X = None

    def predict(self, X):
        self.predict_calls += 1
        self.last_X = np.asarray(X, dtype=float)
        n = len(X)
        if self._probs is None:
            return np.full(n, 0.5, dtype=float)
        return np.asarray(self._probs[:n], dtype=float)


def _make_bars(
    ticker: str,
    n: int = 65,
    start_price: float = 50000.0,
    seed: int = 0,
    drift: float = 0.0,
) -> list[dict[str, Any]]:
    """합성 1분봉. warmup 이상 길이 기본 65."""
    rng = np.random.default_rng(seed)
    bars = []
    price = start_price
    for i in range(n):
        delta = rng.normal(drift, 50)
        price = max(1.0, price + float(delta))
        hour = 9 + (i // 60)
        minute = i % 60
        bars.append({
            "ticker": ticker,
            "ts_close": f"2026-04-20T{hour:02d}:{minute:02d}:00+09:00",
            "open": price,
            "high": price + 10.0,
            "low": price - 10.0,
            "close": price,
            "volume": 1000.0,
        })
    return bars


@pytest.fixture
def empty_registry(tmp_path: Path) -> ModelRegistry:
    """모델 미저장 registry."""
    return ModelRegistry(artifacts_dir=tmp_path / "lgbm")


@pytest.fixture
def populated_registry(tmp_path: Path) -> ModelRegistry:
    """Mock booster가 baseline으로 저장된 registry."""
    reg = ModelRegistry(artifacts_dir=tmp_path / "lgbm")
    mock = MockBooster(scores=None)   # default linspace
    metadata = {
        "version": "baseline",
        "bundle_id": None,
        "train_start": "2026-01-01",
        "train_end": "2026-04-19",
        "feature_cols": [
            "feat_1m_close_robust_z",
            "feat_5m_ret",
            "feat_30m_vol",
            "feat_60m_trend",
        ],
        "label_horizon_bars": 5,
        "target_col": "label_5m_ret",
        "label_generation_version": "session_local_v2",
        "label_session_scope": "ticker_trading_day",
        "metrics": {"ic": 0.01, "icir": 0.5, "rank_ic": 0.012,
                    "arr": 0.1, "ir": 1.0, "mdd": -0.08, "sr": 1.0},
        "data_version": "v1",
    }
    reg.save(mock, metadata, is_latest=True)
    return reg


@pytest.fixture
def populated_dual_source_registry(tmp_path: Path) -> ModelRegistry:
    """Dual-Source 5피처까지 요구하는 Mock booster registry."""
    reg = ModelRegistry(artifacts_dir=tmp_path / "lgbm_ds")
    mock = MockBooster(scores=None)
    feature_cols = [
        "feat_1m_close_robust_z",
        "feat_5m_ret",
        "feat_30m_vol",
        "feat_60m_trend",
        "news_score_t",
        "comm_score_t_1",
        "comm_score_t_2",
        "news_comm_divergence",
        "community_noise_multiplier",
    ]
    metadata = {
        "version": "dual-source-v1",
        "bundle_id": None,
        "train_start": "20260101",
        "train_end": "20260419",
        "feature_cols": feature_cols,
        "label_horizon_bars": 5,
        "target_col": "label_5m_ret",
        "label_generation_version": "session_local_v2",
        "label_session_scope": "ticker_trading_day",
        "metrics": {"ic": 0.01, "icir": 0.5, "rank_ic": 0.012,
                    "arr": 0.1, "ir": 1.0, "mdd": -0.08, "sr": 1.0},
        "data_version": "v1",
    }
    reg.save(mock, metadata, is_latest=True)
    return reg


@pytest.fixture
def agent_passive(empty_registry: ModelRegistry) -> QuantAgent:
    return QuantAgent(registry=empty_registry, bar_buffer=BarBuffer())


@pytest.fixture
def agent_active(populated_registry: ModelRegistry) -> QuantAgent:
    return QuantAgent(registry=populated_registry, bar_buffer=BarBuffer())


# ====================================================================== #
# 1. 초기화
# ====================================================================== #


def test_init_passive_mode_no_model(agent_passive: QuantAgent) -> None:
    assert agent_passive.has_model is False
    assert agent_passive.model_metadata is None


def test_init_active_mode_loads_model(agent_active: QuantAgent) -> None:
    assert agent_active.has_model is True
    assert agent_active.model_metadata is not None
    assert agent_active.model_metadata["version"] == "baseline"
    assert agent_active._inference_feature_cols == agent_active.model_metadata["feature_cols"]


def test_init_config_values(agent_passive: QuantAgent) -> None:
    assert agent_passive._warmup_bars == 60
    assert agent_passive._anomaly_zscore_threshold == 3.0
    assert agent_passive._latency_window == 1000
    assert agent_passive._investor_flow_stale_sec == 1800
    assert agent_passive._multi_scale_windows == [1, 5, 30, 60]
    assert agent_passive._feature_cols == [
        "feat_1m_close_robust_z",
        "feat_5m_ret",
        "feat_30m_vol",
        "feat_60m_trend",
        "news_score_t",
        "comm_score_t_1",
        "comm_score_t_2",
        "news_comm_divergence",
        "community_noise_multiplier",
        "us_sp500_change",
        "us_nasdaq_change",
        "us_vix",
        "us_soxx_change",
        "foreign_net_buy",
        "institutional_net_buy",
        "retail_net_buy",
        "interest_rate",
        "usd_krw",
    ]


# ====================================================================== #
# 2. on_bar
# ====================================================================== #


def test_on_bar_appends_to_buffer(agent_passive: QuantAgent) -> None:
    bar = {
        "ticker": "005930",
        "ts_close": "2026-04-20T09:00:00+09:00",
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000.0,
    }
    agent_passive.on_bar(bar)
    latest = agent_passive._bar_buffer.get_latest("005930", n=10)
    assert len(latest) == 1
    assert latest[0]["close"] == 100.5


def test_on_bar_missing_field_raises(agent_passive: QuantAgent) -> None:
    bar = {"ticker": "005930", "open": 100.0}   # 대부분 누락
    with pytest.raises(ValueError, match="필수 필드 누락"):
        agent_passive.on_bar(bar)


def test_on_bar_pads_ticker(agent_passive: QuantAgent) -> None:
    bar = {
        "ticker": "5930",   # pad_ticker가 "005930"으로 전환
        "ts_close": "2026-04-20T09:00:00+09:00",
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0,
    }
    agent_passive.on_bar(bar)
    # 6자리 padding 된 ticker로 저장됨
    assert "005930" in agent_passive._bar_buffer.tickers


def test_investor_flow_snapshot_tracks_age_and_stale(agent_passive: QuantAgent) -> None:
    """수급 snapshot은 모델 feature가 아니라 side-channel context로 보관된다."""
    agent_passive.update_investor_flow_snapshot(
        "5930",
        {
            "foreign_net_buy": -2538322,
            "institutional_net_buy": 285970,
            "retail_net_buy": 2200059,
            "provider": "kis_investor_trade_by_stock_daily",
        },
        received_at="2026-04-20T10:00:00+09:00",
    )

    fresh = agent_passive.get_investor_flow_snapshot(
        "005930",
        asof="2026-04-20T10:20:00+09:00",
    )
    stale = agent_passive.get_investor_flow_snapshot(
        "005930",
        asof="2026-04-20T10:31:00+09:00",
    )

    assert fresh is not None
    assert fresh["ticker"] == "005930"
    assert fresh["foreign_net_buy"] == pytest.approx(-2538322.0)
    assert fresh["age_sec"] == pytest.approx(1200.0)
    assert fresh["is_stale"] is False
    assert stale is not None
    assert stale["is_stale"] is True


def test_update_foreign_snapshot_alias(agent_passive: QuantAgent) -> None:
    """예경님 코드 호환 alias는 새 수급 side-channel로 연결된다."""
    agent_passive.update_foreign_snapshot(
        "660",
        -123.0,
        received_at=datetime.fromisoformat("2026-04-20T10:00:00+09:00"),
    )

    snap = agent_passive.get_investor_flow_snapshot(
        "000660",
        asof="2026-04-20T10:00:10+09:00",
    )

    assert snap is not None
    assert snap["foreign_net_buy"] == pytest.approx(-123.0)
    assert snap["provider"] == "foreign_snapshot"
    assert snap["age_sec"] == pytest.approx(10.0)


# ====================================================================== #
# 3. score_cross_section
# ====================================================================== #


def test_score_cross_section_passive_no_model(agent_passive: QuantAgent) -> None:
    for bar in _make_bars("005930", n=65):
        agent_passive.on_bar(bar)
    result = agent_passive.score_cross_section(["005930"], asof="2026-04-20T10:00:00+09:00")
    assert result["mode"] == "passive"
    assert result["scores"] == {}
    assert result["n_tickers"] == 0
    assert "latency_ms" in result


def test_score_cross_section_warmup_insufficient(agent_active: QuantAgent) -> None:
    # 60 bars 미만 주입
    for bar in _make_bars("005930", n=30):
        agent_active.on_bar(bar)
    result = agent_active.score_cross_section(["005930"], asof="2026-04-20T10:00:00+09:00")
    assert result["mode"] == "warmup"
    assert result["n_tickers"] == 0


def test_score_cross_section_happy_path(agent_active: QuantAgent) -> None:
    tickers = ["005930", "000660", "035420", "051910"]
    for t in tickers:
        for bar in _make_bars(t, n=65, start_price=50000 + int(t) % 1000, seed=int(t[-1])):
            agent_active.on_bar(bar)
    result = agent_active.score_cross_section(tickers, asof="2026-04-20T10:04:00+09:00")
    assert result["mode"] == "active"
    assert result["n_tickers"] == 4
    assert set(result["scores"].keys()) == set(tickers)
    for s in result["scores"].values():
        assert isinstance(s, float)
    assert result["latency_ms"] > 0


def test_score_cross_section_adds_trade_probs_from_classifier(tmp_path: Path) -> None:
    reg = ModelRegistry(artifacts_dir=tmp_path / "lgbm_trade")
    classifier_path = reg.base_dir / "baseline_trade_classifier.pkl"
    with classifier_path.open("wb") as fh:
        pickle.dump(MockTradeClassifier(probs=[0.2, 0.8]), fh)
    reg.save(
        MockBooster(scores=[0.1, 0.9]),
        {
            "version": "baseline",
            "bundle_id": None,
            "train_start": "2026-01-01",
            "train_end": "2026-04-19",
            "feature_cols": [
                "feat_1m_close_robust_z",
                "feat_5m_ret",
                "feat_30m_vol",
                "feat_60m_trend",
            ],
            "label_horizon_bars": 5,
            "target_col": "label_5m_ret",
            "label_generation_version": "session_local_v2",
            "label_session_scope": "ticker_trading_day",
            "metrics": {},
            "data_version": "v1",
            "trade_no_trade_classifier": {
                "status": "PASS",
                "model_path": str(classifier_path),
                "tradeable_col": "label_5m_tradeable",
            },
        },
        is_latest=True,
    )
    agent = QuantAgent(registry=reg, bar_buffer=BarBuffer())
    tickers = ["005930", "000660"]
    for ticker in tickers:
        for bar in _make_bars(ticker, n=65):
            agent.on_bar(bar)

    result = agent.score_cross_section(tickers, asof="2026-04-20T10:04:00+09:00")

    assert result["mode"] == "active"
    assert result["trade_probs"] == {
        "005930": pytest.approx(0.2),
        "000660": pytest.approx(0.8),
    }


def test_score_cross_section_mixed_warmup(agent_active: QuantAgent) -> None:
    # 일부 ticker만 warmup 달성
    for bar in _make_bars("005930", n=65):
        agent_active.on_bar(bar)
    for bar in _make_bars("000660", n=30):   # 부족
        agent_active.on_bar(bar)
    result = agent_active.score_cross_section(
        ["005930", "000660"], asof="2026-04-20T10:04:00+09:00"
    )
    assert result["n_tickers"] == 1
    assert "005930" in result["scores"]
    assert "000660" not in result["scores"]


def test_score_cross_section_excludes_future_buffered_bars(
    populated_registry: ModelRegistry,
) -> None:
    """이미 buffer에 들어간 미래 bar도 asof 이후면 추론 feature에서 제외한다."""
    agent_active = QuantAgent(
        registry=populated_registry,
        bar_buffer=BarBuffer(max_bars=120),
    )
    bars = _make_bars("005930", n=65, seed=11)
    for bar in bars:
        agent_active.on_bar(bar)
    future_bar = dict(bars[-1])
    future_bar["ts_close"] = "2026-04-20T10:05:00+09:00"
    future_bar["close"] = future_bar["close"] * 10.0
    agent_active.on_bar(future_bar)

    result = agent_active.score_cross_section(
        ["005930"],
        asof="2026-04-20T10:04:00+09:00",
    )
    expected = agent_active._compute_features(bars)

    assert result["mode"] == "active"
    assert expected is not None
    assert agent_active._booster.last_X is not None
    assert agent_active._booster.last_X[0, 0] == pytest.approx(
        expected["feat_1m_close_robust_z"]
    )
    assert agent_active._booster.last_X[0, 0] < agent_active._outlier_cap_z


def test_score_cross_section_empty_asof_does_not_use_latest_buffer(
    agent_active: QuantAgent,
) -> None:
    """asof가 비어 있으면 buffer의 최신 bar를 암묵적으로 쓰지 않는다."""
    for bar in _make_bars("005930", n=65):
        agent_active.on_bar(bar)

    result = agent_active.score_cross_section(["005930"], asof="")

    assert result["mode"] == "blocked"
    assert result["blocker"] == "asof_required"
    assert result["scores"] == {}
    assert agent_active._booster.predict_calls == 0


def test_detect_anomalies_empty_asof_does_not_use_latest_buffer(
    agent_active: QuantAgent,
) -> None:
    """anomaly sidecar도 empty asof에서 최신 buffer fallback을 쓰지 않는다."""
    for bar in _make_bars("005930", n=65):
        agent_active.on_bar(bar)

    assert agent_active.detect_anomalies(["005930"], asof="") == []


def test_score_cross_section_latency_under_100ms(agent_active: QuantAgent) -> None:
    """Hot Path SLA: 단일 20종목 추론 p95 < 100ms 목표.

    Mock booster라 실제 LightGBM 부하 없음. 코드 오버헤드만 측정 (실측 ~5ms 예상).
    50ms threshold는 CI 환경 변동성 고려한 tight 상한.
    """
    tickers = [f"00593{i}" for i in range(10)]
    for t in tickers:
        for bar in _make_bars(t, n=65, seed=int(t[-1])):
            agent_active.on_bar(bar)
    result = agent_active.score_cross_section(tickers, asof="2026-04-20T10:04:00+09:00")
    # Mock inference + feature 계산은 50ms 이내 (실측 ~5ms)
    assert result["latency_ms"] < 50.0, f"latency={result['latency_ms']}ms"


def test_score_cross_section_uses_dual_source_scores(
    populated_dual_source_registry: ModelRegistry,
) -> None:
    """Dual-Source 모델은 serve 시 C3A 배치 점수를 feature vector에 주입한다."""
    loader_calls: list[str | None] = []

    def loader(date_str: str | None) -> list[dict[str, Any]]:
        loader_calls.append(date_str)
        return [{
            "ticker": "005930",
            "news_score_t": 0.7,
            "comm_score_t_1": 0.3,
            "comm_score_t_2": 0.1,
            "news_comm_divergence": 0.4,
            "community_noise_multiplier": 0.8,
        }]

    agent = QuantAgent(
        registry=populated_dual_source_registry,
        bar_buffer=BarBuffer(),
        dual_source_loader=loader,
    )
    for bar in _make_bars("005930", n=65):
        agent.on_bar(bar)

    result = agent.score_cross_section(["005930"], asof="2026-04-20T10:04:00+09:00")

    assert result["mode"] == "active"
    assert loader_calls == ["20260420"]
    assert agent._booster.last_X is not None
    ds_values = agent._booster.last_X[0, 4:9].tolist()
    assert ds_values == pytest.approx([0.7, 0.3, 0.1, 0.4, 0.8])


def test_score_cross_section_blocks_future_dual_source_scores(
    populated_dual_source_registry: ModelRegistry,
) -> None:
    """asof 이후 생성된 Dual-Source score는 Hot Path 추론에 섞지 않는다."""
    def loader(_date_str: str | None) -> list[dict[str, Any]]:
        return [{
            "ticker": "005930",
            "snapshot_ts": "2026-04-20T10:30:00+09:00",
            "generated_at": "2026-04-20T10:31:00+09:00",
            "news_score_t": 0.7,
            "comm_score_t_1": 0.3,
            "comm_score_t_2": 0.1,
            "news_comm_divergence": 0.4,
            "community_noise_multiplier": 0.8,
        }]

    agent = QuantAgent(
        registry=populated_dual_source_registry,
        bar_buffer=BarBuffer(),
        dual_source_loader=loader,
    )
    for bar in _make_bars("005930", n=65):
        agent.on_bar(bar)

    result = agent.score_cross_section(["005930"], asof="2026-04-20T10:04:00+09:00")

    assert result["mode"] == "warmup"
    assert result["scores"] == {}


def test_dual_source_cache_rechecks_asof_on_each_lookup(
    populated_dual_source_registry: ModelRegistry,
) -> None:
    """늦은 asof에서 채운 cache를 이른 asof가 재사용해도 PIT guard를 다시 적용한다."""
    loader_calls: list[str | None] = []

    def loader(date_str: str | None) -> list[dict[str, Any]]:
        loader_calls.append(date_str)
        return [{
            "ticker": "005930",
            "generated_at": "2026-04-20T10:30:00+09:00",
            "news_score_t": 0.7,
            "comm_score_t_1": 0.3,
            "comm_score_t_2": 0.1,
            "news_comm_divergence": 0.4,
            "community_noise_multiplier": 0.8,
        }]

    agent = QuantAgent(
        registry=populated_dual_source_registry,
        bar_buffer=BarBuffer(),
        dual_source_loader=loader,
    )
    for bar in _make_bars("005930", n=65):
        agent.on_bar(bar)

    later = agent.score_cross_section(["005930"], asof="2026-04-20T10:31:00+09:00")
    earlier = agent.score_cross_section(["005930"], asof="2026-04-20T10:04:00+09:00")

    assert later["mode"] == "active"
    assert earlier["mode"] == "warmup"
    assert earlier["scores"] == {}
    assert loader_calls == ["20260420"]


def test_score_cross_section_blocks_invalid_model_predictions(
    populated_dual_source_registry: ModelRegistry,
) -> None:
    """NaN/short model output은 Hot Path 예외 대신 fail-closed warmup으로 닫는다."""
    def loader(_date_str: str | None) -> list[dict[str, Any]]:
        return [{
            "ticker": "005930",
            "news_score_t": 0.7,
            "comm_score_t_1": 0.3,
            "comm_score_t_2": 0.1,
            "news_comm_divergence": 0.4,
            "community_noise_multiplier": 0.8,
        }]

    agent = QuantAgent(
        registry=populated_dual_source_registry,
        bar_buffer=BarBuffer(),
        dual_source_loader=loader,
    )
    agent._booster = MockBooster(scores=[np.nan])  # type: ignore[assignment]
    for bar in _make_bars("005930", n=65):
        agent.on_bar(bar)

    result = agent.score_cross_section(["005930"], asof="2026-04-20T10:04:00+09:00")

    assert result["mode"] == "warmup"
    assert result["scores"] == {}
    assert result["error"] == "model_prediction_invalid"


def test_score_cross_section_uses_investor_flow_features(tmp_path: Path) -> None:
    """수급 side-channel 피처가 LightGBM 추론 feature vector에 반영된다."""
    reg = ModelRegistry(artifacts_dir=tmp_path / "lgbm_flow")
    mock = MockBooster(scores=None)
    feature_cols = [
        "feat_1m_close_robust_z",
        "feat_5m_ret",
        "feat_30m_vol",
        "feat_60m_trend",
        "foreign_net_buy",
        "institutional_net_buy",
        "retail_net_buy",
    ]
    reg.save(
        mock,
        {
            "version": "flow-v1",
            "bundle_id": None,
            "train_start": "20260101",
            "train_end": "20260419",
            "feature_cols": feature_cols,
                "label_horizon_bars": 5,
                "target_col": "label_5m_ret",
                "label_generation_version": "session_local_v2",
            "label_session_scope": "ticker_trading_day",
            "metrics": {},
            "data_version": "v1",
        },
        is_latest=True,
    )
    agent = QuantAgent(registry=reg, bar_buffer=BarBuffer())
    for bar in _make_bars("005930", n=65):
        agent.on_bar(bar)
    agent.update_investor_flow_snapshot(
        "005930",
        {
            "foreign_net_buy": -2538322,
            "institutional_net_buy": 285970,
            "retail_net_buy": 2200059,
        },
        received_at="2026-04-20T10:00:00+09:00",
    )

    result = agent.score_cross_section(["005930"], asof="2026-04-20T10:04:00+09:00")

    assert result["mode"] == "active"
    assert agent._booster.last_X is not None
    flow_values = agent._booster.last_X[0, 4:7].tolist()
    assert flow_values == pytest.approx([-2538322.0, 285970.0, 2200059.0])


def test_score_cross_section_ignores_future_investor_flow(tmp_path: Path) -> None:
    """asof 이후 수신된 수급 snapshot은 PIT-safety상 추론 feature에 섞지 않는다."""
    reg = ModelRegistry(artifacts_dir=tmp_path / "lgbm_future_flow")
    mock = MockBooster(scores=None)
    feature_cols = [
        "feat_1m_close_robust_z",
        "feat_5m_ret",
        "feat_30m_vol",
        "feat_60m_trend",
        "foreign_net_buy",
    ]
    reg.save(
        mock,
        {
            "version": "future-flow-v1",
            "bundle_id": None,
            "train_start": "20260101",
            "train_end": "20260419",
            "feature_cols": feature_cols,
                "label_horizon_bars": 5,
                "target_col": "label_5m_ret",
                "label_generation_version": "session_local_v2",
            "label_session_scope": "ticker_trading_day",
            "metrics": {},
            "data_version": "v1",
        },
        is_latest=True,
    )
    agent = QuantAgent(registry=reg, bar_buffer=BarBuffer())
    for bar in _make_bars("005930", n=65):
        agent.on_bar(bar)
    agent.update_investor_flow_snapshot(
        "005930",
        {"foreign_net_buy": 999999},
        received_at="2026-04-20T10:05:00+09:00",
    )

    result = agent.score_cross_section(["005930"], asof="2026-04-20T10:04:00+09:00")

    assert result["mode"] == "active"
    assert agent.get_investor_flow_snapshot(
        "005930",
        asof="2026-04-20T10:04:00+09:00",
    )["is_future"] is True
    assert agent._booster.last_X[0, 4] == pytest.approx(0.0)


def test_score_cross_section_ignores_stale_exogenous_snapshot(tmp_path: Path) -> None:
    """received_at 메타가 있는 stale generic exogenous snapshot은 추론 입력에서 제외한다."""
    reg = ModelRegistry(artifacts_dir=tmp_path / "lgbm_stale_exog")
    mock = MockBooster(scores=None)
    feature_cols = [
        "feat_1m_close_robust_z",
        "feat_5m_ret",
        "feat_30m_vol",
        "feat_60m_trend",
        "us_sp500_change",
    ]
    reg.save(
        mock,
        {
            "version": "stale-exog-v1",
            "bundle_id": None,
            "train_start": "20260101",
            "train_end": "20260419",
            "feature_cols": feature_cols,
                "label_horizon_bars": 5,
                "target_col": "label_5m_ret",
                "label_generation_version": "session_local_v2",
            "label_session_scope": "ticker_trading_day",
            "metrics": {},
            "data_version": "v1",
        },
        is_latest=True,
    )
    agent = QuantAgent(registry=reg, bar_buffer=BarBuffer())
    for bar in _make_bars("005930", n=65):
        agent.on_bar(bar)
    agent.update_exogenous_snapshot(
        {"us_sp500_change": 0.99},
        received_at="2026-04-20T09:00:00+09:00",
        max_age_sec=60,
    )

    result = agent.score_cross_section(["005930"], asof="2026-04-20T10:30:00+09:00")

    assert result["mode"] == "active"
    assert agent._booster.last_X[0, 4] == pytest.approx(0.0)


def test_score_cross_section_ignores_exogenous_snapshot_without_provenance(
    tmp_path: Path,
) -> None:
    """received_at 메타 없는 generic exogenous snapshot은 neutral default로 대체한다."""
    reg = ModelRegistry(artifacts_dir=tmp_path / "lgbm_missing_exog_meta")
    mock = MockBooster(scores=None)
    feature_cols = [
        "feat_1m_close_robust_z",
        "feat_5m_ret",
        "feat_30m_vol",
        "feat_60m_trend",
        "us_sp500_change",
    ]
    reg.save(
        mock,
        {
            "version": "missing-exog-meta-v1",
            "bundle_id": None,
            "train_start": "20260101",
            "train_end": "20260419",
            "feature_cols": feature_cols,
            "label_horizon_bars": 5,
            "target_col": "label_5m_ret",
            "label_generation_version": "session_local_v2",
            "label_session_scope": "ticker_trading_day",
            "metrics": {},
            "data_version": "v1",
        },
        is_latest=True,
    )
    agent = QuantAgent(registry=reg, bar_buffer=BarBuffer())
    for bar in _make_bars("005930", n=65):
        agent.on_bar(bar)
    agent._market_exogenous_snapshot["us_sp500_change"] = 0.99

    result = agent.score_cross_section(["005930"], asof="2026-04-20T10:04:00+09:00")

    assert result["mode"] == "active"
    assert agent._booster.last_X[0, 4] == pytest.approx(0.0)


def test_score_cross_section_blocks_dual_source_model_when_scores_missing(
    populated_dual_source_registry: ModelRegistry,
) -> None:
    """Dual-Source feature가 필요한 모델은 점수 파일 누락 시 active 추론하지 않는다."""
    agent = QuantAgent(
        registry=populated_dual_source_registry,
        bar_buffer=BarBuffer(),
        dual_source_loader=lambda _date: [],
    )
    for bar in _make_bars("005930", n=65):
        agent.on_bar(bar)

    result = agent.score_cross_section(["005930"], asof="2026-04-20T10:04:00+09:00")

    assert result["mode"] == "warmup"
    assert result["scores"] == {}
    assert result["n_tickers"] == 0


def test_score_cross_section_default_does_not_load_dual_source_from_disk(
    populated_dual_source_registry: ModelRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hot Path 기본값은 Dual-Source artifact 파일을 직접 읽지 않는다."""
    calls: list[str | None] = []

    def fake_load_latest_scores(date_key: str | None = None) -> list[dict[str, Any]]:
        calls.append(date_key)
        return [{"ticker": "005930"}]

    monkeypatch.setattr(
        "src.data.dual_source_runner.load_latest_scores",
        fake_load_latest_scores,
    )
    agent = QuantAgent(
        registry=populated_dual_source_registry,
        bar_buffer=BarBuffer(),
        dual_source_loader=None,
    )
    for bar in _make_bars("005930", n=65):
        agent.on_bar(bar)

    result = agent.score_cross_section(["005930"], asof="2026-04-20T10:04:00+09:00")

    assert result["mode"] == "warmup"
    assert result["scores"] == {}
    assert calls == []


def test_score_cross_section_blocks_dual_source_model_when_loader_fails(
    populated_dual_source_registry: ModelRegistry,
) -> None:
    """Dual-Source loader 오류는 Hot Path 예외 대신 해당 ticker skip으로 처리한다."""
    def failing_loader(_date: str | None) -> list[dict[str, Any]]:
        raise RuntimeError("dual source store unavailable")

    agent = QuantAgent(
        registry=populated_dual_source_registry,
        bar_buffer=BarBuffer(),
        dual_source_loader=failing_loader,
    )
    for bar in _make_bars("005930", n=65):
        agent.on_bar(bar)

    result = agent.score_cross_section(["005930"], asof="2026-04-20T10:04:00+09:00")

    assert result["mode"] == "warmup"
    assert result["scores"] == {}


def test_score_cross_section_blocks_dual_source_model_when_values_invalid(
    populated_dual_source_registry: ModelRegistry,
) -> None:
    """Dual-Source artifact 값이 비수치면 Hot Path 예외 대신 해당 ticker skip."""
    def invalid_loader(_date: str | None) -> list[dict[str, Any]]:
        return [{
            "ticker": "005930",
            "news_score_t": "N/A",
            "comm_score_t_1": 0.3,
            "comm_score_t_2": 0.1,
            "news_comm_divergence": 0.4,
            "community_noise_multiplier": 0.8,
        }]

    agent = QuantAgent(
        registry=populated_dual_source_registry,
        bar_buffer=BarBuffer(),
        dual_source_loader=invalid_loader,
    )
    for bar in _make_bars("005930", n=65):
        agent.on_bar(bar)

    result = agent.score_cross_section(["005930"], asof="2026-04-20T10:04:00+09:00")

    assert result["mode"] == "warmup"
    assert result["scores"] == {}


# ====================================================================== #
# 4. compute_features
# ====================================================================== #


def test_compute_features_returns_feat_prefix(agent_active: QuantAgent) -> None:
    bars = _make_bars("005930", n=65)
    feats = agent_active._compute_features(bars)
    assert feats is not None
    assert set(feats.keys()) == {
        "feat_1m_close_robust_z",
        "feat_5m_ret",
        "feat_30m_vol",
        "feat_60m_trend",
        "news_score_t",
        "comm_score_t_1",
        "comm_score_t_2",
        "news_comm_divergence",
        "community_noise_multiplier",
        "us_sp500_change",
        "us_nasdaq_change",
        "us_vix",
        "us_soxx_change",
        "foreign_net_buy",
        "institutional_net_buy",
        "retail_net_buy",
        "interest_rate",
        "usd_krw",
    }
    for col in agent_active._dual_source_feature_cols:
        expected = 1.0 if col == "community_noise_multiplier" else 0.0
        assert feats[col] == pytest.approx(expected)
    for col in agent_active._exogenous_feature_cols:
        assert feats[col] == pytest.approx(0.0)
    for v in feats.values():
        assert isinstance(v, float)
        assert np.isfinite(v)


def test_compute_features_none_on_short_bars(agent_active: QuantAgent) -> None:
    bars = _make_bars("005930", n=30)
    assert agent_active._compute_features(bars) is None


def test_compute_features_z_clip_on_extreme(agent_active: QuantAgent) -> None:
    """마지막 close가 극단값이면 outlier_cap_z로 clip."""
    bars = _make_bars("005930", n=65, seed=42)
    # 마지막 bar close를 10x spike
    bars[-1]["close"] = bars[-2]["close"] * 10.0
    feats = agent_active._compute_features(bars)
    assert feats is not None
    assert feats["feat_1m_close_robust_z"] == pytest.approx(
        agent_active._outlier_cap_z, abs=1e-6,
    )


# ====================================================================== #
# 5. detect_anomalies
# ====================================================================== #


def test_detect_anomalies_intraday_drop(agent_active: QuantAgent) -> None:
    # 잔잔한 시세 → 마지막 bar만 큰 하락 (z-score 크게 음수)
    bars = _make_bars("005930", n=65, seed=1)
    for b in bars[:-1]:
        b["close"] = 50000.0    # 아예 plat
    bars[-1]["close"] = 50000.0
    # 마지막 하락 삽입: 50000 → 40000 (-20% 단일 bar, 극단)
    for bar in bars:
        agent_active.on_bar(bar)
    # 한 번 더 drop bar 추가 (flat history 끝나고 현재만 drop)
    drop_bar = dict(bars[-1])
    drop_bar["ts_close"] = "2026-04-20T10:05:00+09:00"
    drop_bar["close"] = 40000.0
    agent_active.on_bar(drop_bar)

    anomalies = agent_active.detect_anomalies(["005930"], asof="2026-04-20T10:05:00+09:00")
    # flat history면 std=0이라 early-return. drop이라도 anomaly 아닐 수 있음.
    # → history에 약간의 noise 포함된 케이스로 재검사 필요
    # 여기서는 flat history 케이스는 no-anomaly.
    assert len(anomalies) == 0   # flat → sigma 0 → return (False, 0)


def test_detect_anomalies_noisy_history_detects_drop(agent_active: QuantAgent) -> None:
    bars = _make_bars("005930", n=65, seed=7)
    for bar in bars:
        agent_active.on_bar(bar)
    # 다음 bar에 큰 drop (-10σ 수준)
    last_close = bars[-1]["close"]
    drop_bar = {
        "ticker": "005930",
        "ts_close": "2026-04-20T10:05:00+09:00",
        "open": last_close,
        "high": last_close,
        "low": last_close * 0.9,
        "close": last_close * 0.9,   # -10% drop
        "volume": 1000.0,
    }
    agent_active.on_bar(drop_bar)

    anomalies = agent_active.detect_anomalies(["005930"], asof="2026-04-20T10:05:00+09:00")
    assert len(anomalies) == 1
    assert anomalies[0]["ticker"] == "005930"
    assert anomalies[0]["anomaly_type"] == "intraday_drop"
    assert anomalies[0]["z_score"] < -3.0


def test_detect_anomalies_excludes_future_buffered_bars(
    populated_registry: ModelRegistry,
) -> None:
    """asof 이후 미래 급락 bar는 anomaly 판단에 쓰지 않는다."""
    agent_active = QuantAgent(
        registry=populated_registry,
        bar_buffer=BarBuffer(max_bars=120),
    )
    bars = _make_bars("005930", n=65, seed=7)
    for bar in bars:
        agent_active.on_bar(bar)
    future_drop = dict(bars[-1])
    future_drop["ts_close"] = "2026-04-20T10:05:00+09:00"
    future_drop["close"] = future_drop["close"] * 0.1
    agent_active.on_bar(future_drop)

    anomalies = agent_active.detect_anomalies(
        ["005930"],
        asof="2026-04-20T10:04:00+09:00",
    )

    assert anomalies == []


def test_detect_anomalies_warmup_insufficient(agent_active: QuantAgent) -> None:
    for bar in _make_bars("005930", n=30):
        agent_active.on_bar(bar)
    anomalies = agent_active.detect_anomalies(["005930"], asof="2026-04-20T09:29:00+09:00")
    assert anomalies == []


def test_detect_anomalies_noisy_volume_detects_spike(agent_active: QuantAgent) -> None:
    """noisy volume + config threshold 초과 spike → volume_spike anomaly 1건.

    spike value는 config volume_zscore_threshold + 2.0 margin 기반으로 동적 계산한다.
    config 값이 바뀌어도 테스트 robust. 가격은 변화 없게 두어 intraday_drop은 트리거 안 함.
    """
    threshold = agent_active._volume_zscore_threshold

    rng = np.random.default_rng(11)
    bars = _make_bars("005930", n=65, seed=11)
    for b in bars:
        b["volume"] = float(1000.0 + rng.normal(0, 100))
    for bar in bars:
        agent_active.on_bar(bar)

    hist_volumes = np.array([b["volume"] for b in bars])
    hist_mean = float(hist_volumes.mean())
    hist_std = float(hist_volumes.std(ddof=0))
    spike_volume = hist_mean + (threshold + 2.0) * hist_std

    last_close = bars[-1]["close"]
    spike_bar = {
        "ticker": "005930",
        "ts_close": "2026-04-20T10:05:00+09:00",
        "open": last_close,
        "high": last_close,
        "low": last_close,
        "close": last_close,
        "volume": spike_volume,
    }
    agent_active.on_bar(spike_bar)

    anomalies = agent_active.detect_anomalies(
        ["005930"], asof="2026-04-20T10:05:00+09:00",
    )
    assert len(anomalies) == 1
    assert anomalies[0]["ticker"] == "005930"
    assert anomalies[0]["anomaly_type"] == "volume_spike"
    assert anomalies[0]["z_score"] > threshold


# ====================================================================== #
# 6. latency_percentiles
# ====================================================================== #


def test_latency_percentiles_empty(agent_active: QuantAgent) -> None:
    p = agent_active.latency_percentiles()
    assert p["n"] == 0
    assert p["p50"] == 0.0


def test_latency_percentiles_records(agent_active: QuantAgent) -> None:
    tickers = ["005930", "000660", "035420", "051910"]
    for t in tickers:
        for bar in _make_bars(t, n=65):
            agent_active.on_bar(bar)
    # score 여러번 호출 → 레이턴시 기록
    for _ in range(5):
        agent_active.score_cross_section(tickers, asof="2026-04-20T10:04:00+09:00")

    p = agent_active.latency_percentiles()
    assert p["n"] == 5
    assert p["p50"] >= 0.0
    assert p["p95"] >= p["p50"]
    assert p["p99"] >= p["p95"]


# ====================================================================== #
# 7. report (C5)
# ====================================================================== #


def test_report_quant_signal(agent_passive: QuantAgent) -> None:
    r = agent_passive.report("quant_signal", {"ticker": "005930", "score": 0.7})
    assert r["report_type"] == "quant_signal"
    assert r["agent"] == "QuantAgent"
    assert r["payload"]["score"] == 0.7


def test_report_quant_alert(agent_passive: QuantAgent) -> None:
    r = agent_passive.report("quant_alert", {"reason": "latency_exceeded"})
    assert r["report_type"] == "quant_alert"


def test_report_anomaly_detected(agent_passive: QuantAgent) -> None:
    r = agent_passive.report("anomaly_detected", {"ticker": "005930", "z": -4.5})
    assert r["report_type"] == "anomaly_detected"


def test_report_invalid_type_raises(agent_passive: QuantAgent) -> None:
    with pytest.raises(ValueError, match="invalid report_type"):
        agent_passive.report("news_signal", {})   # News는 QuantAgent publishes 아님


# ====================================================================== #
# 8. reload_model
# ====================================================================== #


def test_reload_model_success(
    agent_passive: QuantAgent,
    populated_registry: ModelRegistry,
) -> None:
    # 시작 시 passive. 이후 registry에 모델 있는 상태로 교체 reload.
    agent_passive._registry = populated_registry
    ok = agent_passive.reload_model()
    assert ok is True
    assert agent_passive.has_model is True


def test_reload_model_failure_keeps_passive(
    agent_passive: QuantAgent,
    tmp_path: Path,
) -> None:
    empty = ModelRegistry(artifacts_dir=tmp_path / "empty_lgbm")
    agent_passive._registry = empty
    ok = agent_passive.reload_model()
    assert ok is False
    assert agent_passive.has_model is False
