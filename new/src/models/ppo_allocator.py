"""S1-2 PPO Allocator (Hot Path, C7 PPOAllocatorContract).

Sprint 1 MVP: heuristic policy (score-based softmax + Top-K + single-name cap).
Sprint 3 S3-7 야간 재학습에서 stable-baselines3 PPO policy로 교체 예정.

아키텍처 원칙:
  - 불변 원칙 2: target_weights 생성 단독 책임. FDA/Portfolio Manager가 수정 못 함.
  - 모든 수치는 risk_config.yaml position_limits/turnover_cap/regime_gate 경유 (하드코딩 금지)
  - Hot Path <100ms
  - 종목코드 pad_ticker 6자리

입력 포맷:
  QuantAgent.score_cross_section 반환 dict (flat {ticker: score}) 수용.
  C7 리스트 포맷 [{ticker, score, confidence}]도 수용 (형식 어댑터 내장).
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

from src.utils.config_loader import load as config_load
from src.utils.id_factory import generate_portfolio_patch_id
from src.utils.logger import get_logger
from src.utils.ticker_utils import pad_ticker

logger = get_logger("ppo_allocator")

_ARTIFACTS_PATH = Path(__file__).resolve().parents[3] / "artifacts" / "ppo"


class PolicyNotLoadedError(RuntimeError):
    """C7 error: POLICY_NOT_LOADED. stable-baselines3 policy 로드 실패."""


class ConstraintViolationError(ValueError):
    """C7 error: CONSTRAINT_VIOLATION. position_limits 위반."""


class InvalidWeightSumError(ValueError):
    """C7 error: INVALID_WEIGHT_SUM. target_weights 합 != expected."""


class PPOAllocator:
    """C7 PPOAllocatorContract 구현. quant_scores → target_weights.

    Sprint 1 MVP는 heuristic policy:
      1. cross-sectional confidence = softmax(scores)
      2. min_confidence 필터
      3. Top-K (max_names) 선택
      4. weights = score / sum(score) × (1 - min_cash)
      5. max_single_name cap
      6. regime_gate multiplier 적용
      7. cash_weight = 1 - sum(target_weights)

    Sprint 3 S3-7에서 PPO policy (stable-baselines3)로 교체. policy_version으로 구분.
    """

    def __init__(self, policy_path: Path | None = None) -> None:
        pos_cfg = config_load("risk_config.yaml", "position_limits")
        turnover_cfg = config_load("risk_config.yaml", "turnover_cap")
        regime_cfg = config_load("risk_config.yaml", "regime_gate")

        self._max_names: int = int(pos_cfg["max_names"])
        self._max_single_name: float = float(pos_cfg["max_single_name"])
        self._max_sector: float = float(pos_cfg["max_sector"])
        self._min_cash: float = float(pos_cfg["min_cash"])
        self._min_confidence: float = float(pos_cfg["min_confidence"])

        self._daily_turnover_max: float = float(turnover_cfg["daily_max"])
        self._regime_actions: dict[str, Any] = dict(regime_cfg["actions"])

        # policy_path 지정 시 stable-baselines3 로드 시도 (Sprint 3+)
        self._policy: Any = None
        self._policy_version: str = "heuristic_v1"
        if policy_path is not None:
            self._load_policy(policy_path)

        logger.info(
            "[ppo_allocator] 초기화: policy=%s, max_names=%d, "
            "max_single=%.2f, min_cash=%.2f, min_conf=%.2f",
            self._policy_version, self._max_names,
            self._max_single_name, self._min_cash, self._min_confidence,
        )

    # ================================================================== #
    # Public API
    # ================================================================== #

    @property
    def policy_version(self) -> str:
        return self._policy_version

    def allocate(
        self,
        quant_output: dict[str, Any] | list[dict[str, Any]],
        current_positions: list[dict[str, Any]] | None = None,
        market_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """quant_output → C7 allocation_plan.

        Args:
            quant_output: QuantAgent.score_cross_section 반환 dict 또는
                          C7 list 포맷 [{ticker, score, confidence}].
            current_positions: 현재 보유. [{ticker, qty, weight}] 선택.
            market_state: {cash_ratio, sector_exposure, regime_state}. 선택.

        Returns: C7 output + metadata (portfolio_patch_id, rejected, method).
        """
        # 1. 입력 정규화 → {ticker: score} dict
        scores = self._normalize_scores(quant_output)
        if not scores:
            return self._empty_allocation(quant_output)

        # PPO inference path: policy 로드 완료 시 PPO로 배분
        if self._policy is not None:
            return self._allocate_ppo(scores, current_positions, market_state, quant_output)

        # 2. Cross-sectional confidence (softmax 기반, heuristic_v1)
        confidence = self._compute_confidence(scores)

        # 3. min_confidence 필터
        filtered, rejected = self._apply_min_confidence(scores, confidence)
        if not filtered:
            return self._empty_allocation(
                quant_output,
                rejected=rejected,
                reason="all_below_min_confidence",
            )

        # 4. Top-K 선택
        top_k_items, rejected_rank = self._select_top_k(filtered)
        rejected.extend(rejected_rank)

        # 5. Weights 산출
        raw_weights = self._compute_weights(top_k_items)

        # 6. max_single_name cap
        capped_weights = self._apply_max_single_cap(raw_weights)

        # 7. regime_gate multiplier
        regime_multiplier = self._resolve_regime_multiplier(market_state)
        scaled_weights = {
            t: float(w * regime_multiplier) for t, w in capped_weights.items()
        }

        # 8. cash_weight
        total_weight = float(sum(scaled_weights.values()))
        cash_weight = max(float(self._min_cash), 1.0 - total_weight)
        # target_weights 합 + cash_weight > 1 이면 비례 축소
        if total_weight + cash_weight > 1.0:
            # min_cash 보장 + 나머지 재정규화
            target_sum = 1.0 - self._min_cash
            if total_weight > target_sum > 1e-12:
                shrink = target_sum / total_weight
                scaled_weights = {t: w * shrink for t, w in scaled_weights.items()}
                total_weight = float(sum(scaled_weights.values()))
            cash_weight = 1.0 - total_weight

        return self._build_output(
            target_weights=scaled_weights,
            cash_weight=cash_weight,
            total_weight=total_weight,
            rejected=rejected,
            quant_output=quant_output,
            regime_multiplier=regime_multiplier,
        )

    # ================================================================== #
    # Internal: 입력 정규화
    # ================================================================== #

    @staticmethod
    def _normalize_scores(
        quant_output: dict[str, Any] | list[dict[str, Any]],
    ) -> dict[str, float]:
        """여러 입력 포맷 지원 → {padded_ticker: score}."""
        scores: dict[str, float] = {}

        if isinstance(quant_output, list):
            for item in quant_output:
                if not isinstance(item, dict) or "ticker" not in item:
                    continue
                t = pad_ticker(str(item["ticker"]))
                s = float(item.get("score", 0.0))
                scores[t] = s
            return scores

        if isinstance(quant_output, dict):
            # QuantAgent flat {tickers, scores, ...}
            raw_scores = quant_output.get("scores")
            if isinstance(raw_scores, dict):
                for t, s in raw_scores.items():
                    if s is None:
                        continue
                    scores[pad_ticker(str(t))] = float(s)
                return scores
            # 또는 직접 {ticker: score} 형태
            for t, s in quant_output.items():
                if isinstance(s, (int, float)) and not isinstance(s, bool):
                    scores[pad_ticker(str(t))] = float(s)
            return scores

        return scores

    # ================================================================== #
    # Internal: confidence / filter / top-k / weights
    # ================================================================== #

    @staticmethod
    def _compute_confidence(scores: dict[str, float]) -> dict[str, float]:
        """Softmax 기반 cross-sectional confidence. 합=1."""
        if not scores:
            return {}
        tickers = list(scores.keys())
        vals = np.array([scores[t] for t in tickers], dtype=float)
        # numerical stable softmax
        vmax = float(np.max(vals))
        exp = np.exp(vals - vmax)
        total = float(exp.sum())
        if total < 1e-12:
            uniform = 1.0 / len(tickers)
            return {t: uniform for t in tickers}
        probs = exp / total
        return {t: float(p) for t, p in zip(tickers, probs)}

    def _apply_min_confidence(
        self,
        scores: dict[str, float],
        confidence: dict[str, float],
    ) -> tuple[dict[str, float], list[dict[str, Any]]]:
        """confidence < min_confidence 제외. rejected 리스트 반환.

        Note: softmax confidence는 20종목 기준 평균 0.05. min_confidence=0.30은
        2~4종목만 통과하는 tight threshold. 필요 시 config 조정 권장.
        """
        filtered: dict[str, float] = {}
        rejected: list[dict[str, Any]] = []
        for t, s in scores.items():
            c = confidence.get(t, 0.0)
            if c < self._min_confidence:
                rejected.append({
                    "ticker": t,
                    "reason": "below_min_confidence",
                    "confidence": c,
                })
            else:
                filtered[t] = s
        return filtered, rejected

    def _select_top_k(
        self,
        scores: dict[str, float],
    ) -> tuple[list[tuple[str, float]], list[dict[str, Any]]]:
        """score 내림차순 정렬 후 Top-K (max_names) 선택."""
        sorted_items = sorted(scores.items(), key=lambda x: -x[1])
        top_k = sorted_items[: self._max_names]
        rejected = [
            {"ticker": t, "reason": "below_topk", "score": s}
            for t, s in sorted_items[self._max_names:]
        ]
        return top_k, rejected

    def _compute_weights(
        self,
        top_k_items: list[tuple[str, float]],
    ) -> dict[str, float]:
        """score 기반 weight 산출. 합 = 1 - min_cash."""
        if not top_k_items:
            return {}
        target_alloc = max(0.0, 1.0 - self._min_cash)
        vals = np.array([max(0.0, s) for _, s in top_k_items], dtype=float)
        total = float(vals.sum())
        if total < 1e-12:
            # 전부 0 또는 음수 → 균등 비중
            uniform = target_alloc / len(top_k_items)
            return {t: uniform for t, _ in top_k_items}
        weights = vals / total * target_alloc
        return {t: float(w) for (t, _), w in zip(top_k_items, weights)}

    def _apply_max_single_cap(self, weights: dict[str, float]) -> dict[str, float]:
        capped = dict(weights)
        
        for _ in range(len(capped)):  # 최대 종목 수만큼 반복
            overflow = 0.0
            for t, w in capped.items():
                if w > self._max_single_name:
                    overflow += w - self._max_single_name
                    capped[t] = self._max_single_name
            
            if overflow < 1e-9:
                break
                
            free_tickers = [
                t for t in capped if capped[t] < self._max_single_name - 1e-9
            ]
            if not free_tickers:
                break  # 모든 종목이 cap → overflow 소진 불가
                
            add = overflow / len(free_tickers)
            for t in free_tickers:
                capped[t] = min(self._max_single_name, capped[t] + add)
        
        return capped

    def _resolve_regime_multiplier(
        self,
        market_state: dict[str, Any] | None,
    ) -> float:
        """market_state.regime_state → new_entry_weight_multiplier."""
        if not market_state:
            return 1.0
        regime = market_state.get("regime_state")
        if not regime:
            return 1.0
        regime = str(regime).lower()
        actions = self._regime_actions.get(regime)
        if not isinstance(actions, dict):
            return 1.0
        return float(actions.get("new_entry_weight_multiplier", 1.0))

    # ================================================================== #
    # Internal: Output 구성
    # ================================================================== #

    def _build_output(
        self,
        target_weights: dict[str, float],
        cash_weight: float,
        total_weight: float,
        rejected: list[dict[str, Any]],
        quant_output: dict | list,
        regime_multiplier: float,
    ) -> dict[str, Any]:
        """C7 output 구조 + metadata."""
        ts = self._extract_ts(quant_output)
        pp_id = generate_portfolio_patch_id()

        # 검증: sum(target_weights) + cash_weight ≈ 1.0 (± tolerance)
        summed = float(sum(target_weights.values())) + cash_weight
        if not math.isclose(summed, 1.0, abs_tol=1e-6):
            raise InvalidWeightSumError(
                f"sum(target_weights) + cash = {summed:.6f}, expected ~ 1.0"
            )

        return {
            "allocation_plan": {
                "target_weights": target_weights,
                "cash_weight": cash_weight,
                "policy_version": self._policy_version,
                "constraints_applied": {
                    "max_names": self._max_names,
                    "max_single_name": self._max_single_name,
                    "max_sector": self._max_sector,
                    "min_cash": self._min_cash,
                },
            },
            "metadata": {
                "portfolio_patch_id": pp_id,
                "ts": ts,
                "method": self._policy_version,
                "n_holdings": len(target_weights),
                "n_rejected": len(rejected),
                "rejected": rejected,
                "total_weight": total_weight,
                "regime_multiplier": regime_multiplier,
            },
        }

    def _empty_allocation(
        self,
        quant_output: dict | list,
        rejected: list[dict[str, Any]] | None = None,
        reason: str = "empty_input",
    ) -> dict[str, Any]:
        ts = self._extract_ts(quant_output)
        pp_id = generate_portfolio_patch_id()
        return {
            "allocation_plan": {
                "target_weights": {},
                "cash_weight": 1.0,
                "policy_version": self._policy_version,
                "constraints_applied": {
                    "max_names": self._max_names,
                    "max_single_name": self._max_single_name,
                    "max_sector": self._max_sector,
                    "min_cash": self._min_cash,
                },
            },
            "metadata": {
                "portfolio_patch_id": pp_id,
                "ts": ts,
                "method": self._policy_version,
                "n_holdings": 0,
                "n_rejected": len(rejected) if rejected else 0,
                "rejected": rejected or [],
                "total_weight": 0.0,
                "reason": reason,
            },
        }

    @staticmethod
    def _extract_ts(quant_output: dict | list) -> str:
        if isinstance(quant_output, dict):
            ts = quant_output.get("ts")
            if ts is not None:
                return str(ts)
        return ""

    # ================================================================== #
    # PPO inference (policy loaded 시 allocate에서 분기)
    # ================================================================== #

    def _allocate_ppo(
        self,
        scores: dict[str, float],
        current_positions: list[dict] | None,
        market_state: dict | None,
        quant_output: dict | list,
    ) -> dict[str, Any]:
        """stable-baselines3 PPO policy로 target_weights 산출.

        obs = [normalized_scores(n_stocks), current_weights(n_stocks)]
        action → softmax → target_weights → constraints 적용.
        """
        tickers = list(scores.keys())
        n = len(tickers)
        n_stocks: int = getattr(self, "_policy_n_stocks", 20)
        if n != n_stocks:
            rejected = [
                {
                    "ticker": ticker,
                    "reason": "ppo_policy_universe_mismatch",
                    "policy_n_stocks": n_stocks,
                    "input_n_stocks": n,
                }
                for ticker in tickers
            ]
            logger.warning(
                "[ppo_allocator] PPO policy universe mismatch. input=%d policy=%d",
                n,
                n_stocks,
            )
            return self._empty_allocation(
                quant_output,
                rejected=rejected,
                reason="ppo_policy_universe_mismatch",
            )

        # scores 벡터 구성 (n_stocks 길이에 맞게 패드/자름)
        score_vals = np.array([scores[t] for t in tickers], dtype=np.float32)
        score_buf = np.zeros(n_stocks, dtype=np.float32)
        take = n
        score_buf[:take] = score_vals[:take]

        # 정규화 scores → [-1, 1]
        s_min, s_max = float(score_buf.min()), float(score_buf.max())
        s_range = s_max - s_min
        if s_range > 1e-8:
            norm_scores = (2.0 * (score_buf - s_min) / s_range - 1.0)
        else:
            norm_scores = np.zeros(n_stocks, dtype=np.float32)

        # current_positions → weights 벡터
        pos_map: dict[str, float] = {}
        for p in (current_positions or []):
            if isinstance(p, dict) and "ticker" in p:
                pos_map[pad_ticker(str(p["ticker"]))] = float(p.get("weight", 0.0))
        curr_weights = np.array(
            [pos_map.get(t, 0.0) for t in tickers[:take]], dtype=np.float32
        )
        weight_buf = np.zeros(n_stocks, dtype=np.float32)
        weight_buf[:take] = curr_weights

        obs = np.concatenate([norm_scores, weight_buf]).astype(np.float32)

        # PPO 추론
        action, _ = self._policy.predict(obs, deterministic=True)

        # action[:take] → softmax → target weights
        a = np.array(action[:take], dtype=np.float64)
        a = a - a.max()
        exp_a = np.exp(a)
        target_alloc = max(0.0, 1.0 - self._min_cash)
        soft = (exp_a / exp_a.sum() * target_alloc).astype(np.float64)

        target_weights: dict[str, float] = {}
        for i in range(take):
            t = tickers[i]
            w = float(soft[i])
            if w > 1e-8:
                target_weights[t] = w

        # constraints (기존 heuristic 경로와 동일)
        capped_weights = self._apply_max_single_cap(target_weights)
        regime_multiplier = self._resolve_regime_multiplier(market_state)
        scaled_weights = {t: float(w * regime_multiplier) for t, w in capped_weights.items()}

        total_weight = float(sum(scaled_weights.values()))
        cash_weight = max(float(self._min_cash), 1.0 - total_weight)
        if total_weight + cash_weight > 1.0:
            target_sum = 1.0 - self._min_cash
            if total_weight > target_sum > 1e-12:
                shrink = target_sum / total_weight
                scaled_weights = {t: w * shrink for t, w in scaled_weights.items()}
                total_weight = float(sum(scaled_weights.values()))
            cash_weight = 1.0 - total_weight

        return self._build_output(
            target_weights=scaled_weights,
            cash_weight=cash_weight,
            total_weight=total_weight,
            rejected=[],
            quant_output=quant_output,
            regime_multiplier=regime_multiplier,
        )

    # ================================================================== #
    # Policy 로드 (S3-7 실구현)
    # ================================================================== #

    def _load_policy(self, path: Path) -> None:
        """stable-baselines3 PPO policy 로드. artifacts/ppo/v{n}.zip."""
        path = Path(path)
        if not path.exists():
            raise PolicyNotLoadedError(f"policy 파일 없음: {path}")
        try:
            from stable_baselines3 import PPO  # noqa
        except ImportError as e:
            raise PolicyNotLoadedError(
                f"stable-baselines3 미설치. {e}"
            ) from e
        try:
            self._policy = PPO.load(str(path))
            # policy_n_stocks = obs_dim / 2 (obs = [scores, weights] 각 n_stocks)
            obs_dim = self._policy.observation_space.shape[0]
            self._policy_n_stocks: int = obs_dim // 2
            # version: v2.zip → stem = "v2" → label = "ppo_v2"
            stem = path.stem  # .zip 제거
            self._policy_version = f"ppo_{stem}"
            logger.info(
                "[ppo_allocator] PPO policy 로드 완료: version=%s n_stocks=%d",
                self._policy_version,
                self._policy_n_stocks,
            )
        except Exception as e:
            raise PolicyNotLoadedError(f"PPO policy 로드 실패: {e}") from e

    def load(self) -> None:
        """레거시 공개 API 호환: 최신 PPO zip이 있으면 로드, 없으면 heuristic 유지."""
        candidates = sorted(
            _ARTIFACTS_PATH.glob("v*.zip"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            self._policy = None
            self._policy_version = "heuristic_v1"
            logger.warning(
                "[ppo_allocator] PPO policy artifact 없음. heuristic_v1 유지: %s",
                _ARTIFACTS_PATH,
            )
            return
        try:
            self._load_policy(candidates[0])
        except PolicyNotLoadedError as e:
            self._policy = None
            self._policy_version = "heuristic_v1"
            logger.warning(
                "[ppo_allocator] PPO policy artifact 로드 실패. heuristic_v1 유지: %s",
                e,
            )
