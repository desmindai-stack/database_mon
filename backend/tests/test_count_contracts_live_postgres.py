"""Sayı ↔ liste sözleşmeleri GERÇEK veride (Faz 31 Commit 7).

Kurulum gerçek: sihirbazla veritabanı grubu (kısıtlı pg_monitor rolü), gerçek iş yükü (yavaş uygulama
sorguları, dbace'in kendi imzalı sorguları, katalog sorgusu), iki gerçek toplama turu, bir sağlık raporu.

Sonra OpenAPI şemasından (elle liste yok) sayı alanı taşıyan HER GET ucu bulunuyor, çağrılıyor ve
yanıttaki her liste bildirimli sayı — iç içe modeller dahil — aynı yanıttaki listeden yeniden
hesaplanıyor. Desteklenmeyen yol parametresi olan uç testi kırmızıya çeviriyor (yeni uç sessizce
kapsam dışı kalmasın). `external:` bildirimleri adlarındaki uçla karşılaştırılıyor.

Tuning'e özgü: "N yavaş ortalama süreli sorgu" içgörüsündeki N, içgörünün bağlantısının açtığı liste
görünümündeki (ortalama süreye göre ilk 20) ≥50 ms kalem sayısına eşit; teşhis paneli listeyle aynı
sorguları gösteriyor. Commit 7 öncesi aynı veride 2 ↔ 1 ve 10 ↔ 1 idi.
"""

from __future__ import annotations

import asyncio
import re
import typing
import uuid

import pytest
from pydantic import BaseModel

import app.schemas as schemas
from app.database import SessionLocal, init_db
from app.models import Instance
from app.services import collection as collection_module
from app.services.count_contracts import KIND_EXTERNAL, discover_count_fields, mismatches
from app.services.slow_query_selection import INSIGHT_LIST_LIMIT, INSIGHT_LIST_SORT, SLOW_MEAN_MS
from tests.live_pg import LIVE_DSNS, ROLE_PASSWORD, SKIP_REASON, dsn_id, prepare_live_database, target_for

asyncpg = pytest.importorskip("asyncpg")
pytestmark = pytest.mark.skipif(not LIVE_DSNS, reason=SKIP_REASON)


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
        await conn.close()


async def _workload(dsn: str, tag: str) -> None:
    app_conn = await asyncpg.connect(**_kw(dsn, "app"))
    try:
        for i in range(3):
            await app_conn.fetchval(
                f"SELECT count(*) AS {tag}_{i} FROM orders WHERE status = $1 AND (SELECT pg_sleep(0.06)) IS NOT NULL", "paid")
        for _ in range(5):
            await app_conn.fetchval(f"SELECT count(*) AS {tag}_fast FROM customers WHERE segment = $1", "gold")
    finally:
        await app_conn.close()
    admin_conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        await admin_conn.fetchval(
            "SELECT count(*) FROM pg_catalog.pg_class c WHERE (SELECT pg_sleep(0.07)) IS NOT NULL LIMIT 1")
    finally:
        await admin_conn.close()


def _kw(dsn: str, role: str) -> dict:
    t = target_for(dsn, role)
    return dict(host=t.host, port=t.port, database=t.database, user=t.username, password=ROLE_PASSWORD,
                statement_cache_size=0)


async def _collect(instance_id: int) -> None:
    collection_module._last_slow_query_at.pop(instance_id, None)
    async with SessionLocal() as session:
        await collection_module.collect_instance(await session.get(Instance, instance_id), session)
        await session.commit()
    await asyncio.sleep(1.1)


def _nested_models(model: type[BaseModel], seen=None) -> dict[str, type[BaseModel]]:
    """Yanıt modelinin alan yolu → alt model (iç içe sayı bildirimleri için)."""
    out: dict[str, type[BaseModel]] = {}
    for name, field in model.model_fields.items():
        ann = field.annotation
        args = [a for a in typing.get_args(ann) if a is not type(None)] or [ann]
        for candidate in args:
            if isinstance(candidate, type) and issubclass(candidate, BaseModel) and name not in ("totals", "summary"):
                out[name] = candidate
    return out


def _check(model: type[BaseModel], payload, fields, problems: list[str], where: str) -> int:
    if not isinstance(payload, dict):
        return 0
    checked = 0
    found = mismatches(model.__name__, payload, fields)
    checked += sum(1 for f in fields if f.model == model.__name__ and f.kind == "list")
    problems += [f"{where}: {p}" for p in found]
    for name, sub in _nested_models(model).items():
        checked += _check(sub, payload.get(name), fields, problems, f"{where}.{name}")
    return checked


async def test_every_count_matches_its_list_on_real_data(admin, dsn):
    from tests.auth_helper import authed_client

    version = (await admin.fetchval("SHOW server_version")).split(" ")[0]
    await admin.execute("SELECT pg_stat_statements_reset()")
    t = target_for(dsn, "monitor")
    fields = discover_count_fields(schemas)
    tag = f"cc{uuid.uuid4().hex[:6]}"
    await init_db()
    async with await authed_client() as client:
        customer = (await client.post("/api/customers", json={"name": f"sayı-{tag}"})).json()
        application = (await client.post("/api/applications", json={"customer_id": customer["id"], "name": "app"})).json()
        group = (await client.post("/api/wizard/database-groups", json={
            "application_id": application["id"], "group_name": f"g-{tag}", "engine": "postgresql", "topology": "standalone",
            "nodes": [{"server_name": f"s-{tag}", "host": t.host, "port": t.port, "database": t.database,
                       "db_username": t.username, "db_password": ROLE_PASSWORD}],
        })).json()
        instance_id = (await client.get(f"/api/groups/{group['id']}/nodes")).json()[0]["instance_id"]

        await _collect(instance_id)
        for _ in range(2):
            await _workload(dsn, tag)
            await _collect(instance_id)

        report = await client.post("/api/reports/run", json={"scope_type": "instance", "scope_id": instance_id, "period_days": 1})
        assert report.status_code in (200, 201, 202), report.text
        report_id = report.json()["id"]
        for _ in range(120):
            if (await client.get(f"/api/reports/{report_id}")).json().get("status") in ("ready", "completed", "done", "failed", "error"):
                break
            await asyncio.sleep(0.5)

        spec = (await client.get("/openapi.json")).json()
        params = {"instance_id": instance_id, "group_id": group["id"], "report_id": report_id}
        model_names = {f.model for f in fields}
        checked_endpoints: dict[str, int] = {}
        problems: list[str] = []
        unsupported: list[str] = []
        for path, ops in spec["paths"].items():
            get = ops.get("get")
            if not get:
                continue
            schema = get.get("responses", {}).get("200", {}).get("content", {}).get("application/json", {}).get("schema", {})
            ref = schema.get("$ref", "").rsplit("/", 1)[-1]
            model = getattr(schemas, ref, None)
            if model is None or not ({model.__name__} | {m.__name__ for m in _nested_models(model).values()}) & model_names:
                continue
            placeholders = re.findall(r"\{([a-z_]+)\}", path)
            if set(placeholders) - set(params):
                unsupported.append(path)
                continue
            response = await client.get(path.format(**params))
            assert response.status_code == 200, (path, response.status_code, response.text[:300])
            checked_endpoints[path] = _check(model, response.json(), fields, problems, path)

        externals = {}
        dashboard = (await client.get("/api/dashboard/summary")).json()
        for field in fields:
            if field.kind != KIND_EXTERNAL:
                continue
            match = re.search(r"GET (/api/[a-z/_-]+)(?:\s|$)", field.contract)
            if field.model == "DashboardSummaryOut" and match and "{" not in field.contract:
                listed = (await client.get(match.group(1))).json()
                externals[field.path] = (dashboard["totals"][field.path.split(".")[-1]], len(listed))

        insights = (await client.get(f"/api/instances/{instance_id}/insights")).json()
        view = (await client.get(f"/api/queries/{instance_id}",
                                 params={"sort": INSIGHT_LIST_SORT, "limit": INSIGHT_LIST_LIMIT})).json()
        default_view = (await client.get(f"/api/queries/{instance_id}", params={"sort": "total", "limit": 10})).json()
        diagnostics = (await client.get(f"/api/queries/{instance_id}/diagnostics", params={"limit": 10})).json()

    log(f"PG {version} uçlar (liste bildirimli sayı adedi)", checked_endpoints)
    log("external", externals)
    slow = next((i for i in insights["insights"] if i["title"].endswith("yavaş ortalama süreli sorgu")), None)
    slow_in_view = [q for q in view["items"] if q["mean_time_ms"] >= SLOW_MEAN_MS]
    log("tuning", {"içgörü": slow and slow["title"], "bağlantı": slow and slow["action_params"],
                   "listede ≥50ms": len(slow_in_view), "gizli": (view["filtered_system"], view["filtered_insignificant"]),
                   "teşhis": [d["query"][:50] for d in diagnostics["diagnoses"]],
                   "liste": [q["query"][:50] for q in default_view["items"]]})

    assert not unsupported, f"yol parametresi desteklenmeyen sayı ucu: {unsupported}"
    assert not problems, "\n".join(problems)
    assert len(checked_endpoints) >= 10 and sum(checked_endpoints.values()) >= 30
    assert all(a == b for a, b in externals.values()), externals
    assert slow is not None and int(slow["title"].split()[0]) == len(slow_in_view) >= 1
    assert slow["action_params"] == {"sort": INSIGHT_LIST_SORT, "limit": str(INSIGHT_LIST_LIMIT)}
    assert [d["query"] for d in diagnostics["diagnoses"]] == [q["query"] for q in default_view["items"]]
    assert view["filtered_system"] > 0, "dbace/katalog sorguları gizlenmiş olmalı — iş yükü bunları üretti"
