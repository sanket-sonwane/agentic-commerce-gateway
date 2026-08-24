"""Merchant Protocol v0.1 client — the ONLY way the buyer touches the merchant.

No merchant internals, no DB, no Razorpay (G2 / NFR-07). Every call returns
(status_code, parsed_json) so the agent loop can react to structured errors.
"""
import uuid

import httpx

from buyer_service.config import settings


class MerchantClient:
    def __init__(self) -> None:
        self.base = settings.merchant_base_url.rstrip("/")

    def _headers(self, idempotency_key: str | None = None) -> dict:
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def discover(
        self,
        query: str,
        exclude_pack_ids: list[str] | None = None,
        deprioritize_product_ids: list[str] | None = None,
        exclude_product_ids: list[str] | None = None,
        skip_llm_parse: bool = False,
    ) -> tuple[int, dict]:
        r = httpx.post(
            f"{self.base}/discover",
            json={
                "query": query,
                "buyer_agent_id": settings.buyer_agent_id,
                "exclude_pack_ids": exclude_pack_ids or [],
                "deprioritize_product_ids": deprioritize_product_ids or [],
                "exclude_product_ids": exclude_product_ids or [],
                "skip_llm_parse": skip_llm_parse,
            },
            timeout=30,
        )
        return r.status_code, r.json()

    def create_checkout(self, line_items: list[dict], delegation_token: str | None = None) -> tuple[int, dict]:
        r = httpx.post(
            f"{self.base}/checkout_sessions",
            json={
                "buyer_agent_id": settings.buyer_agent_id,
                "delegation_token": delegation_token or settings.delegation_token,
                "line_items": line_items,
            },
            timeout=30,
        )
        return r.status_code, r.json()

    def update_checkout(self, checkout_session_id: str, line_items: list[dict]) -> tuple[int, dict]:
        r = httpx.put(
            f"{self.base}/checkout_sessions/{checkout_session_id}",
            json={
                "buyer_agent_id": settings.buyer_agent_id,
                "delegation_token": settings.delegation_token,
                "line_items": line_items,
            },
            timeout=30,
        )
        return r.status_code, r.json()

    def get_checkout(self, checkout_session_id: str) -> tuple[int, dict]:
        r = httpx.get(f"{self.base}/checkout_sessions/{checkout_session_id}", timeout=30)
        return r.status_code, r.json()

    def complete_checkout(
        self, checkout_session_id: str, delegation_token: str | None = None
    ) -> tuple[int, dict]:
        r = httpx.post(
            f"{self.base}/checkout_sessions/{checkout_session_id}/complete",
            json={
                "buyer_agent_id": settings.buyer_agent_id,
                "delegation_token": delegation_token or settings.delegation_token,
            },
            headers=self._headers(f"comp_{uuid.uuid4().hex[:12]}"),
            timeout=60,
        )
        return r.status_code, r.json()

    def confirm_checkout(
        self, checkout_session_id: str, accept_new_terms: bool, version: int
    ) -> tuple[int, dict]:
        r = httpx.post(
            f"{self.base}/checkout_sessions/{checkout_session_id}/confirm",
            json={
                "buyer_agent_id": settings.buyer_agent_id,
                "delegation_token": settings.delegation_token,
                "accept_new_terms": accept_new_terms,
                "version": version,
            },
            timeout=30,
        )
        return r.status_code, r.json()

    def cancel_checkout(self, checkout_session_id: str) -> tuple[int, dict]:
        r = httpx.post(
            f"{self.base}/checkout_sessions/{checkout_session_id}/cancel",
            json={
                "buyer_agent_id": settings.buyer_agent_id,
                "delegation_token": settings.delegation_token,
            },
            timeout=30,
        )
        return r.status_code, r.json()

    def reconcile(self, checkout_session_id: str) -> tuple[int, dict]:
        r = httpx.post(
            f"{self.base}/checkout_sessions/{checkout_session_id}/reconcile",
            timeout=30,
        )
        return r.status_code, r.json()

    # ---------------- Merchant Agent (agent-to-agent protocol) ----------------

    def start_agent_session(self, buyer_request: str, requested_items: list[dict] | None = None) -> tuple[int, dict]:
        """Hand the request to the Merchant Agent; it runs its own loop and
        returns paused/completed/declined outcomes (may take ~60s)."""
        body = {
            "buyer_request": buyer_request,
            "buyer_agent_id": settings.buyer_agent_id,
            "delegation_token": settings.delegation_token,
            "execution_mode": "buyer_completes",
        }
        if requested_items:
            body["requested_items"] = requested_items
        r = httpx.post(f"{self.base}/agent_sessions", json=body, timeout=150)
        return r.status_code, r.json()

    def get_agent_session(self, agent_session_id: str) -> tuple[int, dict]:
        r = httpx.get(f"{self.base}/agent_sessions/{agent_session_id}", timeout=30)
        return r.status_code, r.json()

    def respond_proposal(self, agent_session_id: str, decision: str, note: str = "") -> tuple[int, dict]:
        """Answer a merchant proposal: decision is 'accept' or 'reject'."""
        r = httpx.post(
            f"{self.base}/agent_sessions/{agent_session_id}/decision",
            json={"decision": decision, "note": note},
            timeout=150,
        )
        return r.status_code, r.json()
