# Paper Service Scheduler Runbook

작성일: 2026-06-05

## 목적

`new/config/paper_service_schedule.yaml`을 서버 배포 환경에서 실행 가능한 형태로 묶는다. 이 스케줄러는 paper-safe 운영용이며 production champion 승격, live 주문, real 주문을 하지 않는다.

## 실행 원칙

- 기본은 dry-run이다. 실제 subprocess 실행은 `--execute`를 명시해야 한다.
- `.env` 파일을 스크립트가 직접 읽지 않는다. 서버 또는 operator가 실행 전에 환경변수를 주입한다.
- `paper_auto_start`는 selected tickers가 없으면 `selected_tickers_required_for_paper_auto_start`로 막힌다.
- `post_close_data_update`는 `ELEPHANT_MODE=mode_b`를 subprocess env에만 주입한다.
- production `active_version=null`, `live_trading_allowed=false`, `registry_mutated=false`는 유지한다.

## 매분 서버 실행 예시

```bash
cd "$(git rev-parse --show-toplevel)"
set -a
source "${ELEPHANT_ENV_FILE:?set ELEPHANT_ENV_FILE to the operator-approved env path}"
set +a
PYTHONPATH=$PWD/new \
/opt/anaconda3/envs/elephant/bin/python \
  new/scripts/run_paper_service_scheduler.py \
  --run-due \
  --execute \
  --write-report \
  --bundle-id BUNDLE-20260602-DEED529F
```

## 선택 종목 자동매매 시작 예시

사용자가 FE에서 추천 종목을 선택하고 BE가 tickers를 명시해 호출하는 경우에만 사용한다.

```bash
cd "$(git rev-parse --show-toplevel)"
PYTHONPATH=$PWD/new \
/opt/anaconda3/envs/elephant/bin/python \
  new/scripts/run_paper_service_scheduler.py \
  --task-id paper_auto_start \
  --selected-tickers 096770 \
  --cycles 10 \
  --interval-sec 60 \
  --execute \
  --write-report \
  --bundle-id BUNDLE-20260602-DEED529F
```

## dry-run 점검

```bash
cd "$(git rev-parse --show-toplevel)"
PYTHONPATH=$PWD/new \
/opt/anaconda3/envs/elephant/bin/python \
  new/scripts/run_paper_service_scheduler.py \
  --now 2026-06-05T08:30:10+09:00 \
  --run-due \
  --bundle-id BUNDLE-20260602-DEED529F
```

## 보고서 위치

`artifacts/reports/paper_service_scheduler/paper_service_scheduler_*.json`

## 아직 BE 영역인 것

서버 데몬, systemd/cron 등록, FE 버튼 이벤트와의 최종 배포 orchestration은 BE 배포 운영 레이어에서 담당한다. AI 스케줄러는 해당 레이어가 호출할 paper-safe 실행기다.
