"""Örnekleme aralığı değerlendirmesi — ekran ve log AYNI kuralı kullanır (Faz 31 Commit 10a).

Bekleme analizi ÖRNEKLEMEYE dayanıyor: AAS = aktif oturum toplamı / alınan örnek sayısı. Örnekler eşit aralıklı
değilse (ya da hedeflenenden seyrekse) ölçüm bozulur: 1 saniyelik hedefle 2 saniyede bir örnek alan bir örnekleyici
200 ms süren bir kilit fırtınasını yarı yarıya kaçırır, üstelik AAS hâlâ makul bir sayı gibi görünür. Bu yüzden
hedeflenen aralık ile ÖLÇÜLEN aralık ayrı tutuluyor ve ikisi ayrıştığında bu sessiz geçilmiyor.

Kural (tek yerde, testle korunuyor):
- ölçülen ortalama aralık hedefin `CADENCE_TOLERANCE` (1,25) katını aşarsa: "tutturulamadı".
- en uzun boşluk hedefin `LONG_GAP_FACTOR` (5) katını aşarsa: "düzensiz" (ortalama iyi görünse bile; örneğin
  30 saniyelik bir kesinti 60 saniyelik dakikada ortalamayı yalnızca 1,5 katına çıkarır ama o 30 saniye ölçülmemiştir).

Ölçüm yoksa (`measured_interval_ms is None`) hüküm de yok: "ölçüm yok" ile "aralık tuttu" farklı şeyler.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Ölçülen ortalama aralık hedefin bu katını aşarsa hedef tutturulamamış sayılır. %25 pay: zamanlayıcı
#: titreşimi (~±10 ms) ve dakika kenarındaki kısmi dakikalar alarm üretmesin.
CADENCE_TOLERANCE = 1.25

#: Tek bir boşluk hedefin bu katını aşarsa örnekleme "düzensiz" sayılır.
LONG_GAP_FACTOR = 5.0


@dataclass
class Cadence:
    target_interval_ms: int
    measured_interval_ms: int | None = None
    max_gap_ms: int | None = None
    #: Ortalama aralık hedefi tutturamadı.
    missed: bool = False
    #: Ortalama iyi olsa bile hedefin çok katı uzunlukta bir boşluk var.
    irregular: bool = False
    #: Kullanıcıya gösterilecek cümle; sorun yoksa None.
    message: str | None = None


def assess(*, target_interval_seconds: float, measured_interval_ms: float | None, max_gap_ms: float | None) -> Cadence:
    target_ms = int(round(max(target_interval_seconds, 1) * 1000))
    cadence = Cadence(
        target_interval_ms=target_ms,
        measured_interval_ms=None if measured_interval_ms is None else int(round(measured_interval_ms)),
        max_gap_ms=None if max_gap_ms is None else int(round(max_gap_ms)),
    )
    if cadence.measured_interval_ms is not None:
        cadence.missed = cadence.measured_interval_ms > target_ms * CADENCE_TOLERANCE
    if cadence.max_gap_ms is not None:
        cadence.irregular = cadence.max_gap_ms > target_ms * LONG_GAP_FACTOR

    if cadence.missed:
        cadence.message = (
            f"Örnekleme aralığı tutturulamadı (ölçülen: {cadence.measured_interval_ms} ms, hedef: {target_ms} ms). "
            "Bu aralıkta veritabanı yükü hedeflenenden daha seyrek örnekle hesaplandı; hedef aralıktan kısa süren "
            "olaylar gözden kaçmış olabilir."
        )
        if cadence.irregular:
            cadence.message += f" En uzun boşluk: {cadence.max_gap_ms} ms."
    elif cadence.irregular:
        cadence.message = (
            f"Örnekleme düzensiz: en uzun boşluk {cadence.max_gap_ms} ms (hedef aralık {target_ms} ms). "
            "Ortalama aralık tutmuş görünse de bu boşlukta neler olduğu ölçülemedi."
        )
    return cadence
