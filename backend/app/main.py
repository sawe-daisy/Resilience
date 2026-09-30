from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import admin, config, disbursements, health, orgs, whoami
from app.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Resilience API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        allow_credentials=False,  # no cookies anywhere
    )
    app.include_router(health.router)
    app.include_router(config.router)
    app.include_router(whoami.router)
    app.include_router(orgs.router)
    app.include_router(admin.router)
    app.include_router(disbursements.router)
    if settings.app_env == "test":
        from app.routers import mock_payments

        app.include_router(mock_payments.router)
    return app


app = create_app()
