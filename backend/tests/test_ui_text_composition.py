"""Arayüzde metin birleşmesi ve ham İngilizce durum değeri (Faz 31 Commit 9, madde 3).

Bildirilen hata: sağlık kartında **"Not: Ahealthy"** — harf notu ile durum etiketi boşluksuz birleşmişti ve
etiket İNGİLİZCEYDİ. İki ayrı satır elemanıydı; aradaki boşluk YALNIZCA CSS'e bağlıydı ve metnin kendisinde
ayırıcı yoktu (kopyalayınca, ekran okuyucuda ve dar ekranda birleşik okunuyor).

İki tarama, ikisi de koddan (elle dosya listesi yok):

1. **Bitişik satır elemanı:** JSX'te iki satır elemanı (`<span>`, `<strong>`, …) arada metin/ayırıcı olmadan
   yan yanaysa, kapsayan kutunun CSS'i çocukları ayırmıyorsa (flex/grid + gap ya da dikey yön) birleşik okunur.
   CSS `frontend/src/index.css`'ten okunuyor — kural "şu dosyada şu satır" değil, gerçek stil.
2. **Ham durum değeri:** `{...status}` gibi bir ifade doğrudan metin olarak basılıyorsa İngilizce sızıyor
   demektir; etiketler `terminology.ts`'deki tek kaynaktan gelmeli.

Negatif kontroller: her iki tarayıcıya da bilerek bozuk JSX veriliyor.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
CSS = FRONTEND / "index.css"

INLINE = r"(?:strong|span|b|em|code|small)"
#: JSX yalnızca SATIR SONU içeren boşluğu siler; aynı satırdaki tek boşluk ekranda korunur. Bu yüzden
#: yalnızca "hiç boşluk yok" ve "satır sonu içeren boşluk" durumları birleşik okunur.
ADJACENT = re.compile(rf"</{INLINE}>(?:[ \t]*\r?\n\s*)?<({INLINE})\b")
#: UYGULAMANIN kendi durum alanı doğrudan metin olarak basılıyor mu (ör. `>{report.status}<`).
#: `pg_stat_activity.state` gibi VERİTABANININ kendi terimleri (DBA'nın bildiği değerler) kapsam dışı.
RAW_STATUS = re.compile(r">\{\s*[\w.?]*\bstatus\b\s*\}<")
#: Etiket sözlüğünden geçenler sorun değil.
LABEL_LOOKUP = re.compile(r"(?:LABELS?|TR|statusLabel|_LABEL)\s*[\[(]")


def css_separating_classes(css_text: str) -> tuple[set[str], set[str]]:
    """Çocuklarını GÖRSEL olarak ayıran kutular.

    İki küme döner: (1) sınıfın KENDİSİ ayırıcı (grid, ya da flex + gap/dikey yön/space-between),
    (2) sınıfın ALT elemanları ayırıcı (`.kutu div { display:flex; ... }` gibi kurallar) — sınıfsız bir
    sarmalayıcı `<div>` içindeki çiftler bu kuralla ayrılıyor olabilir.
    """
    separating: set[str] = set()
    descendants: set[str] = set()
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css_text):
        selector, body = match.group(1), match.group(2)
        display = re.search(r"display:\s*([\w-]+)", body)
        if not display:
            continue
        kind = display.group(1)
        # `gap: 0.35rem` de ayırıyor — yalnızca tam "0" ayırmıyor.
        has_gap = re.search(r"(?:^|[\s;])gap:\s*(?!0(?:\s|;|$))", body) is not None
        column = re.search(r"flex-direction:\s*column", body) is not None
        # `justify-content: space-between/around/evenly` de çocukları görsel olarak ayırıyor.
        spread = re.search(r"justify-content:\s*space-", body) is not None
        if not (kind in ("grid", "inline-grid") or (kind in ("flex", "inline-flex") and (has_gap or column or spread))):
            continue
        names = re.findall(r"\.([A-Za-z0-9_-]+)", selector)
        if re.search(r"\.[A-Za-z0-9_-]+[\s>]+[a-z]", selector.strip()):
            descendants |= set(names)  # ör. `.query-stats-grid div`
        else:
            separating |= set(names)
    return separating, descendants


#: Kutu (blok) elemanları — kapsayan kutu bunlardan biridir, kardeş satır elemanı değil.
BLOCK = r"(?:div|li|p|td|th|h[1-6]|section|article|header|footer|figcaption)"


def _enclosing_class(lines: list[str], index: int, column: int, same_line: bool) -> str | None:
    """Bitişik çiftin KAPSAYAN kutusunun sınıfı.

    Çift aynı satırdaysa, o satırda çiftten ÖNCE açılan son blok elemanın sınıfı; satır sonu içeriyorsa
    yukarıda daha az girintili ilk `className` (kardeş satır elemanının sınıfı değil).
    """
    if same_line:
        opens = list(re.finditer(rf"<{BLOCK}[^>]*className=(?:\"([^\"]+)\"|\{{`([^`]*)`)", lines[index][:column]))
        if opens:
            value = opens[-1].group(1) or opens[-1].group(2)
            return value.split("$")[0].strip()
        return None
    indent = len(lines[index]) - len(lines[index].lstrip())
    for line in reversed(lines[:index]):
        if not line.strip():
            continue
        if (len(line) - len(line.lstrip())) < indent and "className=" in line:
            match = re.search(r'className=(?:"([^"]+)"|\{`([^`]*)`)', line)
            if match:
                return (match.group(1) or match.group(2)).split("$")[0].strip()
    return None


def glued_text_problems(source: str, filename: str, separating: tuple[set[str], set[str]]) -> list[str]:
    own, descendants = separating
    problems = []
    lines = source.splitlines()
    for match in ADJACENT.finditer(source):
        line_index = source[:match.start()].count("\n")
        column = match.start() - (source.rfind("\n", 0, match.start()) + 1)
        same_line = "\n" not in match.group(0)
        enclosing = _enclosing_class(lines, line_index, column, same_line)
        classes = set((enclosing or "").split())
        if enclosing is None:
            # Sarmalayıcının sınıfı yok: üstteki en yakın sınıfı kullan (ör. `<ul class=event-list><li>`).
            classes = set((_enclosing_class(lines, line_index, column, False) or "").split())
        # Sınıfın kendisi ya da alt elemanları için tanımlı kural ayırıyorsa sorun yok.
        if classes & (own | descendants):
            continue
        snippet = " ".join(source[match.start():match.end() + 40].split())
        problems.append(f"{filename}:{line_index + 1} ayırıcısız bitişik satır elemanı "
                        f"(kutu: {enclosing or 'bilinmiyor'}): {snippet[:80]}")
    return problems


def raw_status_problems(source: str, filename: str) -> list[str]:
    problems = []
    for match in RAW_STATUS.finditer(source):
        text = match.group(0)
        if LABEL_LOOKUP.search(text):
            continue
        line = source[:match.start()].count("\n") + 1
        problems.append(f"{filename}:{line} ham durum değeri ekranda: {text[:60]}")
    return problems


@pytest.fixture(scope="module")
def separating() -> tuple[set[str], set[str]]:
    return css_separating_classes(CSS.read_text(encoding="utf-8"))


def test_css_separating_classes_are_read_from_the_real_stylesheet(separating):
    own, descendants = separating
    assert {"tuning-score-meta", "tuning-grade"} <= own, sorted(own)[:20]
    assert "query-stats-grid" in descendants, sorted(descendants)[:20]


def test_no_glued_inline_text_in_the_ui(separating):
    problems: list[str] = []
    for path in sorted(FRONTEND.rglob("*.tsx")):
        problems += glued_text_problems(path.read_text(encoding="utf-8"), path.relative_to(FRONTEND).as_posix(),
                                        separating)
    assert problems == [], "\n".join(problems)


def test_no_raw_english_status_value_is_rendered():
    problems: list[str] = []
    for path in sorted(FRONTEND.rglob("*.tsx")):
        problems += raw_status_problems(path.read_text(encoding="utf-8"), path.relative_to(FRONTEND).as_posix())
    assert problems == [], "\n".join(problems)


def test_negative_control_the_reported_bug_is_caught(separating):
    """Commit 9 öncesi sağlık kartının TAM HÂLİ — iki tarayıcı da yakalamalı."""
    broken = '''
          <div className="tuning-score-meta-old">
            <strong>Not: {report.grade}</strong>
            <span className={`tuning-status ${report.status}`}>{report.status}</span>
          </div>
'''
    glued = glued_text_problems(broken, "sentetik.tsx", separating)
    raw = raw_status_problems(broken, "sentetik.tsx")
    assert len(glued) == 1 and "ayırıcısız" in glued[0], glued
    assert len(raw) == 1 and "ham durum" in raw[0], raw


def test_negative_control_separating_box_and_label_lookup_are_accepted(separating):
    fine = '''
          <div className="tuning-score-meta">
            <strong>Not: {report.grade}</strong>
            <span className="tuning-status">{statusLabel(report.status)}</span>
          </div>
'''
    assert glued_text_problems(fine, "sentetik.tsx", separating) == []
    assert raw_status_problems(fine, "sentetik.tsx") == []
