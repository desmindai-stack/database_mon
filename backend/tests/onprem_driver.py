"""On-prem kurulum testinin dbace-app KONTEYNERİ İÇİNDE çalışan sürücüsü (Faz 31 Commit 8).

Kurulan uygulamanın kendi API'sini (http://127.0.0.1:8000) kullanıyor; eski ve yeni sürümde çalışacak kadar
sade. Her komut tek satır JSON yazar.

    python driver.py bootstrap-user <kullanıcı> <şifre>     # eski paket: ADMIN_PASSWORD okunmuyordu
    python driver.py setup <hedef_host> <veritabanı> <login> <şifre>
    python driver.py read <instance_id>
    python driver.py set-enabled <instance_id> <true|false>

Kimlik: DBACE_USER / DBACE_PASS; ilk girişte şifre değiştirme zorunluysa DBACE_NEW_PASS'e değiştirilir.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import uuid

import httpx

BASE = "http://127.0.0.1:8000"


@contextlib.contextmanager
def _client():
    """`with httpx.Client()` kullanılmıyor: istemci giriş yapmak için ZATEN bir istek attı ve httpx
    kullanılmış bir istemciyi ikinci kez bağlam yöneticisi olarak açmıyor."""
    client = _login()
    try:
        yield client
    finally:
        client.close()


def _login() -> httpx.Client:
    user, password = os.environ["DBACE_USER"], os.environ["DBACE_PASS"]
    client = httpx.Client(base_url=BASE, timeout=60)
    response = client.post("/api/auth/login", json={"username": user, "password": password})
    if response.status_code == 401 and os.environ.get("DBACE_NEW_PASS"):
        response = client.post("/api/auth/login", json={"username": user, "password": os.environ["DBACE_NEW_PASS"]})
        password = os.environ["DBACE_NEW_PASS"]
    response.raise_for_status()
    body = response.json()
    client.headers["Authorization"] = f"Bearer {body['access_token']}"
    if body["user"].get("must_change_password"):
        changed = client.post("/api/auth/change-password",
                              json={"current_password": password, "new_password": os.environ["DBACE_NEW_PASS"]})
        changed.raise_for_status()
        # Şifre değişiminden ÖNCE alınan jeton (yukarıdaki Authorization header'ı) artık backend'de kasıtlı
        # olarak geçersiz (token_is_stale, Commit 9b) — YANITTAKİ taze jetona geçilmeli, aksi hâlde bir
        # sonraki istek 401 "Oturum gerekli" ile düşer (Faz 31 Commit 10c takip — bu satır olmadan böyleydi).
        client.headers["Authorization"] = f"Bearer {changed.json()['access_token']}"
    return client


def _ok(response: httpx.Response) -> dict:
    if response.status_code >= 400:
        raise SystemExit(f"{response.request.method} {response.request.url} → {response.status_code}: {response.text[:500]}")
    return response.json()


async def _bootstrap_user(username: str, password: str) -> dict:
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import User
    from app.services.security import hash_password

    async with SessionLocal() as session:
        user = (await session.execute(select(User).where(User.username == username))).scalar_one_or_none()
        if user is None:
            session.add(User(username=username, password_hash=hash_password(password), role="admin", is_active=True,
                             must_change_password=False))
            await session.commit()
    return {"user": username}


def main() -> None:
    command, args = sys.argv[1], sys.argv[2:]
    if command == "bootstrap-user":
        out = asyncio.run(_bootstrap_user(*args))
    elif command == "setup":
        host, database, login, password = args
        tag = uuid.uuid4().hex[:6]
        with _client() as client:
            customer = _ok(client.post("/api/customers", json={"name": f"banka-{tag}"}))
            application = _ok(client.post("/api/applications", json={"customer_id": customer["id"], "name": "uygulama"}))
            group = _ok(client.post("/api/wizard/database-groups", json={
                "application_id": application["id"], "group_name": f"grup-{tag}", "engine": "postgresql",
                "topology": "standalone",
                "nodes": [{"server_name": host, "host": host, "port": 5432, "database": database,
                           "db_username": login, "db_password": password}],
            }))
            nodes = _ok(client.get(f"/api/groups/{group['id']}/nodes"))
        out = {"instance_id": nodes[0]["instance_id"], "group_id": group["id"]}
    elif command == "read":
        instance_id = int(args[0])
        with _client() as client:
            instance = _ok(client.get(f"/api/instances/{instance_id}"))
            health = _ok(client.get(f"/api/instances/{instance_id}/cluster-health"))
            history = _ok(client.get(f"/api/instances/{instance_id}/blocking-history"))
            web_health = _ok(client.get("/api/health"))
        out = {
            "enabled": instance["enabled"], "last_collect_ok_at": instance.get("last_collect_ok_at"),
            "last_collect_error": instance.get("last_collect_error"), "topology": health.get("topology"),
            "deadlock_detail_reason": history.get("deadlock_detail_reason"), "health": web_health,
        }
    elif command == "set-enabled":
        with _client() as client:
            out = {"enabled": _ok(client.patch(f"/api/instances/{int(args[0])}", json={"enabled": args[1] == "true"}))["enabled"]}
    else:
        raise SystemExit(f"bilinmeyen komut: {command}")
    print(json.dumps(out, default=str))


if __name__ == "__main__":
    main()
