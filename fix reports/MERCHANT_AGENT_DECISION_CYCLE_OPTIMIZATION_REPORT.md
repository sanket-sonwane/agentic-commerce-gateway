# MERCHANT AGENT DECISION-CYCLE OPTIMIZATION REPORT

**Scope:** Merchant Agent loop (ACG project) — `merchant/agent/loop.py`, `merchant/agent/tools.py`, new `merchant/tests/test_decision_loop.py`.
**Date:** 2026-08-30
**Status:** Implemented, verified (157 tests green), reported.

---

## 1. Executive Summary

The Merchant Agent's runtime loop previously invoked the LLM brain on **every** iteration, including the turns whose correct action is mathematically determined by the preceding tool envelope (e.g. the `ready_for_completion` handoff after a policy-clean checkout; the gated completion of an `agent_completed` checkout; the next-ranked alternative after a buyer rejection).

This optimization converted the loop into a **decision-driven adaptive loop**:

- The loop now checks a **decision point** before consulting the brain. When the last tool result determines the next domain action unambiguously, that action is executed deterministically — the brain is not asked.
- **Deterministic rewind for reads**: identical re-reads in an unchanged world are **replayed** from an observation cache instead of re-executed; repeated identical re-requests escalate to `BLOCKED_REPEAT`.
- **Real call budget**: the tool-call counter (previously never incremented anywhere — an inert constant checked against `ma_max_tool_calls`) now counts every executed call, making `ma_max_tool_calls` a real ceiling.
- **Adaptation is preserved, not collapsed**: `STOCK_UNAVAILABLE` / price failures at the money ladder still fall through to multi-cycle LLM deliberation; only *policy-clean* completions become deterministic. Payment truth, the gated complete ladder, audit hashing, and every guardrail stay intact.

Result (tool turns removed, deterministic fallback-brain harness; "brain call" ≈ one `_next_action` round ≈ one LLM round under the real brain):

| Scenario (agent_completed unless noted) | Before | After | Saved |
|---|---|---|---|
| `buyer_completes`, pre-validated requested item | 3 | 2 | 1 |
| `buyer_completes`, create-first model turn | 2 | 1 | 1 |
| agent_completed, pre-validated requested item | 3 | 2 | 1 |
| agent_completed, search → inventory → create | 4 | 3 | 1 |
| substitution REJECT → next-ranked proposal | 1–2 | **0** | 1–2 |
| substitution ACCEPT → complete | 1 | **0** | 1 |

Latency is **not** measured or claimed (see §16).

## 2. Method

- **Measurable unit:** number of `_next_action` invocations (brain rounds). Each round is one LLM call in a live LLM-brain session; a per-session `llm_call_count` counter is also maintained (`loop.py:230`) for observability.
- **Harness:** the existing guardrail suite pattern — `MA_LLM_BRAIN=false` (deterministic brain), with a **recording wrapper** around `_next_action` that records `st.status` per invocation. The *after* counts in this report are the literal assertions of `merchant/tests/test_decision_loop.py`.
- **Before counts** are the turn-counts of the pre-change control flow for the same scenario (derivable deterministically from the pre-change code: each post-tool transition required a fresh `_next_action` round).
- **Non-goals:** latency, tokens, cost. No timing instrumentation exists in the codebase; nothing here claims wall-clock improvement.

## 3. Baseline (before): fixed iterative loop

Every loop iteration called `_next_action` regardless of what the previous tool envelope just proved. Concretely:

- After a successful `create_checkout`, the next iteration still called the brain to "decide" the handoff (`finish ready_for_completion` / `request_completion`).
- After the buyer rejected a proposal, the brain was re-invoked to re-search and re-derive the next candidate from scratch, even though the ranked candidate list had already been produced.
- Identical read tools (`check_inventory`, `search_catalog`, `get_current_price`) re-hit the DB every round.
- `tool_call_count` was initialized, surfaced in API output, and compared to `ma_max_tool_calls` — but **never incremented anywhere**, so the budget was inert.

## 4. Design after: decision-driven adaptive loop

The loop (run → resume) now gates each brain round behind a deterministic decision point:

1. **`_decision_point(state)`** (`loop.py:731`) — consumes `fallback_ctx["pending_continuation"]`. If a live policy-clean checkout's last envelope was `create_checkout` / `update_checkout` / `terms_confirmed` success:
   - `buyer_completes` → deterministic `_apply_finish(ready_for_completion, basket_ready_for_buyer_payment)`.
   - `agent_completed` → deterministic gated `request_completion` via the same money ladder.
2. **`_observe_or_execute(...)`** (`loop.py:787`) — for read tools only, consults module-level `_maybe_replay_observation` first (replay path, §10); executed path audits `agent_{name}`; replay path audits `agent_{name}_replayed`.
3. **`_continue_after_rejection(...)`** (`loop.py:856`) — after a buyer REJECT, proposes the **next-ranked candidate from the already-observed `find_alternative` list**, respecting `excluded_pack_ids` ∪ `offered_pack_ids`. The brain is consulted only when the observed list is exhausted/absent.

## 5. Outcome table (turns removed)

Covered in §1. Additional per-scenario after-measurements enforced by tests:

- `buyer_completes` handoff: `test_buyer_completes_create_short_circuits_to_ready` (2 calls), `test_buyer_completes_single_brain_call_when_prechecked` (1 call).
- `agent_completed` auto-complete: `test_agent_completed_auto_completes_live_checkout` (2 calls, `tool_call_count == 3`), `test_agent_completed_search_flow_auto_completes` (3 calls).
- reject seam: `test_buyer_rejection_proposes_next_ranked_without_brain` (**0 calls**), `test_accepted_alternative_still_completes_via_seam` (**0 calls**).

## 6. Seam A — buyer_completes handoff

After a loop-executed `create_checkout` succeeds in `buyer_completes` mode, the buyer-side contract is fully determined: request payment. The loop now performs this deterministically on the next turn instead of asking the model. Guardrails unchanged: in `buyer_completes` the model cannot trigger payment-worthy actions; no money moves (handoff hands the session to the buyer for payment).

## 7. Seam B — agent_completed gated completion

After a loop-executed `create_checkout` succeeds in `agent_completed` mode, the merchant now runs the **same gated `request_completion`** ladder deterministically (`test_agent_completed_auto_completes_live_checkout`: status `completed`, `reason_code == fulfilled_after_gated_completion`, `payment_id` present, audit `agent_request_completion` present). The completion is still performed by `checkout_service.complete_checkout` (unchanged): authorization check, price binding, stock deduction, capture.

## 8. Seam C — substitution reject → next-ranked candidate

The observable-ranked `find_alternative` envelope is reused after a REJECT. `excluded_pack_ids` (buyer-rejected packs, now seeded from `rejected_proposals`) and `offered_pack_ids` (already-proposed, never re-proposed) guarantee monotonic progress. The brain is **never** invoked for the first follow-up proposal (`test_buyer_rejection_proposes_next_ranked_without_brain`: 0 calls; rejected pack in `excluded_pack_ids`, different pack proposed). When the observed list is exhausted, the seam yields to the adaptive cycle which declines gracefully with `no_valid_alternative` (`test_reject_exhausts_observed_candidates_then_declines`) — no livelock.

## 9. Seam D — accept / confirm continuation

`_accept_substitution` (`loop.py:1105`) and `_confirm_terms` (`loop.py:1148`) create/confirm the checkout and set `pending_continuation`; the decision point then drives the deterministic completion. Accepting a seam-proposed alternative completes with **0** further brain calls (`test_accepted_alternative_still_completes_via_seam`).

`pending_continuation` is set **only** by: (a) loop-executed `create_checkout` success (`loop.py:664`), (b) accepted substitution (`loop.py:1125`), (c) confirmed terms (`loop.py:1175`). It is never retrofitted onto pre-seeded history or model-driven `update_checkout` — this is what keeps the guardrail suites intact.

## 10. Duplicate-action prevention

- `canonical_args(args)` (`tools.py:73`) — recursive, order-insensitive JSON key, so `get_product({"product_id":"x","ext":"y"})` and its key-shuffled twin map to the same observation.
- Every executed tool now appends its envelope to `state.tool_results` (was loop-side-only before) and bumps `world_version` on writes (`tools.py:912`).
- Read successes are memoized in `observe_map` keyed `f"{name}|{canonical_args(args)}"` with the **world version at observation time** (`tools.py:914`).
- `_maybe_replay_observation` (`loop.py:538`) replays only if `name` is a read tool AND the world is unchanged; any intervening write forces a fresh read (safe over-invalidation, `test_duplicate_read_after_write_is_not_replayed`).
- First identical re-request replays (`agent_{name}_replayed`, decision `approved`); the second escalates to `BLOCKED_REPEAT` (`agent_{name}_replayed`, decision `blocked`) — the brain cannot spin on duplicate observations (`test_identical_repeat_escalates_to_blocked_repeat`).
- Budget still honored on replays (`tool_call_count` incremented in the replay path, `loop.py:825`).

## 11. Discovered latent bug (fixed in scope): inert `tool_call_count`

No code path ever incremented `tool_call_count`. The budget check (`tool_call_count >= ma_max_tool_calls` → `TOOL_BUDGET_EXHAUSTED`) was unreachable in practice and the API's `tool_usage` figure was always 0. Now: every executed call counts (success or error) in `execute_tool`, and replayed/blocked reads count in the replay path. `test_tool_budget_now_binds_real_calls` proves the ceiling triggers (2 budgeted → 3rd call → `TOOL_BUDGET_EXHAUSTED`). This is a discovered correctness restoral, not a behavior change to payment flow.

## 12. Safety invariants preserved (verified by full suite)

- **Money truth untouched:** `checkout_service.complete_checkout` and the payment finalizer are unchanged; deterministic completion runs the same authorization/amount-binding/capture.
- **Guardrails intact:** 143 pre-existing tests still green, including `test_wrong_mode_ready_for_completion_held_then_request_completion`, `test_mode_hold_honors_second_decision_if_model_insists`, `test_finish_completed_without_payment_held_then_downgraded`, `test_decline_with_live_checkout_held_once_then_honored`, `test_fake_run_gating_id_rejected_and_audited_via_loop`, AP2 provenance/C-1/C-2/C-3 suites, `test_c2_c3_state_security`.
- **Hard failures not swallowed:** a deterministic *pending* outcome (payment not captured) still terminates honestly as `declined / payment_pending` — no false `completed` (`test_agent_completed_payment_pending_still_terminates_honestly`).
- **API surface / schemas / DB / config defaults:** unchanged. `ma_max_iterations`, `ma_max_tool_calls`, `ma_llm_brain` defaults untouched.

## 13. Audit-chain additions

- New replay events: `agent_{name}_replayed` with `decision: approved | blocked` (through the same `audit_service` hash chain).
- Existing executed-tool events unchanged; deterministic completion still emits `agent_request_completion`.
- `_transaction_state_note` now includes the resolved live basket (`current basket: {pack} xN | none yet`) so the model never re-derives it (STEP-11-minimal context change; no transcript redesign).

## 14. Tests

`python -m pytest merchant/tests -q` → **157 passed** (143 pre-existing + 14 new, 4.80s).

New `merchant/tests/test_decision_loop.py`:

| # | Test | Seam |
|---|---|---|
| 1 | `test_buyer_completes_create_short_circuits_to_ready` | A — 2 calls |
| 2 | `test_buyer_completes_single_brain_call_when_prechecked` | A — 1 call (create-first) |
| 3 | `test_agent_completed_auto_completes_live_checkout` | B — money ladder deterministic |
| 4 | `test_agent_completed_search_flow_auto_completes` | B — search flow, 3 calls |
| 5 | `test_stock_failure_at_auto_completion_keeps_adaptive_loop` | B — adaptation preserved |
| 6 | `test_agent_completed_payment_pending_still_terminates_honestly` | B — hard failure honest |
| 7 | `test_buyer_rejection_proposes_next_ranked_without_brain` | C — 0 calls |
| 8 | `test_reject_exhausts_observed_candidates_then_declines` | C — graceful decline |
| 9 | `test_accepted_alternative_still_completes_via_seam` | D — 0 calls |
| 10 | `test_duplicate_read_in_loop_is_replayed_not_reexecuted` | replay — 1 exec, 1 replay |
| 11 | `test_duplicate_read_after_write_is_not_replayed` | replay — write invalidates |
| 12 | `test_identical_repeat_escalates_to_blocked_repeat` | replay — BLOCKED_REPEAT |
| 13 | `test_tool_budget_now_binds_real_calls` | budget — real ceiling |
| 14 | `test_current_basket_note_in_transaction_state` | context — basket line |

## 15. Files changed (this optimization)

- `merchant/agent/loop.py` (+301/−21): decision point, observe-or-execute, reject continuation, replay, counters, basket-note context.
- `merchant/agent/tools.py` (+32): `canonical_args`, real `tool_call_count` increment, `tool_results` append, `observe_map`/`world_version` bookkeeping.
- `merchant/tests/test_decision_loop.py` (new, +260): 14 tests above.

`merchant/domain/checkout_service.py` shows a pre-existing modification from the C-1/C-2/C-3 session; **not** part of this delta and intentionally untouched here.

## 16. What this does NOT claim

- **No latency numbers.** No timer instrumentation exists; "faster" is not asserted anywhere.
- **No token/cost figures.** Removed brain rounds in the deterministic harness strongly imply fewer LLM calls under `ma_llm_brain=true`, but the live brain's round-trips are not measured here.
- **Not a rework of the buyer side.** `buyer_service/*`, hidden discovery parser, streaming, prompt redesign are out of scope.
- **Deterministic shorthand does not bypass policy.** Only *policy-clean* transitions become deterministic; every failure path still adapts.

## 17. Remaining performance problems (deferred)

- **Buyer-agent causality (BUY-3, see `docs/audits/BUYER_AGENT_AUDIT.md`):** the buyer-side loop still reasons on inferred basket semantics; a future change should align buyer loop with the same observe-or-execute replay cache.
- The hidden discovery parser's resolution/expiration logic and the `awaiting_authorization` window retry strategy are untouched.
- Streaming/stream-first transcripts remain a roadmap item; the `llm_call_count` counter (§2) is the hook for future call-time telemetry, which would enable honest latency measurement.

## 18. Conclusion

The Merchant Agent loop is now decision-driven: deterministic seams remove redundant reasoning rounds on the happy paths and the first-rejection path, duplicate reads are replayed/blocked instead of re-executed, and the tool-call budget is now real. Adaptation to recoverable failures, payment truth, and every existing guardrail are preserved and covered by 157 green tests. Deferred items (§17) are recorded for the next cycle.

**No commit made — work is in the working tree for review.**