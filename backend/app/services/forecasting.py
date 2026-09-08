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

from dataclasses import dataclass, field
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
    # Eğimin kendi %90 güven aralığı (Faz 20 İŞ 3). Nokta tahmininin aralığından FARKLI bir
    # şey: "büyüme hızı ne kadar belirsiz" sorusunu cevaplar ve "45-60 gün arası" biçimindeki
    # tarih aralıkları buradan türetilir. se(slope) = sqrt(kalıntı varyansı / Sxx).
    slope_lower: float = 0.0
    slope_upper: float = 0.0
    # Kalıntıların standart sapması — doğrusallık testi bunu ölçek olarak kullanıyor.
    residual_std: float = 0.0
    residuals: list[float] = field(default_factory=list)


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

    # Eğimin standart hatası — nokta tahmininin değil, EĞİMİN belirsizliği.
    se_slope = sqrt(residual_var / sxx) if sxx else 0.0
    slope_margin = z * se_slope

    return RegressionResult(
        point=point,
        lower=point - margin,
        upper=point + margin,
        slope_per_x=slope,
        r_squared=r_squared,
        slope_lower=slope - slope_margin,
        slope_upper=slope + slope_margin,
        residual_std=sqrt(residual_var),
        residuals=residuals,
    )


@dataclass
class SeasonalPoint:
    timestamp: datetime
    value: float


# En fazla bu oranda nokta aykırı sayılıp atılabilir. Üstünü atmak "veriyi tahmine uydurmak"
# olurdu — o noktada sorun tek bir sıçrama değil, modelin yanlış olmasıdır.
MAX_OUTLIER_FRACTION = 0.2
# Medyan mutlak sapmanın kaç katı aykırı sayılır. 3.5, MAD tabanlı aykırı tespitinde yaygın
# kullanılan eşiktir (normal dağılımda ~%0.05'lik uç).
OUTLIER_MAD_MULTIPLIER = 3.5


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def detect_outliers(xs: list[float], ys: list[float]) -> list[bool]:
    """Tek seferlik sıçramaları işaretler (Faz 20 İŞ 3).

    Aykırılık ORTALAMAYA değil TRENDE göre ölçülüyor: büyüyen bir seride değerler zaten geniş
    bir aralığa yayılır, ortalamadan uzaklık orada anlamsızdır. Bu yüzden önce bir doğru
    geçiriliyor, sonra o doğruya olan kalıntılar üzerinde MAD (medyan mutlak sapma) tabanlı bir
    eşik uygulanıyor — standart sapma kullanılsaydı aykırı değerin kendisi eşiği şişirir ve
    kendini gizlerdi.

    Örnek: 20 günlük düzgün büyüyen disk serisinde bir günlük yedek alma yüzünden 3 kat sıçrayan
    tek bir nokta, eğimi olduğundan çok daha dik gösterir ve "disk 5 gün sonra dolacak" gibi
    yanlış bir aciliyet üretir.
    """
    n = len(xs)
    if n < 5:
        return [False] * n  # bu kadar az noktada "aykırı" demek için dayanak yok

    first_pass = linear_regression_with_ci(xs, ys, xs[-1])
    residuals = first_pass.residuals or [0.0] * n
    med = _median(residuals)
    mad = _median([abs(r - med) for r in residuals])
    if mad <= 0:
        return [False] * n  # kalıntılar tekbiçim — ayıracak bir şey yok

    threshold = OUTLIER_MAD_MULTIPLIER * mad
    flags = [abs(r - med) > threshold for r in residuals]

    # Çok fazla nokta aykırı çıkıyorsa sorun tek bir sıçrama değildir; hiçbirini atmıyoruz.
    if sum(flags) > max(1, int(n * MAX_OUTLIER_FRACTION)):
        return [False] * n
    return flags


@dataclass
class FitQuality:
    """Verinin doğrusal modele gerçekten uyup uymadığı (Faz 20 İŞ 3)."""

    kind: str  # "linear" | "exponential" | "curved" | "noisy" | "flat"
    note: str
    r_squared: float
    log_r_squared: float | None = None

    @property
    def is_linear(self) -> bool:
        return self.kind in ("linear", "flat")


# Log dönüşümlü uyum bu kadar daha iyiyse büyüme üstel kabul ediliyor: doğrusal bir tahmin
# üstel bir seriyi SİSTEMATİK olarak olduğundan iyimser gösterir.
EXPONENTIAL_R2_GAIN = 0.15
# Bunun altındaki uyumla "trend" demek gürültüyü trend sanmaktır.
NOISY_R2 = 0.3
# Kalıntıların ortadaki üçte biriyle uçlar arasındaki fark, kalıntı ölçeğinin bu katını
# aşarsa veri düz bir doğru değil bir eğri çiziyor demektir. Değer ölçülerek seçildi: belirgin
# şekilde eğri bir seri (y = x^0.45) 1.02 veriyor, düzgün doğrusal bir seri 0.1'in altında —
# 0.8 ikisini rahatça ayırıyor.
CURVATURE_SIGMA = 0.8


def assess_fit(xs: list[float], ys: list[float]) -> FitQuality:
    """Doğrusal model bu veriye uyuyor mu — uymuyorsa zorla doğru çizmek yerine söylenir.

    Üç kontrol:
    1. **Üstel büyüme**: log(y) üzerine kurulan regresyon belirgin şekilde daha iyi uyuyorsa
       büyüme üsteldir; doğrusal tahmin bu durumda sistematik olarak İYİMSER olur.
    2. **Eğrilik**: kalıntılar ortada bir yöne, uçlarda diğer yöne sapıyorsa veri bir eğri
       çiziyordur (doyuma ulaşan ya da hızlanan büyüme).
    3. **Gürültü**: R² çok düşükse ortada bir trend yok demektir.
    """
    n = len(xs)
    if n < 3:
        return FitQuality("noisy", "Doğrusallık değerlendirmesi için en az 3 ölçüm gerekiyor.", 0.0)

    linear = linear_regression_with_ci(xs, ys, xs[-1])

    # Neredeyse yatay bir seri: "gürültülü" demek yanıltıcı olur, gerçekten değişmiyor.
    value_span = max(ys) - min(ys)
    if value_span == 0 or (linear.residual_std and value_span < linear.residual_std * 0.5):
        return FitQuality("flat", "Seri neredeyse sabit — anlamlı bir trend yok.", linear.r_squared)

    log_r2: float | None = None
    if all(y > 0 for y in ys):
        from math import log

        log_fit = linear_regression_with_ci(xs, [log(y) for y in ys], xs[-1])
        log_r2 = log_fit.r_squared
        if log_r2 - linear.r_squared >= EXPONENTIAL_R2_GAIN:
            return FitQuality(
                "exponential",
                "Büyüme doğrusal değil, üstel görünüyor (log dönüşümlü uyum belirgin şekilde "
                "daha iyi). Doğrusal tahmin bu durumda gerçekleşenden DAHA İYİMSER çıkar — "
                "aşağıdaki tarih büyük ihtimalle olduğundan geç.",
                linear.r_squared,
                log_r2,
            )

    if linear.r_squared < NOISY_R2:
        return FitQuality(
            "noisy",
            f"Veri bir trend çizmiyor (R²={linear.r_squared:.2f}). Dalgalanma trendden büyük; "
            "bu seriden güvenilir bir tahmin çıkmaz.",
            linear.r_squared,
            log_r2,
        )

    # Eğrilik: kalıntıların üç ardışık dilimdeki ortalamaları.
    residuals = linear.residuals
    if len(residuals) >= 6 and linear.residual_std > 0:
        third = len(residuals) // 3
        first = mean(residuals[:third])
        middle = mean(residuals[third : 2 * third])
        last = mean(residuals[2 * third :])
        curvature = abs((first + last) / 2 - middle) / linear.residual_std
        if curvature > CURVATURE_SIGMA:
            return FitQuality(
                "curved",
                "Veri düz bir doğru değil bir eğri çiziyor (büyüme hızlanıyor ya da doyuma "
                "ulaşıyor). Doğrusal tahmin bu şeklin ortasından geçer, uçlarda sapar.",
                linear.r_squared,
                log_r2,
            )

    return FitQuality("linear", "Doğrusal trend veriye uyuyor.", linear.r_squared, log_r2)


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
    # --- Faz 20 İŞ 3: yöntem ve veri şeffaflığı (kara kutu olmasın) ---
    slope_lower_per_day: float = 0.0
    slope_upper_per_day: float = 0.0
    sample_count: int = 0
    span_days: float = 0.0
    outliers_removed: int = 0
    fit: FitQuality | None = None

    @property
    def method(self) -> str:
        """Kullanıcıya gösterilen kısa yöntem adı."""
        parts = ["doğrusal regresyon"]
        if self.seasonality == "weekday":
            parts.append("haftaiçi/haftasonu düzeltmesi")
        elif self.seasonality == "hour":
            parts.append("saat bazlı düzeltme")
        if self.outliers_removed:
            parts.append(f"{self.outliers_removed} aykırı ölçüm çıkarıldı")
        return " + ".join(parts)


# Bir mevsim deseninin döngü uzunluğu. Desenin trendden AYIRT EDİLEBİLMESİ için serinin en az
# iki tam döngü içermesi gerekir (aşağıya bakın).
_CYCLE_DAYS = {"hour": 1.0, "weekday": 7.0}


def _usable_seasonality(points: list[SeasonalPoint], requested: str) -> str:
    """Mevsimsellik bu veriye gerçekten uygulanabilir mi (Faz 20 İŞ 3).

    **Bulunan hata:** saat bazlı mevsimsellik, seri bir günden kısaysa TRENDİ YUTUYORDU.
    16 saatlik yükselen bir seride her saat kovası bir kez görülür, dolayısıyla "o saatin
    ortalamadan sapması" ile "o ana kadarki artış" aynı şeydir — mevsimsel bileşen çıkarıldığında
    geriye düz bir seri kalır, eğim sıfıra iner ve tahmin ŞU ANKİ DEĞERİN ALTINA düşer.
    Somut örnek: %55'ten %82'ye tırmanan bir bağlantı serisi için bir saat sonrası %67.8
    tahmin ediliyordu — yani yükselen bir metrik için düşüş öngörülüyordu.

    Eski koruma (`len(points) < 8` ve en az 2 farklı kova) bunu yakalamıyordu: 16 saatte 17
    farklı kova ve 50 nokta var, ikisi de sağlanıyor.

    Doğru ölçüt, desenin TEKRAR ETMESİ: her mevsim kovası en az iki AYRI döngüde görülmeli
    (saat deseni için iki farklı gün, haftaiçi/haftasonu deseni için iki farklı hafta). Tek bir
    döngüde görülen kova, "o dönemin deseni" ile "o ana kadarki artış"ı ayırt edemez. Ölçüt
    sağlanmazsa düz regresyona düşülüyor — uydurma bir desen uygulamaktansa desensiz kalmak
    doğru.
    """
    if len(points) < 8 or requested == "none":
        return "none"
    cycle = _CYCLE_DAYS.get(requested)
    if cycle is None:
        return "none"

    # Her kovanın hangi DÖNGÜLERDE görüldüğü. Bir desenin trendden ayrılabilmesi için aynı
    # kovanın en az iki AYRI döngüde tekrar etmesi gerekir — tek bir döngüde görülen kova,
    # "o dönemin deseni" ile "o ana kadarki artış"ı ayırt edemez.
    t0 = points[0].timestamp
    cycles_per_bucket: dict[str, set[int]] = {}
    for p in points:
        key = _seasonal_key(p.timestamp, requested)
        cycle_index = int((p.timestamp - t0).total_seconds() / 86400.0 / cycle)
        cycles_per_bucket.setdefault(key, set()).add(cycle_index)

    if len(cycles_per_bucket) < 2:
        return "none"
    if any(len(seen) < 2 for seen in cycles_per_bucket.values()):
        return "none"
    return requested


def forecast_with_seasonality(
    points: list[SeasonalPoint], target: datetime, *, seasonality: str = "weekday"
) -> ForecastResult:
    """points: kronolojik sırayla (timestamp, value) — günlük rollup ise "weekday", saatlik/ham
    örnek ise "hour" mevsimsellik daha anlamlı olur (çağıran taraf seçer). En az 8 nokta ve en az
    2 farklı mevsim bucket'ı yoksa mevsimsellik uygulanmaz (yetersiz veriyle "mevsimsel desen"
    uydurmamak için — sadece düz regresyona düşer)."""
    seasonality = _usable_seasonality(points, seasonality)

    if seasonality == "none":
        deseasonalized = [p.value for p in points]
        offsets: dict[str, float] = {}
    else:
        deseasonalized, offsets = deseasonalize(points, seasonality)

    x0 = points[0].timestamp
    xs = [(p.timestamp - x0).total_seconds() / 86400.0 for p in points]  # gün cinsinden
    x_new = (target - x0).total_seconds() / 86400.0
    span_days = xs[-1] - xs[0] if len(xs) > 1 else 0.0

    raw_ys = [p.value for p in points]

    # Aykırı temizliği ile doğrusallık testi birbirine bağlı, bu yüzden iki geçişli:
    #
    # 1. Önce aykırılar işaretleniyor. MAD tabanlı olduğu için tek bir sıçramadan etkilenmez —
    #    oysa doğrusallık testi etkilenir: tek bir uç değer log dönüşümlü uyumu yapay olarak
    #    iyileştirip düz bir seriyi "üstel" gösterebilir.
    # 2. Uyum, KALAN noktaların HAM değerleriyle ölçülüyor. Ham, çünkü mevsimsellik çıkarma her
    #    noktadan genel ortalamaya göre bir sapma düşer; güçlü büyüyen bir seride bu erken
    #    değerleri negatife çekip log testini imkânsız kılar.
    # 3. Uyum "eğri" ya da "üstel" çıktıysa temizlik GERİ ALINIYOR: doğrusal bir modele göre bir
    #    eğrinin UÇLARI en büyük kalıntıya sahiptir, yani orada "aykırı" görünen şey gerçek
    #    veridir. Kırpmak, asıl söylenmesi gereken şeyi (bu veri doğrusal değil) gizlerdi.
    outlier_flags = detect_outliers(xs, deseasonalized)
    kept_idx = [i for i, bad in enumerate(outlier_flags) if not bad]
    fit = assess_fit([xs[i] for i in kept_idx], [raw_ys[i] for i in kept_idx])
    if fit.kind in ("curved", "exponential") and len(kept_idx) < len(xs):
        kept_idx = list(range(len(xs)))
        fit = assess_fit(xs, raw_ys)

    outliers_removed = len(xs) - len(kept_idx)
    fit_xs = [xs[i] for i in kept_idx]
    fit_ys = [deseasonalized[i] for i in kept_idx]

    reg = linear_regression_with_ci(fit_xs, fit_ys, x_new)
    offset = seasonal_offset_for(target, seasonality, offsets) if seasonality != "none" else 0.0

    return ForecastResult(
        point=reg.point + offset,
        lower=reg.lower + offset,
        upper=reg.upper + offset,
        slope_per_day=reg.slope_per_x,
        r_squared=reg.r_squared,
        seasonality=seasonality,
        slope_lower_per_day=reg.slope_lower,
        slope_upper_per_day=reg.slope_upper,
        sample_count=len(points),
        span_days=span_days,
        outliers_removed=outliers_removed,
        fit=fit,
    )


#: ETA aralığının en dar hâli — merkezin ±%10'u.
#:
#: NEDEN GEREKLİ: eğim belirsizliği regresyonun ARTIKLARINDAN hesaplanıyor. Veri kusursuz
#: doğrusalsa artıklar sıfır, belirsizlik sıfır, aralık da tek noktaya çöküyor: "39-39 gün".
#: Bu, Faz 20 İŞ 3'ün tam olarak yasakladığı şey — sahip olmadığımız bir kesinliği iddia etmek.
#:
#: Geçmişin kusursuz uyması GELECEĞİ garanti etmiyor: yük deseni değişebilir, yeni bir iş
#: eklenebilir, temizlik çalışabilir. Bu taban, "model geçmişe ne kadar iyi oturdu" ile
#: "gelecek ne kadar öngörülebilir" arasındaki farkı temsil ediyor.
MIN_ETA_RELATIVE_SPREAD = 0.10


def eta_days_range(
    distance: float, forecast: ForecastResult
) -> tuple[float | None, float | None]:
    """Bir eşiğe ulaşmanın gün cinsinden ARALIĞI (Faz 20 İŞ 3).

    `distance` = hedef değer - şimdiki değer. Tek bir nokta yerine aralık veriliyor çünkü
    eğimin kendisi belirsiz: "~52 gün" demek, sahip olmadığımız bir kesinliği iddia etmek olur.
    Hızlı uç eğimin üst sınırından, yavaş uç alt sınırından hesaplanıyor.

    Dönüş `(en_erken, en_geç)`. Eğimin alt sınırı sıfır veya negatifse "en geç" bilinemez
    (o senaryoda eşiğe hiç ulaşılmayabilir) — `None` dönüyor ve arayüz bunu "belirsiz" diye
    gösteriyor, uydurma bir üst sınır yazmıyor.

    ARALIK ASLA TEK NOKTAYA ÇÖKMEZ: bkz. `MIN_ETA_RELATIVE_SPREAD`.
    """
    if distance <= 0:
        return (0.0, 0.0)
    fastest = distance / forecast.slope_upper_per_day if forecast.slope_upper_per_day > 0 else None
    slowest = distance / forecast.slope_lower_per_day if forecast.slope_lower_per_day > 0 else None
    return _widen_to_minimum(fastest, slowest)


def _widen_to_minimum(
    fastest: float | None, slowest: float | None
) -> tuple[float | None, float | None]:
    """Aralık taban genişliğin altındaysa merkez etrafında genişletir.

    Yalnızca ÇÖKMÜŞ aralıklara dokunuyor: gerçek belirsizlik zaten tabandan genişse olduğu
    gibi bırakılıyor — hesaplanmış bir aralığı yapay olarak büyütmek, ölçümü bozmak olurdu.
    """
    if fastest is None or slowest is None:
        # Üst uç bilinmiyorsa genişletecek bir merkez de yok; "belirsiz" olarak kalıyor.
        return (fastest, slowest)
    center = (fastest + slowest) / 2
    if center <= 0:
        return (fastest, slowest)
    minimum_spread = center * MIN_ETA_RELATIVE_SPREAD
    if (slowest - fastest) >= minimum_spread:
        return (fastest, slowest)
    half = minimum_spread / 2
    return (max(0.0, center - half), center + half)


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
