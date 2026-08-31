// Workstream E — golden-vector parity check (JS <-> Python canonical strings).
//
// Node driver: builds / signs a UserIntentAssertion with the SAME code the
// browser runs (dashboard/src/lib/userAssertion.js) plus tweetnacl, then prints
// one JSON line {"canonical", "canonical_payload_hash", "signature",
// "public_key_pem", "artifact"}. scripts/_gray_check_canonical.py re-computes
// the canonical string in Python and asserts:
//   1. byte equality (JS canonical == Python canonical),
//   2. SHA-256 matches,
//   3. the JS signature verifies under the SPKI PEM (cross-runtime).
//
// Run:  node scripts/_js_canonical_check.mjs
import nacl from '../dashboard/node_modules/tweetnacl/nacl.js'
import {
  assertionCanonicalString,
  spkiPemFromPublicKey,
  keyIdFromPem,
  sha256Hex,
  u8ToHex,
  isoUtcSeconds,
} from '../dashboard/src/lib/userAssertion.js'

// Deterministic fixtures (no browser localStorage / CSPRNG involvement).
const seed = Uint8Array.from({ length: 32 }, (_, i) => (i + 1) & 0xff)
const kp = nacl.sign.keyPair.fromSeed(seed)

const now = new Date('2026-08-30T12:00:00Z')
const issuedAt = new Date(now.getTime())
const expiresAt = new Date(now.getTime() + 15 * 60 * 1000)

// Key identity is derived from the public key BEFORE anything is signed —
// exactly like buildUserAssertion() (keypair -> pem -> key id -> canonical).
const pem = await spkiPemFromPublicKey(kp.publicKey)
const keyId = await keyIdFromPem(pem)

const artifact = {
  assertion_id: 'ua_golden_vector_0001',
  version: '1.0',
  type: 'UserIntentAssertion',
  user_id: 'user_golden_0001',
  user_key_id: keyId,
  merchant_id: 'merchant_grocery_01',
  delegate_id: 'buyer_agent_llm_01',
  intent: {
    action: 'checkout',
    checkout_session_id: 'cs_golden_vector_0001',
    description: 'golden vector basket | with \\ pipe and space',
  },
  constraint_ceiling: {
    currency: 'INR',
    max_amount_paise: 45950,
  },
  issued_at: isoUtcSeconds(issuedAt),
  expires_at: isoUtcSeconds(expiresAt),
  nonce: 'nonce_golden_vector_0001',
  key_id: keyId,
}

const canonical = assertionCanonicalString({
  assertionId: artifact.assertion_id,
  userId: artifact.user_id,
  userKeyId: artifact.user_key_id,
  merchantId: artifact.merchant_id,
  delegateId: artifact.delegate_id,
  intent: artifact.intent,
  constraintCeiling: artifact.constraint_ceiling,
  issuedAt,
  expiresAt,
  nonce: artifact.nonce,
})

artifact.canonical_payload_hash = await sha256Hex(canonical)
artifact.signature = u8ToHex(nacl.sign.detached(new TextEncoder().encode(canonical), kp.secretKey))
const out = {
  canonical,
  canonical_payload_hash: artifact.canonical_payload_hash,
  signature: artifact.signature,
  public_key_pem: pem,
  artifact,
}
const { writeFileSync } = await import('node:fs')
writeFileSync('artifacts/user_assertion_golden.json', JSON.stringify(out, null, 2) + '\n')
console.log('wrote artifacts/user_assertion_golden.json')