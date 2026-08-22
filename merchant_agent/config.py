"""Application settings loaded from .env (see .env.example)."""
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
    payment_driver: str = "sim"  # sim | checkout

    # Merchant agent
    merchant_db_path: str = "data/merchant.db"
    admin_token: str = "admin_demo_token"
    shipping_fee: int = 40
    free_shipping_threshold: int = 999
    reservation_ttl_minutes: int = 10

    # Protocol identity
    protocol_version: str = "Agentic Commerce Protocol — Reference v0.1"

    # Ports
    merchant_port: int = 8000
    buyer_service_port: int = 8001


settings = Settings()
