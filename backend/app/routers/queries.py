from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import ConnectionTarget, classify_connection_error
from app.database import get_db
from app.models import Instance, Node, Server, SlowQuerySample
from app.schemas import (
    ExplainOut,
    ExplainRequest,
    IndexAdviceOut,
    IndexAdviceReportOut,
    IndexAdviceRequest,
    NoAdviceReasonOut,
    QueryDiagnosisOut,
    QueryDiagnosticsReportOut,
    QueryHistoryListOut,
    QueryHistorySeriesOut,
    SlowQueryAvailabilityOut,
    SlowQueryOut,
)
from app.services import query_cache
from app.services.credentials import decrypt_secret
from app.services.explain_service import PostgreSQLExplainService
from app.services.index_advisor import PostgreSQLIndexAdvisor
from app.services.query_diagnostics import diagnose_queries
from app.services.query_history import build_query_series, group_rows_by_queryid, summarize_history
from app.services.slow_query_status import get_slow_query_availability

router = APIRouter(prefix="/queries", tags=["queries"])

# Both EXPLAIN and index advice are on-demand, user-triggered probes against the live target
# instance (EXPLAIN ANALYZE executes the query; advice does catalog scans + sometimes a hypopg
# re-plan) — cached for a few minutes so re-opening the same panel (tab switch, re-render,
# accidental double click) doesn't repeat the expensive/executing work. See services/query_cache.py.
_EXPLAIN_CACHE_TTL_SECONDS = 300.0
_ADVICE_CACHE_TTL_SECONDS = 300.0


@router.get("/{instance_id}/history", response_model=QueryHistoryListOut)
async def get_query_history(
    instance_id: int,
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> QueryHistoryListOut:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    since = datetime.now(UTC) - timedelta(hours=hours)
    result = await db.execute(
        select(SlowQuerySample)
        .where(
            SlowQuerySample.instance_id == instance_id,
            SlowQuerySample.collected_at >= since,
            SlowQuerySample.queryid.is_not(None),
        )
        .order_by(SlowQuerySample.collected_at.asc())
    )
    rows = list(result.scalars().all())
    grouped = group_rows_by_queryid(rows)

    scored: list[tuple[float, QueryHistorySeriesOut]] = []
    for qid, qrows in grouped.items():
        series = build_query_series(qrows)
        summary = summarize_history(qid, qrows[-1].query if qrows else "", series)
        item = QueryHistorySeriesOut.model_validate(summary)
        # Rank by recent total time impact
        score = float(qrows[-1].total_time_ms or 0) if qrows else 0.0
        scored.append((score, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    return QueryHistoryListOut(hours=hours, series=[item for _, item in scored[:limit]])


@router.get("/{instance_id}/history/{queryid}", response_model=QueryHistorySeriesOut)
async def get_query_history_detail(
    instance_id: int,
    queryid: str,
    hours: int = Query(default=24, ge=1, le=168),
    db: AsyncSession = Depends(get_db),
) -> QueryHistorySeriesOut:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    since = datetime.now(UTC) - timedelta(hours=hours)
    result = await db.execute(
        select(SlowQuerySample)
        .where(
            SlowQuerySample.instance_id == instance_id,
            SlowQuerySample.queryid == queryid,
            SlowQuerySample.collected_at >= since,
        )
        .order_by(SlowQuerySample.collected_at.asc())
    )
    rows = list(result.scalars().all())
    if not rows:
        raise HTTPException(status_code=404, detail="No history for this queryid")
    series = build_query_series(rows)
    return QueryHistorySeriesOut.model_validate(summarize_history(queryid, rows[-1].query, series))


@router.get("/{instance_id}/availability", response_model=SlowQueryAvailabilityOut)
async def get_slow_query_availability_endpoint(
    instance_id: int, db: AsyncSession = Depends(get_db)
) -> SlowQueryAvailabilityOut:
    """Yavaş sorgu listesi boşsa NEDEN boş — ön koşul paneliyle aynı probe'dan türetilir.

    Yol "/{instance_id}" catch-all'ından ÖNCE tanımlı olmalı (FastAPI ilk eşleşen yolu seçer).
    """
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    report = await get_slow_query_availability(db, instance)
    return SlowQueryAvailabilityOut(**vars(report))


_SLOW_QUERY_SORT_KEYS = {
    "total": lambda r: r.total_time_ms,
    "mean": lambda r: r.mean_time_ms,
    "calls": lambda r: r.calls,
}

# Aralık modunda bu eşiğin altındaki sorgular listeye alınmaz: pencerede toplam 1 ms'den az
# harcamış bir sorgu "en sorunlu N" listesinde yer kaplamamalı (Faz 16-B İŞ 4 — kullanıcı
# listede sorunlu olmayan sorgular görüyordu).
_NEGLIGIBLE_TOTAL_MS = 1.0


def _window_delta(rows: list[SlowQuerySample]) -> SlowQuerySample:
    """Bir queryid'nin pencere içindeki DEĞİŞİMİ — kümülatif sayaçlardan fark alınarak.

    pg_stat_statements sayaçları sıfırlanana kadar birikir; "14:00–14:30 arasında en çok süre
    harcayan sorgu" sorusunun cevabı son değerin kendisi değil, pencerenin başı ile sonu
    arasındaki farktır. Sayaç pencerede sıfırlanmışsa (son < ilk) son değer olduğu gibi alınır.
    """
    first, last = rows[0], rows[-1]
    reset = last.total_time_ms < first.total_time_ms or last.calls < first.calls
    total = last.total_time_ms if reset else last.total_time_ms - first.total_time_ms
    calls = last.calls if reset else last.calls - first.calls
    delta = SlowQuerySample(
        instance_id=last.instance_id,
        queryid=last.queryid,
        query=last.query,
        calls=calls,
        total_time_ms=total,
        mean_time_ms=(total / calls) if calls > 0 else last.mean_time_ms,
        rows=last.rows,
        shared_blks_hit=last.shared_blks_hit,
        shared_blks_read=last.shared_blks_read,
        local_blks_hit=last.local_blks_hit,
        local_blks_read=last.local_blks_read,
        temp_blks_read=last.temp_blks_read,
        temp_blks_written=last.temp_blks_written,
        plan_user_time=last.plan_user_time,
        plan_sys_time=last.plan_sys_time,
        exec_user_time=last.exec_user_time,
        exec_sys_time=last.exec_sys_time,
    )
    # Bu nesne veritabanına EKLENMİYOR; sadece yanıt şemasını doldurmak için var. id/collected_at
    # son gerçek örnekten geliyor ki frontend EXPLAIN/index önerisi için onu kullanabilsin.
    delta.id = last.id
    delta.collected_at = last.collected_at
    return delta


@router.get("/{instance_id}", response_model=list[SlowQueryOut])
async def get_slow_queries(
    instance_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    sort: str = Query(default="mean", pattern="^(total|mean|calls)$"),
    start: datetime | None = Query(default=None, description="Aralık başlangıcı (ISO-8601)"),
    end: datetime | None = Query(default=None, description="Aralık bitişi (ISO-8601)"),
    db: AsyncSession = Depends(get_db),
) -> list[SlowQuerySample]:
    """En sorunlu N sorgu (Faz 16-B İŞ 4).

    İki mod:

    * **Aralık verilmezse** — en son toplama döngüsünün anlık görüntüsü, seçilen ölçüte göre
      sıralanıp ilk N. (Eski davranış; sadece sıralama ve N artık parametre.)
    * **`start`/`end` verilirse** — pencere içindeki DEĞİŞİM. Kümülatif sayaçlardan fark
      alınır, böylece "bu aralıkta en çok süre harcayan sorgu" gerçekten o aralığın sorusu
      olur; pencerede kayda değer iş yapmamış sorgular listeden düşer.
    """
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    key = _SLOW_QUERY_SORT_KEYS[sort]

    if start or end:
        conditions = [SlowQuerySample.instance_id == instance_id]
        if start:
            conditions.append(SlowQuerySample.collected_at >= _as_utc(start))
        if end:
            conditions.append(SlowQuerySample.collected_at <= _as_utc(end))
        rows = list(
            (
                await db.execute(
                    select(SlowQuerySample).where(*conditions).order_by(SlowQuerySample.collected_at.asc())
                )
            )
            .scalars()
            .all()
        )
        by_queryid: dict[str, list[SlowQuerySample]] = {}
        for row in rows:
            # queryid yoksa (çok eski örnekler) sorgu metni parmak izi olarak kullanılır.
            by_queryid.setdefault(row.queryid or row.query, []).append(row)
        deltas = [_window_delta(group) for group in by_queryid.values()]
        deltas = [d for d in deltas if d.total_time_ms >= _NEGLIGIBLE_TOTAL_MS]
        deltas.sort(key=key, reverse=True)
        return deltas[:limit]

    latest_at = (
        await db.execute(
            select(SlowQuerySample.collected_at)
            .where(SlowQuerySample.instance_id == instance_id)
            .order_by(SlowQuerySample.collected_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not latest_at:
        return []

    rows = list(
        (
            await db.execute(
                select(SlowQuerySample).where(
                    SlowQuerySample.instance_id == instance_id,
                    SlowQuerySample.collected_at == latest_at,
                )
            )
        )
        .scalars()
        .all()
    )
    rows.sort(key=key, reverse=True)
    return rows[:limit]


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@router.get("/{instance_id}/diagnostics", response_model=QueryDiagnosticsReportOut)
async def get_query_diagnostics(
    instance_id: int,
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> QueryDiagnosticsReportOut:
    """Faz 16 İŞ 3: Top-N yavaş sorguyu kaynak türüne göre sınıflandırır (I/O, CPU, bellek,
    kilit/bekleme) — en son toplanan snapshot'tan, ek bir canlı sorgu çalıştırmadan."""
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")

    subq = (
        select(SlowQuerySample.collected_at)
        .where(SlowQuerySample.instance_id == instance_id)
        .order_by(SlowQuerySample.collected_at.desc())
        .limit(1)
    )
    latest_at = (await db.execute(subq)).scalar_one_or_none()
    rows: list[SlowQuerySample] = []
    if latest_at:
        result = await db.execute(
            select(SlowQuerySample)
            .where(
                SlowQuerySample.instance_id == instance_id,
                SlowQuerySample.collected_at == latest_at,
            )
            .order_by(SlowQuerySample.total_time_ms.desc())
            .limit(limit)
        )
        rows = list(result.scalars().all())

    diagnoses = diagnose_queries(rows)
    by_resource: dict[str, int] = {}
    for d in diagnoses:
        by_resource[d.resource] = by_resource.get(d.resource, 0) + 1

    # Host-agent bugün sadece servis durumu + log tail'i sağlıyor (v1/services, v1/logs) — CPU/
    # RAM/disk kullanımı toplayan bir uç yok, bu yüzden "sorun sorgu mu kaynak mı" ayrımını burada
    # dürüstçe yapamıyoruz. Uydurmak yerine bunu açıkça söylüyoruz (bkz. SORULAR.md).
    agent_configured = bool(
        (
            await db.execute(
                select(Node.id)
                .join(Server, Node.server_id == Server.id)
                .where(Node.instance_id == instance_id, Server.agent_url.is_not(None))
            )
        ).first()
    )
    if agent_configured:
        server_resource_note = (
            "Bu instance için host-agent yapılandırılmış ama agent protokolü şu an CPU/RAM/disk "
            "kullanımı toplamıyor (sadece servis durumu ve log erişimi var) — bu yüzden bir "
            "sorunun sorgu optimizasyonuyla mı yoksa sunucu kaynağı artırımıyla mı çözüleceği "
            "burada ayırt edilemiyor. Aşağıdaki sınıflandırma sadece sorgunun kendi metriklerine "
            "(I/O, CPU zamanı, geçici dosya) dayanıyor."
        )
    else:
        server_resource_note = (
            "Bu instance için host-agent yapılandırılmamış — sunucu seviyesi CPU/RAM/disk "
            "kullanımı hiç görülemiyor. Ağır sorgu yükünün sunucu kaynağı yetersizliğinden mi "
            "yoksa optimize edilebilir bir sorgudan mı kaynaklandığını ayırt etmek için Server "
            "ayarlarından bir host-agent tanımlayın."
        )

    return QueryDiagnosticsReportOut(
        generated_at=datetime.now(UTC),
        limit=limit,
        diagnoses=[QueryDiagnosisOut(**vars(d)) for d in diagnoses],
        by_resource=by_resource,
        agent_configured=agent_configured,
        server_resource_note=server_resource_note,
    )


@router.post("/{instance_id}/explain", response_model=ExplainOut)
async def explain_query(
    instance_id: int,
    body: ExplainRequest,
    db: AsyncSession = Depends(get_db),
) -> ExplainOut:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    if instance.engine != "postgresql":
        raise HTTPException(status_code=400, detail="EXPLAIN is only available for PostgreSQL")

    cache_key = ("explain", instance_id, bool(body.analyze), body.query.strip())
    cached = query_cache.get(cache_key)
    if cached is not None:
        return cached

    target = ConnectionTarget(
        host=instance.host,
        port=instance.port,
        database=instance.database,
        username=instance.username,
        password=decrypt_secret(instance.password),
        options=instance.options,
    )
    service = PostgreSQLExplainService(target)
    try:
        result = await service.explain(body.query, analyze=body.analyze)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=classify_connection_error(exc)) from exc
    out = ExplainOut.model_validate(PostgreSQLExplainService.to_payload(result))
    query_cache.set(cache_key, out, ttl_seconds=_EXPLAIN_CACHE_TTL_SECONDS)
    return out


@router.post("/{instance_id}/advice", response_model=IndexAdviceReportOut)
async def advise_indexes(
    instance_id: int,
    body: IndexAdviceRequest,
    db: AsyncSession = Depends(get_db),
) -> IndexAdviceReportOut:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance not found")
    if instance.engine != "postgresql":
        raise HTTPException(status_code=400, detail="Index advice is only available for PostgreSQL")

    cache_key = ("advice", instance_id, body.query.strip(), body.calls)
    cached = query_cache.get(cache_key)
    if cached is not None:
        return cached

    target = ConnectionTarget(
        host=instance.host,
        port=instance.port,
        database=instance.database,
        username=instance.username,
        password=decrypt_secret(instance.password),
        options=instance.options,
    )
    advisor = PostgreSQLIndexAdvisor(target)
    try:
        recommendations, reasons = await advisor.advise(body.query, calls=body.calls)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=classify_connection_error(exc)) from exc
    report = IndexAdviceReportOut(
        advice=[
            IndexAdviceOut(
                table_name=r.table_name,
                schema_name=r.schema_name,
                columns=r.columns,
                index_ddl=r.index_ddl,
                reason=r.reason,
                estimated_improvement_pct=r.estimated_improvement_pct,
                has_hypopg_estimate=r.has_hypopg_estimate,
                before_cost=r.before_cost,
                after_cost=r.after_cost,
                existing_indexes=r.existing_indexes,
            )
            for r in recommendations
        ],
        no_advice_reasons=[
            NoAdviceReasonOut(code=r.code, message=r.message, what_to_do=r.what_to_do) for r in reasons
        ],
    )
    query_cache.set(cache_key, report, ttl_seconds=_ADVICE_CACHE_TTL_SECONDS)
    return report
