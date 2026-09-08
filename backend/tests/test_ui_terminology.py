"""Arayüz terminolojisi tutarlılığı (Faz 27 İŞ 3).

SORUN: aynı eylem farklı yerlerde farklı adlarla anılıyordu. Ana ekranda sağ üstte
"+ Yeni instance" yazıyordu; aynı hedefe (`/customers`) giden Veritabanları sayfasındaki
buton "+ Veritabanı Ekle" diyordu. Aynı yere götüren iki buton, iki farklı şey yapıyormuş
gibi görünüyordu. Ayrıca büyük harf kullanımı bile tutarsızdı: "+ Uygulama ekle" ile
"+ Uygulama Ekle" aynı üründe yan yana duruyordu.

Frontend'in kendi test koşucusu olmadığı için (CLAUDE.md) bu garantiler burada statik
olarak korunuyor.

KARAR: kullanıcı **veritabanı** ekliyor. "Instance" teknik bir terim ve hedef kitlenin
yarısı (müşteri yöneticisi) için hiçbir şey ifade etmiyor — ürünün kendi boş durum metni de
onu tanımlamak zorunda kalıyordu ("Bir instance, bağlantı bilgileriyle izlenen tek bir
veritabanıdır"), ki tanım gerektiren bir arayüz terimi yanlış terimdir.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
TERMINOLOGY = FRONTEND / "terminology.ts"

#: Kullanıcıya görünen metinlerde geçmemesi gereken teknik terim. Aşağıdaki istisnalar
#: dışında "instance" ekranda görünmemeli.
FORBIDDEN_IN_UI = "instance"

#: "instance" geçmesi MEŞRU olan yerler.
#
#: SQL Server'ın KENDİ terimi: "named instance" bir SQL Server kavramı. Alan adı olarak
#: ekranda bu şekilde geçmesi DOĞRU — "veritabanı adı" demek bambaşka bir şeyi kastederdi
#: ve kullanıcıyı yanlış değeri girmeye yönlendirirdi. Karşılaştırma küçük harfe indirgenmiş
#: metinle yapılıyor ("SQL Server instance adı" ile "Instance adı" aynı istisnaya girsin).
ALLOWED_CONTEXTS = (
    "instance portu",
    "instance adı",
)


def _tsx_files() -> list[Path]:
    return sorted(FRONTEND.rglob("*.tsx"))


def _user_visible_strings(source: str) -> list[str]:
    """JSX metinleri ve çift tırnaklı dizgi sabitleri.

    Kod tanımlayıcıları (`instanceId`, `getInstances`) ve rota adresleri (`/instances`)
    kapsam dışı: onlar kullanıcıya görünmüyor ve değiştirmek adresleri kırardı.
    """
    strings: list[str] = []
    # Çift tırnaklı JSX öznitelik/prop değerleri ve tek tırnaklı TS dizgileri.
    for match in re.finditer(r'"([^"\n]{3,})"|\'([^\'\n]{3,})\'', source):
        value = match.group(1) or match.group(2)
        if value.startswith("/") or value.startswith("./") or value.startswith("../"):
            continue  # yol / rota
        strings.append(value)
    # JSX gövde metni: >Metin<
    for match in re.finditer(r">\s*([^<>{}\n]{3,}?)\s*<", source):
        strings.append(match.group(1))
    return strings


def test_terminology_module_exists_as_the_single_source():
    """Metinleri elle yazmak yerine tek yerden almak, bir sonraki tutarsızlığı baştan
    engelliyor."""
    assert TERMINOLOGY.exists(), "terminology.ts yok — terimler yine dağılır"
    source = TERMINOLOGY.read_text(encoding="utf-8")
    assert "ADD_ACTIONS" in source
    assert "ADD_ACTION_BY_TARGET" in source


def test_buttons_going_to_the_same_target_carry_the_same_text():
    """KULLANICININ BİLDİRDİĞİ HATA. `/customers`'a giden iki buton vardı ve biri
    "+ Yeni instance", diğeri "+ Veritabanı Ekle" diyordu."""
    terminology = TERMINOLOGY.read_text(encoding="utf-8")
    targets = dict(
        re.findall(r'"(/[^"]+)":\s*ADD_ACTIONS\.(\w+)', terminology)
    )
    assert targets, "ADD_ACTION_BY_TARGET tablosu okunamadı"

    offenders: list[str] = []
    for path in _tsx_files():
        source = path.read_text(encoding="utf-8")
        for target in targets:
            # `<Link to="/customers" className="btn btn-primary">METIN</Link>`
            for match in re.finditer(
                rf'<Link\s+to="{re.escape(target)}"[^>]*className="[^"]*btn-primary[^"]*"[^>]*>\s*([^<]+?)\s*</Link>',
                source,
            ):
                text = match.group(1).strip()
                # Ortak sabitten geliyorsa doğru; elle yazılmışsa yanlış.
                if not text.startswith("{ADD_ACTIONS."):
                    offenders.append(f"{path.name}: {target} -> {text!r}")
    assert not offenders, (
        "Aynı hedefe giden birincil butonlar ortak metni kullanmıyor: " + "; ".join(offenders)
    )


def test_add_buttons_use_the_shared_constants():
    """Elle yazılmış bir "+ ... Ekle" metni, bir sonraki tutarsızlığın tohumudur."""
    offenders: list[str] = []
    for path in _tsx_files():
        if path.name == "terminology.ts":
            continue
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r'>\s*(\+\s*[A-ZÇĞİÖŞÜ][^<>{}\n]{2,30}?)\s*<', source):
            offenders.append(f"{path.name}: {match.group(1).strip()!r}")
    assert not offenders, (
        "Elle yazılmış ekleme butonu metni bulundu (terminology.ts'teki ADD_ACTIONS "
        "kullanılmalı): " + "; ".join(offenders)
    )


def test_turkish_sentence_case_is_used_not_title_case():
    """Türkçede başlık büyük harfi (Title Case) kuralı yok. Öncesinde "+ Uygulama ekle" ile
    "+ Uygulama Ekle" aynı üründe yan yana duruyordu."""
    source = TERMINOLOGY.read_text(encoding="utf-8")
    block = source.split("ADD_ACTIONS", 1)[1].split("}", 1)[0]
    for match in re.finditer(r'`\+ \$\{[^}]+\}\s+(\w+)`', block):
        word = match.group(1)
        assert word.islower(), f"'{word}' büyük harfle başlıyor — cümle düzeni kullanılmalı"


@pytest.mark.parametrize("filename", [p.name for p in _tsx_files()])
def test_no_technical_instance_term_in_user_visible_text(filename):
    """"Instance" ekranda görünmemeli: teknik bir terim ve hedef kitlenin yarısı için
    hiçbir şey ifade etmiyor. Kodda (`Instance` modeli) ve rotalarda (`/instances`)
    kalıyor — adresleri değiştirmek kullanıcıların kaydettiği bağlantıları kırardı."""
    path = next(p for p in _tsx_files() if p.name == filename)
    source = path.read_text(encoding="utf-8")

    offenders: list[str] = []
    for text in _user_visible_strings(source):
        if FORBIDDEN_IN_UI not in text.lower():
            continue
        if any(allowed in text.lower() for allowed in ALLOWED_CONTEXTS):
            continue
        # Kod tanımlayıcıları: camelCase/snake_case/PascalCase içinde geçenler.
        if re.search(r"[A-Za-z_]instance|instance[A-Za-z_]|instance_id|instanceId", text):
            continue
        # CSS sınıf adları.
        if re.fullmatch(r"[a-z0-9-]+", text):
            continue
        offenders.append(text)

    assert not offenders, (
        f"{filename} içinde kullanıcıya görünen 'instance' metni: {offenders}"
    )


def test_the_glossary_is_documented_in_claude_md():
    """Terminoloji kararı kodda yaşıyor ama bir sonraki oturumun onu bilmesi gerekiyor."""
    claude_md = (FRONTEND.parents[1] / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Terminoloji" in claude_md
    assert "Veritabanı" in claude_md
