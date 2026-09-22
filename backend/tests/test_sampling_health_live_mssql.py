"""'Örnek yok' ile 'örnekleyici bağlanamıyor' ayrımı — GERÇEK SQL Server, gerçek KİMLİK DOĞRULAMA hatası
(Faz 31 Commit 10c-B).

Yanlış parolayla bir instance kaydedilir: örnekleyici GERÇEKTEN başarısız olur (pyodbc'nin gerçek "Login failed"
hatası). Bu dosya GERÇEK HTTP yolundan (veritabanı yükü ve bloklama geçmişi uçları) "ölçülemedi + gerekçe"
mesajının çıktığını, parola düzeltilince mesajın normale döndüğünü kanıtlıyor. Kısıtlı izleme kimliği
(`dbace_monitor`, VIEW SERVER STATE + VIEW DATABASE STATE, sysadmin YOK) — yalnızca PAROLASI yanlış.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.database import SessionLocal, init_db
from app.models import Instance
from app.services import wait_sampling
from app.services.credentials import encrypt_secret
from tests.auth_helper import authed_client
from tests.live_mssql import (
    APP_DATABASE,
    MONITOR_LOGIN,
    MONITOR_PASSWORD,
    MSSQL_SKIP_REASON,
    MSSQL_TARGETS,
    prepare_monitor_login,
    standalone_target,
)


def log(title, value) -> None:
    print(f"\n  [{title}] {value}")


pyodbc = pytest.importorskip("pyodbc")
pyodbc.pooling = False
pytest.importorskip("aioodbc")
pytestmark = pytest.mark.skipif("standalone" not in MSSQL_TARGETS, reason=MSSQL_SKIP_REASON)


@pytest.fixture(scope="module", autouse=True)
def monitor_login():
    prepare_monitor_login()


@pytest.fixture(autouse=True)
async def clean_state():
    await init_db()
    wait_sampling.reset_state()
    yield
    wait_sampling.reset_state()


async def _create_instance(password: str) -> Instance:
    target = standalone_target(MONITOR_LOGIN, password, APP_DATABASE)
    async with SessionLocal() as session:
        row = Instance(name="health-live-mssql", engine="sqlserver", host=target.host, port=target.port,
                       database=target.database, username=target.username,
                       password=encrypt_secret(password), options=target.options or None, enabled=True)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def test_an_authentication_failure_makes_the_screens_say_measurement_failed_and_recovery_clears_it():
    instance = await _create_instance("Wrong!Pass_2026")  # GERÇEK yanlış parola: dbace_monitor'ün DEĞİL

    await wait_sampling._sample_instance(instance, datetime.now(UTC))  # gerçek "Login failed" hatası
    async with SessionLocal() as session:
        row = await session.get(Instance, instance.id)
    log("kaydedilen hata", row.last_sample_error)
    assert row.last_sample_error is not None and "kimlik doğrulama" in row.last_sample_error.lower()
    assert row.last_sample_error_at is not None and row.last_sample_ok_at is None

    async with await authed_client() as client:
        load = (await client.get(f"/api/instances/{instance.id}/database-load?hours=1")).json()
        history = (await client.get(f"/api/instances/{instance.id}/blocking-history")).json()
    log("veritabanı yükü", load["unavailable_reason"])
    log("bloklama geçmişi", history["unavailable_reason"])
    assert load["unavailable_reason"].startswith("Ölçülemedi:")
    assert history["unavailable_reason"].startswith("Ölçülemedi:")
    assert "birkaç dakika sürer" not in load["unavailable_reason"], \
        "arızalı hedef 'yeni instance, bekle' gibi gösterilmemeli"
    assert "kayda değer" not in history["unavailable_reason"], \
        "arızalı hedef 'olay görülmedi' gibi gösterilmemeli"

    # TOPARLANMA: parola düzeltiliyor (DBA'nın .env/arayüzden yapacağı iş).
    async with SessionLocal() as session:
        row = await session.get(Instance, instance.id)
        row.password = encrypt_secret(MONITOR_PASSWORD)
        await session.commit()
        await session.refresh(row)
    wait_sampling.reset_state()  # yeni bağlantı kurulsun (eski sampler nesnesi eski parolayı taşıyordu)
    await wait_sampling._sample_instance(row, datetime.now(UTC))

    async with SessionLocal() as session:
        recovered = await session.get(Instance, instance.id)
    log("toparlanma", {"ok_at": recovered.last_sample_ok_at, "error_at": recovered.last_sample_error_at})
    assert recovered.last_sample_ok_at is not None
    assert recovered.last_sample_ok_at >= recovered.last_sample_error_at

    async with await authed_client() as client:
        load = (await client.get(f"/api/instances/{instance.id}/database-load?hours=1")).json()
    log("toparlanma sonrası veritabanı yükü", load["unavailable_reason"])
    assert not (load["unavailable_reason"] or "").startswith("Ölçülemedi:")
