"""Faz 19 İŞ 2 — uçtan uca gezinme denetiminde bulunan iki hata sınıfı.

Denetimde her sayfa için dört soru soruldu: sayfa açılıyor mu, veri yokken ne oluyor, hata
durumunda ne oluyor, geri dönüş çalışıyor mu. İki soru sistematik olarak yanlış cevaplanıyordu:

**(1) "Veri yok" ile "yüklenemedi" aynı görünüyordu.** Liste sayfalarının hepsi boş tabloya
`<td className="empty">Kayıt yok</td>` basıyordu — ve bunu YÜKLEME BAŞARISIZ OLDUĞUNDA DA
basıyordu. Yani API düştüğünde kullanıcı "Kayıtlı instance yok" okuyup gerçekten kayıt
olmadığına inanıyordu. Bir izleme aracında bu, sessiz yanlış bilgilendirmedir.

**(2) Yazma işlemleri sessizce başarısız oluyordu.** Müşteri/uygulama/grup/düğüm silme ve
tahmin kabul etme çağrılarında `catch` yoktu: işlem reddedilirse (başka sekmede zaten
silinmiş, sunucu hatası, ağ kopması) kullanıcıya HİÇBİR ŞEY söylenmiyor, satır yerinde
kalıyordu; hata yalnızca tarayıcı konsoluna yakalanmamış bir promise reddi olarak düşüyordu.

Bu testler ikisini de statik olarak kapatıyor.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
PAGES = sorted((FRONTEND / "pages").glob("*.tsx"))
# Bileşenler şu an yazma ucunu doğrudan çağırmıyor (geri çağırım alıyorlar); denetim yine de
# onları kapsıyor ki ileride eklenen bir çağrı sessizce yutulmasın.
TSX = PAGES + sorted((FRONTEND / "components").glob("*.tsx"))

# Yazma uçları: başarısızlığı kullanıcıya söylenmek ZORUNDA olan çağrılar.
_MUTATIONS = re.compile(
    r"\bapi\.(delete|create|update|ack|save|run|set|reset|convert|apply|add|remove)\w*\("
)


def _function_blocks(source: str) -> list[tuple[str, str]]:
    """(isim, gövde) — `const foo = async (…) => { … }` bloklarını süslü parantez sayarak ayırır."""
    blocks: list[tuple[str, str]] = []
    for match in re.finditer(r"const (\w+) = async \([^)]*\) => \{", source):
        start = source.index("{", match.end() - 1)
        depth = 0
        for i in range(start, len(source)):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append((match.group(1), source[start : i + 1]))
                    break
    return blocks


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_empty_table_rows_go_through_the_shared_state_component(page: Path):
    """Elle yazılmış `<td className="empty">` üç durumu ayıramaz. `TableState` ayırıyor."""
    source = page.read_text(encoding="utf-8")
    offenders = re.findall(r'<td[^>]*className="empty"', source)
    assert not offenders, (
        f"{page.name}: boş tablo satırı TableState yerine elle yazılmış — "
        "yükleme hatası 'kayıt yok' gibi görünür"
    )


@pytest.mark.parametrize("page", TSX, ids=lambda p: p.name)
def test_mutating_handlers_report_their_failures(page: Path):
    """Bir yazma işlemi başarısız olduğunda kullanıcı bunu görmeli."""
    source = page.read_text(encoding="utf-8")
    offenders = [
        name
        for name, body in _function_blocks(source)
        if _MUTATIONS.search(body) and "catch" not in body
    ]
    assert not offenders, (
        f"{page.name}: {offenders} — yazma çağrısı var ama hata yakalanmıyor; "
        "başarısızlık kullanıcıya görünmeden yutuluyor"
    )


def test_the_shared_state_components_exist():
    """Bu testlerin dayandığı ortak bileşenler yerinde mi (yeniden adlandırma kırmasın)."""
    page_state = (FRONTEND / "components" / "PageState.tsx").read_text(encoding="utf-8")
    for export in ("PageLoading", "PageError", "NotFoundState", "EmptyState", "TableState"):
        assert f"export function {export}" in page_state, f"{export} yok"
