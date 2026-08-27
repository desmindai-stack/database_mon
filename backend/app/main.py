from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select

from app.collectors.scheduler import start_scheduler, stop_scheduler
from app.config import settings
from app.database import SessionLocal, init_db
from app.models import MetricSample, User
from app.routers import (
    alerts,
    applications,
    auth,
    customers,
    dashboard,
    database_groups,
    instances,
    metrics,
    nodes,
    predictions,
    queries,
    servers,
    wizard,
)
from app.schemas import ConfigOut, HealthResponse
from app.services.auth_deps import get_current_user, require_write_access
from app.services.bootstrap import ensure_default_admin, ensure_default_customer


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    await ensure_default_admin()
    await ensure_default_customer()
    if settings.run_mode in ("worker", "all"):
        await start_scheduler()
    yield
    if settings.run_mode in ("worker", "all"):
        stop_scheduler()


app = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan)

_cors_origins = settings.get_cors_origins()
_allow_credentials = "*" not in _cors_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Public — no auth (login itself, obviously; health is used by deploy platforms' liveness
# probes, which don't carry a bearer token).
app.include_router(auth.router, prefix="/api")

# Everything else requires a valid session; require_write_access additionally blocks the
# viewer role from any non-GET request ("salt-okunur" — Faz 15 İŞ 1, see auth_deps.py).
_protected = [Depends(require_write_access)]
app.include_router(instances.router, prefix="/api", dependencies=_protected)
app.include_router(metrics.router, prefix="/api", dependencies=_protected)
app.include_router(queries.router, prefix="/api", dependencies=_protected)
app.include_router(alerts.router, prefix="/api", dependencies=_protected)
app.include_router(predictions.router, prefix="/api", dependencies=_protected)
app.include_router(customers.router, prefix="/api", dependencies=_protected)
app.include_router(applications.router, prefix="/api", dependencies=_protected)
app.include_router(database_groups.router, prefix="/api", dependencies=_protected)
app.include_router(nodes.router, prefix="/api", dependencies=_protected)
app.include_router(servers.router, prefix="/api", dependencies=_protected)
app.include_router(dashboard.router, prefix="/api", dependencies=_protected)
app.include_router(wizard.router, prefix="/api", dependencies=_protected)


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    from app.models import Instance

    async with SessionLocal() as session:
        count = (await session.execute(select(func.count()).select_from(Instance))).scalar_one()
        last = (
            await session.execute(select(func.max(MetricSample.collected_at)))
        ).scalar_one_or_none()
    return HealthResponse(
        status="ok",
        mode=settings.run_mode,
        deployment_mode=settings.deployment_mode,
        default_customer_name=settings.default_customer_name,
        instances=int(count or 0),
        last_collection=last,
    )



@app.get("/api/config", response_model=ConfigOut)
async def get_config(_: User = Depends(get_current_user)) -> ConfigOut:
    return ConfigOut(
        deployment_mode=settings.deployment_mode,
        default_customer_name=settings.default_customer_name,
    )


@app.get("/")
async def root() -> dict:
    return {"name": settings.app_name, "docs": "/docs", "health": "/api/health", "mode": settings.run_mode}
