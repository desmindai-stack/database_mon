"""Faz 16 İŞ 6 — genel amaçlı tahmin motoru: doğrusal regresyon + basit mevsimsellik
(haftaiçi/haftasonu, saat bazlı) + güven aralığı. Sadece Python stdlib (`statistics`, `math`) —
makine öğrenmesi kütüphanesi veya LLM kullanmıyor, hepsi kapalı-form istatistik.

Mimari: ham örnekler önce (opsiyonel) mevsimsel bileşenlerine ayrıştırılır (additive model — her
noktadan kendi "mevsim" ortalamasının sapması çıkarılır), kalan trend üzerine en küçük kareler
regresyonu uygulanır, tahmin noktasına mevsimsel bileşen geri eklenir. Güven aralığı, regresyonun
kalıntı (residual) varyansından türetilen standart bir tahmin aralığı formülüyle hesaplanır — bu,
istatistik ders kitaplarındaki "prediction interval" formülüdür, uydurma bir sayı değil.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import sqrt
from statistics import mean

# t-dağılımı tablosuna ihtiyaç duymamak için normal dağılım yaklaşıklığı kullanılıyor (n arttıkça
# t-dağılımı zaten normale yaklaşır; dbace'in tahmin ettiği örnek sayıları (>=8) için fark
# pratikte önemsiz). %90 güven aralığı için iki-kuyruklu z değeri.
Z_90 = 1.645


@dataclass
class RegressionResult:
    point: float
    lower: float
    upper: float
    slope_per_x: float
    r_squared: float


def linear_regression_with_ci(
    xs: list[float], ys: list[float], x_new: float, z: float = Z_90
) -> RegressionResult:
    """En küçük kareler doğrusal regresyonu + x_new noktası için tahmin aralığı.

    `n < 3` ise anlamlı bir regresyon kurulamaz — çağıran taraf bunu `min_samples` kontrolüyle
    zaten engellemiş olmalı (bkz. PREDICTION_REQUIREMENTS), burada sadece son çare olarak
    dejenere olmayan bir sonuç döndürülür.
    """
    n = len(xs)
    if n < 2 or len(set(xs)) < 2:
        y = ys[-1] if ys else 0.0
        return RegressionResult(point=y, lower=y, upper=y, slope_per_x=0.0, r_squared=0.0)

    x_mean = mean(xs)
    y_mean = mean(ys)
    sxx = sum((x - x_mean) ** 2 for x in xs)
    sxy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    slope = sxy / sxx if sxx else 0.0
    intercept = y_mean - slope * x_mean

    residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    ss_res = sum(r * r for r in residuals)
    ss_tot = sum((y - y_mean) ** 2 for y in ys) or 1.0
    r_squared = max(0.0, 1 - ss_res / ss_tot)

    dof = max(n - 2, 1)
    residual_var = ss_res / dof
    point = intercept + slope * x_new
    se_pred = sqrt(residual_var * (1 + 1 / n + ((x_new - x_mean) ** 2 / sxx if sxx else 0)))
    margin = z * se_pred

    return RegressionResult(point=point, lower=point - margin, upper=point + margin, slope_per_x=slope, r_squared=r_squared)


@dataclass
class SeasonalPoint:
    timestamp: datetime
    value: float


def _seasonal_key(ts: datetime, granularity: str) -> str:
    if granularity == "weekday":
        return "weekend" if ts.weekday() >= 5 else "weekday"
    return f"h{ts.hour:02d}"  # hour-of-day


def deseasonalize(points: list[SeasonalPoint], granularity: str) -> tuple[list[float], dict[str, float]]:
    """Additive mevsimsel ayrıştırma: her nokta - (o mevsimin ortalama sapması). granularity:
    "weekday" (haftaiçi/haftasonu) veya "hour" (saat bazlı). Döndürülen dict, tahmin noktasına
    mevsimsel bileşeni geri eklemek için kullanılır (bkz. seasonal_offset_for)."""
    overall = mean(p.value for p in points)
    buckets: dict[str, list[float]] = {}
    for p in points:
        key = _seasonal_key(p.timestamp, granularity)
        buckets.setdefault(key, []).append(p.value)
    offsets = {key: mean(vals) - overall for key, vals in buckets.items()}
    deseasonalized = [p.value - offsets.get(_seasonal_key(p.timestamp, granularity), 0.0) for p in points]
    return deseasonalized, offsets


def seasonal_offset_for(ts: datetime, granularity: str, offsets: dict[str, float]) -> float:
    return offsets.get(_seasonal_key(ts, granularity), 0.0)


@dataclass
class ForecastResult:
    point: float
    lower: float
    upper: float
    slope_per_day: float
    r_squared: float
    seasonality: str  # "weekday" | "hour" | "none"


def forecast_with_seasonality(
    points: list[SeasonalPoint], target: datetime, *, seasonality: str = "weekday"
) -> ForecastResult:
    """points: kronolojik sırayla (timestamp, value) — günlük rollup ise "weekday", saatlik/ham
    örnek ise "hour" mevsimsellik daha anlamlı olur (çağıran taraf seçer). En az 8 nokta ve en az
    2 farklı mevsim bucket'ı yoksa mevsimsellik uygulanmaz (yetersiz veriyle "mevsimsel desen"
    uydurmamak için — sadece düz regresyona düşer)."""
    if len(points) < 8:
        seasonality = "none"
    else:
        keys = {_seasonal_key(p.timestamp, seasonality) for p in points}
        if len(keys) < 2:
            seasonality = "none"

    if seasonality == "none":
        deseasonalized = [p.value for p in points]
        offsets: dict[str, float] = {}
    else:
        deseasonalized, offsets = deseasonalize(points, seasonality)

    x0 = points[0].timestamp
    xs = [(p.timestamp - x0).total_seconds() / 86400.0 for p in points]  # gün cinsinden
    x_new = (target - x0).total_seconds() / 86400.0

    reg = linear_regression_with_ci(xs, deseasonalized, x_new)
    offset = seasonal_offset_for(target, seasonality, offsets) if seasonality != "none" else 0.0

    return ForecastResult(
        point=reg.point + offset,
        lower=reg.lower + offset,
        upper=reg.upper + offset,
        slope_per_day=reg.slope_per_x,
        r_squared=reg.r_squared,
        seasonality=seasonality,
    )


@dataclass
class PredictionRequirement:
    kind: str
    label: str
    min_samples: int
    min_days: float
    note: str = ""


# Her tahmin türü için minimum veri gereksinimi — İŞ 6'nın "kaç gün/kaç örnek gerekli" isteği.
# min_days değerleri, o tahminin dayandığı verinin doğal periyoduna göre seçildi: günlük
# rollup'a dayananlar (disk/tablo büyümesi, wraparound) en az bir haftalık trend ister (haftaiçi/
# haftasonu döngüsünü ayırt edebilmek için); ham örneklere dayanan bağlantı trendi çok daha kısa
# bir pencerede de anlamlıdır.
PREDICTION_REQUIREMENTS: dict[str, PredictionRequirement] = {
    "database_size": PredictionRequirement(
        "database_size", "Veritabanı boyutu dolma tarihi", min_samples=7, min_days=7,
        note="Günlük rollup üzerinden — haftaiçi/haftasonu deseni ayırt edilebilsin diye en az 7 gün.",
    ),
    "connection_trend": PredictionRequirement(
        "connection_trend", "Bağlantı sayısı trendi", min_samples=40, min_days=0.5,
        note="Ham metrik örnekleri üzerinden — saat bazlı deseni yakalamak için en az 12 saat.",
    ),
    "table_growth": PredictionRequirement(
        "table_growth", "Tablo büyüme hızı", min_samples=7, min_days=7,
        note="Günlük şema taraması rollup'ı üzerinden.",
    ),
    "wraparound": PredictionRequirement(
        "wraparound", "Transaction ID wraparound riski", min_samples=7, min_days=7,
        note="Günlük rollup üzerinden — freeze/VACUUM olayları arasında en az 7 günlük tutarlı artış.",
    ),
    "index_bloat": PredictionRequirement(
        "index_bloat", "Index şişmesi", min_samples=7, min_days=7,
        note="Günlük şema taraması rollup'ı üzerinden.",
    ),
}


@dataclass
class DataSufficiency:
    kind: str
    label: str
    have_days: float
    need_days: float
    have_samples: int
    need_samples: int
    ready: bool
    days_remaining: float
    note: str = ""


def check_sufficiency(kind: str, have_days: float, have_samples: int) -> DataSufficiency:
    req = PREDICTION_REQUIREMENTS[kind]
    ready = have_days >= req.min_days and have_samples >= req.min_samples
    remaining = max(0.0, req.min_days - have_days)
    return DataSufficiency(
        kind=kind,
        label=req.label,
        have_days=round(have_days, 1),
        need_days=req.min_days,
        have_samples=have_samples,
        need_samples=req.min_samples,
        ready=ready,
        days_remaining=round(remaining, 1),
        note=req.note,
    )
