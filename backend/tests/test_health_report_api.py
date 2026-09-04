"""Faz 17 İŞ 1 — sağlık raporu uçları.

Kanıtlananlar: elle tetikleme arka planda çalışıp "queued" döndürüyor, rapor listesi/detayı
bulguları önceliğe göre sıralı veriyor, kabul (acknowledgement) idempotent ve geri alınabilir,
viewer rolü raporu görebiliyor ama tetikleyemiyor/kabul edemiyor, zamanlama ayarı kaydediliyor.
"""

from __future__ import annotations

import asyncio
import uuid

from app.database import SessionLocal
from app.models import HealthReport, ReportFinding
from tests.auth_helper import authed_client


async def _seed_report(scope_id: int, findings: list[dict]) -> int:
    """Motoru çalıştırmadan doğrudan bir rapor yazar — uç testleri üretim hattına değil,
    HTTP davranışına odaklansın."""
    from datetime import UTC, datetime, timedelta

    async with SessionLocal() as session:
        now = datetime.now(UTC)
        report = HealthReport(
            scope_type="group",
            scope_id=scope_id,
            scope_label="api-test",
            period_start=now - timedelta(days=1),
            period_end=now,
            generated_by="manual",
            overall_status="critical",
            status="done",
            progress_pct=100,
            sections={"order": ["availability"], "items": {}},
        )
        session.add(report)
        await session.commit()
        for f in findings:
            session.add(
                ReportFinding(
                    report_id=report.id,
                    section=f.get("section", "availability"),
                    severity=f["severity"],
                    title=f["title"],
                    detail="detay",
                    evidence={"metric": "m", "value": 1},
                    fingerprint=f["fingerprint"],
                    priority=f.get("priority", 1.0),
                    acknowledged=f.get("acknowledged", False),
                    change_state=f.get("change_state", "new"),
                )
            )
        await session.commit()
        return report.id


async def test_report_detail_orders_findings_by_priority():
    report_id = await _seed_report(
        uuid.uuid4().int % 1_000_000_000,
        [
            {"severity": "warning", "title": "Az önemli", "fingerprint": "fp-low", "priority": 10.0},
            {"severity": "critical", "title": "Çok önemli", "fingerprint": "fp-high", "priority": 200.0},
        ],
    )
    async with await authed_client() as c:
        body = (await c.get(f"/api/reports/{report_id}")).json()

    assert [f["title"] for f in body["findings"]] == ["Çok önemli", "Az önemli"]
    assert body["critical_count"] == 1
    assert body["warning_count"] == 1


async def test_acknowledged_findings_do_not_count_as_critical():
    report_id = await _seed_report(
        uuid.uuid4().int % 1_000_000_000,
        [
            {"severity": "critical", "title": "Kabul edilmiş", "fingerprint": "fp-ack", "acknowledged": True},
            {"severity": "critical", "title": "Açık", "fingerprint": "fp-open"},
        ],
    )
    async with await authed_client() as c:
        body = (await c.get(f"/api/reports/{report_id}")).json()

    assert body["critical_count"] == 1, "kabul edilen bulgu kritik sayısını şişirmemeli"
    assert len(body["findings"]) == 2, "ama rapordan silinmemeli"


async def test_resolved_findings_are_excluded_from_counts():
    report_id = await _seed_report(
        uuid.uuid4().int % 1_000_000_000,
        [{"severity": "ok", "title": "Kapandı", "fingerprint": "fp-done", "change_state": "resolved"}],
    )
    async with await authed_client() as c:
        body = (await c.get(f"/api/reports/{report_id}")).json()

    assert body["critical_count"] == 0 and body["warning_count"] == 0


async def test_manual_run_returns_queued_and_completes_in_background():
    async with await authed_client() as c:
        r = await c.post("/api/reports/run", json={"scope_type": "global", "period_days": 1})
        assert r.status_code == 202, r.text
        queued = r.json()
        assert queued["status"] in ("queued", "running", "done")
        assert queued["generated_by"] == "manual"

        # Arka plan task'ının bitmesini bekle. Test veritabanı diğer testlerin instance'larını
        # da biriktirdiği için "global" kapsam gerçekçi biçimde yavaş olabilir.
        for _ in range(300):
            await asyncio.sleep(0.05)
            body = (await c.get(f"/api/reports/{queued['id']}")).json()
            if body["status"] in ("done", "failed"):
                break

    assert body["status"] == "done", body.get("error")
    assert body["progress_pct"] == 100
    assert "availability" in body["sections"]["order"]


async def test_run_requires_scope_id_for_non_global_scopes():
    async with await authed_client() as c:
        r = await c.post("/api/reports/run", json={"scope_type": "customer"})
        assert r.status_code == 400
        assert "scope_id" in r.json()["detail"]


async def test_acknowledgement_is_idempotent_and_reversible():
    fingerprint = f"fp-{uuid.uuid4().hex[:12]}"
    report_id = await _seed_report(
        uuid.uuid4().int % 1_000_000_000,
        [{"severity": "critical", "title": "Susturulacak", "fingerprint": fingerprint}],
    )
    async with await authed_client() as c:
        first = await c.post(
            "/api/reports/acknowledgements",
            json={"fingerprint": fingerprint, "expires_in_days": 30, "note": "bilinen"},
        )
        assert first.status_code == 201, first.text
        ack_id = first.json()["id"]
        assert first.json()["expires_at"] is not None

        # Aynı fingerprint tekrar kabul edilince yeni kayıt açılmaz, mevcut güncellenir.
        second = await c.post(
            "/api/reports/acknowledgements",
            json={"fingerprint": fingerprint, "expires_in_days": 60, "note": "süre uzatıldı"},
        )
        assert second.status_code == 201
        assert second.json()["id"] == ack_id
        assert second.json()["note"] == "süre uzatıldı"

        # Rapor hemen güncellenmiş olmalı — kullanıcı yenilemeden sonucu görsün.
        body = (await c.get(f"/api/reports/{report_id}")).json()
        assert body["findings"][0]["acknowledged"] is True
        assert body["critical_count"] == 0

        assert (await c.delete(f"/api/reports/acknowledgements/{ack_id}")).status_code == 204
        body = (await c.get(f"/api/reports/{report_id}")).json()
        assert body["findings"][0]["acknowledged"] is False
        assert body["critical_count"] == 1


async def test_permanent_acknowledgement_when_no_expiry_given():
    fingerprint = f"fp-{uuid.uuid4().hex[:12]}"
    async with await authed_client() as c:
        r = await c.post(
            "/api/reports/acknowledgements",
            json={"fingerprint": fingerprint, "expires_in_days": None},
        )
        assert r.json()["expires_at"] is None


async def test_schedule_can_be_read_and_updated():
    async with await authed_client() as c:
        current = (await c.get("/api/reports/schedule")).json()
        assert 0 <= current["hour"] <= 23

        updated = await c.put("/api/reports/schedule", json={"hour": 4, "scope_mode": "customers"})
        assert updated.status_code == 200, updated.text
        assert updated.json()["hour"] == 4
        assert updated.json()["scope_mode"] == "customers"

        # Geri al (diğer testleri etkilemesin).
        await c.put("/api/reports/schedule", json={"hour": 6, "scope_mode": "both"})


async def test_invalid_schedule_hour_is_rejected():
    async with await authed_client() as c:
        r = await c.put("/api/reports/schedule", json={"hour": 99})
        assert r.status_code == 422


async def test_viewer_can_read_but_not_trigger_or_acknowledge():
    report_id = await _seed_report(
        uuid.uuid4().int % 1_000_000_000,
        [{"severity": "warning", "title": "Görüntülenebilir", "fingerprint": f"fp-{uuid.uuid4().hex[:8]}"}],
    )
    async with await authed_client(role="viewer") as c:
        assert (await c.get("/api/reports")).status_code == 200
        assert (await c.get(f"/api/reports/{report_id}")).status_code == 200

        assert (await c.post("/api/reports/run", json={"scope_type": "global"})).status_code == 403
        assert (
            await c.post("/api/reports/acknowledgements", json={"fingerprint": "x"})
        ).status_code == 403


async def test_latest_report_endpoint_powers_the_dashboard_card():
    async with await authed_client() as c:
        before = (await c.get("/api/reports/latest?scope_type=global")).json()
        r = await c.post("/api/reports/run", json={"scope_type": "global", "period_days": 1})
        report_id = r.json()["id"]
        for _ in range(300):
            await asyncio.sleep(0.05)
            if (await c.get(f"/api/reports/{report_id}")).json()["status"] == "done":
                break
        latest = (await c.get("/api/reports/latest?scope_type=global")).json()

    assert latest is not None
    assert latest["scope_type"] == "global"
    if before is not None:
        assert latest["generated_at"] >= before["generated_at"]


async def test_report_can_be_deleted_even_when_referenced_as_previous():
    """Aynı tabloya kendi kendine referans (previous_report_id) silmeyi engellememeli."""
    scope_id = uuid.uuid4().int % 1_000_000_000
    first_id = await _seed_report(scope_id, [])
    second_id = await _seed_report(scope_id, [])
    async with SessionLocal() as session:
        second = await session.get(HealthReport, second_id)
        second.previous_report_id = first_id
        await session.commit()

    async with await authed_client() as c:
        assert (await c.delete(f"/api/reports/{first_id}")).status_code == 204
        assert (await c.get(f"/api/reports/{first_id}")).status_code == 404
        assert (await c.get(f"/api/reports/{second_id}")).json()["previous_report_id"] is None


async def test_report_list_filters_by_scope():
    scope_id = uuid.uuid4().int % 1_000_000_000
    await _seed_report(scope_id, [])
    async with await authed_client() as c:
        rows = (await c.get(f"/api/reports?scope_type=group&scope_id={scope_id}")).json()

    assert len(rows) == 1
    assert rows[0]["scope_id"] == scope_id


async def test_executive_endpoint_returns_a_customer_safe_view():
    """Faz 17 İŞ 3: aynı rapor, yönetici görünümü — teknik detay içermez."""
    report_id = await _seed_report(
        uuid.uuid4().int % 1_000_000_000,
        [
            {
                "severity": "critical",
                "title": "SELECT * FROM orders yavaşladı",
                "fingerprint": f"fp-{uuid.uuid4().hex[:8]}",
                "section": "performance",
            }
        ],
    )
    async with await authed_client() as c:
        body = (await c.get(f"/api/reports/{report_id}/executive")).json()

    assert body["grade"] in ("Sağlıklı", "Dikkat", "Riskli")
    assert "SELECT" not in str(body)
    assert body["risks"][0]["area"] == "Performans"


async def test_executive_endpoint_refuses_an_unfinished_report():
    from datetime import UTC, datetime, timedelta

    async with SessionLocal() as session:
        now = datetime.now(UTC)
        report = HealthReport(
            scope_type="global", scope_id=None, scope_label="Tüm sistem",
            period_start=now - timedelta(days=1), period_end=now,
            status="running", progress_pct=40,
        )
        session.add(report)
        await session.commit()
        report_id = report.id

    async with await authed_client() as c:
        r = await c.get(f"/api/reports/{report_id}/executive")

    assert r.status_code == 409


async def test_viewer_can_read_the_executive_report():
    report_id = await _seed_report(uuid.uuid4().int % 1_000_000_000, [])
    async with await authed_client(role="viewer") as c:
        assert (await c.get(f"/api/reports/{report_id}/executive")).status_code == 200
