"""`/api/instances/test`, `/{id}/test`, `/{id}/test-config` — GERÇEK PostgreSQL'e karşı (Faz 31 Commit 10d madde D).

Test kapsam taraması (`scripts/coverage_matrix.py`) bu üç ucu P1 boşluk olarak işaretledi: hiçbir test bunları
GERÇEK bir sunucuya karşı koşturmuyordu — yalnızca `test_endpoint_status_codes.py`'de sahte/kapalı bağlantıyla
404/durum kodu doğrulanıyordu. Bankaya giden yol bu: kullanıcı sihirbazda "Bağlantıyı test et" düğmesine basıyor,
kısıtlı (salt okunur) rolle gerçek bir sonuç bekliyor.
"""

from __future__ import annotations

import uuid

import pytest

from app.database import init_db
from app.models import Instance
from app.services.credentials import encrypt_secret
from tests.auth_helper import authed_client
from tests.live_pg import LIVE_DSNS, RESTRICTED_ROLE, ROLE_PASSWORD, SKIP_REASON, prepare_restricted_database, restricted_target

pytestmark = pytest.mark.skipif(not LIVE_DSNS, reason=SKIP_REASON)


def log(title, value) -> None:
    print(f"\n  [{title}] {value}")


async def _register(target) -> Instance:
    from app.database import SessionLocal

    async with SessionLocal() as session:
        row = Instance(name=f"conn-test-{uuid.uuid4().hex[:6]}", engine="postgresql", host=target.host,
                       port=target.port, database=target.database, username=target.username,
                       password=encrypt_secret(target.password), options=target.options or None, enabled=True)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def test_post_instances_test_succeeds_with_the_restricted_role_and_fails_with_a_wrong_password():
    """`POST /api/instances/test` — sihirbazın kaydetmeden ÖNCE çağırdığı yol, kısıtlı rolle."""
    await init_db()
    dsn = LIVE_DSNS[0]
    await prepare_restricted_database(dsn)
    target = restricted_target(dsn)
    body = {
        "name": "wizard-probe", "engine": "postgresql", "host": target.host, "port": target.port,
        "database": target.database, "username": target.username, "password": target.password,
    }
    async with await authed_client() as client:
        ok = await client.post("/api/instances/test", json=body)
        assert ok.status_code == 200 and ok.json()["ok"] is True, ok.text

        wrong = dict(body, password="kesinlikle-yanlis-bir-sifre")
        bad = await client.post("/api/instances/test", json=wrong)
        log("kısıtlı rol / yanlış şifre", (ok.json(), bad.json()))
        assert bad.status_code == 200 and bad.json()["ok"] is False, bad.text
        assert bad.json()["message"], "başarısızlık nedeni boş olmamalı"


async def test_post_instance_id_test_uses_the_stored_credentials_against_the_real_server():
    """`POST /api/instances/{id}/test` — kayıtlı bir instance'ın şifresini KENDİSİ çözüp bağlanıyor."""
    await init_db()
    dsn = LIVE_DSNS[0]
    await prepare_restricted_database(dsn)
    instance = await _register(restricted_target(dsn))
    async with await authed_client() as client:
        result = await client.post(f"/api/instances/{instance.id}/test")
        log("kayıtlı instance testi", result.json())
        assert result.status_code == 200 and result.json()["ok"] is True, result.text

        missing = await client.post("/api/instances/999999999/test")
        assert missing.status_code == 404, "olmayan instance 404 vermeli (negatif kontrol)"


async def test_post_instance_id_test_config_reuses_stored_password_when_blank_and_tries_override_when_given():
    """`POST /api/instances/{id}/test-config` — düzenleme formunun 'Bağlantı testi' düğmesi.

    Şifre alanı BOŞ bırakılırsa (form var olanı hiç göstermiyor) KAYITLI şifre denenmeli — Faz 16-B İŞ 2'nin
    kendi düzelttiği hata tam bu: eskiden boş şifre gönderiliyor ve her zaman başarısız oluyordu.
    """
    await init_db()
    dsn = LIVE_DSNS[0]
    await prepare_restricted_database(dsn)
    target = restricted_target(dsn)
    instance = await _register(target)
    async with await authed_client() as client:
        blank_password = await client.post(f"/api/instances/{instance.id}/test-config", json={"port": target.port})
        log("boş şifre (kayıtlıyı kullanmalı)", blank_password.json())
        assert blank_password.status_code == 200 and blank_password.json()["ok"] is True, blank_password.text

        overridden = await client.post(f"/api/instances/{instance.id}/test-config",
                                       json={"password": "kesinlikle-yanlis-bir-sifre"})
        assert overridden.status_code == 200 and overridden.json()["ok"] is False, "verilen şifre denenmeli, kayıtlı değil"

        # Negatif kontrol: gerçekten YENİ (doğru) bir şifre verilirse o da denenebilmeli — sabit kayıtlı
        # şifreye TAKILI kalmadığını kanıtlar. Kısıtlı rolün kendi şifresini tekrar vermek yeterli.
        same_but_explicit = await client.post(f"/api/instances/{instance.id}/test-config",
                                              json={"password": target.password})
        assert same_but_explicit.status_code == 200 and same_but_explicit.json()["ok"] is True
