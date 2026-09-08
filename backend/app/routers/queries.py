from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.collectors.base import ConnectionTarget, classify_connection_error
from app.database import get_db
from app.models import CapturedPlan, Instance, Node, Server, SlowQuerySample
from app.schemas import (
    CapturedPlanListOut,
    CapturedPlanOut,
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
from app.services.advice import Advice, AdviceStep, advice_to_dict
from app.services.index_advisor import PostgreSQLIndexAdvisor
from app.services.database_load import wait_profiles_by_query
from app.services.query_diagnostics import diagnose_queries
from app.services.query_history import build_query_series, group_rows_by_queryid, summarize_history
from app.services.noise_settings import get_noise_settings
from app.services.slow_query_selection import DEFAULT_WINDOW_HOURS, select_slow_queries
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
        raise HTTPException(status_code=404, detail="Instance bulunamadi")

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
        raise HTTPException(status_code=404, detail="Instance bulunamadi")

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
        # Faz 19 IS 1 — API DEGISIKLIGI: eskiden 404 donuyordu. Instance de sorgu da var
        # olabilir; yalnizca SECILEN PENCEREDE ornek yoktur (yeni eklenmis instance, uzun
        # aralikli toplama, ya da o pencerede calismamis bir sorgu). "Yok" degil "bos" —
        # istemcinin bunu silinmis bir kayittan ayirabilmesi icin bos seri donuyoruz.
        return QueryHistorySeriesOut.model_validate(summarize_history(queryid, "", []))
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
        raise HTTPException(status_code=404, detail="Instance bulunamadi")
    report = await get_slow_query_availability(db, instance)
    return SlowQueryAvailabilityOut(**vars(report))


@router.get("/{instance_id}", response_model=SlowQueryListOut)
async def get_slow_queries(
    instance_id: int,
    limit: int = Query(default=20, ge=1, le=100),
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

    window_end = end or datetime.now(UTC)
    window_start = start or (window_end - timedelta(hours=DEFAULT_WINDOW_HOURS))

    # Eşikler ve sistem sorgusu görünürlüğü TEK yerden (Faz 18 İŞ 2) — rapor da aynısını
    # okuyor, ikisinin farklı eşik kullanması Faz 18 İŞ 1'deki tutarsızlığı geri getirirdi.
    noise = await get_noise_settings(db)
    selection = await select_slow_queries(
        db,
        instance_id,
        start=window_start,
        end=window_end,
        sort=sort,
        limit=limit,
        include_system=noise["show_system_queries"] if include_system is None else include_system,
        min_total_ms=noise["list_min_total_ms"],
        min_calls=noise["list_min_calls"],
    )
    return selection_to_out(selection, window_start, window_end)


def selection_to_out(selection, window_start: datetime, window_end: datetime) -> SlowQueryListOut:
    """SlowQuerySelection → API şeması. Rapor derin bağlantısının hedef ucu da bunu kullanıyor."""
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
                sample_count=e.sample_count,
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

    # Faz 25 İŞ 3: bekleme ölçümü varsa teşhis ONDAN yapılıyor. Öncesinde sınıflandırma
    # exec_user_time/exec_sys_time'a bağlıydı ve bu sütunlar çoğu kurulumda boş geldiği için
    # sonuç sık sık "unknown" oluyordu — yani darboğaz sınıflandırması pratikte çalışmıyordu.
    wait_profiles = await wait_profiles_by_query(db, instance_id)
    diagnoses = diagnose_queries(rows, wait_profiles)
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
        if instance.engine != "postgresql":
            out.unavailable_reason = (
                "auto_explain yalnızca PostgreSQL'de var; bu motor için gerçek çalıştırma planı "
                "yakalanamıyor."
            )
        elif not (instance.options or {}).get("agent_url"):
            out.unavailable_reason = (
                "Bu instance için host-agent yapılandırılmamış. auto_explain planları sunucu "
                "log'undan okunuyor ve log'a erişim agent üzerinden sağlanıyor."
            )
            out.managed_service_guidance = MANAGED_SERVICE_GUIDANCE
        else:
            out.unavailable_reason = (
                "Henüz yakalanmış plan yok. Ön koşullar panelindeki auto_explain kontrollerine "
                "bakın: kütüphane yüklü, eşik ayarlı ve log_format='json' olmalı. Üçü de "
                "tamamsa, eşiği aşan bir sorgu çalışana kadar plan birikmez."
            )
    return out


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
        source_caveat=(
            None
            if row.has_actual_rows
            else (
                "Bu plan gerçek çalıştırmadan yakalandı ama auto_explain.log_analyze kapalı "
                "olduğu için GERÇEK SATIR SAYISI yok — yalnızca planlayıcının tahmini var. "
                "Tahmini/gerçek sapma analizi bu planda yapılamaz."
            )
        ),
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


def _index_advice(recommendation) -> dict:
    """Index önerisini standart öneri yapısına çevirir (Faz 17 Ek İŞ B).

    CREATE INDEX CONCURRENTLY bilinçli tercih: tabloyu yazmaya kapatmaz. Buna karşılık kendi
    riskleri var (yarıda kalırsa INVALID index bırakır, iki kopya birden diskte durur) ve bu
    riskler "Dikkat" başlığında yazılı — komutu uyarısız vermek sorumsuzluk olurdu.
    """
    target = f"{recommendation.schema_name}.{recommendation.table_name}"
    columns = ", ".join(recommendation.columns)
    concurrent_ddl = recommendation.index_ddl.replace("CREATE INDEX ", "CREATE INDEX CONCURRENTLY ", 1)
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
                AdviceStep(
                    "Index'i tabloyu kilitlemeden oluşturun.",
                    concurrent_ddl,
                ),
                AdviceStep(
                    "İstatistikleri tazeleyin ki planlayıcı yeni index'i hemen kullanabilsin.",
                    f"ANALYZE {target};",
                ),
            ],
            cautions=[
                "CREATE INDEX CONCURRENTLY işlem bloğu içinde çalıştırılamaz ve normalinden uzun sürer.",
                "Yarıda kalırsa geride INVALID bir index kalır; DROP INDEX ile temizlenmelidir.",
                "İşlem sırasında index'in diskte yer kaplayacağını hesaba katın.",
            ],
            estimated_duration="Tablo boyutuna göre dakikalar; büyük tablolarda saatler sürebilir.",
            rollback=f"DROP INDEX CONCURRENTLY IF EXISTS {recommendation.schema_name}.{_index_name(recommendation)};",
            verification=(
                f"EXPLAIN (ANALYZE, BUFFERS) <sorgunuz>;\n"
                f"-- Planda Seq Scan yerine Index Scan görmelisiniz.\n"
                f"SELECT idx_scan FROM pg_stat_user_indexes\n"
                f"WHERE schemaname = '{recommendation.schema_name}' "
                f"AND indexrelname = '{_index_name(recommendation)}';"
            ),
        )
    )


def _index_name(recommendation) -> str:
    """CREATE INDEX ifadesinden index adını çıkarır (geri alma ve doğrulama komutları için)."""
    parts = recommendation.index_ddl.split()
    try:
        return parts[parts.index("INDEX") + 1]
    except (ValueError, IndexError):
        return "<index_adi>"


@router.post("/{instance_id}/advice", response_model=IndexAdviceReportOut)
async def advise_indexes(
    instance_id: int,
    body: IndexAdviceRequest,
    db: AsyncSession = Depends(get_db),
) -> IndexAdviceReportOut:
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Instance bulunamadi")
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
                advice=_index_advice(r),
            )
            for r in recommendations
        ],
        no_advice_reasons=[
            NoAdviceReasonOut(code=r.code, message=r.message, what_to_do=r.what_to_do) for r in reasons
        ],
    )
    query_cache.set(cache_key, report, ttl_seconds=_ADVICE_CACHE_TTL_SECONDS)
    return report
