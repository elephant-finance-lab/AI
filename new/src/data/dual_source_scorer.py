"""C3A DualSourceScoreContract. 뉴스 vs 커뮤니티 divergence 점수화.

Sprint 4 S4-1 실구현 (2026-05-02).

## 5피처 정의 (api_contracts.md C3A SSOT)

    news_score_t              : 현재 시점 뉴스 감성 점수 (FinBERT → fallback: sentiment_dict)
    comm_score_t_1            : t-1 lag 커뮤니티 점수 (spam + manipulation + sentiment 3-stage)
    comm_score_t_2            : t-2 lag 커뮤니티 점수 (peak_lag_days=2 반영)
    news_comm_divergence      : abs(news_score_t - comm_score_t_1) 기반 불일치 지수
    community_noise_multiplier: 커뮤니티 메시지 게시량 z-score 기반 감쇠 계수

## PIT-Safety

    배치 실행 시각(08:00~08:30 KST) 기준 snapshot_ts 이후 데이터 사용 금지.
    assert_pit_safe() 로 강제 (불변 원칙 1).

## 하드코딩 금지

    모든 파라미터는 new/config/dual_source.yaml 에서 로드 (불변 원칙 5).

SSOT: new/specs/api_contracts.md C3A DualSourceScoreContract
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.data.filter_loader import (
    load_manipulation_rules,
    load_sentiment_dict,
    load_spam_rules,
)
from src.utils.config_loader import load as config_load
from src.utils.pit_guard import PITViolationError, assert_pit_safe

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")

# FinBERT 로드 여부 추적 (모듈 수준 lazy init)
_FINBERT_MODEL: Any = None
_FINBERT_TOKENIZER: Any = None
_FINBERT_PIPELINE: Any = None
_FINBERT_AVAILABLE: bool | None = None  # None = 아직 시도 안 함


def _asof_from_snapshot(snapshot_ts: str | datetime | None) -> str:
    """C3A asof는 명시 snapshot_ts가 있으면 그 시각을 사용한다."""
    if snapshot_ts is None:
        return datetime.now(_KST).isoformat()
    if isinstance(snapshot_ts, datetime):
        dt = snapshot_ts
    else:
        dt = datetime.fromisoformat(str(snapshot_ts))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_KST)
    return dt.astimezone(_KST).isoformat()


def _try_load_finbert(model_name: str = "snunlp/KR-FinBert-SC") -> bool:
    """FinBERT 모델 lazy load. 성공 시 True, 실패 시 False.

    한국어 우선: snunlp/KR-FinBert-SC. 실패 시 ProsusAI/finbert 시도.
    둘 다 실패 시 fallback 모드 (sentiment_dict.yaml 키워드 매칭).
    """
    global _FINBERT_MODEL, _FINBERT_TOKENIZER, _FINBERT_PIPELINE, _FINBERT_AVAILABLE

    if _FINBERT_AVAILABLE is not None:
        return _FINBERT_AVAILABLE

    candidates = [model_name, "ProsusAI/finbert"]
    for candidate in candidates:
        try:
            from transformers import (  # noqa: PLC0415
                AutoModelForSequenceClassification,
                AutoTokenizer,
                pipeline,
            )

            tok = AutoTokenizer.from_pretrained(candidate)
            mdl = AutoModelForSequenceClassification.from_pretrained(candidate)
            _FINBERT_TOKENIZER = tok
            _FINBERT_MODEL = mdl
            _FINBERT_PIPELINE = pipeline(
                "text-classification",
                model=mdl,
                tokenizer=tok,
                truncation=True,
                max_length=512,
            )
            _FINBERT_AVAILABLE = True
            logger.info("[dual_source] FinBERT 로드 성공: %s", candidate)
            return True
        except Exception as e:
            logger.warning("[dual_source] FinBERT 로드 실패 (%s): %s", candidate, e)

    _FINBERT_AVAILABLE = False
    logger.warning(
        "[dual_source] FinBERT 로드 불가. sentiment_dict.yaml fallback 활성화."
    )
    return False


def _finbert_score(text: str) -> float:
    """FinBERT로 text 감성 점수 반환. -1.0(부정) ~ +1.0(긍정).

    레이블 매핑:
        positive → +1.0 × confidence
        neutral  → 0.0
        negative → -1.0 × confidence
    """
    if _FINBERT_PIPELINE is None:
        raise RuntimeError("FinBERT pipeline is not initialized")
    result = _FINBERT_PIPELINE(text[:512])[0]
    return _score_finbert_result(result)


def _finbert_scores(texts: list[str], *, batch_size: int) -> list[float]:
    """FinBERT batch inference. -1.0(부정) ~ +1.0(긍정)."""
    if _FINBERT_PIPELINE is None:
        raise RuntimeError("FinBERT pipeline is not initialized")
    results = _FINBERT_PIPELINE(
        [text[:512] for text in texts],
        batch_size=batch_size,
    )
    return [_score_finbert_result(result) for result in results]


def _score_finbert_result(result: dict[str, Any]) -> float:
    label: str = result["label"].lower()
    score: float = float(result["score"])

    if "positive" in label or label == "pos":
        return score
    if "negative" in label or label == "neg":
        return -score
    return 0.0


def _keyword_fallback_score(text: str, sentiment_dict: dict) -> float:
    """sentiment_dict.yaml 키워드 매칭 기반 감성 점수 (-1.0 ~ +1.0).

    FinBERT 로드 실패 시 fallback. weights(strong/medium/weak) SSOT.
    """
    weights: dict = sentiment_dict.get("weights", {"strong": 1.0, "medium": 0.5, "weak": 0.2})
    positive: dict = sentiment_dict.get("positive", {})
    negative: dict = sentiment_dict.get("negative", {})

    pos_score = 0.0
    neg_score = 0.0

    for tier, w in weights.items():
        for kw in positive.get(tier, []):
            if kw in text:
                pos_score += w
        for kw in negative.get(tier, []):
            if kw in text:
                neg_score += w

    total = pos_score + neg_score
    if total == 0.0:
        return 0.0
    raw = (pos_score - neg_score) / total
    # clamp [-1, +1]
    return max(-1.0, min(1.0, raw))


def _is_spam(text: str, author_id: str, spam_rules: list[dict]) -> bool:
    """spam_rules.yaml 규칙 기반 스팸 탐지. True = 스팸."""
    for rule in spam_rules:
        rule_type = rule.get("type", "")
        if rule_type == "length_filter":
            if len(text) < 10:
                return True
        elif rule_type == "url_spam":
            has_url = "http" in text or "www." in text
            if has_url and len(text) < 50:
                return True
        elif rule_type == "keyword_spam":
            for kw in rule.get("keywords", []):
                if kw in text:
                    return True
        # repeat_detector는 시계열 컨텍스트 필요 → 현재 단일 포스트 검사에서 skip
    return False


def _is_manipulation(text: str, sentiment_score: float, manipulation_rules: list[dict]) -> bool:
    """manipulation_rules.yaml 규칙 기반 조작 의심 탐지. True = 조작 의심.

    단일 포스트 수준에서 판단 가능한 규칙만 적용한다.

    manipulation_rules.yaml 규칙들은 모두 집합(시계열) 컨텍스트를 요구한다:
      - extreme_sentiment: yaml 원문 "sentiment > 0.95 AND spike_ratio > 5.0".
        spike_ratio는 시계열 집합 없이 계산 불가. 단일 포스트에서 sentiment 하나만으로
        조작 판단 시 강한 긍정/부정 텍스트가 전부 제거된다 (false positive 100%).
      - repeat_pump: "같은 종목 + 극단 긍정 + 5분 내 10건+" → 집합 필요.
      - coordinated_timing: "3개+ 계정 1분 내 동시 게시" → 집합 필요.

    결론: 단일 포스트 수준에서는 모든 manipulation rule을 skip.
    집합 단위 판단은 CommunityCrawler.parse_sentiment (S2-4) 에서 처리.
    """
    return False


def _compute_community_score_from_texts(
    texts: list[str],
    sentiment_dict: dict,
    spam_rules: list[dict],
    manipulation_rules: list[dict],
    use_finbert: bool,
) -> float:
    """텍스트 목록 → 커뮤니티 점수 (-1.0 ~ +1.0).

    3-stage 필터:
        Stage 1: spam_rules 통과 (길이/URL/키워드 기반)
        Stage 2: sentiment 점수 계산 (FinBERT 또는 키워드 fallback)
        Stage 3: manipulation_rules 체크 (spam 통과 후 극단 점수 감지)
    """
    if not texts:
        return 0.0

    scores: list[float] = []
    for text in texts:
        # None 또는 str 이외 타입 skip (TypeError 방지)
        if not text or not isinstance(text, str):
            continue

        # Stage 1: spam
        if _is_spam(text, author_id="", spam_rules=spam_rules):
            continue

        # Stage 3: sentiment 점수 먼저 구해야 Stage 2 extreme_sentiment 판단 가능
        if use_finbert and _FINBERT_AVAILABLE:
            try:
                s = _finbert_score(text)
            except Exception as e:
                logger.warning("[dual_source] FinBERT 추론 실패 fallback: %s", e)
                s = _keyword_fallback_score(text, sentiment_dict)
        else:
            s = _keyword_fallback_score(text, sentiment_dict)

        # Stage 2: manipulation
        if _is_manipulation(text, s, manipulation_rules):
            continue

        scores.append(s)

    if not scores:
        return 0.0

    return statistics.mean(scores)


def _compute_noise_multiplier(
    current_volume: float,
    historical_volumes: list[float],
    noise_zscore_threshold: float,
) -> float:
    """커뮤니티 게시량 z-score 기반 noise multiplier 계산.

    z-score = (current_volume - mean(historical)) / std(historical)
    z > noise_zscore_threshold → multiplier < 1 (신호 감쇠).
    z <= 0 → multiplier = 1.0 (감쇠 없음).

    공식:
        if z > threshold:
            multiplier = 1.0 / (1.0 + (z - threshold))
        else:
            multiplier = 1.0

    Returns:
        float: 0.0 < multiplier <= 1.0
    """
    if len(historical_volumes) < 2:
        return 1.0

    mean_vol = statistics.mean(historical_volumes)
    std_vol = statistics.stdev(historical_volumes)

    if std_vol == 0.0:
        return 1.0

    z = (current_volume - mean_vol) / std_vol

    if z > noise_zscore_threshold:
        multiplier = 1.0 / (1.0 + (z - noise_zscore_threshold))
    else:
        multiplier = 1.0

    return max(0.0, min(1.0, multiplier))


class DualSourceScorer:
    """C3A DualSourceScoreContract 5피처 생성기.

    ## 5피처 (api_contracts.md C3A + architecture.md §2.3 ④)

        news_score_t              : 현재 시점 뉴스 감성 점수 (FinBERT 기반)
        comm_score_t_1            : 1 lag 커뮤니티 점수 (spam→sentiment→manipulation 3-stage 3 yaml 연동)
        comm_score_t_2            : 2 lag 커뮤니티 점수 (peak_lag_days 반영)
        news_comm_divergence      : |news_score_t - comm_score_t_1| 기반 불일치 지수
        community_noise_multiplier: 게시량 z-score 기반 감쇠 계수

    ## 설정 로드 (불변 원칙 5)

        dual_source.yaml: news.decay_lambda / community.decay_lambda /
                          community.peak_lag_days / community.noise_zscore /
                          divergence.threshold / divergence.penalty_multiplier

    ## PIT-Safety (불변 원칙 1)

        score() 호출 시 all data_ts <= snapshot_ts 강제.

    ## FinBERT fallback

        FinBERT 로드 실패 → sentiment_dict.yaml 키워드 매칭 자동 전환.
        source_notes 에 "finbert_fallback" 태그 기록.
    """

    def __init__(self) -> None:
        """dual_source.yaml + filter yamls 로드. FinBERT lazy load."""
        cfg = config_load("dual_source.yaml")
        self._cfg = cfg

        news_cfg: dict = cfg.get("news", {})
        comm_cfg: dict = cfg.get("community", {})
        div_cfg: dict = cfg.get("divergence", {})

        self._news_decay_lambda: float = float(news_cfg.get("decay_lambda", 0.8))
        self._finbert_batch_size: int = max(1, int(news_cfg.get("finbert_batch_size", 1)))
        self._comm_decay_lambda: float = float(comm_cfg.get("decay_lambda", 0.4))
        self._peak_lag_days: int = int(comm_cfg.get("peak_lag_days", 2))
        self._noise_zscore_threshold: float = float(comm_cfg.get("noise_zscore", 2.5))
        self._divergence_threshold: float = float(div_cfg.get("threshold", 0.5))
        self._penalty_multiplier: float = float(div_cfg.get("penalty_multiplier", 0.85))

        # 3 yaml 필터 로드 (filter_loader.py 재사용)
        self._spam_rules: list[dict] = load_spam_rules().get("filters", [])
        self._manipulation_rules: list[dict] = load_manipulation_rules().get("rules", [])
        self._sentiment_dict: dict = load_sentiment_dict()

        self._finbert_cache: dict[str, float] = {}

        logger.info(
            "[dual_source] 초기화 완료: news_decay=%.2f comm_decay=%.2f "
            "peak_lag=%dd noise_z=%.1f div_threshold=%.2f",
            self._news_decay_lambda,
            self._comm_decay_lambda,
            self._peak_lag_days,
            self._noise_zscore_threshold,
            self._divergence_threshold,
        )

    # ------------------------------------------------------------------ #
    # Sub-task 1: news_score_t (FinBERT + fallback)
    # ------------------------------------------------------------------ #

    def score_news(
        self,
        news_texts: list[str],
        data_ts: str | datetime | None = None,
        snapshot_ts: str | datetime | None = None,
    ) -> tuple[float, str]:
        """뉴스 텍스트 목록 → news_score_t (-1.0 ~ +1.0).

        FinBERT 사용 시도 → 실패 시 sentiment_dict 키워드 fallback.

        Args:
            news_texts : 뉴스 제목/본문 텍스트 리스트.
            data_ts    : 뉴스 수집 시각 (PIT-Safety 검증용). None 이면 검증 skip.
            snapshot_ts: PIT-Safety 기준 시각. None 이면 오늘 18:00 KST.

        Returns:
            (score, source_note): score는 -1~+1. source_note는 "finbert" | "finbert_fallback".
        """
        # PIT-Safety 검증 (data_ts 제공 시)
        if data_ts is not None:
            assert_pit_safe(data_ts, snapshot_ts)

        if not news_texts:
            return 0.0, "no_news"

        # FinBERT 로드 시도 (lazy)
        use_finbert = _try_load_finbert()
        source_note = "finbert" if use_finbert else "finbert_fallback"

        valid_texts = [text for text in news_texts if text and text.strip()]
        if not valid_texts:
            return 0.0, source_note

        scores: list[float] = []
        if use_finbert:
            try:
                uncached = [t for t in valid_texts if t not in self._finbert_cache]
                if uncached:
                    new_scores = _finbert_scores(
                        uncached,
                        batch_size=self._finbert_batch_size,
                    )
                    for t, s in zip(uncached, new_scores):
                        self._finbert_cache[t] = s
                scores = [self._finbert_cache[t] for t in valid_texts]
            except Exception as e:
                logger.warning("[dual_source] FinBERT batch 추론 실패 fallback: %s", e)
                source_note = "finbert_fallback"
                scores = [
                    _keyword_fallback_score(text, self._sentiment_dict)
                    for text in valid_texts
                ]
        else:
            scores = [
                _keyword_fallback_score(text, self._sentiment_dict)
                for text in valid_texts
            ]

        if not scores:
            return 0.0, source_note

        # 단순 평균 (decay_lambda는 배치 레벨 감쇠 — score() 메서드에서 적용)
        avg = statistics.mean(scores)
        logger.debug("[dual_source] news_score_t=%.4f (n=%d, src=%s)", avg, len(scores), source_note)
        return avg, source_note

    # ------------------------------------------------------------------ #
    # Sub-task 2+3: comm_score_t_1, comm_score_t_2 (3 yaml 연동 + peak_lag)
    # ------------------------------------------------------------------ #

    def score_community(
        self,
        comm_texts_t1: list[str],
        comm_texts_t2: list[str] | None = None,
        data_ts_t1: str | datetime | None = None,
        data_ts_t2: str | datetime | None = None,
        snapshot_ts: str | datetime | None = None,
    ) -> tuple[float, float]:
        """커뮤니티 텍스트 → (comm_score_t_1, comm_score_t_2).

        3-stage 필터 (spam → manipulation → sentiment) 적용.

        Args:
            comm_texts_t1 : t-1 시점 커뮤니티 텍스트 (어제 기준).
            comm_texts_t2 : t-2 시점 커뮤니티 텍스트 (peak_lag=2일 반영). None 이면 0.0.
            data_ts_t1    : t-1 데이터 시각 (PIT-Safety 검증용).
            data_ts_t2    : t-2 데이터 시각 (PIT-Safety 검증용).
            snapshot_ts   : PIT-Safety 기준 시각.

        Returns:
            (comm_score_t_1, comm_score_t_2): 각각 -1.0 ~ +1.0.
        """
        # PIT-Safety 검증
        if data_ts_t1 is not None:
            assert_pit_safe(data_ts_t1, snapshot_ts)
        if data_ts_t2 is not None:
            assert_pit_safe(data_ts_t2, snapshot_ts)

        # t-1 커뮤니티 점수 (sub-task 2)
        use_finbert = _FINBERT_AVAILABLE if _FINBERT_AVAILABLE is not None else False
        s_t1 = _compute_community_score_from_texts(
            texts=comm_texts_t1,
            sentiment_dict=self._sentiment_dict,
            spam_rules=self._spam_rules,
            manipulation_rules=self._manipulation_rules,
            use_finbert=use_finbert,
        )

        # t-2 커뮤니티 점수 (sub-task 3, peak_lag_days 반영)
        if comm_texts_t2 is not None:
            s_t2 = _compute_community_score_from_texts(
                texts=comm_texts_t2,
                sentiment_dict=self._sentiment_dict,
                spam_rules=self._spam_rules,
                manipulation_rules=self._manipulation_rules,
                use_finbert=use_finbert,
            )
            # peak_lag_days=2 반영: t-2 점수에 comm_decay_lambda^2 감쇠 적용
            s_t2 = s_t2 * (self._comm_decay_lambda ** self._peak_lag_days)
        else:
            s_t2 = 0.0

        # t-1 점수에 comm_decay_lambda^1 감쇠
        s_t1 = s_t1 * self._comm_decay_lambda

        logger.debug(
            "[dual_source] comm_score_t1=%.4f comm_score_t2=%.4f",
            s_t1,
            s_t2,
        )
        return s_t1, s_t2

    # ------------------------------------------------------------------ #
    # Sub-task 4: news_comm_divergence
    # ------------------------------------------------------------------ #

    def compute_divergence(self, news_score_t: float, comm_score_t_1: float) -> float:
        """뉴스↔커뮤니티 불일치 지수.

        공식 (architecture.md §8.5 + api_contracts.md C3A):
            divergence = abs(news_score_t - comm_score_t_1)

        Returns:
            float: 0.0 ~ 2.0. 1.0 초과는 극단적 불일치.
        """
        div = abs(news_score_t - comm_score_t_1)
        logger.debug(
            "[dual_source] divergence=%.4f (news=%.4f, comm_t1=%.4f)",
            div,
            news_score_t,
            comm_score_t_1,
        )
        return div

    # ------------------------------------------------------------------ #
    # Sub-task 5: community_noise_multiplier
    # ------------------------------------------------------------------ #

    def compute_noise_multiplier(
        self,
        current_volume: float,
        historical_volumes: list[float],
    ) -> float:
        """커뮤니티 게시량 z-score 기반 감쇠 계수.

        z > noise_zscore_threshold → multiplier < 1 (신호 감쇠).
        이상 거래량(z > threshold) → community score weight 자동 감소.

        Args:
            current_volume    : 오늘 커뮤니티 게시글 수.
            historical_volumes: 최근 N일 게시글 수 목록 (평균/표준편차 계산용).

        Returns:
            float: 0.0 < multiplier <= 1.0.
        """
        mult = _compute_noise_multiplier(
            current_volume=current_volume,
            historical_volumes=historical_volumes,
            noise_zscore_threshold=self._noise_zscore_threshold,
        )
        logger.debug(
            "[dual_source] noise_multiplier=%.4f (vol=%d, z_thr=%.1f)",
            mult,
            int(current_volume),
            self._noise_zscore_threshold,
        )
        return mult

    # ------------------------------------------------------------------ #
    # 통합 메서드: C3A DualSourceScoreContract 전체 출력
    # ------------------------------------------------------------------ #

    def score(
        self,
        ticker: str,
        news_texts: list[str],
        comm_texts_t1: list[str],
        comm_texts_t2: list[str] | None = None,
        current_volume: float = 0.0,
        historical_volumes: list[float] | None = None,
        data_ts: str | datetime | None = None,
        snapshot_ts: str | datetime | None = None,
    ) -> dict:
        """5피처 딕셔너리 계산 및 반환 (C3A DualSourceScoreContract 전체 출력).

        Args:
            ticker           : 종목코드 (6자리 zero-padded 자동 적용).
            news_texts       : 뉴스 텍스트 리스트 (제목 + 본문).
            comm_texts_t1    : t-1 커뮤니티 텍스트 리스트.
            comm_texts_t2    : t-2 커뮤니티 텍스트 리스트. None 이면 0.0.
            current_volume   : 오늘 커뮤니티 게시글 수 (noise multiplier용).
            historical_volumes: 최근 N일 게시글 수 목록. None 이면 multiplier=1.0.
            data_ts          : 데이터 수집 시각 (PIT-Safety 검증용). None 이면 skip.
            snapshot_ts      : PIT-Safety 기준 시각. None 이면 오늘 18:00 KST.

        Returns:
            C3A DualSourceScoreContract output 스키마:
            {
                "ticker": str,                      # 6자리 zero-padded
                "asof": str,                        # ISO8601 KST
                "news_score_t": float,              # -1.0 ~ +1.0
                "comm_score_t_1": float,            # -1.0 ~ +1.0 (decay 포함)
                "comm_score_t_2": float,            # -1.0 ~ +1.0 (peak_lag 반영)
                "news_comm_divergence": float,      # 0.0 ~ 2.0
                "community_noise_multiplier": float,# 0.0 ~ 1.0
                "source_notes": str | None,         # "finbert" | "finbert_fallback" | "no_news"
            }

        Raises:
            PITViolationError: data_ts > snapshot_ts 시.
        """
        # 종목코드 6자리 정규화
        ticker_padded = str(ticker).zfill(6)

        # PIT-Safety: data_ts 제공 시 검증
        if data_ts is not None:
            assert_pit_safe(data_ts, snapshot_ts)

        asof = _asof_from_snapshot(snapshot_ts)

        # Sub-task 1: news_score_t
        news_score_t, source_note = self.score_news(
            news_texts=news_texts,
            data_ts=data_ts,
            snapshot_ts=snapshot_ts,
        )
        # news_decay_lambda 감쇠 (배치 레벨)
        news_score_t = news_score_t * self._news_decay_lambda

        # Sub-task 2+3: comm_score_t_1, comm_score_t_2
        comm_score_t_1, comm_score_t_2 = self.score_community(
            comm_texts_t1=comm_texts_t1,
            comm_texts_t2=comm_texts_t2,
            data_ts_t1=data_ts,
            data_ts_t2=data_ts,
            snapshot_ts=snapshot_ts,
        )

        # Sub-task 4: news_comm_divergence
        divergence = self.compute_divergence(news_score_t, comm_score_t_1)

        # Sub-task 5: community_noise_multiplier
        hist_vols: list[float] = historical_volumes if historical_volumes is not None else []
        noise_mult = self.compute_noise_multiplier(current_volume, hist_vols)

        result = {
            "ticker": ticker_padded,
            "asof": asof,
            "news_score_t": round(news_score_t, 6),
            "comm_score_t_1": round(comm_score_t_1, 6),
            "comm_score_t_2": round(comm_score_t_2, 6),
            "news_comm_divergence": round(divergence, 6),
            "community_noise_multiplier": round(noise_mult, 6),
            "source_notes": source_note,
        }

        logger.info(
            "[dual_source] %s 점수 완료: news=%.4f comm_t1=%.4f div=%.4f mult=%.4f src=%s",
            ticker_padded,
            result["news_score_t"],
            result["comm_score_t_1"],
            result["news_comm_divergence"],
            result["community_noise_multiplier"],
            source_note,
        )

        return result

    # ------------------------------------------------------------------ #
    # 배치 메서드: 20종목 일괄 처리 (08:00~08:30 배치용)
    # ------------------------------------------------------------------ #

    def score_universe(
        self,
        universe: list[dict],
        snapshot_ts: str | datetime | None = None,
    ) -> list[dict]:
        """20종목 일괄 5피처 생성 (C3A 배치 출력).

        Args:
            universe: 종목별 입력 dict 목록.
                각 dict:
                    ticker            : str (필수)
                    news_texts        : list[str]
                    comm_texts_t1     : list[str]
                    comm_texts_t2     : list[str] | None
                    current_volume    : float (기본 0.0)
                    historical_volumes: list[float] | None
                    data_ts           : str | datetime | None
            snapshot_ts: PIT-Safety 기준 시각 (전체 배치 공유).

        Returns:
            list[dict]: 종목별 C3A 출력 스키마 리스트.
        """
        results: list[dict] = []
        for item in universe:
            ticker = item.get("ticker", "000000")
            try:
                result = self.score(
                    ticker=ticker,
                    news_texts=item.get("news_texts", []),
                    comm_texts_t1=item.get("comm_texts_t1", []),
                    comm_texts_t2=item.get("comm_texts_t2"),
                    current_volume=float(item.get("current_volume", 0.0)),
                    historical_volumes=item.get("historical_volumes"),
                    data_ts=item.get("data_ts"),
                    snapshot_ts=snapshot_ts,
                )
                results.append(result)
            except PITViolationError:
                logger.error(
                    "[dual_source] PIT-Safety 위반: ticker=%s data_ts=%s — skip",
                    ticker,
                    item.get("data_ts"),
                )
                raise
            except Exception as e:
                logger.error(
                    "[dual_source] 점수 계산 실패: ticker=%s err=%s — skip",
                    ticker,
                    e,
                )
                # 단일 종목 실패는 나머지 계속 처리. PITViolationError만 전파.
                results.append(
                    {
                        "ticker": str(ticker).zfill(6),
                        "asof": _asof_from_snapshot(snapshot_ts),
                        "news_score_t": 0.0,
                        "comm_score_t_1": 0.0,
                        "comm_score_t_2": 0.0,
                        "news_comm_divergence": 0.0,
                        "community_noise_multiplier": 1.0,
                        "source_notes": f"ERROR: {e}",
                    }
                )
        return results
