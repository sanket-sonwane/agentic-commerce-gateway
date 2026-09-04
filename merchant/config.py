"""Application settings loaded from .env (see .env.example)."""
import os

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM provider (Groq — OpenAI-compatible). Used by the merchant agent
    # brain and the discovery query parser.
    llm_api_key: str = ""
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "openai/gpt-oss-safeguard-20b"

    # Razorpay test mode
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    payment_driver: str = "saved"  # saved | checkout  ('sim' removed — see validator)

    @field_validator("payment_driver")
    @classmethod
    def _reject_sim_outside_tests(cls, v: str) -> str:
        """Simulated money movement is a pytest-only fixture.

        It was removed permanently after it silently served real-looking
        confirmations (paid ₹155 '(test-mode simulation)') when a stale env
        var overrode .env. Outside pytest (ACG_TEST_ALLOW_SIM=1), starting
        with anything other than saved|checkout is a hard config error.
        """
        if v not in ("saved", "checkout") and not os.environ.get("ACG_TEST_ALLOW_SIM"):
            raise ValueError(
                f"PAYMENT_DRIVER='{v}' is not allowed. "
                "Simulated payments were removed permanently — use 'saved' "
                "(headless after one mandate registration) or 'checkout'."
            )
        return v

    # Merchant agent
    merchant_db_path: str = "data/merchant.db"
    admin_token: str = "admin_demo_token"
    # Operator session cookie lifetime (POST /admin/login -> HttpOnly cookie).
    admin_session_ttl_minutes: int = 120
    # Max age of a user-signed intent assertion consent (issued -> expiry window).
    user_assertion_ttl_minutes: int = 15
    shipping_fee: int = 40
    free_shipping_threshold: int = 999
    reservation_ttl_minutes: int = 10

    # Merchant Agent runtime (PRD §30 bounded autonomy; 8 recommended minimum,
    # +2 headroom for reasoning-only turns under the LLM tool protocol)
    ma_llm_brain: bool = True  # False -> deterministic fallback brain (tests / offline demos)
    ma_max_iterations: int = 10
    ma_max_tool_calls: int = 10

    # AP2 — merchant cryptographic identity (PRD §18–19)
    merchant_id: str = "merchant_grocery_01"
    merchant_key_id: str = "merchant-key-01"
    # Optional: explicit path to the Ed25519 private key PEM. When empty, the
    # key is load-or-generated under ./secrets/merchant_signing_key.pem
    # (gitignored). The private key is never exposed.
    merchant_signing_key_path: str = ""
    mandate_ttl_minutes: int = 10  # Execution Mandate freshness window

    # Protocol identity
    protocol_version: str = "Agentic Commerce Protocol — Reference v0.1"

    # Acting consumer (demo) for the server-side consumer order proxy. The
    # delegation token stays SERVER-SIDE: the browser dashboard calls the
    # consumer routes and never holds the buyer's ownership credential.
    demo_buyer_agent_id: str = "buyer_agent_llm_01"
    demo_delegation_token: str = "dlg_9f8e7d"

    # Ports
    merchant_port: int = 8000
    buyer_service_port: int = 8001


settings = Settings()
