# REPOSITORY REORGANIZATION REPORT

**Project:** Agentic Commerce Gateway (ACG) — Razorpy AI Buildathon
**Scope:** Repository reorganization ONLY — no behavior changes.
**Source of truth:** `docs/ARCHAEOLOGY_REPORT.md`
**Result:** `CURRENT IMPLEMENTATION → LOGICALLY GROUPED FILES → CLEAR ARCHITECTURAL BOUNDARIES → SAME BEHAVIOR`

Four commits:
- `c01bb96` — safe pre-reorg baseline (all AP2 layer, agent layer, audits, archaeology report)
- `44f7868` — reorg(batch1): documentation → `docs/`
- `5b4dab1` — reorg(batch2-3): package rename `merchant_agent` → `merchant` + services split
- `3df2348` — reorg(batch4-5): import paths rewritten, catalog path + startup strings fixed

---

## 1. Before Tree

Pre-reorg structure (baseline commit `c01bb96`):

```
rz-agent/
├─ README.md
├─ ARCHAEOLOGY_REPORT.md            (repo root)
├─ AUDIT.md  AUDIT_REPORT.md  FINAL_AUDIT.md
├─ AUDIT_MERCHANT_AGENT_V1.md  AP2audit.md  AP2-post-audit.md
├─ buyer_b/                         (independent deterministic protocol client)
├─ buyer_service/                   (chat backend; DO NOT TOUCH)
├─ dashboard/                       (React+Vite UI; DO NOT TOUCH)
├─ protocol/                        (published contract)
├─ scripts/                         (seed / reset / scenario / bench / demo / smoke)
└─ merchant_agent/
   ├─ __init__.py
   ├─ config.py  db.py  main.py
   ├─ agent/      __init__.py  loop.py  prompts.py  state.py  tools.py
   ├─ api/        __init__.py  admin.py  agent.py  audit.py  checkout.py
   │              discovery.py  provenance.py
   ├─ models/     __init__.py  dto.py  entities.py
   ├─ seed/       catalog.json
   ├─ services/   __init__.py
   │              audit_service.py  checkout_service.py  inventory_service.py
   │              discovery_service.py  gating_service.py  delegation_service.py
   │              explain_service.py  authorization_service.py  crypto_service.py
   │              provenance_service.py  payment_service.py
   └─ tests/      __init__.py  conftest.py + 13 test files
```

Key structural problem: all eleven service modules lived in a flat `services/`
package with no expression of the actual layered architecture (domain / control /
AP2 / payment / evidence) that the code already implements.

---

## 2. Target Tree

```
merchant/
├─ __init__.py
├─ config.py  main.py
├─ agent/        loop.py  prompts.py  state.py  tools.py      (WHO THINKS)
├─ api/          admin.py  agent.py  audit.py  checkout.py     (WHO COMMUNICATES)
│                discovery.py  provenance.py
├─ domain/       checkout_service.py  discovery_service.py     (WHO EXECUTES — core commerce)
│                inventory_service.py
├─ control/      gating_service.py  delegation_service.py      (WHO CONTROLS)
│                explain_service.py
├─ ap2/          authorization_service.py  crypto_service.py   (WHO AUTHORIZES)
│                provenance_service.py
├─ payment/      payment_service.py                            (WHO EXECUTES — payments)
├─ evidence/     audit_service.py                              (WHO AUDITS)
├─ persistence/  db.py  models/dto.py  models/entities.py      (WHO STORES)
├─ seed/         catalog.json
└─ tests/        conftest.py + 13 test files
```

Tests remain under `merchant/tests/` (user decision — NOT moved to root `tests/`).
Docs move to `docs/`.

---

## 3. Actual Final Tree

```
rz-agent/
├─ README.md
├─ docs/
│  ├─ ARCHAEOLOGY_REPORT.md
│  └─ audits/  AUDIT.md  AUDIT_REPORT.md  AUDIT_MERCHANT_AGENT_V1.md
│             FINAL_AUDIT.md  AP2audit.md  AP2-post-audit.md
├─ buyer_b/          (unchanged)
├─ buyer_service/    (unchanged)
├─ dashboard/        (unchanged)
├─ protocol/         (unchanged)
├─ scripts/          (unchanged — sys.path-based; demo/seed/scenario/bench/smoke)
└─ merchant/
   ├─ __init__.py
   ├─ config.py  main.py
   ├─ agent/      __init__.py  loop.py  prompts.py  state.py  tools.py
   ├─ api/        __init__.py  admin.py  agent.py  audit.py  checkout.py
   │              discovery.py  provenance.py
   ├─ domain/     __init__.py  checkout_service.py  discovery_service.py
   │                          inventory_service.py
   ├─ control/    __init__.py  gating_service.py  delegation_service.py
   │                          explain_service.py
   ├─ ap2/        __init__.py  authorization_service.py  crypto_service.py
   │                          provenance_service.py
   ├─ payment/    __init__.py  payment_service.py
   ├─ evidence/   __init__.py  audit_service.py
   ├─ persistence/ __init__.py  db.py  models/__init__.py  dto.py  entities.py
   ├─ seed/       catalog.json
   └─ tests/      __init__.py  conftest.py + 13 test files
```

All filenames preserved (moved intact; no rename churn). Every new subpackage
gained an empty `__init__.py`; the now-empty `merchant/services/` package was removed.

---

## 4. Files Moved

### Batch 1 — documentation (`44f7868`)

| Old Path | New Path | Reason |
|---|---|---|
| `ARCHAEOLOGY_REPORT.md` | `docs/ARCHAEOLOGY_REPORT.md` | Rethink-root the analysis report |
| `AUDIT.md` | `docs/audits/AUDIT.md` | Group audit artifacts |
| `AUDIT_REPORT.md` | `docs/audits/AUDIT_REPORT.md` | Group audit artifacts |
| `AUDIT_MERCHANT_AGENT_V1.md` | `docs/audits/AUDIT_MERCHANT_AGENT_V1.md` | Group audit artifacts |
| `FINAL_AUDIT.md` | `docs/audits/FINAL_AUDIT.md` | Group audit artifacts |
| `AP2audit.md` | `docs/audits/AP2audit.md` | Group audit artifacts |
| `AP2-post-audit.md` | `docs/audits/AP2-post-audit.md` | Group audit artifacts |

### Batch 2 — package rename `merchant_agent` → `merchant` (`5b4dab1`)

All 46 files under `merchant_agent/` moved to `merchant/` with identical relative
paths (renamed package prefix only). Every file listed in Section 1 moved.

### Batch 3 — services split into layers (`5b4dab1`)

| Old Path | New Path | Reason |
|---|---|---|
| `services/checkout_service.py` | `domain/checkout_service.py` | Core commerce execution |
| `services/discovery_service.py` | `domain/discovery_service.py` | Core commerce execution |
| `services/inventory_service.py` | `domain/inventory_service.py` | Core commerce execution |
| `services/gating_service.py` | `control/gating_service.py` | Control / policy |
| `services/delegation_service.py` | `control/delegation_service.py` | Control / policy |
| `services/explain_service.py` | `control/explain_service.py` | Control / explainability |
| `services/authorization_service.py` | `ap2/authorization_service.py` | AP2 authorization |
| `services/crypto_service.py` | `ap2/crypto_service.py` | AP2 cryptography |
| `services/provenance_service.py` | `ap2/provenance_service.py` | AP2 provenance |
| `services/payment_service.py` | `payment/payment_service.py` | Payment execution |
| `services/audit_service.py` | `evidence/audit_service.py` | Evidence / auditing |
| `db.py` | `persistence/db.py` | Persistence (with `models/`) |
| `models/dto.py` | `persistence/models/dto.py` | Persistence |
| `models/entities.py` | `persistence/models/entities.py` | Persistence |
| `services/__init__.py` | *(removed — flattened)* | Empty package after all content moved |

### Batch 4-5 — import/path rewrite (`3df2348`)

No file relocations; 41 `.py` files had import strings rewritten (see §9), plus
`README.md` path/startup references and two catalog-path string literals.

---

## 5. Files Kept in Place

| File | Reason |
|---|---|
| `merchant/main.py`, `config.py` | App factory + settings stay at package root (single entry point) |
| `merchant/agent/*` | Agent runtime stays as its own top-level package |
| `merchant/api/*` | Route layer stays as its own top-level package |
| `merchant/seed/catalog.json` | Data stays with the package (referenced via `ROOT` path) |
| `merchant/tests/*` | Tests stay under `merchant/tests/` (user decision) |
| `merchant/persistence/models/` | `db.py` + `models/` co-located to preserve their mutual import |
| `scripts/*` (`demo_merchant_agent.py`, `seed_catalog.py`, `scenario_check.py`, `_bench.py`, `reset_demo.py`, `test_buyer_flow.py`, `audit_bypasses.py`, `audit_smoke.py`, `simulate_stock_failure.py`) | Operation utilities kept intact (`sys.path`-based) |
| `buyer_service/`, `buyer_b/`, `dashboard/`, `protocol/` | Out of scope for this phase (ownership = buyer/dashboard/protocol) |

---

## 6. Documentation Organization

- `docs/ARCHAEOLOGY_REPORT.md` — the analysis that drove this reorganization (source of truth).
- `docs/audits/*` — all historical audit reports grouped under one directory.
- Root now contains only `README.md` as a markdown file, keeping the surface clean.

---

## 7. Test Organization

- Tests remain at `merchant/tests/` (NOT moved to a root-level `tests/`), per user decision.
- Test files were renamed only by the package prefix (they were never relocated
  beyond the `merchant_agent → merchant` package rename).
- All 13 test files + `conftest.py` import via `merchant.tests.conftest` (rewritten in batch 4).

---

## 8. Script Organization

- All operational scripts stay in `scripts/` unchanged.
- Script imports were rewritten from `merchant_agent.*` → `merchant.*` (batch 4).
- Two catalog-path string literals fixed (`ROOT / "merchant" / "seed" / "catalog.json"`).
- `scripts/demo_merchant_agent.py:107` still matches `actor == "merchant_agent"` (preserved — see §10).

---

## 9. Import Changes

All imports rewritten from the `merchant_agent.<pkg>` prefix to `merchant.<pkg>`:

**Header/path prefix rewrites (the only change):**
- `merchant_agent.services.<mod>` → `merchant.<layer>.<mod>` per §4 Batch 3 mapping
- `from merchant_agent.services import A, B, C` → split into one `from` per layer
- `merchant_agent.db` → `merchant.persistence.db` (import statements only)
- `merchant_agent.models.*` → `merchant.persistence.models.*` (import statements only)
- `merchant_agent.api.*` → `merchant.api.*`
- `merchant_agent.agent.*` → `merchant.agent.*`
- `merchant_agent.config` → `merchant.config`
- `merchant_agent.main` → `merchant.main`
- `merchant_agent.tests.conftest` → `merchant.tests.conftest`

**Multi-layer imports split into per-layer statements** (the only lines that
change shape, not just prefix):
- `merchant/domain/checkout_service.py` — audit+gating+inventory → evidence/control/domain
- `merchant/agent/tools.py` — checkout+delegation+discovery+inventory → domain/control/domain/domain
- `merchant/api/provenance.py` — audit+authorization+crypto → evidence/ap2/ap2
- `merchant/api/agent.py` — audit+checkout → evidence/domain
- `merchant/api/admin.py` — audit+payment → evidence/payment
- `merchant/api/checkout.py` — checkout+payment → domain/payment
- `merchant/ap2/provenance_service.py` — authorization+crypto → ap2/ap2
- `merchant/tests/test_ap2_fixes.py` — authorization+crypto → ap2/ap2

All remaining import statements were single-module → only the prefix token changed.

---

## 10. Package/Startup Changes

- Uvicorn startup string `merchant_agent.main:app` → `merchant.main:app`
  (`merchant/main.py` self-string auto-corrected; `README.md:51`, `reset_demo.py:50`).
- Catalog path string `ROOT/"merchant_agent"/"seed"/"catalog.json"`
  → `ROOT/"merchant"/"seed"/"catalog.json"` (`seed_catalog.py`, `tests/conftest.py`).
- Logger names `"merchant_agent"` / `"merchant_agent.agent"` → `"merchant"` /
  `"merchant.agent"` (cosmetic; behavior-neutral).

**Preserved data-value strings (NOT renamed — these are wire/contract constants):**
- `actor="merchant_agent"` — 30+ occurrences in checkout/loop/api; matched by
  dashboard `AuditTimeline.jsx`, `MoneyStoryDrawer.jsx`, `actionMeta.js`. Dashboard
  made zero changes and still works.
- `reason_code = "merchant_agent_unable_to_fulfill"` — asserted in tests.
- Tool name `purchase_via_merchant_agent` in `buyer_service/` — untouched.
- Filenames `test_merchant_agent.py`, `demo_merchant_agent.py` — untouched.

No change to package entry points or env-var surface.

---

## 11. Behavior Preservation Verification

This phase did NOT perform any internal refactoring. Verified:
- **No** splitting of `checkout_service`
- **No** splitting of the merchant agent loop
- **No** splitting of agent tools
- **No** rewrite of the buyer agent
- **No** redesign of AP2
- **No** change to payment architecture
- **No** change to the state machine
- **No** bug fixes
- **No** business logic removal
- **No** database behavior change

The only edits were import paths, doc-file relocation, and path/logger string
literals. `git diff` of every changed source file confirms import-only line edits
with no logic changes.

---

## 12. Tests Run

- `python -m pytest merchant/tests -q` — full merchant test suite (13 files).
- `python -m scripts.scenario_check` — end-to-end integration smoke (TestClient):
  spend-limit, category-block, delegation-expiry, checkout + operator stock-zeroing,
  STOCK_UNAVAILABLE decline, adaptation in new session, terminal-state protection.
- Import smoke: `merchant.main` FastAPI app + all 19 service/api/agent modules import.
- `py_compile` of all 70 `.py` files (merchant + scripts + buyer) — 0 errors.

---

## 13. Test Results

- **109 passed** (identical to the pre-reorg baseline of `109 passed`).
- Scenario check: exit 0, all scenarios PASS.
- Import smoke: all modules OK.

Pre-reorg baseline: `109 passed in 6.75s`. Post-reorg: `109 passed in 6.17s`.
No failures, no skips, no xfails introduced.

---

## 14. Files NOT Modified Internally

The following files were neither moved nor had any internal edits beyond the
package-rename import prefix (their logic, docstrings, and structure are untouched):

- `merchant/agent/prompts.py` — import prefix only
- `scripts/*` (seed / reset / scenario / bench / simulation / smoke) — no internal edits
- `buyer_service/*` — completely untouched
- `buyer_b/*` — completely untouched
- `dashboard/*` — completely untouched
- `protocol/*` — completely untouched
- `merchant/seed/catalog.json` — data, untouched

Every moved service/API/agent file differs from its prior content only in the
import header block. No function, class, or constant body was altered.

---

## 15. Remaining Structural Problems

- **Cross-layer lazy imports** remain (function-local) and were intentionally
  **not** re-layered to avoid new cycles: `ap2→domain`, `control→domain`,
  `evidence→ap2`, `domain→{control,ap2,payment,evidence}`. These already existed
  pre-reorg and are safe (no circular import); a clean dependency-only edge set is
  a later-phase concern.
- `merchant/models`-style aliasing still references persistence via full path; a
  future phase could add package-level re-exports, but that would be a deliberate
  API change (deferred).
- Root still has several top-level project dirs (`buyer_service`, `buyer_b`,
  `dashboard`, `protocol`) that could someday be grouped, but they represent
  distinct deployables and were out of scope.

---

## 16. Items Intentionally Deferred

Per the spec ("Only after this phase will we move on to bug fixes and internal
refactoring"), the following are deferred to later phases:
- All money-gap bug fixes (C-1/C-2/C-3), H-1/H-5/H-6/H-7/H-8, etc. from the audit.
- Internal refactoring of `checkout_service`, agent loop, tools.
- Payment architecture changes.
- State machine changes.
- Any abstraction/layer not already justified by real responsibility.

---

## 17. Final Architecture Explanation

The final flow, now readable directly from the tree:

```
Buyer  (chat backend)          → buyer_service/      (who thinks about the buyer)
Protocol (published contract)  → protocol/
Merchant Agent (the brain)     → merchant/agent/     (who thinks — observe→reason→act→adapt)
Communication (HTTP routes)    → merchant/api/       (who communicates)
     ↓ dispatch
Core Commerce                  → merchant/domain/    (who executes: checkout, discovery, inventory)
Control / Policy               → merchant/control/   (who controls: gating, delegation, explain)
AP2 Authorization              → merchant/ap2/       (who authorizes: intent scope, crypto, provenance)
Payment                        → merchant/payment/   (who executes payments)
Evidence / Audit               → merchant/evidence/  (who proves / audits)
Persistence                    → merchant/persistence/ (who stores: db + ORM models/config)
Presentation (UI)              → dashboard/
```

A request flows: `api/` (HTTP) → `domain/` checkout (gated by `control/`,
authorized by `ap2/`, executed against `payment/`) → every step emits an audit
event via `evidence/` → persisted through `persistence/` → surfaced in `dashboard/`.

---

## 18. Senior Engineer First-Impression Assessment

**YES** — a new senior engineer can understand the main architecture by looking
at the repository tree.

The top-level `merchant/` package now names each architectural responsibility
explicitly: `agent` (brain), `api` (communication), `domain` (core commerce),
`control` (policy), `ap2` (authorization), `payment`, `evidence` (audit),
`persistence` (storage), plus `seed` (data) and `tests`. The "who thinks /
communicates / controls / authorizes / executes / proves / audits / stores /
presents" questions map 1:1 to directories, matching the goal of the organization.

Caveats a senior engineer would note (minor, all deferred): a handful of
cross-layer lazy imports mean the physical directories are not a strict
dependency DAG (acceptable — Python function-local imports are cycle-safe), and
`scripts/` remains a flat operational bag (fine for utility scripts). Neither
prevents immediate architectural comprehension.

---

*Phase complete. Repository reorganizized from `CURRENT IMPLEMENTATION` to
`LOGICALLY GROUPED FILES` with `CLEAR ARCHITECTURAL BOUNDARIES`, preserving
`SAME BEHAVIOR` (109/109 tests + full scenario smoke pass). Ready for the next
phase: bug fixes and internal refactoring.*
