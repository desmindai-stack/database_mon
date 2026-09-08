"""Bloklama olaylarının geçmiş kaydı (Faz 26 İŞ 3).

Canlı ağaç "şu anda kim kimi blokluyor" sorusunu cevaplıyor. Bu modül ikinci soruyu
cevaplıyor: **"dün gece 03:14'te ne oldu."**

İkisi ayrı sorular ve ikincisi olmadan bloklama hep "olduğu anda ekrana bakabilirsen"
görünür kalır — oysa en kötü olaylar kimsenin bakmadığı saatlerde yaşanır ve sabah geriye
kalan tek şey "gece sistem yavaştı" cümlesidir.

OLAY TANIMI: aynı kök engelleyicinin (pid) kesintisiz bekletme dönemi. Aynı pid tekrar
bekletmeye başlarsa bu YENİ bir olaydır — arada sistem düzelmiş demektir ve iki dönemi tek
olay saymak süreyi olduğundan uzun gösterirdi.

ZİRVE DEĞERLER SAKLANIYOR, anlık değil: olayın etkisini "o an kaç oturum bekliyordu" değil
"en fazla kaç oturum bekledi" anlatır.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BlockingEpisode
from app.services.blocking import BlockingTree

logger = logging.getLogger(__name__)

#: Bu süreden kısa bloklamalar kaydedilmiyor. Kilit beklemesi normal bir olaydır — her
#: milisaniyelik çakışmayı olay olarak yazmak, tabloyu gürültüyle doldurup gerçek olayları
#: görünmez yapardı.
MIN_EPISODE_SECONDS = 5.0

#: Kök engelleyici bu kadar tur boyunca görünmezse olay kapatılıyor. Tek turluk bir
#: kayboluş, ölçüm penceresine denk gelmemiş olabilir; hemen kapatmak tek bir olayı
#: onlarca kısa parçaya bölerdi.
MISSING_ROUNDS_BEFORE_CLOSE = 2


@dataclass
class _OpenEpisode:
    """Bellekte izlenen, henüz bitmemiş olay."""

    instance_id: int
    root_pid: int
    started_at: datetime
    last_seen_at: datetime
    root_query: str = ""
    root_username: str | None = None
    root_application: str | None = None
    root_was_idle: bool = False
    max_blocked_sessions: int = 0
    max_chain_depth: int = 0
    lock_type: str | None = None
    lock_object: str | None = None
    missing_rounds: int = 0
    #: Veritabanına yazıldıysa satır kimliği — olay sürerken de görünsün diye açılışta
    #: yazılıyor, bitişte güncelleniyor.
    row_id: int | None = None


#: (instance_id, root_pid) -> açık olay. Modül seviyesinde: worker tek süreç
#: (`wait_sampling._samplers` ile aynı kalıp).
_open_episodes: dict[tuple[int, int], _OpenEpisode] = {}


def reset_state() -> None:
    _open_episodes.clear()


def open_episodes() -> list[_OpenEpisode]:
    return list(_open_episodes.values())


async def record_tree(
    session: AsyncSession,
    instance_id: int,
    tree: BlockingTree,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Bir bloklama fotoğrafını geçmişe işler. Açılan/kapanan olay sayısını döner."""
    moment = now or datetime.now(UTC)
    counts = {"opened": 0, "closed": 0, "updated": 0}

    seen_pids: set[int] = set()
    for root in tree.roots:
        if root.blocked_total <= 0:
            continue
        seen_pids.add(root.pid)
        key = (instance_id, root.pid)
        episode = _open_episodes.get(key)
        if episode is None:
            episode = _OpenEpisode(
                instance_id=instance_id,
                root_pid=root.pid,
                # Olayın başlangıcı, kök engelleyicinin BEKLETMEYE başladığı an. En iyi
                # yaklaşım transaction'ının açıldığı an; yoksa şu an.
                started_at=_estimate_start(root, moment),
                last_seen_at=moment,
                root_query=root.query,
                root_username=root.username,
                root_application=root.application,
                root_was_idle=root.is_idle_in_transaction,
                lock_type=root.lock_type,
                lock_object=root.lock_object,
            )
            _open_episodes[key] = episode
            counts["opened"] += 1
        else:
            episode.last_seen_at = moment
            episode.missing_rounds = 0
            counts["updated"] += 1
            # Kök engelleyici arada sorgu çalıştırmaya başlayıp durabilir; "sessizdi" bilgisi
            # olay boyunca BİR KEZ bile doğruysa korunuyor, çünkü teşhis açısından belirleyici
            # olan odur.
            episode.root_was_idle = episode.root_was_idle or root.is_idle_in_transaction
            if root.query and not episode.root_query:
                episode.root_query = root.query

        episode.max_blocked_sessions = max(episode.max_blocked_sessions, root.blocked_total)
        episode.max_chain_depth = max(episode.max_chain_depth, tree.max_depth)

        if episode.row_id is None:
            if _duration(episode, moment) >= MIN_EPISODE_SECONDS:
                episode.row_id = await _insert(session, episode, moment)
        else:
            # SÜREN OLAYIN SATIRI DA GÜNCELLENİYOR. Yalnızca açılışta yazıp kapanışta
            # güncellemek, olay devam ederken zirve değerlerin ilk okumada donması demekti:
            # 9 oturumu bekleten bir olay, açılışta 2 görülmüşse raporda 2 kalırdı. Worker
            # olay sürerken çökerse de son bilinen değerler diskte kalmış olur.
            await _refresh(session, episode, moment)

    # Görünmeyen olaylar
    for key, episode in list(_open_episodes.items()):
        if key[0] != instance_id or episode.root_pid in seen_pids:
            continue
        episode.missing_rounds += 1
        if episode.missing_rounds < MISSING_ROUNDS_BEFORE_CLOSE:
            continue
        await _close(session, episode)
        _open_episodes.pop(key, None)
        counts["closed"] += 1

    return counts


def _estimate_start(root, moment: datetime) -> datetime:
    """Olayın başlangıcı. Kök engelleyicinin transaction'ı ne kadar süredir açıksa o kadar
    geriye gidiliyor — bekletme, kilidin alındığı anda başlar, bizim baktığımız anda değil.

    Bu bir YAKLAŞIMDIR ve olduğundan uzun çıkabilir (transaction açık olup henüz kilit
    almamış olabilir). Yine de "ilk gördüğümüz an" demekten çok daha doğru: örnekleme
    aralığı 10 saniye olduğu için o yaklaşım olayları sistematik olarak kısa gösterirdi.
    """
    seconds = root.transaction_seconds or root.query_seconds
    if seconds and seconds > 0:
        try:
            return moment.fromtimestamp(moment.timestamp() - float(seconds), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return moment
    return moment


def _duration(episode: _OpenEpisode, moment: datetime) -> float:
    return max(0.0, (moment - episode.started_at).total_seconds())


async def _insert(session: AsyncSession, episode: _OpenEpisode, moment: datetime) -> int:
    row = BlockingEpisode(
        instance_id=episode.instance_id,
        started_at=episode.started_at,
        ended_at=None,
        duration_seconds=_duration(episode, moment),
        root_pid=episode.root_pid,
        root_query=episode.root_query[:4000],
        root_username=episode.root_username,
        root_application=episode.root_application,
        root_was_idle=episode.root_was_idle,
        max_blocked_sessions=episode.max_blocked_sessions,
        max_chain_depth=episode.max_chain_depth,
        lock_type=episode.lock_type,
        lock_object=episode.lock_object,
    )
    session.add(row)
    await session.flush()
    return row.id


async def _refresh(session: AsyncSession, episode: _OpenEpisode, moment: datetime) -> None:
    """Süren olayın kaydını günceller (kapanmadan)."""
    row = await session.get(BlockingEpisode, episode.row_id)
    if row is None:
        return
    row.duration_seconds = _duration(episode, moment)
    row.max_blocked_sessions = episode.max_blocked_sessions
    row.max_chain_depth = episode.max_chain_depth
    row.root_was_idle = episode.root_was_idle
    if episode.root_query and not row.root_query:
        row.root_query = episode.root_query[:4000]


async def _close(session: AsyncSession, episode: _OpenEpisode) -> None:
    """Olayı kapatır. Kaydı yoksa (kısa sürdüğü için hiç yazılmadıysa) bir şey yapmaz."""
    if episode.row_id is None:
        return
    row = await session.get(BlockingEpisode, episode.row_id)
    if row is None:
        return
    row.ended_at = episode.last_seen_at
    row.duration_seconds = _duration(episode, episode.last_seen_at)
    row.max_blocked_sessions = episode.max_blocked_sessions
    row.max_chain_depth = episode.max_chain_depth
    row.root_was_idle = episode.root_was_idle
    if episode.root_query:
        row.root_query = episode.root_query[:4000]


async def close_all(session: AsyncSession) -> int:
    """Worker kapanırken açık olayları kapatır — aksi halde `ended_at` sonsuza kadar NULL
    kalır ve olay "hâlâ sürüyor" gibi görünür."""
    closed = 0
    for key, episode in list(_open_episodes.items()):
        await _close(session, episode)
        _open_episodes.pop(key, None)
        closed += 1
    return closed


async def recent_episodes(
    session: AsyncSession,
    instance_ids: list[int],
    start: datetime,
    end: datetime,
    limit: int = 50,
) -> list[BlockingEpisode]:
    if not instance_ids:
        return []
    rows = (
        await session.execute(
            select(BlockingEpisode)
            .where(
                BlockingEpisode.instance_id.in_(instance_ids),
                BlockingEpisode.started_at >= start,
                BlockingEpisode.started_at <= end,
            )
            .order_by(BlockingEpisode.max_blocked_sessions.desc(), BlockingEpisode.started_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)
