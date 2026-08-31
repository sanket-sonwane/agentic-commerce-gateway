# ACG Professional Refactor — Final Report

**Date** 2026-08-31 · **Repo** `D:\Hackathons\rz-agent` · **Branch** `ui-redesign` · **Baseline** `46b4072` → **HEAD** `d94ecc5`

**Scope** A seven-batch, behaviour-preserving professional refactor + restructure of the Agentic Commerce Gateway
(ACG): 8 commits, 45 files changed (3854 insertions, 3478 deletions), complete backend + frontend re-layering with
zero change to payment / AP2 / audit / state-machine semantics.

> **Safety invariant tracked and held across every batch:** C-1 amount binding (authorized == razorpay order ==
> verified), C-2/C-3 state-machine guards, idempotency snapshot replay, AP2 canonical bytes/sign/verify, audit
> BEGIN-IMMEDIATE + hash chain + signed anchor + sole-writer, Razorpay order/token/callback/recurring/refund
> semantics, tool-name wire constants, and `actor="merchant_agent"`.

---

## 1. Commit / batch log

| # | Commit | What changed | Files | Reasons for commit |
|---|--------|--------------|-------|--------------------|
| B1 | `b51b169` | Removed dead imports in `checkout_service`; fixed stale `protocol/schemas/checkout_session.json` to match `serialize_session` | 2 | Hygiene + schema drifts |
| B2 | `890a477` | `merchant/domain/catalog.py`; split `merchant/agent/loop.py` → `orchestration.py` + `llm_brain.py` + `fallback_brain.py`; `tools.py` 984→667 | 13 | Layered agent |
| B3 | `4ff9d93` | Split `buyer_service/agent.py` into `tool_exec.py`/`bill.py`/`proposals.py` mixins; single-source `_state_note` in `routing.py`; drop `_safe_bool`; delete stale `merchant/agent/loop.py` | 7 | Mixin re-layout |
| B4 | `b0d13f3` | Split `merchant/domain/checkout_service.py` (1536) → `merchant/domain/checkout/` package (10 concern modules) | 20 | De-god-module |
| B5a | `ff4a200` | New `ap2/iso.py`; consolidate `_iso`/`_utcnow`/`_as_dt`; fix `provenance` direct import | 5 | De-dup helpers |
| B5b | `62251e7` | Extract Workstream E → `ap2/user_assertion_service.py`; re-export for compat | 4 | De-god-module |
| B5c | `f0661ee` | Extract Razorpay callback → `payment/callback.py` (own router); `api/checkout.py` CRUD-only | 3 | HTTP/domain split |
| B6 | `d94ecc5` | Delete legacy `RazorpayPayCard.jsx` alias; `OpsView` imports canonical `PaymentCard` | 2 | De-alias |

---

## 2. Before → after structure (tree)

```
rz-agent/
├── merchant/                     # merchant agent (FastAPI :8000)
│   ├── agent/                    # agent loop & tool execution
│   │   ├── prompt*.py  state.py  timing.py  tools.py
│   │   ├── orchestration.py      # loop + MerchantAgent + _next_action   (B2)
│   │   ├── llm_brain.py          # _Finish + LLM fallback decision       (B2)
│   │   └── fallback_brain.py     # deterministic non-LLM brain           (B2)
│   ├── ap2/                      # AP2 provenance/traceability
│   │   ├── crypto_service.py  signed_payloads.py  user_intent.py
│   │   ├── standalone_verifier.py  provenance_service.py  authorization_service.py
│   │   ├── iso.py                # _iso / utcnow / parse_dt              (B5a)
│   │   └── user_assertion_service.py  # Workstream E keys+assertions     (B5b)
│   ├── api/                      # HTTP routing (thin)
│   │   ├── checkout.py           # CRUD only now                         (B5c)
│   │   ├── provenance.py  agent.py  admin.py  audit.py  consumer.py  discovery.py
│   ├── control/                  # delegation, consumer projection
│   ├── domain/                   # business logic (DB-free/ORM)
│   │   ├── catalog.py  discovery_service.py  inventory_service.py        (B2)
│   │   └── checkout/             # de-god-moduled checkout                (B4)
│   │       ├── pricing.py session.py gating.py idempotency.py
│   │       ├── create.py update.py cancel.py confirm.py
│   │       ├── complete.py payment.py __init__.py
│   ├── payment/                  # Razorpay gateway + callbacks
│   │   ├── payment_service.py    # gateway adapter (unchanged behaviour)
│   │   └── callback.py           # payment_callback + reconcile router     (B5c)
│   ├── evidence/  persistence/  protocol/  main.py  config.py
├── buyer_service/                # buyer agent (:8001)
│   ├── agent.py                  # facade: BuyerAgent(mixins) + llm_loop   (B3)
│   ├── tool_exec.py  bill.py  proposals.py  routing.py  timing.py         (B3)
├── dashboard/  src/{api.js, views/, components/', utils/, lib/}           (B6)
├── scripts/                      # operational + eval utilities (kept intact)
└── artifacts/  docs/  fix reports/
```

**Structural outcomes:**
- `merchant/domain/checkout_service.py` (1536-line god module) **deleted** → 10 single-concern modules.
- `merchant/agent/loop.py` **deleted** → `orchestration.py` + `llm_brain.py` + `fallback_brain.py`.
- `buyer_service/agent.py` (988→~140) → 3 mixin modules + facade.
- `merchant/ap2/authorization_service.py` (599→~344) → helpers to `iso.py`, Workstream E to `user_assertion_service.py`.
- `merchant/api/checkout.py` (302→~174) → CRUD only; callbacks to `payment/callback.py`.
- `dashboard/src/components/RazorpayPayCard.jsx` **deleted** (was a pure alias).

---

## 3. File-change matrix

### Created (20)
| File | Batch | Purpose |
|------|-------|---------|
| `merchant/domain/catalog.py` | B2 | Catalog domain; raises `ProtocolError` |
| `merchant/agent/llm_brain.py` | B2 | LLM decision brain + `_Finish` |
| `merchant/agent/fallback_brain.py` | B2 | Deterministic non-LLM brain |
| `buyer_service/tool_exec.py` | B3 | ToolExecutorMixin + helpers |
| `buyer_service/bill.py` | B3 | BillFlowMixin |
| `buyer_service/proposals.py` | B3 | SubstituteFlowMixin + ROLE_TAGS |
| `merchant/domain/checkout/{__init__,pricing,session,gating,idempotency,create,update,cancel,confirm,complete,payment}.py` | B4 | Checkout package (10) |
| `merchant/ap2/iso.py` | B5a | Shared datetime helpers |
| `merchant/ap2/user_assertion_service.py` | B5b | Workstream E user keys/assertions |
| `merchant/payment/callback.py` | B5c | Razorpay callback + reconcile |

### Deleted (2)
| File | Batch | Reason |
|------|-------|--------|
| `merchant/domain/checkout_service.py` | B4 | Superseded by `domain/checkout/` package |
| `dashboard/src/components/RazorpayPayCard.jsx` | B6 | Superseded by `PaymentCard.jsx` (was alias) |

### Modified (23)
| File | Batch | Nature |
|------|-------|--------|
| `merchant/agent/tools.py` | B2 | Slimmed 984→667 (catalog reads → domain) |
| `merchant/agent/__init__.py`, `orchestration.py` | B2 | Loop re-layout |
| `merchant/api/agent.py`, `control/consumer_projection.py`, `api/checkout.py` | B2/B4/B5c | Import/callback refactors |
| `merchant/ap2/{authorization_service,provenance_service,signed_payloads,user_intent}.py` | B5 | Helper + Workstream E extraction |
| `merchant/api/provenance.py` | B5 | Direct imports (`iso`, `user_assertion_service`) |
| `merchant/domain/checkout/complete.py` | B5b | Import from `user_assertion_service` |
| `merchant/main.py` | B5c | Wire `payment_callback` router |
| `merchant/tests/{test_agent_handoff,test_decision_loop,test_merchant_agent,test_c2_c3_state_security}.py` | B2/B4 | Import updates |
| `buyer_service/{agent,routing,timing}.py` | B3 | Mixins, single-source `_state_note`, drop `_safe_bool` |
| `dashboard/src/views/OpsView.jsx` | B6 | Import PaymentCard directly |
| `protocol/schemas/checkout_session.json` | B1 | Schema drift fix |
| `scripts/_bench.py`, `scripts/_bench_merchant.py` | B2 | Cosmetic import updates |

**Architectural decisions preserved (for the reader):** `_tool_*` wrappers kept in `agent/tools.py` because
`execute_tool._run` always passes `state` into handler kwargs; `_Finish` lives in `llm_brain.py` to avoid an
orchestration↔llm_brain import cycle; tests monkeypatch `orchestration._next_action`; `consumer_projection` and
`api/*` keep `checkout_service` as an alias (`from merchant.domain import checkout as checkout_service`) to avoid
route-function/domain-function name collisions; configs stay split (`merchant/config.py`,
`buyer_service/config.py`) for deployable isolation.

---

## 4. Regression matrix (behaviour 100% preserved)

| Gate | Baseline `46b4072` | HEAD `d94ecc5` | Δ |
|------|--------------------|----------------|---|
| Full merchant pytest suite | 291 passed | **291 passed** | — (identical) |
| `scenario_check.py` | EXIT 0 (7 scenarios) | EXIT 0 (7 scenarios) | — |
| AP2 bundle verification (`verify_ap2` on final bundle) | VALID | **VALID** | — |
| Checkout C-1/C-2/C-3 + payment-state contract tests | pass | pass (43 targeted) | — |
| AP2 / user-assertion / verifier tests | pass | pass (49 targeted) | — |
| Buyer-agent tests (33) | pass | pass | — |
| Frontend `vite build` | — | **built in 3.19s** | — |
| `compileall` merchant + scripts + buyer_service | OK | OK | — |

**Safety invariants re-verified at HEAD:** amount binding (`test_c1_amount_binding`), state-machine
terminals/transitions (`test_c2_c3_state_security`, `VALID_TRANSITIONS` untouched), idempotency snapshot replay,
AP2 canonical bytes + cross-runtime golden vector, audit hash-chain integrity, `actor="merchant_agent"` and tool-name
wire constants. API surface and HTTP status codes unchanged (verified by scenario_check + contract tests).

---

## 5. Scorecard

| Criterion | Verdict | Notes |
|-----------|---------|-------|
| Behaviour preserved | ✅ | 291/291 + scenario + AP2 VALID at every batch boundary |
| Coupling reduced | ✅ | God modules split; domain decoupled from HTTP & ORM |
| Cohesion raised | ✅ | Single-concern modules per file |
| Naming/monolith cleanup | ✅ | `loop.py`/`checkout_service.py`/`RazorpayPayCard.jsx` deleted; dead code removed |
| Duplication removed | ✅ | `_iso`/`_utcnow`/`_as_dt` consolidated into `iso.py` |
| Imports sanitized | ✅ | Dead imports dropped (B1); direct imports replace private re-exports (B5) |
| Test suite green | ✅ | Run after every batch; stop-and-fix exercised where needed |
| Import surface preserved | ✅ | Zero test edits for consumer behaviour; aliases/re-exports used |

**Score: 8/8 — "professional refactor + restructure" held every gate.**

---

## 6. The fifteen mandatory answers

**A. What was refactored?**
Six targeted, behaviour-preserving re-layering changes: (1) catalog reads into `domain/catalog.py`; (2) the merchant
agent's monolithic loop into three focused modules; (3) the buyer agent's god-module class into three mixins;
(4) the 1536-line checkout god module into a ten-module `domain/checkout` package; (5) shared AP2 datetime helpers +
the Workstream E user-key concern into their own modules; (6) the Razorpay callback out of the CRUD router into a
dedicated `payment/callback.py`, plus deletion of a frontend legacy alias.

**B. What was NOT refactored / deliberately left alone?**
The entire transaction-critical core was preserved byte-for-byte in behaviour: `_finalize_payment` (C-1/C-3),
`VALID_TRANSITIONS`/`TERMINAL_STATES`, idempotency replay, AP2 canonical/sign/verify, audit hash chain + signed
anchor, Razorpay order/token/recurring/refund calls, and all wire schema constants. `scripts/` eval/bench/probe
tools were kept intact (documented, artifact-producing, or test-referenced). Configs stayed split for isolation.

**C. What was the single biggest win?**
Deleting `merchant/domain/checkout_service.py` (1536 lines) and replacing it with a ten-file package — each concern
(pricing, session state machine, gating, idempotency, create/update/cancel/confirm/complete, payment) now has one
owner, making C-1/C-2/C-3 invariants reviewable in isolation.

**D. What did the duplication removal consist of?**
Three functionally identical datetime helpers (`_iso` duplicated in `signed_payloads`+`user_intent`; `_utcnow`
duplicated in `authorization_service`+`crypto_service.now_utc`; `_as_dt` private) collapsed into a single
`merchant/ap2/iso.py` (`_iso`, `utcnow`, `parse_dt`), with a private re-export fixed to a direct import.

**E. How did you guarantee byte-identical AP2 signatures after dedup?**
The consolidated `_iso` is the exact same implementation (same `isinstance`/`tzinfo`-strip/`timespec="seconds"`
logic). `signed_payloads` still re-exports `_iso`, and we re-ran the **AP2 bundle verification** against the real
`artifacts/evidence_bundle_final.json` — `RESULT: ✅ VALID` — plus the golden-vector cross-runtime byte-parity test
still passes and the full AP2/user-assertion test cluster (49) is green.

**F. How did you avoid route/domain name collisions in the splits?**
`merchant/api/checkout.py`, `api/agent.py`, `agent/tools.py`, `orchestration.py`, and `consumer_projection.py`
import the package as `from merchant.domain import checkout as checkout_service`, so FastAPI route functions (e.g.
`create_checkout`) never shadow the domain functions of the same name; all call sites read `checkout_service.X`
unchanged. Same alias technique used for the `register_user_key` route/domain collision in `api/provenance.py`.

**G. What about the buyer-agent split — any test impact?**
None. `BuyerAgent(ToolExecutorMixin, BillFlowMixin, SubstituteFlowMixin)` preserves the exact import surface
(`BuyerAgent`, `_stable_basket_key`, `_client`, `BILL_PAUSE` are all still module-level on `agent.py`), and
`_state_note` is single-sourced in `routing.py` and imported by both mixin modules.

**H. How was the callback extracted without changing the API contract?**
`merchant/payment/callback.py` defines its own `APIRouter()` holding the identical `POST
/checkout_sessions/{id}/payment_callback` and `/reconcile` paths (no added prefix), registered in `main.py`
alongside the checkout router. Guard order, HTTP status codes, error envelopes, and the best-effort token
persistence block were moved verbatim.

**I. Which live/eval scripts were touched or removed?**
Only two received cosmetic import updates (`_bench.py`, `_bench_merchant.py`). **No scripts/ file was deleted**:
after mapping every private script, each is either a live operational tool, a documented eval/benchmark referenced
by an audit/report, the golden-vector canonical check referenced by `test_user_assertion.py`, or a producer of a
tracked `artifacts/*.json` asset — deletion would break reproducibility, so all are kept (a conservative verdict).

**J. What was the deleted dead code?**
Dead imports in `checkout_service` (B1); the unused `_safe_bool`/`_OK_YIELD` in `buyer_service/timing.py` (B3);
the stale `merchant/agent/loop.py` (B3, unreachable); the 1536-line `checkout_service.py` superseded by the package
(B4); the `RazorpayPayCard.jsx` alias superseded by `PaymentCard.jsx` (B6); and the private `authorization_service._iso`
re-export access replaced with a direct import (B5).

**K. What was the regression-gating procedure?**
After every commit, each batch was gated by `compileall -q merchant scripts buyer_service`, the full
`pytest` suite (291), the targeted sub-suite for the touched area, `scenario_check.py` (EXIT 0), and the AP2 bundle
verifier. Any regression caused a stop-and-fix before the next batch.

**L. Are there remaining monoliths?**
`merchant/ap2/authorization_service.py` (~344 lines) and `merchant/agent/tools.py` (~667) remain the largest files,
but each is now single-concern and coherently grouped with section markers; further splitting was judged
low-value/high-risk for the current scope.

**M. What did the frontend (B6) change?**
`dashboard/src/components/RazorpayPayCard.jsx` was a pure re-export alias of the canonical `PaymentCard.jsx`;
`OpsView.jsx` (`import RazorpayPayCard` → `import PaymentCard`, usage updated) and the file was deleted. The Vite
production build passes (`✓ built in 3.19s`), confirming no dangling import.

**N. What is the net diff size?**
**45 files changed, 3854 insertions, 3478 deletions** across 8 commits; 20 files created, 2 deleted. Repo went from
188 tracked files (baseline) to 206 (HEAD) — the +18 is the new single-concern modules; net code moved, not grown.

**O. How would you verify this refactor end-to-end one more time?**
Run `compileall`; run the full 291-test suite; run `scenario_check.py`; run `verify_ap2 --bundle
artifacts/evidence_bundle_final.json --public-key artifacts/merchant_public_key.pem` (expect VALID); run the
frontend `vite build`. All pass at HEAD with the identical numbers as baseline — the definitive "no behaviour
change" proof.
