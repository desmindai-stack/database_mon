from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import ConnectionTarget, classify_connection_error
from app.database import get_db
from app.pagination import Page, page_params
from app.domain.query_metrics import derive_metrics, flag_metrics, metric_dictionary
from app.models import CapturedPlan, IndexAdviceOutcome, Instance, Node, Server, SlowQuerySample, WaitQuerySignature
from app.services.collection import connection_target_for
from app.schemas import (
    IndexAdviceOutcomeOut,
    MonitoringRoleOut,
    MetricMeaningOut,
    CapturedPlanListOut,
    CapturedPlanOut,
    ExplainOut,
    ExplainRequest,
    PlanSourcesOut,
    IndexAdviceBatchItemOut,
    IndexAdviceBatchOut,
    IndexAdviceBatchRequest,
    IndexAdviceBatchSummaryOut,
    IndexAdviceReportOut,
    IndexAdviceRequest,
    IndexAdviceWatchListItemOut,
    QueryDiagnosisOut,
    QueryDiagnosticsReportOut,
    QueryHistoryListOut,
    QueryHistorySeriesOut,
    SlowQueryAvailabilityOut,
    PlanRegressionOut,
    PlanRegressionReportOut,
    QueryStorePlanOut,
    SlowQueryListOut,
    SlowQueryOut,
)
from app.services import query_cache
from app.services.credentials import decrypt_secret
from app.services.auto_explain import MANAGED_SERVICE_GUIDANCE, plan_source_label
from app.services.explain_service import PostgreSQLExplainService, _parse_node, _plan_to_dict
from app.services.plan_analysis import (
    advice_for_analysis,
    analysis_to_dict,
    analyze_plan,
    annotate_plan_dict,
)
from app.services.advice import advice_to_dict
from app.services.analysis_settings import get_analysis_settings
from app.services.index_advice_outcome import outcome_payload
from app.services.index_advice_watch import list_watches, run_index_advice, summarize
from app.services.monitoring_role import monitoring_role_status
from app.services.plan_source import KIND_SAMPLE, captured_unavailable, resolve_plan_sources
from app.services.database_load import wait_profiles_by_query
from app.services.query_diagnostics import diagnose_queries
from app.services.query_history import build_query_series, group_rows_by_queryid, summarize_history
from app.services.query_store import build_report as build_query_store_report
from app.services.slow_query_selection import MARKER_CONFLICT_NOTE, default_slow_query_selection
from app.services.slow_query_status import get_slow_query_availability

router = APIRouter(prefix="/queries", tags=["queries"])

# Both EXPLAIN and index advice are on-demand, user-triggered probes against the live target
# instance (EXPLAIN ANALYZE executes the query; advice does catalog scans + sometimes a hypopg
# re-plan) — cached for a few minutes so re-opening the same panel (tab switch, re-render,
# accidental double click) doesn't repeat the expensive/executing work. See services/query_cache.py.
_EXPLAIN_CACHE_TTL_SECONDS = 300.0
_ADVICE_CACHE_TTL_SECONDS = 300.0


# SABİT yollar `/{instance_id}`'den ÖNCE kayıtlı olmalı: FastAPI rotaları kayıt sırasıyla
# eşleştiriyor ve `/{instance_id}` her tek segmenti yakalıyor. Bu uç eskiden dosyanın
# sonundaydı ve `GET /metric-dictionary` "metric-dictionary" bir instance_id sanılarak
# 422 dönüyordu — sözlüğe hiç erişilemiyordu (Faz 29 İŞ 2a düzeltmesi).
@router.get("/metric-dictionary", response_model=list[MetricMeaningOut])
async def get_metric_dictionary(page: Page = Depends(page_params)) -> list[dict]:
    """Sorgu metriklerinin sözlüğü: her metrik ne ölçüyor, ne zaman sorun (Faz 29 İŞ 2a).

    Arayüz bu metinleri ELLE YAZMIYOR. Aynı açıklamanın iki yerde farklı olması, kullanıcının
    hangisine güveneceğini bilememesi demekti; eşikler de aynı modülden geliyor.

    Instance gerektirmiyor: sözlük sunucudan bağımsız.
    """
    return page.slice(metric_dictionary())


#: Geçmiş serisinin ihtiyaç duyduğu kolonlar — metin YOK (Faz 31 Commit 9).
_SERIES_COLUMNS = (
    SlowQuerySample.id,
    SlowQuerySample.queryid,
    SlowQuerySample.collected_at,
    SlowQuerySample.calls,
    SlowQuerySample.total_time_ms,
    SlowQuerySample.mean_time_ms,
    SlowQuerySample.rows,
)


async def _query_texts(db: AsyncSession, ids: list[int]) -> dict[int, str]:
    """Gösterilecek kalemlerin metni — yalnızca verilen satırlar (seri başına bir)."""
    if not ids:
        return {}
    return dict((await db.execute(select(SlowQuerySample.id, SlowQuerySample.query).where(SlowQuerySample.id.in_(ids)))).all())


@router.get("/{instance_id}/history", response_model=QueryHistoryListOut)
async def get_query_history(
    instance_id: int,
    hours: int = Query(default=24, ge=1, le=168),
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> QueryHistoryListOut:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance bulunamadi")

    since = datetime.now(UTC) - timedelta(hours=hours)
    # Faz 31 Commit 9 (egress): sıralama SQL'de (penceredeki SON örneğin toplam süresi), seri yalnızca ilk
    # `limit` sorgu için ve METİNSİZ; metin seri başına bir satır. Eskiden penceredeki her örnek tam kolon.
    in_window = [
        SlowQuerySample.instance_id == instance_id,
        SlowQuerySample.collected_at >= since,
        SlowQuerySample.queryid.is_not(None),
    ]
    latest = (
        select(
            SlowQuerySample.queryid,
            SlowQuerySample.id,
            SlowQuerySample.total_time_ms,
            func.row_number().over(
                partition_by=SlowQuerySample.queryid,
                order_by=[SlowQuerySample.collected_at.desc(), SlowQuerySample.id.desc()],
            ).label("rn"),
            func.min(SlowQuerySample.id).over(partition_by=SlowQuerySample.queryid).label("first_seen"),
        )
        .where(*in_window)
        .subquery()
    )
    top = (
        await db.execute(
            select(latest.c.queryid, latest.c.id)
            .where(latest.c.rn == 1)
            .order_by(latest.c.total_time_ms.desc(), latest.c.first_seen)
            .limit(limit)
        )
    ).all()
    queryids = [qid for qid, _ in top]
    texts = await _query_texts(db, [last_id for _, last_id in top])
    series_rows = (
        await db.execute(
            select(*_SERIES_COLUMNS)
            .where(*in_window, SlowQuerySample.queryid.in_(queryids or [""]))
            .order_by(SlowQuerySample.collected_at.asc(), SlowQuerySample.id.asc())
        )
    ).all()
    grouped = group_rows_by_queryid(series_rows)
    series_out = []
    for qid, last_id in top:
        qrows = grouped.get(str(qid), [])
        summary = summarize_history(qid, texts.get(last_id, ""), build_query_series(qrows))
        series_out.append(QueryHistorySeriesOut.model_validate(summary))
    return QueryHistoryListOut(hours=hours, series=series_out)


@router.get("/{instance_id}/history/{queryid}", response_model=QueryHistorySeriesOut)
async def get_query_history_detail(
    instance_id: int,
    queryid: str,
    hours: int = Query(default=24, ge=1, le=168),
    db: AsyncSession = Depends(get_db),
) -> QueryHistorySeriesOut:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance bulunamadi")

    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = (
        await db.execute(
            select(*_SERIES_COLUMNS)
            .where(
                SlowQuerySample.instance_id == instance_id,
                SlowQuerySample.queryid == queryid,
                SlowQuerySample.collected_at >= since,
            )
            .order_by(SlowQuerySample.collected_at.asc(), SlowQuerySample.id.asc())
        )
    ).all()
    if not rows:
        # Faz 19 IS 1 — API DEGISIKLIGI: eskiden 404 donuyordu. Instance de sorgu da var
        # olabilir; yalnizca SECILEN PENCEREDE ornek yoktur (yeni eklenmis instance, uzun
        # aralikli toplama, ya da o pencerede calismamis bir sorgu). "Yok" degil "bos" —
        # istemcinin bunu silinmis bir kayittan ayirabilmesi icin bos seri donuyoruz.
        return QueryHistorySeriesOut.model_validate(summarize_history(queryid, "", []))
    series = build_query_series(rows)
    texts = await _query_texts(db, [rows[-1].id])
    return QueryHistorySeriesOut.model_validate(summarize_history(queryid, texts.get(rows[-1].id, ""), series))


@router.get("/{instance_id}/availability", response_model=SlowQueryAvailabilityOut)
async def get_slow_query_availability_endpoint(
    instance_id: int, db: AsyncSession = Depends(get_db)
) -> SlowQueryAvailabilityOut:
    """Yavaş sorgu listesi boşsa NEDEN boş — ön koşul paneliyle aynı probe'dan türetilir.

    Yol "/{instance_id}" catch-all'ından ÖNCE tanımlı olmalı (FastAPI ilk eşleşen yolu seçer).
    """
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance bulunamadi")
    report = await get_slow_query_availability(db, instance)
    return SlowQueryAvailabilityOut(**vars(report))


@router.get("/{instance_id}", response_model=SlowQueryListOut)
async def get_slow_queries(
    instance_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, description="Sayfalama: atlanacak kalem sayısı (Faz 31 Commit 9)."),
    sort: str = Query(default="total", pattern="^(total|mean|calls)$"),
    start: datetime | None = Query(default=None, description="Aralık başlangıcı (ISO-8601)"),
    end: datetime | None = Query(default=None, description="Aralık bitişi (ISO-8601)"),
    include_system: bool | None = Query(
        default=None,
        description=(
            "Sistem/platform sorgularını da göster. Verilmezse yönetim ayarındaki varsayılan "
            "kullanılır (pg_catalog, pg_stat_*, Supabase/RDS iç sorguları, dbace'in kendi sorguları)."
        ),
    ),
    db: AsyncSession = Depends(get_db),
) -> SlowQueryListOut:
    """En sorunlu N sorgu — rapor ile ORTAK seçim servisinden (Faz 18 İŞ 1).

    Aralık verilmezse varsayılan pencere son `DEFAULT_WINDOW_HOURS` saat. Eskiden aralıksız
    çağrı "yalnızca son toplama döngüsü"nü döndürüyordu; rapor ise dönemin tamamına bakıyordu
    ve bu yüzden raporda görünen bir sorgu DPA'da bulunamıyordu. Artık ikisi de aynı pencere
    mantığını kullanıyor (davranış değişikliği ILERLEME.md'de yazılı).
    """
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance bulunamadi")

    # Eşikler, sistem sorgusu görünürlüğü ve pencere TEK yerden (Faz 18 İŞ 2; Faz 31 Commit 7'de
    # Tuning içgörüsü ve teşhis paneli de aynı fonksiyona bağlandı).
    selection = await default_slow_query_selection(
        db, instance_id, start=start, end=end, sort=sort, limit=limit, offset=offset, include_system=include_system
    )
    out = selection_to_out(selection, selection.window_start, selection.window_end)
    out.total, out.offset = selection.visible_count, offset
    out.monitoring_role = MonitoringRoleOut(**monitoring_role_status(instance))
    return out


def _metric_fields(entry, total_time_all_ms: float | None) -> dict:
    """Türetilmiş göstergeler + eşiği aşanlar.

    Göstergeler PENCERE İÇİN HESAPLANMIŞ değerlerden türetiliyor (`entry`), ham örnekten
    değil: pg_stat_statements kümülatif sayar ve ham satır, sunucunun açılışından beri olan
    her şeyi taşır. Kullanıcının gördüğü pencere neyse gösterge de o olmalı.
    """
    from types import SimpleNamespace

    sample = entry.sample
    window_row = SimpleNamespace(
        calls=entry.calls,
        total_time_ms=entry.total_time_ms,
        mean_time_ms=entry.mean_time_ms,
        rows=entry.rows,
        cpu_time_ms=sample.cpu_time_ms,
        spills=sample.spills,
        grant_kb=sample.grant_kb,
        used_grant_kb=sample.used_grant_kb,
        stddev_time_ms=sample.stddev_time_ms,
        min_time_ms=sample.min_time_ms,
        max_time_ms=sample.max_time_ms,
        shared_blks_hit=sample.shared_blks_hit,
        shared_blks_read=sample.shared_blks_read,
        shared_blks_dirtied=sample.shared_blks_dirtied,
        shared_blks_written=sample.shared_blks_written,
        temp_blks_read=sample.temp_blks_read,
        temp_blks_written=sample.temp_blks_written,
        blk_read_time_ms=sample.blk_read_time_ms,
        blk_write_time_ms=sample.blk_write_time_ms,
        wal_records=sample.wal_records,
        wal_fpi=sample.wal_fpi,
        wal_bytes=sample.wal_bytes,
        plans=sample.plans,
        total_plan_time_ms=sample.total_plan_time_ms,
        jit_time_ms=sample.jit_time_ms,
    )
    derived = derive_metrics(window_row, total_time_all_ms=total_time_all_ms)
    return {"metrics": derived, "metric_flags": flag_metrics(derived)}


def selection_to_out(selection, window_start: datetime, window_end: datetime) -> SlowQueryListOut:
    """SlowQuerySelection → API şeması. Rapor derin bağlantısının hedef ucu da bunu kullanıyor."""
    # Toplam etki payı için PAYDA: bu penceredeki tüm sorguların toplam süresi. Paydayı
    # vermezsek `derive_metrics` payı hesaplamıyor (uydurmuyor) — ve pay, "hangi sorguyu
    # önce düzeltmeliyim" sorusunun tek anlamlı cevabı.
    total_time_all_ms = sum(float(e.total_time_ms or 0) for e in selection.entries) or None
    return SlowQueryListOut(
        mode=selection.mode,
        window_start=window_start,
        window_end=window_end,
        filtered_system=selection.filtered_system,
        filtered_insignificant=selection.filtered_insignificant,
        items=[
            SlowQueryOut(
                id=e.sample.id,
                instance_id=e.sample.instance_id,
                collected_at=e.sample.collected_at,
                key=e.key,
                queryid=e.queryid,
                query=e.query,
                calls=e.calls,
                total_time_ms=e.total_time_ms,
                mean_time_ms=e.mean_time_ms,
                rows=e.rows,
                shared_blks_hit=e.sample.shared_blks_hit,
                shared_blks_read=e.sample.shared_blks_read,
                local_blks_hit=e.sample.local_blks_hit,
                local_blks_read=e.sample.local_blks_read,
                temp_blks_read=e.sample.temp_blks_read,
                temp_blks_written=e.sample.temp_blks_written,
                plan_user_time=e.sample.plan_user_time,
                plan_sys_time=e.sample.plan_sys_time,
                exec_user_time=e.sample.exec_user_time,
                exec_sys_time=e.sample.exec_sys_time,
                is_system=e.is_system,
                system_reason=e.system_reason,
                marker_conflict=e.marker_conflict,
                marker_note=MARKER_CONFLICT_NOTE if e.marker_conflict else None,
                toplevel=e.sample.toplevel,
                sample_count=e.sample_count,
                stddev_time_ms=e.sample.stddev_time_ms,
                min_time_ms=e.sample.min_time_ms,
                max_time_ms=e.sample.max_time_ms,
                shared_blks_dirtied=e.sample.shared_blks_dirtied,
                shared_blks_written=e.sample.shared_blks_written,
                blk_read_time_ms=e.sample.blk_read_time_ms,
                blk_write_time_ms=e.sample.blk_write_time_ms,
                wal_records=e.sample.wal_records,
                wal_fpi=e.sample.wal_fpi,
                wal_bytes=e.sample.wal_bytes,
                plans=e.sample.plans,
                total_plan_time_ms=e.sample.total_plan_time_ms,
                jit_time_ms=e.sample.jit_time_ms,
                jit_functions=e.sample.jit_functions,
                cpu_time_ms=e.sample.cpu_time_ms,
                logical_reads=e.sample.logical_reads,
                physical_reads=e.sample.physical_reads,
                logical_writes=e.sample.logical_writes,
                spills=e.sample.spills,
                grant_kb=e.sample.grant_kb,
                used_grant_kb=e.sample.used_grant_kb,
                **_metric_fields(e, total_time_all_ms),
            )
            for e in selection.entries
        ],
    )




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
        raise HTTPException(status_code=404, detail="Instance bulunamadi")

    # Faz 31 Commit 7: yavaş sorgu LİSTESİYLE aynı seçim (pencere, sistem/imza filtresi, eşik). Eskiden
    # son anlık görüntünün ham satırlarıydı: dbace'in kendi imzalı sorguları ve katalog sorguları
    # "en sorunlu sorgu" olarak teşhis ediliyordu (gerçek veride 10 satırın 8'i). Ayrıca `collected_at ==`
    # eşitliği SQLite'ta hiç satır bulmuyordu.
    selection = await default_slow_query_selection(db, instance_id, sort="total", limit=limit)
    wait_profiles = await wait_profiles_by_query(db, instance_id)
    diagnoses = diagnose_queries([e.sample for e in selection.entries], wait_profiles)
    for diagnosis, entry in zip(diagnoses, selection.entries):
        # Sayılar listedekiyle AYNI olsun: pencere içindeki değişim, ham kümülatif değil.
        diagnosis.calls, diagnosis.mean_time_ms, diagnosis.total_time_ms = (
            entry.calls, entry.mean_time_ms, entry.total_time_ms,
        )
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
        mode=selection.mode,
        window_start=selection.window_start,
        window_end=selection.window_end,
        filtered_system=selection.filtered_system,
        filtered_insignificant=selection.filtered_insignificant,
        diagnoses=[QueryDiagnosisOut(**vars(d)) for d in diagnoses],
        by_resource=by_resource,
        agent_configured=agent_configured,
        server_resource_note=server_resource_note,
    )


@router.get("/{instance_id}/captured-plans", response_model=CapturedPlanListOut)
async def list_captured_plans(
    instance_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    queryid: str | None = Query(default=None, description="Yalnızca bu queryid'ye bağlı planlar."),
    db: AsyncSession = Depends(get_db),
) -> CapturedPlanListOut:
    """auto_explain ile GERÇEK çalıştırmadan yakalanmış planlar (Faz 26 İŞ 1).

    Boş liste dönmek yetmez: "neden hiç plan yok" sorusu burada cevaplanıyor — auto_explain
    kurulu değil mi, host-agent yok mu, yoksa henüz eşiği aşan sorgu mu olmadı.
    """
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance bulunamadı")

    conditions = [CapturedPlan.instance_id == instance_id]
    if queryid:
        conditions.append(CapturedPlan.queryid == queryid)
    rows = (
        await db.execute(
            select(CapturedPlan)
            .where(*conditions)
            .order_by(CapturedPlan.captured_at.desc())
            .limit(limit)
        )
    ).scalars().all()

    out = CapturedPlanListOut(
        instance_id=instance_id,
        plans=[
            CapturedPlanOut(
                id=row.id,
                captured_at=row.captured_at,
                source=row.source,
                source_label=plan_source_label(row.source),
                duration_ms=row.duration_ms,
                query_text=row.query_text,
                queryid=row.queryid,
                has_actual_rows=row.has_actual_rows,
            )
            for row in rows
        ],
    )
    if not out.plans:
        # Faz 31: plan kaynakları ile AYNI açıklama (services/plan_source.py).
        unavailable = captured_unavailable(instance)
        out.unavailable_reason = unavailable["reason"]
        out.unavailable_kind = unavailable["kind"]
        if instance.engine == "postgresql" and not (instance.options or {}).get("agent_url"):
            out.managed_service_guidance = MANAGED_SERVICE_GUIDANCE
    return out


@router.get("/{instance_id}/plan-sources", response_model=PlanSourcesOut)
async def plan_sources(
    instance_id: int,
    sample_id: int = Query(description="Yavaş sorgu satırının kimliği (SlowQueryOut.id)."),
    db: AsyncSession = Depends(get_db),
) -> PlanSourcesOut:
    """Bu sorgunun planı hangi kaynaklardan alınabilir — öncelik sırasıyla (Faz 31 İŞ 2).

    GET: viewer da görebiliyor. Yanıtta gerçek değerli örnek METNİ yok; yalnızca varlığı,
    zamanı ve süresi.
    """
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance bulunamadı")
    if instance.engine != "postgresql":
        raise HTTPException(status_code=400, detail="Plan kaynakları yalnızca PostgreSQL için")
    sample = await db.get(SlowQuerySample, sample_id)
    if sample is None or sample.instance_id != instance_id:
        raise HTTPException(status_code=404, detail="Sorgu bulunamadı")
    return PlanSourcesOut.model_validate(await resolve_plan_sources(db, instance, sample))


@router.get("/{instance_id}/captured-plans/{plan_id}", response_model=ExplainOut)
async def get_captured_plan(
    instance_id: int,
    plan_id: int,
    db: AsyncSession = Depends(get_db),
) -> ExplainOut:
    """Yakalanmış tek bir planın ağacı — canlı EXPLAIN ile AYNI yapıda dönüyor.

    Aynı yapı bilinçli: arayüz tek bir plan bileşeni kullanıyor. İki ayrı şekil, iki ayrı
    bileşen ve zamanla ayrışan iki görünüm demekti (projede daha önce yaşandı).
    """
    row = await db.get(CapturedPlan, plan_id)
    if not row or row.instance_id != instance_id:
        raise HTTPException(status_code=404, detail="Plan bulunamadı")

    plan_root = (row.plan_json or {}).get("Plan")
    node = _parse_node(plan_root) if isinstance(plan_root, dict) else None
    insights: list[str] = []
    if node:
        _collect_plan_insights(node, insights)
    plan_dict = _plan_to_dict(node) if node else None
    # Sapma analizi HAM plan JSON'undan: `Actual Loops` ve koşul metinleri sadeleştirilmiş
    # ağaçta yok ve analiz onlara ihtiyaç duyuyor.
    analysis = analyze_plan(row.plan_json or {})
    annotate_plan_dict(plan_dict, analysis)

    return ExplainOut(
        query=row.query_text,
        # auto_explain planı gerçek çalıştırmadan geldiği için "analyzed" ancak gerçek satır
        # sayıları da varsa doğrudur (log_analyze açık).
        analyzed=row.has_actual_rows,
        planning_time_ms=_plan_float(row.plan_json, "Planning Time"),
        execution_time_ms=_plan_float(row.plan_json, "Execution Time") or row.duration_ms,
        total_cost=node.total_cost if node else None,
        insights=insights,
        plan=plan_dict,
        raw_plan=[row.plan_json] if row.plan_json else [],
        analysis=analysis_to_dict(analysis),
        analysis_advice=advice_to_dict(advice_for_analysis(analysis)),
        source=row.source,
        source_label=plan_source_label(row.source),
        source_caveat=" ".join(
            part
            for part in (
                None
                if row.has_actual_rows
                else (
                    "Bu plan gerçek çalıştırmadan yakalandı ama auto_explain.log_analyze kapalı "
                    "olduğu için GERÇEK SATIR SAYISI yok — yalnızca planlayıcının tahmini var. "
                    "Tahmini/gerçek sapma analizi bu planda yapılamaz."
                ),
                None
                if (await get_analysis_settings(db))["store_real_query_samples"]
                else (
                    "Gerçek değerli metin saklama kapalı: plandaki sabitler ($1, $2 …) "
                    "değerlerden arındırıldı; plan yapısı ve ölçümler değişmedi."
                ),
            )
            if part
        ) or None,
        captured_at=row.captured_at,
    )


def _plan_float(plan_json: dict | None, key: str) -> float | None:
    if not isinstance(plan_json, dict):
        return None
    value = plan_json.get(key)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _collect_plan_insights(node, out: list[str]) -> None:
    for tip in node.insights:
        if tip not in out:
            out.append(tip)
    for child in node.children:
        _collect_plan_insights(child, out)


@router.post("/{instance_id}/explain", response_model=ExplainOut)
async def explain_query(
    instance_id: int,
    body: ExplainRequest,
    db: AsyncSession = Depends(get_db),
) -> ExplainOut:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance bulunamadi")
    if instance.engine != "postgresql":
        raise HTTPException(status_code=400, detail="EXPLAIN is only available for PostgreSQL")

    if body.use_sample:
        return await _explain_with_sample(db, instance, body)

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


async def _explain_with_sample(db: AsyncSession, instance: Instance, body: ExplainRequest) -> ExplainOut:
    """Gerçek değerli örnekle EXPLAIN ANALYZE (Faz 31 İŞ 2).

    Karar plan kaynakları servisinde veriliyor — bu uç aynı değerlendirmeyi tekrar yapıyor ki
    arayüz "kullanılamaz" dediği bir seçeneği doğrudan istek atarak çalıştıramasın. Yetki:
    POST olduğu için `require_write_access` viewer'ı zaten reddediyor.
    """
    if body.sample_id is None:
        raise HTTPException(status_code=400, detail="Gerçek değerli örnek için sample_id gerekli.")
    sample = await db.get(SlowQuerySample, body.sample_id)
    if sample is None or sample.instance_id != instance.id:
        raise HTTPException(status_code=404, detail="Sorgu bulunamadı")
    sources = await resolve_plan_sources(db, instance, sample)
    option = next(o for o in sources["options"] if o["kind"] == KIND_SAMPLE)
    if not option["available"]:
        raise HTTPException(status_code=400, detail=option["reason"])
    signature = (
        await db.execute(
            select(WaitQuerySignature).where(
                WaitQuerySignature.instance_id == instance.id, WaitQuerySignature.queryid == sample.queryid
            )
        )
    ).scalar_one()

    service = PostgreSQLExplainService(connection_target_for(instance))
    try:
        result = await service.explain_sample(
            signature.sample_query_text,
            captured_at=signature.sample_captured_at,
            duration_ms=signature.sample_duration_ms,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=classify_connection_error(exc)) from exc
    return ExplainOut.model_validate(PostgreSQLExplainService.to_payload(result))


# Önbelleğe yalnızca DURUMU DEĞİŞMEYECEK sonuçlar alınıyor. "Eşik altında" yanıtı
# önbelleklenseydi, zamanlayıcı öneriyi ürettikten sonra da 5 dakika "2/5" görünürdü.
_CACHEABLE_ADVICE_STATUSES = {"advised", "no_advice", "system", "unparsable", "truncated", "empty"}


async def _postgres_instance(db: AsyncSession, instance_id: int) -> Instance:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance bulunamadi")
    if instance.engine != "postgresql":
        raise HTTPException(status_code=400, detail="Index advice is only available for PostgreSQL")
    return instance


async def _advice_report(db: AsyncSession, instance: Instance, item: IndexAdviceRequest) -> IndexAdviceReportOut:
    cache_key = ("advice", instance.id, item.query.strip(), item.queryid)
    cached = query_cache.get(cache_key)
    if cached is not None:
        return cached
    payload = await run_index_advice(
        db, instance, query=item.query, queryid=item.queryid, client_calls=item.calls
    )
    report = IndexAdviceReportOut.model_validate(payload)
    if report.status in _CACHEABLE_ADVICE_STATUSES:
        query_cache.set(cache_key, report, ttl_seconds=_ADVICE_CACHE_TTL_SECONDS)
    return report


@router.post("/{instance_id}/advice", response_model=IndexAdviceReportOut)
async def advise_indexes(
    instance_id: int,
    body: IndexAdviceRequest,
    db: AsyncSession = Depends(get_db),
) -> IndexAdviceReportOut:
    instance = await _postgres_instance(db, instance_id)
    try:
        return await _advice_report(db, instance, body)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=classify_connection_error(exc)) from exc


@router.post("/{instance_id}/advice/batch", response_model=IndexAdviceBatchOut)
async def advise_indexes_batch(
    instance_id: int,
    body: IndexAdviceBatchRequest,
    db: AsyncSession = Depends(get_db),
) -> IndexAdviceBatchOut:
    """Birden çok sorgu için öneri — SAYILI özetle (Faz 31 İŞ 1b).

    "Top sorgulara index öner" eskiden istemcide tek tek çağrı yapıyordu ve çözümlenemeyen
    sorgular sessizce "öneri yok" satırına karışıyordu. Burada her sonuç durumuna göre
    sayılıyor: "3 sorgudan 1'i çözümlenemedi; 1'i için öneri üretildi; …".
    """
    instance = await _postgres_instance(db, instance_id)
    items: list[IndexAdviceBatchItemOut] = []
    statuses: list[str] = []
    for item in body.items:
        try:
            report = await _advice_report(db, instance, item)
            items.append(IndexAdviceBatchItemOut(query=item.query, queryid=item.queryid, report=report))
            statuses.append(report.status)
        except Exception as exc:  # noqa: BLE001 — bir sorgunun hatası diğerlerini düşürmemeli
            items.append(
                IndexAdviceBatchItemOut(
                    query=item.query, queryid=item.queryid, error=classify_connection_error(exc)
                )
            )
            statuses.append("failed")
    return IndexAdviceBatchOut(summary=IndexAdviceBatchSummaryOut(**summarize(statuses)), items=items)


@router.get("/{instance_id}/plan-regressions", response_model=PlanRegressionReportOut)
async def get_plan_regressions(
    instance_id: int,
    hours: int = Query(default=168, ge=1, le=720),
    page: Page = Depends(page_params),
    db: AsyncSession = Depends(get_db),
) -> PlanRegressionReportOut:
    """SQL Server plan regresyonu: aynı sorgunun ESKİ planına göre yavaşlayan YENİ planı (Faz 31 Commit 9).

    Salt-okunur: `sys.query_store_*` görünümleri VIEW DATABASE STATE ile okunuyor. Query Store kapalıysa,
    sürüm desteklemiyorsa ya da yetki yetmiyorsa sonuç "ölçülemedi" + gerekçe + gereken ayar/yetki.
    """
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance bulunamadı")
    try:
        report = await build_query_store_report(instance, hours=hours)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=classify_connection_error(exc)) from exc
    items = [
        PlanRegressionOut(
            query_id=r.query_id,
            query=r.query_text,
            slowdown_factor=r.slowdown_factor,
            current=QueryStorePlanOut(**vars(r.current)),
            baseline=QueryStorePlanOut(**vars(r.baseline)),
        )
        for r in page.slice(report.regressions)
    ]
    return PlanRegressionReportOut(
        instance_id=instance_id,
        state=report.state,
        items=items,
        regression_count=len(items),
        queries_with_history=report.queries_with_history,
        plans=report.plans,
        unavailable_kind=report.unavailable_kind,
        unavailable_reason=report.unavailable_reason,
        required_setting=report.required_setting,
        checked_at=report.checked_at,
    )


@router.get("/{instance_id}/advice-outcomes", response_model=list[IndexAdviceOutcomeOut])
async def list_advice_outcomes(
    instance_id: int,
    page: Page = Depends(page_params),
    db: AsyncSession = Depends(get_db),
) -> list[IndexAdviceOutcomeOut]:
    """Index önerilerinin ölçülmüş etkisi: kurulmadan önce/sonra aynı sorgunun planı (Faz 31 Commit 5)."""
    await _postgres_instance(db, instance_id)
    rows = (
        await db.execute(
            select(IndexAdviceOutcome)
            .where(IndexAdviceOutcome.instance_id == instance_id)
            .order_by(IndexAdviceOutcome.registered_at.desc(), IndexAdviceOutcome.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
    ).scalars().all()
    return [IndexAdviceOutcomeOut(**outcome_payload(row)) for row in rows]


@router.get("/{instance_id}/advice-watches", response_model=list[IndexAdviceWatchListItemOut])
async def list_advice_watches(
    instance_id: int,
    page: Page = Depends(page_params),
    db: AsyncSession = Depends(get_db),
) -> list[IndexAdviceWatchListItemOut]:
    """Çağrı eşiği nedeniyle izlenen sorgular ve eşik dolunca üretilen öneriler (Faz 31 İŞ 1c)."""
    await _postgres_instance(db, instance_id)
    watches = await list_watches(db, instance_id, limit=page.limit, offset=page.offset)
    return [IndexAdviceWatchListItemOut.model_validate(w) for w in watches]
