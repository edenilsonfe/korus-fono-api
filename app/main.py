from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.api.v1.router import api_router
from app.core.config import get_settings, validate_settings
from app.db.session import AsyncSessionLocal
from app.middleware.entitlement import EntitlementMiddleware
from app.seeds.demo import seed_protocols
from app.services.plan_catalog_seed import seed_plan_catalog
from app.services.sentry_init import init_sentry

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncSessionLocal() as session:
        # Catálogo de protocolos (idempotente) — sem isto /protocolos fica vazio em prod.
        await seed_protocols(session)
        await seed_plan_catalog(session)
        await session.commit()
    logger.info("Protocol and plan catalog seed checked")
    try:
        yield
    finally:
        from app.services.evolution_api_client import EvolutionApiClient

        await EvolutionApiClient.aclose_shared()



def create_app() -> FastAPI:
    settings = get_settings()
    validate_settings(settings)
    if init_sentry(settings):
        logger.info("Sentry initialized")
    if not settings.debug:
        docs_kwargs = {"docs_url": None, "redoc_url": None, "openapi_url": None}
    else:
        docs_kwargs = {}
    app = FastAPI(title=settings.app_name, lifespan=lifespan, **docs_kwargs)

    @app.exception_handler(ValidationError)
    async def handle_validation_error(
        request: Request, exc: ValidationError
    ) -> JSONResponse:
        # Routers constroem corpos Pydantic manualmente (ex.: form-data em
        # resources) — sem este handler, ValidationError viraria 500 em vez
        # de 422 para categorias inválidas removidas do catálogo.
        details = [err["msg"].removeprefix("Value error, ") for err in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": "; ".join(details)})

    cors_kwargs: dict = {
        "allow_credentials": True,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
        "expose_headers": ["Content-Type"],
    }
    if settings.debug:
        # Dev: aceita qualquer porta em localhost / 127.0.0.1
        cors_kwargs["allow_origin_regex"] = r"https?://(localhost|127\.0\.0\.1)(:\d+)?"
    else:
        cors_kwargs["allow_origins"] = settings.cors_origin_list

    app.add_middleware(CORSMiddleware, **cors_kwargs)
    app.add_middleware(EntitlementMiddleware)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


app = create_app()
