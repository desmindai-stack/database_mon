"""Analiz derinliği ayarları (Faz 31 İŞ 1c).

`noise_settings.py` "ne listede görünür" sorusunu cevaplıyor; bu modül "dbace bir sorgu için
ne zaman ve ne kadar derin analiz yapar" sorusunu. İkisi ayrı çünkü eşikleri farklı
şeyleri koruyor: gürültü eşiği DBA'nın dikkatini, analiz eşiği önerinin istatistiksel
temelini.

Faz 31 İŞ 2: gerçek değerli sorgu metni saklama anahtarı da burada — üçü de "dbace ne kadar
derin analiz eder ve ne saklar" sorusuna ait. Gizlilik gerekçesi SORULAR.md'de.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting

INDEX_ADVICE_MIN_CALLS_KEY = "analysis_index_advice_min_calls"
INDEX_ADVICE_WATCH_ENABLED_KEY = "analysis_index_advice_watch_enabled"
STORE_REAL_QUERY_SAMPLES_KEY = "analysis_store_real_query_samples"

DEFAULTS: dict[str, Any] = {
    # Bir index önerisinin dayandığı asgari çağrı sayısı. Tek haneli bir sayı "henüz veri
    # birikmedi" ile "gerçekten nadir çalışan sorgu" arasındaki en makul ayrım; kesin bir
    # doğru değer yok, bu yüzden ayarlanabilir.
    "index_advice_min_calls": 5,
    # Eşik altındaki sorgular izlemeye alınıp eşik dolunca öneri otomatik üretilsin mi.
    "index_advice_watch_enabled": True,
    # GERÇEK DEĞERLİ sorgu metni saklama. VARSAYILAN KAPALI: pg_stat_activity'den okunan metin
    # uygulamanın gömdüğü değerleri (kimlik no, e-posta, tutar) içerebilir. Kapalıyken sözlüğe
    # normalize metin yazılıyor ve örnek saklanmıyor; açık olması DBA'nın bilinçli kararı.
    "store_real_query_samples": False,
}

LIMITS = {
    # 1: her çalışmış sorgu için hemen öneri. Üst sınır saçma bir değerin (ör. 10^9) hiçbir
    # sorgunun eşiği geçemediği sessiz bir kapanmaya dönüşmesini engelliyor.
    "index_advice_min_calls": (1, 100_000),
}


async def _get(session: AsyncSession, key: str) -> str | None:
    row = await session.get(AppSetting, key)
    return row.value if row else None


async def _set(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


async def get_analysis_settings(session: AsyncSession) -> dict[str, Any]:
    """Geçerli ayarlar. Bozuk/eksik kayıt varsayılana düşer — ayar hatası analizi
    durdurmamalı."""
    raw_calls = await _get(session, INDEX_ADVICE_MIN_CALLS_KEY)
    try:
        min_calls = int(raw_calls) if raw_calls is not None else DEFAULTS["index_advice_min_calls"]
    except ValueError:
        min_calls = DEFAULTS["index_advice_min_calls"]
    low, high = LIMITS["index_advice_min_calls"]
    if not low <= min_calls <= high:
        min_calls = DEFAULTS["index_advice_min_calls"]

    raw_watch = await _get(session, INDEX_ADVICE_WATCH_ENABLED_KEY)
    watch_enabled = DEFAULTS["index_advice_watch_enabled"] if raw_watch is None else raw_watch == "true"

    raw_store = await _get(session, STORE_REAL_QUERY_SAMPLES_KEY)

    return {
        "index_advice_min_calls": min_calls,
        "index_advice_watch_enabled": watch_enabled,
        # Bozuk değer AÇIK sayılmıyor: gizlilik anahtarında belirsizlik kapalı demek.
        "store_real_query_samples": raw_store == "true",
        "defaults": dict(DEFAULTS),
    }


async def set_analysis_settings(
    session: AsyncSession,
    *,
    index_advice_min_calls: int | None = None,
    index_advice_watch_enabled: bool | None = None,
    store_real_query_samples: bool | None = None,
) -> dict[str, Any]:
    if index_advice_min_calls is not None:
        low, high = LIMITS["index_advice_min_calls"]
        if not low <= index_advice_min_calls <= high:
            raise ValueError(
                f"index_advice_min_calls {low} ile {high} arasında olmalı (verilen: {index_advice_min_calls})"
            )
        await _set(session, INDEX_ADVICE_MIN_CALLS_KEY, str(index_advice_min_calls))
    if index_advice_watch_enabled is not None:
        await _set(session, INDEX_ADVICE_WATCH_ENABLED_KEY, "true" if index_advice_watch_enabled else "false")
    if store_real_query_samples is not None:
        await _set(session, STORE_REAL_QUERY_SAMPLES_KEY, "true" if store_real_query_samples else "false")
    await session.commit()
    if store_real_query_samples is False:
        # Kapatmak "bundan sonra saklama" DEĞİL, "saklananı da sil" demek — yoksa anahtar
        # kullanıcının sandığı şeyi yapmazdı. Döngüsel içe aktarmayı önlemek için yerel.
        from app.services.wait_sampling import enforce_query_text_privacy

        await enforce_query_text_privacy(session)
    return await get_analysis_settings(session)
