"""Sabit bir yol, parametreli bir yol tarafından yutulmamalı.

FastAPI rotaları kayıt sırasıyla eşleştiriyor ve `/{instance_id}` her tek segmenti
yakalıyor. `GET /api/queries/metric-dictionary` bu yüzden 422 dönüyordu:
"metric-dictionary" bir instance_id sanılıp tamsayıya çevrilmeye çalışılıyordu. Sözlüğe
hiç erişilemiyordu (Faz 29 İŞ 2a).

Mevcut test yalnızca `metric_dictionary()` FONKSİYONUNU sınıyordu; fonksiyon doğruydu,
rota hiç çalışmıyordu.

## Neden rota tablosuna bakmıyor, istek atıyor

İlk sürüm `app.routes`'u tarıyordu ve BOŞ geçti — hata varken de. FastAPI 0.141 dahil
edilen router'ları düzleştirmiyor: `app.routes`'ta API rotaları yerine yolu `''` olan tek
bir `_IncludedRouter` duruyor. Alt rotalara yalnızca özel alanlarla ulaşılıyor ve özel iç
yapıya dayanan bir test bir sonraki sürümde yine sessizce boş geçerdi.

Bu yüzden BELİRTİ herkese açık yüzeyden test ediliyor: yolunda parametre olmayan bir uç
asla `loc: ["path", ...]` doğrulama hatası üretemez. Üretiyorsa istek başka bir rotaya
gitmiştir.
"""

from __future__ import annotations

import re

import httpx
from fastapi import APIRouter, FastAPI

from app.main import app
from tests.auth_helper import authed_client

_PARAM = re.compile(r"\{[^}]+\}")


def shadowable_static_get_paths(application: FastAPI) -> list[str]:
    """Parametreli bir GET yoluyla aynı üst yolu paylaşan sabit GET yolları.

    Yalnızca bunlar gölgelenebilir; OpenAPI şeması herkese açık yüzey olduğu için sürümden
    bağımsız.
    """
    paths = application.openapi()["paths"]
    param_parents = {
        p.rsplit("/", 1)[0] for p, ops in paths.items() if "get" in ops and _PARAM.search(p.rsplit("/", 1)[-1])
    }
    return sorted(
        p for p, ops in paths.items() if "get" in ops and not _PARAM.search(p) and p.rsplit("/", 1)[0] in param_parents
    )


def swallowed_by_a_param_route(response: httpx.Response) -> bool:
    """Parametresiz bir uca yapılan istek YOL parametresi hatası veriyorsa başka rotaya gitmiştir."""
    if response.status_code != 422:
        return False
    detail = response.json().get("detail")
    return isinstance(detail, list) and any((err.get("loc") or [None])[0] == "path" for err in detail)


def _broken_app() -> FastAPI:
    router = APIRouter(prefix="/queries")

    @router.get("/{instance_id}")
    async def by_instance(instance_id: int) -> dict:
        return {}

    @router.get("/metric-dictionary")
    async def dictionary() -> list:
        return []

    broken = FastAPI()
    broken.include_router(router, prefix="/api")
    return broken


async def test_the_check_catches_the_bug_it_exists_for():
    """Denetim BOŞ geçmesin: bugünkü uygulamada hata olmadığı için gerçek uygulamaya karşı
    geçmesi, denetimin çalıştığını kanıtlamaz. Hatanın birebir kopyasını yakalamalı."""
    broken = _broken_app()
    assert shadowable_static_get_paths(broken) == ["/api/queries/metric-dictionary"]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=broken), base_url="http://test") as c:
        response = await c.get("/api/queries/metric-dictionary")
    assert swallowed_by_a_param_route(response), response.text


async def test_no_static_get_route_is_swallowed_in_the_app():
    candidates = shadowable_static_get_paths(app)
    # Aday listesi boşsa test hiçbir şey sınamıyordur — ilk sürümün düştüğü tuzak.
    assert "/api/queries/metric-dictionary" in candidates, candidates

    async with await authed_client() as c:
        swallowed = []
        for path in candidates:
            response = await c.get(path)
            if swallowed_by_a_param_route(response):
                swallowed.append(f"{path}: {response.text[:120]}")

    assert swallowed == []


async def test_metric_dictionary_is_reachable_and_has_content():
    async with await authed_client() as c:
        response = await c.get("/api/queries/metric-dictionary")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert rows and all(row.get("meaning") and row.get("when_problem") for row in rows)
