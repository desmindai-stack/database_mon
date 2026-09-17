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


# --- Faz 31 İŞ 1: index önerisi ------------------------------------------------------------

_PAGE = FRONTEND / "pages" / "InstanceDetailPage.tsx"
_PANEL = FRONTEND / "components" / "IndexAdvicePanel.tsx"


def test_advice_failure_is_not_shown_as_no_advice():
    """Önceden hata boş bir rapora çevriliyordu ve ekranda "Index önerisi bulunamadı" yazıyordu:
    bağlantı hatası "öneri yok" gibi görünüyordu. Hata ayrı tutulup ayrı gösterilmeli."""
    page = _PAGE.read_text(encoding="utf-8")
    assert "{ advice: [], no_advice_reasons: [] }" not in page
    load = dict(_function_blocks(page))["loadAdvice"]
    assert "setAdviceError" in load
    assert "Index önerisi alınamadı" in page


def test_unmeasured_benefit_is_never_rendered_as_a_number():
    panel = _PANEL.read_text(encoding="utf-8")
    assert "estimated_improvement_pct != null" in panel
    assert "Fayda ölçülemedi" in panel
    assert "measurement_notes" in panel


def test_found_predicates_threshold_and_watch_state_are_visible():
    panel = _PANEL.read_text(encoding="utf-8")
    page = _PAGE.read_text(encoding="utf-8")
    assert "unusable_reason" in panel, "dönüştürülemeyen filtrenin nedeni gösterilmeli"
    assert "calls_now" in panel and "threshold.threshold" in panel, '"şu anda X/Y çağrı" gösterilmeli'
    assert "tekrar deneyin" not in panel.lower() and "tekrar deneyin" not in page.lower()
    assert "<AdviceWatchList" in page and "getAdviceWatches" in page
    assert "bulkAdviceSummary" in page and "getIndexAdviceBatch" in page


def test_index_advice_types_come_from_the_generated_schema():
    api = (FRONTEND / "api.ts").read_text(encoding="utf-8")
    for name in ("IndexAdvice", "NoAdviceReason", "IndexAdviceReport", "IndexPredicate", "AnalysisSettings"):
        assert f"export interface {name} " not in api, f"{name} elle yazılmış"
        assert re.search(rf'export type {name} = Gen\["\w+"\];', api), f"{name} şemadan türetilmemiş"


# --- Faz 31 İŞ 2: plan kaynakları ----------------------------------------------------------

_PLAN_PANEL = FRONTEND / "components" / "PlanSourcePanel.tsx"


def test_every_plan_source_is_listed_with_its_reason_not_filtered_out():
    panel = _PLAN_PANEL.read_text(encoding="utf-8")
    assert "sources.options.map(" in panel
    assert "sources.options.filter(" not in panel, "kullanılamayan kaynak gizlenmemeli"
    assert "option.reason" in panel and "option.caveat" in panel


def test_running_the_query_is_admin_only_and_confirmed_with_the_cost_warning():
    panel = _PLAN_PANEL.read_text(encoding="utf-8")
    assert "canWrite" in panel and 'role === "admin"' in panel
    run = panel[panel.index('option.kind === "sample_analyze" && canWrite') :]
    assert "confirm(" in run[:600] and "option.caveat" in run[:600]


def test_the_plan_shown_always_carries_its_source_label():
    tree = (FRONTEND / "components" / "ExplainPlanTree.tsx").read_text(encoding="utf-8")
    assert "source_label" in tree and "source_caveat" in tree
    page = (FRONTEND / "pages" / "InstanceDetailPage.tsx").read_text(encoding="utf-8")
    assert "<PlanSourcePanel" in page


def test_enabling_real_value_samples_requires_confirmation():
    admin = (FRONTEND / "pages" / "AdminPage.tsx").read_text(encoding="utf-8")
    block = admin[admin.index("store_real_query_samples ?? false") :][:900]
    assert "confirm(" in block and "!e.target.checked ||" in block, "açarken onay istenmeli, kapatırken değil"


def test_plan_source_types_come_from_the_generated_schema():
    api = (FRONTEND / "api.ts").read_text(encoding="utf-8")
    assert 'export type PlanSources = Gen["PlanSourcesOut"];' in api


# --- Faz 31 Commit 4: kararlar ------------------------------------------------------------


def test_unverified_advice_is_rendered_in_its_own_section_with_the_reason():
    """Karar: yetkisi eksik kullanıcıda ifade index'i "doğrulanmadı" etiketiyle AYRI bölümde."""
    panel = _PANEL.read_text(encoding="utf-8")
    assert 'a.verified === false' in panel and 'a.verified !== false' in panel
    assert "Doğrulanmamış öneriler" in panel and "Doğrulanmadı" in panel
    assert "a.verification_note" in panel


def test_required_grants_list_tables_reasons_and_one_command():
    """Karar: GRANT mesajı hangi tablolar için ve NEDEN gerektiğini açıkça yazsın."""
    panel = _PANEL.read_text(encoding="utf-8")
    block = panel[panel.index("function RequiredGrants"):]
    assert "t.table" in block and "t.reasons.map" in block and "grants.command" in block
    assert "<RequiredGrants" in panel


def test_bind_parameter_and_track_utility_reasons_reach_the_ui_instead_of_an_empty_panel():
    """Karar: bind parametreli uygulamada örnek yoksa arayüz boş kalmasın, gerekçeyi göstersin.
    Gerekçe metni sunucuda üretiliyor; panel kullanılamayan her kaynağın sebebini basıyor."""
    panel = (FRONTEND / "components" / "PlanSourcePanel.tsx").read_text(encoding="utf-8")
    assert 'option.kind !== "unavailable" && <p className="muted-note">{option.reason}</p>' in panel
    backend = (FRONTEND.parents[1] / "backend" / "app" / "services" / "plan_source.py").read_text(encoding="utf-8")
    assert "PARAMETRELİ" in backend and "bind" in backend
    assert "track_utility" in backend and "okunamadı" in backend.lower()
