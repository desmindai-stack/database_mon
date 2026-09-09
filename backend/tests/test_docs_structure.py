"""Doküman yapısı denetimi.

CLAUDE.md **her oturumda** okunuyor ve token maliyeti var. Mimari detayı ise ihtiyaç anında
okunacak bir referans — orada durması hem maliyet hem de dikkat israfı. Dosya zamanla 212
satıra çıkmıştı; sınırı yükseltmek yerine yapı düzeltildi (mimari `docs/MIMARI.md`'ye
taşındı) ve bu test sınırın sessizce tekrar aşılmasını engelliyor.

AYRIM: neyin CLAUDE.md'de kalacağı "her oturumda uygulanması gerekiyor mu" sorusuyla
belirleniyor. Kurallar ve terminoloji evet; servis tablosu ve süreç diyagramı hayır.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD = ROOT / "CLAUDE.md"

#: Üst sınır. Kural 200'dü; 180 seçildi ki gelecekteki eklemeler için pay kalsın ve sınır
#: aşılmadan ÖNCE fark edilsin — tam sınıra dayanmış bir dosya, bir sonraki eklemede yine
#: aynı yapısal soruna düşerdi.
MAX_CLAUDE_MD_LINES = 180

#: CLAUDE.md'de OLMAMASI gereken içerik: ihtiyaç anında okunacak referans bilgi.
#: Değer, o içeriğin taşındığı dosya.
REFERENCE_CONTENT_BELONGS_ELSEWHERE = {
    "| Servis | Ne yapar |": "docs/MIMARI.md",
    "backend/app/\n": "docs/MIMARI.md",
    "pip install -r requirements.txt": "README.md",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_claude_md_stays_within_the_line_budget():
    """Sınırı yükseltmek yapısal sorunu çözmez, erteler. Aşıldığında yapılacak şey içeriği
    ilgili `docs/` dosyasına taşıyıp yönlendirme bırakmak."""
    line_count = len(_read(CLAUDE_MD).splitlines())
    assert line_count <= MAX_CLAUDE_MD_LINES, (
        f"CLAUDE.md {line_count} satır (sınır {MAX_CLAUDE_MD_LINES}). Sınırı yükseltmeyin — "
        "her oturumda okunmayan bölümleri docs/ altına taşıyıp yönlendirme bırakın."
    )


def test_reference_content_is_not_duplicated_in_claude_md():
    """Mükerrer bilgi iki yerde ayrışır ve hangisinin doğru olduğu belirsizleşir."""
    source = _read(CLAUDE_MD)
    offenders = [
        f"{marker.strip()!r} → {target}"
        for marker, target in REFERENCE_CONTENT_BELONGS_ELSEWHERE.items()
        if marker in source
    ]
    assert not offenders, "CLAUDE.md'de referans içeriği var: " + "; ".join(offenders)


def test_claude_md_points_to_the_architecture_document():
    """Yönlendirme olmadan taşımak, bilgiyi kaybetmek olur."""
    source = _read(CLAUDE_MD)
    assert "docs/MIMARI.md" in source


def test_rules_and_terminology_stay_in_claude_md():
    """Bunlar HER OTURUMDA uygulanması gereken kurallar, referans değil — taşınmamalı."""
    source = _read(CLAUDE_MD)
    assert "## Kurallar" in source
    assert "## Terminoloji" in source
    # Kuralların özü: bunlar kaybolursa dosya işlevini yitirir.
    for rule in ("Her iş ayrı commit", "migration zorunlu", "beş parçalı", "kanıt zorunlu"):
        assert rule in source, f"kural kaybolmuş: {rule}"


@pytest.mark.parametrize(
    "document", ["CLAUDE.md", "docs/MIMARI.md"]
)
def test_referenced_documents_exist(document):
    """Var olmayan bir dosyaya yönlendirmek, bilgiyi taşımaktan beter: okuyucu arar ve
    bulamaz."""
    source = _read(ROOT / document)
    missing: list[str] = []
    # Markdown bağlantıları ve düz metin dosya adları.
    candidates = set(re.findall(r"\]\(([^)#]+?)(?:#[^)]*)?\)", source))
    candidates |= set(re.findall(r"\*\*([A-Za-z0-9_./-]+\.md)\*\*", source))
    for target in candidates:
        if target.startswith(("http://", "https://")):
            continue
        base = (ROOT / document).parent if "/" in document else ROOT
        if not (base / target).resolve().exists():
            missing.append(target)
    assert not missing, f"{document} var olmayan dosyalara yönlendiriyor: {missing}"


def test_the_architecture_document_carries_what_was_moved():
    """Taşıma kaybı olmamalı: servis tablosu ve dizin ağaçları artık orada."""
    source = _read(ROOT / "docs" / "MIMARI.md")
    assert "| Servis | Ne yapar |" in source
    assert "backend/app/" in source
    assert "frontend/src/" in source
    # Yönlendirme çift yönlü: referans dokümandan kurallara dönüş yolu da olmalı.
    assert "CLAUDE.md" in source
