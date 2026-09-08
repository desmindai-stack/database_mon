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


def reset_state() -> None:
    """Testler ve worker yeniden başlangıcı için: bellekteki her şeyi bırakır."""
    _samplers.clear()
    _buckets.clear()
    _known_signatures.clear()
    _pending_flush.clear()
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
            )
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


async def _write_signatures(session: AsyncSession, instance_id: int, texts: dict[str, str]) -> None:
    """queryid → metin sözlüğünü günceller. Bilinen queryid'ler süreç belleğinde tutuluyor;
    aynı sorgu için dakikada bir SELECT atmak gereksiz."""
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
                query_text=text,
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


async def sampling_tick() -> None:
    """Örnekleyicinin bir turu: her instance'tan bir fotoğraf + kapanmış dakikaları yaz."""
    if not settings.wait_sampling_enabled:
        return
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        instances = await _load_instances(session, now)

    if instances:
        # Sıralı değil PARALEL: yavaş/erişilemeyen tek bir sunucu, diğerlerinin örneklemesini
        # geciktirmemeli. `return_exceptions` — bir instance'ın hatası turu düşürmesin
        # (hata zaten _sample_instance içinde yakalanıyor, bu ikinci savunma).
        await asyncio.gather(*(_sample_instance(i, now) for i in instances), return_exceptions=True)

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
