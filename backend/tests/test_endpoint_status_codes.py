"""Faz 19 İŞ 1 — 404 dönen uçların denetimi: gerçekten yok mu, yoksa başka bir şey mi?

Denetimde üç soru soruldu: (a) 404 gerçekten "kayıt yok" mu, (b) yetkisizlik 404 ile
maskeleniyor mu, (c) parametre hatası 404 ile karışıyor mu.

Sonuç: yetki tarafı temiz (401/403 doğru kullanılıyor), ama İKİ uç instance VAR olduğu hâlde
yalnızca alt koleksiyon BOŞ diye 404 dönüyordu. İstemci bunu silinmiş bir instance'tan
ayıramadığı için yeni eklenmiş (henüz veri toplanmamış) bir instance "bulunamadı" gibi
görünüyordu. Bu testler o davranışı kalıcı olarak kapatıyor.
"""

from __future__ import annotations

import uuid

import httpx

from app.main import app
from tests.auth_helper import authed_client


async def _make_instance(c: httpx.AsyncClient) -> dict:
    r = await c.post(
        "/api/instances",
        json={
            "name": f"codes-{uuid.uuid4().hex[:8]}",
            "engine": "postgresql",
            "host": "127.0.0.1",
            "port": 5432,
            "database": "postgres",
            "username": "postgres",
            "password": "x",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


# --- (a) Boş alt koleksiyon "yok" değildir ------------------------------------------------


async def test_latest_metrics_returns_null_when_nothing_collected_yet():
    """Instance duruyor, yalnızca henüz ölçüm yok. Eskiden 404 "No metrics collected yet"."""
    async with await authed_client() as c:
        instance = await _make_instance(c)
        r = await c.get(f"/api/metrics/{instance['id']}/latest")

    assert r.status_code == 200, r.text
    assert r.json() is None


async def test_latest_metrics_still_404s_for_a_deleted_instance():
    """Asıl 404 burada olmalı — ve mesajı Türkçe."""
    async with await authed_client() as c:
        r = await c.get("/api/metrics/99999999/latest")

    assert r.status_code == 404
    assert "bulunamadi" in r.json()["detail"].lower()


async def test_query_history_returns_an_empty_series_when_the_window_has_no_samples():
    """Sorgunun o pencerede örneği yok — bu "kayıt yok" değil, "boş pencere"."""
    async with await authed_client() as c:
        instance = await _make_instance(c)
        r = await c.get(f"/api/queries/{instance['id']}/history/12345")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["queryid"] == "12345"
    assert body["points"] == []
    assert body["latest_calls"] == 0


async def test_query_history_still_404s_for_a_deleted_instance():
    async with await authed_client() as c:
        r = await c.get("/api/queries/99999999/history/12345")

    assert r.status_code == 404


# --- (b) Yetkisizlik 404 ile maskelenmemeli ------------------------------------------------


async def test_viewer_write_attempt_is_403_not_404():
    """Viewer bir instance silmeye kalkarsa "yetkiniz yok" demeli; kaydı yokmuş gibi
    davranmak kullanıcıyı yanlış yere (silinmiş sanmaya) götürür."""
    async with await authed_client("admin") as admin:
        instance = await _make_instance(admin)

    async with await authed_client("viewer") as viewer:
        r = await viewer.delete(f"/api/instances/{instance['id']}")

    assert r.status_code == 403, r.text
    assert "admin" in r.json()["detail"].lower()


async def test_missing_session_is_401_not_404():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as anon:
        r = await anon.get("/api/instances/1")

    assert r.status_code == 401


# --- (c) Parametre hatası 404 ile karışmamalı ----------------------------------------------


async def test_a_non_numeric_id_is_422_not_404():
    """`/api/instances/abc` bir doğrulama hatasıdır. Frontend bunu "bulunamadı" ekranına
    çevirir ama HTTP kodu yanlış sorulmuş bir isteği anlatmalı."""
    async with await authed_client() as c:
        r = await c.get("/api/instances/abc")

    assert r.status_code == 422, r.text
