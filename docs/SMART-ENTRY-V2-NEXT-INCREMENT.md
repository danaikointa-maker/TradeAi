# Smart Entry V2 — Next Increment: Historical Replay Verification

Status: REVIEW CHECKLIST ONLY. This document does not certify MT5 Demo or LIVE readiness.

## Verified baseline
The current master plan at `docs/SMART-ENTRY-V2-MASTER-PLAN.md` records merged PRs #24, #27, #28 and describes partial post-entry M5 candle observations. This checklist avoids reimplementing those completed slices.

## Remaining work, in order
- [ ] Inspect the current archive schema and API endpoints, and list observed versus decision-time candles by broker/account/ticket.
- [ ] Add deterministic tests for pre-entry/unfinished/future/stale candles, duplicate conflicting candle payloads, restarts, and multiple symbols.
- [ ] Audit archive source provenance, UTC timestamps, feed mode, and gap flags; never infer unseen candles from later market data.
- [ ] Verify chart uses archived originals for original plan/SR/Fibonacci/structure and separately labels observed post-entry bars.
- [ ] Add independent broker deal-history attribution checks for partial fills, out/out_by/inout and position-ID ambiguity; ambiguous cases remain unresolved and block automatic assertions.
- [ ] Test disconnect/reconnect, unexpected manual broker changes, idempotency and fail-closed protections with simulated fixtures first.
- [ ] Run focused pytest, full regression, frontend build, release gate; record actual commands/results and unresolved failures.
- [ ] Only then arrange controlled MT5 Demo acceptance, broker-sourced ticks and restore tests with operator authorization.

## Acceptance criteria
1. Reopened trades reproduce original archived plan and original chart levels without recomputing from current bars.
2. Missing post-entry periods are marked gaps, not filled or labelled complete.
3. Every observation is associated with correct broker/account/workspace/ticket, timestamp and source.
4. Archive failures never suppress protective position management, and do not enable new orders.
5. No false claims of broker-verified fills, complete replay or LIVE readiness.
6. Reviewable PR includes test outputs, rollback notes and limitations.

## Safety
PAPER/DEMO only; no live orders, AutoTrading changes, credentials, production deployment or VM changes. LLM remains read-only with respect to order execution.
