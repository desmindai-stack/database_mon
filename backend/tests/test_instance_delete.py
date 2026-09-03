"""Faz 16-B İŞ 2 — instance silme ve kaydetmeden bağlantı testi.

Bildirilen sorun: "kullanılmayan instance silinemiyor". Kök neden, instance'a işaret eden 7
tablo + nodes.instance_id foreign key kısıtlarıydı; düz DELETE veritabanı hatasıyla düşüyordu.
Artık: bağlı kayıt varsa 409 + sayılar, cascade=true ile birlikte siliniyor, düğümler
silinmiyor sadece bağlantıları kopuyor.
"""

from __future__ import annotations

import uuid

import httpx

from tests.auth_helper import authed_client


async def _client() -> httpx.AsyncClient:
    return await authed_client()


async def _make_instance(c: httpx.AsyncClient, name_prefix: str = "del") -> dict:
    suffix = uuid.uuid4().hex[:8]
    r = await c.post(
        "/api/instances",
        json={
            "name": f"{name_prefix}-{suffix}",
            "engine": "postgresql",
            "host": "127.0.0.1",
            "port": 5432,
            "database": "postgres",
            "username": "postgres",
            "password": "secret",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_unused_instance_deletes_cleanly():
    async with await _client() as c:
        inst = await _make_instance(c)

        deps = (await c.get(f"/api/instances/{inst['id']}/dependencies")).json()
        assert deps["total_records"] == 0
        assert deps["linked_nodes"] == []

        r = await c.delete(f"/api/instances/{inst['id']}")
        assert r.status_code == 204, r.text
        assert (await c.get(f"/api/instances/{inst['id']}")).status_code == 404


async def test_instance_with_dependent_records_refuses_without_cascade():
    async with await _client() as c:
        inst = await _make_instance(c)
        # Alarm kuralı en ucuz bağımlılık: API üzerinden oluşturulabiliyor.
        rule = await c.post(
            "/api/alerts/rules",
            json={
                "instance_id": inst["id"],
                "name": "del-test-rule",
                "metric": "connections_total",
                "operator": ">",
                "threshold": 100,
                "severity": "warning",
            },
        )
        assert rule.status_code in (200, 201), rule.text

        deps = (await c.get(f"/api/instances/{inst['id']}/dependencies")).json()
        assert deps["alert_rules"] == 1
        assert deps["total_records"] == 1

        blocked = await c.delete(f"/api/instances/{inst['id']}")
        assert blocked.status_code == 409
        assert "cascade" in blocked.json()["detail"]

        # Instance hâlâ duruyor — reddedilen silme yarım iş bırakmadı.
        assert (await c.get(f"/api/instances/{inst['id']}")).status_code == 200

        ok = await c.delete(f"/api/instances/{inst['id']}?cascade=true")
        assert ok.status_code == 204, ok.text
        assert (await c.get(f"/api/instances/{inst['id']}")).status_code == 404


async def test_cascade_delete_detaches_node_instead_of_deleting_it():
    """Düğüm cluster topolojisinin parçası — instance silinince düğüm silinmemeli, sadece
    bağlantısı kopmalı ki topoloji bozulmasın."""
    suffix = uuid.uuid4().hex[:8]
    async with await _client() as c:
        cust = (await c.post("/api/customers", json={"name": f"del-node-{suffix}", "type": "public"})).json()
        app_ = (await c.post("/api/applications", json={"customer_id": cust["id"], "name": f"app-{suffix}"})).json()
        group = (
            await c.post(
                "/api/wizard/database-groups",
                json={
                    "application_id": app_["id"],
                    "group_name": "del-standalone",
                    "engine": "postgresql",
                    "topology": "standalone",
                    "nodes": [
                        {
                            "server_name": f"srv-{suffix}",
                            "host": "node-1.internal",
                            "port": 5432,
                            "database": "postgres",
                            "db_username": "postgres",
                            "db_password": "x",
                            "role_hint": "primary",
                        }
                    ],
                },
            )
        ).json()

        nodes = (await c.get(f"/api/groups/{group['id']}/nodes")).json()
        assert len(nodes) == 1
        node = nodes[0]
        instance_id = node["instance_id"]
        assert instance_id is not None

        deps = (await c.get(f"/api/instances/{instance_id}/dependencies")).json()
        assert [n["id"] for n in deps["linked_nodes"]] == [node["id"]]

        assert (await c.delete(f"/api/instances/{instance_id}")).status_code == 409

        assert (await c.delete(f"/api/instances/{instance_id}?cascade=true")).status_code == 204

        after = (await c.get(f"/api/nodes/{node['id']}")).json()
        assert after["instance_id"] is None, "düğüm silinmemeli, sadece bağlantısı kopmalı"


async def test_test_config_uses_stored_password_when_left_blank(monkeypatch):
    """Düzenleme formu var olan şifreyi göstermez; boş bırakılırsa test SAKLANAN şifreyi
    kullanmalı — eskiden boş şifreyle denendiği için test hep başarısız oluyordu."""
    import app.routers.instances as instances_module

    seen: dict = {}

    class _FakeCollector:
        def __init__(self, target):
            seen["target"] = target

        async def test_connection(self):
            return True, "Connection successful", {"version": "PostgreSQL 16"}

    async with await _client() as c:
        inst = await _make_instance(c, "testcfg")
        monkeypatch.setattr(instances_module, "get_collector", lambda engine, target: _FakeCollector(target))

        r = await c.post(f"/api/instances/{inst['id']}/test-config", json={"host": "yeni-host"})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

        target = seen["target"]
        assert target.host == "yeni-host"  # formdaki değişiklik kullanıldı
        assert target.password == "secret"  # şifre kayıtlıdan tamamlandı
        assert target.database == "postgres"  # gönderilmeyen alanlar kayıtlıdan geldi


async def test_test_config_prefers_supplied_password():
    import app.routers.instances as instances_module

    seen: dict = {}

    class _FakeCollector:
        def __init__(self, target):
            seen["target"] = target

        async def test_connection(self):
            return False, "auth failed", {}

    async with await _client() as c:
        inst = await _make_instance(c, "testcfg2")
        original = instances_module.get_collector
        instances_module.get_collector = lambda engine, target: _FakeCollector(target)  # type: ignore[assignment]
        try:
            r = await c.post(
                f"/api/instances/{inst['id']}/test-config", json={"password": "yeni-sifre"}
            )
            assert r.status_code == 200
            assert r.json()["ok"] is False
            assert seen["target"].password == "yeni-sifre"
        finally:
            instances_module.get_collector = original  # type: ignore[assignment]
