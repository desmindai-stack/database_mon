"""Faz 22 İŞ 2 — görsel tutarlılık ve hizalama.

Denetimde bulunanlar:

* **Boşluk değerleri göz kararıydı.** gap/padding/margin için 26 farklı rem değeri vardı
  (0.05'ten 3'e; aralarında 0.35, 0.45, 0.55, 0.65, 0.85, 1.1, 1.3). Aynı işlevdeki iki öğe
  1-2px farkla hizasız duruyordu.
* **Sayfa geçişinde yatay kayma.** Kısa bir sayfadan uzun bir sayfaya geçerken dikey kaydırma
  çubuğu belirip içeriği ~15px sola itiyordu, geri dönerken geri itiyordu.
* **Yükleniyor durumu içeriğin yerini tutmuyordu.** `PageLoading` ortalanmış küçük bir kutu;
  veri gelince sayfa boyu birden değişiyor ve içerik sıçrıyordu.
* **Birincil eylem kabı tutarsızdı.** Çoğu sayfada `.header-actions` içinde, `InstancesPage`'de
  doğrudan `<header>` çocuğu, `ReportsPage`'de ayrı tanımlı `.report-actions`.

Bu testler statik: CSS ve JSX kaynağını denetliyor, tarayıcıda ölçüm yapmıyor (frontend'in
test koşucusu yok). Yani "kural yerinde mi" sorusunu cevaplıyor, "piksel doğru mu" sorusunu
değil.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "frontend" / "src" / "index.css").read_text(encoding="utf-8")
PAGES_DIR = ROOT / "frontend" / "src" / "pages"
COMPONENTS_DIR = ROOT / "frontend" / "src" / "components"

# 4px'lik ölçek. Yeni bir adım gerekiyorsa önce buraya ve `:root`'a eklenmeli.
SCALE_REM = {0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0}
SPACING_PROPS = ("gap", "row-gap", "column-gap", "padding", "margin")


def _spacing_values(source: str) -> list[tuple[str, float]]:
    """(bildirim, rem değeri) — yalnızca boşluk özellikleri."""
    props = "|".join(SPACING_PROPS)
    decl = re.compile(rf"\b((?:{props})(?:-(?:top|right|bottom|left))?:\s*[^;{{}}]+);")
    found = []
    for match in decl.finditer(source):
        body = match.group(1)
        for num in re.findall(r"(\d*\.?\d+)rem", body):
            found.append((body.strip(), float(num)))
    return found


# --- Boşluk ölçeği ---------------------------------------------------------------------------


def test_the_spacing_scale_is_declared():
    """Ölçek CSS değişkeni olarak tanımlı olmalı — yeni kod göz kararı değer yazmasın."""
    for step in ("--space-1", "--space-2", "--space-3", "--space-4", "--space-6", "--space-8"):
        assert step in CSS, f"{step} tanımlı değil"


def test_every_spacing_value_in_css_is_on_the_scale():
    off_scale = sorted({value for _decl, value in _spacing_values(CSS) if value not in SCALE_REM})
    assert not off_scale, (
        f"ölçek dışı boşluk değerleri: {off_scale}. Ölçek: {sorted(SCALE_REM)}. "
        "Yeni bir adım gerçekten gerekiyorsa :root'a ve bu testin SCALE_REM'ine ekleyin."
    )


@pytest.mark.parametrize(
    "path",
    sorted(PAGES_DIR.glob("*.tsx")) + sorted(COMPONENTS_DIR.glob("*.tsx")),
    ids=lambda p: p.name,
)
def test_inline_style_spacing_is_on_the_scale(path: Path):
    """Satır içi stiller de aynı ölçeğe uymalı; CSS'i düzeltip JSX'i unutmak ayrışma yaratır."""
    source = path.read_text(encoding="utf-8")
    pattern = r'(?:gap|padding|margin)(?:Top|Bottom|Left|Right)?: "([^"]+)"'
    off_scale = []
    for value in re.findall(pattern, source):
        for num in re.findall(r"(\d*\.?\d+)rem", value):
            if float(num) not in SCALE_REM:
                off_scale.append(f"{num}rem")
    assert not off_scale, f"{path.name}: ölçek dışı satır içi boşluk: {sorted(set(off_scale))}"


# --- Sayfa geçişinde kayma -------------------------------------------------------------------


def test_the_scrollbar_gutter_is_reserved():
    """Kaydırma çubuğunun belirip kaybolması içeriği yatay olarak itiyordu."""
    block = re.search(r"^html \{(.*?)^\}", CSS, re.S | re.M)
    assert block, "html kuralı yok"
    assert "scrollbar-gutter: stable" in block.group(1), (
        "scrollbar-gutter: stable yok — sayfa geçişlerinde yatay kayma geri gelir"
    )


def test_the_page_header_has_a_stable_height():
    """Başlığın altında açıklama olan ve olmayan sayfalar arasında geçerken içerik dikey
    olarak sıçrıyordu."""
    block = re.search(r"^\.page-header \{(.*?)^\}", CSS, re.S | re.M)
    assert block and "min-height" in block.group(1), ".page-header için sabit alt sınır yok"


# --- Yükleniyor durumu içeriğin yerini koruyor mu ---------------------------------------------


def test_a_skeleton_loader_exists():
    source = (COMPONENTS_DIR / "PageState.tsx").read_text(encoding="utf-8")
    assert "export function PageSkeleton" in source
    assert "export function TableSkeleton" in source
    assert ".skeleton" in CSS


def test_table_loading_uses_skeleton_rows_not_a_single_spinner():
    """Tek satırlık spinner, veri gelince tablo yüksekliğini birden değiştiriyordu."""
    source = (COMPONENTS_DIR / "PageState.tsx").read_text(encoding="utf-8")
    block = re.search(r"if \(loading\) \{(.*?)\n  \}", source, re.S)
    assert block, "TableState yükleniyor dalı bulunamadı"
    assert "TableSkeleton" in block.group(1), "tablo yüklenirken iskelet kullanılmıyor"


@pytest.mark.parametrize(
    "page",
    ["ApplicationsPage.tsx", "DatabaseGroupsPage.tsx", "ServersPage.tsx",
     "GroupDetailPage.tsx", "InstanceDetailPage.tsx", "DatabaseWizardPage.tsx"],
)
def test_record_pages_reserve_space_while_loading(page: str):
    """Kayıt yükleyen sayfalar ortalanmış kutu yerine iskelet göstermeli."""
    source = (PAGES_DIR / page).read_text(encoding="utf-8")
    assert "PageSkeleton" in source, f"{page} iskelet kullanmıyor"
    assert "<PageLoading" not in source, f"{page} hâlâ yer tutmayan yükleniyor kutusunu kullanıyor"


def test_the_skeleton_respects_reduced_motion():
    """Sürekli parıldayan bir iskelet hareket hassasiyeti olan kullanıcıyı rahatsız eder."""
    assert "prefers-reduced-motion" in CSS
    reduced = CSS[CSS.index("@media (prefers-reduced-motion: reduce)") :]
    assert "skeleton" in reduced or ".skeleton-line { animation: none" in CSS


# --- Birincil eylem konumu --------------------------------------------------------------------


def test_header_action_containers_share_one_rule():
    """`.report-actions` ayrı tanımlıydı ve dikey hizası diğer sayfalardan farklı düşüyordu."""
    block = re.search(r"^\.header-actions,\s*\n\.report-actions \{(.*?)^\}", CSS, re.S | re.M)
    assert block, ".header-actions ve .report-actions aynı kurala bağlı değil"
    body = block.group(1)
    assert "align-items: center" in body, "dikey hiza tanımlı değil"
    assert "gap: var(--space-" in body, "boşluk ölçekten gelmiyor"


@pytest.mark.parametrize(
    "page",
    ["InstancesPage.tsx", "ApplicationsPage.tsx", "CustomersPage.tsx",
     "DatabaseGroupsPage.tsx", "AlertsPage.tsx", "DashboardPage.tsx"],
)
def test_primary_actions_live_in_the_shared_container(page: str):
    """Birincil eylem her sayfada aynı kapta olmalı; doğrudan `<header>` çocuğu olan bir
    düğme diğerlerinden farklı hizalanır."""
    source = (PAGES_DIR / page).read_text(encoding="utf-8")
    if "btn btn-primary" not in source:
        pytest.skip("bu sayfada birincil eylem yok")
    header = re.search(r'<header className="page-header">(.*?)</header>', source, re.S)
    if header is None or "btn-primary" not in header.group(1):
        pytest.skip("birincil eylem sayfa başlığında değil")
    assert "header-actions" in header.group(1), (
        f"{page}: başlıktaki birincil eylem .header-actions içinde değil"
    )


# --- Kart yükseklikleri ------------------------------------------------------------------------


def test_grid_cards_stretch_to_equal_height():
    """Yan yana kartlar aynı hizada başlayıp bitmeli; içeriği kısa olan kart yukarıda
    bitiyor ve satır kırık görünüyordu."""
    block = re.search(r"^\.grid \{(.*?)^\}", CSS, re.S | re.M)
    assert block and "align-items: stretch" in block.group(1)
    assert re.search(r"^\.grid > \.card \{[^}]*height: 100%", CSS, re.S | re.M), (
        "ızgara içindeki kartlar yüksekliği doldurmuyor"
    )
