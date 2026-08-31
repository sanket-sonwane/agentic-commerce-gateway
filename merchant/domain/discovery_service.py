"""Discovery & intent resolution (PRD §11).

LLM (NIM) is allowed ONLY to parse natural language into a structured query.
Product matching, ranking and pricing are deterministic. If NIM is slow or
unavailable, a keyword fallback parser keeps demos working (NFR-01/NFR-10).
"""
import json
import re
import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from merchant.config import settings
from merchant.persistence.models.entities import Pack, Product
from merchant.evidence import audit_service
from merchant.agent import timing as agent_timing

STOPWORDS = {
    "a", "an", "the", "for", "and", "or", "under", "below", "less", "than",
    "buy", "get", "need", "want", "order", "please", "some", "me", "my", "of",
    "with", "to", "from", "in", "on", "at", "is", "are", "ingredients",
    "people", "person", "servings", "serving", "make", "cook", "grocery",
    "groceries", "items", "item", "things", "stuff", "rs", "inr", "rupees",
    "budget", "maximum", "max", "around", "about", "approx", "within",
}

SYNONYMS = {
    "milk": ["doodh"],
    "curd": ["yogurt", "dahi"],
    "rice": ["chawal", "basmati", "biryani-rice"],
    "bread": ["paav", "bun", "pav"],
    "eggs": ["egg", "anda"],
    "chicken": ["murgh"],
    "paneer": ["cottage-cheese", "cottage", "cheese-block"],
    "onion": ["onions", "pyaz"],
    "tomato": ["tomatoes", "tamatar"],
    "potato": ["potatoes", "aloo"],
    "oil": ["tel", "cooking-oil", "sunflower"],
    "salt": ["namak"],
    "sugar": ["cheeni"],
    "tea": ["chai"],
    "coffee": [],
    "masala": ["spices", "spice-mix", "garam-masala", "chicken-masala"],
    "dal": ["pulses", "lentils", "toor", "arhar"],
    "atta": ["flour", "wheat-flour", "gehu"],
    "ghee": [],
    "butter": ["makhan"],
    "cheese": [],
    "juice": [],
    "water": ["bottled-water", "mineral-water"],
    "biscuits": ["biscuit", "parle"],
    "chips": ["wafers"],
    "detergent": ["surf", "washing-powder"],
    "dishwash": ["dishwash-bar", "vim"],
    "coriander": ["dhania", "cilantro"],
    "mint": ["pudina"],
    "lemon": ["lemons", "nimbu"],
    "capsicum": ["bell-pepper", "shimla"],
    "fish": ["rohu"],
    "mutton": ["goat-meat", "lamb"],
    "tofu": ["soy-paneer"],
}

BUDGET_RE = re.compile(
    r"(?:under|below|less\s+than|max(?:imum)?(?:\s+of)?|budget\s+(?:of|is)?|within|upto|up\s+to)\s*"
    r"(?:₹|rs\.?|inr)?\s*(\d{2,6})",
    re.IGNORECASE,
)
QTY_RE = re.compile(r"(\d+)\s*(?:x|pcs?|packs?|units?|kg|g|l|ml)?\s+(?:of\s+)?([a-z][a-z\- ]+)")


@dataclass
class ParsedQuery:
    keywords: list[str] = field(default_factory=list)
    quantities: dict[str, int] = field(default_factory=dict)
    budget: int | None = None
    used_llm: bool = False


# ---------------------------------------------------------------- parsing


def _parse_budget(text: str) -> int | None:
    m = BUDGET_RE.search(text)
    if m:
        return int(m.group(1))
    m = re.search(r"₹\s*(\d{2,6})", text)
    return int(m.group(1)) if m else None


def _fallback_parse(query: str) -> ParsedQuery:
    text = query.lower()
    parsed = ParsedQuery(budget=_parse_budget(text))

    cleaned = re.sub(r"[₹,]", " ", text)
    cleaned = re.sub(r"[^a-z0-9\s\-]", " ", cleaned)
    tokens = [t for t in cleaned.split() if t not in STOPWORDS and len(t) > 1]

    # explicit quantity patterns like "2 kg rice", "12 eggs"
    for m in QTY_RE.finditer(cleaned):
        qty, phrase = int(m.group(1)), m.group(2).strip()
        head = phrase.split()[0]
        if 1 <= qty <= 50 and head not in STOPWORDS:
            parsed.quantities[head] = qty

    # expand synonyms -> canonical keywords present in query
    canonical_hits: list[str] = []
    token_set = set(tokens)
    for canonical, syns in SYNONYMS.items():
        candidates = {canonical} | set(syns)
        # 5-char prefixes avoid collisions like breast/bread (4-char prefix trap).
        if any(tok.startswith(c[:5]) for c in candidates for tok in token_set):
            canonical_hits.append(canonical)

    # direct multi-word tags like "garam-masala"
    for tok in tokens:
        if "-" in tok and tok not in canonical_hits:
            canonical_hits.append(tok)

    parsed.keywords = list(dict.fromkeys(canonical_hits))
    return parsed


def _llm_parse(query: str) -> ParsedQuery | None:
    """Ask NIM to structure the request. Returns None on any failure."""
    if not settings.llm_api_key:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=10.0)
        prompt = (
            "Convert this Indian grocery shopping request into JSON.\n"
            'Schema: {"items":[{"keyword":"<single grocery noun, lowercase>","quantity":<int>}],'
            '"budget": <int|null>}\n'
            "Rules: keywords must be simple grocery nouns (e.g. milk, bread, eggs, chicken, rice, "
            "paneer, onion, tomato, masala, dal, oil, tea). quantity defaults to 1. "
            "Extract budget only when an amount is stated.\n"
            f'Request: "{query}"\n'
            "Reply with ONLY the JSON object."
        )
        resp = client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=200,
        )
        content = resp.choices[0].message.content or ""
        content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
        data = json.loads(content)
        parsed = ParsedQuery(budget=data.get("budget") or _parse_budget(query.lower()), used_llm=True)
        for item in data.get("items", []):
            kw = str(item.get("keyword", "")).strip().lower()
            if kw:
                parsed.keywords.append(kw)
                parsed.quantities[kw] = max(1, int(item.get("quantity", 1)))
        return parsed
    except Exception:  # noqa: BLE001 — LLM is best-effort only
        return None


# ---------------------------------------------------------------- matching


def _score_product(product: Product, tags: list[str], keyword: str) -> tuple[int, list[str]]:
    name = product.name.lower()
    reasons: list[str] = []
    score = 0
    kw = keyword.replace("-", " ")
    if kw in name:
        score += 5
        reasons.append(f"name~{keyword}")
    for tag in tags:
        tag_l = tag.lower()
        if kw == tag_l:
            score += 4  # exact role match (e.g. tag 'chicken' for keyword 'chicken')
            reasons.append(f"tag={tag}")
        elif kw in tag_l or tag_l in kw:
            score += 2  # weaker substring association (e.g. 'chicken-masala')
            reasons.append(f"tag~{tag}")
    if kw in product.category.lower():
        score += 1
        reasons.append(f"category={product.category}")
    if kw in product.brand.lower():
        score += 1
        reasons.append(f"brand={product.brand}")
    # synonym expansion
    for syn in SYNONYMS.get(keyword, []):
        s = syn.replace("-", " ")
        if s in name or s in " ".join(tags):
            score += 2
            reasons.append(f"synonym={syn}")
    return score, reasons


def _pick_pack(packs: list[Pack], budget_left: int | None) -> Pack | None:
    """Cheapest pack that fits remaining budget; else cheapest overall."""
    affordable = [p for p in packs if budget_left is None or p.price <= budget_left]
    pool = affordable or packs
    return min(pool, key=lambda p: p.price) if pool else None


def _match_keyword(
    db: Session,
    keyword: str,
    exclude_products: set[str],
    exclude_packs: set[str] | None = None,
    deprioritize_product_ids: set[str] | None = None,
) -> tuple[Product, Pack, list[str]] | None:
    """Best matching product for a keyword. Products whose every pack is excluded
    (or whose chosen pack is excluded) fall through to the next-best match.
    Deprioritized products are only considered if nothing else matches."""
    exclude_packs = exclude_packs or set()
    deprioritize = deprioritize_product_ids or set()
    products = db.query(Product).all()
    scored: list[tuple[int, Product, list[str]]] = []
    for product in products:
        if product.product_id in exclude_products:
            continue
        tags = json.loads(product.tags)
        score, reasons = _score_product(product, tags, keyword)
        if score > 0:
            scored.append((score, product, reasons))
    scored.sort(key=lambda t: t[0], reverse=True)

    for pass_skip in (deprioritize, set()):
        for _, product, reasons in scored:
            if product.product_id in pass_skip:
                continue
            packs = db.query(Pack).filter(Pack.product_id == product.product_id).all()
            usable = [p for p in packs if p.pack_id not in exclude_packs]
            pack = _pick_pack(usable or [], None)
            if pack is not None:
                return product, pack, reasons
    return None


def discover(
    db: Session,
    *,
    query: str,
    buyer_agent_id: str,
    exclude_pack_ids: list[str] | None = None,
    deprioritize_product_ids: list[str] | None = None,
    exclude_product_ids: list[str] | None = None,
    skip_llm_parse: bool = False,
) -> dict:
    audit_service.append_event(
        db,
        actor="buyer_agent",
        action="discovery_requested",
        payload={"query": query, "buyer_agent_id": buyer_agent_id},
    )

    if skip_llm_parse:
        _start = time.perf_counter()
        parsed = _fallback_parse(query)
        agent_timing.record("discovery", "discover_parse", _start, time.perf_counter(),
                            outcome="ok", parser="keyword-fallback")
    else:
        _start = time.perf_counter()
        parsed = _llm_parse(query)
        if parsed is not None:
            agent_timing.record("discovery", "discover_parse", _start, time.perf_counter(),
                                outcome="ok", parser="llm")
        else:
            parsed = _fallback_parse(query)
            agent_timing.record("discovery", "discover_parse", _start, time.perf_counter(),
                                outcome="fallback", parser="keyword-fallback")
    if not parsed.keywords:
        parsed = _fallback_parse(query)

    excluded = set(exclude_pack_ids or [])
    deprioritized = set(deprioritize_product_ids or [])
    proposal: list[dict] = []
    unmatched: list[str] = []
    excluded_products: set[str] = set(exclude_product_ids or [])
    spent = 0

    for keyword in parsed.keywords:
        match = _match_keyword(db, keyword, excluded_products, excluded, deprioritized)
        if match is None:
            unmatched.append(keyword)
            continue
        product, pack, reasons = match
        qty = parsed.quantities.get(keyword, 1)
        if parsed.budget is not None and spent + pack.price * qty > parsed.budget:
            cheaper = db.query(Pack).filter(Pack.product_id == product.product_id).all()
            alt = _pick_pack(cheaper, parsed.budget - spent)
            if alt is None or alt.price * qty > parsed.budget - spent:
                unmatched.append(keyword)
                continue
            pack = alt
        spent += pack.price * qty
        excluded_products.add(product.product_id)
        proposal.append(
            {
                "product_id": product.product_id,
                "pack_id": pack.pack_id,
                "name": product.name,
                "category": product.category,
                "tags": json.loads(product.tags),
                "matched_for": keyword,
                "match_reasons": sorted(set(reasons))[:3],
                "quantity": qty,
                "price": pack.price,
                "size": pack.size,
                "unit": pack.unit,
                "available_stock": max(0, pack.stock_actual),
            }
        )

    result = {
        "basket_proposal": proposal,
        "estimated_total": spent,
        "currency": "INR",
        "unmatched": unmatched,
        "_meta": {"parsed_by": "llm" if parsed.used_llm else "keyword-fallback", "budget": parsed.budget},
    }
    audit_service.append_event(
        db,
        actor="merchant_agent",
        action="discovery_result_generated",
        decision="info",
        reasoning_summary=(
            f"{len(proposal)} candidate(s) proposed, {len(unmatched)} unmatched "
            f"(parser={'llm' if parsed.used_llm else 'keyword-fallback'})"
        ),
        payload={"estimated_total": spent, "unmatched": unmatched},
    )
    return result
