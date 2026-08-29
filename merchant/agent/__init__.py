"""Merchant Agent — LLM orchestration layer over the deterministic services.

Layering (PRD §4/§49):
    Merchant Agent        -> reasoning + orchestration + adaptation
    Deterministic plane   -> financial authority + policy enforcement
    Razorpay              -> payment execution
"""
from merchant.agent.loop import MerchantAgent
from merchant.agent.state import SESSIONS, MerchantAgentState, new_session_id

__all__ = ["MerchantAgent", "MerchantAgentState", "SESSIONS", "new_session_id"]
