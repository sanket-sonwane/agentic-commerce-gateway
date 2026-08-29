"""Payment orchestration behind a dual-driver boundary (PRD §25).

Both drivers create a REAL Razorpay Order via the test-mode Orders API when
credentials are present. They differ only in how authorization happens:

    sim      -> merchant simulates capture server-side (labeled simulated=True)
    checkout -> human authorizes via Razorpay Checkout.js in the dashboard;
                backend verifies the HMAC-SHA256 signature on callback.

The buyer agent never sees credentials or calls Razorpay directly (G2/NFR-07).
"""
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass, field

import httpx

from merchant_agent.config import settings


def _sim_allowed() -> bool:
    """Simulation is a pytest-only fixture (ACG_TEST_ALLOW_SIM=1, set by conftest).

    Production runs can never silently fall back to simulated captures — a
    missing-credentials situation must fail loudly instead.
    """
    return os.environ.get("ACG_TEST_ALLOW_SIM") == "1"


@dataclass
class PaymentOutcome:
    status: str  # completed | declined | pending
    order_id: str | None = None
    payment_id: str | None = None
    error_code: str | None = None  # PAYMENT_FAILED | PAYMENT_PENDING
    message: str = ""
    simulated: bool = False
    raw: dict = field(default_factory=dict)
    rejection_class: str | None = None  # auth_required | declined (token charges)


_client = None


def _get_client():
    global _client
    if _client is None:
        import razorpay

        _client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
    return _client


def has_credentials() -> bool:
    return bool(settings.razorpay_key_id and settings.razorpay_key_secret)


def create_order(
    checkout_session_id: str,
    amount_rupees: int,
    *,
    mandate: dict | None = None,
) -> dict:
    """Create a real test-mode Razorpay Order.

    `mandate` (driver=saved): {"customer_id"} — the order then carries the
    RBI-mandated token block (method=card, max_amount/expire_at/frequency),
    making it a valid authorization transaction so Checkout can register an
    E-Mandate and later orders can be charged headlessly via
    POST /payments/create/recurring.
    """
    if not has_credentials():
        if not _sim_allowed():
            raise RuntimeError(
                "Razorpay credentials missing (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET); "
                "refusing to simulate payment outside tests"
            )
        return {
            "id": f"order_sim_{uuid.uuid4().hex[:12]}",
            "amount": amount_rupees * 100,
            "currency": "INR",
            "receipt": checkout_session_id,
            "status": "created",
            "_simulated_no_credentials": True,
        }
    payload = {
        "amount": amount_rupees * 100,
        "currency": "INR",
        "receipt": checkout_session_id,
        "payment_capture": 1,
        "notes": {"checkout_session_id": checkout_session_id},
    }
    if mandate:
        # Authorization-transaction order per Razorpay Recurring Payments docs:
        # customer_id + method + token{max_amount, expire_at, frequency}.
        payload["customer_id"] = mandate["customer_id"]
        payload["method"] = "card"
        payload["token"] = {
            "max_amount": 1500000,  # ₹15,000 — RBI AFA threshold per debit
            "expire_at": 4102444799,  # 2050-01-01; card expiry caps it anyway
            "frequency": "as_presented",
        }
    order = _get_client().order.create(payload)
    return order


def execute_sim_capture(order_id: str, amount_rupees: int) -> PaymentOutcome:
    """Deterministic simulated capture for autonomous flows."""
    if has_credentials():
        try:
            payments = _get_client().order.payments(order_id)
            captured = [p for p in payments.get("items", []) if p.get("status") == "captured"]
            if captured:
                pay = captured[0]
                return PaymentOutcome(
                    status="completed",
                    order_id=order_id,
                    payment_id=pay["id"],
                    message="Captured via Razorpay test mode",
                    simulated=False,
                    raw={"amount": pay.get("amount"), "method": pay.get("method")},
                )
        except Exception:  # noqa: BLE001 — fall through to labeled simulation
            pass
    return PaymentOutcome(
        status="completed",
        order_id=order_id,
        payment_id=f"pay_sim_{uuid.uuid4().hex[:12]}",
        message="Simulated capture (razorpay_sim driver)",
        simulated=True,
        raw={"amount": amount_rupees * 100},
    )


def fetch_order_payments(order_id: str) -> list[dict]:
    """Reconciliation helper: list payments for an order."""
    if not has_credentials() or order_id.startswith("order_sim_"):
        return []
    result = _get_client().order.payments(order_id)
    return result.get("items", [])


def reconcile_pending(order_id: str) -> PaymentOutcome:
    """Determine final state for a payment_pending session. Never retries."""
    try:
        items = fetch_order_payments(order_id)
    except Exception as exc:  # noqa: BLE001
        return PaymentOutcome(
            status="pending",
            order_id=order_id,
            error_code="PAYMENT_PENDING",
            message=f"Reconciliation unavailable: {exc}",
        )
    captured = [p for p in items if p.get("status") == "captured"]
    if captured:
        return PaymentOutcome(
            status="completed",
            order_id=order_id,
            payment_id=captured[0]["id"],
            message="Confirmed captured during reconciliation",
            raw={"amount": captured[0].get("amount")},
        )
    failed = [p for p in items if p.get("status") == "failed"]
    if failed and not [p for p in items if p.get("status") in ("authorized", "created")]:
        return PaymentOutcome(
            status="declined",
            order_id=order_id,
            payment_id=failed[0]["id"],
            error_code="PAYMENT_FAILED",
            message=failed[0].get("error_description", "Payment failed at gateway"),
        )
    return PaymentOutcome(
        status="pending",
        order_id=order_id,
        error_code="PAYMENT_PENDING",
        message="No captured payment yet; still unresolved",
    )


def verify_checkout_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """HMAC-SHA256(order_id|payment_id, key_secret) == signature."""
    if not has_credentials():
        return False
    expected = hmac.new(
        settings.razorpay_key_secret.encode(),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def get_key_id() -> str:
    return settings.razorpay_key_id


# ---------------------------------------------------------------- saved-card flow

# The SAME identity values must be used at customer creation and at every
# subsequent recurring charge (they identify the customer to the bank).
def _customer_identity(buyer_agent_id: str) -> tuple[str, str]:
    email = f"{buyer_agent_id}@acg-demo.local"
    contact = "+919000000000"
    return email, contact


def ensure_customer(buyer_agent_id: str) -> dict:
    """Create-or-fetch a Razorpay Customer for the human behind this buyer scope."""
    if not has_credentials():
        if not _sim_allowed():
            raise RuntimeError(
                "Razorpay credentials missing; cannot create customer outside tests"
            )
        return {"id": f"cust_sim_{buyer_agent_id}", "_simulated_no_credentials": True}
    email, contact = _customer_identity(buyer_agent_id)
    customer = _get_client().customer.create(
        {
            "name": f"Shopper ({buyer_agent_id})",
            "email": email,
            "contact": contact,
            "fail_existing": "0",  # return the existing customer instead of erroring
            "notes": {"buyer_agent_id": buyer_agent_id},
        }
    )
    return customer


def fetch_payment(payment_id: str) -> dict:
    """Full payment entity — used to extract the saved-card token after checkout."""
    if not has_credentials() or payment_id.startswith("pay_sim_"):
        return {}
    return _get_client().payment.fetch(payment_id)


def capture_payment(payment_id: str, amount_paise: int) -> dict:
    return _get_client().payment.capture(payment_id, amount_paise, {"currency": "INR"})


def classify_token_rejection(body: dict) -> str:
    """Classify a declined token-charge error body.

    Returns 'auth_required' when the saved token cannot be charged
    merchant-initiated (e.g. it was created without a recurring mandate —
    Razorpay surfaces this as a generic Authentication failed), otherwise
    'declined' (normal gateway/bank decline; retrying is reasonable).
    """
    err = body.get("error") or {}
    blob = " ".join(
        str(err.get(k) or "") for k in ("code", "description", "reason", "source", "step")
    ).lower()
    if not blob:
        blob = json.dumps(body).lower()
    auth_markers = (
        "authentication failed",
        "authentication_required",
        "authentication failed",  # exact phrase from the gateway
        "recurring",
        "mandate",
        "token is invalid",
        "token has been cancelled",
        "token expired",
    )
    return "auth_required" if any(m in blob for m in auth_markers) else "declined"


def charge_with_token(
    order_id: str,
    amount_paise: int,
    customer_id: str,
    token_id: str,
    buyer_agent_id: str,
) -> PaymentOutcome:
    """Server-side charge against a saved card token (zero-UI).

    Uses the documented Recurring Payments API — POST /payments/create/recurring
    with the customer identity, `token` (NOT token_id) and mandatory
    `recurring: true`. The order passed here is a fresh, plain order created
    for this checkout session.
    """
    if not has_credentials():
        # Pytest-only hermetic behaviour; production fails loudly.
        if not _sim_allowed():
            raise RuntimeError(
                "Razorpay credentials missing; refusing to simulate a token "
                "charge outside tests"
            )
        return execute_sim_capture(order_id, amount_paise // 100)
    email, contact = _customer_identity(buyer_agent_id)

    def _post_recurring() -> httpx.Response:
        return httpx.post(
            "https://api.razorpay.com/v1/payments/create/recurring",
            json={
                "email": email,
                "contact": contact,
                "amount": amount_paise,
                "currency": "INR",
                "order_id": order_id,
                "customer_id": customer_id,
                "token": token_id,
                "recurring": True,
                "description": "Agentic Commerce Gateway order",
            },
            auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
            timeout=30,
        )

    try:
        resp = _post_recurring()
        # SERVER_ERROR is transient by definition ("try again shortly") — one
        # short-backoff retry before giving up, without duplicating money risk
        # (a failed 5xx never created a payment).
        body_probe = {}
        try:
            body_probe = resp.json()
        except Exception:  # noqa: BLE001
            pass
        if resp.status_code >= 500 and str(body_probe.get("error", {}).get("code", "")) == "SERVER_ERROR":
            time.sleep(3)
            resp = _post_recurring()
    except Exception as exc:  # noqa: BLE001 — network-level uncertainty
        return PaymentOutcome(
            status="pending",
            order_id=order_id,
            error_code="PAYMENT_PENDING",
            message=f"Token charge errored ({exc}); reconciliation required",
        )

    body = {}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        pass

    if resp.status_code != 200:
        description = str(body.get("error", {}).get("description") or body)[:200]
        return PaymentOutcome(
            status="declined",
            order_id=order_id,
            error_code="PAYMENT_FAILED",
            message=f"Token charge rejected by gateway: {description}",
            raw=body,
            rejection_class=classify_token_rejection(body),
        )

    pay_status = body.get("status")
    if pay_status in ("captured", "authorized"):
        if pay_status == "authorized":
            try:
                capture_payment(body["id"], amount_paise)
            except Exception:  # noqa: BLE001 — treat as pending for reconciliation
                return PaymentOutcome(
                    status="pending",
                    order_id=order_id,
                    payment_id=body.get("id"),
                    error_code="PAYMENT_PENDING",
                    message="Authorized but capture unresolved; reconciliation required",
                    raw=body,
                )
        return PaymentOutcome(
            status="completed",
            order_id=order_id,
            payment_id=body.get("id"),
            message="Charged using saved card token (server-side)",
            raw={"method": body.get("method"), "card_last4": (body.get("card") or {}).get("last4")},
        )
    if pay_status == "failed":
        return PaymentOutcome(
            status="declined",
            order_id=order_id,
            payment_id=body.get("id"),
            error_code="PAYMENT_FAILED",
            message=str(body.get("error_description") or "Saved-card charge failed"),
            raw=body,
        )
    return PaymentOutcome(
        status="pending",
        order_id=order_id,
        payment_id=body.get("id"),
        error_code="PAYMENT_PENDING",
        message=f"Gateway state '{pay_status}' is unresolved; reconciliation required",
        raw=body,
    )


def ping_api() -> bool:
    """Cheap connectivity probe used by the ops view."""
    if not has_credentials():
        return False
    try:
        _get_client().order.all(count=1)
        return True
    except (httpx.HTTPError, Exception):  # noqa: BLE001
        return False
