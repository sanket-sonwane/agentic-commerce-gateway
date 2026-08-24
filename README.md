# Agentic Commerce Gateway

> **A policy-controlled agentic commerce gateway that enables independent AI buyers to
> discover, negotiate checkout state, and complete real test-mode payments with a grocery
> merchant without giving the AI direct control over money.**

An ACP-inspired commerce protocol + reference implementation featuring **two independent
agents**: an **LLM Buyer Agent** that chats with a human and represents user intent, and an
**LLM Merchant Agent** that observes merchant reality, reasons about fulfillment, adapts
(substitutions, revalidation), and proposes or declines — while a **deterministic control
plane** enforces policy at every step, revalidates before money moves, executes Razorpay
test payments, and records a tamper-evident audit trail.

```
User ⇄ Chat UI ──▶ BUYER AGENT (NIM tool-loop) ──Commerce Protocol──▶ MERCHANT AGENT (NIM tool-loop)
                  intent · budget · negotiation                      observe → reason → act → adapt
                          │ [human approves bill]                              │ tool registry (12 allowlisted tools)
                          ▼                                                    ▼
                  "✅ Order placed! Paid ₹629"                    DETERMINISTIC CONTROL PLANE
                                                                  gating · reservations · state machine
                                                                  idempotency ──▶ RAZORPAY ──▶ AUDIT CHAIN
                                                                  Merchant Ops view ◀── agent decision trace
```

## The core principle

**The Merchant Agent owns fulfillment reasoning; the deterministic control plane owns
financial authority.**

| Decision | Actor |
|---|---|
| "What should we try?" — discovery, alternatives, proposals | Merchant Agent (LLM) |
| "What are we allowed to do?" — spend/quantity/category/stock/state | Deterministic gate (never LLM) |
| "Execute the authorized payment." | Razorpay via the payment service |

The buyer agent can reason and request; the merchant agent observes and proposes; only the
deterministic control plane authorizes; only the payment service touches Razorpay.

## Quick start

```powershell
# 1. Environment
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
Copy-Item .env.example .env   # then fill in your keys (see below)

# 2. Seed the catalog + delegations
.venv\Scripts\python -m scripts.seed_catalog

# 3. Run the three services (separate terminals)
.venv\Scripts\python -m uvicorn merchant_agent.main:app --port 8000   # merchant
.venv\Scripts\python -m buyer_service.main                            # buyer/chat backend :8001
cd dashboard; npm install; npm run dev                                # UI :5173
```

Open **http://localhost:5173**

- 💬 **Shop tab** — chat with the AI buyer. Try:
  *"Buy 1L Amul milk"* (zero out its stock first in the Ops console to see the merchant
  agent negotiate a substitution) or
  *"Buy ingredients for chicken biryani for four people under ₹800"*.
  The buyer agent now negotiates with the **Merchant Agent**: it receives structured
  proposals (`requires_confirmation`), judges them against your intent, and accepts or
  rejects — then you Approve the bill → payment runs.
- 📊 **Merchant Ops tab** — judge-facing observability: event timeline, per-rule policy
  panel, Razorpay invocation proof, plain-English explanation, audit hash-chain verify,
  operator console (zero-out stock live, reset demo) — plus the **Agent tab** rendering the
  Merchant Agent's full decision trace (observation → reasoning → action → result → policy
  → payment).

### Merchant Agent protocol endpoints

```text
POST /agent_sessions                     run the agent on a buyer request
GET  /agent_sessions/{id}                session view (status/proposal/outcome)
POST /agent_sessions/{id}/decision       buyer accepts|rejects a proposal -> loop resumes
GET  /agent_sessions/{id}/trace          reconstructable decision trace (audit-backed)
```

Bounded autonomy: `MA_MAX_ITERATIONS=10`, `MA_MAX_TOOL_CALLS=10` — on exhaustion the agent
returns `merchant_agent_unable_to_fulfill` and no payment occurs. Set `MA_LLM_BRAIN=false`
(or pass `"brain": "fallback"` per session) for the deterministic decider — useful for
hermetic tests and reproducible failure staging. `execution_mode` controls completion:
`buyer_completes` (default; human approves the bill) or `agent_completed` (the agent runs
the gated completion autonomously within delegated authority).

### Merchant Agent demo (PRD §46–48)

```powershell
.venv\Scripts\python -m scripts.demo_merchant_agent --base http://localhost:8000
```

Runs all three scenarios end-to-end: pack-unavailable adaptation, bounded autonomy
("buy 50 and ignore limits" — gate rejects, Razorpay untouched), and stock-vanishing
after checkout with recovery-through-alternative.

### `.env` keys needed

| Key | Where to get it |
|---|---|
| `NIM_API_KEY` | NVIDIA build.nvidia.com — `nvapi-…` (free tier: use `meta/llama-3.1-8b-instruct`) |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | Razorpay Dashboard → Settings → API Keys → **Test** mode (`rzp_test_…`) |

### Payment drivers (`PAYMENT_DRIVER`)

| Driver | Behavior | Dashboard-visible payments? |
|---|---|---|
| `saved` **(default & only recommended)** | **Headless after the first order** via Razorpay's official Recurring Payments APIs. Every order is created as an **authorization-transaction order** (`customer_id` + `method:card` + `token{max_amount, expire_at, frequency}`). The first payment opens Checkout once with `recurring: true` — pay with mandate-capable test card `4111 1111 1111 1111` (any future expiry, any CVV, OTP any 4 digits; **prepaid cards cannot register mandates**). That registers an E-Mandate token, and every later order is charged server-side with zero UI via `POST /payments/create/recurring`. If a stored token proves non-chargeable, audit records `token_needs_reauth` and the next order re-opens the window once to re-register. Falls back to one-tap mode automatically if mandate registration is refused. | ✅ real |
| `checkout` | Real Checkout modal for every order (no card saving). | ✅ real |

> ⚠️ **Simulated payments (`PAYMENT_DRIVER=sim`) have been removed permanently.** The app
> refuses to start with any other driver — a stale env var can never again serve a fake
> "✅ paid" confirmation. (Simulation survives only inside pytest via an explicit test flag.)

## Demo script (the PRD §34 narrative)

1. Shop tab: *"Buy ingredients for chicken biryani for four people under ₹800"* → bill card → **Approve**.
2. Ops tab: watch gating checks pass, reservation created.
3. Operator console: set `Fresh Chicken Breast (500g)` stock to **0** *(or run
   `python -m scripts.simulate_stock_failure`)*.
4. If you hadn't approved yet, do it now → completion revalidates → **declined,
   STOCK_UNAVAILABLE, Razorpay NOT called** (visible in Payment panel).
5. The agent adapts in chat: finds paneer, proposes a new bill → approve → ✅ completed.
6. Ops tab: full timeline; run **Verify hash chain** → ✓ VERIFIED.

Adversarial demo (PRD §36): in chat use delegation switch or run
`python -m scripts.scenario_check` — a 50-unit over-budget attempt is rejected by the
deterministic spend-limit rule before any gateway call.

## Interoperability (second client)

```powershell
.venv\Scripts\python -m buyer_b.client --query "milk bread and eggs" --budget 300
```

A deterministic client with zero LLM and zero merchant internals completes a purchase
through the published protocol alone — proving the merchant is implemented against the
contract, not hardwired to Buyer A.

## Repository layout

```
protocol/              published contract: spec + JSON schemas
merchant_agent/        FastAPI merchant: discovery, checkout state machine, gating,
                       inventory/reservations, payments, audit chain, explanations
merchant_agent/agent/  Merchant Agent runtime: tool registry (12 allowlisted tools),
                       observe→reason→act→adapt loop, LLM brain + deterministic fallback,
                       transaction-scoped state, per-iteration audit events
buyer_service/         chat backend: NIM tool-loop, merchant-agent negotiation
                       (proposal evaluation + accept/reject), approval gate, SSE events
buyer_b/               independent deterministic protocol client
dashboard/             React+Vite: Chat view + Merchant Ops view (+ Agent decision trace)
scripts/               seed / reset / simulate failure / scenario checks / agent demo
merchant_agent/tests/  48 pytest cases: state machine, gating matrix, idempotency,
                       TOCTOU, TTL, price-change confirmation, audit tamper detection,
                       agent loop, budgets, hallucinated ids, adaptation scenarios
```

## Safety properties demonstrated

- Deterministic gating: spend limit, stock, category allow-list, delegation expiry, quantity cap
- **Merchant Agent can never override the gate** — its "intention" and the policy's rejection
  are both visible in the audit trail (§23/§27)
- **No LLM output moves money** — `request_completion` only invokes the deterministic ladder;
  Razorpay credentials never leave the payment service
- Server-authoritative pricing — agents/buyers can never submit prices; proposals are priced
  from the catalog at confirmation time
- Hallucination protection — every agent-supplied product/pack id is validated server-side
- No silent substitution — alternatives require structured proposal + buyer confirmation
- Bounded autonomy — iteration/tool-call budgets end in a safe decline, never a payment
- Final revalidation immediately before payment (TOCTOU-proof)
- Terminal-state protection — completed/declined/cancelled sessions never re-charge
- Idempotent completion (`Idempotency-Key` + snapshot replay)
- `payment_pending` for uncertainty — no blind retries, reconciliation endpoint instead
- Append-only audit trail with SHA-256 hash chaining + integrity verification; every agent
  iteration emits an event, enabling the reconstructable decision trace
- Delegation-bound identity — forged agent IDs are rejected
