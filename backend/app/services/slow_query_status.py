"""Yavaş sorgu kullanılabilirliği — "veri neden yok?" sorusunun TEK kaynağı (Faz 16-B İŞ 1).

Önceden DPA sayfası veri boş geldiğinde sabit bir metin gösteriyordu: "Yavaş sorgu verisi yok.
Eklentiyi aktif edin: CREATE EXTENSION pg_stat_statements". Bu metin ön koşullar panelinden
bağımsızdı; panel eklentiyi "var" gösterirken DPA "kurun" diyordu.

Artık her iki taraf da services/pgss.py'deki aynı probe'u kullanıyor ve bu modül probe'un
sonucunu + dbace'in kendi kayıtlarını (kaç örnek saklandı, en son ne zaman) birleştirip TEK bir
durum döndürüyor. Frontend'deki bütün "yavaş sorgu yok" mesajları bunu gösteriyor.

Ayırt edilen durumlar ve verilen cevap:
  ok                  → veri var
  extension_missing   → "kurun" (tek meşru CREATE EXTENSION mesajı burası)
  extension_unreachable → eklenti search_path dışı bir şemada
  unauthorized        → okuma yetkisi yok
  not_preloaded       → shared_preload_libraries'de yok, hiç istatistik toplanmıyor
  track_off           → pg_stat_statements.track = none
  restricted_visibility → sadece kendi sorgularınız görünüyor (Supabase/RDS klasiği)
  no_data_yet         → her şey hazır, sunucuda henüz istatistik birikmemiş
  not_collected_yet   → sunucuda veri var ama worker henüz saklamamış
  probe_failed        → sunucuya bağlanılamadı
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import ConnectionTarget, classify_connection_error
from app.models import Instance, SlowQuerySample
from app.services.credentials import decrypt_secret
from app.services.pgss import PgStatStatementsProbe, probe_pg_stat_statements


# Hangi durumun hangi ön koşul kontrolünden kaynaklandığı (Faz 16-B İŞ 6). Kullanıcı o kontrolü
# "ortamımda geçerli değil" diye yoksaydıysa, etkilediği özelliğin neden çalışmadığını burada
# açıkça söylüyoruz — sessizce boş bir liste göstermek yerine.
_STATUS_TO_PREREQUISITE = {
    "extension_missing": "pg_stat_statements",
    "extension_unreachable": "pg_stat_statements",
    "unauthorized": "pg_stat_statements",
    "not_preloaded": "shared_preload_libraries",
    "track_off": "pg_stat_statements_track",
    "restricted_visibility": "pg_stat_statements_visibility",
}


@dataclass
class SlowQueryAvailability:
    status: str
    title: str
    message: str
    fix: str | None = None
    stored_samples: int = 0
    last_collected_at: datetime | None = None
    server_rows: int | None = None
    redacted_rows: int | None = None
    # Bu durumun kaynağı olan ön koşul kontrolü kullanıcı tarafından yoksayıldıysa anahtarı.
    ignored_prerequisite: str | None = None

    @property
    def has_data(self) -> bool:
        return self.status == "ok"


def _pg_status(probe: PgStatStatementsProbe, stored: int) -> tuple[str, str, str, str | None]:
    """(status, title, message, fix) — sıralama önemli: en temel eksiklik önce raporlanır."""
    if not probe.installed:
        return (
            "extension_missing",
            "pg_stat_statements kurulu değil",
            "Yavaş sorgu listesi, sorgu geçmişi ve index önerisi bu uzantıya dayanır; uzantı olmadan "
            "hiçbiri veri üretemez.",
            "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;\n"
            "-- Ardından shared_preload_libraries'e ekleyip PostgreSQL'i yeniden başlatın.",
        )
    if not probe.reachable:
        if probe.read_error_code == "unauthorized":
            return (
                "unauthorized",
                "pg_stat_statements okunamıyor (yetki)",
                "Uzantı kurulu ama bağlanan kullanıcının okuma yetkisi yok.",
                "GRANT pg_read_all_stats TO <kullanıcı>;",
            )
        schema = probe.schema or "extensions"
        return (
            "extension_unreachable",
            "pg_stat_statements erişilemiyor",
            f"Uzantı '{schema}' şemasında kurulu ama bağlanan rolün search_path'inde bu şema yok. "
            f"dbace toplama sırasında şemayı niteleyerek sorguluyor; bu mesajı görüyorsanız view "
            f"başka bir sebeple okunamıyor: {probe.read_error}",
            f'ALTER ROLE <kullanıcı> SET search_path = public, "{schema}";',
        )
    if not probe.preloaded:
        return (
            "not_preloaded",
            "pg_stat_statements önyüklü değil",
            "Uzantı kurulu ve okunabiliyor, ama shared_preload_libraries'de olmadığı için sunucu "
            "hiç istatistik toplamıyor — tablo her zaman boş kalır.",
            "ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements';\n"
            "-- Ardından PostgreSQL'i YENİDEN BAŞLATIN (reload yetmez).",
        )
    if probe.track not in ("top", "all"):
        return (
            "track_off",
            f"pg_stat_statements.track = {probe.track or 'okunamadı'}",
            "İzleme kapalı olduğu için hiçbir sorgu istatistiği kaydedilmiyor.",
            "ALTER SYSTEM SET pg_stat_statements.track = 'top';\nSELECT pg_reload_conf();",
        )
    if probe.restricted_visibility and not stored:
        return (
            "restricted_visibility",
            "Sadece kendi sorgularınızı görebiliyorsunuz",
            f"Uzantı çalışıyor ve sunucuda {probe.total_rows} satır var, ama bağlanan rol "
            "superuser/pg_read_all_stats üyesi olmadığı için başka kullanıcıların sorgu metni "
            f"maskeli geliyor ({probe.redacted_rows} satır '<insufficient privilege>'). "
            "Uygulamanız farklı bir rolle bağlanıyorsa asıl yavaş sorgular listede hiç görünmez.",
            "GRANT pg_read_all_stats TO <kullanıcı>;\n-- (pg_monitor bu rolü de kapsar)",
        )
    if not probe.total_rows:
        return (
            "no_data_yet",
            "Veri henüz birikmedi",
            "pg_stat_statements kurulu, önyüklü ve okunabilir durumda — sunucuda henüz kayda değer "
            "sorgu istatistiği yok. Uygulama trafiği geldikçe liste dolacak.",
            None,
        )
    if not stored:
        return (
            "not_collected_yet",
            "Toplama henüz yapılmadı",
            f"Sunucuda {probe.total_rows} sorgu istatistiği var ama dbace worker'ı bunları henüz "
            "kaydetmedi. Bir toplama döngüsü (varsayılan 15 sn) bekleyin; instance duraklatılmış "
            "olabilir.",
            None,
        )
    if probe.restricted_visibility:
        # Veri var ama eksik olabilir — "ok" diyoruz, yine de kapsam uyarısını taşıyoruz.
        return (
            "ok",
            "Yavaş sorgu verisi mevcut (kısıtlı kapsam)",
            f"{stored} örnek saklandı. Dikkat: bağlanan rol pg_read_all_stats üyesi olmadığı için "
            f"başka kullanıcıların {probe.redacted_rows} sorgusu maskeli geliyor ve listeye alınmıyor.",
            "GRANT pg_read_all_stats TO <kullanıcı>;",
        )
    return ("ok", "Yavaş sorgu verisi mevcut", f"{stored} örnek saklandı.", None)


async def get_slow_query_availability(session: AsyncSession, instance: Instance) -> SlowQueryAvailability:
    stored = int(
        (
            await session.execute(
                select(func.count(SlowQuerySample.id)).where(SlowQuerySample.instance_id == instance.id)
            )
        ).scalar_one()
        or 0
    )
    last_at = (
        await session.execute(
            select(func.max(SlowQuerySample.collected_at)).where(SlowQuerySample.instance_id == instance.id)
        )
    ).scalar_one_or_none()

    if instance.engine == "sqlserver":
        # SQL Server tarafında eşdeğer bir "tek probe" yok: yavaş sorgular DMV'lerden geliyor ve
        # ön koşulları (VIEW SERVER STATE, Query Store) ön koşul paneli zaten canlı denetliyor.
        # Burada veriyi uydurmak yerine kullanıcıyı o panele yönlendiriyoruz.
        if stored:
            return SlowQueryAvailability(
                "ok", "Yavaş sorgu verisi mevcut", f"{stored} örnek saklandı.",
                stored_samples=stored, last_collected_at=last_at,
            )
        return SlowQueryAvailability(
            "no_data_yet",
            "Veri henüz birikmedi",
            "Henüz yavaş sorgu örneği kaydedilmedi. DMV erişimi ve Query Store durumu için "
            "Ön koşullar paneline bakın.",
            stored_samples=stored,
            last_collected_at=last_at,
        )

    if instance.engine != "postgresql":
        return SlowQueryAvailability(
            "engine_unsupported",
            "Bu engine için yavaş sorgu analizi yok",
            f"Yavaş sorgu toplama şu an sadece PostgreSQL ve SQL Server için mevcut (engine: {instance.engine}).",
            stored_samples=stored,
            last_collected_at=last_at,
        )

    import asyncpg

    target = ConnectionTarget(
        host=instance.host,
        port=instance.port,
        database=instance.database,
        username=instance.username,
        password=decrypt_secret(instance.password),
        options=instance.options,
    )
    ssl_mode = (target.options or {}).get("ssl_mode")
    try:
        conn = await asyncpg.connect(
            host=target.host,
            port=target.port,
            database=target.database,
            user=target.username,
            password=target.password,
            timeout=10,
            ssl=True if ssl_mode == "require" else None,
            statement_cache_size=0,
        )
    except Exception as exc:  # noqa: BLE001 — bağlanamamak da geçerli bir "neden veri yok" cevabı
        return SlowQueryAvailability(
            "probe_failed",
            "Sunucuya bağlanılamadı",
            classify_connection_error(exc),
            stored_samples=stored,
            last_collected_at=last_at,
        )

    try:
        probe = await probe_pg_stat_statements(conn)
    finally:
        await conn.close()

    status, title, message, fix = _pg_status(probe, stored)

    ignored = set(instance.ignored_prerequisites or [])
    ignored_key = _STATUS_TO_PREREQUISITE.get(status)
    ignored_key = ignored_key if ignored_key in ignored else None
    if ignored_key:
        title = f"{title} (yoksayılan ön koşul)"
        message = (
            f"Bu özellik çalışmıyor çünkü '{ignored_key}' ön koşulu eksik ve siz bu kontrolü "
            f"yoksaydınız. Yoksaymayı Ön koşullar panelinden geri alabilirsiniz. Özgün sebep: {message}"
        )

    return SlowQueryAvailability(
        status=status,
        title=title,
        message=message,
        fix=fix,
        stored_samples=stored,
        last_collected_at=last_at,
        server_rows=probe.total_rows,
        redacted_rows=probe.redacted_rows,
        ignored_prerequisite=ignored_key,
    )
