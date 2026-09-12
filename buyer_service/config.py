"""Buyer service settings (same .env as merchant)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class BuyerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_api_key: str = ""
    llm_base_url: str = "https://api.groq.com/openai/v1"
    buyer_llm_model: str = "openai/gpt-oss-20b"

    buyer_agent_id: str = "buyer_agent_llm_01"
    delegation_token: str = "dlg_9f8e7d"
    merchant_base_url: str = "http://localhost:8000"
    merchant_id: str = "merchant_grocery_01"
    max_agent_iterations: int = 12
    buyer_service_port: int = 8001

    # CORS: comma-separated allowlist (deployed). Empty -> "*" (local dev).
    cors_origins: str = ""


settings = BuyerSettings()
