"""LightGBM 퀀트 시그널 추론 facade. artifacts/lgbm/ registry 로드."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from src.models.registry import ModelRegistry
from src.utils.config_loader import load as config_load


class LGBMQuant:
    """Hot Path LightGBM 추론 전용 facade.

    QuantAgent는 ModelRegistry를 직접 사용하지만, 과거 import 경로
    ``src.models.lgbm_quant.LGBMQuant``도 실제 추론이 가능해야 한다.
    """

    _ARTIFACTS_PATH = Path(__file__).resolve().parents[3] / "artifacts" / "lgbm"

    def __init__(self, registry: ModelRegistry | None = None) -> None:
        self._registry = registry or ModelRegistry(self._ARTIFACTS_PATH)
        self._model: Any | None = None
        self._metadata: dict[str, Any] = {}

    def load(self) -> None:
        """ModelRegistry latest 모델 로드."""
        model, metadata = self._registry.load_latest()
        self._model = model
        self._metadata = dict(metadata or {})

    def predict(self, features: dict[str, Any]) -> dict[str, Any]:
        """피처 딕셔너리 → 시그널 점수 반환.

        입력 형식:
          - 단일 종목: ``{feature_col: value}``
          - 다중 종목: ``{ticker: {feature_col: value}}``
        """
        if self._model is None:
            self.load()

        if self._model is None:  # assert 대신 명시적 체크
            raise RuntimeError("모델 로드 실패. ModelRegistry 확인 필요.")
        feature_cols = self._feature_cols()
        if self._is_batch(features):
            tickers = list(features.keys())
            matrix = np.asarray(
                [
                    [float((features[ticker] or {}).get(col, 0.0)) for col in feature_cols]
                    for ticker in tickers
                ],
                dtype=float,
            )
            preds = self._model.predict(matrix)
            return {
                "scores": {
                    str(ticker).zfill(6): float(score)
                    for ticker, score in zip(tickers, preds, strict=False)
                },
                "model_version": self._metadata.get("version"),
                "feature_cols": list(feature_cols),
            }

        matrix = np.asarray(
            [[float(features.get(col, 0.0)) for col in feature_cols]],
            dtype=float,
        )
        score = float(self._model.predict(matrix)[0])
        return {
            "score": score,
            "model_version": self._metadata.get("version"),
            "feature_cols": list(feature_cols),
        }

    def _feature_cols(self) -> list[str]:
        metadata_cols = self._metadata.get("feature_cols")
        if metadata_cols:
            return list(metadata_cols)
        cfg = config_load("risk_config.yaml", "preprocessor")
        return list(cfg["feature_cols"])

    @staticmethod
    def _is_batch(features: dict[str, Any]) -> bool:
        return bool(features) and all(isinstance(v, dict) for v in features.values())
