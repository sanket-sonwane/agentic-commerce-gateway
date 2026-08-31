# AP2 Standalone Verifier

Standalone verification of our AP2-inspired signed transaction artifacts.

This document describes the DB‑free, offline verifier that validates the signed
AP2 artifacts (Execution Mandate, Policy Decision, Decision Receipt, Intent
Mandate) solely from the supplied JSON artifacts and a public key — with no
merchant database, no application runtime and no private key.

It closes the gap between *"artifacts are verifiable in principle"* and
*"artifacts are independently verifiable offline."*

---

## 1. What this is (and is not)

The merchant runtime already signs artifacts and re‑verifies them during the
lifecycle inside the running application (`merchant.ap2.authorization_service`,
`merchant.ap2.provenance_service`). Those checks are **business/runtime**
verification that also consult the merchant DB (inventory, delegation status,
Razorpay settlement, persisted snapshots).

The **standalone verifier** (`merchant/ap2/standalone_verifier.py`) is a separate,
independent verifier whose job is purely **cryptographic integrity**:

- It re‑constructs the exact canonical payload that was signed at runtime.
- It checks the Ed25519 signature and the recorded payload hash.
- It checks the cross‑artifact relationships that are **explicitly carried
  inside the signed payloads** of a complete evidence bundle.

It is deliberately **not** the same as `verify_execution_mandate()`: that runtime
function performs business verification. The standalone verifier never loads or
falls back to the private key, never touches a database, and returns structured
results (with stable machine‑readable reason codes).

### What the standalone verifier CAN prove

- That an artifact was signed by the holder of the corresponding Ed25519 **private**
  key (by verifying against the **public** key you supply).
- That the artifact's contents are **exactly** what were signed (any modification
  to a signed field invalidates the signature / payload hash).
- That the payload hash recorded on the artifact matches the reconstructed
  canonical payload.
- That, within an evidence bundle, the artifacts reference each other
  consistently (receipt → execution mandate, policy → execution mandate,
  receipt ↔ policy decision id, shared checkout session, signed amounts agree,
  and — when supplied — the execution mandate → intent mandate linkage).
- That the `key_id` matches an expected value (if you supply one).

### What the standalone verifier CANNOT prove (by design)

It reports these as `NOT_VERIFIABLE_FROM_ARTIFACTS` and never guesses:

- **User identity** — that a named human/agent genuinely owned the intent.
- **Real-world intent** — that the intent text reflects a real decision.
- **Current inventory** — whether stock is still available now.
- **Delegation status** — whether the delegation is currently valid/revoked.
- **Payment settlement** — whether Razorpay actually settled the payment now.
- **DB truth** — whether the artifact matches the current merchant database.

These require live state (a database, an API, a wallet) and are outside the scope
of a pure artifact/public‑key verifier.

---

## 2. Supported artifacts

| Artifact | Payload builder | Signed fields |
| --- | --- | --- |
| `execution` | `execution_mandate_payload` | `mandate_id`, `type`, `intent_mandate_id`, `merchant_id`, `buyer_agent_id`, `checkout_session_id`, `items`, `total_paise`, `currency`, `iss_ts`, `exp`, `nonce` |
| `policy_decision` | `policy_decision_payload` | `decision_id`, `execution_mandate_id`, `checkout_session_id`, `decision`, `checks`, `authorized_amount_paise`, `ts` |
| `decision_receipt` | `decision_receipt_payload` | `receipt_id`, `intent_mandate_id`, `execution_mandate_id`, `decision_id`, `checkout_session_id`, `authorized_amount_paise`, `razorpay_order_id`, `razorpay_payment_id`, `payment_status`, `outcome`, `ts` |
| `intent` | `intent_mandate_payload` | `mandate_id`, `type`, `merchant_id`, `buyer_agent_id`, `intent`, `constraints`, `iss_ts`, `exp`, `nonce`, `version` |

The payload builders live in `merchant/ap2/signed_payloads.py` and are the
**single source of truth** for *what is signed*. They are shared by:

- the merchant runtime that creates and signs artifacts, and
- the standalone verifier that re‑verifies exported artifacts.

Because both sides use the same builders and the same
`crypto_service.canonical_json` serialization, a signature produced by the
runtime verifies identically in the standalone verifier.

---

## 3. Required inputs

1. **Signed artifact JSON** (a single artifact) **or** an **evidence bundle** JSON
   (all four artifact dicts under `artifacts/`).
2. **The merchant Ed25519 public key** as a PEM string (path or inline).
3. *Optional* `--expected-key-id` to bind the artifact's signature to a specific
   key id.

The verifier needs **no** database, **no** nonce store, **no** private key. The
public key is supplied by you for verification.

---

## 4. Offline and private-key independence

- `standalone_verifier.py` imports only `signed_payloads` and `crypto_service`;
  it never imports `merchant.persistence` (the DB/app layer).
- `verify_artifact` / `verify_transaction_evidence` accept a **public key PEM**
  and pass it to `crypto_service.verify(..., public_key_pem_str=...)`, which
  never touches the private key when the PEM is supplied.
- Tests prove this two ways:
  - an import hook forbidding `merchant.persistence` is satisfied (no DB module
    loaded), and
  - the private key accessor is patched to raise — verification still succeeds,
    proving the verifier never reaches for the private key.
- Running the verifier in a clean directory creates **no** merchant DB `data/`
  directory.

---

## 5. CLI usage

```bash
# Verify one artifact
python -m scripts.verify_ap2 --artifact artifacts/execution_mandate.json \
    --public-key artifacts/merchant_public_key.pem --expected-key-id merchant-key-01

# Verify a complete evidence bundle
python -m scripts.verify_ap2 --bundle artifacts/evidence_bundle.json \
    --public-key artifacts/merchant_public_key.pem --expected-key-id merchant-key-01

# Machine-readable output
python -m scripts.verify_ap2 --bundle artifacts/evidence_bundle.json \
    --public-key artifacts/merchant_public_key.pem --json
```

Exit codes: `0` = VALID, `1` = INVALID, `2` = usage/IO error.

### Example — single artifact (VALID)

```
AP2 VERIFICATION
--------------------------------------------------------
Artifact: artifact
Public key:      VALID
Required fields: VALID
Canonical form:  VALID
Payload hash:    VALID
Signature:       VALID
Key ID:          VALID
Expiry relationship:VALID

RESULT: ✅ VALID
```

### Example — tampered artifact (INVALID)

Modifying any signed field after signing (`total_paise` in this example) makes
the bundle invalid:

```
AP2 VERIFICATION
--------------------------------------------------------
Artifact: Evidence bundle

RESULT: ❌ INVALID
Reason: BUNDLE_RELATIONSHIP_FAILURE
Failed checks: execution_mandate_valid
```

### Example — machine-readable bundle result

```json
{
  "valid": true,
  "artifact_type": "transaction_evidence",
  "checks": {
    "execution_mandate_valid": true,
    "policy_decision_valid": true,
    "decision_receipt_valid": true,
    "receipt_refs_execution": true,
    "policy_refs_execution": true,
    "receipt_refs_policy": true,
    "session_agreement": true,
    "amounts_agree": true,
    "intent_valid": true,
    "execution_refs_intent": true,
    "current_razorpay_settlement": null,
    "current_checkout_state": null,
    "note": "NOT_VERIFIABLE_FROM_ARTIFACTS"
  },
  "reason": "OK",
  "key_id": "merchant-key-01",
  "verified_at_utc": "2026-08-30T05:22:22"
}
```

Note `current_razorpay_settlement` and `current_checkout_state` are `null` and
the `note` is `NOT_VERIFIABLE_FROM_ARTIFACTS`: those live-state aspects are never
guessed.

---

## 6. Generating the evidence bundle

`scripts/export_ap2_evidence.py` is a **demo export utility** (DB‑coupled by
design; it reads persisted artifacts) that writes a self‑contained JSON bundle
with **no secrets**:

```bash
python -m scripts.export_ap2_evidence <checkout_session_id> \
    --out artifacts/evidence_bundle.json
```

It is **not** the verifier. It only gathers and serializes the artifacts that
the DB‑free verifier then validates offline. It emits the execution mandate,
policy decision, decision receipt, the optional intent mandate, and the merchant
public key / key id.

---

## 7. Reason codes

Stable, machine‑readable codes returned in `VerificationResult.reason`:

| Code | Meaning |
| --- | --- |
| `OK` | All checks passed. |
| `MALFORMED_ARTIFACT` | Input is not a dict / not usable. |
| `INVALID_ARTIFACT_TYPE` | Unknown artifact type. |
| `MISSING_FIELD` | A required signed field is absent. |
| `INVALID_PUBLIC_KEY` | Supplied PEM is not a usable Ed25519 public key. |
| `CANONICAL_PAYLOAD_FAILURE` | Could not reconstruct the canonical payload. |
| `PAYLOAD_HASH_MISMATCH` | Recorded payload hash ≠ reconstructed hash. |
| `SIGNATURE_INVALID` | Signature does not verify under the public key. |
| `KEY_ID_MISMATCH` | key_id absent or does not match `expected_key_id`. |
| `INVALID_EXPIRY_RELATIONSHIP` | `iss_ts` ≥ `exp` (execution mandate). |
| `BUNDLE_RELATIONSHIP_FAILURE` | Cross-artifact relationships broken. |
| `NOT_VERIFIABLE_FROM_ARTIFACTS` | Live-state aspect; not determinable offline (never guessed). |

---

## 8. Security boundaries & guarantees

- **Private key is never loaded.** The verifier takes only the public key and
  verifies against it. There is no code path and no fallback that reads or signs
  with the private key.
- **Exact canonicalization.** The verifier reconstructs the payload with the same
  builders and `crypto_service.canonical_json` used at signing time, so a
  legitimate artifact reproduces byte‑identical canonical bytes.
- **Failure is structured, not silent.** Malformed input returns a structured
  `VerificationResult` with `valid=False` and a reason code; it never raises and
  never crashes the CLI.
- **No LLM dependency.** Verification is deterministic pure cryptography.
- **Honest about scope.** Anything that depends on current live state is reported
  as `NOT_VERIFIABLE_FROM_ARTIFACTS`, not asserted.

### Canonicalization fix note

The shared payload builders' dict accessor (`_get` in `signed_payloads.py`)
initially probed object attributes before dict keys. For a **plain dict**
artifact this made a field literally named `items` collide with the built‑in
`dict.items` method, corrupting reconstruction. Runtime signing uses ORM entity
objects (no such collision) but the verifier consumes plain dicts, so this would
have broken offline verification of genuinely exported artifacts. The accessor
now reads real dict keys first and only falls back to attribute access for
non‑dict entity objects — runtime behavior is unchanged, and offline
verification of exported artifacts is correct.

---

## 9. Files

| File | Role |
| --- | --- |
| `merchant/ap2/signed_payloads.py` | Shared DB‑free payload builders (single source of truth for *what is signed*); includes the `_get` dict/attribute accessor fix. |
| `merchant/ap2/standalone_verifier.py` | The deliverable: `verify_artifact`, `verify_transaction_evidence`, `VerificationResult`, stable reason codes. |
| `scripts/verify_ap2.py` | CLI (human table, `--json`, exit codes). |
| `scripts/export_ap2_evidence.py` | DB‑coupled demo exporter producing the bundle for the verifier. |
| `merchant/tests/test_standalone_verifier.py` | 23 tests: valid/malformed/mutation, wrong key, offline, no‑private‑key, evidence‑bundle relationships. |
| `merchant/ap2/authorization_service.py` | Runtime (unchanged behavior) — imports the shared payload builders. |

Preserved code (not modified by this work): `merchant/domain/checkout_service.py`,
`merchant/ap2/provenance_service.py`, `merchant/evidence/audit_service.py`,
`merchant/control/explain_service.py`, `merchant/api/provenance.py`, and the
full C‑1/C‑2/C‑3 and decision‑cycle paths.
