"""Analiz derinliği ayarları (Faz 31 İŞ 1c).

`noise_settings.py` "ne listede görünür" sorusunu cevaplıyor; bu modül "dbace bir sorgu için
ne zaman ve ne kadar derin analiz yapar" sorusunu. İkisi ayrı çünkü eşikleri farklı
şeyleri koruyor: gürültü eşiği DBA'nın dikkatini, analiz eşiği önerinin istatistiksel
temelini.

Faz 31 İŞ 2 bu modüle gerçek değerli sorgu örneği saklama anahtarını ekleyecek — üçü de
"dbace ne kadar derin analiz eder ve ne saklar" sorusuna ait.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting

INDEX_ADVICE_MIN_CALLS_KEY = "analysis_index_advice_min_calls"
INDEX_ADVICE_WATCH_ENABLED_KEY = "analysis_index_advice_watch_enabled"

DEFAULTS: dict[str, Any] = {
    # Bir index önerisinin dayandığı asgari çağrı sayısı. Tek haneli bir sayı "henüz veri
    # birikmedi" ile "gerçekten nadir çalışan sorgu" arasındaki en makul ayrım; kesin bir
    # doğru değer yok, bu yüzden ayarlanabilir.
    "index_advice_min_calls": 5,
    # Eşik altındaki sorgular izlemeye alınıp eşik dolunca öneri otomatik üretilsin mi.
    "index_advice_watch_enabled": True,
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

    return {
        "index_advice_min_calls": min_calls,
        "index_advice_watch_enabled": watch_enabled,
        "defaults": dict(DEFAULTS),
    }


async def set_analysis_settings(
    session: AsyncSession,
    *,
    index_advice_min_calls: int | None = None,
    index_advice_watch_enabled: bool | None = None,
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
    await session.commit()
    return await get_analysis_settings(session)
