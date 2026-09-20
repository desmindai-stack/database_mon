"""Aktif oturum örnekleyicisi (Faz 25 İŞ 1).

NEDEN AYRI BİR DÖNGÜ:
Mevcut toplama döngüsü 15 saniyede bir çalışıyor ve KÜMÜLATİF sayaçlar okuyor
(pg_stat_statements, pg_stat_database). Bekleme analizi bambaşka bir şey ister: anlık durumun
SIK tekrarlanan fotoğrafı. 15 saniyede bir bakmak, 200 ms süren bir kilit fırtınasını hiç
görmemek demek. Bu yüzden örnekleme kendi işinde, kendi aralığında (varsayılan 1 sn) ve kendi
kalıcı bağlantısında çalışıyor — toplama döngüsüne eklenmedi.

TASARIMIN İKİ SINIRI:
1. İzlenen sunucuya yük bindirmemek. Kalıcı tek bağlantı, tek round trip, 1 saniyelik
   statement_timeout, sunucu tarafında filtre. Ölçüm maliyeti README'de "İzleme yükü"
   bölümünde ölçümüyle birlikte yazılı.
2. Kendi veritabanımızı şişirmemek. Ham örnek SAKLANMIYOR; örnekler süreç belleğinde dakikalık
   kovalarda toplanıp dakika kapandığında tek seferde yazılıyor.

VERİ KAYBI SÖZLEŞMESİ (bilerek):
Süreç, bir dakikanın ortasında yeniden başlarsa o dakikanın biriken kovası kaybolur (en fazla
1 dakikalık kırılım). Bunu diske yazmak, saniyede yazma demekti — kaçınmak istediğimiz şeyin
ta kendisi. Kaybın ölçüsü `samples_taken` üzerinden GÖRÜNÜR kalıyor: eksik örnekli dakika AAS'i
yanlış değil, sadece daha az örnekle hesaplanmış olur.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import BaseCollector, SamplingConnection
from app.collectors.registry import get_collector
from app.config import settings
from app.database import SessionLocal
from app.domain.engines import DatabaseEngine
from app.models import ActiveSessionMinute, Instance, WaitQuerySignature, WaitSampleMinute
from app.services.blocking import build_blocking_tree
from app.services.blocking_history import close_all as close_open_episodes
from app.services.blocking_history import record_tree
from app.services.collection import connection_target_for

logger = logging.getLogger(__name__)

#: Örnekleme yalnızca bu motorlarda mümkün. MongoDB'de karşılığı olan bir bekleme sözlüğü yok
#: (currentOp bekleme TİPİ vermiyor); sessizce boş veri üretmek yerine hiç örneklenmiyor.
SAMPLED_ENGINES = frozenset({DatabaseEngine.POSTGRESQL, DatabaseEngine.SQLSERVER})

#: Instance listesi her turda (saniyede bir) veritabanından okunmuyor — kendi veritabanımıza
#: gereksiz yük. Yeni eklenen bir instance en geç bu kadar sonra örneklenmeye başlar.
INSTANCE_CACHE_TTL_SECONDS = 30

#: Arka arkaya bu kadar başarısız turdan sonra hata log'u seyreltilir. Erişilemeyen bir sunucu
#: saniyede bir log satırı üretirse log'lar kullanılamaz hale gelir.
FAILURE_LOG_EVERY = 60

#: Bloklama kontrolü örnekleyicinin KALICI bağlantısı üzerinde, ama çok daha seyrek.
#: Ayrı bir iş yapıp 10 saniyede bir yeni bağlantı açmak, ölçmeye çalıştığımız yükün
#: kendisini üretirdi. 10 saniye: bir bloklama olayının başlangıcını kaçırmayacak kadar
#: sık, `pg_locks` taramasını sürekli tekrarlamayacak kadar seyrek.
BLOCKING_CHECK_INTERVAL_SECONDS = 10.0

#: Örnekleyici ne kadarda bir ÖZET loglayacak. Tur başına log yazmak saniyede bir satır,
#: günde 86.400 satır demek — gerçek hatalar o yığının içinde kaybolur. Özet, aynı bilgiyi
#: (kaç örnek, kaç hata, ne kadar gecikme) 288 satırda veriyor.
SUMMARY_LOG_INTERVAL_SECONDS = 300.0

#: Bir tur bu kadar gecikirse ANLAMLI bir olaydır ve tek başına loglanır: örnekleme
#: aralığının katı kadar süren bir tur, ölçümde delik açıyor demektir.
SLOW_ROUND_FACTOR = 3.0


def minute_floor(moment: datetime) -> datetime:
    return moment.replace(second=0, microsecond=0)


@dataclass
class _MinuteBucket:
    """Tek bir instance'ın tek bir dakikası için bellekteki birikim."""

    minute: datetime
    samples_taken: int = 0
    active_sessions_sampled: int = 0
    blocked_sessions_sampled: int = 0
    # (queryid, wait_category, wait_event) -> kaç örnekte görüldü
    counts: dict[tuple[str, str, str], int] = field(default_factory=dict)
    # queryid -> sorgu metni (sözlüğe yazılacak; dakikada bir kez)
    query_texts: dict[str, str] = field(default_factory=dict)
    # Faz 31 İŞ 2: queryid -> (gerçek değerli metin, süre ms) — dakika içindeki EN YAVAŞ
    # çalıştırma. Bellekte her zaman tutuluyor; YAZILIP yazılmayacağına flush anında ayar
    # karar veriyor (ayar kapatıldıysa o dakikanın örnekleri de atılır).
    samples: dict[str, tuple[str, float]] = field(default_factory=dict)
    # Faz 31 İŞ 2: bind parametreli ($1) hâliyle görülen queryid'ler — değer içermeyen işaret.
    bind_parameter_queryids: set[str] = field(default_factory=set)


@dataclass
class _InstanceSampler:
    instance_id: int
    engine: DatabaseEngine
    collector: BaseCollector
    conn: SamplingConnection | None = None
    consecutive_failures: int = 0
    has_query_id: bool = True
    #: Son bloklama kontrolü — örnekleme her saniye, bu kontrol 10 saniyede bir.
    last_blocking_check: datetime | None = None


# Süreç ömrü boyunca yaşayan durum. Modül seviyesinde: `_previous_state` (collection.py) ile
# aynı kalıp — worker tek süreç ve tek olay döngüsü.
_samplers: dict[int, _InstanceSampler] = {}
_buckets: dict[int, _MinuteBucket] = {}
_known_signatures: set[tuple[int, str]] = set()
_instance_cache: list[Instance] = []
_instance_cache_at: datetime | None = None
#: Dakikası kapanmış, henüz yazılmamış kovalar.
_pending_flush: list[tuple[int, _MinuteBucket]] = []
#: Faz 31 İŞ 2: süreç başına bir kez, saklanmış gerçek değerli metnin temizlenip temizlenmediği.
_privacy_enforced = False


@dataclass
class _RoundStats:
    """Özet log için biriken sayaçlar (Faz 27 İŞ 2)."""

    rounds: int = 0
    failures: int = 0
    samples: int = 0
    total_duration: float = 0.0
    max_duration: float = 0.0
    slow_rounds: int = 0
    last_logged_at: datetime | None = None


_stats = _RoundStats()


def reset_state() -> None:
    """Testler ve worker yeniden başlangıcı için: bellekteki her şeyi bırakır."""
    _samplers.clear()
    _buckets.clear()
    _known_signatures.clear()
    _pending_flush.clear()
    global _stats, _privacy_enforced
    _stats = _RoundStats()
    _privacy_enforced = False
    _instance_cache.clear()
    global _instance_cache_at
    _instance_cache_at = None


async def _load_instances(session: AsyncSession, now: datetime) -> list[Instance]:
    global _instance_cache_at
    if _instance_cache_at is not None and (now - _instance_cache_at).total_seconds() < INSTANCE_CACHE_TTL_SECONDS:
        return list(_instance_cache)
    result = await session.execute(select(Instance).where(Instance.enabled.is_(True)))
    instances = [i for i in result.scalars().all() if i.engine in {str(e) for e in SAMPLED_ENGINES}]
    _instance_cache.clear()
    _instance_cache.extend(instances)
    _instance_cache_at = now
    return list(instances)


async def _ensure_sampler(instance: Instance) -> _InstanceSampler:
    sampler = _samplers.get(instance.id)
    if sampler is None:
        engine = DatabaseEngine(instance.engine)
        sampler = _InstanceSampler(
            instance_id=instance.id,
            engine=engine,
            collector=get_collector(engine, connection_target_for(instance)),
        )
        _samplers[instance.id] = sampler
    return sampler


async def _drop_connection(sampler: _InstanceSampler) -> None:
    if sampler.conn is not None:
        try:
            await sampler.conn.close()
        except Exception:  # pragma: no cover - kapatma hatası örneklemeyi durdurmamalı
            logger.debug("Örnekleme bağlantısı kapatılamadı (instance %s)", sampler.instance_id)
        sampler.conn = None


async def _sample_instance(instance: Instance, now: datetime) -> None:
    """Tek instance için tek fotoğraf. Hata durumunda bağlantı düşürülür ve bir sonraki turda
    yeniden kurulur — kalıcı bağlantı, kopmuş bağlantıyı sonsuza kadar taşımak demek değil."""
    sampler = await _ensure_sampler(instance)
    try:
        if sampler.conn is None:
            sampler.conn = await sampler.collector.open_sampling_connection()
            if sampler.conn is None:
                return  # motor desteklemiyor
        snapshot = await sampler.collector.sample_active_sessions(sampler.conn)
    except Exception as exc:
        sampler.consecutive_failures += 1
        await _drop_connection(sampler)
        if sampler.consecutive_failures == 1 or sampler.consecutive_failures % FAILURE_LOG_EVERY == 0:
            logger.warning(
                "Bekleme örneklemesi başarısız (instance %s, üst üste %s tur): %s",
                instance.name, sampler.consecutive_failures, exc,
            )
        return

    if snapshot is None:
        return
    sampler.consecutive_failures = 0
    sampler.has_query_id = bool(snapshot.get("has_query_id", True))

    minute = minute_floor(now)
    bucket = _buckets.get(instance.id)
    if bucket is None or bucket.minute != minute:
        # Dakika değişti: eski kovayı yazılmak üzere bırak, yenisini başlat. Yazma işi
        # flush_completed_buckets() içinde, tek oturumda ve toplu yapılıyor.
        if bucket is not None:
            _pending_flush.append((instance.id, bucket))
        bucket = _MinuteBucket(minute=minute)
        _buckets[instance.id] = bucket

    sessions = snapshot.get("sessions") or []
    bucket.samples_taken += 1
    bucket.active_sessions_sampled += len(sessions)
    bucket.blocked_sessions_sampled += int(snapshot.get("blocked") or 0)
    for row in sessions:
        queryid = (row.get("queryid") or "")[:64]
        key = (queryid, row.get("wait_category") or "other", (row.get("wait_event") or "")[:64])
        bucket.counts[key] = bucket.counts.get(key, 0) + 1
        text = row.get("query") or ""
        if queryid and text and queryid not in bucket.query_texts:
            bucket.query_texts[queryid] = text
        if queryid and text and _has_placeholders(text):
            bucket.bind_parameter_queryids.add(queryid)
        elapsed = row.get("elapsed_ms")
        if queryid and text and elapsed is not None and is_sample_candidate(text):
            current = bucket.samples.get(queryid)
            if current is None or elapsed > current[1]:
                bucket.samples[queryid] = (text, float(elapsed))


async def _write_bucket(session: AsyncSession, instance_id: int, bucket: _MinuteBucket) -> None:
    """Kovayı EKLEYEREK yazar (üzerine yazmaz).

    Aynı (instance, dakika) için satır zaten varsa sayaçlar toplanır. Bu, worker'ın dakika
    ortasında yeniden başladığı durumu doğru ele alır: iki yarım kova aynı dakikayı temsil eder
    ve toplamları o dakikanın gerçeğidir. Üzerine yazsaydık ilk yarı sessizce kaybolurdu.
    """
    totals = (
        await session.execute(
            select(ActiveSessionMinute).where(
                ActiveSessionMinute.instance_id == instance_id,
                ActiveSessionMinute.minute == bucket.minute,
            )
        )
    ).scalar_one_or_none()
    if totals is None:
        session.add(
            ActiveSessionMinute(
                instance_id=instance_id,
                minute=bucket.minute,
                samples_taken=bucket.samples_taken,
                active_sessions_sampled=bucket.active_sessions_sampled,
                blocked_sessions_sampled=bucket.blocked_sessions_sampled,
            )
        )
    else:
        totals.samples_taken += bucket.samples_taken
        totals.active_sessions_sampled += bucket.active_sessions_sampled
        totals.blocked_sessions_sampled += bucket.blocked_sessions_sampled

    existing_rows = (
        await session.execute(
            select(WaitSampleMinute).where(
                WaitSampleMinute.instance_id == instance_id,
                WaitSampleMinute.minute == bucket.minute,
            ).limit(MAX_WAIT_ROWS_PER_MINUTE)
        )
    ).scalars().all()
    by_key = {(r.queryid, r.wait_category, r.wait_event): r for r in existing_rows}
    for (queryid, category, event), count in bucket.counts.items():
        row = by_key.get((queryid, category, event))
        if row is None:
            session.add(
                WaitSampleMinute(
                    instance_id=instance_id,
                    minute=bucket.minute,
                    queryid=queryid,
                    wait_category=category,
                    wait_event=event,
                    sample_count=count,
                )
            )
        else:
            row.sample_count += count

    await _write_signatures(session, instance_id, bucket.query_texts)
    await session.flush()
    if bucket.bind_parameter_queryids:
        await _mark_bind_parameters(session, instance_id, bucket.bind_parameter_queryids)
    if bucket.samples and await _store_real_query_samples(session):
        await _write_samples(session, instance_id, bucket.samples, captured_at=bucket.minute)


def _has_placeholders(text: str) -> bool:
    from app.services.generic_plan import has_placeholders

    return has_placeholders(text)


async def _mark_bind_parameters(session: AsyncSession, instance_id: int, queryids: set[str]) -> None:
    rows = (
        await session.execute(
            select(WaitQuerySignature).where(
                WaitQuerySignature.instance_id == instance_id,
                WaitQuerySignature.queryid.in_(list(queryids)),
                WaitQuerySignature.seen_bind_parameters.is_(False),
            )
        )
    ).scalars().all()
    for row in rows:
        row.seen_bind_parameters = True


def is_sample_candidate(text: str) -> bool:
    """Metin EXPLAIN ANALYZE için temsili gerçek değerli örnek olabilir mi.

    Yer tutucu ($1) içeren metin DEĞER TAŞIMIYOR: bind parametreli (extended protocol)
    sürücüler pg_stat_activity'de `$1` gösteriyor — PG 17 ve 15'te ölçüldü. Kesik metin
    çalıştırılamaz; dbace'in kendi sorgusu ve sistem sorguları öneri konusu değil.
    """
    from app.services.query_text_privacy import is_utility_statement
    from app.services.slow_query_selection import classify_system_query
    from app.services.sql_analysis import detect_truncation

    # Yardımcı ifade (SET, DO, ALTER ROLE ...) ANALYZE edilemez ve değer taşıyor — ayar açık olsa
    # bile örnek olarak saklanmıyor (Faz 31 Commit 5: DO bloğunun metni saklanıyordu).
    if _has_placeholders(text) or classify_system_query(text) or is_utility_statement(text):
        return False
    return not detect_truncation(text).truncated


async def _store_real_query_samples(session: AsyncSession) -> bool:
    from app.services.analysis_settings import get_analysis_settings

    return bool((await get_analysis_settings(session))["store_real_query_samples"])


async def _write_samples(
    session: AsyncSession, instance_id: int, samples: dict[str, tuple[str, float]], *, captured_at: datetime
) -> None:
    """Örneği, saklanan örnekten DAHA YAVAŞSA değiştirir: temsili örnek en yavaş çalıştırma."""
    from app.services.query_text_privacy import sanitize_stored_query

    rows = (
        await session.execute(
            select(WaitQuerySignature).where(
                WaitQuerySignature.instance_id == instance_id,
                WaitQuerySignature.queryid.in_(list(samples.keys())),
            )
        )
    ).scalars().all()
    for row in rows:
        text, elapsed = samples[row.queryid]
        if row.sample_duration_ms is None or elapsed > row.sample_duration_ms:
            # Yardımcı ifade örneğe hiç girmiyor (is_sample_candidate); ikinci savunma.
            row.sample_query_text = sanitize_stored_query(text, keep_values=True)
            row.sample_duration_ms = round(elapsed, 3)
            row.sample_captured_at = captured_at


#: Bir dakikalık bekleme kovasında (instance başına) en fazla satır — sorgu × bekleme olayı.
MAX_WAIT_ROWS_PER_MINUTE = 5000


async def enforce_query_text_privacy(session: AsyncSession) -> int:
    """Saklanmış gerçek değerli metni temizler.

    - `query_text` HER DURUMDA değerlerden arındırılıyor (yük kırılımında ve teknik raporda
      görünen alan).
    - `sample_*` alanları ayar KAPALIYSA siliniyor.
    - auto_explain planları (`captured_plans`) ayar KAPALIYSA değerlerden arındırılıyor
      (Faz 31 Commit 4 kararı: planlar aynı ayara bağlı).

    İki yerden çağrılıyor: ayar kapatıldığında ve süreç başına ilk yazımda. İkincisi
    yükseltme durumu için: Faz 31 öncesinde `query_text` her zaman ham metindi. İşlem
    idempotent. Değişen satır sayısını döner.
    """
    from app.services.query_text_privacy import sanitize_stored_query

    keep_samples = await _store_real_query_samples(session)
    changed = 0
    # Faz 31 Commit 9: parti parti (id sırasıyla PRIVACY_BATCH satır) — tek sorguda bütün tablo değil.
    async for row in _batches(session, WaitQuerySignature):
        normalized = sanitize_stored_query(row.query_text or "", keep_values=False)
        drop_sample = not keep_samples and row.sample_query_text is not None
        if normalized != row.query_text or drop_sample:
            row.query_text = normalized
            if drop_sample:
                row.sample_query_text = None
                row.sample_duration_ms = None
                row.sample_captured_at = None
            changed += 1
    if not keep_samples:
        from app.models import CapturedPlan
        from app.services.sql_analysis import strip_plan_values

        async for plan in _batches(session, CapturedPlan):
            text = sanitize_stored_query(plan.query_text or "", keep_values=False)
            body = strip_plan_values(plan.plan_json) if plan.plan_json is not None else None
            if text != plan.query_text or body != plan.plan_json:
                plan.query_text = text
                plan.plan_json = body
                changed += 1
    await session.commit()
    return changed


PRIVACY_BATCH = 500


async def _batches(session: AsyncSession, model):
    last_id = 0
    while True:
        rows = (
            await session.execute(select(model).where(model.id > last_id).order_by(model.id).limit(PRIVACY_BATCH))
        ).scalars().all()
        if not rows:
            return
        for row in rows:
            yield row
        last_id = rows[-1].id


async def _write_signatures(session: AsyncSession, instance_id: int, texts: dict[str, str]) -> None:
    """queryid → metin sözlüğünü günceller. Bilinen queryid'ler süreç belleğinde tutuluyor;
    aynı sorgu için dakikada bir SELECT atmak gereksiz.

    Faz 31 İŞ 2: sözlüğe HER ZAMAN değerlerden arındırılmış metin yazılıyor — eskiden
    pg_stat_activity'nin ham metni yazılıyordu ve yük kırılımında gerçek değerler görünüyordu.
    Gerçek değer yalnızca `sample_*` alanlarında ve yalnızca ayar açıkken.
    """
    from app.services.query_text_privacy import sanitize_stored_query

    unknown = {qid: text for qid, text in texts.items() if (instance_id, qid) not in _known_signatures}
    if not unknown:
        return
    existing = (
        await session.execute(
            select(WaitQuerySignature).where(
                WaitQuerySignature.instance_id == instance_id,
                WaitQuerySignature.queryid.in_(list(unknown.keys())),
            )
        )
    ).scalars().all()
    now = datetime.now(UTC)
    for row in existing:
        row.last_seen_at = now
        _known_signatures.add((instance_id, row.queryid))
    found = {row.queryid for row in existing}
    for queryid, text in unknown.items():
        if queryid in found:
            continue
        session.add(
            WaitQuerySignature(
                instance_id=instance_id,
                queryid=queryid,
                query_text=sanitize_stored_query(text, keep_values=False),
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        _known_signatures.add((instance_id, queryid))


async def flush_completed_buckets(session: AsyncSession) -> int:
    """Dakikası kapanmış kovaları yazar. Yazılan kova sayısını döner."""
    if not _pending_flush:
        return 0
    pending = list(_pending_flush)
    _pending_flush.clear()
    global _privacy_enforced
    if not _privacy_enforced:
        try:
            await enforce_query_text_privacy(session)
            _privacy_enforced = True
        except Exception:
            logger.exception("Sorgu metni gizlilik temizliği başarısız; bir sonraki turda tekrar denenecek")
    for instance_id, bucket in pending:
        if bucket.samples_taken == 0:
            continue
        try:
            await _write_bucket(session, instance_id, bucket)
        except Exception:
            logger.exception(
                "Bekleme kovası yazılamadı (instance %s, dakika %s)", instance_id, bucket.minute
            )
    return len(pending)


async def flush_all_buckets(session: AsyncSession) -> int:
    """Açık dakikalar dahil HER ŞEYİ yazar — worker kapanırken çağrılır ki son dakika
    kaybolmasın."""
    for instance_id, bucket in list(_buckets.items()):
        _pending_flush.append((instance_id, bucket))
    _buckets.clear()
    return await flush_completed_buckets(session)


def _record_round(now: datetime, duration: float, instance_count: int) -> None:
    """Tur istatistiğini biriktirir; gerekiyorsa özet ya da gecikme uyarısı yazar.

    TUR BAŞINA LOG YAZILMIYOR (Faz 27 İŞ 2). Saniyede bir çalışan bir işte her turu
    loglamak günde 86.400 satır demek ve gerçek hatalar o yığının içinde kaybolur.
    Yazılan iki şey var: 5 dakikada bir ÖZET, ve tek tek anlamlı olaylar (gecikmiş tur).
    """
    _stats.rounds += 1
    _stats.samples += instance_count
    _stats.total_duration += duration
    _stats.max_duration = max(_stats.max_duration, duration)
    _stats.failures = sum(1 for s in _samplers.values() if s.consecutive_failures > 0)

    interval = max(settings.wait_sample_interval_seconds, 1)
    if duration >= interval * SLOW_ROUND_FACTOR:
        _stats.slow_rounds += 1
        # Gecikmiş tur ANLAMLI bir olay: ölçümde delik açıyor ve AAS'in paydasını düşürüyor.
        logger.warning(
            "Bekleme örnekleme turu gecikti: %.1f sn (aralık %s sn, %s instance). "
            "Bu süre boyunca örnek alınamadı.",
            duration, interval, instance_count,
        )

    if _stats.last_logged_at is None:
        _stats.last_logged_at = now
        return
    if (now - _stats.last_logged_at).total_seconds() < SUMMARY_LOG_INTERVAL_SECONDS:
        return

    average = _stats.total_duration / _stats.rounds if _stats.rounds else 0.0
    logger.info(
        "Bekleme örnekleyici özeti: %s tur, %s instance, ortalama %.0f ms, en yavaş %.0f ms, "
        "%s gecikmiş tur, %s instance hata veriyor",
        _stats.rounds, instance_count, average * 1000, _stats.max_duration * 1000,
        _stats.slow_rounds, _stats.failures,
    )
    _stats.rounds = 0
    _stats.samples = 0
    _stats.total_duration = 0.0
    _stats.max_duration = 0.0
    _stats.slow_rounds = 0
    _stats.last_logged_at = now


async def sampling_tick() -> None:
    """Örnekleyicinin bir turu: her instance'tan bir fotoğraf + kapanmış dakikaları yaz."""
    if not settings.wait_sampling_enabled:
        return
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        instances = await _load_instances(session, now)

    started = time.monotonic()
    if instances:
        # Sıralı değil PARALEL: yavaş/erişilemeyen tek bir sunucu, diğerlerinin örneklemesini
        # geciktirmemeli. `return_exceptions` — bir instance'ın hatası turu düşürmesin
        # (hata zaten _sample_instance içinde yakalanıyor, bu ikinci savunma).
        await asyncio.gather(*(_sample_instance(i, now) for i in instances), return_exceptions=True)
    _record_round(now, time.monotonic() - started, len(instances))

    due_for_blocking = [
        instance
        for instance in instances
        if _blocking_check_due(_samplers.get(instance.id), now)
    ]
    if _pending_flush or due_for_blocking:
        async with SessionLocal() as session:
            written = await flush_completed_buckets(session)
            for instance in due_for_blocking:
                await _check_blocking(session, instance, now)
            await session.commit()
            if written:
                logger.debug("Bekleme örnekleri yazıldı: %s dakika kovası", written)


def _blocking_check_due(sampler: _InstanceSampler | None, now: datetime) -> bool:
    if sampler is None or sampler.conn is None:
        return False
    last = sampler.last_blocking_check
    return last is None or (now - last).total_seconds() >= BLOCKING_CHECK_INTERVAL_SECONDS


async def _check_blocking(session: AsyncSession, instance: Instance, now: datetime) -> None:
    """Bloklama fotoğrafı çekip geçmişe işler (Faz 26 İŞ 3).

    Örnekleyicinin KALICI bağlantısı kullanılıyor. Hata durumunda sessizce geçiliyor:
    bloklama geçmişi bir ek yetenek, bekleme örneklemesini düşürmesi kabul edilemez.
    """
    sampler = _samplers.get(instance.id)
    if sampler is None or sampler.conn is None:
        return
    sampler.last_blocking_check = now
    try:
        rows = await sampler.collector.collect_blocking(conn=sampler.conn.raw)
    except Exception as exc:
        logger.debug("Bloklama kontrolü atlandı (instance %s): %s", instance.name, exc)
        return
    try:
        await record_tree(session, instance.id, build_blocking_tree(rows), now=now)
    except Exception:
        logger.exception("Bloklama geçmişi yazılamadı (instance %s)", instance.name)


async def shutdown_sampling() -> None:
    """Bağlantıları kapatır, yarım kalmış dakikaları ve açık bloklama olaylarını yazar."""
    async with SessionLocal() as session:
        try:
            await flush_all_buckets(session)
            # Açık bloklama olayları kapatılmazsa `ended_at` sonsuza kadar NULL kalır ve olay
            # raporlarda "hâlâ sürüyor" gibi görünür.
            await close_open_episodes(session)
            await session.commit()
        except Exception:
            logger.exception("Kapanışta bekleme kovaları yazılamadı")
    for sampler in list(_samplers.values()):
        await _drop_connection(sampler)
    _samplers.clear()


def sampling_status() -> dict:
    """Örnekleyicinin canlı durumu — admin ekranı ve teşhis için."""
    return {
        "enabled": settings.wait_sampling_enabled,
        "interval_seconds": settings.wait_sample_interval_seconds,
        "instances_sampled": len(_samplers),
        "instances_failing": sum(1 for s in _samplers.values() if s.consecutive_failures > 0),
        "open_buckets": len(_buckets),
        "pending_flush": len(_pending_flush),
    }


def expected_samples_per_minute() -> float:
    interval = max(settings.wait_sample_interval_seconds, 1)
    return 60.0 / interval


def sampling_window_start(hours: int) -> datetime:
    return minute_floor(datetime.now(UTC) - timedelta(hours=hours))
