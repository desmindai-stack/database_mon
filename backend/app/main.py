from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select

from app.collector.scheduler import start_scheduler, stop_scheduler
from app.config import settings
from app.database import SessionLocal, init_db
from app.models import MetricSample
from app.routers import alerts, instances, metrics, predictions, queries
from app.schemas import HealthResponse


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    if settings.run_mode in ("worker", "all"):
        start_scheduler()
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

app.include_router(instances.router, prefix="/api")
app.include_router(metrics.router, prefix="/api")
app.include_router(queries.router, prefix="/api")
app.include_router(alerts.router, prefix="/api")
app.include_router(predictions.router, prefix="/api")


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
        instances=int(count or 0),
        last_collection=last,
    )



@app.get("/")
async def root() -> dict:
    return {"name": settings.app_name, "docs": "/docs", "health": "/api/health", "mode": settings.run_mode}
