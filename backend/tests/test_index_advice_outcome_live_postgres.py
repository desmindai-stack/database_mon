"""Index önerisinin ÖLÇÜLMÜŞ etkisi — gerçek sunucuda, gerçek çağrı yoluyla (Faz 31 Commit 5).

API (POST /advice) → `run_index_advice` → `register_outcomes` "önce" planını alıyor →
test hedefte önerilen DDL'i GERÇEKTEN çalıştırıyor → zamanlayıcının gerçek tur fonksiyonu
(`outcome_tick`) yeni index'i bulup "sonra" planını alıyor → API (GET /advice-outcomes) ölçümü
gösteriyor. Negatif kontrol: index kurulmadan tur hiçbir "sonra" ölçümü üretmiyor.
pg_monitor rolünde (tabloda SELECT yok) sonuç boş değil "ölçülemedi" + GRANT.
"""

from __future__ import annotations

import uuid

import pytest

from app.database import SessionLocal, init_db
from app.models import Instance
from app.services.credentials import encrypt_secret
from app.services.index_advice_outcome import (
    STATUS_MEASURED,
    STATUS_NOT_MEASURABLE,
    STATUS_WAITING,
    outcome_tick,
)
from tests.live_pg import LIVE_DSNS, SKIP_REASON, dsn_id, prepare_live_database, target_for

asyncpg = pytest.importorskip("asyncpg")
pytestmark = pytest.mark.skipif(not LIVE_DSNS, reason=SKIP_REASON)

QUERY = "SELECT id, name FROM adv_users WHERE email = $1"


def log(title, value) -> None:
    print(f"\n  [{title}] {value}")


@pytest.fixture(params=LIVE_DSNS, ids=dsn_id)
def dsn(request):
    return request.param


@pytest.fixture
async def admin(dsn):
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    await prepare_live_database(conn)
    try:
        yield conn
    finally:
        await prepare_live_database(conn)  # idx_dbace_% index'lerini düşürür
        await conn.close()


async def _instance(dsn: str, role: str) -> int:
    t = target_for(dsn, role)
    await init_db()
    async with SessionLocal() as session:
        instance = Instance(
            name=f"outcome-it-{uuid.uuid4().hex[:8]}", engine="postgresql", host=t.host, port=t.port,
            database=t.database, username=t.username, password=encrypt_secret(t.password), enabled=True,
        )
        session.add(instance)
        await session.commit()
        return instance.id


async def _outcomes(client, instance_id: int) -> list[dict]:
    response = await client.get(f"/api/queries/{instance_id}/advice-outcomes")
    assert response.status_code == 200, response.text
    return response.json()


async def test_before_and_after_plan_is_measured_once_the_index_exists(admin, dsn):
    from tests.auth_helper import authed_client

    version = (await admin.fetchval("SHOW server_version")).split(" ")[0]
    instance_id = await _instance(dsn, "super")
    tag = uuid.uuid4().hex[:6]
    query = f"{QUERY} /* outcome-{tag} */"
    async with await authed_client() as client:
        report = (await client.post(f"/api/queries/{instance_id}/advice", json={"query": query, "calls": 500})).json()
        assert report["status"] == "advised", report
        ddl = report["advice"][0]["index_ddl"]
        assert [o["status"] for o in report["outcomes"]] == [STATUS_WAITING]
        before = report["outcomes"][0]
        log(f"PG {version} önce", {k: before[k] for k in ("index_ddl", "before_cost", "before_indexes_used")})
        assert before["before_cost"] > 0 and before["after_cost"] is None
        assert before["measured_cost_reduction_pct"] is None, "index kurulmadan yüzde yok"

        # NEGATİF KONTROL: index kurulmadan tur "sonra" ölçümü üretmiyor.
        totals = await outcome_tick()
        waiting = [o for o in await _outcomes(client, instance_id) if o["index_ddl"] == ddl][0]
        log("index yokken tur", {"tur": totals, "durum": waiting["status"], "sonra": waiting["after_cost"]})
        assert waiting["status"] == STATUS_WAITING and waiting["after_cost"] is None

        await admin.execute(ddl)
        await admin.execute("ANALYZE adv_users")
        totals = await outcome_tick()
        measured = [o for o in await _outcomes(client, instance_id) if o["index_ddl"] == ddl][0]
        log("index kurulduktan sonra", {k: measured[k] for k in (
            "status", "before_cost", "after_cost", "after_index_name", "after_indexes_used",
            "after_uses_new_index", "measured_cost_reduction_pct", "source_label")} | {"tur": totals})
        assert measured["status"] == STATUS_MEASURED
        assert measured["after_uses_new_index"] is True
        assert measured["after_cost"] < measured["before_cost"]
        assert measured["measured_cost_reduction_pct"] > 0
        assert "Ölçüldü" in measured["source_label"] and "çalıştırılmadı" in measured["source_label"]

        # Aynı öneri tekrar istenince "önce" ölçümü EZİLMİYOR (index artık var; önce değeri korunmalı).
        # Farklı yorum: API önbelleğini atlıyor, parmak izi (yorumsuz metin) aynı kalıyor.
        again = (await client.post(f"/api/queries/{instance_id}/advice",
                                   json={"query": f"{QUERY} /* outcome-{tag}-2 */", "calls": 500})).json()
        kept = [o for o in await _outcomes(client, instance_id) if o["index_ddl"] == ddl]
        log("tekrar öneri", {"durum": again["status"], "kayıt": [(o["status"], o["before_cost"]) for o in kept]})
        assert [o["before_cost"] for o in kept if o["status"] == STATUS_MEASURED] == [before["before_cost"]]


async def test_monitor_role_without_select_is_not_measurable_with_a_grant(admin, dsn):
    from tests.auth_helper import authed_client

    instance_id = await _instance(dsn, "monitor")
    query = f"SELECT id FROM orders WHERE status = $1 AND created_at > $2 /* outcome-mon-{uuid.uuid4().hex[:6]} */"
    async with await authed_client() as client:
        report = (await client.post(f"/api/queries/{instance_id}/advice", json={"query": query, "calls": 500})).json()
    assert report["status"] == "advised", report
    log("pg_monitor", [(o["status"], o["note"]) for o in report["outcomes"]])
    assert report["outcomes"] and all(o["status"] == STATUS_NOT_MEASURABLE for o in report["outcomes"])
    assert all("GRANT SELECT ON public.orders" in o["note"] for o in report["outcomes"])
    assert all(o["before_cost"] is None and o["measured_cost_reduction_pct"] is None for o in report["outcomes"])
