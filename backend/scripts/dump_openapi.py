"""OpenAPI şemasını dosyaya yazar — TypeScript tip üretiminin girdisi (Faz 21 İŞ 2).

Neden ayrı bir betik: tipleri üretmek için sunucuyu ayağa kaldırıp `/openapi.json`'a HTTP
isteği atmak gerekmiyor. `app.openapi()` şemayı doğrudan üretiyor. Bunun iki faydası var:

* **CI'da çalışır.** Sürüklenme kontrolü (tipler yeniden üretilip commit'lenmiş hâliyle
  karşılaştırılıyor) sunucu, port, sağlık beklemesi gerektirmeden koşuyor.
* **Deterministik.** Aynı kod, aynı çıktı — ağ ya da zamanlama devrede değil.

Kullanım:

    cd backend && python scripts/dump_openapi.py ../frontend/openapi.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_OUT = Path(__file__).resolve().parents[2] / "frontend" / "openapi.json"


def _utf8_stdout() -> None:
    """Windows'ta konsol varsayılanı cp1252; Türkçe karakter yazınca UnicodeEncodeError verip
    betiği düşürüyor (dosya çoktan yazılmış olsa bile). CI Linux/UTF-8 olduğu için orada
    görünmez — yani tam olarak "yerelde patlar, CI'da geçer" durumu."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")



def main() -> None:
    _utf8_stdout()
    from app.main import app

    out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_OUT
    out.parent.mkdir(parents=True, exist_ok=True)

    spec = app.openapi()
    # `sort_keys` + sabit girinti: çıktı anahtar sırasına göre oynamasın, yoksa CI'daki
    # sürüklenme kontrolü kod değişmeden de kırmızı olurdu.
    out.write_text(
        json.dumps(spec, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    paths = len(spec.get("paths", {}))
    schemas = len(spec.get("components", {}).get("schemas", {}))
    print(f"{out} yazıldı ({paths} uç, {schemas} şema)")


if __name__ == "__main__":
    main()
