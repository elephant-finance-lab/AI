# Demo Smoke Checklist

Scope: FE to BE to AI paper-safe demo. This is not strict operational proof.

## Preconditions

- [ ] AI server is running.
- [ ] BE server is running.
- [ ] FE local or deployed site is reachable.
- [ ] KIS mode is virtual or paper.
- [ ] `live_trading_allowed=false`.
- [ ] production `active_version=null`.
- [ ] `registry_mutated=false`.
- [ ] Demo bundle is `BUNDLE-20260602-DEED529F`.
- [ ] No secret is visible.

## Capture Targets

Set `SCREENSHOT_DIR` for the current checkout, then save screenshots under
`${SCREENSHOT_DIR}`. Example: `export SCREENSHOT_DIR=$PWD/../artifacts/screenshots`.

- [ ] login screen or logged-in landing
- [ ] recommendation list
- [ ] recommendation detail
- [ ] confirm/readiness screen
- [ ] start success or fail-closed reason
- [ ] chart page

Set `DEMO_EVIDENCE_DIR` for the current checkout, then save copied JSON under
`${DEMO_EVIDENCE_DIR}`. Example: `export DEMO_EVIDENCE_DIR=$PWD/../artifacts/demo_evidence/20260605`.

- [ ] recommendations response
- [ ] recommendation detail response
- [ ] readiness response
- [ ] start response or disabled reason
- [ ] active session response
- [ ] safety flags summary

## Screen Checks

### Recommendations

- [ ] `/recommend` opens.
- [ ] Ten recommendations are visible, or degraded/stale/no-cache reason is explicit.
- [ ] Fresh or stale state is visible when available.
- [ ] The page does not imply cached recommendations can bypass readiness.

### Detail

- [ ] stock name and code visible.
- [ ] natural-language reason visible.
- [ ] score/rank or clear fallback visible.
- [ ] chart or chart fallback visible.

### Confirm / Readiness

- [ ] readiness status visible.
- [ ] blocked reason visible when blocked.
- [ ] stale recommendation disables start.
- [ ] `liveTradingAllowed=false`.
- [ ] `safeToEnableLiveActions=false`.
- [ ] `registryMutated=false`.

### Paper Start

Success path:

- [ ] start enabled only for fresh recommendation plus readiness PASS.
- [ ] session becomes STARTING or RUNNING.
- [ ] KIS virtual or paper mode confirmed.
- [ ] live/real order count remains zero.

Fail-closed path:

- [ ] start disabled or rejected safely.
- [ ] concrete reason shown.
- [ ] no live/real order occurs.

## Verdict

- `DEMO_READY_PASS`: core screens work, safety flags are correct, start succeeds or clearly fail-closes.
- `DEMO_READY_PARTIAL`: recommendation/detail works, but start is blocked with clear reason.
- `DEMO_READY_BLOCKED`: login or recommendation page cannot be demonstrated.

## Follow-Up Not Blocking Demo

- Kafka notification path.
- strict 30T cadence validator.
- BE full-day scheduler proof.
- DB migration hardening.
- CORS profile hardening.
- evidence selector trace hardening.
