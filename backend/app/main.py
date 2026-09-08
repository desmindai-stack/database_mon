import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.collectors.scheduler import start_scheduler, stop_scheduler_async
from app.config import settings
from app.database import SessionLocal, init_db
from app.models import MetricSample, User
from app.routers import (
    admin,
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
    reports,
    servers,
    wizard,
)
from app.schemas import ConfigOut, HealthResponse
from app.services.auth_deps import get_current_user, require_admin, require_write_access
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
        await stop_scheduler_async()


app = FastAPI(title=settings.app_name, version="0.2.0", lifespan=lifespan)

_cors_origins = settings.get_cors_origins()
_allow_credentials = "*" not in _cors_origins

logger = logging.getLogger(__name__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Yakalanmamış hatalar için 500 üretir — CORS başlıklarının EKLENEBİLMESİ için.

    Starlette'in yerleşik sunucu-hatası katmanı CORS middleware'inin DIŞINDA duruyor: bir
    istek işleyicisi patladığında dönen 500 yanıtında `Access-Control-Allow-Origin` yok.
    Tarayıcı böyle bir yanıtı okuyamıyor ve `fetch` ağ hatası gibi başarısız oluyor —
    kullanıcı "Sunucuya ulaşılamıyor" görüyor, oysa sunucuya ulaşılmış ve 500 dönmüş.
    Canlıdaki instance silme hatasının teşhisi tam olarak bu yüzden zorlaştı.

    Bu işleyici middleware zincirinin İÇİNDE çalıştığı için yanıt CORS'tan geçiyor; istemci
    gerçek durum kodunu ve mesajı görebiliyor.
    """
    logger.exception("İşlenmeyen hata: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": (
                "Sunucuda beklenmeyen bir hata oluştu. Ayrıntı sunucu günlüklerinde: "
                f"{type(exc).__name__}"
            )
        },
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
app.include_router(reports.router, prefix="/api", dependencies=_protected)
app.include_router(wizard.router, prefix="/api", dependencies=_protected)

# Admin screen (Faz 15 İŞ 2) — every route here needs role=admin, not just a valid session.
app.include_router(admin.router, prefix="/api", dependencies=[Depends(require_admin)])


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
