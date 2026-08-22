"""Buyer service settings (same .env as merchant)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class BuyerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    nim_api_key: str = ""
    nim_base_url: str = "https://integrate.api.nvidia.com/v1"
    nim_model: str = "meta/llama-3.3-70b-instruct"

    buyer_agent_id: str = "buyer_agent_llm_01"
    delegation_token: str = "dlg_9f8e7d"
    merchant_base_url: str = "http://localhost:8000"
    max_agent_iterations: int = 12
    buyer_service_port: int = 8001


settings = BuyerSettings()
