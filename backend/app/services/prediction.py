from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.metrics import METRIC_KEYS
from app.services.prediction_playbooks import (
    connections_playbook,
    database_size_playbook,
    index_bloat_playbook,
    table_growth_playbook,
    wraparound_playbook,
)
from app.models import MetricRollupDaily, MetricSample, PredictionInsight, SchemaObjectDailySample
from app.services.advice import advice_to_dict
from app.services.prediction_advice import (
    database_size_advice,
    index_bloat_advice,
    short_horizon_advice,
    table_growth_advice,
    wraparound_advice,
)
from app.services.forecasting import DataSufficiency, SeasonalPoint, check_sufficiency, forecast_with_seasonality

# PostgreSQL'in kendi sabit eşikleri (uydurma değil — belgelenmiş varsayılanlar/limitler):
# autovacuum_freeze_max_age varsayılanı 200M; XID'ler 32-bit olduğundan ~2.1B'de "wraparound"
# acil durumu (yazmalar reddedilir) başlar — güvenlik payıyla 1.9B'yi "kritik" eşik alıyoruz.
AUTOVACUUM_FREEZE_MAX_AGE = 200_000_000
WRAPAROUND_DANGER_AGE = 1_900_000_000

# table_growth/index_bloat kaç nesne için tahmin üretilecek (en hızlı büyüyenler) — sınırsız
# olursa büyük şemalarda PredictionInsight tablosu şişer.
TOP_N_OBJECTS = 5


def _format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# Pooler/driver hint per engine — same "go configure this" style as
# services/dashboard_snapshot.py's connectivity recommendations, kept generic (no specific
# numeric target) so it can't contradict parameter_audit's own, more precise findings for the
# same setting (Faz 15 İŞ 6 — see SORULAR.md).
_CONNECTION_ACTIONS = {
    "postgresql": ("max_connections değerini ve bir connection pooler (PgBouncer/pgpool-II) kullanımını değerlendirin.", "SHOW max_connections;"),
    "sqlserver": ("Bağlantı havuzlama (connection pooling) ayarlarını ve uygulama tarafı pool boyutunu gözden geçirin.", None),
    "mongodb": ("Sürücü tarafı bağlantı havuzu (maxPoolSize) ayarını gözden geçirin.", None),
}


async def _existing_open_insight(session: AsyncSession, instance_id: int, metric_key: str) -> bool:
    existing = await session.execute(
        select(PredictionInsight).where(
            PredictionInsight.instance_id == instance_id,
            PredictionInsight.metric_key == metric_key,
            PredictionInsight.acknowledged_at.is_(None),
        )
    )
    return existing.scalar_one_or_none() is not None


async def run_predictions(
    session: AsyncSession,
    instance_id: int,
    current_metrics: dict,
    *,
    engine: str = "postgresql",
    horizon_minutes: int = 60,
    sample_interval_seconds: int = 15,
) -> list[PredictionInsight]:
    """İki tür tahmin üretir:
    1. Kısa vadeli eşik ihlali (ör. "1 saat içinde bağlantı limiti dolabilir") — ham örnekler
       üzerinden, saat-bazlı mevsimsellikle.
    2. Uzun vadeli kapasite planlaması (disk dolma tarihi, tablo büyümesi, wraparound, index
       şişmesi) — günlük rollup'lar üzerinden, haftaiçi/haftasonu mevsimsellikle. Rollup 1
       günde bir yazıldığından (bkz. services/rollup.py) bu tahminler günde bir kez anlamlı
       şekilde değişir; her toplama döngüsünde tekrar hesaplanmaları zararsızdır (zaten açık
       bir PredictionInsight varsa atlanır) ama gereksizdir — gelecekte ayrı bir cadence'e
       taşınabilir (bkz. SORULAR.md)."""
    created: list[PredictionInsight] = []
    created += await _short_horizon_predictions(
        session, instance_id, current_metrics, engine=engine, horizon_minutes=horizon_minutes,
        sample_interval_seconds=sample_interval_seconds,
    )
    created += await _database_size_prediction(session, instance_id)
    created += await _wraparound_prediction(session, instance_id, engine)
    created += await _table_growth_predictions(session, instance_id)
    created += await _index_bloat_predictions(session, instance_id)
    return created


async def _short_horizon_predictions(
    session: AsyncSession,
    instance_id: int,
    current_metrics: dict,
    *,
    engine: str,
    horizon_minutes: int,
    sample_interval_seconds: int,
) -> list[PredictionInsight]:
    horizon_points = max(int(horizon_minutes * 60 / sample_interval_seconds), 4)
    watch_metrics = (
        "connection_utilization_pct",
        "active_connections",
        "cache_hit_ratio",
        "replication_lag_bytes",
        "ops_per_sec",
        "transactions_per_sec",
    )
    created: list[PredictionInsight] = []

    for metric_key in watch_metrics:
        if metric_key not in current_metrics or current_metrics[metric_key] is None:
            continue
        if metric_key not in METRIC_KEYS and metric_key not in {"connection_utilization_pct", "ops_per_sec"}:
            continue
        if await _existing_open_insight(session, instance_id, metric_key):
            continue

        result = await session.execute(
            select(MetricSample)
            .where(MetricSample.instance_id == instance_id)
            .order_by(MetricSample.collected_at.desc())
            .limit(200)
        )
        samples = list(reversed(result.scalars().all()))
        points: list[SeasonalPoint] = []
        for sample in samples:
            val = sample.get_metric(metric_key)
            if val is not None:
                ts = sample.collected_at if sample.collected_at.tzinfo else sample.collected_at.replace(tzinfo=UTC)
                points.append(SeasonalPoint(ts, float(val)))
        current = float(current_metrics[metric_key])
        now = datetime.now(UTC)
        points.append(SeasonalPoint(now, current))
        if len(points) < 5:
            continue  # "insufficient_samples" — bu kısa-vadeli tahminler ham örnek sayısına bakar

        target = now + timedelta(minutes=horizon_minutes)
        forecast = forecast_with_seasonality(points, target, seasonality="hour")

        threshold, severity, message = _default_risk(metric_key, current, forecast.point, forecast.slope_per_day)
        if threshold is None:
            continue
        if not _will_breach(metric_key, current, forecast.point, threshold):
            continue
        recommendation, action = _recommendation_for(metric_key, engine, current, forecast.point)
        # Bağlantı sayısı tahminleri için adım adım plan (Faz 16-B İŞ 7). Diğer kısa vadeli
        # metrikler (cache hit, replication lag, TPS) için plan üretmiyoruz: çözümleri
        # sunucuya/uygulamaya özgü, uyduramayacağımız kadar bağlama bağlı.
        playbook = (
            connections_playbook(engine)
            if metric_key in ("connection_utilization_pct", "active_connections")
            else None
        )
        # Faz 20 İŞ 1: her kısa vadeli metrik artık tam standarda uyan bir öneri taşıyor.
        # Cache hit, replikasyon gecikmesi ve yük artışı için de adım/komut yazıldı; karşılığı
        # doğrulanmamış engine'lerde `unavailable_reason` ile NEDEN üretilemediği yazılıyor.
        advice = short_horizon_advice(
            metric_key=metric_key, engine=engine, current=current,
            predicted=forecast.point, horizon_minutes=horizon_minutes,
        )
        if playbook is None and advice.steps:
            # Standart öneri adım üretebildiyse ham playbook da onunla aynı adımları taşısın —
            # ikisinin ayrışması "aynı veriyi gösteren yerler tek kaynaktan beslensin" kuralını
            # bozardı (arayüz eski istemcilerde hâlâ playbook'u okuyor).
            playbook = [
                {"title": step.action, "detail": "", "command": step.command} for step in advice.steps
            ]

        insight = PredictionInsight(
            instance_id=instance_id,
            metric_key=metric_key,
            horizon_minutes=horizon_minutes,
            current_value=current,
            predicted_value=round(forecast.point, 2),
            lower_bound=round(forecast.lower, 2),
            upper_bound=round(forecast.upper, 2),
            seasonality=forecast.seasonality,
            threshold=threshold,
            confidence=round(max(0.05, min(0.95, forecast.r_squared)), 2),
            severity=severity,
            message=message,
            recommendation=recommendation,
            action=action,
            playbook=playbook,
            advice=advice_to_dict(advice),
        )
        session.add(insight)
        created.append(insight)

    return created


def _default_risk(metric_key: str, current: float, predicted: float, slope: float) -> tuple[float | None, str, str]:
    if metric_key in ("connection_utilization_pct", "active_connections"):
        threshold = 85.0 if metric_key == "connection_utilization_pct" else 100.0
        return (
            threshold,
            "critical" if predicted >= threshold else "warning",
            f"{metric_key} yükseliş trendinde (şimdi {current:.1f}, tahmin ~{predicted:.1f})",
        )
    if metric_key == "cache_hit_ratio":
        threshold = 90.0
        if slope >= 0:
            return None, "info", ""
        return threshold, "warning", f"Cache hit oranı düşüyor (şimdi {current:.1f}%, tahmin ~{predicted:.1f}%)"
    if metric_key == "replication_lag_bytes":
        threshold = 10 * 1024 * 1024
        return threshold, "warning", f"Replication lag artabilir (şimdi {current:.0f} byte, tahmin ~{predicted:.0f})"
    if metric_key in ("transactions_per_sec", "ops_per_sec"):
        if slope <= 0:
            return None, "info", ""
        threshold = current * 2 + 1
        return threshold, "info", f"Yük artışı bekleniyor ({metric_key}: ~{predicted:.1f})"
    return None, "info", ""


def _will_breach(metric_key: str, current: float, predicted: float, threshold: float) -> bool:
    if metric_key == "cache_hit_ratio":
        return predicted < threshold and current >= threshold - 5
    return predicted >= threshold or (current >= threshold * 0.85 and predicted > current)


def _recommendation_for(metric_key: str, engine: str, current: float, predicted: float) -> tuple[str | None, str | None]:
    """Actionable follow-up text (+ optional copy-pasteable command) — deliberately generic/
    non-numeric where a specific setting is involved, so it can never contradict
    parameter_audit's own (more precise, live-value-aware) recommendation for that same setting
    (Faz 15 İŞ 6, see SORULAR.md)."""
    if metric_key in ("connection_utilization_pct", "active_connections"):
        return _CONNECTION_ACTIONS.get(engine, _CONNECTION_ACTIONS["postgresql"])
    if metric_key == "cache_hit_ratio":
        return "Parametreler sekmesindeki bellek/cache ayarlarını (ör. shared_buffers) gözden geçirin.", None
    if metric_key == "replication_lag_bytes":
        return "Replika sunucunun ağ/IO gecikmesini ve kaynak kullanımını kontrol edin.", None
    if metric_key in ("transactions_per_sec", "ops_per_sec"):
        return "Artan yükü karşılamak için bağlantı havuzu ve kapasite planlaması yapın.", None
    return None, None


async def _rollup_points(session: AsyncSession, instance_id: int, metric_key: str) -> list[SeasonalPoint]:
    rows = (
        await session.execute(
            select(MetricRollupDaily)
            .where(MetricRollupDaily.instance_id == instance_id, MetricRollupDaily.metric_key == metric_key)
            .order_by(MetricRollupDaily.day.asc())
        )
    ).scalars().all()
    return [SeasonalPoint(datetime.combine(r.day, datetime.min.time(), tzinfo=UTC), r.last_value) for r in rows]


async def _database_size_prediction(session: AsyncSession, instance_id: int) -> list[PredictionInsight]:
    metric_key = "database_size_bytes"
    if await _existing_open_insight(session, instance_id, metric_key):
        return []
    points = await _rollup_points(session, instance_id, metric_key)
    sufficiency = check_sufficiency("database_size", have_days=len(points), have_samples=len(points))
    if not sufficiency.ready or not points:
        return []

    current = points[-1].value
    if current <= 0:
        return []
    target = points[-1].timestamp + timedelta(days=180)
    forecast = forecast_with_seasonality(points, target, seasonality="weekday")
    if forecast.slope_per_day <= 0:
        return []  # büyümüyor/küçülüyor — dolma tarihi tahmini anlamsız

    days_to_double = current / (forecast.slope_per_day) if forecast.slope_per_day > 0 else None
    if days_to_double is None or days_to_double > 180:
        return []  # çok yavaş bir trend — 6 aydan uzunsa tahmin olarak sunmaya değmez

    doubling_date = (points[-1].timestamp + timedelta(days=days_to_double)).date().isoformat()
    severity = "critical" if days_to_double < 14 else "warning" if days_to_double < 45 else "info"
    bytes_per_day = forecast.slope_per_day
    message = (
        f"Veritabanı boyutu büyüyor (şimdi {_format_bytes(current)}, günlük ~{_format_bytes(bytes_per_day)}) "
        f"— bu hızla ~{doubling_date} civarında iki katına çıkabilir. 180 gün sonraki tahmin: "
        f"{_format_bytes(forecast.point)} (%90 aralık: {_format_bytes(max(0, forecast.lower))}–{_format_bytes(forecast.upper)}). "
        f"Gerçek disk kapasitesi dbace'de izlenmiyor, bu sadece veri büyüme trendi; {sufficiency.have_days:.0f} "
        f"günlük veriye dayanıyor."
    )
    recommendation = (
        "En hızlı büyüyen tabloları belirleyip arşivleme/partitioning değerlendirin, eski/soğuk "
        "veriyi temizleyin, VACUUM ile boşluğu geri kazanın, gerekirse disk kapasitesini büyütün."
    )
    insight = PredictionInsight(
        instance_id=instance_id,
        metric_key=metric_key,
        horizon_minutes=int(days_to_double * 1440),
        current_value=current,
        predicted_value=round(forecast.point, 2),
        lower_bound=round(forecast.lower, 2),
        upper_bound=round(forecast.upper, 2),
        seasonality=forecast.seasonality,
        threshold=current,  # gerçek bir eşik değil — satırın kendi kaydı için
        confidence=round(max(0.05, min(0.95, forecast.r_squared)), 2),
        severity=severity,
        message=message,
        recommendation=recommendation,
        action=None,
        playbook=database_size_playbook(
            current_human=_format_bytes(current),
            per_day_human=_format_bytes(bytes_per_day),
            doubling_date=doubling_date,
        ),
        advice=advice_to_dict(
            database_size_advice(
                current_human=_format_bytes(current),
                per_day_human=_format_bytes(bytes_per_day),
                doubling_date=doubling_date,
                horizon_label=f"{days_to_double:.0f} gün içinde",
            )
        ),
    )
    session.add(insight)
    return [insight]


async def _wraparound_prediction(session: AsyncSession, instance_id: int, engine: str) -> list[PredictionInsight]:
    if engine != "postgresql":
        return []
    metric_key = "transaction_id_age"
    if await _existing_open_insight(session, instance_id, metric_key):
        return []
    points = await _rollup_points(session, instance_id, metric_key)
    sufficiency = check_sufficiency("wraparound", have_days=len(points), have_samples=len(points))
    if not sufficiency.ready or not points:
        return []

    # VACUUM FREEZE az önce çalıştıysa yaş sıfırlanır (büyük bir düşüş) — trend henüz yeniden
    # kurulmamış demektir, uydurma bir tahmin vermek yerine bu döngüde atla.
    if points[-1].value < points[0].value * 0.5:
        return []

    current = points[-1].value
    target = points[-1].timestamp + timedelta(days=365)
    forecast = forecast_with_seasonality(points, target, seasonality="weekday")
    if forecast.slope_per_day <= 0:
        return []

    days_to_freeze_max = (AUTOVACUUM_FREEZE_MAX_AGE - current) / forecast.slope_per_day
    if days_to_freeze_max < 0 or days_to_freeze_max > 365:
        return []

    eta_date = (points[-1].timestamp + timedelta(days=days_to_freeze_max)).date().isoformat()
    severity = "critical" if days_to_freeze_max < 14 else "warning" if days_to_freeze_max < 60 else "info"
    message = (
        f"Transaction ID yaşı artıyor (şimdi {current:.0f}, günlük ~{forecast.slope_per_day:.0f}) — "
        f"bu hızla ~{eta_date} civarında autovacuum_freeze_max_age ({AUTOVACUUM_FREEZE_MAX_AGE:,}) "
        f"eşiğine ulaşabilir. autovacuum bu noktada zorunlu bir freeze VACUUM'u tetikler (yoğun I/O); "
        f"gerçek wraparound acil durumu ({WRAPAROUND_DANGER_AGE:,}) çok daha ilerideki bir eşiktir."
    )
    recommendation = (
        "autovacuum'un engellenmediğini doğrulayın (uzun süren transaction/idle-in-transaction "
        "oturumları freeze VACUUM'u engelleyebilir); gerekirse büyük tablolarda elle VACUUM FREEZE çalıştırın."
    )
    insight = PredictionInsight(
        instance_id=instance_id,
        metric_key=metric_key,
        horizon_minutes=int(days_to_freeze_max * 1440),
        current_value=current,
        predicted_value=round(forecast.point, 2),
        lower_bound=round(forecast.lower, 2),
        upper_bound=round(forecast.upper, 2),
        seasonality=forecast.seasonality,
        threshold=AUTOVACUUM_FREEZE_MAX_AGE,
        confidence=round(max(0.05, min(0.95, forecast.r_squared)), 2),
        severity=severity,
        message=message,
        recommendation=recommendation,
        action="SELECT datname, age(datfrozenxid) FROM pg_database ORDER BY 2 DESC;",
        playbook=wraparound_playbook(
            current_age=current, freeze_max_age=AUTOVACUUM_FREEZE_MAX_AGE, eta_date=eta_date
        ),
        advice=advice_to_dict(
            wraparound_advice(
                current_age=current, freeze_max_age=AUTOVACUUM_FREEZE_MAX_AGE, eta_date=eta_date
            )
        ),
    )
    session.add(insight)
    return [insight]


async def _object_growth_predictions(
    session: AsyncSession, instance_id: int, object_kind: str, requirement_kind: str, prefix: str
) -> list[PredictionInsight]:
    rows = (
        await session.execute(
            select(SchemaObjectDailySample)
            .where(SchemaObjectDailySample.instance_id == instance_id, SchemaObjectDailySample.object_kind == object_kind)
            .order_by(SchemaObjectDailySample.day.asc())
        )
    ).scalars().all()
    by_object: dict[tuple[str, str], list[SchemaObjectDailySample]] = {}
    for r in rows:
        by_object.setdefault((r.schema_name, r.object_name), []).append(r)

    candidates: list[tuple[float, tuple[str, str], list[SchemaObjectDailySample]]] = []
    for key, obj_rows in by_object.items():
        if len(obj_rows) < 2:
            continue
        growth = obj_rows[-1].size_bytes - obj_rows[0].size_bytes
        if growth > 0:
            candidates.append((growth, key, obj_rows))
    candidates.sort(key=lambda c: c[0], reverse=True)

    created: list[PredictionInsight] = []
    for _growth, (schema_name, object_name), obj_rows in candidates[:TOP_N_OBJECTS]:
        metric_key = f"{prefix}:{schema_name}.{object_name}"[:64]
        if await _existing_open_insight(session, instance_id, metric_key):
            continue
        points = [
            SeasonalPoint(datetime.combine(r.day, datetime.min.time(), tzinfo=UTC), r.size_bytes) for r in obj_rows
        ]
        sufficiency = check_sufficiency(requirement_kind, have_days=len(points), have_samples=len(points))
        if not sufficiency.ready:
            continue
        current = points[-1].value
        target = points[-1].timestamp + timedelta(days=30)
        forecast = forecast_with_seasonality(points, target, seasonality="weekday")
        if forecast.slope_per_day <= 0:
            continue
        severity = "warning" if forecast.slope_per_day * 30 > current * 0.5 else "info"
        message = (
            f"{schema_name}.{object_name} ({object_kind}) büyüyor (şimdi {_format_bytes(current)}, "
            f"günlük ~{_format_bytes(forecast.slope_per_day)}) — 30 gün sonra ~{_format_bytes(forecast.point)} "
            f"olabilir ({sufficiency.have_days:.0f} günlük veriye dayanıyor)."
        )
        recommendation = (
            "VACUUM (FULL değil, rutin) ve index yeniden oluşturmayı (REINDEX CONCURRENTLY) "
            "değerlendirin; gerçekten kullanılmıyorsa kaldırmayı düşünün."
            if object_kind == "index"
            else "Partitioning/arşivleme değerlendirin; VACUUM ile ölü satırları geri kazanın."
        )
        insight = PredictionInsight(
            instance_id=instance_id,
            metric_key=metric_key,
            horizon_minutes=30 * 1440,
            current_value=current,
            predicted_value=round(forecast.point, 2),
            lower_bound=round(forecast.lower, 2),
            upper_bound=round(forecast.upper, 2),
            seasonality=forecast.seasonality,
            threshold=current,
            confidence=round(max(0.05, min(0.95, forecast.r_squared)), 2),
            severity=severity,
            message=message,
            recommendation=recommendation,
            action=None,
            playbook=(
                index_bloat_playbook(schema_name, object_name, _format_bytes(forecast.slope_per_day))
                if object_kind == "index"
                else table_growth_playbook(schema_name, object_name, _format_bytes(forecast.slope_per_day))
            ),
            advice=advice_to_dict(
                index_bloat_advice(
                    schema_name=schema_name, index_name=object_name,
                    per_day_human=_format_bytes(forecast.slope_per_day),
                    horizon_label="30 gün sonra", predicted_human=_format_bytes(forecast.point),
                )
                if object_kind == "index"
                else table_growth_advice(
                    schema_name=schema_name, table_name=object_name,
                    per_day_human=_format_bytes(forecast.slope_per_day),
                    horizon_label="30 gün sonra", predicted_human=_format_bytes(forecast.point),
                )
            ),
        )
        session.add(insight)
        created.append(insight)
    return created


async def _table_growth_predictions(session: AsyncSession, instance_id: int) -> list[PredictionInsight]:
    return await _object_growth_predictions(session, instance_id, "table", "table_growth", "table_growth")


async def _index_bloat_predictions(session: AsyncSession, instance_id: int) -> list[PredictionInsight]:
    return await _object_growth_predictions(session, instance_id, "index", "index_bloat", "index_bloat")


async def _rollup_day_count(session: AsyncSession, instance_id: int, metric_key: str) -> int:
    rows = (
        await session.execute(
            select(MetricRollupDaily.id).where(
                MetricRollupDaily.instance_id == instance_id, MetricRollupDaily.metric_key == metric_key
            )
        )
    ).all()
    return len(rows)


async def _best_object_day_count(session: AsyncSession, instance_id: int, object_kind: str) -> int:
    """En çok geçmişe sahip TEK nesnenin gün sayısı — table_growth/index_bloat "en iyi durumda
    ne kadar veri var" sorusuna cevap verir (her nesnenin kendi ayrı tarihçesi var)."""
    rows = (
        await session.execute(
            select(SchemaObjectDailySample.schema_name, SchemaObjectDailySample.object_name).where(
                SchemaObjectDailySample.instance_id == instance_id,
                SchemaObjectDailySample.object_kind == object_kind,
            )
        )
    ).all()
    if not rows:
        return 0
    counts: dict[tuple[str, str], int] = {}
    for schema_name, object_name in rows:
        counts[(schema_name, object_name)] = counts.get((schema_name, object_name), 0) + 1
    return max(counts.values())


async def compute_prediction_readiness(session: AsyncSession, instance_id: int, engine: str) -> list[DataSufficiency]:
    """Faz 16 İŞ 6: her tahmin türü için "kaç gün/örnek gerekli, şu an ne kadar var" — UI bunu
    her zaman gösterir (hazır olsun olmasın), böylece "neden tahmin yok" sorusu her zaman
    cevaplanabilir olur, sessizce boş bir liste dönmez."""
    results: list[DataSufficiency] = []

    db_days = await _rollup_day_count(session, instance_id, "database_size_bytes")
    results.append(check_sufficiency("database_size", have_days=db_days, have_samples=db_days))

    conn_count_result = await session.execute(
        select(MetricSample.collected_at)
        .where(MetricSample.instance_id == instance_id)
        .order_by(MetricSample.collected_at.desc())
        .limit(200)
    )
    conn_rows = conn_count_result.scalars().all()
    if conn_rows:
        span_days = (max(conn_rows) - min(conn_rows)).total_seconds() / 86400.0
    else:
        span_days = 0.0
    results.append(check_sufficiency("connection_trend", have_days=span_days, have_samples=len(conn_rows)))

    if engine == "postgresql":
        wraparound_days = await _rollup_day_count(session, instance_id, "transaction_id_age")
        results.append(check_sufficiency("wraparound", have_days=wraparound_days, have_samples=wraparound_days))

        table_days = await _best_object_day_count(session, instance_id, "table")
        results.append(check_sufficiency("table_growth", have_days=table_days, have_samples=table_days))

        index_days = await _best_object_day_count(session, instance_id, "index")
        results.append(check_sufficiency("index_bloat", have_days=index_days, have_samples=index_days))

    return results
