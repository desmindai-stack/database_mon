"""Faz 19 İŞ 3 — arayüz tutarlılığı: aynı işlev her yerde aynı görünmeli.

Üç kural denetleniyor:

1. **Sınırsız liste render'ı yok.** Büyümesi beklenen listeler (instance, alarm, alarm
   kaydı, rapor bulgusu) sayfalama ya da "daha göster" ile sınırlanmalı. Öncesinde her şey
   tek seferde render ediliyordu; bir bankada birkaç yüz instance ve yüzlerce alarm kaydı
   normal ve her satır kendi düğmeleri/rozetleriyle geldiği için bu, tarayıcıyı kilitler.

2. **Yetki reddi ekranı çıkış yolu vermeli.** Ciplak bir `<div className="error">` kullanıcıyı
   çıkamayacağı bir sayfada bırakıyordu.

3. **Dar ekran kırılma noktaları yerinde.** Bankada tablet kullanımı olabilir; öncesinde tek
   kırılma noktası 800px'ti ve altında kenar çubuğu `min-height: 100vh` ile tüm ilk ekranı
   kaplıyor, içerik ekranın altında kalıyordu.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
CSS = FRONTEND / "index.css"

# Büyümesi beklenen listeleri gösteren sayfalar ve hangi mekanizmayı kullanmaları gerektiği.
PAGINATED = {
    "InstancesPage.tsx": "usePagination",
    "AlertsPage.tsx": "usePagination",
    "ReportsPage.tsx": "useShowMore",
}


@pytest.mark.parametrize("page,mechanism", sorted(PAGINATED.items()))
def test_long_lists_are_bounded(page: str, mechanism: str):
    source = (FRONTEND / "pages" / page).read_text(encoding="utf-8")
    assert mechanism in source, f"{page}: uzun liste sınırlanmıyor ({mechanism} yok)"


@pytest.mark.parametrize("page", sorted(PAGINATED))
def test_paginated_pages_render_the_slice_not_the_whole_array(page: str):
    """Sayfalama kurulup listenin TAMAMI render edilirse hiçbir işe yaramaz."""
    source = (FRONTEND / "pages" / page).read_text(encoding="utf-8")
    if "usePagination" not in source:
        pytest.skip("bu sayfa 'daha göster' kullanıyor")
    slices = re.findall(r"const (\w+Slice) = usePagination\(", source)
    assert slices, f"{page}: usePagination var ama dilim değişkeni bulunamadı"
    for name in slices:
        assert f"{name}.items.map(" in source, f"{page}: {name} dilimi render edilmiyor"


def test_permission_denied_screens_offer_a_way_back():
    """`canWrite`/`isAdmin` kontrolüyle sayfayı komple kapatan her yer geri dönüş vermeli."""
    offenders = []
    for path in sorted((FRONTEND / "pages").glob("*.tsx")):
        source = path.read_text(encoding="utf-8")
        # "yetki" geçen bir erken dönüş var mı, ve o dönüş çıplak hata kutusu mu?
        for match in re.finditer(r"return <div className=\"error\">([^<]*)</div>", source):
            if "yetki" in match.group(1).lower():
                offenders.append(f"{path.name}: {match.group(1)[:50]}")
    assert not offenders, f"çıkış yolu olmayan yetki ekranları: {offenders}"


def test_narrow_screen_breakpoints_exist():
    """Tablet genişlikleri (yatay ~1024px, dikey ~820px) için kırılma noktası olmalı."""
    css = CSS.read_text(encoding="utf-8")
    assert "@media (max-width: 1024px)" in css, "yatay tablet kırılma noktası yok"
    assert "@media (max-width: 820px)" in css, "dikey tablet kırılma noktası yok"


def test_the_main_column_does_not_clip_overflowing_content():
    """`.main { overflow-x: hidden }` taşan içeriği KESİYORDU; dar ekranda geniş bir araç
    çubuğunun sağ tarafına hiç ulaşılamıyordu."""
    css = CSS.read_text(encoding="utf-8")
    # Son tanım kazanır: dosyada `.main` için en son yazılan overflow-x değeri `auto` olmalı.
    values = re.findall(r"\.main\s*\{[^}]*?overflow-x:\s*(\w+)", css, re.S)
    assert values, ".main için overflow-x tanımı yok"
    assert values[-1] == "auto", f".main son overflow-x değeri {values[-1]}, taşan içerik kesiliyor"


def test_the_sidebar_does_not_fill_the_viewport_on_narrow_screens():
    """Tek sütuna düşen düzende `min-height: 100vh` kenar çubuğunu ilk ekranın tamamı yapar."""
    css = CSS.read_text(encoding="utf-8")
    narrow = css[css.index("@media (max-width: 820px)") :]
    block = narrow[: narrow.index("@media", 10)]
    assert "min-height: auto" in block, "dar ekranda kenar çubuğu hâlâ tüm ekranı kaplıyor"
