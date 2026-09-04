# Agentic Commerce Gateway (ACG)

> **A policy-controlled agentic commerce gateway that enables independent AI buyers to
> discover, negotiate checkout state, and complete real test-mode payments with a grocery
> merchant — without giving any LLM direct control over money.**

ACG is a reference implementation of an ACP-inspired commerce protocol. It pairs an
**LLM Buyer Agent** (represents a human shopper) with an **LLM Merchant Agent** (observes
merchant reality, reasons about fulfillment, adapts) behind a **deterministic control
plane** that is the sole authority over money. An unknown, third-party buyer can transact
against the merchant purely through the published protocol — no internal knowledge, no
shared code — and every step is gated, signed, audited, and independently verifiable.

```
User ⇄ Chat UI ──▶ BUYER AGENT (Groq tool-loop) ──Commerce Protocol──▶ MERCHANT AGENT (Groq tool-loop)
                  intent · budget · negotiation                      observe → reason → act → adapt
                          │ [human approves bill]                              │ tool registry (12 allowlisted tools)
                          ▼                                                    ▼
                  "✅ Order placed! Paid ₹629"                    DETERMINISTIC CONTROL PLANE
                                                                  gating · reservations · state machine
                                                                  idempotency ──▶ RAZORPAY ──▶ AUDIT CHAIN
                                                                  Merchant Ops view ◀── agent decision trace
```

---

## Problem

AI agents are starting to act on behalf of people, but letting an LLM authorize or move
money is unsafe. **How can an independent AI buyer complete a real purchase with a
merchant while no LLM — and no untrusted third party — is ever in control of the money?
And how can an unknown buyer be trusted to transact despite the merchant never having
seen it before?**

ACG answers both: the buyer and merchant agents negotiate; a deterministic control plane
gates and executes; and a certified protocol + signed, verifiable artifacts let strangers
transact safely.

---

## Architecture: three services, one control plane

| Service | Role | Stack |
|---|---|---|
| `buyer_service/` | Chat backend: LLM agent represents user intent, negotiates proposals | Groq tool-loop (:8001) |
| `merchant/` | Merchant: discovery, checkout state machine, gating, payments, audit | FastAPI (:8000) |
| `dashboard/` | React frontend: Chat view + Merchant Ops view (agent trace, money story) | Vite (:5173) |

The separation is deliberate:

- **LLM Buyer Agent** reasons about *what the user wants* and negotiates.
- **LLM Merchant Agent** reasons about *what the merchant can fulfill* (substitutions,
  revalidation, bounded autonomy) and proposes or declines.
- **Deterministic control plane** (gating service, reservation service, checkout state
  machine, payment service) is the only code that can move money. **No LLM output can
  authorize a payment.**

| Decision | Actor |
|---|---|
| "What should we try?" — discovery, alternatives, proposals | Merchant Agent (LLM) |
| "What are we allowed to do?" — spend/quantity/category/stock/state | Deterministic gate (never LLM) |
| "Execute the authorized payment." | Razorpay via the payment service |

---

## How an unknown AI buyer transacts

1. The buyer fetches the merchant's **capability manifest** at `/.well-known/merchant`
   and the machine-readable **schemas** in `protocol/schemas/`.
2. It calls `/discover` with a natural-language request; the merchant returns a structured
   basket proposal priced from the catalog (agents can never submit prices).
3. It creates a checkout session via `/checkout_sessions`; deterministic **gating** runs
   (delegation expiry, category allow-list, quantity cap, spend ceiling, stock).
4. The human **Approve**s in the UI, which signs a **UserIntentAssertion** with the
   user's own Ed25519 key (private key never leaves the device).
5. Completion requires that assertion and runs a deterministic revalidation ladder before
   any money moves: authorization → Razorpay order → verified, **fail-closed on amount
   mismatch** (C-1).
6. The payment is executed and the result recorded. Because it is **idempotent and
   append-only audited**, an unknown buyer's successful purchase is reproducible and
   provable.

The independent `buyer_b/` client — zero LLM, zero merchant internals — completes the full
cycle through the protocol alone, proving the merchant is implemented against the contract,
not hardwired to one buyer.

---

## Why the money is safe

- **No LLM output moves money.** `request_completion` only invokes the deterministic
  ladder; Razorpay credentials never leave the payment service.
- **Server-authoritative pricing.** Agents/buyers can never submit prices; proposals are
  priced from the catalog at confirmation time.
- **Deterministic gating executes before payment**: spend limit, stock, category
  allow-list, delegation expiry, quantity cap.
- **Final revalidation immediately before payment** (TOCTOU-proof).
- **C-1 amount binding**: the authorized amount must equal the Razorpay order amount which
  must equal the verified/settled amount — mismatch fails closed, nothing is charged.
- **C-2/C-3 state security**: terminal states (completed/declined/cancelled) never
  re-charge; the payment path is guarded by the state machine at every transition.
- **Idempotent completion** (`Idempotency-Key` + snapshot replay) — a retried payment
  cannot double-charge.
- **Graceful failure**: if inventory changes or a limit is exceeded, the checkout is
  declined with a stable error code and *Razorpay is never called*; the agent then adapts
  with a legitimate alternative.

---

## Deterministic control plane

The control plane is the **money authority** and is pure, deterministic Python:

- `merchant/control/gating_service.py` — policy checks (spend, category, quantity, expiry, stock)
- `merchant/domain/checkout/` — checkout state machine (create/update/cancel/confirm/complete/payment)
- `merchant/domain/order.py` + `fulfillment` — durable order + fulfillment binding
- `merchant/payment/` — Razorpay integration, callback re-verification, reconciliation
- `merchant/evidence/audit_service.py` — append-only hash-chained audit trail
- `merchant/ap2/` — AP2-inspired signed artifacts + user intent assertions
- `merchant/domain/reconcile_worker.py` — async settlement reconciliation

Gating rules always run; the LLM can neither bypass them nor override their outcome.

---

## User authorization & the audit trail

- **UserIntentAssertion (Ed25519):** on approval the user signs intent with their own key.
  Completing checkout requires a verified, single-use assertion bound to the session,
  delegation, merchant, amount ceiling, and freshness. Single-use (nonce) prevents replay.
- **AP2 artifacts:** Execution Mandate, Policy Decision, Decision Receipt and Intent
  Mandate are signed and cross-bound. They are verifiable **offline** by a standalone
  verifier with only the public key — `merchant/ap2/standalone_verifier.py`.
- **Audit chain:** every event is appended to a SHA-256 hash-chained, integrity-verifiable
  log. The Ops view reconstructs a full agent decision trace from audit facts.

---

## Certificate for unknown buyers

`cert_unknown_buyer/` is a pure-HTTP, machine-checkable (MAC) interop client. With only
the manifest + schemas it validates live merchant responses against the published JSON
schemas and exercises the full discover → checkout → complete → order → provenance cycle
an unknown buyer can run — pinning that a real purchase certifies cleanly and that
contract deviations are reported as structured failures, not crashes.

Tested in `merchant/tests/test_unknown_buyer_cert.py`.

---

## Quick start

```powershell
# 1. Environment
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
Copy-Item .env.example .env   # then fill in your keys (see below)

# 2. Seed the catalog + delegations
.venv\Scripts\python -m scripts.seed_catalog

# 3. Run the three services (separate terminals)
.venv\Scripts\python -m uvicorn merchant.main:app --port 8000   # merchant
.venv\Scripts\python -m buyer_service.main                       # buyer/chat backend :8001
cd dashboard; npm install; npm run dev                           # UI :5173
```

Open **http://localhost:5173**

- **Shop tab** — chat with the AI buyer. Try *"Buy 1L Amul milk"* (zero its stock in the
  Ops console first to see the merchant agent negotiate a substitution) or
  *"Buy ingredients for chicken biryani for four people under ₹800"*. The buyer negotiates
  with the Merchant Agent, then you **Approve** the bill → payment runs.
- **Merchant Ops tab** — judge-facing observability: event timeline, per-rule policy
  panel, Razorpay invocation proof, plain-English explanation, audit hash-chain verify,
  operator console (zero-out stock live, reset demo), and the **Agent tab** rendering the
  Merchant Agent's full decision trace.

### `.env` keys needed

| Key | Where to get it |
|---|---|
| `LLM_API_KEY` | console.groq.com → API Keys — `gsk_…` (models: `openai/gpt-oss-120b`, `openai/gpt-oss-20b`) |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | Razorpay Dashboard → Settings → API Keys → **Test** mode (`rzp_test_…`) |

> `ADMIN_TOKEN=admin_demo_token` in `.env.example` is a **demo-only** credential for the
> local Ops console. It is not a real secret.

### Payment drivers (`PAYMENT_DRIVER`)

| Driver | Behavior | Dashboard-visible payments? |
|---|---|---|
| `saved` **(default & only recommended)** | **Headless after the first order** via Razorpay's official Recurring Payments APIs. Every order is an authorization-transaction order (`customer_id` + `method:card` + `token{max_amount, expire_at, frequency}`). The first payment opens Checkout once with `recurring: true` — pay with mandate-capable test card `4111 1111 1111 1111` (any future expiry, any CVV, OTP any 4 digits; **prepaid cards cannot register mandates**). Registers an E-Mandate token; later orders charge server-side with zero UI. If a stored token proves non-chargeable, audit records `token_needs_reauth` and the next order re-opens the window once to re-register. Falls back to one-tap mode if mandate registration is refused. | real |
| `checkout` | Real Checkout modal for every order (no card saving). | real |

> Simulated payments were removed permanently: the app refuses to start with any other
> `PAYMENT_DRIVER`. A stale env var can never again serve a fake "paid" confirmation.
> (Simulation survives only inside pytest via an explicit test flag.)

---

## Running the tests

```powershell
# Merchant backend (unit + integration: ~337 tests)
.venv\Scripts\python -m pytest merchant/tests -q

# Frontend (unit)
cd dashboard; npm test

# Frontend build
cd dashboard; npm run build
```

The suite covers: checkout flow, orders, fulfillment binding, gating matrix, idempotency,
TOCTOU, TTL, price-change confirmation, C-1 amount binding, C-2/C-3 state security,
UserIntentAssertion verification (single-use), standalone AP2 verification, provenance,
audit tamper detection, audit concurrency, Unknown Buyer certification, Buyer-B
interoperability, consumer projection, inventory + reservation lifecycle, settlement /
reconciliation, and the merchant agent loop (including bounded-autonomy decline).

---

## Demo flow

1. **Shop tab:** *"Buy ingredients for chicken biryani for four people under ₹800"* → bill
   card → **Approve**.
2. **Ops tab:** watch gating checks pass, reservation created.
3. **Operator console:** set `Fresh Chicken Breast (500g)` stock to **0** *(or run
   `python -m scripts.simulate_stock_failure`)*.
4. If you hadn't approved yet, do it now → completion **revalidates → declined,
   STOCK_UNAVAILABLE, Razorpay NOT called** (visible in the Payment panel).
5. The agent adapts in chat: finds paneer, proposes a new bill → approve → **completed**.
6. **Ops tab:** full timeline; run **Verify hash chain** → **VERIFIED**.

Adversarial: `python -m scripts.scenario_check` — a 50-unit over-budget attempt is
rejected by the deterministic spend-limit rule before any gateway call.

---

## Interoperability (independent deterministic client)

```powershell
.venv\Scripts\python -m buyer_b.client --query "milk bread and eggs" --budget 300
```

A deterministic client with zero LLM and zero merchant internals completes a purchase
through the published protocol alone — proving the merchant is implemented against the
contract, not hardwired to Buyer A.

## Unknown-Buyer certificate

```powershell
.venv\Scripts\python -m cert_unknown_buyer.client
```

Runs the machine-checkable certificate: reads the manifest, validates live responses
against `protocol/schemas/`, and exercises the discover → checkout → complete → order →
provenance cycle as an unknown buyer.

---

## Repository layout

```
protocol/              published contract: spec + JSON schemas
cert_unknown_buyer/    machine-checkable cert client (unknown third-party buyer)
merchant/              FastAPI merchant: discovery, checkout state machine, gating,
                       inventory/reservations, payments, audit chain, explanations
merchant/agent/        Merchant Agent runtime: tool registry (12 allowlisted tools),
                       observe→reason→act→adapt loop, LLM brain + deterministic fallback,
                       transaction-scoped state, per-iteration audit events
merchant/ap2/          AP2-inspired signed artifacts: execution/policy/decision/intent,
                       user assertion service, standalone offline verifier
merchant/control/      deterministic control plane: gating, delegation, explain, projection
merchant/domain/       checkout package, order + fulfillment, catalog, reconcile worker
merchant/payment/      Razorpay integration: payment service, callback re-verify, reconcile
merchant/persistence/  SQLite entities + DTOs
buyer_service/         chat backend: Groq tool-loop, merchant-agent negotiation
                       (proposal evaluation + accept/reject), approval gate, SSE events
buyer_b/               independent deterministic protocol client
dashboard/             React+Vite: Chat view + Merchant Ops view (+ Agent decision trace)
scripts/               seed / reset / simulate failure / scenario checks / agent demo
merchant/tests/        ~337 pytest cases (see above)
docs/                  consumer provenance + standalone AP2 verifier documentation
artifacts/             reproducible evidence: evidence bundle, public key, final report
```

---

## What is implemented, demonstrated, and out of scope

**Implemented (source of truth is `merchant/`, `buyer_service/`, protocol/):**
- Full discover → checkout → complete → payment → order → provenance lifecycle
- Merchant Agent loop with bounded autonomy (iteration/tool-call budgets) and a
  deterministic fallback brain
- Deterministic gating, C-1 amount binding, C-2/C-3 state security, idempotent
  completion, terminal-state protection
- Real Razorpay **test-mode** payments via the `saved` (recurring) or `checkout` driver
- AP2-inspired signed artifacts + user Ed25519 intent assertion + standalone verifier +
  append-only hash-chained audit with integrity verification
- Unknown-buyer certificate + Buyer-B interoperability

**Demonstrated:**
- Stock-vanishing adaptation (graceful failure → substitution → recovery)
- Bounded autonomy decline (agent cannot exceed delegated limits)
- Real test-mode settlement with reconciliation / `payment_pending` recovery

**Intentionally out of scope (not implemented):**
- Full ACP/AP2 compliance (this is an **ACP-inspired** educational/reference contract)
- Production-grade identity / KYC verification
- Universal payment-provider portability (Razorpay test mode only)
- Production-scale infrastructure
- A full logistics platform (delivery/pickup + address binding only)
- Refunds / returns / reorders

---

## Documentation

- **Protocol / spec:** [`protocol/protocol_spec.md`](protocol/protocol_spec.md)
- **Schemas:** [`protocol/schemas/`](protocol/schemas/)
- **Consumer provenance & authorization:** [`docs/CONSUMER_PROVENANCE.md`](docs/CONSUMER_PROVENANCE.md)
- **AP2 standalone offline verifier:** [`docs/AP2_STANDALONE_VERIFIER.md`](docs/AP2_STANDALONE_VERIFIER.md)
- **Reproducible evidence:** [`artifacts/`](artifacts/) — evidence bundle, merchant public
  key, golden user-assertion vector, final build + refactor reports

---

## Corrections & notes for reviewers

- All amounts are in **₹**, driven by real Razorpay **test mode**; no real money moves.
- The merchant Ed25519 signing key is load-or-generated and stored under `secrets/`
  (gitignored); a committed `merchant_public_key.pem` is shipped in `artifacts/` for
  verification.
- Backend is FastAPI + SQLite; frontend is React + Vite; both are dependency-light and
  run entirely locally (except the Razorpay/Groq test-mode calls).