"""LLM 라우터 v3 Sprint 2 S2-2 (2026-04-21).

Kanana-o 100회/일 + GPT-4o fallback + circuit breaker.

아키텍처:
    Mode A Cold Path: kanana-o 우선 → 예산 초과/429/timeout/circuit OPEN → gpt-4o fallback
    Mode B: gpt-4o 전용 (별도 예산, unlimited)
    Hot Path: LLM 호출 금지 (call(mode='hot') → RuntimeError)

컴포넌트:
    _BudgetTracker: 일일 예산 + caller 별 allocation (KST 자정 리셋)
    _CircuitBreaker: 3회 연속 실패 → 5분 OPEN → HALF_OPEN → 성공 시 CLOSED
    LLMRouter: 위 2개 + HTTP 호출 (Sprint 4 S4-6 실 API 교체)

SSOT: api_contracts.md v3.4 C5 (llm_budget.source: risk_config.yaml llm_budget)
"""
from __future__ import annotations

import os
import json
import time
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger
from src.utils.safe_cast import safe_bool, safe_int
from src.utils.time_utils import now_kst

logger = get_logger("llm_router")

_KST = ZoneInfo("Asia/Seoul")


class LLMModel(str, Enum):
    """사용 가능한 LLM 모델 식별자."""

    KANANA_O = "kanana-o"
    GPT_4O = "gpt-4o"


class CircuitState(str, Enum):
    """Circuit breaker 상태."""

    CLOSED = "closed"       # 정상 (호출 허용)
    OPEN = "open"           # 차단 (failure_threshold 연속 실패 후 open_duration_sec 동안)
    HALF_OPEN = "half_open" # 시험 호출 1회 허용 (open_duration_sec 경과 후)


@dataclass
class LLMCallResult:
    """LLM 호출 결과 컨테이너.

    Fields:
        success: 호출 성공 여부
        model_used: 실제 사용된 모델 ('kanana-o' | 'gpt-4o')
        content: 모델 응답 텍스트 (실패 시 None)
        latency_ms: 호출 지연 시간 (ms)
        tokens_in: 입력 토큰 수 (추정치 또는 API 반환값)
        tokens_out: 출력 토큰 수
        cost_usd: 비용 추정치 (USD)
        error: 오류 메시지 (성공 시 None)
        fallback_used: Kanana 실패 후 GPT-4o로 전환 여부
        circuit_state: 호출 시점의 circuit breaker 상태
    """

    success: bool
    model_used: str
    content: str | None
    latency_ms: float
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    error: str | None = None
    fallback_used: bool = False
    circuit_state: str = "closed"


class _BudgetTracker:
    """일일 LLM 호출 예산 추적기.

    KST 자정 기준 매일 리셋. caller별 allocation 초과 차단.
    총 daily_limit 및 caller별 allocation 양쪽 모두 체크.
    """

    def __init__(self, daily_limit: int, allocation: dict[str, int]) -> None:
        # TODO(Sprint4-async): threading.Lock 추가. 현재 단일 스레드 전제.
        #   async 도입 시 _counts (BudgetTracker) 동시 접근 보호 필요.
        self._daily_limit = daily_limit
        self._allocation: dict[str, int] = dict(allocation)
        self._counts: dict[str, int] = defaultdict(int)   # caller → 누적 횟수
        self._total: int = 0
        self._reset_day: str | None = None  # 'YYYY-MM-DD' 형식

    def _maybe_reset(self) -> None:
        """KST 자정 경과 시 카운터 리셋."""
        today = now_kst().strftime("%Y-%m-%d")
        if self._reset_day != today:
            self._counts.clear()
            self._total = 0
            self._reset_day = today
            logger.info(f"일일 예산 리셋: {today}")

    def can_call(self, caller: str) -> tuple[bool, str]:
        """호출 허용 여부 + 사유 반환.

        Returns:
            (True, 'OK') 허용.
            (False, 'DAILY_LIMIT_REACHED: N/M') 총 한도 초과.
            (False, 'CALLER_QUOTA_EXCEEDED: caller N/M') caller 할당 초과.
        """
        self._maybe_reset()

        if self._total >= self._daily_limit:
            return False, f"DAILY_LIMIT_REACHED: {self._total}/{self._daily_limit}"

        alloc = self._allocation.get(caller)
        if alloc is not None and self._counts[caller] >= alloc:
            return False, f"CALLER_QUOTA_EXCEEDED: {caller} {self._counts[caller]}/{alloc}"

        return True, "OK"

    def record(self, caller: str) -> None:
        """호출 1회 기록. can_call() 통과 후 반드시 호출."""
        self._maybe_reset()
        self._counts[caller] += 1
        self._total += 1

    def remaining_total(self) -> int:
        """일일 총 잔여 호출 횟수."""
        self._maybe_reset()
        return max(0, self._daily_limit - self._total)

    def remaining_for(self, caller: str) -> int:
        """특정 caller의 잔여 호출 횟수. allocation 없으면 총 잔여 반환."""
        self._maybe_reset()
        alloc = self._allocation.get(caller)
        if alloc is None:
            return self.remaining_total()
        return max(0, alloc - self._counts[caller])

    @property
    def total_today(self) -> int:
        """오늘 총 호출 횟수 (테스트/모니터링용)."""
        self._maybe_reset()
        return self._total


class _CircuitBreaker:
    """연속 실패 감지 + 자동 복구 circuit breaker.

    상태 전이:
      CLOSED → OPEN: failure_threshold 연속 실패 시
      OPEN → HALF_OPEN: open_duration_sec 경과 후
      HALF_OPEN → CLOSED: 시험 호출 1회 성공 시
      HALF_OPEN → OPEN: 시험 호출 1회 실패 시 (즉시 OPEN, 타이머 리셋)
    """

    def __init__(
        self,
        failure_threshold: int,
        open_duration_sec: int,
    ) -> None:
        """CB 설정은 LLMRouter 가 risk_config.yaml 에서 로드 후 주입. default 금지."""
        # TODO(Sprint4-async): threading.Lock 추가. 현재 단일 스레드 전제.
        #   async 도입 시 _failures/_state 동시 접근 보호 필요.
        self._threshold = failure_threshold
        self._open_duration_sec = open_duration_sec
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None

    def can_attempt(self) -> bool:
        """호출 시도 가능 여부."""
        if self._state == CircuitState.CLOSED:
            return True

        if self._state == CircuitState.OPEN:
            if (
                self._opened_at is not None
                and time.time() - self._opened_at >= self._open_duration_sec
            ):
                self._state = CircuitState.HALF_OPEN
                logger.info("circuit HALF_OPEN (시험 호출 허용, %ss 경과)", self._open_duration_sec)
                return True
            return False

        # HALF_OPEN: 1회 시험 허용
        return True

    def record_success(self) -> None:
        """성공 기록. 모든 상태에서 CLOSED 복귀."""
        self._failures = 0
        if self._state != CircuitState.CLOSED:
            logger.info("circuit CLOSED (복귀)")
        self._state = CircuitState.CLOSED
        self._opened_at = None

    def record_failure(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            # HALF_OPEN 실패: 즉시 OPEN 복귀, 타이머 리셋, _failures 초기화
            self._failures = 0
            self._state = CircuitState.OPEN
            self._opened_at = time.time()
            logger.warning(f"circuit OPEN (HALF_OPEN 실패, {self._open_duration_sec}s 재차단)")
            return

        self._failures += 1
        if self._failures >= self._threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.time()
            logger.warning(
                f"circuit OPEN (연속 {self._failures}회 실패, {self._open_duration_sec}s 차단)"
            )

    @property
    def state(self) -> str:
        """현재 circuit 상태 문자열."""
        return self._state.value

    @property
    def failure_count(self) -> int:
        """현재 누적 실패 횟수 (테스트/모니터링용)."""
        return self._failures


class LLMRouter:
    """LLM 호출 라우터. 불변 원칙 4 코드 레벨 강제.

    Mode 별 동작:
      - 'cold': Kanana-o 우선. 예산 초과/circuit OPEN/API 오류 시 GPT-4o fallback.
      - 'mode_b': GPT-4o 전용. Kanana-o 예산과 독립. Mode B 장마감 18:00~22:00 전용.
      - 'hot': RuntimeError raise. Hot Path 동기 LLM 호출 금지.

    구성요소:
      - _BudgetTracker: KST 자정 리셋 + caller별 allocation
      - _CircuitBreaker(kanana): Kanana-o 연속 실패 감지
      - _CircuitBreaker(gpt4o): GPT-4o 연속 실패 감지

    모든 임계값은 risk_config.yaml llm_budget 섹션에서 로드 (불변 원칙 5).
    API 키는 os.environ에서만 로드 (.env 직접 읽기 금지).

    Sprint 2 구현 레벨:
    실제 HTTP 호출은 mock (TODO: Sprint4-S4-6에서 실 API 교체).
      인터페이스 + 예산/circuit/fallback 로직은 완전 구현.
    """

    def __init__(self, config: dict | None = None) -> None:
        """LLMRouter 초기화.

        Args:
            config: 전체 config dict (테스트 주입용). None이면 risk_config.yaml 로드.
        """
        if config is not None:
            cfg = config.get("llm_budget", {})
        else:
            cfg = config_load("risk_config.yaml", "llm_budget") or {}
        if not cfg:
            logger.warning(
                "[llm_router] llm_budget 섹션 없음. "
                "기본값(daily_limit=100, cb_threshold=3, cb_duration=300) 사용"
            )

        self._daily_limit: int = int(cfg.get("kanana_daily_limit", 100))
        self._budget_allocation: dict[str, int] = dict(cfg.get("budget_allocation", {}))
        self._overflow_to: str = str(cfg.get("overflow_to", "gpt-4o"))

        # circuit breaker 설정
        cb_cfg = cfg.get("circuit_breaker", {})
        if not cb_cfg:
            logger.warning(
                "[llm_router] circuit_breaker 섹션 없음. "
                "기본값(cb_threshold=3, cb_duration=300) 사용"
            )
        self._cb_failure_threshold: int = int(cb_cfg.get("failure_threshold", 3))
        self._cb_open_duration_sec: int = int(cb_cfg.get("open_duration_sec", 300))

        # SLA 설정
        sla_cfg = cfg.get("sla", {})
        self._timeout_sec: float = float(sla_cfg.get("timeout_sec", 30.0))
        self._gpt4o_cost_placeholder: float = float(sla_cfg.get("gpt4o_cost_placeholder_usd", 0.0))
        self._kanana_cost_placeholder: float = float(sla_cfg.get("kanana_cost_placeholder_usd", 0.0))

        # Mode B caller 화이트리스트 (불변 원칙 3: Backtest Agent Mode B 전용)
        self._mode_b_allowed_callers: set[str] = set(cfg.get("mode_b_allowed_callers", []))

        # 컴포넌트 초기화
        self._budget = _BudgetTracker(self._daily_limit, self._budget_allocation)
        self._kanana_cb = _CircuitBreaker(self._cb_failure_threshold, self._cb_open_duration_sec)
        self._gpt4o_cb = _CircuitBreaker(self._cb_failure_threshold, self._cb_open_duration_sec)

        # API 키 (environ에서만 로드, .env 직접 읽기 금지)
        self._kanana_key: str | None = os.environ.get("KANANA_API_KEY")
        self._openai_key: str | None = os.environ.get("OPENAI_API_KEY")
        self._kanana_api_url: str | None = os.environ.get("KANANA_API_URL")
        self._allow_mock_provider: bool = (
            safe_bool(cfg.get("allow_mock_provider", False), default=False)
            or os.environ.get("ELEPHANT_ALLOW_LLM_MOCK") == "1"
            or bool(os.environ.get("PYTEST_CURRENT_TEST"))
        )

        logger.info(
            f"초기화 완료: Kanana 한도={self._daily_limit}회/일, "
            f"circuit threshold={self._cb_failure_threshold}회, "
            f"timeout={self._timeout_sec}s"
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def call(
        self,
        prompt: str,
        mode: str = "cold",
        caller: str = "unknown",
        structured_schema: dict | None = None,
        force_model: str | None = None,
    ) -> LLMCallResult:
        """LLM 호출 메인 진입점.

        Args:
            prompt: 사용자 프롬프트
            mode: 'cold' | 'mode_b' | 'hot'
              - 'cold': Kanana-o 우선, fallback 자동 (Cold Path 에이전트용)
              - 'mode_b': GPT-4o 전용 (Mode B 에이전트용, 불변 원칙 4)
              - 'hot': 즉시 RuntimeError (Hot Path 동기 LLM 금지, 불변 원칙 4)
            caller: budget_allocation 키 ('news_agent', 'fda_cold_path' 등)
            structured_schema: JSON schema (structured output 요청, 현재 mock)
            force_model: 'kanana-o' | 'gpt-4o' 강제 지정 (mode='cold' 시만 유효)

        Returns:
            LLMCallResult (성공 여부, 사용 모델, content, latency, 오류 등)

        Raises:
            RuntimeError: mode='hot' (불변 원칙 4 위반 방지)
        """
        if mode == "hot":
            raise RuntimeError(
                "HOT_PATH_LLM_FORBIDDEN: 불변 원칙 4 위반. "
                "Hot Path에서 동기 LLM 호출 금지."
            )

        if mode == "mode_b":
            if self._mode_b_allowed_callers and caller not in self._mode_b_allowed_callers:
                raise RuntimeError(
                    f"MODE_B_CALLER_FORBIDDEN: {caller} not in allowed list "
                    f"{sorted(self._mode_b_allowed_callers)}. "
                    f"불변 원칙 3 (Backtest Agent Mode B 전용) 강제."
                )
            logger.info(f"Mode B 호출: GPT-4o 전용, caller={caller}")
            return self._call_gpt4o(prompt, caller, structured_schema, is_fallback=False)

        # mode='cold': Kanana-o 우선
        if force_model == LLMModel.GPT_4O.value:
            logger.info(f"force_model=gpt-4o: GPT-4o 직접 호출, caller={caller}")
            return self._call_gpt4o(prompt, caller, structured_schema, is_fallback=False)

        # 1) 예산 체크
        ok, reason = self._budget.can_call(caller)
        if not ok:
            logger.info(f"예산 차단 → GPT-4o fallback: {reason}")
            result = self._call_gpt4o(prompt, caller, structured_schema, is_fallback=True)
            result.fallback_used = True
            return result

        # 2) Kanana-o circuit breaker 체크
        if not self._kanana_cb.can_attempt():
            logger.info(
                f"Kanana-o circuit {self._kanana_cb.state} → GPT-4o fallback, caller={caller}"
            )
            result = self._call_gpt4o(prompt, caller, structured_schema, is_fallback=True)
            result.fallback_used = True
            return result

        # 3) Kanana-o 호출 시도
        result = self._call_kanana(prompt, caller, structured_schema)

        if result.success:
            self._kanana_cb.record_success()
            return result

        # 실패: circuit breaker 업데이트 + GPT-4o fallback
        self._kanana_cb.record_failure()
        logger.warning(
            f"Kanana-o 실패 → GPT-4o fallback: {result.error}, caller={caller}"
        )
        fb_result = self._call_gpt4o(prompt, caller, structured_schema, is_fallback=True)
        fb_result.fallback_used = True
        return fb_result

    def budget_remaining(self, caller: str | None = None) -> int:
        """잔여 예산 조회.

        Args:
            caller: None이면 총 잔여량. 지정하면 해당 caller 잔여량.
        """
        if caller is not None:
            return self._budget.remaining_for(caller)
        return self._budget.remaining_total()

    def circuit_state(self, model: str) -> str:
        """특정 모델의 circuit breaker 상태 조회.

        Args:
            model: 'kanana-o' | 'gpt-4o'

        Returns:
            'closed' | 'open' | 'half_open'

        Raises:
            ValueError: 알 수 없는 model 식별자
        """
        if model == LLMModel.KANANA_O.value:
            return self._kanana_cb.state
        if model == LLMModel.GPT_4O.value:
            return self._gpt4o_cb.state
        raise ValueError(f"UNKNOWN_MODEL: {model}. 허용: kanana-o | gpt-4o")

    # ------------------------------------------------------------------ #
    # Provider 래퍼
    # ------------------------------------------------------------------ #

    def _call_kanana(
        self,
        prompt: str,
        caller: str,
        schema: dict | None,
    ) -> LLMCallResult:
        """Kanana-o 호출.

        성공 시 budget.record() 호출.
        API 키 누락 시 graceful failure (RuntimeError 미발생).
        """
        if self._allow_mock_provider:
            return self._mock_provider_result(
                model=LLMModel.KANANA_O.value,
                prompt=prompt,
                caller=caller,
                cost_usd=self._kanana_cost_placeholder,
                is_fallback=False,
                record_budget=True,
            )
        if self._kanana_key is None:
            logger.warning("KANANA_API_KEY 미설정: Kanana-o 호출 불가")
            return LLMCallResult(
                success=False,
                model_used=LLMModel.KANANA_O.value,
                content=None,
                latency_ms=0.0,
                error="KANANA_API_KEY_MISSING",
                circuit_state=self._kanana_cb.state,
            )
        if not self._kanana_api_url:
            logger.warning("KANANA_API_URL 미설정: Kanana-o 실 호출 불가")
            return LLMCallResult(
                success=False,
                model_used=LLMModel.KANANA_O.value,
                content=None,
                latency_ms=0.0,
                error="KANANA_API_URL_MISSING",
                circuit_state=self._kanana_cb.state,
            )

        start = time.time()
        try:
            import requests

            response = requests.post(
                self._kanana_api_url,
                headers={
                    "Authorization": f"Bearer {self._kanana_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": LLMModel.KANANA_O.value,
                    "prompt": prompt,
                    "schema": schema,
                },
                timeout=self._timeout_sec,
            )
            response.raise_for_status()
            payload = response.json()
            content = (
                payload.get("content")
                or payload.get("text")
                or payload.get("message")
                or json.dumps(payload, ensure_ascii=False)
            )
            latency_ms = (time.time() - start) * 1000.0
            self._budget.record(caller)
            return LLMCallResult(
                success=True,
                model_used=LLMModel.KANANA_O.value,
                content=str(content),
                latency_ms=latency_ms,
                tokens_in=max(1, len(prompt) // 4),
                tokens_out=safe_int(payload.get("tokens_out"), default=0, min_value=0) or None,
                cost_usd=self._kanana_cost_placeholder,
                circuit_state=self._kanana_cb.state,
            )
        except Exception as e:
            latency_ms = (time.time() - start) * 1000.0
            logger.warning("[llm_router] Kanana-o 실 호출 실패: %s", e)
            return LLMCallResult(
                success=False,
                model_used=LLMModel.KANANA_O.value,
                content=None,
                latency_ms=latency_ms,
                error=str(e),
                circuit_state=self._kanana_cb.state,
            )

    def _call_gpt4o(
        self,
        prompt: str,
        caller: str,
        schema: dict | None,
        is_fallback: bool,
    ) -> LLMCallResult:
        """GPT-4o 호출.

        API 키 누락 또는 circuit OPEN 시 graceful failure.
        """
        if self._allow_mock_provider:
            return self._mock_provider_result(
                model=LLMModel.GPT_4O.value,
                prompt=prompt,
                caller=caller,
                cost_usd=self._gpt4o_cost_placeholder,
                is_fallback=is_fallback,
                record_budget=False,
            )
        if self._openai_key is None:
            logger.warning("OPENAI_API_KEY 미설정: GPT-4o 호출 불가")
            return LLMCallResult(
                success=False,
                model_used=LLMModel.GPT_4O.value,
                content=None,
                latency_ms=0.0,
                error="OPENAI_API_KEY_MISSING",
                circuit_state=self._gpt4o_cb.state,
                fallback_used=is_fallback,
            )

        if not self._gpt4o_cb.can_attempt():
            logger.warning(f"GPT-4o circuit OPEN: caller={caller}")
            return LLMCallResult(
                success=False,
                model_used=LLMModel.GPT_4O.value,
                content=None,
                latency_ms=0.0,
                error="GPT4O_CIRCUIT_OPEN",
                circuit_state=self._gpt4o_cb.state,
                fallback_used=is_fallback,
            )

        start = time.time()
        try:
            from openai import OpenAI

            client = OpenAI(api_key=self._openai_key, timeout=self._timeout_sec)
            kwargs: dict[str, Any] = {}
            if schema is not None:
                kwargs["response_format"] = {"type": "json_object"}
            response = client.chat.completions.create(
                model=LLMModel.GPT_4O.value,
                messages=[{"role": "user", "content": prompt}],
                **kwargs,
            )
            content = response.choices[0].message.content or ""
            usage = getattr(response, "usage", None)
            latency_ms = (time.time() - start) * 1000.0
            self._gpt4o_cb.record_success()
            return LLMCallResult(
                success=True,
                model_used=LLMModel.GPT_4O.value,
                content=content,
                latency_ms=latency_ms,
                tokens_in=getattr(usage, "prompt_tokens", None),
                tokens_out=getattr(usage, "completion_tokens", None),
                cost_usd=self._gpt4o_cost_placeholder,
                circuit_state=self._gpt4o_cb.state,
                fallback_used=is_fallback,
            )
        except Exception as e:
            latency_ms = (time.time() - start) * 1000.0
            self._gpt4o_cb.record_failure()
            logger.warning("[llm_router] GPT-4o 실 호출 실패: %s", e)
            return LLMCallResult(
                success=False,
                model_used=LLMModel.GPT_4O.value,
                content=None,
                latency_ms=latency_ms,
                error=str(e),
                circuit_state=self._gpt4o_cb.state,
                fallback_used=is_fallback,
            )

    def _mock_provider_result(
        self,
        *,
        model: str,
        prompt: str,
        caller: str,
        cost_usd: float,
        is_fallback: bool,
        record_budget: bool,
    ) -> LLMCallResult:
        """테스트/명시적 demo 모드에서만 쓰는 provider mock."""
        start = time.time()
        if record_budget:
            self._budget.record(caller)
        if model == LLMModel.GPT_4O.value:
            self._gpt4o_cb.record_success()
        latency_ms = (time.time() - start) * 1000.0
        prefix = f"MOCK {model}" + (" fallback" if is_fallback else "")
        logger.info(
            "[llm_router] mock provider 호출: model=%s caller=%s fallback=%s",
            model,
            caller,
            is_fallback,
        )
        return LLMCallResult(
            success=True,
            model_used=model,
            content=f"[{prefix}] caller={caller} prompt='{prompt[:50]}'",
            latency_ms=latency_ms,
            tokens_in=max(1, len(prompt) // 4),
            tokens_out=50,
            cost_usd=cost_usd,
            circuit_state=(
                self._kanana_cb.state
                if model == LLMModel.KANANA_O.value
                else self._gpt4o_cb.state
            ),
            fallback_used=is_fallback,
        )


# === S2-7 에이전트 구현 가이드 ===
#
# News Agent / Risk Agent / Debate Agent 가 LLMRouter.call() 후 PubSubBroker.publish() 할 때:
#
#     llm_result = router.call(prompt, mode="cold", caller="news_agent")
#     if not llm_result.success:
#         return None   # handler None 반환 → dispatch skip
#
#     # LLMCallResult.content (str) → C4 message_schema (dict) 변환
#     # content 는 LLM raw text. 에이전트별 parser 로 C5 AgentReportContract payload 구조화.
#     # 예: News Agent → stance/impacted_tickers/narrative 추출
#     message = {
#         "content": llm_result.content,          # C4 필수
#         "cause_by": "NewsAgent",                # C4 필수
#         "sent_from": "news_agent",              # C4 필수
#         "priority": "normal",                   # urgent|normal|low
#         "confidence": llm_result.confidence or 0.8,
#         "reasoning": f"LLM: {llm_result.model_used}, fallback={llm_result.fallback_used}",
#         "scope": f"ticker:{event['ticker']}",  # event 에서 추출
#         "action_type": "signal",                # signal|alert|veto_recommendation|regime_change|resolution
#         "timestamp": now_kst().isoformat(),
#         # 선택: evidence_ids / uncertainty / prediction / risk_level / event_id / supersedes
#     }
#     return message   # EventGateway.dispatch_next() auto_publish 트리거
#
# 각 에이전트는 LLM content 를 자기 agent 용 parser 로 구조화해서 C4 17 필드 채움.
# parser 는 Sprint 2 S2-7/8/9 내부 구현 책임.
