"""Bekleme örnekleyicisinin bağlantı durumu: 'örnek yok' ile 'örnekleyici bağlanamıyor' ayrımı (Faz 31 Commit 10c-B).

Kabul kriteri: RTT ≥ ~1 sn'de (ya da yetki hatası/zaman aşımı) ekran "ölçülemedi + gerekçe" desin; "örnek yok"
yalnızca bağlantı sağlıklı ve gerçekten örnek yoksa görünsün. Uçtan uca kanıt: `test_database_load.py`,
`test_blocking_history_api.py`, gerçek arızayı üreten `test_sampling_health_live_mssql.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.services.sampling_health import sampling_status_for, unavailable_message

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)


@dataclass
class FakeInstance:
    last_sample_error: str | None = None
    last_sample_error_at: datetime | None = None
    last_sample_ok_at: datetime | None = None


def test_never_attempted_is_not_broken():
    """Yeni instance, sampler henüz hiç denemedi: 'bağlanamıyor' iddiası YOK — 'henüz örneklenmedi' ile
    'bağlanamıyor' farklı şeyler."""
    health = sampling_status_for(FakeInstance())
    assert health.broken is False and health.reason is None


def test_a_recorded_failure_with_no_success_is_broken():
    health = sampling_status_for(FakeInstance(last_sample_error="Bağlantı zaman aşımına uğradı.", last_sample_error_at=NOW))
    assert health.broken is True and health.reason == "Bağlantı zaman aşımına uğradı."


def test_a_success_after_the_failure_clears_broken():
    """NEGATİF KONTROL: hata kaydı satırda dursa bile ondan SONRA bir başarı varsa artık bozuk sayılmaz."""
    health = sampling_status_for(FakeInstance(
        last_sample_error="Kimlik doğrulama başarısız.", last_sample_error_at=NOW - timedelta(hours=1),
        last_sample_ok_at=NOW - timedelta(minutes=1)))
    assert health.broken is False


def test_a_failure_after_the_last_success_is_broken():
    """NEGATİF KONTROL (ters yön): eski bir başarı varlığı, ondan SONRAKİ arızayı gizlememeli."""
    health = sampling_status_for(FakeInstance(
        last_sample_error="Bağlantı reddedildi.", last_sample_error_at=NOW - timedelta(minutes=1),
        last_sample_ok_at=NOW - timedelta(hours=1)))
    assert health.broken is True and health.reason == "Bağlantı reddedildi."


def test_equal_timestamps_favor_healthy():
    """Sınır durum: hata ve başarı TAM aynı anda kaydedilmişse (saniye çözünürlüğü çakışması) sağlıklı sayılır —
    'bozuk' iddiası yalnızca hatanın KESİNLİKLE daha yeni olduğu durumda yapılmalı."""
    health = sampling_status_for(FakeInstance(last_sample_error="x", last_sample_error_at=NOW, last_sample_ok_at=NOW))
    assert health.broken is False


def test_missing_error_text_with_a_timestamp_is_not_broken():
    """Tutarsız veri (elle düzenlenmiş satır, ör. yalnızca last_sample_error_at dolu): reason olmadan 'bozuk'
    iddia edilmez — cümle 'None' göstermemeli."""
    health = sampling_status_for(FakeInstance(last_sample_error=None, last_sample_error_at=NOW))
    assert health.broken is False


def test_unavailable_message_names_the_context_and_the_reason_and_never_says_no_events():
    health = sampling_status_for(FakeInstance(last_sample_error="Bağlantı zaman aşımına uğradı.", last_sample_error_at=NOW))
    message = unavailable_message(health, context="bekleme örneği")
    assert message.startswith("Ölçülemedi:")
    assert "Bağlantı zaman aşımına uğradı." in message
    assert "bekleme örneği yok" not in message.lower() or "YANLIŞ" in message  # açıkça reddediyor, iddia etmiyor
    assert "2026-09-22 10:00" in message  # zaman damgası kullanıcıya görünür


def test_unavailable_message_handles_a_missing_timestamp_without_crashing():
    """NEGATİF KONTROL: error_at None olsa bile (kuramsal olarak sampling_status_for bunu 'broken' saymaz ama
    doğrudan çağrılırsa) fonksiyon çökmemeli."""
    from app.services.sampling_health import SamplingHealth

    message = unavailable_message(SamplingHealth(broken=True, reason="x", error_at=None), context="olay")
    assert "bilinmiyor" in message
