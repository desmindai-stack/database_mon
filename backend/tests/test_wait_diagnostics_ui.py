"""Bekleme verisinin darboğaz teşhisine ve arayüze bağlanması (Faz 25 İŞ 3).

Bu turdan önce darboğaz sınıflandırması `exec_user_time`/`exec_sys_time` sütunlarına
dayanıyordu. O sütunlar pg_stat_statements'ın sürüm/ayarına bağlı ve pratikte çoğu kurulumda
BOŞ geliyor — yani sınıflandırma çalışıyor görünüyor ama neredeyse her zaman "unknown"
dönüyordu. Bekleme örneklemesi bu boşluğu ölçümle dolduruyor.

Ayrıca frontend'in kendi test koşucusu olmadığı için (bkz. CLAUDE.md) arayüz garantileri burada
statik olarak korunuyor: kaynak sınıfı listesi ile backend'in üretebildiği sınıflar ayrışırsa
kullanıcı etiketi olmayan boş bir hücre görür.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.services.database_load import CategoryShare
from app.services.query_diagnostics import (
    WAIT_CATEGORY_TO_RESOURCE,
    WAIT_DOMINANCE_THRESHOLD_PCT,
    diagnose_queries,
    diagnose_query,
)

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"


@dataclass
class FakeRow:
    """SlowQuerySample ile aynı alan adları; teşhis fonksiyonu ORM'e bağlı değil."""

    queryid: str | None = "q1"
    query: str = "SELECT 1"
    calls: int = 10
    mean_time_ms: float = 100.0
    total_time_ms: float = 1000.0
    shared_blks_hit: int | None = None
    shared_blks_read: int | None = None
    local_blks_hit: int | None = None
    local_blks_read: int | None = None
    temp_blks_read: int | None = None
    temp_blks_written: int | None = None
    plan_user_time: float | None = None
    plan_sys_time: float | None = None
    exec_user_time: float | None = None
    exec_sys_time: float | None = None


def _profile(*pairs: tuple[str, float]) -> list[CategoryShare]:
    return [
        CategoryShare(category=cat, label=cat.upper(), meaning=f"{cat} anlamı", aas=0.0, share_pct=pct)
        for cat, pct in pairs
    ]


# --- Ölçüm, türetmeden önce gelir -------------------------------------------------------


def test_wait_measurement_replaces_the_unknown_verdict():
    """ESKİ DAVRANIŞ: hiçbir sütun dolu değilse "bilinmiyor". Bekleme ölçümü varsa artık
    gerçek bir teşhis çıkıyor."""
    row = FakeRow()
    assert diagnose_query(row).resource == "unknown"
    assert diagnose_query(row, _profile(("io", 82.0), ("cpu", 18.0))).resource == "io"


def test_wait_based_verdict_is_observed_not_inferred():
    """Kilit teşhisi eskiden her zaman çıkarımdı ("dbace sorgu başına kilit bekleme süresi
    tutmuyor"). Artık örnekleyici bunu doğrudan gördüğü için ÖLÇÜM."""
    diagnosis = diagnose_query(FakeRow(), _profile(("lock", 71.0), ("cpu", 29.0)))
    assert diagnosis.resource == "lock"
    assert diagnosis.confidence == "observed"
    assert "bekleme ölçümü" in diagnosis.reason.lower()


def test_wait_measurement_beats_the_block_counter_heuristic():
    """Blok sayaçları "IO" derken bekleme ölçümü "kilit" diyorsa ÖLÇÜM kazanır.

    Sayaçlar sorgunun neye DOKUNDUĞUNU söyler, beklemenin nerede olduğunu değil: diskten çok
    okuyan bir sorgu, süresinin çoğunu bir kilidi beklerken geçiriyor olabilir.
    """
    io_heavy = FakeRow(shared_blks_read=900, shared_blks_hit=100)
    assert diagnose_query(io_heavy).resource == "io"
    assert diagnose_query(io_heavy, _profile(("lock", 88.0), ("io", 12.0))).resource == "lock"


def test_a_spread_out_profile_refuses_to_name_a_single_culprit():
    """Hiçbir kategori %40'ı geçmiyorsa tek bir kaynağa işaret etmek yanıltıcı olur."""
    diagnosis = diagnose_query(FakeRow(), _profile(("io", 35.0), ("cpu", 33.0), ("lock", 32.0)))
    assert diagnosis.resource == "unknown"
    # Ama bu bir "veri yok" değil, ölçülmüş bir dağılım — kullanıcıya öyle söyleniyor.
    assert diagnosis.confidence == "observed"
    assert "tek bir kaynakta yoğunlaşmıyor" in diagnosis.reason


def test_dominance_threshold_matches_the_database_load_service():
    """İki modülün aynı soruya farklı eşikle cevap vermesi, aynı sorgu için grafikte "IO
    baskın" derken tanıda "belirsiz" demek olurdu."""
    from app.services.database_load import DOMINANCE_THRESHOLD_PCT

    assert WAIT_DOMINANCE_THRESHOLD_PCT == DOMINANCE_THRESHOLD_PCT


def test_client_waits_say_the_problem_is_not_in_the_database():
    """Teşhisin en değerli hâli bazen "sorun burada değil"dir: sorgu süresini uygulamanın
    veriyi çekmesini bekleyerek geçiriyorsa, veritabanında iyileştirilecek bir şey yok."""
    diagnosis = diagnose_query(FakeRow(), _profile(("client", 90.0), ("cpu", 10.0)))
    assert diagnosis.resource == "client"


def test_missing_profile_falls_back_to_the_old_derivation():
    """Bekleme verisi henüz birikmemiş bir sorgu teşhissiz kalmamalı."""
    row = FakeRow(temp_blks_written=500)
    assert diagnose_query(row, None).resource == "memory"
    assert diagnose_query(row, []).resource == "memory"


def test_diagnose_queries_matches_profiles_by_queryid():
    rows = [FakeRow(queryid="a"), FakeRow(queryid="b"), FakeRow(queryid=None)]
    profiles = {"a": _profile(("io", 90.0)), "b": _profile(("lock", 90.0))}
    resources = [d.resource for d in diagnose_queries(rows, profiles)]
    assert resources == ["io", "lock", "unknown"]


def test_unknown_verdict_points_at_the_new_tab():
    """Kullanıcı "bilinmiyor" görünce ne yapacağını bilmeli — mesaj Veritabanı Yükü sekmesini
    işaret ediyor."""
    assert "Veritabanı Yükü" in diagnose_query(FakeRow()).reason


# --- Arayüz garantileri (frontend'in kendi test koşucusu yok) ----------------------------


def _panel_source() -> str:
    return (FRONTEND / "components" / "QueryDiagnosticsPanel.tsx").read_text(encoding="utf-8")


def test_every_resource_class_the_backend_can_emit_has_a_turkish_label():
    """Backend yeni bir kaynak sınıfı üretip arayüz etiket haritasına eklenmezse, kullanıcı
    tabloda BOŞ bir hücre görür — hata da vermez, sessizce eksik kalır."""
    labels = _panel_source().split("RESOURCE_LABELS_TR", 1)[1].split("}", 1)[0]
    emitted = set(WAIT_CATEGORY_TO_RESOURCE.values()) | {"memory", "unknown"}
    missing = [r for r in sorted(emitted) if f"{r}:" not in labels]
    assert not missing, f"arayüzde etiketi olmayan kaynak sınıfları: {missing}"


def test_resource_union_type_lists_the_same_classes():
    api_source = (FRONTEND / "api.ts").read_text(encoding="utf-8")
    union = re.search(r"export type QueryResourceType = ([^;]+);", api_source)
    assert union, "QueryResourceType tanımı bulunamadı"
    emitted = set(WAIT_CATEGORY_TO_RESOURCE.values()) | {"memory", "unknown"}
    missing = [r for r in sorted(emitted) if f'"{r}"' not in union.group(1)]
    assert not missing, f"QueryResourceType'ta eksik kaynak sınıfları: {missing}"


def test_the_load_tab_is_registered_in_the_detail_page():
    """Sekme yalnızca butonu eklenip TABS listesine yazılmazsa derin bağlantı
    (`?tab=load`) sessizce Özet'e düşer."""
    source = (FRONTEND / "pages" / "InstanceDetailPage.tsx").read_text(encoding="utf-8")
    # `const TABS: Tab[] = [...]` — tip anotasyonundaki `Tab[]` de köşeli parantez içeriyor,
    # bu yüzden listeyi `= [` sonrasından kesiyoruz.
    tabs_list = source.split("const TABS", 1)[1].split("= [", 1)[1].split("]", 1)[0]
    assert '"load"' in tabs_list, (
        "load sekmesi TABS listesinde yok — derin bağlantı çalışmaz"
    )
    assert 'value="load"' in source, "sekme butonu eklenmemiş"
    assert "DatabaseLoadPanel" in source


def test_wait_category_colors_cover_every_category():
    """Grafikte rengi olmayan bir kategori, diğerlerinden ayırt edilemeyen gri bir yığın
    olarak çıkar."""
    from app.domain.waits import WaitCategory

    source = (FRONTEND / "components" / "DatabaseLoadPanel.tsx").read_text(encoding="utf-8")
    colors = source.split("CATEGORY_COLORS", 1)[1].split("}", 1)[0]
    missing = [c.value for c in WaitCategory if f"{c.value}:" not in colors]
    assert not missing, f"rengi tanımlanmamış bekleme kategorileri: {missing}"


def test_the_panel_does_not_hard_code_category_labels():
    """Kategori adı ve anlamı SUNUCUDAN geliyor. Arayüzde ikinci bir çeviri tablosu tutmak,
    aynı beklemenin grafikte ve raporda iki farklı adla görünmesi demekti."""
    source = (FRONTEND / "components" / "DatabaseLoadPanel.tsx").read_text(encoding="utf-8")
    assert "category.label" in source and "category.meaning" in source
    assert "CATEGORY_LABELS" not in source
