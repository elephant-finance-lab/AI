"""Top-10 추천 서빙(build_recommendations_payload) 지연 측정 - 30회 배치 + JSON 리포트.

전제: 장중(09:00~15:30) + KIS 자격 + 오늘치 dual_source 피처 준비.
실행: repo 루트에서  python measure_recs.py
결과: artifacts/reports/recs_latency/recs_latency_<시각>.json
주의: payloads.py에 timing 패치가 적용돼 있어야 bars/score/init 구간값이 채워짐
      (미적용 시 total_ms만 의미 있음).
"""
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "new")
from src.integration.grpc.payloads import build_recommendations_payload  # noqa: E402

ROOT = Path(".").resolve()
N = 30
SLEEP_SEC = 3  # KIS rate-limit 여유 (호출이 많아 간격을 둠)
BUNDLE = "BUNDLE-20260521-POSTCLOSE"

rows = []
for i in range(N):
    t = time.perf_counter()
    try:
        payload = build_recommendations_payload(
            bundle_id=BUNDLE, include_diagnostics=True, root=ROOT,
        )
        total_ms = round((time.perf_counter() - t) * 1000, 1)
        try:
            diag = json.loads(payload.get("diagnostics_json") or "{}")
        except Exception:
            diag = {}
        row = {
            "run": i + 1,
            "status": payload.get("status"),
            "reason": payload.get("reason"),
            "total_ms": total_ms,
            "quant_init_ms": diag.get("timing_quant_init_ms"),
            "bars_ms": diag.get("timing_bars_ms"),
            "score_ms": diag.get("timing_score_ms"),
        }
    except Exception as e:
        total_ms = round((time.perf_counter() - t) * 1000, 1)
        row = {
            "run": i + 1,
            "status": "ERROR",
            "reason": f"{type(e).__name__}: {e}",
            "total_ms": total_ms,
            "quant_init_ms": None,
            "bars_ms": None,
            "score_ms": None,
        }
    rows.append(row)
    print(row)
    if i < N - 1:
        time.sleep(SLEEP_SEC)


def summarize(key):
    vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
    if not vals:
        return None
    s = sorted(vals)
    return {
        "n": len(vals),
        "min": min(vals),
        "mean": round(statistics.mean(vals), 1),
        "p95": s[int(len(vals) * 0.95)] if len(vals) > 1 else s[0],
        "max": max(vals),
    }


summary = {k: summarize(k) for k in ("total_ms", "quant_init_ms", "bars_ms", "score_ms")}
out_dir = ROOT / "artifacts" / "reports" / "recs_latency"
out_dir.mkdir(parents=True, exist_ok=True)
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
report_path = out_dir / f"recs_latency_{ts}.json"
with report_path.open("w", encoding="utf-8") as f:
    json.dump(
        {
            "generated_at": ts,
            "bundle_id": BUNDLE,
            "n_runs": N,
            "sleep_sec": SLEEP_SEC,
            "summary": summary,
            "runs": rows,
        },
        f,
        ensure_ascii=False,
        indent=2,
    )

print("\n=== SUMMARY (ms) ===")
for k, v in summary.items():
    print(k, v)
print("\nreport saved:", report_path)
