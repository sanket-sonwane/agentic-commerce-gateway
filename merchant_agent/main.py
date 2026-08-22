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

from merchant_agent.api import admin, audit, checkout, discovery
from merchant_agent.config import settings
from merchant_agent.db import init_db
from merchant_agent.models.dto import ErrorCode, ProtocolError

logger = logging.getLogger("merchant_agent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("Merchant agent ready | driver=%s", settings.payment_driver)
    yield


app = FastAPI(
    title="Agentic Commerce Gateway — Merchant Agent",
    version="0.1.0",
    description=settings.protocol_version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # demo environment
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(discovery.router)
app.include_router(checkout.router)
app.include_router(audit.router)
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
        "merchant_agent.main:app",
        host="0.0.0.0",
        port=settings.merchant_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
