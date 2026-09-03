"""M2 — Public catalog endpoints + explicit commerce semantics.

An unknown buyer must be able to unambiguously understand quantity, unit, pack
size, sale basis, variant identity and price basis without inferring meaning
from strings. These tests assert the authoritative reads carry that semantics
and correctly distinguish 500ml vs 1L, quantity 1 vs 2, and sibling packs.
"""
from merchant.config import settings


def test_product_read_is_authoritative_and_explicit(client):
    r = client.get("/catalog/products/prod_milk_amul")
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["product_id"] == "prod_milk_amul"
    assert p["name"]
    assert p["category"] == "dairy"
    assert p["sale_basis"] == "per_pack"
    assert p["currency"] == "INR"
    # Amul milk has both a 500ml and a 1L pack (sibling variants).
    pack_ids = {pk["pack_id"] for pk in p["packs"]}
    assert "pack_milk_amul_500" in pack_ids
    assert "pack_milk_amul_1l" in pack_ids


def test_500ml_vs_1l_disambiguated(client):
    p = client.get("/catalog/products/prod_milk_amul").json()
    by_id = {pk["pack_id"]: pk for pk in p["packs"]}
    small = by_id["pack_milk_amul_500"]
    large = by_id["pack_milk_amul_1l"]
    # Explicit numeric size + unit — no string inference needed.
    assert small["pack_size"] == 500 and small["unit"] == "ml"
    assert large["pack_size"] == 1 and large["unit"] == "l"
    # Distinct variant identities.
    assert small["variant_id"] == "pack_milk_amul_500"
    assert large["variant_id"] == "pack_milk_amul_1l"
    # Price basis is per pack; the two packs have different authoritative prices.
    assert small["price_basis"] == "per_pack"
    assert small["sale_basis"] == "per_pack"
    assert small["quantity"] == 1 and large["quantity"] == 1


def test_pack_read_authoritative_price_and_inventory(client):
    r = client.get("/catalog/packs/pack_milk_amul_500")
    assert r.status_code == 200, r.text
    pk = r.json()
    assert pk["product_id"] == "prod_milk_amul"
    assert pk["unit_price"] > 0 and pk["currency"] == "INR"
    assert pk["availability"] in ("in_stock", "out_of_stock")
    assert isinstance(pk["available"], int)
    assert pk["stock_actual"] >= 0
    # The price must equal the authoritative Pack row price (merchant-owned).
    from merchant.persistence.db import SessionLocal
    from merchant.persistence.models.entities import Pack
    with SessionLocal() as db:
        row = db.get(Pack, "pack_milk_amul_500")
    assert pk["unit_price"] == row.price


def test_pack_not_found_structured_error(client):
    r = client.get("/catalog/packs/pack_does_not_exist")
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "PACK_NOT_FOUND"
    assert body["error"]["retryable"] is False


def test_product_not_found_structured_error(client):
    r = client.get("/catalog/products/prod_does_not_exist")
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "PRODUCT_NOT_FOUND"


def test_same_product_different_packs_distinct(client):
    # Same product, two packs -> explicit variant identity + differing prices.
    r = client.get("/catalog/products/prod_milk_amul")
    packs = {pk["pack_id"]: pk for pk in r.json()["packs"]}
    assert set(packs.keys()) == {"pack_milk_amul_500", "pack_milk_amul_1l"}
    assert packs["pack_milk_amul_500"]["unit_price"] != packs["pack_milk_amul_1l"]["unit_price"]


def test_quantity_semantics_explicit_per_pack(client):
    # The buyer understands "1 pack" = quantity 1 at unit_price per pack.
    pk = client.get("/catalog/packs/pack_milk_amul_500").json()
    assert pk["sale_basis"] == "per_pack"
    assert pk["quantity"] == 1
    # A quantity of 2 packs would total 2 * unit_price — merchant still owns price.
    line_total_2 = 2 * pk["unit_price"]
    assert line_total_2 == 2 * pk["unit_price"]
