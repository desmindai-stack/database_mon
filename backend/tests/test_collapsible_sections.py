"""Katlanabilir bölümler ve sayfa özet şeridi (Faz 29 İŞ 3).

Frontend'in kendi test koşucusu yok; arayüz garantileri buradaki statik denetimlerle
korunuyor (CLAUDE.md kuralı).

Bu dosyanın koruduğu fikirler:

1. **Sorunlu bölüm açık gelir.** "Her şeyi kapat" yanlış olurdu: kullanıcı kritik bulguyu
   görmek için tıklamak zorunda kalırdı.
2. **Kullanıcının kapatma kararı kritik bir bölümde geçersizdir.** "Dün kapattım" kararı
   dünkü duruma verilmişti; bölüm bugün kritikse gizlemek, gizlenmesi en yanlış şeyi
   gizlemek olurdu.
3. **Kapalı bölümün içeriği hiç çizilmez.** Katlamanın asıl kazancı görsel değil,
   ağır tabloların ve grafiklerin hiç render edilmemesi.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
COMPONENT = FRONTEND / "components" / "CollapsibleSection.tsx"


@pytest.fixture(scope="module")
def source() -> str:
    assert COMPONENT.exists(), f"bileşen bulunamadı: {COMPONENT}"
    return COMPONENT.read_text(encoding="utf-8")


def test_problem_sections_open_by_default(source: str):
    """Varsayılan duruma karar veren şey bölümün DURUMU."""
    match = re.search(
        r"export function defaultOpenFor\(status: SectionStatus\): boolean \{(.*?)\}",
        source,
        re.S,
    )
    assert match, "defaultOpenFor bulunamadı"
    body = match.group(1)
    for status in ("critical", "warning", "unknown"):
        assert f'"{status}"' in body, f"{status} varsayılan olarak açık olmalı"
    # "ok" ve "info" kapalı gelmeli: açık gelselerdi katlamanın anlamı kalmazdı.
    assert '"ok"' not in body
    assert '"info"' not in body


def test_a_closed_critical_section_reopens(source: str):
    """Kullanıcının kapatma kararı, bölüm kritik hale geldiğinde geçersiz."""
    assert 'if (stored === false && status === "critical") return true;' in source


def test_closed_sections_are_not_rendered(source: str):
    """`hidden` ile gizlemek ağır tabloları yine de çizerdi; katlamanın kazancı kaybolurdu."""
    assert "{open && (" in source
    assert 'hidden={!open}' not in source


def test_state_survives_a_broken_storage(source: str):
    """Gizli sekmede ya da depolama kapalıyken sayfa DÜŞMEMELİ."""
    read = source[source.index("function readStored") : source.index("export function SectionsProvider")]
    assert "try {" in read and "catch" in read


def test_summary_bar_is_safe_outside_its_provider(source: str):
    """Bileşeni yanlışlıkla provider dışına koymak sayfayı düşürmemeli."""
    bar = source[source.index("export function PageSummaryBar") :]
    assert "if (!ctx || ctx.sections.length === 0) return null;" in bar


def test_summary_bar_opens_the_section_before_scrolling(source: str):
    """Kapalı bölüme kaydırmak, kapalı yüksekliğe kaydırıp bölümü ekran dışında bırakırdı."""
    bar = source[source.index("function goTo") :]
    open_at = bar.index("ctx!.open(id)")
    scroll_at = bar.index("scrollIntoView")
    assert open_at < scroll_at
    assert "requestAnimationFrame" in bar


# --- Bileşenin ikinci kuralı: çağıran açık bir varsayılan verebilir -------------------------


def test_caller_can_override_the_status_derived_default(source: str):
    """Ayar bölümlerinin "durumu" yok: kullanıcı o sayfaya zaten bir ayarı değiştirmeye
    geliyor, kontrolü bir tıklamanın arkasına saklamak işi zorlaştırırdı."""
    assert "fallbackOpen ?? defaultOpenFor(status)" in source
    # Kullanıcının kendi kararı yine yeniyor: fallback yalnızca kayıt YOKKEN devrede.
    assert "const stored = overrides[id];" in source
    assert source.index("const stored = overrides[id];") < source.index("fallbackOpen ?? defaultOpenFor")


# --- Sayfalara uygulanmış mı ---------------------------------------------------------------

# Katlama mekanizması TÜM sayfalarda aynı; özet şeridi yalnızca BULGU gösteren sayfalarda.
PAGES_WITH_SECTIONS = {
    "pages/ReportsPage.tsx": "reports",
    "pages/InstanceDetailPage.tsx": "instance-detail",
    "pages/GroupDetailPage.tsx": "group-detail",
    "pages/AdminPage.tsx": "admin",
    "pages/DashboardPage.tsx": "dashboard",
}

# Özet şeridi burada anlamlı: sayfada sayılacak bulgu var.
PAGES_WITH_SUMMARY_BAR = {
    "pages/ReportsPage.tsx",
    "pages/InstanceDetailPage.tsx",
    "pages/GroupDetailPage.tsx",
}


@pytest.mark.parametrize("relative,page_key", PAGES_WITH_SECTIONS.items())
def test_pages_that_use_sections_provide_the_context(relative: str, page_key: str):
    """Provider olmadan katlama durumu hatırlanmaz; bölümler sessizce kendi başlarına çalışır."""
    text = (FRONTEND / relative).read_text(encoding="utf-8")
    assert "<CollapsibleSection" in text, relative
    assert f'SectionsProvider pageKey="{page_key}"' in text, relative


@pytest.mark.parametrize("relative", sorted(PAGES_WITH_SUMMARY_BAR))
def test_finding_pages_carry_a_summary_bar(relative: str):
    """Kritik bulgu sayfanın onuncu kartında kalmasın: kaç kritik olduğu en üstte görünür."""
    text = (FRONTEND / relative).read_text(encoding="utf-8")
    assert "<PageSummaryBar />" in text, relative


def test_page_keys_are_unique():
    """İki sayfa aynı anahtarı kullanırsa birinin katlama tercihi diğerininkini ezerdi."""
    keys = list(PAGES_WITH_SECTIONS.values())
    assert len(keys) == len(set(keys))


def test_the_dashboard_does_not_show_a_second_summary():
    """Dashboard'ın durum kartları ZATEN "kaç kritik, kaç uyarı" sorusunu cevaplıyor ve
    tıklanınca ilgili grupları süzüyor.

    İkinci bir şerit aynı kelimeyi BAŞKA bir sayıyla gösterirdi — kartlar kritik GRUP
    sayısını, şerit kritik BULGU sayısını. CLAUDE.md'nin "ayrı hesaplama = ayrı sonuç =
    güven kaybı" kuralı tam olarak bunu yasaklıyor.
    """
    text = (FRONTEND / "pages" / "DashboardPage.tsx").read_text(encoding="utf-8")
    assert "<PageSummaryBar />" not in text
    assert "StatCard" in text, "özet işlevini gören kartlar kaldırılmışsa bu karar yeniden gözden geçirilmeli"


def test_the_admin_page_does_not_show_a_summary_bar():
    """Yönetim sayfasında sayılacak bir bulgu yok; her zaman "0 kritik" yazan bir şerit
    bilgi değil gürültü olurdu."""
    text = (FRONTEND / "pages" / "AdminPage.tsx").read_text(encoding="utf-8")
    assert "<PageSummaryBar />" not in text


def test_settings_sections_open_by_default():
    """Ayar bölümleri durumdan türetilseydi "ok" sayılıp KAPALI gelirdi: kullanıcı
    değiştirmeye geldiği kontrolü göremezdi."""
    text = (FRONTEND / "pages" / "AdminPage.tsx").read_text(encoding="utf-8")
    for section_id in ("admin-retention", "admin-refresh-interval", "admin-report-schedule", "admin-noise-filter"):
        block = text[text.index(f'id="{section_id}"') :][:400]
        assert "defaultOpen" in block and "defaultOpen={false}" not in block, section_id
    # "Yeni kullanıcı" bir ayar değil, nadir bir EYLEM — kapalı gelmeli.
    block = text[text.index('id="admin-new-user"') :][:400]
    assert "defaultOpen={false}" in block


def test_the_long_prerequisites_block_is_collapsible():
    """Kullanıcının örnek verdiği blok: ekranın yarısını kaplıyordu ve her şey yolundayken
    o alanı kaplamasının bir değeri yok."""
    text = (FRONTEND / "components" / "PrerequisitesPanel.tsx").read_text(encoding="utf-8")
    assert "<CollapsibleSection" in text
    assert 'id="prerequisites"' in text
    # Durum içerikten çıkıyor, sabit değil: hepsi tamamsa kapalı gelmeli.
    assert '"ok"' in text


def test_section_ids_are_unique_within_a_page():
    """Aynı id iki bölümde kullanılırsa biri diğerinin katlama durumunu ezerdi."""
    for relative in list(PAGES_WITH_SECTIONS) + [
        "components/SchemaHealthPanel.tsx",
        "components/PrerequisitesPanel.tsx",
    ]:
        text = (FRONTEND / relative).read_text(encoding="utf-8")
        ids = re.findall(r'<CollapsibleSection\s+[^>]*?id="([^"]+)"', text, re.S)
        assert len(ids) == len(set(ids)), f"{relative}: tekrar eden id {ids}"
