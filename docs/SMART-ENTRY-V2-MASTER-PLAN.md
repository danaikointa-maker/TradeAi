# No7 AI Trading Pro V2 — Smart Entry & Auto Zone Master Plan

Status: IN PROGRESS. This is a development plan, not a trading-system acceptance or profitability claim.
Source of truth: this repository's executable code and current tests. Historical docs may be stale.
Delivery: PR #24, #27, #28 and #30 were merged into `main` after review. The
next increment is proposed on `feature/smart-entry-v2-conservative-recovery`;
no direct push to `main`.

## Goal
Extend the existing FastAPI / Vue 3 / Lightweight Charts / MT5-adapter architecture. Do not replace existing zone, structure, risk, execution, backtest, journal or authentication modules.

### Feature-to-code map
| Capability | Existing basis | V2 extension | Acceptance |
|---|---|---|---|
| W-Flip support/resistance | `packages/strategy_rules/zones.py` and `packages/market_structure/` | Detect confirmed break and subsequent retest using only closed candles; persist event evidence | Deterministic tests: break, retest, invalidation, no future leakage |
| Fibonacci 50–61.8 | Existing `fibonacci` family in zones | Named anchor swing and configurable retracement band | Tests for both directions, anchor age, no repaint |
| Multi-TF trend and momentum | `services/strategy_engine/analysis.py` M1/M5/M15/H1/H4 | Stochastic K/D on 3 selected frames, stale/missing frame veto | Alignment and conflicting-signal tests |
| Signal lifecycle | Signal score and WAIT/NO_TRADE | SCANNING → WAIT → READY / EXPIRED / BLOCKED, with reason codes | Transition tests; blocked never executes |
| Plan generation | `TradePlan`, risk engine | Up to 3 **proposals** per scan, not 3 automatic positions | Aggregate exposure and per-account limits enforced |
| Visual overlays | Vue 3 Lightweight Charts | Per-layer and per-trade toggles: SR, flip, fib, structure, entry, SL, TP, closed orders | Hiding layers never changes analysis, execution or stored plans |
| Trade snapshots | Journal, persistence, execution IDs | Immutable pre-order original plan, versioned live revisions, fill/deal ledger mapping | Reopen a closed order with its historical levels exactly preserved |
| MT5 execution | Existing gateway, EA bridge, broker adapters | Reconciliation, duplicate protection, partial fill, manual terminal changes | Demo-only disconnect/restart and fail-closed evidence |
| Validation | Existing pytest/backtest/walk-forward | Point-in-time replay, cost-aware OOS, demo forward tests | Documented pass/fail gates, not a win-rate promise |

## Mandatory architecture
1. Analytics and decision services own canonical zone/plan state; chart visibility is a user preference and must not mutate it.
2. A plan has `analysis_id`, `plan_id`, `strategy_version`, `source_timestamp`, `symbol`, `broker_id`, `timeframe`, `zone_ids`, `entry`, `sl`, `tp`, `risk_decision`, and `created_at`.
3. Persist the ORIGINAL plan before order submission. Every subsequent adjustment is a separate append-only event with time and actor. Preserve mapping to broker order, position, and deal IDs, including partial fills.
4. Only completed bars may confirm a swing, break or retest; distinguish candidates from confirmed events. Never use later candles when replaying an earlier decision.
5. LLM may explain but may not send, approve or override orders. Risk veto and account/environment authorization remain independent.
6. Default PAPER/DEMO, full auto disabled. No credentials or trading permission changes in this branch.
7. An unverified broker state, stale feed, news lockout, bad spread, missing contract spec, or duplicate/ambiguous order => block new orders.
8. Show historical levels as they existed at decision time, not levels recalculated from the present.

## Implementation stages
- P0: audit baseline, identify canonical repository/branch, inspect docs conflicts, run existing Windows verification and CI; record evidence.
- P1: extend SR/flip and Fibonacci detection with closed-bar provenance and tests.
- P2: add three-timeframe Stochastic/trend agreement, explicit WAIT/READY/EXPIRED/BLOCKED states and notification events.
- P3: build at most three independent trade proposals with risk-approved lot, stops and net RR; never assume proposals equal executions.
- P4: add Vue chart overlay toggles (global, per-layer, per-order) and keep engine independent.
- P5: add immutable original plan, append-only revisions and historical replay with broker reconciliation.
- P6: harden order lifecycle on MT5 Demo, including partial fills, duplicate commands, restart and manual broker changes.
- P7: test point-in-time correctness, walk-forward OOS on real data, realistic tick/spread/slippage costs, demo forward validation, operator release/rollback.

## Delivery rules
- Small PRs, each with changed-file inventory, tests and limitations.
- Keep `main` unchanged until reviewed.
- Never claim a successful local test, MT5 demo acceptance, or profitability without evidence.
- Preserve current server responsibilities and safety guidance; do not alter Proxmox storage, networking, or trading accounts.
- Before any LIVE enablement: independent explicit operator authorization and verified release gates.

## Merged PR #24: implemented vertical slice
- Pure SR Flip detector, plus read-only confirmed flip readings from **previously confirmed** M5 swing levels (with source swing, breakout and retest timestamps); no extra trading signal or executor action.
- Read-only named Fibonacci 50% / 61.8% band anchored to the last two confirmed opposite M5 swings, with anchor and confirmation timestamps. The existing zone-engine Fibonacci family is not replaced.
- Additive, backward-compatible `AnalysisSnapshot.sr_flips` and `AnalysisSnapshot.fibonacci_band` fields consumed by the dashboard.
- Dashboard chart switches for EMA, S/R, SR Flip, Fibonacci, BOS/CHoCH, signal plan Entry/SL/TP, and current positions. Switches are stored only in browser local storage, not in trading state.
- Closed-trade review displays a **price-level diagram**, separately switchable between the archived linked signal's original Entry/SL/TP and executed Entry/Exit. It never reconstructs missing historical levels from today's data, and is not yet a historical candlestick replay.
- Focused tests cover pre-retest silence, wrong-side and unconfirmed swings, stale Fibonacci anchors, bullish and bearish retracement maths. Dashboard bundle must build.

### Follow-up: readiness and multi-TF visibility
- Added a separate, read-only `Signal.readiness` and `readiness_reason` populated **after** the existing risk gate. `SCANNING`, `WAIT`, `READY`, `BLOCKED`, `EXPIRED` do not grant execution permission, override `Signal.action` or bypass approval.
- Differentiates missing entry conditions (WAIT) from health/news/feed vetoes and rejected actionable plans (BLOCKED). Uses market time to avoid false expiry during historical replay.
- The Signal Card displays readiness and the reason; on an MT5 live feed, it also labels an expired signal immediately and disables the stale approval button.
- An in-app/existing-channel alert is emitted when entering BLOCKED and on meaningful transition back to waiting. Initial idle WAIT and repeated identical states do not generate alerts; existing BUY/SELL alerts remain authoritative for READY. Alert exceptions cannot change the order decision.
- Added closed-bar Stochastic (14,3,3) M5/M15/H1 values to AnalysisSnapshot. Unavailable or stale frames are marked explicitly. The display says this is **observation only**, not a new execution gate.
- Unit tests cover lifecycle precedence, expiry, existing payload compatibility, transition deduplication, indicator lookahead and stale/missing timeframes.

### Follow-up: immutable pre-order evidence (P5 partial)
- `pre_order_snapshots` is appended **in the same SQLite transaction** as the pending idempotency order claim, before any broker send. Snapshot failures roll back the claim and block sending; duplicate-key replays do not overwrite original evidence.
- For engine-created signals, the record freezes the original signal, Entry/SL/TP, broker/account/workspace identity, request details, the immediate pre-send risk-gate verdict/checks (and risk state when serializable), and a signal-ID-pinned analysis: support/resistance zones with original bounds, confirmed flips, Fibonacci anchors and structure. The per-signal analysis cache is bounded; if a restarted process cannot recover that context, it honestly marks analysis `unavailable` instead of borrowing new zones.
- Direct gateway orders that have no engine plan still record an immutable `request_only` snapshot (broker, request, SL/TP) with no fabricated analysis.
- SQLite update/delete guards protect the original records; later adjustments will require a separate append-only revision stream.
- Historical trade review resolves evidence by broker ticket, falling back to an **unambiguous** signal ID, and checks saved broker/account identity. The original evidence takes precedence over later mutable signal rows. The trade details diagram has independent client-only toggles for archived plan levels, fill/exit, S/R zone boundaries, flips, Fibonacci and BOS/CHoCH.
- Tests cover before-send durability, restart/replay invariance, account/plan mismatch veto, failed-insert atomic rollback, schema guards, API historical plan priority and ordinary legacy records. No LIVE or full-auto setting is changed.

### Merged PR #27: further V2 code and audit trail
- Added append-only `position_events` for position-management requests and observed outcomes, read-only open-position review, decision-time archived M5 candles and historical candle chart, up to three **non-executable** aggregate-budgeted zone-entry scenarios, and restart-aware readiness deduplication. Broker position-ID-matched deals remain labelled as incomplete attribution.
- A standalone `scripts/smart_entry_release_gate.py` checks real-data provenance, four walk-forward folds, tick-level ordering, two higher-cost stress reports and 14-day/30-trade MT5 Demo evidence. It cannot enable LIVE; examples and an operator checklist are in `docs/SMART-ENTRY-V2-ACCEPTANCE.md`.

### Merged PR #28: reconciliation follow-up (not MT5 Demo verified)
- Persist broker-position observations for changes in SL/TP, volume, direction or entry, unexpected positions, missing positions and restoration to the old baseline. Identical polls have no duplicate event across restarts/processes; events are append-only and tied to the original account/workspace identity.
- Management-cycle mirror updates now archive discrepancies and preserve unverified baselines **atomically in one SQLite transaction** before accepting that cycle's cache; rejection cannot silently overwrite the earlier local evidence.
- No broker observation is labelled as a verified fill or as a human-originated manual change. A broker-missing position keeps its last baseline pending authoritative history. New observations trigger in-app/configured-channel alerts without changing execution permissions or the original plan.

### Merged PR #30: historical-candle follow-up (not MT5 Demo verified)
- Archive M5 candles **only after closing** and **only while an owned management cycle actually sees a matching open position and cached analysis for its symbol** (also covering non-active symbols when their own analysis remains available). Ignore pre-entry, unfinished, future, and >15-minute-old bars; never fill missing periods from a later download. Save observations per broker/ticket/workspace/account with a source and feed-mode label. Append-only SQLite guards preserve the original bars; a conflicting duplicate fails that position's archive transaction.
- Read-only open/closed order reviews now serve the observed post-entry bars alongside the original frozen decision-time bars. The dashboard colors observed post-entry bars purple and warns that missing periods are unobserved; all historical S/R, flips, Fibonacci, structure and Entry/SL/TP layer switches remain visual-only.
- Broker deal matching by order ID now counts `entry=in` fragments only. Close (`out`, `out_by`) and netting-reversal (`inout`) fragments cannot inflate a purported opening fill; their presence is reported as ambiguous, never automatically resolved. A complete broker-side reconciliation still needs independent Demo evidence.
- Archive errors are logged and cannot block protective position management. Position polling and a fresh matching analysis context are required: the archive is **partial**, can have gaps during downtime or when no matching context is refreshed, and is **not** MT5 tick-history proof. The `complete`/`broker_tick_verified` flags remain false.

### Conservative unknown-order recovery follow-up (proposed, not MT5 Demo verified)
- If the broker send timed out or lost its acknowledgment, a position with matching `signal_id`, side, symbol, and volume is only a **candidate**, not proof that this No7 order was executed. Even a single identical position might already have existed before the send. Remove automatic promotion of such a pending claim to `filled/verified` on retry/restart.
- Keep the persisted claim pending, reject new keys while unresolved, and never resend automatically. Confirm execution ownership only through a separately implemented, independently reviewed broker order/deal ID reconciliation process. Immediate broker acknowledgements still use the existing explicit fill-verification path; this change does **not** introduce an approval/auto-fill endpoint.
- Regression tests cover a matching foreign pre-existing position, a broker fill followed by lost receipt, and process restart; software tests do not certify a real MT5 disconnection scenario.
- Broker Manager displays an explicit WAIT warning with unresolved claim IDs from the already account-scoped live state; the warning is read-only and cannot acknowledge, resend or clear an order.
- If `verify_after_send` is disabled, a broker adapter's optimistic `verified` flag is forcibly downgraded for `filled` orders. The claim remains unresolved and blocks further orders rather than treating the missing local check as evidence.

### What is still missing / not production-ready
- Still missing: independent validation of optional Stochastic veto, **complete broker-verified post-entry candle replay** (especially restart/offline intervals and symbols without cached analysis), attested broker-deal and partial-fill mapping, deterministic terminal disconnection/manual-change acceptance, target-host restore testing, and real-market forward evidence. No terminal access or real dataset was provided in this development environment. **Do not claim LIVE readiness.**

Do not interpret a displayed flip or Fibonacci band as a validated entry. Hidden chart lines have no influence on signal evaluation, risk checks or execution.
