# KIS Virtual Paper Trading Runbook

Purpose: prove the paper-trading path before any real-account switch. Real/live
trading stays blocked unless a separate operator gate changes the policy.

## Safety Rules

- Do not read or commit `.env`.
- Keep `KIS_MODE=virtual`.
- Keep `execution.live_enabled=false` in `new/config/risk_config.yaml`.
- Use limit orders only.
- Probe order quantity must stay within `paper_trading.max_probe_order_qty`.
- Do not edit `artifacts/lgbm/registry.json` manually. Active model promotion
  must go through C12 real backtest and C14 deploy.

## Artifact Root Separation

Keep deploy, paper, and research artifacts in separate roots. This is not just
cleanup; it prevents research experiments from being mistaken for deployable
evidence.

| Purpose | Root | Rule |
|---|---|---|
| Production model registry | `artifacts/lgbm` | Only deploy-gated production promotion may mutate it. Current paper work must keep `active_version=null`. |
| Paper model registry | `artifacts/lgbm_paper` | Paper-only registry mirror for virtual trading checks. |
| Paper candidate registry | `artifacts/lgbm_paper_candidate/{bundle_id}` | Bundle-scoped paper-auto candidate path. Market-open runs should use this root explicitly. |
| Candidate bundle | `artifacts/bundles/{bundle_id}` | Frozen candidate bytes used by C12, deploy dry-run, service readiness, and validation zip. |
| Research model registry | `artifacts/lgbm_research/...` | Hyperparameter/feature/window experiments only. Never treat as deployable until staged into a bundle and re-gated. |
| Research reports | `artifacts/reports/rolling_window_ic`, `artifacts/reports/lgbm_hyperparam_sweep`, `artifacts/reports/feature_window_grid`, `artifacts/reports/research_threshold_sweep` | Diagnostic evidence only. |

Validate the separation before handoff or market-open operation:

```bash
PYTHONPATH=new /opt/anaconda3/envs/elephant/bin/python new/scripts/validate_artifact_roots.py
```

## Environment

Set these in the user terminal that owns the KIS paper credentials:

```bash
export PYTHONPATH=new
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export KIS_MODE=virtual
```

Inject the KIS paper app key, app secret, account number, and product code from
the operator terminal only. Do not write those values or env assignments into
repo files or release notes.

Check sanitized readiness:

```bash
PYTHONPATH=new /opt/anaconda3/envs/elephant/bin/python new/scripts/print_env_readiness.py
```

The output must show `status=PASS`; it prints presence and length only, never
secret values.

## Paper Candidate Registry Staging (Prerequisite)

The paper-auto preflight checks `active_registry` against the bundle-scoped paper
candidate registry. That check reads `active_version` from
`artifacts/lgbm_paper_candidate/{bundle_id}/registry.json`. If that file is missing
or has no `active_version`, preflight fails with `active_version_null` and every
downstream paper step (rehearsal, `collect_kis_paper_evidence`, paper-auto) blocks,
even on an empty account.

Copying model files into the candidate directory is NOT enough. Only
`prepare_paper_lgbm_registry.py` writes `registry.json` with `active_version`, and
no scheduler or nightly job runs it automatically.

Stage the active version once per bundle (paper-only; never mutates the production
registry):

```bash
PYTHONPATH=new /opt/anaconda3/envs/elephant/bin/python new/scripts/prepare_paper_lgbm_registry.py \
  --candidate-version <version from artifacts/bundles/{bundle_id}/lgbm/latest_model_metadata.json> \
  --source-dir artifacts/bundles/{bundle_id}/lgbm \
  --target-dir artifacts/lgbm_paper_candidate/{bundle_id} \
  --force \
  --confirm-phrase PREPARE_PAPER_LGBM_OK
```

Verify (read-only):

```bash
PYTHONPATH=new /opt/anaconda3/envs/elephant/bin/python new/scripts/model_registry_readiness.py \
  --registry-dir artifacts/lgbm_paper_candidate/{bundle_id} \
  --require-active
```

Expect `blockers: []` and a populated `active_version`. `status` may be `WARN` on
`candidate_not_marked_deploy_quality`; preflight accepts `WARN`, so that is fine.
The staging output must show `paper_only_registry`, `live_trading_allowed=false`,
and `production_registry_mutated=false`.

Re-run this **once whenever paper trading is pointed at a different bundle** (for
example, a newly trained post-close candidate). The staged `registry.json` persists
on disk, so the same bundle never needs re-staging; only a bundle switch does.

## Balance And Reconciliation

```bash
PYTHONPATH=new /opt/anaconda3/envs/elephant/bin/python new/scripts/paper_trading_smoke.py \
  --action balance \
  --assume-empty-system-positions
```

Expected report:

```text
artifacts/reports/paper_trading/paper_trading_balance_reconciliation_*.json
```

Pass criteria:

- `status=PASS`
- `mode_guard.status=PASS`
- `balance.status=PASS`
- `reconciliation.status=PASS`

## Probe Order

Run this only after balance/reconciliation PASS. Choose a conservative limit
price from the current KIS virtual quote path.

```bash
PYTHONPATH=new /opt/anaconda3/envs/elephant/bin/python new/scripts/paper_trading_smoke.py \
  --action submit-probe \
  --ticker 005930 \
  --side buy \
  --qty 1 \
  --auto-price \
  --order-type 00 \
  --confirm-phrase PAPER_ORDER_OK
```

Pass criteria:

- `mode_guard.status=PASS`
- `order_guard.status=PASS`
- broker response contains an accepted/submitted order identifier
- order history/fill lookup is recorded

KIS OAuth token issuance is rate-limited. If `EGW00133` appears, wait at least
75 seconds and rerun the same command. Do not change credentials.

## One-Cycle Paper Auto

Run this only after:

- C12 real backtest PASS for the candidate bundle
- C14 service-policy replay PASS
- KIS virtual balance/reconciliation/probe PASS

```bash
PYTHONPATH=new /opt/anaconda3/envs/elephant/bin/python new/scripts/paper_auto_service_rehearsal.py \
  --registry-dir artifacts/lgbm_paper \
  --tickers 005930 \
  --cycles 1 \
  --interval-sec 0 \
  --confirm-phrase PAPER_AUTO_OK
```

The final report must show hot decision, FDA approve/veto reason, paper
execution response, and reconciliation evidence while `live_enabled=false`.

## 2026-05-26 Market-Open Plan

Scope: KIS virtual/paper only. Do not enable live trading. Do not mutate the
production registry. `2026-05-25` is a KRX holiday in `risk_config.yaml`, so the
next market-open check is `2026-05-26 09:00 KST`.

Candidate:

```text
BUNDLE-20260521-POSTCLOSE
```

Pre-open, around `08:30 KST`, refresh the current trading day's Dual-Source
artifact through the deploy-quality archive path. This is required because the
deployed candidate expects `news_score_t` during serving. Missing current-day
required features must block before broker reads or orders.

```bash
# In an operator-approved shell, inject API credentials without printing them.
# Do not commit credential-loading commands or secret values.
export PYTHONPATH=$PWD/new
/opt/anaconda3/envs/elephant/bin/python new/scripts/build_dart_corp_code_cache.py
/opt/anaconda3/envs/elephant/bin/python new/scripts/build_news_dart_archive.py \
  --end-date 20260526 \
  --business-days 1 \
  --naver-max-pages 10
/opt/anaconda3/envs/elephant/bin/python new/scripts/materialize_dual_source_history.py \
  --end-date 20260526 \
  --business-days 1 \
  --raw-events-dir artifacts/raw/dual_source
```

Machine-check canonical forms:

```text
new/scripts/build_dart_corp_code_cache.py
new/scripts/build_news_dart_archive.py --end-date 20260526 --business-days 1 --naver-max-pages 10
new/scripts/materialize_dual_source_history.py --end-date 20260526 --business-days 1 --raw-events-dir artifacts/raw/dual_source
```

Pre-open pass criteria:

- `artifacts/cache/dart_corp_code_kospi20.json` may keep the historical filename,
  but it must be freshly rebuilt from the current 30 active universe and show
  `matched=30`, `total=30`, `missing=[]`.
- `artifacts/raw/dual_source/20260526.json` exists and has
  `provenance.deploy_quality=true`.
- The raw archive provenance has `ticker_count=30`.
- The `materialize_dual_source_history` report for `20260526` is `PASS`.
- `artifacts/dual_source/20260526.json` exists.
- It has one `scores[]` row for each active ticker.
- Required model feature `news_score_t` exists for every requested paper ticker.
- `source_stats.input_mode` is either `real` or `archived_raw_events`.
  The deploy-quality archive path writes `archived_raw_events`; this is valid
  when the raw archive provenance is real and `neutral_rehearsal_file=false`.
- Community may be `unavailable_empty` if real scraping is disabled, but mock
  community content must not be mixed in.

At `09:00 KST`, collect KIS virtual evidence first. This command performs the
paper-only balance, probe order, order-history requery, and one-cycle rehearsal
bundle. It must remain virtual/paper only.

Before this run on a fresh machine — or after switching to a new bundle — stage the
paper candidate registry first (see "Paper Candidate Registry Staging"). Without it
the rehearsal blocks with `active_version_null`.

```bash
# In an operator-approved shell, inject KIS paper credentials without printing them.
# Do not commit credential-loading commands or secret values.
export PYTHONPATH=$PWD/new
/opt/anaconda3/envs/elephant/bin/python new/scripts/collect_kis_paper_evidence.py \
  --bundle-id BUNDLE-20260521-POSTCLOSE \
  --registry-dir artifacts/lgbm_paper_candidate/BUNDLE-20260521-POSTCLOSE \
  --tickers 005930 \
  --ticker 005930 \
  --side buy \
  --qty 1 \
  --auto-price \
  --cycles 1 \
  --interval-sec 0 \
  --assume-empty-system-positions \
  --probe-confirm-phrase PAPER_ORDER_OK \
  --auto-confirm-phrase PAPER_AUTO_OK
```

This service-rehearsal command intentionally uses one ticker for the external
paper order-path probe. It proves balance, reconciliation, probe order, order
history, and paper-auto service evidence for the candidate bundle. It is not a
30-stock cadence proof.

Use `--assume-empty-system-positions` only when the latest paper balance shows a
flat system account. If the operator manually changes paper holdings before the
open, refresh the system position snapshot first and do not reuse stale `/tmp`
files from prior trading days.

After evidence PASS, rerun read-only status gates:

```bash
export PYTHONPATH=$PWD/new
/opt/anaconda3/envs/elephant/bin/python new/scripts/service_readiness_status.py \
  --bundle-id BUNDLE-20260521-POSTCLOSE
/opt/anaconda3/envs/elephant/bin/python new/scripts/prelive_gate.py \
  --bundle-id BUNDLE-20260521-POSTCLOSE \
  --end-date 20260521 \
  --business-days 253 \
  --max-tickers 30
```

If the gates PASS, start a guarded paper-auto run. For the first market-open
verification after the zero-score fix, prefer a 60-cycle run before considering
longer windows.

```bash
# In an operator-approved shell, inject KIS paper credentials without printing them.
# Do not commit credential-loading commands or secret values.
export PYTHONPATH=$PWD/new
/opt/anaconda3/envs/elephant/bin/python new/scripts/paper_auto_trade.py \
  --bundle-id BUNDLE-20260521-POSTCLOSE \
  --registry-dir artifacts/lgbm_paper_candidate/BUNDLE-20260521-POSTCLOSE \
  --tickers "" \
  --max-tickers 30 \
  --cycles 60 \
  --interval-sec 60 \
  --end-date 20260521 \
  --business-days 253 \
  --confirm-phrase PAPER_AUTO_OK
```

For `paper_auto_trade.py`, empty `--tickers ""` means load the active universe
from `new/config/universe_config.yaml` and cap it with `--max-tickers 30`.
Do not use the historical five-ticker semiconductor subset as 30-stock evidence.

Only add `--cold-risk-report <path>` when a fresh `20260526` cold-risk report
has been generated and inspected. Do not reuse stale reports from prior trading
days for the market-open proof run.

Stop conditions:

- `serving_feature_readiness.status != PASS`
- `quant_output.mode=blocked`
- active model produces zero scores after warmup
- KIS rejects for non-transient broker/account/risk reasons
- consecutive read-only KIS errors exceed the configured skip budget
- any report shows `live_trading_allowed=true` or `registry_mutated=true`

Expected report families:

- `artifacts/reports/paper_trading/*.json`
- `artifacts/reports/paper_auto_trading/*.json`
- `artifacts/reports/service_readiness/*.json`
- `artifacts/reports/prelive_gate/*.json`

## 2026-05-15 Evidence Snapshot

- `paper_trading_balance_reconciliation_20260515_134605.json`: PASS.
- `paper_trading_submit_probe_order_20260515_134857.json`: PASS, order-history
  matched count 1.
- `paper_auto_service_rehearsal_20260515_135618.json`: PASS, external KIS
  virtual, paper auto cycle PASS.
- `service_readiness_BUNDLE-20260512-0AEEE37A_20260515_135651.json`: PASS,
  `deploy_quality=PASS`, `broker_evidence=PASS`, `registry_mutated=false`,
  `live_trading_allowed=false`.
