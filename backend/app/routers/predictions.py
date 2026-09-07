from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import PredictionInsight
from app.schemas import PredictionAccuracyOut, PredictionOut, PredictionReliabilityOut
from app.services.prediction_accuracy import accuracy_by_kind, reliability_map

router = APIRouter(prefix="/predictions", tags=["predictions"])

# Tahmin ailesinin kullanıcıya görünen adı. `metric_key` teknik bir anahtar; doğruluk paneli
# ve güvenilirlik rozeti bu etiketi gösteriyor.
KIND_LABELS = {
    "database_size": "Veritabanı boyutu",
    "connection_trend": "Bağlantı sayısı",
    "table_growth": "Tablo büyümesi",
    "wraparound": "Transaction ID wraparound",
    "index_bloat": "Index şişmesi",
    "cache_hit_ratio": "Cache hit oranı",
    "replication_lag": "Replikasyon gecikmesi",
    "throughput": "İşlem hacmi",
}

# Bir tahminin hangi doğruluk kovasına ait olduğu — `prediction.py`'deki kayıtla AYNI eşleme
# olmak zorunda, yoksa rozet yanlış türün doğruluğunu gösterir.
_SHORT_HORIZON_KIND = {
    "connection_utilization_pct": "connection_trend",
    "active_connections": "connection_trend",
    "cache_hit_ratio": "cache_hit_ratio",
    "replication_lag_bytes": "replication_lag",
    "transactions_per_sec": "throughput",
    "ops_per_sec": "throughput",
}


def kind_of(metric_key: str) -> str:
    """Tahminin doğruluk kovası. Tablo/index tahminlerinde `metric_key` nesne adını taşır
    (`table_growth:public.olaylar`), bu yüzden önek üzerinden çözülüyor."""
    if metric_key in _SHORT_HORIZON_KIND:
        return _SHORT_HORIZON_KIND[metric_key]
    if metric_key.startswith("table_growth:"):
        return "table_growth"
    if metric_key.startswith("index_bloat:"):
        return "index_bloat"
    if metric_key == "database_size_bytes":
        return "database_size"
    if metric_key == "transaction_id_age":
        return "wraparound"
    return metric_key


@router.get("", response_model=list[PredictionOut])
async def list_predictions(
    active_only: bool = True,
    accuracy_days: int = Query(default=30, ge=7, le=365),
    db: AsyncSession = Depends(get_db),
) -> list[PredictionOut]:
    """Açık tahminler + her birinin TÜRÜNE ait ölçülmüş güvenilirlik (Faz 20 İŞ 2).

    Güvenilirlik tahminin kendisine değil ailesine aittir: "bu tür tahminler son N günde
    ne kadar tuttu". Düşük çıkan türler arayüzde işaretleniyor — gizlenmiyor, çünkü modelin
    zayıf olması riskin gerçek olmadığı anlamına gelmez (bkz. ILERLEME.md).
    """
    query = select(PredictionInsight).order_by(PredictionInsight.created_at.desc())
    if active_only:
        query = query.where(PredictionInsight.acknowledged_at.is_(None))
    rows = list((await db.execute(query.limit(100))).scalars().all())

    accuracy = await reliability_map(db, days=accuracy_days)
    out: list[PredictionOut] = []
    for row in rows:
        item = PredictionOut.model_validate(row, from_attributes=True)
        stats = accuracy.get(kind_of(row.metric_key))
        item.reliability = PredictionReliabilityOut(
            level=stats.reliability if stats else "unknown",
            interval_hit_rate=stats.interval_hit_rate if stats else None,
            evaluated_count=stats.evaluated_count if stats else 0,
            note=stats.note
            if stats
            else "Bu tür için henüz tamamlanmış bir doğruluk ölçümü yok.",
        )
        out.append(item)
    return out


@router.get("/accuracy", response_model=list[PredictionAccuracyOut])
async def get_prediction_accuracy(
    days: int = Query(default=30, ge=7, le=365),
    instance_id: int | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[PredictionAccuracyOut]:
    """Tür bazında ölçülmüş doğruluk: ortalama mutlak hata, ortalama yüzde hata ve güven
    aralığının tutma oranı."""
    stats = await accuracy_by_kind(db, days=days, instance_id=instance_id)
    return [
        PredictionAccuracyOut(
            kind=s.kind,
            label=KIND_LABELS.get(s.kind, s.kind),
            evaluated_count=s.evaluated_count,
            pending_count=s.pending_count,
            expired_count=s.expired_count,
            mean_absolute_error=s.mean_absolute_error,
            mean_percent_error=s.mean_percent_error,
            interval_hit_rate=s.interval_hit_rate,
            reliability=s.reliability,
            window_days=s.window_days,
            note=s.note,
        )
        for s in stats
    ]


@router.post("/{prediction_id}/ack", response_model=PredictionOut)
async def acknowledge_prediction(
    prediction_id: int, db: AsyncSession = Depends(get_db)
) -> PredictionInsight:
    from datetime import UTC, datetime

    row = await db.get(PredictionInsight, prediction_id)
    if not row:
        raise HTTPException(status_code=404, detail="Tahmin bulunamadı")
    row.acknowledged_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(row)
    return row
