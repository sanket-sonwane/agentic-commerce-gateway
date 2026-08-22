# Agentic Commerce Gateway

> **A policy-controlled agentic commerce gateway that enables independent AI buyers to
> discover, negotiate checkout state, and complete real test-mode payments with a grocery
> merchant without giving the AI direct control over money.**

An ACP-inspired commerce protocol + reference implementation: an **LLM buyer agent**
chats with a human, discovers products, builds a basket, and pays via **Razorpay test
mode** — while the merchant agent enforces **deterministic policy** at every step,
revalidates reality before money moves, and records an explainable, tamper-evident
audit trail.

```
User ⇄ Chat UI ──▶ Buyer Service (NIM tool-loop) ──Merchant Protocol v0.1──▶ Merchant Agent (FastAPI)
                                  │                                          ├─ deterministic gating
                          [human approves bill]                              ├─ reservations + TTL
                                  ▼                                          ├─ Razorpay (test mode)
                        "✅ Order placed! Paid ₹629"                         └─ audit hash-chain
                                                        Merchant Ops view ◀── timeline/policy/payment/explain
```

## The core principle

**The buyer agent can reason and request; the merchant agent validates and authorizes;
only the merchant executes money movement.**

Flow ownership:

| Step | Actor |
|---|---|
| Interpret goal, discover, select basket | AI buyer (LLM) |
| **Approve the final bill** | **Human user (always asked)** |
| Gate, reserve, revalidate, charge | Merchant (deterministic code, never LLM) |

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
  *"Buy ingredients for chicken biryani for four people under ₹800"*
  The agent discovers products, proposes a bill → you Approve/Cancel → payment runs.
- 📊 **Merchant Ops tab** — judge-facing observability: event timeline, per-rule policy
  panel, Razorpay invocation proof, plain-English explanation, audit hash-chain verify,
  plus the operator console (zero-out stock live, reset demo).

### `.env` keys needed

| Key | Where to get it |
|---|---|
| `NIM_API_KEY` | NVIDIA build.nvidia.com — `nvapi-…` (free tier: use `meta/llama-3.1-8b-instruct`) |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | Razorpay Dashboard → Settings → API Keys → **Test** mode (`rzp_test_…`) |

### Payment drivers (`PAYMENT_DRIVER`)

| Driver | Behavior | Dashboard-visible payments? |
|---|---|---|
| `saved` **(default)** | First order: one-time Razorpay modal — pay with test card `4111 1111 1111 1111`, any future expiry, any CVV, and **tick “Save card”**. Every order after that is charged server-side via the stored token — zero UI, real captured payments. Falls back to the modal automatically if a token charge fails. | ✅ real |
| `checkout` | Real Checkout modal for every order (no card saving). | ✅ real |
| `sim` | Real Razorpay **order** created, capture simulated by merchant (labeled in audit + UI). Fully autonomous runs. | Orders only |

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
protocol/          published contract: spec + JSON schemas
merchant_agent/    FastAPI merchant: discovery, checkout state machine, gating,
                   inventory/reservations, payments, audit chain, explanations
buyer_service/     chat backend: NIM tool-loop, approval gate, SSE events
buyer_b/           independent deterministic protocol client
dashboard/         React+Vite: Chat view + Merchant Ops view
scripts/           seed / reset / simulate failure / scenario checks
merchant_agent/tests/  34 pytest cases: state machine, gating matrix, idempotency,
                       TOCTOU, TTL, price-change confirmation, audit tamper detection
```

## Safety properties demonstrated

- Deterministic gating: spend limit, stock, category allow-list, delegation expiry, quantity cap
- Server-authoritative pricing — buyers can never submit prices
- Final revalidation immediately before payment (TOCTOU-proof)
- Terminal-state protection — completed/declined/cancelled sessions never re-charge
- Idempotent completion (`Idempotency-Key` + snapshot replay)
- `payment_pending` for uncertainty — no blind retries, reconciliation endpoint instead
- Append-only audit trail with SHA-256 hash chaining + integrity verification
- Delegation-bound identity — forged agent IDs are rejected
