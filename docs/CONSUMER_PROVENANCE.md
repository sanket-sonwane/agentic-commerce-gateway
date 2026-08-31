# Consumer Provenance & Authorization View

A plain‑English view that lets a normal end user understand **how one purchase
was authorized, gated, and (where applicable) paid for** — without reading
signatures or audit logs.

This is a **presentation layer over the existing AP2 machinery**. It introduces
no new authorization logic, no cryptography, and no policy decisions. The
backend remains authoritative; the UI only renders what the projection returns.

---

## 1. What this is (and is not)

The merchant runtime already signs Intent Mandates, Execution Mandates, Policy
Decisions and Decision Receipts, and re‑verifies them inside the application
(`merchant.ap2.*`, `merchant.evidence.audit_service`). The consumer provenance
projection is a **thin read model** that composes those existing, authoritative
outputs into a consumer‑friendly shape.

- **It is not** a second AP2 implementation.
- **It does not** duplicate crypto verification in the frontend.
- **It does not** let the frontend decide authorization/policy/payment validity.
- **It does not** expose secrets (no private keys, no PAN/CVV, no gateway secrets).

## 2. Endpoint

```
GET /consumer/transactions/{checkout_session_id}/provenance
```

- Requires read access (same guard as `/provenance/{id}`).
- Returns a typed `ConsumerProvenanceResponse` (see
  `merchant/persistence/models/dto.py`).
- Unknown or malformed session → structured `404` (`CHECKOUT_NOT_FOUND`),
  same error envelope as the rest of the API.

### Response shape (abridged)

```jsonc
{
  "checkout_session_id": "cs_...",
  "transaction": { "id", "status", "amount", "currency", "merchant", "agent", "created_at", "updated_at" },
  "journey": [ { "stage", "status", "title", "summary", "timestamp", "evidence_ref" } ],
  "authorization": {
    "authorized_by": "Your shopping agent (delegation)",
    "agent", "scope", "max_amount_paise", "merchant_scope", "expires_at",
    "verified": true,
    "authority_note": "Authorization recognized from your agent delegation. The intent artifact is signed by the merchant key; authority derives from the delegation."
  },
  "checks": [ { "name", "status", "summary" } ],
  "payment": { "provider": "Razorpay", "status", "razorpay_called", "order_id", "payment_id", "amount", "amount_paise" },
  "proof": { "execution_mandate", "policy_decision", "decision_receipt", "provenance", "audit_chain", "key_id", "mandate_id", "verifier_status" },
  "current_stage", "last_successful_stage", "failure_stage", "failure_reason",
  "stopped_at", "razorpay_called"
}
```

## 3. Mapping rules (deterministic, evidence‑driven)

| Field | Source |
| --- | --- |
| `transaction.merchant` | Safe display map `settings.merchant_id → "GreenMart"`, fallback to the raw merchant id. No new DB field. |
| `authorization` | Linked `IntentMandate` (via `execution_mandate_id → intent_mandate_id`). Falls back to delegation framing when no intent is linked. |
| `checks` | Latest `gating_evaluated` / `final_validation` event `payload.checks` (same source as OpsView / Money Story). |
| `payment.razorpay_called` | `True` iff `cs.razorpay_order_id` **or** an authoritative razorpay audit event exists. Never inferred from a missing payment id. |
| `payment.status` | `verified` / `declined` / `pending` / `order_created` / `not_called`, mapped from authoritative events + session state. |
| `proof` | `verify_provenance(db, id)` + `audit_service.verify_chain(db)` statuses (`verified` / `failed` / `unavailable`). Present even when provenance is unavailable. |
| `journey` / `stopped_at` | Deterministic stage model driven by audit actions. |

### Journey stages

`intent → authorization → merchant_decision → execution_authorization →
safety_checks → razorpay → payment_verification → completed`

Only stages that are actually evidenced by audit events are marked reached;
each has one of `completed` / `reached` / `failed` / `not_reached`.

### Payment status mapping

`razorpay_called=false` → `not_called`. Otherwise:
`payment_success` + `completed` → `verified`; `payment_failure`/`PAYMENT_FAILED`
→ `declined`; `payment_pending` → `pending`; an order id with no capture →
`order_created`.

## 4. Truthful authorization language

The **Intent Mandate is merchant‑signed**; authority derives from the **buyer's
delegation**. The projection therefore **never** says the user "signed" a
transaction. It always uses:

> "Authorization recognized from your agent delegation."
>
> Technical note (technical evidence section): "Intent artifact signed by
> merchant key; authority derived from delegation."

Similarly, Razorpay absence is stated only when backed by authoritative evidence,
never guessed from a missing payment id.

## 5. Frontend

`dashboard/src/components/ConsumerProvenanceDrawer.jsx` is a separate drawer
opened from the Money Story drawer. It makes a **single** call to the projection
endpoint and renders: header, status badge, "Stopped at" banner (when present),
journey stepper, Who/What/Limit, safety checks, Razorpay block (explicit
NOT CALLED / ORDER CREATED / VERIFIED / DECLINED / PENDING), friendly Proof
block, and an expandable **technical evidence** section with the truthful
authority note.

There is **no business logic, cryptography, or DB access in React** — the drawer
is a pure renderer of the projection.

## 6. Security boundaries

- No secrets are emitted (validated by `test_no_secrets_exposed`).
- The frontend never decides authorization/policy/payment validity.
- The consumer layer never weakens C‑1 / C‑2 / C‑3; it is read‑only over the
  same authoritative state.

## 7. Tests

`merchant/tests/test_consumer_projection.py` covers: successful transaction;
policy decline; amount/intent decline; payment decline; payment pending;
Razorpay‑not‑called (evidence‑based, not inferred); authorization information
(no "signed by you"); proof status; missing optional artifacts (graceful
degradation); unknown session (404); and no‑secrets.
