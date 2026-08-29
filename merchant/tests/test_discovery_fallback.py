"""Discovery fallback parser tests (deterministic, no LLM)."""
from merchant.persistence.db import SessionLocal
from merchant.domain.discovery_service import _fallback_parse, discover


def test_budget_extraction():
    assert _fallback_parse("milk under 300").budget == 300
    assert _fallback_parse("get me ₹500 of groceries").budget == 500
    assert _fallback_parse("milk and bread").budget is None


def test_keyword_and_synonym_extraction():
    p = _fallback_parse("buy doodh and bread")  # hindi synonym for milk
    assert "milk" in p.keywords or "bread" in p.keywords


def test_quantity_extraction():
    p = _fallback_parse("buy 2 kg rice and 12 eggs")
    assert p.quantities.get("rice") == 2 or p.quantities.get("eggs") == 12


def test_discover_end_to_end_deterministic():
    db = SessionLocal()
    try:
        result = discover(db, query="milk, bread and eggs for under 300", buyer_agent_id="t")
        names = [p["matched_for"] for p in result["basket_proposal"]]
        assert set(names) >= {"milk", "bread", "eggs"}
        assert result["estimated_total"] <= 300 + 40  # items must respect stated budget
        assert all("match_reasons" in p for p in result["basket_proposal"])
    finally:
        db.close()
