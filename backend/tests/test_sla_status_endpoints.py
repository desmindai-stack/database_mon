"""`GET /api/sla/status` ve `/api/sla/status/{target_id}` (Faz 31 Commit 10d madde D — kapsam taramasının P1 boşluğu).

Meta veritabanı üstünde çalışıyor, gerçek izlenen sunucu gerektirmiyor — bu yüzden offline (SQLite). `POST
/api/sla/targets` zaten test ediliyordu (`test_sla.py`); durum uçlarının KENDİSİ (evaluate_all/evaluate_target'ı
gerçekten çağırıp JSON'a çeviren yol) hiç çağrılmamıştı.
"""

from __future__ import annotations

from tests.auth_helper import authed_client


async def test_sla_status_lists_evaluated_targets_and_single_status_matches_the_list():
    async with await authed_client() as client:
        created = await client.post("/api/sla/targets", json={"scope_type": "customer", "scope_id": 123456,
                                                               "target_pct": 99.5, "period": "monthly"})
        assert created.status_code == 201, created.text
        target_id = created.json()["id"]

        listing = await client.get("/api/sla/status")
        assert listing.status_code == 200, listing.text
        # `SlaStatus.to_dict()` bir `id` alanı taşımıyor — (scope_type, scope_id) çifti eşleştirme anahtarı
        # (`create_target` de tekilliği bu çiftle zorluyor, aşağıda ayrıca doğrulanıyor).
        match = next((item for item in listing.json()
                     if item["scope_type"] == "customer" and item["scope_id"] == 123456), None)
        assert match is not None, "yeni hedef durum listesinde görünmeli"

        single = await client.get(f"/api/sla/status/{target_id}")
        assert single.status_code == 200, single.text
        # Aynı hesaplama yolu: liste ve tekil uç FARKLI sonuç vermemeli (tek gerçeklik kaynağı).
        assert single.json() == match

        duplicate = await client.post("/api/sla/targets", json={"scope_type": "customer", "scope_id": 123456,
                                                                 "target_pct": 95.0, "period": "monthly"})
        assert duplicate.status_code == 409, "aynı kapsama ikinci hedef reddedilmeli (negatif kontrol)"


async def test_negative_control_sla_status_for_a_missing_target_is_404():
    async with await authed_client() as client:
        missing = await client.get("/api/sla/status/999999999")
        assert missing.status_code == 404, missing.text
