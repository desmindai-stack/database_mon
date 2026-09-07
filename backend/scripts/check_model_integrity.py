"""Import zamanı bütünlük kontrolü — CI'ın ilk kapısı (Faz 21 İŞ 1).

Bu betik, üç kez "yerelde yeşil, canlıda patlak" yaşamamıza yol açan hata sınıfını, testlerin
tamamını çalıştırmadan, saniyeler içinde yakalar:

1. **`AdviceOut` ileri referansı** — `schemas.py`'de bir tip, kendisini KULLANAN modelden sonra
   tanımlanmıştı. Python 3.14 (PEP 649) annotation'ları ertelemeli değerlendirdiği için yerelde
   sessizce geçti; canlıdaki 3.12 import anında `NameError` verdi ve API tamamen çöktü.
2. **`PredictionOut.advice`** — şemada alan vardı, modelde karşılığı yoktu: API her tahminde
   `null` döndü.
3. **`ReportFindingOut.facts` / `.note`** — modelde kolon vardı, şemada alan yoktu: Pydantic
   sessizce kırptı, arayüz `undefined` üzerinden çöktü.

Üçünün ortak yanı: uygulama AYAĞA KALKIYOR ama sözleşme bozuk. Bu yüzden yalnızca "import
edilebiliyor mu" yetmiyor; modellerin GERÇEKTEN kurulabildiği de doğrulanıyor.

CI bu betiği canlıyla aynı Python sürümünde (3.12) çalıştırır. Yerelde de çalıştırılabilir:

    cd backend && python scripts/check_model_integrity.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _fail(message: str) -> None:
    print(f"BAŞARISIZ: {message}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    # --- 1. Uygulama import edilebiliyor mu ------------------------------------------------
    # Canlıdaki çöküş tam olarak burada oluyordu: `from app.main import app` NameError veriyor,
    # container ayağa kalkmıyordu.
    try:
        from app.main import app  # noqa: F401
    except Exception as exc:  # pragma: no cover - CI'da görünür olması yeterli
        _fail(f"app.main import edilemedi: {type(exc).__name__}: {exc}")

    print(f"[1/3] app.main import edildi (Python {sys.version.split()[0]})")

    # --- 2. Her Pydantic modeli tam kurulmuş mu --------------------------------------------
    # Python 3.14'te ertelemeli annotation değerlendirmesi yüzünden eksik bir ileri referans
    # `__pydantic_complete__ = False` olarak sessizce kalır; model ilk kullanımda yeniden
    # kurulmaya çalışılır. 3.12'de aynı durum import anında patlar. İkisini de burada
    # yakalıyoruz — sürümden bağımsız.
    import inspect

    from pydantic import BaseModel

    from app import schemas

    incomplete = [
        name
        for name, obj in inspect.getmembers(schemas)
        if inspect.isclass(obj)
        and issubclass(obj, BaseModel)
        and obj is not BaseModel
        and not getattr(obj, "__pydantic_complete__", True)
    ]
    if incomplete:
        _fail(
            "şu Pydantic modelleri tam kurulamadı (çözülemeyen ileri referans): "
            + ", ".join(sorted(incomplete))
        )

    model_count = sum(
        1
        for _name, obj in inspect.getmembers(schemas)
        if inspect.isclass(obj) and issubclass(obj, BaseModel) and obj is not BaseModel
    )
    print(f"[2/3] {model_count} Pydantic modelinin tamamı tam kurulmuş")

    # --- 3. OpenAPI şeması üretilebiliyor mu ------------------------------------------------
    # Bir response_model çözülemiyorsa `app.openapi()` patlar. Bu aynı zamanda İŞ 2'deki
    # TypeScript tip üretiminin dayandığı çıktı: burada kırıksa tip üretimi de kırılır.
    try:
        spec = app.openapi()
    except Exception as exc:  # pragma: no cover
        _fail(f"OpenAPI şeması üretilemedi: {type(exc).__name__}: {exc}")

    path_count = len(spec.get("paths", {}))
    schema_count = len(spec.get("components", {}).get("schemas", {}))
    if path_count == 0:
        _fail("OpenAPI şemasında hiç uç yok — router'lar bağlanmamış olabilir")
    print(f"[3/3] OpenAPI şeması üretildi ({path_count} uç, {schema_count} şema)")

    print("Model bütünlüğü kontrolü geçti.")


if __name__ == "__main__":
    main()
