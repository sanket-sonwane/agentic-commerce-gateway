// Workstream E — user-signed intent assertion (browser half).
//
// The buying human's browser holds an Ed25519 keypair in localStorage (the
// private key NEVER leaves the browser). On Approve it builds the SAME
// deterministic pipe-separated canonical string as
// merchant/ap2/user_intent.py (byte-identical), signs it with tweetnacl, and
// the resulting assertion travels buyer-service -> merchant, where the merchant
// recomputes the canonical string from the wire fields and verifies the
// signature under the user's REGISTERED public key.
//
// Field order + escaping MUST stay in exact sync with
// merchant/ap2/user_intent.py::assertion_canonical_string.
import nacl from 'tweetnacl'

const SCHEMA_PREFIX = 's1'
const TYPE = 'UserIntentAssertion'
const VERSION = '1.0'
const TTL_MS = 15 * 60 * 1000 // matches config.user_assertion_ttl_minutes

// ---- localStorage identity ------------------------------------------------
export const LS_USER_ID = 'acg.user_id'
export const LS_KEYPAIR = 'acg.user_keypair' // base64 publicKey(32) + secretKey(64)

export function getUserId() {
  let id = localStorage.getItem(LS_USER_ID)
  if (!id) {
    id = `user_${crypto.randomUUID()}`
    localStorage.setItem(LS_USER_ID, id)
  }
  return id
}

export function loadKeypair() {
  const raw = localStorage.getItem(LS_KEYPAIR)
  if (!raw) return null
  try {
    const { publicKey, secretKey } = JSON.parse(raw)
    return {
      publicKey: base64ToU8(publicKey),
      secretKey: base64ToU8(secretKey),
    }
  } catch {
    return null
  }
}

export async function getOrCreateKeypair() {
  const existing = loadKeypair()
  if (existing) return existing
  // nacl.sign.keyPair() uses the browser CSPRNG; secretKey (64 bytes) is the
  // seed+publicKey bundle tweetnacl's detached signing consumes directly.
  const kp = nacl.sign.keyPair()
  const pair = { publicKey: kp.publicKey, secretKey: kp.secretKey }
  localStorage.setItem(
    LS_KEYPAIR,
    JSON.stringify({ publicKey: u8ToBase64(pair.publicKey), secretKey: u8ToBase64(pair.secretKey) }),
  )
  return pair
}

// ---- SPKI PEM + key id -----------------------------------------------------
export async function spkiPemFromPublicKey(publicKey) {
  if (publicKey.length !== 32) throw new Error('Ed25519 public key must be 32 bytes')
  // DER: SEQUENCE { SEQUENCE { OID 1.3.101.112, NULL }, BIT STRING (0 unused) 0x20, key }
  const der = new Uint8Array(12 + publicKey.length)
  der.set([0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00], 0)
  der.set(publicKey, 12)
  const b64 = u8ToBase64(der).match(/.{1,64}/g).join('\n')
  return `-----BEGIN PUBLIC KEY-----\n${b64}\n-----END PUBLIC KEY-----`
}

export async function keyIdFromPem(pem) {
  const digest = await sha256Hex(pem)
  return `userkey_${digest.slice(0, 16)}`
}

// ---- canonical string (mirror of user_intent.py, byte-for-byte) ----------
function esc(value) {
  return String(value).replace(/\\/g, '\\\\').replace(/\|/g, '\\|')
}

// Python: json.dumps(value, sort_keys=True, separators=(",", ":"))
// JSON.stringify without `space` produces separators (",", ":"); we sort ALL
// object keys (recursive) beforehand to match sort_keys=True.
function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (value && typeof value === 'object') {
    const keys = Object.keys(value).sort()
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(value[k])}`).join(',')}}`
  }
  return JSON.stringify(value)
}

export function isoUtcSeconds(date) {
  const p = (n) => String(n).padStart(2, '0')
  return (
    `${date.getUTCFullYear()}-${p(date.getUTCMonth() + 1)}-${p(date.getUTCDate())}` +
    `T${p(date.getUTCHours())}:${p(date.getUTCMinutes())}:${p(date.getUTCSeconds())}`
  )
}

export function assertionCanonicalString({
  assertionId,
  userId,
  userKeyId,
  merchantId,
  delegateId,
  intent,
  constraintCeiling,
  issuedAt,
  expiresAt,
  nonce,
}) {
  const fields = [
    `${SCHEMA_PREFIX}|type=${TYPE}`,
    `version=${esc(VERSION)}`,
    `assertion_id=${esc(assertionId)}`,
    `user_id=${esc(userId)}`,
    `user_key_id=${esc(userKeyId)}`,
    `merchant_id=${esc(merchantId)}`,
    `delegate_id=${esc(delegateId)}`,
    `intent=${esc(canonicalJson(intent))}`,
    `constraint_ceiling=${esc(canonicalJson(constraintCeiling))}`,
    `issued_at=${esc(isoUtcSeconds(issuedAt))}`,
    `expires_at=${esc(isoUtcSeconds(expiresAt))}`,
    `nonce=${esc(nonce)}`,
  ]
  return fields.join('|')
}

// ---- signing --------------------------------------------------------------
export async function sha256Hex(input) {
  const bytes = typeof input === 'string' ? new TextEncoder().encode(input) : input
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return u8ToHex(new Uint8Array(digest))
}

export function hexToU8(hex) {
  const out = new Uint8Array(hex.length / 2)
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16)
  return out
}

// Build a complete, signed user-intent assertion artifact.
export async function buildUserAssertion({
  checkoutSessionId,
  totalPaise,
  description,
  merchantId,
  delegateId,
}) {
  const userId = getUserId()
  const keypair = await getOrCreateKeypair()
  const pem = await spkiPemFromPublicKey(keypair.publicKey)
  const userKeyId = await keyIdFromPem(pem)

  const now = new Date()
  const issuedAt = new Date(now.getTime())
  const expiresAt = new Date(now.getTime() + TTL_MS)

  const artifact = {
    assertion_id: `ua_${crypto.randomUUID()}`,
    version: VERSION,
    type: TYPE,
    user_id: userId,
    user_key_id: userKeyId,
    merchant_id: merchantId,
    delegate_id: delegateId,
    intent: {
      action: 'checkout',
      checkout_session_id: checkoutSessionId,
      description: description || '',
    },
    constraint_ceiling: {
      currency: 'INR',
      max_amount_paise: totalPaise,
    },
    issued_at: isoUtcSeconds(issuedAt),
    expires_at: isoUtcSeconds(expiresAt),
    nonce: `nonce_${crypto.randomUUID()}`,
    key_id: userKeyId,
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
  const sig = nacl.sign.detached(new TextEncoder().encode(canonical), keypair.secretKey)
  artifact.signature = u8ToHex(sig)
  return artifact
}

// ---- encoding helpers ------------------------------------------------------
export function u8ToHex(bytes) {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

function u8ToBase64(bytes) {
  let bin = ''
  for (const b of bytes) bin += String.fromCharCode(b)
  return btoa(bin)
}

function base64ToU8(b64) {
  const bin = atob(b64)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}