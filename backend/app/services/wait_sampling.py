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

TUR, META VERİTABANINI BEKLEMEZ (Faz 31 Commit 10a):
Canlıda 1 saniyelik iş "maximum number of running instances reached" ile tur atlıyordu ve özet "0 gecikmiş tur"
diyordu. Neden: tur, izlenen sunucuya giden örneklemeyle AYNI görevde meta veritabanına yazımı (dakika
kapanışı, bloklama geçmişi, instance listesi) da yapıyordu ve tur süresi (`_record_round`) yalnızca ilk kısmı
ölçüyordu. Şimdi:
- Zamanlayıcı turu (`sampling_tick`) yalnızca instance başına BİR örnekleme görevi başlatıp dönüyor; hedef
  tarafındaki yavaşlık yalnızca o instance'ın kendi örneğini geciktiriyor, diğerlerini ve zamanlayıcıyı değil.
- Meta veritabanına yazım ayrı işte (`flush_tick`, kendi aralığında): yavaş meta veritabanı örneklemeyi durdurmaz.
- Aralık ÖLÇÜLÜYOR: ardışık başarılı örneklerin geliş farkı. Atlanan tur, hedefle ölçülen ayrışması ve dakika başına
  en uzun boşluk hem özet logda hem veritabanı yükü ekranında görünüyor (`sampling_cadence.py`).

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

from sqlalchemy import case, func, null, select
from sqlalchemy.dialects import postgresql as pg_dialect
from sqlalchemy.dialects import sqlite as sqlite_dialect
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import BaseCollector, SamplingConnection
from app.collectors.registry import get_collector
from app.config import settings
from app.database import SessionLocal
from app.domain.engines import DatabaseEngine
from app.models import ActiveSessionMinute, Instance, WaitQuerySignature, WaitSampleMinute
from app.services.blocking import build_blocking_tree
from app.services.blocking_history import close_all as close_open_episodes
from app.services.blocking_history import has_open_episode, record_tree
from app.services.collection import connection_target_for
from app.services.sampling_cadence import assess as assess_cadence

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


#: Meta veritabanına yazım işinin aralığı. Kapanmış dakika kovaları ve bloklama fotoğrafları en geç bu kadar sonra
#: yazılır; örnekleme turundan BAĞIMSIZ (yavaş meta veritabanı örneklemeyi geciktirmesin diye ayrı iş).
FLUSH_INTERVAL_SECONDS = 5

#: Meta veritabanı ulaşılamazken bekleyen bloklama fotoğrafı sayısı bunu aşarsa en eskiler atılır.
MAX_PENDING_BLOCKING = 200

#: Bir özet penceresinde saklanan aralık örneği sayısı (p95 için). Sayaçlar bunun ötesinde de sayılır.
MAX_GAP_SAMPLES = 50_000


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
    # Faz 31 Commit 10a: bu dakikaya düşen örneklerin ÖLÇÜLEN en uzun boşluğu (ms). None = ölçülmedi (dakikanın
    # tek örneği süreç başlangıcına denk geldi).
    max_gap_ms: int | None = None


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
    #: Çalışan örnekleme görevi. Bir instance'ın kalıcı bağlantısında aynı anda TEK sorgu olabilir: önceki görev
    #: sürerken gelen tur bu instance için atlanır (ve sayılır), diğer instance'lar etkilenmez.
    in_flight: asyncio.Task | None = None
    #: Son BAŞARILI örneğin geliş anı (time.monotonic) — gerçek aralığın ölçüsü.
    last_success: float | None = None
    skipped_in_flight: int = 0


# Süreç ömrü boyunca yaşayan durum. Modül seviyesinde: `_previous_state` (collection.py) ile
# aynı kalıp — worker tek süreç ve tek olay döngüsü.
_samplers: dict[int, _InstanceSampler] = {}
_buckets: dict[int, _MinuteBucket] = {}
_known_signatures: set[tuple[int, str]] = set()
_instance_cache: list[Instance] = []
_instance_cache_at: datetime | None = None
#: Dakikası kapanmış, henüz yazılmamış kovalar.
_pending_flush: list[tuple[int, _MinuteBucket]] = []
#: Hedeften okunmuş, henüz meta veritabanına yazılmamış bloklama fotoğrafları: (instance_id, ad, satırlar, an).
_pending_blocking: list[tuple[int, str, list[dict], datetime]] = []
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
    # Faz 31 Commit 10a — aralık ve bileşen ölçümü.
    gap_count: int = 0
    gap_sum: float = 0.0
    gap_max: float = 0.0
    gaps: list[float] = field(default_factory=list)
    #: Aralıklardan türetilen atlanan tur: bir boşluk k aralık uzunluğundaysa k-1 örnek alınamamış demektir.
    missed_slots: int = 0
    #: Önceki örnekleme sürerken gelen tur nedeniyle o instance için atlananlar (missed_slots'un bir nedeni).
    skipped_in_flight: int = 0
    #: APScheduler'ın "maximum number of running instances" ile atladığı tur (örnekleme ve yazım işleri).
    scheduler_skips: int = 0
    connects: int = 0
    connect_seconds: float = 0.0
    queries: int = 0
    query_seconds: float = 0.0
    blocking_checks: int = 0
    blocking_seconds: float = 0.0
    flushes: int = 0
    flush_seconds: float = 0.0
    flush_max: float = 0.0

    def reset_window(self, now: datetime) -> None:
        """Özet yazıldıktan sonra sayaçları sıfırlar; yeni pencere `now`'dan başlar."""
        self.__dict__.update(_RoundStats(last_logged_at=now).__dict__)


_stats = _RoundStats()


def reset_state() -> None:
    """Testler ve worker yeniden başlangıcı için: bellekteki her şeyi bırakır."""
    for sampler in _samplers.values():
        if sampler.in_flight is not None and not sampler.in_flight.done():
            sampler.in_flight.cancel()
    _samplers.clear()
    _buckets.clear()
    _known_signatures.clear()
    _pending_flush.clear()
    _pending_blocking.clear()
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


async def _sample_instance(instance: Instance, now: datetime, *, clock=time.monotonic) -> None:
    """Tek instance için tek fotoğraf. Hata durumunda bağlantı düşürülür ve bir sonraki turda
    yeniden kurulur — kalıcı bağlantı, kopmuş bağlantıyı sonsuza kadar taşımak demek değil.

    Yalnızca İZLENEN SUNUCUYLA konuşur; meta veritabanına dokunmaz (yazım `flush_tick`'te). Süre ve gerçek aralık
    burada ölçülür: bileşenler (bağlanma / sorgu / bloklama) ayrı sayaçlarda, aralık ardışık başarılı örneklerin
    geliş farkı olarak.
    """
    sampler = await _ensure_sampler(instance)
    started = clock()
    try:
        if sampler.conn is None:
            connect_started = clock()
            sampler.conn = await sampler.collector.open_sampling_connection()
            _stats.connects += 1
            _stats.connect_seconds += clock() - connect_started
            if sampler.conn is None:
                return  # motor desteklemiyor
        query_started = clock()
        snapshot = await sampler.collector.sample_active_sessions(sampler.conn)
        _stats.queries += 1
        _stats.query_seconds += clock() - query_started
    except Exception as exc:
        sampler.consecutive_failures += 1
        await _drop_connection(sampler)
        if sampler.consecutive_failures == 1 or sampler.consecutive_failures % FAILURE_LOG_EVERY == 0:
            logger.warning(
                "Bekleme örneklemesi başarısız (instance %s, üst üste %s tur): %s",
                instance.name, sampler.consecutive_failures, exc,
            )
        _record_round(now, clock() - started, len(_samplers))
        return

    if snapshot is None:
        return
    finished = clock()
    # Gerçek aralık: bu örneğin geliş anı - önceki başarılı örneğin geliş anı. Başarısız turlar, atlanan turlar ve
    # yeniden bağlanma süresi bu farka OTOMATİK girer (hiçbiri ayrıca sayılmak zorunda değil).
    gap = None if sampler.last_success is None else finished - sampler.last_success
    sampler.last_success = finished
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
    if gap is not None:
        gap_ms = int(round(gap * 1000))
        bucket.max_gap_ms = gap_ms if bucket.max_gap_ms is None else max(bucket.max_gap_ms, gap_ms)

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

    _record_round(now, finished - started, len(_samplers), gap_seconds=gap)
    if _blocking_wanted(sampler, snapshot, now):
        rows = await _collect_blocking(sampler, instance, now)
        if rows is not None:
            _pending_blocking.append((instance.id, instance.name, rows, now))
            del _pending_blocking[:-MAX_PENDING_BLOCKING]


def _is_sqlite() -> bool:
    return settings.database_url.startswith("sqlite")


def _dialect_insert(model):
    """INSERT ... ON CONFLICT lehçeye göre: PostgreSQL ve SQLite aynı sözdizimini destekliyor, sınıf farklı."""
    return (sqlite_dialect if _is_sqlite() else pg_dialect).insert(model)


def _greatest(left, right):
    # SQLite'ta çok argümanlı max() skaler; PostgreSQL'de GREATEST. İkisi de NULL'u YUTMAZ/YAYMAZ diye
    # çağıran coalesce ile sarıyor.
    return func.max(left, right) if _is_sqlite() else func.greatest(left, right)


#: Tek INSERT'te en fazla kaç bekleme satırı. asyncpg parametre sınırı 32 767; satır başına 6 parametre.
WAIT_ROWS_PER_STATEMENT = 500


#: Tek INSERT'te en fazla kaç dakika toplamı satırı (satır başına 6 parametre).
TOTAL_ROWS_PER_STATEMENT = 1000


def _merge_buckets(pairs: list[tuple[int, _MinuteBucket]]) -> tuple[dict, dict]:
    """Kovaları (instance, dakika) anahtarına göre TEK satıra indirir.

    `INSERT ... ON CONFLICT DO UPDATE` aynı satırı tek ifadede iki kez güncelleyemez; aynı anahtar iki kez gelirse
    (örneğin kapanışta yarım kalmış kova ile yeniden açılmış aynı dakikanın kovası) burada toplanır."""
    totals: dict[tuple[int, datetime], dict] = {}
    waits: dict[tuple[int, datetime, str, str, str], int] = {}
    for instance_id, bucket in pairs:
        key = (instance_id, bucket.minute)
        row = totals.setdefault(key, {"samples_taken": 0, "active_sessions_sampled": 0,
                                      "blocked_sessions_sampled": 0, "max_gap_ms": None})
        row["samples_taken"] += bucket.samples_taken
        row["active_sessions_sampled"] += bucket.active_sessions_sampled
        row["blocked_sessions_sampled"] += bucket.blocked_sessions_sampled
        if bucket.max_gap_ms is not None:
            row["max_gap_ms"] = bucket.max_gap_ms if row["max_gap_ms"] is None else max(row["max_gap_ms"], bucket.max_gap_ms)
        for (queryid, category, event), count in bucket.counts.items():
            wait_key = (instance_id, bucket.minute, queryid, category, event)
            waits[wait_key] = waits.get(wait_key, 0) + count
    return totals, waits


async def _write_counts(session: AsyncSession, pairs: list[tuple[int, _MinuteBucket]]) -> None:
    """Kovaların SAYAÇLARINI (dakika toplamı + bekleme kırılımı) EKLEYEREK yazar — kova sayısından bağımsız,
    en fazla birkaç ifadede ve önce okumadan.

    Aynı (instance, dakika) için satır zaten varsa sayaçlar toplanır (`ON CONFLICT DO UPDATE ... + excluded`).
    Bu, worker'ın dakika ortasında yeniden başladığı durumu doğru ele alır: iki yarım kova aynı dakikayı temsil
    eder ve toplamları o dakikanın gerçeğidir. Üzerine yazsaydık ilk yarı sessizce kaybolurdu.

    Faz 31 Commit 10a: eskiden kova başına önce mevcut satırlar OKUNUYOR (dakika toplamı + en fazla 5.000 bekleme
    satırı), sonra tek tek ekleniyordu; sonra kova başına 2 ifade oldu, 20 kova = 40 gidiş-dönüş. Meta veritabanı
    uzaktayken (canlıda Supabase) gidiş-dönüş sayısı yazım süresinin ta kendisi; şimdi TÜM kovalar tek toplu ifadede
    (satır sayısına göre bölünerek) yazılıyor.
    """
    totals_table = ActiveSessionMinute.__table__
    waits_table = WaitSampleMinute.__table__
    merged_totals, merged_waits = _merge_buckets(pairs)

    total_rows = [{"instance_id": instance_id, "minute": minute, **values}
                  for (instance_id, minute), values in merged_totals.items()]
    for offset in range(0, len(total_rows), TOTAL_ROWS_PER_STATEMENT):
        total_insert = _dialect_insert(ActiveSessionMinute).values(total_rows[offset:offset + TOTAL_ROWS_PER_STATEMENT])
        incoming = total_insert.excluded
        await session.execute(
            total_insert.on_conflict_do_update(
                index_elements=[totals_table.c.instance_id, totals_table.c.minute],
                set_={
                    "samples_taken": totals_table.c.samples_taken + incoming.samples_taken,
                    "active_sessions_sampled": totals_table.c.active_sessions_sampled + incoming.active_sessions_sampled,
                    "blocked_sessions_sampled": totals_table.c.blocked_sessions_sampled + incoming.blocked_sessions_sampled,
                    # İkisi de NULL ise NULL kalır ("ölçülmedi"); biri ölçülmüşse büyüğü.
                    "max_gap_ms": case(
                        (totals_table.c.max_gap_ms.is_(None) & incoming.max_gap_ms.is_(None), null()),
                        else_=_greatest(func.coalesce(totals_table.c.max_gap_ms, 0),
                                        func.coalesce(incoming.max_gap_ms, 0)),
                    ),
                },
            )
        )

    wait_rows = [
        {"instance_id": instance_id, "minute": minute, "queryid": queryid, "wait_category": category,
         "wait_event": event, "sample_count": count}
        for (instance_id, minute, queryid, category, event), count in merged_waits.items()
    ]
    for offset in range(0, len(wait_rows), WAIT_ROWS_PER_STATEMENT):
        wait_insert = _dialect_insert(WaitSampleMinute).values(wait_rows[offset:offset + WAIT_ROWS_PER_STATEMENT])
        await session.execute(
            wait_insert.on_conflict_do_update(
                index_elements=[waits_table.c.instance_id, waits_table.c.minute, waits_table.c.queryid,
                                waits_table.c.wait_category, waits_table.c.wait_event],
                set_={"sample_count": waits_table.c.sample_count + wait_insert.excluded.sample_count},
            )
        )


async def _write_extras(session: AsyncSession, instance_id: int, bucket: _MinuteBucket,
                        store_samples: bool | None = None) -> None:
    """Sorgu sözlüğü, bind işareti ve temsili örnek: yalnızca kovada VARSA sorgu atılır (çoğu kova için sıfır ifade)."""
    await _write_signatures(session, instance_id, bucket.query_texts)
    await session.flush()
    if bucket.bind_parameter_queryids:
        await _mark_bind_parameters(session, instance_id, bucket.bind_parameter_queryids)
    if bucket.samples:
        if store_samples is None:
            store_samples = await _store_real_query_samples(session)
        if store_samples:
            await _write_samples(session, instance_id, bucket.samples, captured_at=bucket.minute)


async def _write_bucket(session: AsyncSession, instance_id: int, bucket: _MinuteBucket) -> None:
    """Tek kovanın tam yazımı (sayaçlar + sözlük/örnek). Toplu yol `flush_completed_buckets`; bu, tek kova isteyen
    çağıranlar (testler, tek seferlik araçlar) ve toplu yazım başarısız olduğunda kova kova yedek yol için."""
    await _write_counts(session, [(instance_id, bucket)])
    await _write_extras(session, instance_id, bucket)


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
    writable = [(instance_id, bucket) for instance_id, bucket in pending if bucket.samples_taken]
    if writable:
        # TOPLU yazım: kova sayısından bağımsız birkaç ifade (bkz. `_write_counts`). Başarısız olursa işlem
        # geri alınıp kova kova denenir — tek bozuk kova diğerlerini götürmesin.
        counts_written = False
        try:
            await _write_counts(session, writable)
            counts_written = True
        except Exception:
            logger.exception("Bekleme kovaları toplu yazılamadı; kova kova denenecek")
            await session.rollback()
        store_samples: bool | None = None
        for instance_id, bucket in writable:
            try:
                if not counts_written:
                    await _write_bucket(session, instance_id, bucket)
                    continue
                if bucket.samples and store_samples is None:
                    store_samples = await _store_real_query_samples(session)  # ayar flush başına BİR kez okunur
                await _write_extras(session, instance_id, bucket, store_samples)
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


def _interval_seconds() -> int:
    return max(settings.wait_sample_interval_seconds, 1)


def note_scheduler_skip() -> None:
    """APScheduler örnekleme ya da yazım işinin bir turunu "maximum number of running instances" ile atladı.

    Örnekleme turu artık hedefi/meta veritabanını beklemediği için bunun 0 kalması BEKLENİR; sıfırdan farklıysa
    zamanlayıcının kendisi (olay döngüsü doygun) sorunludur ve özet bunu ayrıca gösterir."""
    _stats.scheduler_skips += 1


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(pct / 100.0 * (len(ordered) - 1))))]


def current_cadence():
    """Şu ana kadarki pencerenin (özet aralığı) aralık değerlendirmesi — özet log ve durum ucu bunu kullanır."""
    measured = _stats.gap_sum / _stats.gap_count * 1000 if _stats.gap_count else None
    longest = _stats.gap_max * 1000 if _stats.gap_count else None
    return assess_cadence(target_interval_seconds=_interval_seconds(), measured_interval_ms=measured,
                          max_gap_ms=longest)


def _record_round(now: datetime, duration: float, instance_count: int, *, gap_seconds: float | None = None) -> None:
    """Tur istatistiğini biriktirir; gerekiyorsa özet ya da gecikme uyarısı yazar.

    TUR BAŞINA LOG YAZILMIYOR (Faz 27 İŞ 2). Saniyede bir çalışan bir işte her turu
    loglamak günde 86.400 satır demek ve gerçek hatalar o yığının içinde kaybolur.
    Yazılan iki şey var: 5 dakikada bir ÖZET, ve tek tek anlamlı olaylar (gecikmiş tur).

    Bir "tur" = BİR instance'ın bir örnekleme denemesi (Faz 31 Commit 10a; eskiden tüm instance'ları saran
    tek `gather`). `gap_seconds` bu instance'ın önceki başarılı örneğinden bu yana geçen GERÇEK süre: atlanan tur
    burada, hedef aralığın katı olan boşluk olarak görünür. Eski özet yalnızca tur süresini ölçüyordu — tur
    yavaşlamadan da tur ATLANABİLİYORDU (zamanlayıcı ya da yeniden bağlanma), o durumda özet "0 gecikmiş tur" diyordu.
    """
    _stats.rounds += 1
    _stats.samples += 1
    _stats.total_duration += duration
    _stats.max_duration = max(_stats.max_duration, duration)
    _stats.failures = sum(1 for s in _samplers.values() if s.consecutive_failures > 0)

    interval = _interval_seconds()
    if gap_seconds is not None:
        _stats.gap_count += 1
        _stats.gap_sum += gap_seconds
        _stats.gap_max = max(_stats.gap_max, gap_seconds)
        if len(_stats.gaps) < MAX_GAP_SAMPLES:
            _stats.gaps.append(gap_seconds)
        skipped = round(gap_seconds / interval) - 1
        if skipped > 0:
            _stats.missed_slots += skipped

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
    cadence = current_cadence()
    gap_average = _stats.gap_sum / _stats.gap_count * 1000 if _stats.gap_count else None
    lines = [
        "Bekleme örnekleyici özeti: %s tur, %s instance, ortalama %.0f ms, en yavaş %.0f ms, "
        "%s gecikmiş tur, %s instance hata veriyor" % (
            _stats.rounds, instance_count, average * 1000, _stats.max_duration * 1000,
            _stats.slow_rounds, _stats.failures,
        ),
        "gerçek aralık: %s (hedef %s ms, p95 %.0f ms, en uzun boşluk %.0f ms)" % (
            "ölçülemedi" if gap_average is None else f"ort {gap_average:.0f} ms", cadence.target_interval_ms,
            _percentile(_stats.gaps, 95) * 1000, _stats.gap_max * 1000,
        ),
        "atlanan tur: %s (%s tanesi önceki örnek sürerken, zamanlayıcı atlaması %s)" % (
            _stats.missed_slots, _stats.skipped_in_flight, _stats.scheduler_skips,
        ),
        "süre kırılımı: bağlanma %s kez ort %.0f ms, sorgu ort %.0f ms, bloklama %s kez ort %.0f ms, "
        "meta yazımı %s kez ort %.0f ms (en yavaş %.0f ms)" % (
            _stats.connects, _stats.connect_seconds / max(_stats.connects, 1) * 1000,
            _stats.query_seconds / max(_stats.queries, 1) * 1000,
            _stats.blocking_checks, _stats.blocking_seconds / max(_stats.blocking_checks, 1) * 1000,
            _stats.flushes, _stats.flush_seconds / max(_stats.flushes, 1) * 1000, _stats.flush_max * 1000,
        ),
    ]
    if cadence.message:
        lines.append(cadence.message)
    # Aralık tutturulamadıysa özet UYARI olarak yazılıyor: ölçümün güvenilirliği sorunlu, sessiz geçilmiyor.
    logger.log(logging.WARNING if cadence.message else logging.INFO, ". ".join(lines))
    _stats.reset_window(now)


async def _instances_for_tick(now: datetime, *, refresh: bool) -> list[Instance]:
    """Örnekleme turunun instance listesi. Zamanlayıcı turu meta veritabanına GİTMEZ: liste `flush_tick`'te
    tazeleniyor. Tek istisna süreç başlangıcı (önbellek hiç dolmadıysa bir kez)."""
    if not refresh and _instance_cache_at is not None:
        return list(_instance_cache)
    async with SessionLocal() as session:
        return await _load_instances(session, now)


async def _retire_removed_samplers(instances: list[Instance]) -> None:
    """Silinen/devre dışı bırakılan instance'ın bağlantısını kapatır ve açık kovasını yazılmaya bırakır.

    Eskiden bu instance'lar `_samplers`'ta sonsuza kadar kalıyordu: açık kalıcı bağlantı ve sızan kova."""
    active = {i.id for i in instances}
    for instance_id in [key for key in _samplers if key not in active]:
        sampler = _samplers.pop(instance_id)
        if sampler.in_flight is not None and not sampler.in_flight.done():
            sampler.in_flight.cancel()
            await asyncio.gather(sampler.in_flight, return_exceptions=True)
        await _drop_connection(sampler)
        bucket = _buckets.pop(instance_id, None)
        if bucket is not None:
            _pending_flush.append((instance_id, bucket))


async def _run_sampling_task(instance: Instance, now: datetime) -> None:
    """Arka plan görevi sarmalayıcısı: hata yutulmuyor ama "Task exception was never retrieved" da üretmiyor."""
    try:
        await _sample_instance(instance, now)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Bekleme örneklemesi görevi başarısız (instance %s)", instance.name)


async def sampling_tick(*, wait: bool = True, flush: bool = True) -> None:
    """Örnekleyicinin bir turu: her instance için bir örnekleme görevi başlatır.

    `wait=False, flush=False` = ZAMANLAYICININ kullandığı biçim: görevleri başlatıp HEMEN döner. Bir instance'ın
    örneklemesi sürerken (yavaş ağ, yeniden bağlanma) o instance için yeni görev başlatılmaz — kalıcı bağlantıda
    aynı anda tek sorgu olabilir — ve bu atlama SAYILIR; diğer instance'lar ve zamanlayıcı etkilenmez. Meta
    veritabanına yazım `flush_tick`'in işi.

    Varsayılan (`wait=True, flush=True`) eski davranış: görevleri bekler, ardından yazar. Testler ve tek seferlik
    araçlar tam sonuç istediği için böyle çağırıyor; zamanlayıcı ÇAĞIRMIYOR.
    """
    if not settings.wait_sampling_enabled:
        return
    now = datetime.now(UTC)
    instances = await _instances_for_tick(now, refresh=flush)
    await _retire_removed_samplers(instances)

    launched: list[asyncio.Task] = []
    for instance in instances:
        sampler = await _ensure_sampler(instance)
        if sampler.in_flight is not None and not sampler.in_flight.done():
            sampler.skipped_in_flight += 1
            _stats.skipped_in_flight += 1
            continue
        sampler.in_flight = asyncio.create_task(_run_sampling_task(instance, now))
        launched.append(sampler.in_flight)
    if wait and launched:
        await asyncio.gather(*launched, return_exceptions=True)
    if flush:
        await flush_tick(refresh_instances=False)


async def flush_tick(*, refresh_instances: bool = True) -> None:
    """Meta veritabanına yazım işi: kapanmış dakika kovaları, bloklama fotoğrafları, instance listesi.

    Örnekleme turundan AYRI ve kendi aralığında (`FLUSH_INTERVAL_SECONDS`). Meta veritabanı yavaşsa ya da uzaktaysa
    (canlıda Supabase) bu iş uzar ama örnekleme aynı hızda sürer; kaçırılan örnek yok, yalnızca yazım gecikir.
    """
    started = time.monotonic()
    now = datetime.now(UTC)
    async with SessionLocal() as session:
        if refresh_instances:
            await _load_instances(session, now)
        written = await flush_completed_buckets(session)
        recorded = await _record_pending_blocking(session)
        await session.commit()
    if written or recorded:
        elapsed = time.monotonic() - started
        _stats.flushes += 1
        _stats.flush_seconds += elapsed
        _stats.flush_max = max(_stats.flush_max, elapsed)
        logger.debug("Bekleme örnekleri yazıldı: %s dakika kovası, %s bloklama fotoğrafı", written, recorded)


def _blocking_check_due(sampler: _InstanceSampler | None, now: datetime) -> bool:
    if sampler is None or sampler.conn is None:
        return False
    last = sampler.last_blocking_check
    return last is None or (now - last).total_seconds() >= BLOCKING_CHECK_INTERVAL_SECONDS


def _blocking_wanted(sampler: _InstanceSampler, snapshot: dict, now: datetime) -> bool:
    """Bloklama ağacı yalnızca GEREKİRSE okunur: fotoğrafta kilit bekleyen oturum varsa ya da bu instance için açık
    bir olay varsa (kapanışının tespiti için). Eskiden her instance için 10 saniyede bir, bloklama olsun olmasın —
    hedefe boşuna sorgu ve bir örnekleme turunun uzaması demekti (kök engelleyici olmadan ağaç zaten boş)."""
    if not _blocking_check_due(sampler, now):
        return False
    return int(snapshot.get("blocked") or 0) > 0 or has_open_episode(sampler.instance_id)


async def _collect_blocking(sampler: _InstanceSampler, instance: Instance, now: datetime) -> list[dict] | None:
    """Bloklama fotoğrafını HEDEFTEN okur (meta veritabanına dokunmaz). Hata → None (bloklama geçmişi ek bir
    yetenek, bekleme örneklemesini düşürmesi kabul edilemez)."""
    if sampler.conn is None:
        return None
    sampler.last_blocking_check = now
    started = time.monotonic()
    try:
        return await sampler.collector.collect_blocking(conn=sampler.conn.raw)
    except Exception as exc:
        logger.debug("Bloklama kontrolü atlandı (instance %s): %s", instance.name, exc)
        return None
    finally:
        _stats.blocking_checks += 1
        _stats.blocking_seconds += time.monotonic() - started


async def _record_blocking(session: AsyncSession, instance_id: int, name: str, rows: list[dict], moment: datetime) -> None:
    try:
        await record_tree(session, instance_id, build_blocking_tree(rows), now=moment)
    except Exception:
        logger.exception("Bloklama geçmişi yazılamadı (instance %s)", name)


async def _record_pending_blocking(session: AsyncSession) -> int:
    if not _pending_blocking:
        return 0
    pending = list(_pending_blocking)
    _pending_blocking.clear()
    for instance_id, name, rows, moment in pending:
        await _record_blocking(session, instance_id, name, rows, moment)
    return len(pending)


async def _check_blocking(session: AsyncSession, instance: Instance, now: datetime) -> None:
    """Bloklama fotoğrafı çekip HEMEN geçmişe işler (Faz 26 İŞ 3) — tek adımda okuma + yazım.

    Zamanlayıcı yolu bunu kullanmaz (okuma örnekleme görevinde, yazım `flush_tick`'te); testler ve tek seferlik
    araçlar için korunuyor. Örnekleyicinin KALICI bağlantısı kullanılıyor."""
    sampler = _samplers.get(instance.id)
    if sampler is None or sampler.conn is None:
        return
    rows = await _collect_blocking(sampler, instance, now)
    if rows is not None:
        await _record_blocking(session, instance.id, instance.name, rows, now)


async def shutdown_sampling() -> None:
    """Bağlantıları kapatır, yarım kalmış dakikaları ve açık bloklama olaylarını yazar."""
    running = [s.in_flight for s in _samplers.values() if s.in_flight is not None and not s.in_flight.done()]
    for task in running:
        task.cancel()
    if running:
        await asyncio.gather(*running, return_exceptions=True)
    async with SessionLocal() as session:
        try:
            await flush_all_buckets(session)
            await _record_pending_blocking(session)
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
    cadence = current_cadence()
    return {
        "enabled": settings.wait_sampling_enabled,
        "interval_seconds": settings.wait_sample_interval_seconds,
        "instances_sampled": len(_samplers),
        "instances_failing": sum(1 for s in _samplers.values() if s.consecutive_failures > 0),
        "open_buckets": len(_buckets),
        "pending_flush": len(_pending_flush),
        "pending_blocking": len(_pending_blocking),
        # Faz 31 Commit 10a: hedeflenen değil ÖLÇÜLEN aralık (mevcut özet penceresi).
        "measured_interval_ms": cadence.measured_interval_ms,
        "missed_slots": _stats.missed_slots,
        "skipped_in_flight": _stats.skipped_in_flight,
        "cadence_message": cadence.message,
    }


def expected_samples_per_minute() -> float:
    interval = max(settings.wait_sample_interval_seconds, 1)
    return 60.0 / interval


def sampling_window_start(hours: int) -> datetime:
    return minute_floor(datetime.now(UTC) - timedelta(hours=hours))
