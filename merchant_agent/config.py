"""Application settings loaded from .env (see .env.example)."""
import os

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # NVIDIA NIM
    nim_api_key: str = ""
    nim_base_url: str = "https://integrate.api.nvidia.com/v1"
    nim_model: str = "meta/llama-3.3-70b-instruct"

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
    shipping_fee: int = 40
    free_shipping_threshold: int = 999
    reservation_ttl_minutes: int = 10

    # Merchant Agent runtime (PRD §30 bounded autonomy; 8 recommended minimum,
    # +2 headroom for reasoning-only turns under the NIM tool protocol)
    ma_llm_brain: bool = True  # False -> deterministic fallback brain (tests / offline demos)
    ma_max_iterations: int = 10
    ma_max_tool_calls: int = 10

    # Protocol identity
    protocol_version: str = "Agentic Commerce Protocol — Reference v0.1"

    # Ports
    merchant_port: int = 8000
    buyer_service_port: int = 8001


settings = Settings()
