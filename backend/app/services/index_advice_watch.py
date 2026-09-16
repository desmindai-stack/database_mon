"""Index önerisinin TEK giriş noktası ve çağrı eşiği izleme listesi (Faz 31 İŞ 1c).

Üç yer öneri üretiyor ve üçü de buradan geçiyor — ayrı hesaplama ayrı sonuç demek:

* `POST /api/queries/{id}/advice` — tek sorgu,
* `POST /api/queries/{id}/advice/batch` — "Top sorgulara index öner" (sayılı özetle),
* `index_advice_watch_tick` — zamanlayıcı; eşiği dolan izlenen sorgular.

## Eşik neden elle tekrar denemeyi gerektirmiyor

Önceden eşik altındaki sorgu için "tekrar deneyin" deniyordu. Artık:

1. Eşik altındaki sorgu izleme listesine yazılıyor ve yanıt "şu anda 2/5 çağrı" diyor.
2. Zamanlayıcı turu çağrı sayısını `slow_query_samples`'tan okuyor (toplama döngüsünün zaten
   yazdığı veri — izlenen sunucuya ek sorgu YOK) ve eşik dolunca öneriyi üretiyor.
3. Kullanıcı sorguyu yeniden açtığında öneri orada; izleme listesi de "hazır" gösteriyor.

## Çağrı sayısının kaynağı

İstemcinin gönderdiği `calls` değerine güvenilmiyor: arayüzdeki liste PENCERE içindeki farkı
gösteriyor (son 24 saatte 2 çağrı), eşik ise sorgunun TOPLAM çağrısını soruyor. İkisini
karıştırmak, bir haftadır binlerce kez çalışan sorguyu "2/5" diye izlemeye alırdı. Sunucu
son toplanan kümülatif değeri kullanıyor; bulunamazsa istemcinin değerine düşüyor.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal
from app.domain.engines import DatabaseEngine
from app.models import IndexAdviceWatch, Instance, SlowQuerySample
from app.services.advice import Advice, AdviceStep, advice_to_dict
from app.services.analysis_settings import get_analysis_settings
from app.services.collection import connection_target_for
from app.services.index_advisor import (
    STATUS_BELOW_THRESHOLD,
    AdviceResult,
    IndexAdvice,
    PostgreSQLIndexAdvisor,
)
from app.services.plan_capture import fingerprint

WATCH_WAITING = "waiting"
WATCH_READY = "ready"
WATCH_FAILED = "failed"

#: Parmak izi ile eşleştirmede geriye bakılan pencere ve satır sınırı.
_CALLS_LOOKBACK = timedelta(days=7)
_CALLS_SCAN_LIMIT = 5000


# --- Tek giriş noktası ---------------------------------------------------------------------


async def run_index_advice(
    session: AsyncSession,
    instance: Instance,
    *,
    query: str,
    queryid: str | None = None,
    client_calls: int | None = None,
) -> dict[str, Any]:
    """Öneriyi üretir, eşik altındaysa izlemeye alır; API şemasına uygun sözlük döner."""
    settings = await get_analysis_settings(session)
    threshold = int(settings["index_advice_min_calls"])
    calls = await current_calls(session, instance.id, query=query, queryid=queryid)
    if calls is None:
        calls = client_calls

    advisor = PostgreSQLIndexAdvisor(connection_target_for(instance))
    result = await advisor.advise(query, calls, min_calls=threshold)

    watch = None
    if result.status == STATUS_BELOW_THRESHOLD and settings["index_advice_watch_enabled"]:
        watch = await _upsert_watch(session, instance.id, query=query, queryid=queryid,
                                    calls=calls or 0, threshold=threshold)
    elif result.status != STATUS_BELOW_THRESHOLD:
        # Eşik aşıldıysa ve bu sorgu izleniyorsa izleme tamamlandı sayılıyor: öneri şimdi
        # canlı üretildi, bekleyen kayıt kafa karıştırırdı.
        watch = await _resolve_watch(session, instance.id, query, result)

    return report_payload(result, watch=watch, watch_enabled=settings["index_advice_watch_enabled"])


async def current_calls(
    session: AsyncSession, instance_id: int, *, query: str, queryid: str | None
) -> int | None:
    """Sorgunun son toplanan KÜMÜLATİF çağrı sayısı (pg_stat_statements.calls).

    Aynı queryid için aynı toplama döngüsünde birden çok satır olabilir (pg_stat_statements
    `toplevel` ayrımı; iç içe çalıştırmalar ayrı satır). EN BÜYÜĞÜ alınıyor: toplamak,
    dbace'in kendi EXPLAIN'inin iç içe satırını da uygulama çağrısı sayardı.
    """
    since = datetime.now(UTC) - _CALLS_LOOKBACK
    if queryid:
        latest = (
            await session.execute(
                select(SlowQuerySample.collected_at)
                .where(SlowQuerySample.instance_id == instance_id, SlowQuerySample.queryid == queryid)
                .order_by(SlowQuerySample.collected_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest is not None:
            values = (
                await session.execute(
                    select(SlowQuerySample.calls).where(
                        SlowQuerySample.instance_id == instance_id,
                        SlowQuerySample.queryid == queryid,
                        SlowQuerySample.collected_at == latest,
                    )
                )
            ).scalars().all()
            return max(int(v or 0) for v in values) if values else None

    key = fingerprint(query)
    rows = (
        await session.execute(
            select(SlowQuerySample.query, SlowQuerySample.calls, SlowQuerySample.collected_at)
            .where(SlowQuerySample.instance_id == instance_id, SlowQuerySample.collected_at >= since)
            .order_by(SlowQuerySample.collected_at.desc())
            .limit(_CALLS_SCAN_LIMIT)
        )
    ).all()
    best: tuple[datetime, int] | None = None
    for text, calls, collected_at in rows:
        if fingerprint(text) != key:
            continue
        if best is None or collected_at > best[0] or (collected_at == best[0] and int(calls or 0) > best[1]):
            best = (collected_at, int(calls or 0))
    return best[1] if best else None


# --- İzleme listesi ------------------------------------------------------------------------


async def _upsert_watch(session, instance_id, *, query, queryid, calls, threshold) -> IndexAdviceWatch:
    key = fingerprint(query)
    watch = (
        await session.execute(
            select(IndexAdviceWatch).where(
                IndexAdviceWatch.instance_id == instance_id, IndexAdviceWatch.query_fingerprint == key
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if watch is None:
        watch = IndexAdviceWatch(
            instance_id=instance_id,
            queryid=queryid,
            query_fingerprint=key,
            query_text=query,
            threshold=threshold,
            calls_at_registration=calls,
            calls_seen=calls,
            status=WATCH_WAITING,
            registered_at=now,
            last_checked_at=now,
        )
        session.add(watch)
    else:
        watch.calls_seen = calls
        watch.threshold = threshold
        watch.last_checked_at = now
        watch.queryid = watch.queryid or queryid
        if watch.status != WATCH_WAITING:
            watch.status = WATCH_WAITING
            watch.advice_json = None
            watch.ready_at = None
    await session.commit()
    await session.refresh(watch)
    return watch


async def _resolve_watch(session, instance_id, query, result: AdviceResult) -> IndexAdviceWatch | None:
    watch = (
        await session.execute(
            select(IndexAdviceWatch).where(
                IndexAdviceWatch.instance_id == instance_id,
                IndexAdviceWatch.query_fingerprint == fingerprint(query),
            )
        )
    ).scalar_one_or_none()
    if watch is None or watch.status == WATCH_READY:
        return watch
    now = datetime.now(UTC)
    watch.status = WATCH_READY
    watch.ready_at = now
    watch.last_checked_at = now
    watch.last_error = None
    watch.advice_json = report_payload(result, watch=None, watch_enabled=True)
    await session.commit()
    await session.refresh(watch)
    return watch


async def list_watches(session: AsyncSession, instance_id: int) -> list[dict[str, Any]]:
    settings = await get_analysis_settings(session)
    rows = (
        await session.execute(
            select(IndexAdviceWatch)
            .where(IndexAdviceWatch.instance_id == instance_id)
            .order_by(IndexAdviceWatch.status.desc(), IndexAdviceWatch.registered_at.desc())
        )
    ).scalars().all()
    return [_watch_payload(w, current_threshold=int(settings["index_advice_min_calls"])) for w in rows]


def _watch_payload(watch: IndexAdviceWatch, *, current_threshold: int | None = None) -> dict[str, Any]:
    threshold = current_threshold or watch.threshold
    return {
        "id": watch.id,
        "queryid": watch.queryid,
        "query": watch.query_text,
        "status": watch.status,
        "calls_seen": watch.calls_seen,
        "threshold": threshold,
        "registered_at": watch.registered_at,
        "last_checked_at": watch.last_checked_at,
        "ready_at": watch.ready_at,
        "last_error": watch.last_error,
        "report": watch.advice_json,
    }


async def index_advice_watch_tick() -> dict[str, int]:
    """Zamanlayıcı turu: eşiği dolan izlenen sorgular için öneri üretir.

    Çağrı sayısı `slow_query_samples`'tan okunuyor — izlenen sunucuya yalnızca eşiği DOLAN
    sorgu için (öneri üretmek üzere) bağlanılıyor.
    """
    totals = {"checked": 0, "ready": 0, "still_waiting": 0, "failed": 0, "unparsable": 0}
    async with SessionLocal() as session:
        settings = await get_analysis_settings(session)
        if not settings["index_advice_watch_enabled"]:
            return totals
        threshold = int(settings["index_advice_min_calls"])
        watches = (
            await session.execute(
                select(IndexAdviceWatch, Instance)
                .join(Instance, Instance.id == IndexAdviceWatch.instance_id)
                .where(
                    IndexAdviceWatch.status.in_([WATCH_WAITING, WATCH_FAILED]),
                    Instance.enabled.is_(True),
                    Instance.engine == str(DatabaseEngine.POSTGRESQL),
                )
            )
        ).all()

        for watch, instance in watches:
            totals["checked"] += 1
            now = datetime.now(UTC)
            calls = await current_calls(session, instance.id, query=watch.query_text, queryid=watch.queryid)
            watch.last_checked_at = now
            if calls is not None:
                watch.calls_seen = calls
            if calls is None or calls < threshold:
                totals["still_waiting"] += 1
                continue
            try:
                advisor = PostgreSQLIndexAdvisor(connection_target_for(instance))
                result = await advisor.advise(watch.query_text, calls, min_calls=threshold)
            except Exception as exc:  # noqa: BLE001 — bağlantı hatası; bir sonraki turda tekrar
                watch.status = WATCH_FAILED
                watch.last_error = f"Öneri üretilemedi: {exc}"[:2000]
                totals["failed"] += 1
                continue
            if result.status == "unparsable":
                totals["unparsable"] += 1
            watch.status = WATCH_READY
            watch.ready_at = now
            watch.last_error = None
            watch.advice_json = report_payload(result, watch=None, watch_enabled=True)
            totals["ready"] += 1
        await session.commit()
    return totals


# --- Rapor yapısı --------------------------------------------------------------------------


def report_payload(result: AdviceResult, *, watch: IndexAdviceWatch | None, watch_enabled: bool) -> dict[str, Any]:
    threshold = None
    if result.threshold is not None:
        threshold = {
            "calls_now": result.calls_now or 0,
            "threshold": result.threshold,
            "watching": watch is not None and watch.status == WATCH_WAITING,
            "watch_enabled": watch_enabled,
            "watch_id": watch.id if watch is not None else None,
        }
    return {
        "status": result.status,
        "advice": [_advice_payload(r) for r in result.recommendations],
        "no_advice_reasons": [
            {"code": r.code, "message": r.message, "what_to_do": r.what_to_do} for r in result.reasons
        ],
        "predicates": [_predicate_payload(p) for p in result.predicates],
        "threshold": threshold,
        "watch": _watch_payload(watch) | {"report": None} if watch is not None else None,
    }


def _predicate_payload(pred) -> dict[str, Any]:
    return {
        "column": pred.column,
        "table": f"{pred.source.schema}.{pred.source.table}" if pred.source else None,
        "candidates": [f"{c.schema}.{c.table}" for c in pred.candidates],
        "kind": pred.kind,
        "clause": pred.clause,
        "context": pred.context,
        "expression": pred.expression,
        "text": pred.text,
        "usable": pred.usable,
        "unusable_reason": pred.unusable_reason,
    }


def _advice_payload(r: IndexAdvice) -> dict[str, Any]:
    return {
        "table_name": r.table_name,
        "schema_name": r.schema_name,
        "columns": r.columns,
        "index_ddl": r.index_ddl,
        "reason": r.reason,
        "estimated_improvement_pct": r.estimated_improvement_pct,
        "has_hypopg_estimate": r.has_hypopg_estimate,
        "before_cost": r.before_cost,
        "after_cost": r.after_cost,
        "existing_indexes": r.existing_indexes,
        "index_kind": r.index_kind,
        "measurement_notes": r.measurement_notes,
        "advice": _standard_advice(r),
    }


def _standard_advice(recommendation: IndexAdvice) -> dict:
    """Index önerisini standart öneri yapısına çevirir (Faz 17 Ek İŞ B).

    CREATE INDEX CONCURRENTLY bilinçli tercih: tabloyu yazmaya kapatmaz. Buna karşılık kendi
    riskleri var (yarıda kalırsa INVALID index bırakır, iki kopya birden diskte durur) ve bu
    riskler "Dikkat" başlığında yazılı — komutu uyarısız vermek sorumsuzluk olurdu.
    """
    target = f"{recommendation.schema_name}.{recommendation.table_name}"
    columns = ", ".join(recommendation.columns)
    concurrent_ddl = recommendation.index_ddl.replace("CREATE INDEX ", "CREATE INDEX CONCURRENTLY ", 1)
    index_name = _index_name(recommendation)
    cautions = [
        "CREATE INDEX CONCURRENTLY işlem bloğu içinde çalıştırılamaz ve normalinden uzun sürer.",
        "Yarıda kalırsa geride INVALID bir index kalır; DROP INDEX ile temizlenmelidir.",
        "İşlem sırasında index'in diskte yer kaplayacağını hesaba katın.",
    ]
    if recommendation.index_kind == "expression":
        cautions.append(
            "İfade index'i yalnızca sorgu AYNI ifadeyi kullandığında devreye girer; uygulama "
            "sorguyu farklı yazarsa (ör. lower yerine upper) index kullanılmaz."
        )
    if recommendation.index_kind == "trigram":
        cautions.append("GIN index yazma işlemlerini B-tree'den belirgin biçimde yavaşlatır.")
    cautions.extend(recommendation.measurement_notes)
    return advice_to_dict(
        Advice(
            title=f"{target} tablosuna ({columns}) index ekleyin",
            why=(
                f"{recommendation.reason} Index olmadan bu sorgu tabloyu baştan sona tarıyor; "
                "veri büyüdükçe süre doğrusal olarak artar ve yoğun saatlerde uygulama yavaşlar."
            ),
            steps=[
                AdviceStep(
                    "Aynı kolonları kapsayan bir index zaten var mı, doğrulayın.",
                    f"SELECT indexname, indexdef FROM pg_indexes\n"
                    f"WHERE schemaname = '{recommendation.schema_name}' "
                    f"AND tablename = '{recommendation.table_name}';",
                ),
                AdviceStep("Index'i tabloyu kilitlemeden oluşturun.", concurrent_ddl),
                AdviceStep(
                    "İstatistikleri tazeleyin ki planlayıcı yeni index'i hemen kullanabilsin.",
                    f"ANALYZE {target};",
                ),
            ],
            cautions=cautions,
            estimated_duration="Tablo boyutuna göre dakikalar; büyük tablolarda saatler sürebilir.",
            rollback=f"DROP INDEX CONCURRENTLY IF EXISTS {recommendation.schema_name}.{index_name};",
            verification=(
                f"EXPLAIN (ANALYZE, BUFFERS) <sorgunuz>;\n"
                f"-- Planda Seq Scan yerine Index Scan görmelisiniz.\n"
                f"SELECT idx_scan FROM pg_stat_user_indexes\n"
                f"WHERE schemaname = '{recommendation.schema_name}' "
                f"AND indexrelname = '{index_name}';"
            ),
        )
    )


def _index_name(recommendation: IndexAdvice) -> str:
    parts = recommendation.index_ddl.split()
    try:
        return parts[parts.index("INDEX") + 1]
    except (ValueError, IndexError):
        return "<index_adi>"


# --- Toplu çalıştırma ----------------------------------------------------------------------

_SUMMARY_LABELS = (
    ("advised", "için öneri üretildi"),
    ("below_threshold", "eşik altında, izlemeye alındı"),
    ("no_advice", "için öneri çıkmadı (sebepleri sorgu satırında)"),
    ("system", "sistem sorgusu, analize alınmadı"),
    ("unparsable", "çözümlenemedi"),
    ("truncated", "metni kesik"),
    ("failed", "için bağlantı/ölçüm hatası"),
    ("empty", "boş"),
)


def summarize(statuses: list[str]) -> dict[str, Any]:
    counts = {key: statuses.count(key) for key, _ in _SUMMARY_LABELS}
    total = len(statuses)
    parts = [f"{counts[key]}'{_suffix(counts[key])} {label}" for key, label in _SUMMARY_LABELS if counts[key]]
    text = f"{total} sorgudan " + "; ".join(parts) + "." if parts else f"{total} sorgu incelendi."
    return {"total": total, "counts": counts, "text": text}


def _suffix(number: int) -> str:
    """Sayıdan sonra kesme işaretiyle gelen belirtme/iyelik eki: 1'i, 2'si, 6'sı, 10'u, 100'ü.

    Ek, sayının OKUNUŞUNDAKİ son kelimeye göre seçiliyor (bir, iki… on, yirmi… yüz, bin).
    """
    if number == 0:
        return "ı"
    ones = {1: "i", 2: "si", 3: "ü", 4: "ü", 5: "i", 6: "sı", 7: "si", 8: "i", 9: "u"}
    tens = {1: "u", 2: "si", 3: "u", 4: "ı", 5: "si", 6: "ı", 7: "i", 8: "i", 9: "ı"}
    if number % 10:
        return ones[number % 10]
    if number % 100:
        return tens[(number % 100) // 10]
    if number % 1000:
        return "ü"  # yüz
    return "i"  # bin
