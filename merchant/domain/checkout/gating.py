"""Deterministic policy-gate evaluation shared by create/update/complete."""
from sqlalchemy.orm import Session

from merchant.control import gating_service
from merchant.persistence.models.dto import ErrorCode, GatingResult, ProtocolError


def _gate(db: Session, delegation, resolved_items: list[dict], totals: dict) -> GatingResult:
    return gating_service.evaluate(db, delegation, resolved_items, totals["total"])


def _decline_error(result: GatingResult) -> ProtocolError:
    code = gating_service.first_failure_code(result)
    failed = [c.detail for c in result.checks if c.result == "fail"]
    return ProtocolError(
        code,
        "; ".join(failed) or "Policy check failed",
        http_status=409,
        retryable=code in (ErrorCode.STOCK_UNAVAILABLE,),
    )