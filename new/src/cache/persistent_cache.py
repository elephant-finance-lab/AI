"""Persistent Cache. S4-7 Cold Path 레이턴시 최적화.

뉴스 분석 결과 + 에이전트 보고서 TTL 기반 캐싱.
백엔드: SQLite (Python stdlib, 동시성 안전).
설정: new/config/risk_config.yaml cache 섹션 (하드코딩 금지).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from src.utils.logger import get_logger
from src.utils.safe_cast import safe_bool

logger = get_logger("cache")

# ============================================================
# 설정 로드 (SSOT: risk_config.yaml)
# ============================================================

# Codex 권고 1, 2026-05-09 fix: parents[3] → parents[2].
# 이전: /Elephant_Lab/config/risk_config.yaml (잘못된 root). 현재: /Elephant_Lab/new/config/risk_config.yaml.
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "risk_config.yaml"

# 기본값 (yaml 로드 실패 시 폴백)
_DEFAULT_CONFIG: dict = {
    "enabled": True,
    "backend": "sqlite",
    "storage_path": "artifacts/cache/persistent_cache.db",
    "news_ttl_seconds": 3600,
    "agent_report_ttl_seconds": 1800,
    "cleanup_interval_seconds": 600,
}


def _load_cache_config() -> dict:
    """risk_config.yaml의 cache 섹션 로드.

    파일 없음 / 파싱 실패 / cache 키 없음 → 기본값 반환.
    """
    try:
        with _CONFIG_PATH.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        if isinstance(cfg, dict) and "cache" in cfg:
            merged = {**_DEFAULT_CONFIG, **cfg["cache"]}
            logger.info("[cache] risk_config.yaml cache 섹션 로드 완료")
            return merged
    except Exception as e:
        logger.warning("[cache] risk_config.yaml 로드 실패, 기본값 사용: %s", e)
    return dict(_DEFAULT_CONFIG)


class PersistentCache:
    """SQLite 기반 TTL 캐시.

    thread-safe: check_same_thread=False + _lock (write 직렬화).
    key: str (PK). value: JSON 직렬화 가능 Any.
    expires_at: UNIX timestamp (int, seconds). 0 = 만료 없음.

    사용 예:
        cache = PersistentCache()
        cache.set("news:005930:EVT-001", result, ttl_seconds=3600)
        hit = cache.get("news:005930:EVT-001")
    """

    def __init__(self, db_path: Optional[Path | str] = None) -> None:
        """PersistentCache 생성.

        Args:
            db_path: SQLite 파일 경로. None이면 risk_config.yaml cache.storage_path 사용.
        """
        self._cfg = _load_cache_config()
        self._enabled: bool = safe_bool(self._cfg.get("enabled", True), default=True)

        if db_path is not None:
            resolved = Path(db_path)
        else:
            raw_path = self._cfg.get("storage_path", _DEFAULT_CONFIG["storage_path"])
            # Codex 권고 1, 2026-05-09 fix: parents[4] → parents[3].
            # 이전: /Users/jangjaewon/Desktop/artifacts/cache/... (잘못된 root 한 단계 위).
            # 현재: repo root 하위 artifacts/cache/... (Full_Part/Elephant_Lab 이동 후에도 정합).
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                resolved = Path(__file__).resolve().parents[3] / candidate
            else:
                resolved = candidate

        self._db_path = resolved
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

        # 주기적 cleanup 타이머
        self._cleanup_interval: int = int(
            self._cfg.get("cleanup_interval_seconds", 600)
        )
        self._last_cleanup: float = time.time()

        # max_entries: DB 최대 항목 수. 초과 시 LRU eviction (C-R4-2).
        # risk_config.yaml cache.max_entries에서 로드. 기본 100000.
        self._max_entries: int = int(self._cfg.get("max_entries", 100_000))

        logger.info("[cache] PersistentCache 초기화. db=%s enabled=%s max_entries=%d",
                    self._db_path, self._enabled, self._max_entries)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        """cache 테이블 + expires_at 인덱스 생성."""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                expires_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_expires_at ON cache (expires_at)"
        )
        logger.info("[cache] 스키마 초기화 완료")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def news_ttl(self) -> int:
        """뉴스 분석 결과 TTL (초). risk_config.yaml cache.news_ttl_seconds."""
        return int(self._cfg.get("news_ttl_seconds", _DEFAULT_CONFIG["news_ttl_seconds"]))

    @property
    def agent_report_ttl(self) -> int:
        """에이전트 보고서 TTL (초). risk_config.yaml cache.agent_report_ttl_seconds."""
        return int(self._cfg.get("agent_report_ttl_seconds", _DEFAULT_CONFIG["agent_report_ttl_seconds"]))

    def get(self, key: str) -> Optional[Any]:
        """캐시 조회.

        thread-safe: _lock으로 SELECT + 만료 DELETE 직렬화.
        _maybe_cleanup은 get 호출 전에 별도로 수행 (get 내부 교차 방지).

        Args:
            key: 캐시 키.

        Returns:
            저장된 값 (Any). miss 또는 만료 시 None.
        """
        if not self._enabled:
            return None

        # cleanup은 get 로직과 분리. lock 외부에서 interval 체크 후 진입.
        self._maybe_cleanup()

        now = int(time.time())

        with self._lock:
            row = self._conn.execute(
                "SELECT value, expires_at FROM cache WHERE key = ?",
                (key,),
            ).fetchone()

            if row is None:
                logger.debug("[cache] MISS key=%s", key)
                return None

            value_json, expires_at = row

            # expires_at = 0 → 만료 없음
            if expires_at != 0 and now >= expires_at:
                self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                logger.debug("[cache] 만료 MISS key=%s expires_at=%s now=%s", key, expires_at, now)
                return None

        try:
            value = json.loads(value_json)
        except (json.JSONDecodeError, TypeError) as e:
            logger.error("[cache] 역직렬화 실패 key=%s error=%s", key, e)
            return None

        logger.debug("[cache] HIT key=%s", key)
        return value

    def set(self, key: str, value: Any, ttl_seconds: int = 0) -> None:
        """캐시 저장.

        total_keys >= max_entries 시 가장 오래된 항목 1개 LRU eviction 후 저장 (C-R4-2).

        Args:
            key: 캐시 키.
            value: 저장 값 (JSON 직렬화 가능).
            ttl_seconds: 만료 시간(초). 0 = 만료 없음.
        """
        if not self._enabled:
            return

        # max_entries 초과 시 LRU eviction (기존 key 업데이트는 건수 변화 없으므로 무조건 체크)
        if self.stats()["total_keys"] >= self._max_entries:
            self._evict_oldest()

        expires_at = int(time.time()) + ttl_seconds if ttl_seconds > 0 else 0

        try:
            value_json = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            logger.error("[cache] 직렬화 실패 key=%s error=%s", key, e)
            return

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cache (key, value, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    expires_at = excluded.expires_at
                """,
                (key, value_json, expires_at),
            )
        logger.debug("[cache] SET key=%s ttl=%s expires_at=%s", key, ttl_seconds, expires_at)

    def delete(self, key: str) -> bool:
        """캐시 키 삭제.

        Args:
            key: 삭제할 캐시 키.

        Returns:
            True: 삭제 성공 (키가 존재했음). False: 키 없음.
        """
        with self._lock:
            cursor = self._conn.execute("DELETE FROM cache WHERE key = ?", (key,))
        deleted = cursor.rowcount > 0
        logger.debug("[cache] DELETE key=%s deleted=%s", key, deleted)
        return deleted

    def clear_expired(self) -> int:
        """만료된 항목 일괄 삭제.

        Returns:
            삭제된 항목 수.
        """
        now = int(time.time())
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM cache WHERE expires_at != 0 AND expires_at <= ?",
                (now,),
            )
        count = cursor.rowcount
        self._last_cleanup = time.time()
        logger.info("[cache] 만료 항목 정리 완료: %d건 삭제", count)
        return count

    def stats(self) -> dict:
        """캐시 통계.

        두 SELECT 사이에 lock 보호: active_keys 음수 방지 (C-R4-1).

        Returns:
            {total_keys, expired_keys, active_keys, db_path, enabled}
        """
        now = int(time.time())
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
            expired = self._conn.execute(
                "SELECT COUNT(*) FROM cache WHERE expires_at != 0 AND expires_at <= ?",
                (now,),
            ).fetchone()[0]
        active = total - expired
        return {
            "total_keys": total,
            "expired_keys": expired,
            "active_keys": active,
            "db_path": str(self._db_path),
            "enabled": self._enabled,
        }

    def close(self) -> None:
        """연결 닫기. 테스트/종료 시 사용."""
        try:
            self._conn.close()
            logger.info("[cache] DB 연결 닫힘: %s", self._db_path)
        except Exception as e:
            logger.error("[cache] DB 연결 닫기 실패: %s", e)

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _evict_oldest(self) -> None:
        """가장 오래된 항목 1개 LRU 삭제.

        max_entries 초과 시 set()이 호출. expires_at ASC, rowid ASC 기준.
        expires_at=0 (만료 없음) 항목도 포함 대상 (rowid 기준으로 처리).
        """
        with self._lock:
            self._conn.execute(
                "DELETE FROM cache WHERE rowid IN ("
                "SELECT rowid FROM cache ORDER BY expires_at ASC, rowid ASC LIMIT 1)"
            )
        logger.warning("[cache] max_entries 초과: LRU eviction 실행 (최고령 항목 1건 삭제)")

    def _maybe_cleanup(self) -> None:
        """cleanup_interval_seconds마다 만료 항목 lazy 정리.

        중복 실행 방지: _last_cleanup 체크를 lock 외부에서 먼저 수행.
        interval 미도달 시 바로 return (잠금 비용 없음).
        """
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        # interval 도달. clear_expired가 내부적으로 _lock 사용.
        self.clear_expired()
