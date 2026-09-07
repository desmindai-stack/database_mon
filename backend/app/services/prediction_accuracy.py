"""Tahmin doğruluğu geri besleme döngüsü (Faz 20 İŞ 2).

**Neden gerekliydi:** dbace her tahmine bir `confidence` yazıyordu ama bu, regresyonun R²
değeriydi — "model GEÇMİŞ veriye ne kadar iyi oturdu" demek. Tahminin TUTUP tutmadığıyla
ilgisi yok: gürültüsüz ama tamamen yanlış eğimli bir seri de R²=0.99 verir. Yani ürün
doğruluk İDDİA ediyordu, ölçmüyordu.

Döngü üç adım:

1. **Kayıt** (`record_prediction`) — her tahmin üretildiğinde ne tahmin edildiği, hangi tarih
   için, hangi güven aralığıyla, hangi yöntemle ve ne kadar veriye dayanarak yazılır.
2. **Değerlendirme** (`evaluate_due_outcomes`) — hedef tarih geldiğinde gerçekleşen değer
   okunur, mutlak/yüzde hata ve aralığın tutup tutmadığı hesaplanır. Değer okunamıyorsa
   (instance kapatılmış, toplama durmuş, nesne silinmiş) satır "expired" işaretlenir;
   bunu hatalı tahmin saymak modeli haksız yere cezalandırırdı.
3. **Metrik** (`accuracy_by_kind`) — tür bazında ortalama mutlak hata, ortalama yüzde hata
   ve güven aralığının tutma oranı; buradan bir güvenilirlik seviyesi türetilir.

**Kontrol noktası (checkpoint) kararı:** uzun vadeli tahminlerin manşet ufku aylar sürüyor
(disk için 180 gün). O tarihi beklemek altı ay boyunca hiçbir geri besleme almamak demekti.
Bunun yerine AYNI modelden kısa bir ufuk için ikinci bir tahmin alınıp o ölçülüyor. Ölçülen
şey modelin kendisi olduğu için bu geçerli bir vekil — ve arayüzde "hangi ufukta ölçüldüğü"
açıkça yazılıyor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    MetricRollupDaily,
    MetricSample,
    PredictionOutcome,
    SchemaObjectDailySample,
)

# Uzun vadeli tahminler için değerlendirme ufku. Manşet ufuk (180/365/30 gün) değil — bkz.
# modül açıklamasındaki "kontrol noktası" kararı.
LONG_HORIZON_CHECKPOINT_DAYS = 7.0

# Hedef tarihten sonra ne kadar beklenip "değer okunamadı" denecek. Günlük kaynaklarda bir
# rollup turu kaçmış olabilir, ham örneklerde toplama birkaç dakika gecikmiş olabilir.
_GRACE = {
    "sample": timedelta(minutes=30),
    "rollup": timedelta(days=2),
    "schema_object": timedelta(days=2),
}

# Ham örnek eşleştirmesinde hedef zamana bu kadar yakın bir örnek kabul edilir.
_SAMPLE_TOLERANCE = timedelta(minutes=15)

# Güvenilirlik eşikleri: %90 aralığın gerçekte ne oranda tuttuğu. Nominal oran %90; pratikte
# bunun altına düşmesi normaldir, ama çok düşükse aralık anlamını yitirmiştir.
RELIABILITY_MIN_SAMPLES = 5
RELIABILITY_HIGH = 0.75
RELIABILITY_MEDIUM = 0.5


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# --- 1. Kayıt ------------------------------------------------------------------------------


def record_prediction(
    session: AsyncSession,
    *,
    prediction_id: int | None,
    instance_id: int,
    kind: str,
    metric_key: str,
    target_at: datetime,
    checkpoint_days: float,
    predicted_value: float,
    lower_bound: float | None,
    upper_bound: float | None,
    method: str,
    sample_count: int,
    span_days: float,
    r_squared: float,
    source: str,
    schema_name: str | None = None,
    object_name: str | None = None,
) -> PredictionOutcome:
    """Tahmini "ileride ölçülecek" olarak kaydeder. `session.add` yapar, commit ETMEZ —
    çağıran taraf tahminin kendisiyle aynı transaction'da yazsın."""
    outcome = PredictionOutcome(
        prediction_id=prediction_id,
        instance_id=instance_id,
        kind=kind,
        metric_key=metric_key,
        target_at=target_at,
        checkpoint_days=checkpoint_days,
        predicted_value=predicted_value,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        method=method,
        sample_count=sample_count,
        span_days=span_days,
        r_squared=r_squared,
        source=source,
        schema_name=schema_name,
        object_name=object_name,
        status="pending",
    )
    session.add(outcome)
    return outcome


# --- 2. Değerlendirme ----------------------------------------------------------------------


async def _actual_from_sample(
    session: AsyncSession, outcome: PredictionOutcome
) -> float | None:
    """Hedef zamana en yakın ham örnek. Hedefin ÖNCESİ ve SONRASI birlikte aranıyor: toplama
    döngüsü hedefi birkaç saniye kaçırmış olabilir."""
    target = _as_utc(outcome.target_at)
    rows = (
        await session.execute(
            select(MetricSample)
            .where(
                MetricSample.instance_id == outcome.instance_id,
                MetricSample.collected_at >= target - _SAMPLE_TOLERANCE,
                MetricSample.collected_at <= target + _SAMPLE_TOLERANCE,
            )
            .order_by(MetricSample.collected_at.asc())
        )
    ).scalars().all()
    best: tuple[float, float] | None = None  # (mesafe, değer)
    for row in rows:
        value = row.get_metric(outcome.metric_key)
        if value is None:
            continue
        distance = abs((_as_utc(row.collected_at) - target).total_seconds())
        if best is None or distance < best[0]:
            best = (distance, float(value))
    return best[1] if best else None


async def _actual_from_rollup(
    session: AsyncSession, outcome: PredictionOutcome
) -> float | None:
    row = (
        await session.execute(
            select(MetricRollupDaily).where(
                MetricRollupDaily.instance_id == outcome.instance_id,
                MetricRollupDaily.metric_key == outcome.metric_key,
                MetricRollupDaily.day == _as_utc(outcome.target_at).date(),
            )
        )
    ).scalar_one_or_none()
    return float(row.last_value) if row else None


async def _actual_from_schema_object(
    session: AsyncSession, outcome: PredictionOutcome
) -> float | None:
    if not outcome.schema_name or not outcome.object_name:
        return None
    object_kind = "index" if outcome.kind == "index_bloat" else "table"
    row = (
        await session.execute(
            select(SchemaObjectDailySample).where(
                SchemaObjectDailySample.instance_id == outcome.instance_id,
                SchemaObjectDailySample.object_kind == object_kind,
                SchemaObjectDailySample.schema_name == outcome.schema_name,
                SchemaObjectDailySample.object_name == outcome.object_name,
                SchemaObjectDailySample.day == _as_utc(outcome.target_at).date(),
            )
        )
    ).scalar_one_or_none()
    return float(row.size_bytes) if row else None


_READERS = {
    "sample": _actual_from_sample,
    "rollup": _actual_from_rollup,
    "schema_object": _actual_from_schema_object,
}


def _apply_actual(outcome: PredictionOutcome, actual: float, now: datetime) -> None:
    outcome.actual_value = actual
    outcome.absolute_error = abs(actual - outcome.predicted_value)
    # Yüzde hata sıfıra bölünemez; gerçekleşen değer 0 ise yüzde anlamsızdır, mutlak hata kalır.
    outcome.percent_error = (
        abs(actual - outcome.predicted_value) / abs(actual) * 100 if actual else None
    )
    if outcome.lower_bound is not None and outcome.upper_bound is not None:
        outcome.within_interval = outcome.lower_bound <= actual <= outcome.upper_bound
    outcome.status = "evaluated"
    outcome.evaluated_at = now


async def evaluate_due_outcomes(session: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """Hedef tarihi gelmiş bekleyen tahminleri değerlendirir. Commit ETMEZ."""
    now = now or datetime.now(UTC)
    pending = (
        await session.execute(
            select(PredictionOutcome).where(
                PredictionOutcome.status == "pending",
                PredictionOutcome.target_at <= now,
            )
        )
    ).scalars().all()

    counts = {"evaluated": 0, "expired": 0, "waiting": 0}
    for outcome in pending:
        reader = _READERS.get(outcome.source)
        actual = await reader(session, outcome) if reader else None
        if actual is not None:
            _apply_actual(outcome, actual, now)
            counts["evaluated"] += 1
            continue
        grace = _GRACE.get(outcome.source, timedelta(days=1))
        if now > _as_utc(outcome.target_at) + grace:
            # Değer okunamadı — modeli haksız yere cezalandırmamak için "hatalı" değil
            # "ölçülemedi" olarak kapatılıyor ve NEDENİ yazılıyor.
            outcome.status = "expired"
            outcome.evaluated_at = now
            outcome.unevaluable_reason = (
                f"Hedef tarihte ({_as_utc(outcome.target_at).strftime('%d.%m.%Y %H:%M')}) "
                f"gerçekleşen değer bulunamadı — toplama durmuş, instance kapatılmış ya da "
                f"ölçülen nesne silinmiş olabilir."
            )
            counts["expired"] += 1
        else:
            counts["waiting"] += 1
    return counts


# --- 3. Metrik -----------------------------------------------------------------------------


@dataclass
class KindAccuracy:
    kind: str
    evaluated_count: int
    pending_count: int
    expired_count: int
    mean_absolute_error: float | None
    mean_percent_error: float | None
    interval_hit_rate: float | None
    reliability: str  # "unknown" | "low" | "medium" | "high"
    window_days: int
    note: str


def _reliability(hit_rate: float | None, evaluated: int) -> tuple[str, str]:
    if evaluated < RELIABILITY_MIN_SAMPLES or hit_rate is None:
        return (
            "unknown",
            f"Henüz {evaluated} ölçüm var; güvenilirlik için en az {RELIABILITY_MIN_SAMPLES} "
            "tamamlanmış tahmin gerekiyor.",
        )
    if hit_rate >= RELIABILITY_HIGH:
        return "high", f"Son dönemde tahminlerin %{hit_rate * 100:.0f}'ı güven aralığı içinde kaldı."
    if hit_rate >= RELIABILITY_MEDIUM:
        return (
            "medium",
            f"Tahminlerin %{hit_rate * 100:.0f}'ı güven aralığı içinde kaldı — yön göstergesi "
            "olarak kullanın, kesin tarih olarak değil.",
        )
    return (
        "low",
        f"Tahminlerin yalnızca %{hit_rate * 100:.0f}'ı güven aralığı içinde kaldı. Bu türdeki "
        "tahminler şu an güvenilir değil; kararı yalnızca buna dayandırmayın.",
    )


async def accuracy_by_kind(
    session: AsyncSession, *, days: int = 30, instance_id: int | None = None
) -> list[KindAccuracy]:
    """Tür bazında doğruluk. Pencere, tahminin ÜRETİLDİĞİ tarihe değil DEĞERLENDİRİLDİĞİ
    tarihe göre — "son 30 günde ölçülenler" sorusunun cevabı bu."""
    since = datetime.now(UTC) - timedelta(days=days)
    query = select(PredictionOutcome).where(PredictionOutcome.created_at >= since - timedelta(days=days))
    if instance_id is not None:
        query = query.where(PredictionOutcome.instance_id == instance_id)
    rows = (await session.execute(query)).scalars().all()

    by_kind: dict[str, list[PredictionOutcome]] = {}
    for row in rows:
        by_kind.setdefault(row.kind, []).append(row)

    results: list[KindAccuracy] = []
    for kind, items in sorted(by_kind.items()):
        evaluated = [
            i
            for i in items
            if i.status == "evaluated"
            and i.evaluated_at is not None
            and _as_utc(i.evaluated_at) >= since
        ]
        pending = [i for i in items if i.status == "pending"]
        expired = [i for i in items if i.status == "expired"]

        errors = [i.absolute_error for i in evaluated if i.absolute_error is not None]
        pct_errors = [i.percent_error for i in evaluated if i.percent_error is not None]
        hits = [i.within_interval for i in evaluated if i.within_interval is not None]
        hit_rate = (sum(1 for h in hits if h) / len(hits)) if hits else None

        level, note = _reliability(hit_rate, len(evaluated))
        results.append(
            KindAccuracy(
                kind=kind,
                evaluated_count=len(evaluated),
                pending_count=len(pending),
                expired_count=len(expired),
                mean_absolute_error=(sum(errors) / len(errors)) if errors else None,
                mean_percent_error=(sum(pct_errors) / len(pct_errors)) if pct_errors else None,
                interval_hit_rate=hit_rate,
                reliability=level,
                window_days=days,
                note=note,
            )
        )
    return results


async def reliability_map(
    session: AsyncSession, *, days: int = 30
) -> dict[str, KindAccuracy]:
    """Tahmin listesini işaretlemek için tür → doğruluk sözlüğü."""
    return {a.kind: a for a in await accuracy_by_kind(session, days=days)}
