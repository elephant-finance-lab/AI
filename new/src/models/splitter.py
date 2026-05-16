"""S1-0 Batch B WalkForwardSplitter.

논문 근거: Marcos López de Prado "Advances in Financial Machine Learning" Ch. 7 + 8.

Mechanic:
  1. panel DataFrame (MultiIndex ticker × ts_close)에서 unique ts_close 배열 추출
  2. 각 ts_close를 date 단위로 grouping → train/test 날짜 윈도우 결정
  3. 분봉 단위 purge (train 말단) + embargo (test 시작)
     purge_bars = 60 (multi-scale 60m + label 5m 커버)
     embargo_bars = 78 (Prado 1% rule of test period)

모든 파라미터는 risk_config.yaml에서 로드 (불변 원칙 5).
"""
from __future__ import annotations

from datetime import date
from typing import Iterator

import numpy as np

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger

logger = get_logger("splitter")


class WalkForwardSplitter:
    """Purge/Embargo 기반 rolling walk-forward split.

    Attributes (risk_config.yaml에서 로드):
        n_splits             : fold 개수
        train_window_days    : train 윈도우 거래일
        test_window_days     : test 윈도우 거래일
        step_days            : fold 간 step
        trading_minutes      : 거래일당 분봉 수 (390)
        purge_bars           : train 말단 잘라낼 분봉 수
        embargo_bars         : test 시작 전 공백 분봉 수

    Usage:
        splitter = WalkForwardSplitter()
        for fold_idx, (train_idx, test_idx) in enumerate(splitter.split(panel)):
            X_train = panel.iloc[train_idx][feature_cols]
            ...
    """

    def __init__(self) -> None:
        wf = config_load("risk_config.yaml", "walk_forward")
        vt = config_load("risk_config.yaml", "validation_tools")
        be = vt["backtest_engine"]

        self.n_splits: int = int(wf["n_splits"])
        self.train_window_days: int = int(wf["train_window_days"])
        self.test_window_days: int = int(wf["test_window_days"])
        self.step_days: int = int(wf["step_days"])
        self.trading_minutes: int = int(wf["trading_minutes_per_day"])

        self.purge_bars: int = int(be["purge_bars"])
        self.embargo_bars: int = int(be["embargo_bars"])

        logger.info(
            "[splitter] 초기화: n_splits=%d, train=%dd, test=%dd, step=%dd, "
            "purge=%d bars, embargo=%d bars",
            self.n_splits, self.train_window_days, self.test_window_days,
            self.step_days, self.purge_bars, self.embargo_bars,
        )

    # ================================================================== #
    # Public API
    # ================================================================== #

    def split(self, panel) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        """Panel DataFrame (MultiIndex ticker × ts_close) → (train_idx, test_idx) iterator.

        train_idx / test_idx: row position array (panel.iloc[idx] 소비용).
        각 fold는 purge/embargo 적용 후 반환.

        Raises:
            ValueError: panel이 비어있거나 MultiIndex 구조가 잘못된 경우
        """
        if panel is None or len(panel) == 0:
            raise ValueError("panel이 비어있음")

        ts_level = self._get_ts_level(panel)

        # 1. unique ts_close 정렬
        ts_unique_sorted = self._unique_ts_sorted(ts_level)

        # 2. date → ts_close list 매핑
        date_to_ts = self._group_by_date(ts_unique_sorted)
        sorted_dates = sorted(date_to_ts.keys())
        n_dates = len(sorted_dates)

        min_required = self.train_window_days + self.test_window_days
        if n_dates < min_required:
            logger.warning(
                "[splitter] 사용 가능 거래일 %d < 최소 요구 %d. 0 folds.",
                n_dates, min_required,
            )
            return

        # 3. fold 생성
        fold_count = 0
        for fold_idx in range(self.n_splits):
            train_start_i = fold_idx * self.step_days
            train_end_i = train_start_i + self.train_window_days
            test_start_i = train_end_i
            test_end_i = test_start_i + self.test_window_days

            if test_end_i > n_dates:
                break

            train_dates = sorted_dates[train_start_i:train_end_i]
            test_dates = sorted_dates[test_start_i:test_end_i]

            train_ts = self._dates_to_ts(train_dates, date_to_ts)
            test_ts = self._dates_to_ts(test_dates, date_to_ts)

            # purge
            if self.purge_bars > 0:
                train_ts = train_ts[:-self.purge_bars] if len(train_ts) > self.purge_bars else []

            # embargo
            if self.embargo_bars > 0:
                test_ts = test_ts[self.embargo_bars:] if len(test_ts) > self.embargo_bars else []

            if len(train_ts) == 0 or len(test_ts) == 0:
                logger.warning(
                    "[splitter] fold %d: purge/embargo 후 empty. skip.", fold_idx
                )
                continue

            # 4. ts_close → row index 변환
            train_idx = self._ts_to_row_idx(panel, set(train_ts), ts_level)
            test_idx = self._ts_to_row_idx(panel, set(test_ts), ts_level)

            fold_count += 1
            logger.debug(
                "[splitter] fold %d: train %d rows (%s~%s), test %d rows (%s~%s)",
                fold_idx,
                len(train_idx), train_dates[0], train_dates[-1],
                len(test_idx), test_dates[0], test_dates[-1],
            )
            yield train_idx, test_idx

        logger.info("[splitter] 총 %d fold 생성", fold_count)

    # ================================================================== #
    # helpers
    # ================================================================== #

    @staticmethod
    def _get_ts_level(panel):
        """panel MultiIndex에서 ts_close level 추출."""
        idx = panel.index
        if "ts_close" in (idx.names or []):
            return idx.get_level_values("ts_close")
        # fallback: 단일 index (ts_close만)
        raise ValueError(
            "panel.index.names에 'ts_close' 필요. MultiIndex(ticker, ts_close) 예상."
        )

    @staticmethod
    def _unique_ts_sorted(ts_level):
        """unique ts_close 정렬. pandas Index → 정렬된 list."""
        import pandas as pd_

        uniq = pd_.Index(ts_level).unique()
        return sorted(uniq.tolist())

    @staticmethod
    def _group_by_date(ts_list):
        """[Timestamp] → {date: [Timestamp...]}."""
        out: dict[date, list] = {}
        for t in ts_list:
            d = t.date() if hasattr(t, "date") else t
            out.setdefault(d, []).append(t)
        return out

    @staticmethod
    def _dates_to_ts(dates_subset, date_to_ts):
        """list[date] → 해당 날짜들의 모든 ts_close 정렬 list."""
        out = []
        for d in dates_subset:
            out.extend(date_to_ts.get(d, []))
        return sorted(out)

    @staticmethod
    def _ts_to_row_idx(panel, ts_set, ts_level):
        """panel에서 ts_close가 ts_set에 속하는 row의 position index 배열 반환."""
        mask = ts_level.isin(list(ts_set))
        return np.where(np.asarray(mask))[0]
