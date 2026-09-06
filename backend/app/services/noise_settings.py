"""Gürültü filtresi ayarları (Faz 18 İŞ 2).

Rapor ve DPA, istatistiksel olarak "değişmiş" ama pratikte önemsiz şeyleri bulgu olarak
sunuyordu — bildirilen örnek: dönemde 1 çağrı, 206 ms harcayan bir Supabase iç sorgusu
"%+229 arttı" diye raporlanıyordu.

Eşikler koda gömülü olsaydı her ortam için doğru olamazdı: OLTP'de 1000 ms'lik bir sorgu
ciddi, raporlama veritabanında sıradan. Bu yüzden ayarlanabilirler ve TEK yerden okunuyorlar
— rapor ile DPA'nın farklı eşikler kullanması, Faz 18 İŞ 1'de düzelttiğimiz tutarsızlığın
aynısını geri getirirdi.

İki farklı eşik kümesi var, çünkü iki farklı soru soruyorlar:

* **Liste eşikleri** (`list_*`) — "bu sorgu en pahalı N listesinde görünmeye değer mi?"
  Düşük tutulur; liste bir keşif aracı.
* **Bulgu eşikleri** (`finding_*`) — "DBA'nın bugün buna bakması gerekir mi?" Yüksek tutulur;
  her bulgu bir dikkat talebidir.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting

LIST_MIN_TOTAL_MS_KEY = "noise_list_min_total_ms"
LIST_MIN_CALLS_KEY = "noise_list_min_calls"
FINDING_MIN_TOTAL_MS_KEY = "noise_finding_min_total_ms"
FINDING_MIN_CALLS_KEY = "noise_finding_min_calls"
SHOW_SYSTEM_QUERIES_KEY = "noise_show_system_queries"

DEFAULTS: dict[str, Any] = {
    # Listeye girmek için: 100 ms toplam. Altındakiler ölçüm gürültüsü sayılır.
    "list_min_total_ms": 100.0,
    "list_min_calls": 1,
    # Bulgu üretmek için: 1 saniye toplam VE en az 5 çağrı. Tek çağrılık bir sorgudan
    # yüzde değişimi anlamsızdır (bildirilen hatanın tam olarak bu yanıydı).
    "finding_min_total_ms": 1000.0,
    "finding_min_calls": 5,
    # Sistem/platform sorguları varsayılan olarak gizli.
    "show_system_queries": False,
}

# Ayarların kabul edilebilir aralıkları — uçta doğrulanıyor, saçma bir değer kaydedilemiyor.
LIMITS = {
    "list_min_total_ms": (0.0, 3_600_000.0),
    "list_min_calls": (0, 1_000_000),
    "finding_min_total_ms": (0.0, 3_600_000.0),
    "finding_min_calls": (0, 1_000_000),
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


def _as_float(raw: str | None, fallback: float) -> float:
    try:
        return float(raw) if raw is not None else fallback
    except ValueError:
        return fallback


def _as_int(raw: str | None, fallback: int) -> int:
    try:
        return int(raw) if raw is not None else fallback
    except ValueError:
        return fallback


async def get_noise_settings(session: AsyncSession) -> dict[str, Any]:
    """Geçerli eşikler. Bozuk/eksik bir kayıt varsayılana düşer — ayar hatası yüzünden rapor
    üretiminin çökmesi, yanlış eşikle çalışmaktan daha kötü olurdu."""
    return {
        "list_min_total_ms": _as_float(
            await _get(session, LIST_MIN_TOTAL_MS_KEY), DEFAULTS["list_min_total_ms"]
        ),
        "list_min_calls": _as_int(await _get(session, LIST_MIN_CALLS_KEY), DEFAULTS["list_min_calls"]),
        "finding_min_total_ms": _as_float(
            await _get(session, FINDING_MIN_TOTAL_MS_KEY), DEFAULTS["finding_min_total_ms"]
        ),
        "finding_min_calls": _as_int(
            await _get(session, FINDING_MIN_CALLS_KEY), DEFAULTS["finding_min_calls"]
        ),
        "show_system_queries": (await _get(session, SHOW_SYSTEM_QUERIES_KEY)) == "true",
        "defaults": dict(DEFAULTS),
    }


async def set_noise_settings(
    session: AsyncSession,
    *,
    list_min_total_ms: float | None = None,
    list_min_calls: int | None = None,
    finding_min_total_ms: float | None = None,
    finding_min_calls: int | None = None,
    show_system_queries: bool | None = None,
) -> dict[str, Any]:
    updates = {
        LIST_MIN_TOTAL_MS_KEY: ("list_min_total_ms", list_min_total_ms),
        LIST_MIN_CALLS_KEY: ("list_min_calls", list_min_calls),
        FINDING_MIN_TOTAL_MS_KEY: ("finding_min_total_ms", finding_min_total_ms),
        FINDING_MIN_CALLS_KEY: ("finding_min_calls", finding_min_calls),
    }
    for key, (name, value) in updates.items():
        if value is None:
            continue
        low, high = LIMITS[name]
        if not low <= value <= high:
            raise ValueError(f"{name} {low} ile {high} arasında olmalı (verilen: {value})")
        await _set(session, key, str(value))

    if show_system_queries is not None:
        await _set(session, SHOW_SYSTEM_QUERIES_KEY, "true" if show_system_queries else "false")

    await session.commit()
    return await get_noise_settings(session)
