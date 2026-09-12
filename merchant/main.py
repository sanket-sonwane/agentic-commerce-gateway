"""Merchant Agent — FastAPI application factory.

Every failure exits through the structured error envelope (PRD §27):
    { "error": {code, message, retryable, details}, "checkout_session"?: {...} }
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from merchant.api import admin, agent, audit, catalog_api, checkout, consumer, discovery, manifest, orders, provenance
from merchant.payment import callback as payment_callback
from merchant.config import settings
from merchant.persistence.db import init_db
from merchant.persistence.models.dto import ErrorCode, ProtocolError

logger = logging.getLogger("merchant")


def _configure_app_logging() -> None:
    """Ensure merchant.* INFO logs (including the reconcile worker) surface.

    uvicorn does not install a handler on the root logger, so INFO messages
    from ``merchant.reconcile`` and friends would otherwise be dropped.
    """
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logger.addHandler(handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_app_logging()
    init_db()
    # Ephemeral environments (serverless / free-tier restart) lose the DB;
    # auto-seed the demo catalog on boot when empty (seed_catalog is guarded).
    from scripts.seed_catalog import main as seed_main

    seed_main()
    logger.info("Merchant agent ready | driver=%s", settings.payment_driver)
    from merchant.domain.reconcile_worker import start_reconcile_worker

    stop_reconcile = await start_reconcile_worker()
    yield
    await stop_reconcile()


app = FastAPI(
    title="Agentic Commerce Gateway — Merchant Agent",
    version="0.1.0",
    description=settings.protocol_version,
    lifespan=lifespan,
)

_cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],  # empty -> permissive (local dev)
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=bool(_cors_origins),  # "*" + credentials is invalid in Starlette
)

app.include_router(discovery.router)
app.include_router(manifest.router)
app.include_router(catalog_api.router)
app.include_router(checkout.router)
app.include_router(payment_callback.router)
app.include_router(audit.router)
app.include_router(provenance.router)
app.include_router(consumer.router)
app.include_router(orders.router)
app.include_router(agent.router)
app.include_router(admin.router)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "protocol": settings.protocol_version,
        "payment_driver": settings.payment_driver,
    }


@app.exception_handler(ProtocolError)
async def protocol_error_handler(request: Request, exc: ProtocolError):
    return JSONResponse(status_code=exc.http_status, content=exc.envelope())


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": ErrorCode.INVALID_REQUEST.value,
                "message": "Request failed schema validation",
                "retryable": True,
                "details": {"errors": exc.errors()[:5]},
            }
        },
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": ErrorCode.INVALID_REQUEST.value,
                "message": "Internal merchant error; transaction not executed",
                "retryable": False,
                "details": {},
            }
        },
    )


def main() -> None:
    import uvicorn

    uvicorn.run(
        "merchant.main:app",
        host="0.0.0.0",
        port=settings.merchant_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
