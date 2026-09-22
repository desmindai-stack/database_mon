"""Bekleme örnekleyicisinin bağlantı durumu — ana toplama döngüsünden AYRI (Faz 31 Commit 10c-B).

## Sorun

RTT ≥ ~1 sn olan bir hedefte örnekleyici hiç bağlanamıyor (sunucu tarafı `statement_timeout` kurulum sorgusunu
iptal ediyor — bkz. `services/wait_sampling.py` docstring, Faz 31 Commit 10a). Bu durumda `_sample_instance` her
turda istisna alıp döner; hiçbir `ActiveSessionMinute`/`WaitSampleMinute` satırı yazılmaz. `database_load.py`
ve bloklama geçmişi ekranı, o aralıkta satır bulamayınca "bu aralıkta örnek/olay yok" diyordu — bu cümle
"örnekleyici sağlıklı, gerçekten yük yok" ile "örnekleyici bağlanamıyor" arasında hiçbir ayrım yapmıyordu; ikincisi
"sorun yok" gibi bir güvence verirdi.

## Çözüm

`Instance.last_sample_ok_at` / `last_sample_error` / `last_sample_error_at` — yalnızca DURUM DEĞİŞİMİNDE yazılır
(arıza başlangıcı: ardışık hata sayacı 0→1; toparlanma: >0→0). 1 saniyelik döngüde her turda yazmak egress'i
katbekat artırırdı (Faz 31 Commit 9a/10a dersleri); bir arıza olayı başına en fazla 2 yazım (başlangıç + bitiş),
süresinden bağımsız. Okuma tarafı tek kural: hata zaman damgası, başarı zaman damgasından daha YENİYSE şu an
bozuk demektir — süregelen bir arızada bu karşılaştırma sonsuza dek doğru kalır, periyodik tazeleme gerekmez.

`sampling_status_for()` bu kuralı TEK yerde uyguluyor; `database_load.py` ve bloklama geçmişi ucu (aynı sınıf
bug, aynı sampler) buradan besleniyor — ayrı hesaplama ayrı sonuç riski taşımasın diye.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

#: Kayıtlı hata metninin kırpıldığı uzunluk (last_collect_error ile aynı sınır — services/collection_status.py).
MAX_ERROR_LENGTH = 500


@dataclass
class SamplingHealth:
    #: Şu an bilinen arıza var mı (son hata, son başarıdan daha yeni ya da hiç başarı yok).
    broken: bool
    #: Kullanıcıya gösterilecek "ölçülemedi: <neden>" cümlesi; broken=False ise None.
    reason: str | None = None
    error_at: datetime | None = None
    ok_at: datetime | None = None


def sampling_status_for(instance) -> SamplingHealth:
    """Instance satırındaki alanlardan (last_sample_error*, last_sample_ok_at) durumu türetir.

    Hiç bilgi yoksa (yeni instance, sampler henüz hiç denemedi) `broken=False` döner — "henüz örneklenmedi"
    ile "bağlanamıyor" farklı şeyler; ikincisini iddia etmek için en az bir başarısız deneme görülmüş olmalı.
    """
    error_at = getattr(instance, "last_sample_error_at", None)
    ok_at = getattr(instance, "last_sample_ok_at", None)
    error = getattr(instance, "last_sample_error", None)
    if error_at is None or error is None:
        return SamplingHealth(broken=False)
    if ok_at is not None and ok_at >= error_at:
        return SamplingHealth(broken=False, error_at=error_at, ok_at=ok_at)
    return SamplingHealth(broken=True, reason=error, error_at=error_at, ok_at=ok_at)


def unavailable_message(health: SamplingHealth, *, context: str) -> str:
    """'{context}' -> tam cümle. `context` örn. 'Bu aralıkta hiç bekleme örneği' / 'Bu dönemde bloklama olayı'."""
    when = health.error_at.strftime("%Y-%m-%d %H:%M UTC") if health.error_at else "bilinmiyor"
    return (
        f"Ölçülemedi: bekleme örnekleyicisi bu veritabanına bağlanamıyor (son deneme: {when}) — {health.reason} "
        f"'{context} yok' demek burada YANLIŞ olurdu: hiçbir şey ölçülemedi, veri eksikliği değil bağlantı sorunu."
    )
