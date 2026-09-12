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
    # Optional: inline Ed25519 private key PEM (deployed environments with no
    # writable secrets dir). Takes precedence over the path/file.
    merchant_signing_key_pem: str = ""
    mandate_ttl_minutes: int = 10  # Execution Mandate freshness window

    # Protocol identity
    protocol_version: str = "Agentic Commerce Protocol — Reference v0.1"

    # Ports
    merchant_port: int = 8000
    buyer_service_port: int = 8001

    # CORS: comma-separated allowlist of origins (deployed). Empty -> "*" (local dev).
    cors_origins: str = ""

    # Cross-origin (deployed) admin cookie requires Secure + SameSite=None.
    cookie_secure: bool = False


settings = Settings()
