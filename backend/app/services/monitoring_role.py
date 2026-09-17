"""İzleme rolü uygulamayla paylaşılıyor mu (Faz 31 Commit 5).

pg_stat_statements satırı `userid` ile anahtarlı. dbace ayrı bir rolle bağlanıyorsa kendi
satırları uygulamanınkinden ayrı durur ve imza (`/* dbace */`) + `from_monitoring_role` ayrımı
güvenilirdir (Faz 31 Commit 4, 15/16/17'de ölçüldü). AYNI rolde ise dbace ile uygulama aynı
queryid için TEK satırı paylaşır ve satırın metni ilk görülen metindir: uygulama çağrıları
dbace'in kendi sorgusu sayılabilir, dbace'in çağrıları uygulama yükü sayılabilir. Bu durumda
köken ayrımı "yok" değil **ölçülemez**.

Kanıt: toplayıcı her döngüde `pg_stat_activity`'de kendi rolünde `application_name` 'dbace'
OLMAYAN istemci oturumu arıyor. dbace'in bütün bağlantıları `application_name=dbace` ile
açılıyor (collectors/query_marker.py). İmzasız metne bakılmıyor: asyncpg'nin iç tip sorguları
dbace bağlantısında imzasız görünür ve yanlış alarm üretirdi. Saklanan tek şey application_name
listesi — sorgu metni saklanmıyor.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.models import Instance

STATUS_SEPARATE = "separate"
STATUS_SHARED = "shared"
STATUS_UNMEASURED = "unmeasured"

#: Paylaşım kanıtı bu süre boyunca geçerli: gece çalışan bir toplu iş gün içinde bağlı değildir.
SHARED_EVIDENCE_WINDOW = timedelta(hours=24)
#: Son kontrol bundan eskiyse durum bilinmiyor sayılır (toplayıcı çalışmıyor).
CHECK_STALE_AFTER = timedelta(hours=1)
MAX_APPS = 5

SHARED_MESSAGE = (
    "Ölçülemedi: izleme rolü uygulamayla paylaşılıyor. dbace'in bağlandığı rolde dbace dışı "
    "oturum görüldü; pg_stat_statements aynı roldeki çağrıları tek satırda topladığı için "
    "\"dbace'in kendi sorgusu\" ile uygulama yükü bu veritabanında ayrılamıyor."
)


def monitoring_role_setup_command(current_role: str | None) -> str:
    return (
        "-- dbace için AYRI bir izleme rolü (uygulama bu rolü kullanmamalı):\n"
        "CREATE ROLE dbace_monitor LOGIN PASSWORD '<güçlü_parola>';\n"
        "GRANT pg_monitor TO dbace_monitor;\n"
        "-- Index önerisi ölçümleri için, gerekirse tablo bazında:\n"
        "-- GRANT SELECT ON <şema>.<tablo> TO dbace_monitor;\n"
        f"-- Ardından dbace'te bu veritabanının kullanıcısını {current_role or '<mevcut rol>'} yerine "
        "dbace_monitor yapın."
    )


def record_observation(instance: Instance, apps: list[str] | None, *, now: datetime) -> None:
    """Toplayıcının gözlemini yazar. `apps is None` = sorgu başarısız, ölçüm yok."""
    if apps is None:
        return
    instance.monitoring_role_checked_at = now
    if apps:
        instance.monitoring_role_shared_at = now
        instance.monitoring_role_shared_apps = sorted(apps)[:MAX_APPS]


def monitoring_role_status(instance: Instance, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    checked = _aware(instance.monitoring_role_checked_at)
    shared = _aware(instance.monitoring_role_shared_at)
    base = {
        "checked_at": checked,
        "shared_seen_at": shared,
        "applications": [],
        "setup_command": None,
    }
    if instance.engine != "postgresql":
        return {**base, "status": STATUS_UNMEASURED,
                "message": "Ölçülmedi: izleme rolü paylaşımı yalnızca PostgreSQL'de ölçülüyor."}
    if shared is not None and now - shared <= SHARED_EVIDENCE_WINDOW:
        return {
            **base,
            "status": STATUS_SHARED,
            "message": SHARED_MESSAGE,
            "applications": list(instance.monitoring_role_shared_apps or []),
            "setup_command": monitoring_role_setup_command(instance.username),
        }
    if checked is None or now - checked > CHECK_STALE_AFTER:
        return {
            **base,
            "status": STATUS_UNMEASURED,
            "message": (
                "Ölçülmedi: toplayıcı son bir saatte bu veritabanında izleme rolünü denetlemedi "
                "(toplama çalışmıyor ya da pg_stat_activity okunamadı). Rolün ayrı olduğu "
                "varsayılmıyor."
            ),
        }
    return {
        **base,
        "status": STATUS_SEPARATE,
        "message": (
            "İzleme rolü ayrı: son 24 saatte dbace'in rolünde dbace dışı oturum görülmedi; "
            "dbace'in kendi sorguları uygulama yükünden ayrılabiliyor."
        ),
    }


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)
