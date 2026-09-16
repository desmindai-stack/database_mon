"""SQL Server analizleri ARAYÜZE ULAŞIYOR mu (Faz 30 İŞ 2).

Faz 29 İŞ 2c'de SQL Server için `collect_schema_health` yazıldı: motorun KENDİ eksik index
önerileri (`sys.dm_db_missing_index_*`) ve kullanılmayan index'ler. Ama uç noktada
`engine != "postgresql"` koruması vardı ve 400 dönüyordu — analiz üretiliyor, hiçbir ekrana
ulaşmıyordu. Kod yazılmış olması, çalıştığı anlamına gelmiyordu.

Bu dosya iki şeyi koruyor: ucun SQL Server'da açık olduğu, ve ÖNERİNİN KAYNAĞININ ekranda
belli olduğu.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

import app.routers.instances as instances_router
from tests.auth_helper import authed_client

FRONTEND = __import__("pathlib").Path(__file__).resolve().parents[2] / "frontend" / "src"


class _FakeCollector:
    """Yalnızca `collect_schema_health` sunuyor — uç noktanın ihtiyacı bu."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def collect_schema_health(self, limit: int = 50) -> dict[str, Any]:
        return self._payload


def _sqlserver_payload() -> dict[str, Any]:
    return {
        "unused_indexes": [],
        "bloated_tables": [],
        "vacuum_lag": [],
        "missing_indexes": [
            {
                "schema_name": "dbo",
                "table_name": "Orders",
                "equality_columns": "[CustomerId]",
                "inequality_columns": "[CreatedAt]",
                "included_columns": "[Total]",
                "user_seeks": 1200,
                "user_scans": 3,
                "avg_user_impact": 92.5,
                "avg_total_user_cost": 18.4,
                "improvement_measure": 20430.6,
                "last_user_seek": None,
            }
        ],
        "errors": {},
        "totals": {
            "unused_indexes": 0,
            "unused_index_bytes": 0,
            "bloated_tables": 0,
            "vacuum_lag_tables": 0,
            "missing_indexes": 1,
        },
    }


async def _make_instance(c: httpx.AsyncClient, engine: str, port: int) -> int:
    r = await c.post(
        "/api/instances",
        json={
            "name": f"mssql-surface-{uuid.uuid4().hex[:8]}",
            "engine": engine,
            "host": "127.0.0.1",
            "port": port,
            "database": "master",
            "username": "sa",
            "password": "x",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_schema_health_is_reachable_for_sqlserver(monkeypatch):
    """ASIL REGRESYON: uç eskiden 400 dönüyordu, analiz hiçbir ekrana ulaşmıyordu."""
    monkeypatch.setattr(
        instances_router, "get_collector", lambda engine, target: _FakeCollector(_sqlserver_payload())
    )
    async with await authed_client() as c:
        instance_id = await _make_instance(c, "sqlserver", 1433)
        response = await c.get(f"/api/instances/{instance_id}/schema-health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["missing_indexes"]) == 1


async def test_the_engines_own_suggestion_arrives_with_runnable_ddl_and_advice(monkeypatch):
    """Beş parçalı öneri standardı: neden, adımlar, komut, dikkat, doğrulama."""
    monkeypatch.setattr(
        instances_router, "get_collector", lambda engine, target: _FakeCollector(_sqlserver_payload())
    )
    async with await authed_client() as c:
        instance_id = await _make_instance(c, "sqlserver", 1433)
        response = await c.get(f"/api/instances/{instance_id}/schema-health")

    row = response.json()["missing_indexes"][0]
    assert row["index_ddl"].startswith("CREATE NONCLUSTERED INDEX")
    # Eşitlik kolonu anahtarda, INCLUDE ayrı: sıra yanlışsa index çalışmaz.
    assert row["index_ddl"].index("[CustomerId]") < row["index_ddl"].index("INCLUDE")

    advice = row["advice"]
    assert advice["why"], "neden yok"
    assert advice["steps"], "adım yok"
    assert any(step.get("command") for step in advice["steps"]), "komut yok"
    assert advice["cautions"], "dikkat notu yok"
    assert advice["verification"], "doğrulama sorgusu yok"


async def test_mongodb_still_says_it_has_no_schema_health(monkeypatch):
    """"Ölçüm yok"u boş bir ekranla "sorun yok" gibi göstermek yanlış olurdu."""
    async with await authed_client() as c:
        instance_id = await _make_instance(c, "mongodb", 27017)
        response = await c.get(f"/api/instances/{instance_id}/schema-health")
    assert response.status_code == 400


# --- Arayüz: kaynağın belli olması --------------------------------------------------------


def test_the_engines_own_suggestions_are_labelled_as_such():
    """İki öneri kaynağı karışırsa kullanıcı hangisine güveneceğini bilemez: motorunki
    planlama sırasında birikmiş gerçek talep, bizimki tek bir sorgu için ölçülmüş fayda."""
    text = (FRONTEND / "components" / "SchemaHealthPanel.tsx").read_text(encoding="utf-8")
    assert 'id="schema-missing-indexes"' in text
    assert "sys.dm_db_missing_index_*" in text, "kaynak yazılmamış"


def test_our_own_suggestion_says_where_it_came_from():
    # Faz 31: öneri gösterimi IndexAdvicePanel bileşenine taşındı; sayfa onu kullanıyor.
    page = (FRONTEND / "pages" / "InstanceDetailPage.tsx").read_text(encoding="utf-8")
    panel = (FRONTEND / "components" / "IndexAdvicePanel.tsx").read_text(encoding="utf-8")
    assert "<IndexAdviceResult" in page
    assert "Kaynak: dbace" in panel, "kendi önerimizin kaynağı yazılmamış"


@pytest.mark.parametrize(
    "token",
    [
        "cpu_time_share_pct",  # CPU payı
        "wait_time_share_pct",  # bekleme payı — elapsed ile CPU'nun FARKI
        "logical_reads",
        "physical_reads",
        "spills",
        "memory_grant_waste_pct",
    ],
)
def test_sqlserver_metrics_are_actually_rendered(token: str):
    """API'den gelip hiçbir ekranda gösterilmeyen alan, toplanmamış alanla aynı şeydir."""
    text = (FRONTEND / "pages" / "InstanceDetailPage.tsx").read_text(encoding="utf-8")
    assert token in text, f"{token} arayüzde gösterilmiyor"
