"""CANLI 500 DÜZELTMESİ — silme öncesi bağımlılık sayımı eksikti (Faz 23).

**Belirti:** `DELETE /api/instances/1` canlıda 500. Hemen öncesindeki kontrol ise "bu
instance'a bağlı hiçbir kayıt yok — güvenle silinebilir" diyordu. İkisi çelişiyordu.

**Kök neden:** bağımlı tablo listesi ELLE yazılmıştı ve model listesiyle ayrışmıştı.
`instances.id`'ye foreign key ile bağlı 10 tablodan yalnızca 8'i biliniyordu; Faz 20'de
eklenen `prediction_outcomes` ve Faz 17'de eklenen `daily_state_snapshots` hiç girmemişti.
Sayım sıfır diyordu, silme foreign key ihlaliyle patlıyordu.

**Neden yerelde yakalanmadı:** SQLite foreign key zorlamasını varsayılan olarak KAPALI tutar.
Yerelde silme sessizce başarılı olup geride öksüz satır bırakıyordu; Postgres her zaman
zorluyor. `database.py` artık SQLite'ta da `PRAGMA foreign_keys=ON` yapıyor — bu olmadan
aşağıdaki testlerin hiçbiri hatayı yakalayamazdı.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from app.database import SessionLocal, engine, init_db
from app.models import (
    Application,
    Customer,
    DailyStateSnapshot,
    DatabaseGroup,
    GroupHealthSnapshot,
    Instance,
    MetricSample,
    Node,
    PredictionOutcome,
    Server,
)
from app.services.credentials import encrypt_secret
from app.services.deletion import dependent_columns
from tests.auth_helper import authed_client


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


async def _instance(session, **over) -> Instance:
    base = dict(
        name=f"del-{uuid.uuid4().hex[:8]}", engine="postgresql", host="h", port=5432,
        database="d", username="u", password=encrypt_secret("x"),
    )
    base.update(over)
    row = Instance(**base)
    session.add(row)
    await session.commit()
    return row


def _outcome(instance_id: int) -> PredictionOutcome:
    return PredictionOutcome(
        instance_id=instance_id, kind="database_size", metric_key="database_size_bytes",
        target_at=datetime.now(UTC) + timedelta(days=7), checkpoint_days=7.0,
        predicted_value=1.0, method="linear_regression", sample_count=10, span_days=10.0,
        r_squared=0.9, source="rollup", status="pending",
    )


# --- Ön koşul: kısıtlar gerçekten uygulanıyor mu ---------------------------------------------


async def test_sqlite_enforces_foreign_keys():
    """Bu KAPALIYSA aşağıdaki testlerin hiçbiri hatayı yakalayamaz — silme sessizce başarılı
    olur ve geride öksüz satır kalır. Hatanın yerelde görünmemesinin sebebi tam olarak buydu."""
    async with engine.begin() as conn:
        pragma = (await conn.execute(text("PRAGMA foreign_keys"))).scalar()
    assert pragma == 1, "SQLite foreign key zorlaması kapalı — testler yanıltıcı geçer"


# --- 1. Bağımlılık listesi eksiksiz mi -------------------------------------------------------


def test_the_dependency_list_is_derived_not_hand_written():
    """Elle yazılmış bir liste üçüncü kez ayrıştı (bkz. PredictionOut.advice,
    ReportFindingOut.facts). Liste artık metadata'dan türetiliyor."""
    tables = {t.name for t, _c, _n in dependent_columns("instances")}
    assert len(tables) >= 10, f"beklenenden az tablo bulundu: {sorted(tables)}"
    # Hatanın konusu olan iki tablo adıyla kilitleniyor.
    assert "prediction_outcomes" in tables, "Faz 20'de eklenen tablo yine listede yok"
    assert "daily_state_snapshots" in tables, "Faz 17'de eklenen tablo yine listede yok"


@pytest.mark.parametrize("target", ["instances", "customers", "applications", "database_groups", "servers"])
def test_every_delete_target_can_enumerate_its_dependents(target: str):
    """Sayım her hedef için çalışmalı; boş dönerse "bağlı kayıt yok" der ve aynı hata sınıfı
    geri gelir."""
    columns = dependent_columns(target)
    assert columns, f"{target} için bağımlı tablo bulunamadı — metadata taraması bozuk olabilir"


# --- 2. Asıl vaka: sayım ve silme tutarlı mı -------------------------------------------------


async def test_dependency_count_sees_prediction_outcomes():
    """CANLIDAKİ VAKA: bu tablo sayılmıyordu, sayım 0 diyordu."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add(_outcome(instance.id))
        await session.commit()

    async with await authed_client() as c:
        deps = (await c.get(f"/api/instances/{instance.id}/dependencies")).json()

    assert deps["prediction_outcomes"] == 1
    assert deps["total_records"] >= 1, "sayım hâlâ sıfır diyor"
    assert deps["breakdown"]["prediction_outcomes"] == 1


async def test_dependency_count_sees_daily_state_snapshots():
    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add(DailyStateSnapshot(
            instance_id=instance.id, day=date.today(), kind="parameters", payload={},
        ))
        await session.commit()

    async with await authed_client() as c:
        deps = (await c.get(f"/api/instances/{instance.id}/dependencies")).json()

    assert deps["daily_state_snapshots"] == 1
    assert deps["total_records"] >= 1


async def test_deleting_an_instance_with_hidden_dependents_returns_409_not_500():
    """Sayımın görmediği bir bağımlılık yüzünden 500 dönüyordu; artık ne engellediği yazılı
    bir 409 dönmeli."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add(_outcome(instance.id))
        await session.commit()

    async with await authed_client() as c:
        response = await c.delete(f"/api/instances/{instance.id}")

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "tahmin doğruluk kaydı" in detail, f"hangi tablonun engellediği yazılmamış: {detail}"


async def test_cascade_delete_removes_the_previously_missed_tables():
    """Cascade listesi de elle yazılmıştı ve aynı iki tabloyu atlıyordu."""
    async with SessionLocal() as session:
        instance = await _instance(session)
        session.add(_outcome(instance.id))
        session.add(DailyStateSnapshot(
            instance_id=instance.id, day=date.today(), kind="parameters", payload={},
        ))
        session.add(MetricSample(
            instance_id=instance.id, collected_at=datetime.now(UTC),
            active_connections=1, max_connections=100, cache_hit_ratio=99.0,
        ))
        await session.commit()

    async with await authed_client() as c:
        response = await c.delete(f"/api/instances/{instance.id}?cascade=true")

    assert response.status_code == 204, response.text

    async with SessionLocal() as session:
        assert await session.get(Instance, instance.id) is None
        for table in ("prediction_outcomes", "daily_state_snapshots", "metric_samples"):
            left = (
                await session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE instance_id = :i"), {"i": instance.id}
                )
            ).scalar_one()
            assert left == 0, f"{table} tablosunda {left} öksüz satır kaldı"


async def test_an_instance_with_no_dependents_still_deletes_cleanly():
    """Düzeltme, temiz silmeyi bozmamalı."""
    async with SessionLocal() as session:
        instance = await _instance(session)

    async with await authed_client() as c:
        assert (await c.delete(f"/api/instances/{instance.id}")).status_code == 204

    async with SessionLocal() as session:
        assert await session.get(Instance, instance.id) is None


async def test_linked_nodes_are_detached_not_deleted():
    """Düğüm cluster topolojisinin parçası; instance silinince bağlantısı kopmalı, kendisi
    yaşamalı."""
    async with SessionLocal() as session:
        suffix = uuid.uuid4().hex[:6]
        customer = Customer(name=f"c-{suffix}", type="private")
        session.add(customer)
        await session.commit()
        application = Application(customer_id=customer.id, name=f"a-{suffix}")
        session.add(application)
        await session.commit()
        group = DatabaseGroup(
            application_id=application.id, name=f"g-{suffix}", engine="postgresql",
            topology="patroni", environment="prod",
        )
        session.add(group)
        await session.commit()
        instance = await _instance(session)
        node = Node(group_id=group.id, name=f"n-{suffix}", instance_id=instance.id, port=5432)
        session.add(node)
        await session.commit()
        node_id = node.id

    async with await authed_client() as c:
        assert (await c.delete(f"/api/instances/{instance.id}?cascade=true")).status_code == 204

    async with SessionLocal() as session:
        survivor = await session.get(Node, node_id)
        assert survivor is not None, "düğüm silinmiş — bağlantısı koparılmalıydı"
        assert survivor.instance_id is None


# --- 3. Diğer silme akışları ------------------------------------------------------------------


async def test_deleting_a_customer_also_removes_its_servers():
    """ORM ilişkisi uygulamaları kapsıyordu ama `servers` kapsanmıyordu: müşteri silme
    Postgres'te foreign key ihlaline düşerdi."""
    async with SessionLocal() as session:
        suffix = uuid.uuid4().hex[:6]
        customer = Customer(name=f"cust-{suffix}", type="private")
        session.add(customer)
        await session.commit()
        session.add(Server(customer_id=customer.id, name=f"srv-{suffix}", host="h", os="linux"))
        await session.commit()
        customer_id = customer.id

    async with await authed_client() as c:
        response = await c.delete(f"/api/customers/{customer_id}")

    assert response.status_code == 204, response.text
    async with SessionLocal() as session:
        left = (
            await session.execute(
                text("SELECT count(*) FROM servers WHERE customer_id = :i"), {"i": customer_id}
            )
        ).scalar_one()
        assert left == 0, "müşterinin sunucuları geride kaldı"


async def test_deleting_a_group_also_removes_its_health_snapshot():
    """`group_health_snapshots` hiçbir ORM ilişkisiyle kapsanmıyordu."""
    async with SessionLocal() as session:
        suffix = uuid.uuid4().hex[:6]
        customer = Customer(name=f"gc-{suffix}", type="private")
        session.add(customer)
        await session.commit()
        application = Application(customer_id=customer.id, name=f"ga-{suffix}")
        session.add(application)
        await session.commit()
        group = DatabaseGroup(
            application_id=application.id, name=f"gg-{suffix}", engine="postgresql",
            topology="patroni", environment="prod",
        )
        session.add(group)
        await session.commit()
        session.add(GroupHealthSnapshot(group_id=group.id, overall="ok", report_json={}))
        await session.commit()
        group_id = group.id

    async with await authed_client() as c:
        response = await c.delete(f"/api/groups/{group_id}")

    assert response.status_code == 204, response.text
    async with SessionLocal() as session:
        left = (
            await session.execute(
                text("SELECT count(*) FROM group_health_snapshots WHERE group_id = :i"),
                {"i": group_id},
            )
        ).scalar_one()
        assert left == 0


async def test_deleting_a_group_detaches_instances_instead_of_deleting_them():
    """`instances.group_id` nullable: instance grubun parçası değil, ona bağlı. Grup silinince
    instance (ve metrik geçmişi) yaşamalı."""
    async with SessionLocal() as session:
        suffix = uuid.uuid4().hex[:6]
        customer = Customer(name=f"dc-{suffix}", type="private")
        session.add(customer)
        await session.commit()
        application = Application(customer_id=customer.id, name=f"da-{suffix}")
        session.add(application)
        await session.commit()
        group = DatabaseGroup(
            application_id=application.id, name=f"dg-{suffix}", engine="postgresql",
            topology="standalone", environment="prod",
        )
        session.add(group)
        await session.commit()
        instance = await _instance(session, group_id=group.id)
        group_id, instance_id = group.id, instance.id

    async with await authed_client() as c:
        assert (await c.delete(f"/api/groups/{group_id}")).status_code == 204

    async with SessionLocal() as session:
        survivor = await session.get(Instance, instance_id)
        assert survivor is not None, "instance silinmiş — yalnızca bağlantısı kopmalıydı"
        assert survivor.group_id is None


async def test_deleting_a_customer_cascades_the_whole_chain():
    """Müşteri → uygulama → grup → düğüm. Düz bir silme zincirin ortasında foreign key
    ihlaline düşerdi; temizlik özyinelemeli olmak zorunda."""
    async with SessionLocal() as session:
        suffix = uuid.uuid4().hex[:6]
        customer = Customer(name=f"ch-{suffix}", type="private")
        session.add(customer)
        await session.commit()
        application = Application(customer_id=customer.id, name=f"ah-{suffix}")
        session.add(application)
        await session.commit()
        group = DatabaseGroup(
            application_id=application.id, name=f"gh-{suffix}", engine="postgresql",
            topology="patroni", environment="prod",
        )
        session.add(group)
        await session.commit()
        session.add(Node(group_id=group.id, name=f"nh-{suffix}", port=5432))
        await session.commit()
        customer_id, application_id, group_id = customer.id, application.id, group.id

    async with await authed_client() as c:
        response = await c.delete(f"/api/customers/{customer_id}")

    assert response.status_code == 204, response.text
    async with SessionLocal() as session:
        assert await session.get(Application, application_id) is None
        assert await session.get(DatabaseGroup, group_id) is None
        left = (
            await session.execute(
                text("SELECT count(*) FROM nodes WHERE group_id = :i"), {"i": group_id}
            )
        ).scalar_one()
        assert left == 0, "düğümler geride kaldı"
