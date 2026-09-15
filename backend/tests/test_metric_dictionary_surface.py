"""Metrik açıklamaları arayüze BAĞLI mı (Faz 30 İŞ 3).

`/api/queries/metric-dictionary` çalışıyordu ama arayüz onu hiç çağırmıyordu: her metriğin
"ne anlama geliyor, ne zaman sorun" açıklaması üretiliyor, hiçbir ekranda görünmüyordu.

İki şeyi koruyor:

1. **Arayüzdeki her anahtar sözlükte var.** Olmayan bir anahtara bağlanan ipucu sessizce
   hiçbir şey çizmez — kullanıcı açıklamanın olmadığını değil, olmamasını gerektiğini sanır.
2. **Açıklama metni arayüzde yazılı değil.** İki tanım olsaydı, eşik kodda değiştiğinde
   arayüzdeki metin sessizce eskirdi.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.domain.query_metrics import metric_dictionary

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
PAGE = FRONTEND / "pages" / "InstanceDetailPage.tsx"
COMPONENT = FRONTEND / "components" / "MetricHint.tsx"


def _keys_used_in_the_ui() -> list[str]:
    return re.findall(r'metricKey="([^"]+)"', PAGE.read_text(encoding="utf-8"))


def test_the_ui_actually_asks_for_the_dictionary():
    """ASIL REGRESYON: uç çalışıyordu, arayüz çağırmıyordu."""
    assert "getMetricDictionary" in (FRONTEND / "api.ts").read_text(encoding="utf-8")
    assert "useMetricDictionary" in PAGE.read_text(encoding="utf-8")


def test_every_key_shown_in_the_ui_exists_in_the_dictionary():
    known = {row["key"] for row in metric_dictionary()}
    used = _keys_used_in_the_ui()
    assert used, "arayüzde hiç metrik ipucu bağlanmamış"
    missing = sorted(set(used) - known)
    assert missing == [], f"sözlükte olmayan anahtarlar: {missing}"


def test_the_explanations_are_not_duplicated_in_the_frontend():
    """Eşikler koddan geliyor; arayüze kopyalansalardı kod değişince metin eskirdi."""
    component = COMPONENT.read_text(encoding="utf-8")
    # Bileşen yalnızca SUNUM yapmalı: alanları okuyor, metni kendisi yazmıyor.
    assert "meaning.meaning" in component and "meaning.when_problem" in component
    for threshold in ("%30", "%95", "%20"):
        assert threshold not in component, f"{threshold} eşiği arayüze kopyalanmış"


def test_an_unknown_key_renders_nothing_instead_of_inventing_text():
    """Uydurulmuş bir açıklama, açıklama olmamasından kötüdür."""
    assert "if (!meaning) return null;" in COMPONENT.read_text(encoding="utf-8")


def test_every_dictionary_entry_carries_both_halves():
    """"Ne ölçüyor" tek başına yetmiyor: kullanıcının kararı "ne zaman sorun"dan çıkıyor."""
    for row in metric_dictionary():
        assert row["meaning"].strip(), row["key"]
        assert row["when_problem"].strip(), row["key"]
        assert row["label"].strip(), row["key"]
