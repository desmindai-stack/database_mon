"""Bakım pencereleri (Faz 28 İŞ 3).

## Neden gerekli

Bakım penceresi olmadan erişilebilirlik sayıları **dürüst değil**: planlı bir bakım için
alınan 40 dakikalık kesinti, plansız bir arızayla aynı kefeye giriyor ve aylık %99.9
hedefini tek başına deliyor. Müşteriye "bu ay SLA'yı tutturamadınız" demek, o kesintiyi
müşterinin kendisi onayladıysa yanlış bir suçlama.

Ters yönü de aynı ölçüde önemli: her kesintiyi "planlıydı" diye etiketlemek de sayıyı
yalancı yapar. Bu yüzden pencere **önceden tanımlanmış** olmak zorunda ve kim tanımladı
kaydediliyor.

## Tekrar (recurrence)

Bakımlar çoğunlukla düzenli: "her salı 02:00-04:00" ya da "her ayın ilk pazarı".
Tekrarlayan pencereyi tek tek kayıt olarak açmak, altı ay sonra 26 satır demek olurdu ve
biri değiştiğinde hepsini düzeltmek gerekirdi. Bunun yerine kural saklanıyor ve **sorgu
anında** genişletiliyor.

Genişletme penceresi bilinçli olarak sınırlı (`MAX_OCCURRENCES`): kuralı yanlış girilmiş
bir pencere (ör. bitiş < başlangıç) sonsuz döngüye dönüşmemeli.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class MaintenanceRecurrence(StrEnum):
    """Tekrar biçimi."""

    NONE = "none"
    DAILY = "daily"
    WEEKLY = "weekly"
    #: Ayın aynı GÜN NUMARASINDA. Ayın "ilk pazarı" gibi kurallar bilinçli olarak yok:
    #: kullanıcıya sunulacak arayüz karmaşıklığı, kazanılan esnekliğe değmiyor ve yanlış
    #: anlaşılan bir kural, olmayan bir bakım penceresi demek.
    MONTHLY = "monthly"


class OutageKind(StrEnum):
    PLANNED = "planned"
    UNPLANNED = "unplanned"


OUTAGE_KIND_LABELS: dict[str, str] = {
    OutageKind.PLANNED: "Planlı",
    OutageKind.UNPLANNED: "Plansız",
}

RECURRENCE_LABELS: dict[str, str] = {
    MaintenanceRecurrence.NONE: "Tek seferlik",
    MaintenanceRecurrence.DAILY: "Her gün",
    MaintenanceRecurrence.WEEKLY: "Her hafta",
    MaintenanceRecurrence.MONTHLY: "Her ay",
}

#: Bir tekrar kuralı bir sorgu aralığında en fazla bu kadar kez genişletiliyor.
#:
#: Sınır bir performans önlemi değil, YANLIŞ VERİYE KARŞI korunma: bitişi başlangıcından
#: önce olan ya da süresi sıfır olan bir kural sonsuz döngü üretirdi. Bir yıllık bir SLA
#: sorgusunda günlük tekrar 365 kez genişler; sınır onun üstünde.
MAX_OCCURRENCES = 500

#: Bakım penceresine bu kadar tolerans ekleniyor.
#:
#: Bakım 02:00'de başlıyorsa servis 01:59'da durmuş olabilir; toplama aralığı da 15 saniye
#: değil dakikalar mertebesinde olabilir. Tolerans olmadan kesintinin baş tarafı "plansız"
#: sayılır ve tek bir bakım iki parçaya bölünürdü. Tolerans tek yönlü büyük tutulmuyor:
#: geniş bir tolerans, pencere dışındaki gerçek bir arızayı planlı göstermeye başlar.
WINDOW_TOLERANCE_SECONDS = 300.0


@dataclass(frozen=True)
class Occurrence:
    """Tekrar kuralının somut bir örneği."""

    start: datetime
    end: datetime

    def covers(self, moment: datetime, tolerance: float = WINDOW_TOLERANCE_SECONDS) -> bool:
        return (
            self.start - timedelta(seconds=tolerance)
            <= moment
            <= self.end + timedelta(seconds=tolerance)
        )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _add_months(moment: datetime, months: int) -> datetime:
    """Aylık tekrar için ay ekler; ayın sonunu taşırmaz.

    31'inde tanımlanmış bir bakım şubatta 31 Şubat olamaz. Bir sonraki aya taşırmak
    ("3 Mart") kullanıcının kastettiği şey değil; ayın son gününe sabitlemek ise en yakın
    dürüst yorum.
    """
    month_index = moment.month - 1 + months
    year = moment.year + month_index // 12
    month = month_index % 12 + 1
    # Ayın son günü: bir sonraki ayın ilk gününden bir gün geri.
    if month == 12:
        next_month_first = datetime(year + 1, 1, 1, tzinfo=moment.tzinfo)
    else:
        next_month_first = datetime(year, month + 1, 1, tzinfo=moment.tzinfo)
    last_day = (next_month_first - timedelta(days=1)).day
    return moment.replace(year=year, month=month, day=min(moment.day, last_day))


def expand_occurrences(
    starts_at: datetime,
    ends_at: datetime,
    recurrence: str,
    window_start: datetime,
    window_end: datetime,
) -> list[Occurrence]:
    """Tekrar kuralını verilen aralıkta somut örneklere açar.

    Kural tanımının KENDİSİ ilk örnektir; tekrar oradan ileriye doğru üretiliyor. Geriye
    doğru üretilmiyor: bir bakım penceresi tanımlanmadan önceki kesintileri geçmişe dönük
    "planlı" saymak, sayıyı istediğin gibi düzeltebilmek demek olurdu.
    """
    starts_at = _as_utc(starts_at)
    ends_at = _as_utc(ends_at)
    window_start = _as_utc(window_start)
    window_end = _as_utc(window_end)
    if ends_at <= starts_at:
        # Bozuk tanım: sessizce "hiç pencere yok" demek, bozukluğu gizlemek olurdu ama
        # burada üretilecek doğru bir şey de yok. Doğrulama giriş katmanında yapılıyor.
        return []

    duration = ends_at - starts_at
    if recurrence == str(MaintenanceRecurrence.NONE) or not recurrence:
        occurrence = Occurrence(starts_at, ends_at)
        return [occurrence] if _overlaps(occurrence, window_start, window_end) else []

    # İLERİ SARMA. Kural aylar önce başlamış olabilir (ör. "her gün 02:00", iki yıl önce
    # tanımlanmış). Örnekleri baştan tek tek üretmek MAX_OCCURRENCES sınırına sorgulanan
    # aralığa VARMADAN takılırdı — yani eski bir bakım penceresi sessizce hiç uygulanmazdı.
    # Sınır bir güvenlik önlemi olarak kalıyor, ilerleme aracı olarak değil.
    occurrences: list[Occurrence] = []
    monthly = recurrence == str(MaintenanceRecurrence.MONTHLY)
    # Aylık tekrarda adım, bir öncekinden değil HER ZAMAN tanımdan hesaplanıyor. Zincirleme
    # eklemek kayma üretirdi: 31 Ocak → 29 Şubat → 29 Mart. Oysa kullanıcının kastettiği
    # "ayın 31'i (yoksa son günü)"; ay indeksi tanımdan sayılınca 31 Mart doğru çıkıyor.
    month_index = _month_offset(starts_at, _fast_forward(starts_at, duration, recurrence, window_start)) if monthly else 0
    cursor = _add_months(starts_at, month_index) if monthly else _fast_forward(
        starts_at, duration, recurrence, window_start
    )
    for _ in range(MAX_OCCURRENCES):
        if cursor > window_end:
            break
        occurrence = Occurrence(cursor, cursor + duration)
        if _overlaps(occurrence, window_start, window_end):
            occurrences.append(occurrence)
        if recurrence == str(MaintenanceRecurrence.DAILY):
            cursor = cursor + timedelta(days=1)
        elif recurrence == str(MaintenanceRecurrence.WEEKLY):
            cursor = cursor + timedelta(days=7)
        elif monthly:
            month_index += 1
            cursor = _add_months(starts_at, month_index)
        else:
            break
    return occurrences


def _month_offset(origin: datetime, moment: datetime) -> int:
    return (moment.year - origin.year) * 12 + (moment.month - origin.month)


def _fast_forward(
    starts_at: datetime, duration: timedelta, recurrence: str, window_start: datetime
) -> datetime:
    """Sorgulanan aralıktan önceki son örneğe atlar.

    Bir önceki örnekten başlanıyor (`- 1`), çünkü aralığın başına SARKAN bir örnek de
    örtüşüyor sayılmalı: 23:00-01:00 arası bir bakım, ertesi günün sorgusunda da geçerli.
    """
    if window_start <= starts_at:
        return starts_at
    if recurrence == str(MaintenanceRecurrence.DAILY):
        period = timedelta(days=1)
    elif recurrence == str(MaintenanceRecurrence.WEEKLY):
        period = timedelta(days=7)
    elif recurrence == str(MaintenanceRecurrence.MONTHLY):
        months = (window_start.year - starts_at.year) * 12 + (window_start.month - starts_at.month)
        return _add_months(starts_at, max(months - 1, 0))
    else:
        return starts_at
    steps = int((window_start - duration - starts_at) / period)
    return starts_at + period * max(steps - 1, 0)


def _overlaps(occurrence: Occurrence, window_start: datetime, window_end: datetime) -> bool:
    return occurrence.start <= window_end and occurrence.end >= window_start


def overlap_seconds(
    outage_start: datetime, outage_end: datetime, occurrences: list[Occurrence]
) -> float:
    """Kesintinin bakım pencereleriyle ÖRTÜŞEN saniye sayısı.

    Kesintiyi bütün olarak "planlı" ya da "plansız" damgalamak yerine örtüşme ölçülüyor:
    bakım 02:00-04:00 iken 03:30'da başlayıp 06:00'a kadar süren bir kesinti yarı planlı
    yarı plansızdır. Hepsini planlı saymak arızayı gizler, hepsini plansız saymak da
    onaylanmış bakımı ceza olarak yazar.
    """
    outage_start = _as_utc(outage_start)
    outage_end = _as_utc(outage_end)
    total = 0.0
    for occurrence in occurrences:
        start = max(outage_start, occurrence.start - timedelta(seconds=WINDOW_TOLERANCE_SECONDS))
        end = min(outage_end, occurrence.end + timedelta(seconds=WINDOW_TOLERANCE_SECONDS))
        if end > start:
            total += (end - start).total_seconds()
    # Örtüşme kesintinin kendisinden uzun olamaz (tolerans yüzünden taşabilir).
    return min(total, max((outage_end - outage_start).total_seconds(), 0.0))


def classify_outage(
    outage_start: datetime, outage_end: datetime, occurrences: list[Occurrence]
) -> tuple[str, float, float]:
    """(tür, planlı saniye, plansız saniye).

    Tür, sürenin ÇOĞUNLUĞUNA göre belirleniyor; kırılım ayrıca döndürülüyor çünkü SLA
    hesabı tek bir etikete değil sürelere bakıyor.
    """
    total = max((_as_utc(outage_end) - _as_utc(outage_start)).total_seconds(), 0.0)
    planned = overlap_seconds(outage_start, outage_end, occurrences)
    unplanned = max(total - planned, 0.0)
    kind = OutageKind.PLANNED if planned >= unplanned and planned > 0 else OutageKind.UNPLANNED
    return str(kind), planned, unplanned
