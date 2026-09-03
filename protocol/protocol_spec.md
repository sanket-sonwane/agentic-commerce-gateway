# Agentic Commerce Protocol — Reference v0.1

An **ACP-inspired educational/reference contract** for machine-readable commerce
between an independent AI buyer agent and a merchant agent. Not a claim of full
production ACP compliance.

## Guarantees

1. **State integrity** — checkout state transitions are enforced server-side;
   terminal sessions (`completed`, `declined`, `cancelled`) never execute payments.
2. **Authority enforcement** — every transactional request is validated against a
   delegation (`max_order_value`, `allowed_categories`, expiry, per-item quantity)
   by a deterministic policy engine. No LLM participates in authorization.
3. **Merchant-owned pricing** — buyers submit `product_id`/`pack_id`/`quantity`
   only; the merchant resolves authoritative prices and computes totals.
4. **Idempotent financial execution** — completion with the same `Idempotency-Key`
   returns the original result; completed sessions return their existing result.
5. **Structured failure semantics** — every error carries a stable `code`,
   human-readable `message`, `retryable` flag and `details`.
6. **Merchant ownership of inventory/order/payment state** — reservations, stock,
   orders and payment execution live exclusively in the merchant backend.
7. **Machine-readable responses** — all responses are JSON matching the schemas
   in [`schemas/`](./schemas).

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/.well-known/merchant` | Read-only merchant capability manifest |
| POST | `/discover` | Natural-language query → structured basket proposal |
| GET | `/catalog/products/{product_id}` | Authoritative product read with explicit commerce semantics |
| GET | `/catalog/packs/{pack_id}` | Authoritative pack read with explicit commerce semantics |
| POST | `/checkout_sessions` | Create session; runs gating; reserves stock |
| PUT | `/checkout_sessions/{id}` | Replace line items; revalidates everything |
| GET | `/checkout_sessions/{id}` | Authoritative current session state |
| POST | `/checkout_sessions/{id}/complete` | Final revalidation ladder → payment |
| POST | `/checkout_sessions/{id}/confirm` | Accept/reject changed terms (version-matched) |
| POST | `/checkout_sessions/{id}/cancel` | Idempotent cancel; releases reservation |
| GET | `/orders` | List the requesting buyer's orders (ownership-scoped) |
| GET | `/orders/{order_id}` | Read an order + fulfillment state |
| GET | `/audit_logs?checkout_session_id=` | Append-only audit events |
| GET | `/audit_logs/verify` | Hash-chain integrity verification |
| GET | `/explain/{id}` | Human-readable explanation from audit facts |
| POST | `/agent_sessions` | Run the Merchant Agent on a buyer request (observe→reason→act→adapt) |
| GET | `/agent_sessions/{id}` | Agent session view: status, pending proposal, outcome |
| POST | `/agent_sessions/{id}/decision` | Buyer `accept`\|`reject` of a structured proposal; agent loop resumes |
| GET | `/agent_sessions/{id}/trace` | Reconstructable agent decision trace (audit-backed) |

### Merchant capability manifest

`GET /.well-known/merchant` returns a read-only projection of the merchant's
public capabilities. It is derived from the actual implementation (see
`merchant/domain/manifest.py`) so the advertised capabilities cannot drift from
reality. The manifest exposes merchant identity, protocol name/version,
currency, supported commerce categories, the capability list, public protocol
endpoint references, payment + fulfillment capabilities, and verification
public-key metadata. It NEVER contains secrets (no private keys, no Razorpay
keys, no tokens). Schema: [`schemas/merchant_manifest.json`](./schemas/merchant_manifest.json).

### Public catalog + commerce semantics

The catalog endpoints return explicit, machine-readable commerce semantics so
an unknown buyer does not have to infer commercial meaning from strings:

- `pack_size` (numeric, e.g. `500`), `unit` (`ml`/`l`/`g`/`kg`/`pcs`)
- `quantity` (count for the sale basis) and `sale_basis` (`per_pack`)
- `unit_price` (authoritative INR per pack) + `price_basis` (`per_pack`)
- `currency` (`INR`), `stock_actual`/`reserved`/`available`/`availability`
- `variant_id` — the pack is the variant; sibling packs of a product differ only
  by `pack_id`/`size`/`unit`/`price`.

Schemas: [`schemas/catalog_product.json`](./schemas/catalog_product.json),
[`schemas/catalog_pack.json`](./schemas/catalog_pack.json).

### Orders + fulfillment

A **Checkout Session** is the payment/session object; the **Order** is the
long-lived commercial object created exactly once when a checkout payment
verifies successfully. Orders are read by the buyer over the public protocol
and never require the dashboard.

- `POST /checkout_sessions/{id}/complete` (on success) creates the order
  `order_status=placed`, `fulfillment_status=pending`, bound to the
  `checkout_session_id`, `razorpay_payment_id`, `execution_mandate_id`, and
  `decision_receipt_id`. Replaying a completed session never double-creates.
- `GET /orders?buyer_agent_id=&delegation_token=` — list the requesting
  buyer's orders; ownership is scoped to buyer **and** delegation token, so a
  caller cannot read another buyer's orders by passing an arbitrary id.
- `GET /orders/{order_id}?buyer_agent_id=&delegation_token=` — authoritative
  order incl. current `fulfillment_status`, `fulfillment_method`,
  `destination`, `tracking_reference`, `estimated_delivery`, and the amount
  actually authorized (paise + whole rupees) with payment provenance.
- Fulfillment advances through a deterministic state machine only via an
  admin-guarded operator endpoint (`POST /admin/orders/{id}/fulfillment`):
  `pending -> placed -> packed -> shipped -> delivered`. Illegal or skipped
  transitions are rejected (`INVALID_STATE_TRANSITION`), so the buyer always
  reads a provenance-clean fulfillment picture.
- The buyer never sees an order unless it was paid; declined/cancelled
  checkouts materialize no order.

**Fulfillment binding (Phase 7).** The buyer may declare fulfillment terms when
creating a checkout (`POST /checkout_sessions`): an optional `fulfillment_method`
(`delivery` | `pickup`) and an optional free-form `destination` address block.
The server validates the method (`INVALID_REQUEST` on anything else), persists
the terms on the session, and carries them verbatim into the Order at payment
completion — so fulfillment terms are part of the commercial agreement and
cannot be silently changed between checkout and delivery. If fulfillment terms
were ever to affect price/availability, the existing `PRICE_CHANGED` +
version-matched `/confirm` machinery governs (no second authorization system).
The buyer reads its fulfillment state entirely via `GET /orders/{id}`.

**Payment boundary (Phase 9).** Public merchant protocol responses speak in
merchant/payment concepts. The provider-specific identifiers live inside the
nested `payment` block (`{provider, driver, order_id, payment_id}`) rather than
as top-level contract fields; they are Razorpay-test-mode-specific and are not
part of the abstract merchant commerce contract.

Schema: [`schemas/order.json`](./schemas/order.json),
[`schemas/checkout_session.json`](./schemas/checkout_session.json).

### Merchant Agent session (v0.2 extension)

Request: `{buyer_request, buyer_agent_id, delegation_token, execution_mode, requested_items?, brain?}`

- `execution_mode`: `buyer_completes` (agent prepares the basket; buyer triggers payment via
  `/complete`) or `agent_completed` (the agent invokes the gated completion itself, within
  delegated authority).
- Response `status`: `awaiting_buyer_confirmation` (structured proposal attached),
  `ready_for_completion`, `awaiting_payment_authorization`, `completed`, `declined`,
  `unable_to_fulfill`.

Structured proposal (`requires_confirmation`, PRD §19):

```json
{
  "kind": "substitution",
  "status": "requires_confirmation",
  "reason_code": "requested_pack_unavailable",
  "original": {"product_id": "...", "pack_id": "...", "quantity": 1},
  "proposal": {"product_id": "...", "pack_id": "...", "name": "...",
               "size": 500, "unit": "ml", "quantity": 1,
               "price": 32, "currency": "INR", "line_total": 32},
  "message": "human-readable rationale"
}
```

Completion handoff (`buyer_completes`): a successfully created or updated checkout is a
handoff, not a fulfillment. The Merchant Agent surfaces it via `ready_for_completion` carrying
the authoritative server-generated `checkout_session_id`; the Buyer Agent evaluates it against
user intent and triggers payment itself via `POST /checkout_sessions/{id}/complete`. Only
`execution_mode=agent_completed` authorizes the agent to invoke the gated completion directly.

Invariants (PRD §52): the agent cannot invent state/ids, modify authoritative prices or
delegated authority, bypass checkout state or idempotency, authorize payment directly, or
silently substitute products. Every agent iteration emits a hash-chained audit event with
`payload.agent_session_id`. Iteration/tool-call budgets end in `unable_to_fulfill`; no
payment occurs afterward.

## Checkout lifecycle

```
created ──gating pass──▶ requires_payment ──complete()──▶ final validation
                              │                             ├─ price changed ─▶ requires_confirmation
                              │                             │                        │ confirm(v)
                              │                             │                        ▼
                              │                             │                  requires_payment
                              │                             ├─ stock/policy fail ─▶ declined
                              │                             ├─ gateway reject ────▶ declined
                              │                             ├─ timeout/unknown ───▶ payment_pending
                              │                             └─ captured ──────────▶ completed
                              └── cancel() ──▶ cancelled
```

## Completion ladder (mandatory order)

```
validate session → validate ownership → validate state → recalculate totals →
revalidate delegation → revalidate stock (actual + available) → revalidate policy →
ONLY THEN create Razorpay order → execute/capture
```

Previous successful checks are never treated as sufficient (changing-world rule).

## Error codes

`INVALID_REQUEST`, `UNAUTHORIZED_AGENT`, `DELEGATION_EXPIRED`,
`DELEGATION_SCOPE_VIOLATION`, `PRODUCT_NOT_FOUND`, `PACK_NOT_FOUND`,
`STOCK_UNAVAILABLE`, `CATEGORY_BLOCKED`, `SPEND_LIMIT_EXCEEDED`,
`CHECKOUT_NOT_FOUND`, `INVALID_STATE_TRANSITION`, `SESSION_EXPIRED`,
`PRICE_CHANGED`, `CONFIRMATION_STALE`, `PAYMENT_FAILED`, `PAYMENT_PENDING`,
`PAYMENT_BLOCKED`, `PROVENANCE_NOT_FOUND`

## Identity model (prototype)

A delegation token (`dlg_…`) binds a `buyer_agent_id` to its spending scope.
This is a prototype representation, not production authentication; production
requires cryptographically verifiable credentials.

Delegations can be **revoked** by the operator (`POST
/admin/delegations/{id}/revoke`). Once revoked, `validate_delegation` fails
closed with `UNAUTHORIZED_AGENT`, so the delegation can no longer create or
complete new purchases. Existing terminal (completed) sessions remain protected
by idempotent completion replay.

## Interoperability

Any client implementing only this document can transact. See `buyer_b/client.py`
for a deterministic reference client that completes purchases without any
knowledge of merchant internals, and `cert_unknown_buyer/client.py` for a
machine-checkable certification client that validates the live merchant against
its published capability manifest + JSON schemas (`/protocol/schemas/*.json`).
