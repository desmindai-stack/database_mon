"""Bekleme tipine göre öneri (Faz 25 İŞ 4).

Bekleme analizinin değeri ölçümde değil, ölçümün EYLEME dönüşmesinde. Bu dosya üç şeyi
kilitliyor:

1. **Beş parçalı standart** (CLAUDE.md kuralı): neden (iş etkisiyle) → numaralı adımlar →
   adım başına komut → dikkat notları → doğrulama. Eksik parça, kullanıcının uygulayamadığı
   bir öneri demek.
2. **Öneri üretilemiyorsa NEDENİ yazılır.** Boş kutu ya da genel geçer bir cümle yasak.
3. **Komutlar motora ait.** PostgreSQL komutunu SQL Server'a önermek, kullanıcının
   kopyalayıp yapıştırdığında hata alması demek — çalışmayan öneri, öneri değildir.
"""

from __future__ import annotations

import pytest

from app.domain.waits import CATEGORY_ORDER, IDLE_CATEGORIES, WaitCategory
from app.services.wait_advice import ADVICE_MIN_SHARE_PCT, advice_for_wait_category

PG = "postgresql"
MSSQL = "sqlserver"

#: Yük üreten (boşta olmayan) kategoriler — öneri sistemi bunların hepsiyle karşılaşabilir.
LOAD_CATEGORIES = [c for c in CATEGORY_ORDER if c not in IDLE_CATEGORIES]


def _actionable(advice) -> bool:
    return advice.unavailable_reason is None


def _says(text: str, phrase: str) -> bool:
    """Büyük/küçük harf duyarsız arama — TÜRKÇEYE UYGUN.

    Python'ın `str.lower()`'ı Türkçe "İ"yi "i" + birleşen nokta (U+0307) olarak veriyor, yani
    "DEĞİLDİR".lower() != "değildir". Metinlerde vurgu için büyük harf kullanıldığından
    (öneriler kullanıcıya görünen Türkçe metinler) bu tuzağa düşmek kolay: test yeşil
    görünürken aslında hiçbir şeyi kontrol etmiyor olurdu.
    """
    normalize = lambda t: t.replace("İ", "i").replace("I", "ı").lower()
    return normalize(phrase) in normalize(text)


# --- Beş parçalı standart ---------------------------------------------------------------


@pytest.mark.parametrize("engine", [PG, MSSQL])
@pytest.mark.parametrize("category", [c.value for c in LOAD_CATEGORIES])
def test_every_category_gets_either_a_full_plan_or_a_stated_reason(engine, category):
    """BOŞ ÖNERİ YOK. Her kategori ya tam bir eylem planı ya da neden plan olmadığını
    söyleyen bir açıklama üretir."""
    advice = advice_for_wait_category(category, 80.0, engine=engine)
    if not _actionable(advice):
        assert len(advice.unavailable_reason) > 40, (
            f"{engine}/{category}: 'öneri yok' cevabı sebebini açıklamıyor"
        )
        return

    assert advice.title
    assert advice.why, f"{engine}/{category}: neden yok"
    assert advice.steps, f"{engine}/{category}: adım yok"
    assert any(s.command for s in advice.steps), f"{engine}/{category}: hiçbir adımda komut yok"
    assert advice.cautions, f"{engine}/{category}: dikkat notu yok"
    assert advice.verification, f"{engine}/{category}: doğrulama yok"


@pytest.mark.parametrize("engine", [PG, MSSQL])
@pytest.mark.parametrize("category", [c.value for c in LOAD_CATEGORIES])
def test_the_reason_states_business_impact_not_just_a_definition(engine, category):
    """"Neden" alanı ne olduğunu değil, YAPILMAZSA NE OLACAĞINI anlatmalı — aksi halde
    öncelik verilemez."""
    advice = advice_for_wait_category(category, 80.0, engine=engine)
    if not _actionable(advice):
        return
    assert len(advice.why) > 120, f"{engine}/{category}: gerekçe iş etkisi anlatacak kadar uzun değil"
    # Payın kendisi gerekçede geçmeli: kullanıcı hangi ölçüme dayandığını görsün.
    assert "%80" in advice.why


# --- Öneri üretilemeyen durumlar --------------------------------------------------------


def test_a_weak_leader_does_not_get_an_action_plan():
    """Yükün %35'ini açıklayan bir kategoriye göre eylem önermek, kullanıcıyı yükün
    yarısından azını hedefleyen bir işe yönlendirmek olurdu."""
    advice = advice_for_wait_category("io", ADVICE_MIN_SHARE_PCT - 5, engine=PG)
    assert not _actionable(advice)
    assert "%35" in advice.unavailable_reason


def test_an_unknown_category_is_refused_not_guessed():
    advice = advice_for_wait_category("kozmik_isinlar", 90.0, engine=PG)
    assert not _actionable(advice)
    assert "kozmik_isinlar" in advice.unavailable_reason


def test_unsupported_engine_says_so():
    advice = advice_for_wait_category("io", 90.0, engine="mongodb")
    assert not _actionable(advice)
    assert "mongodb" in advice.unavailable_reason


# --- İçerik doğruluğu -------------------------------------------------------------------


@pytest.mark.parametrize("engine", [PG, MSSQL])
def test_client_waits_send_the_user_away_from_the_database(engine):
    """ÜRÜNÜN VEREBİLECEĞİ EN DEĞERLİ CEVAPLARDAN BİRİ: "sorun burada değil".

    Bunu gizleyip yerine genel bir "sorgularınızı gözden geçirin" önerisi üretmek, ölçümü
    çöpe atmak ve ekibi haftalarca yanlış yerde arattırmak olurdu.
    """
    advice = advice_for_wait_category("client", 85.0, engine=engine)
    assert _actionable(advice)
    assert _says(advice.title, "uygulama")
    assert _says(advice.why, "veritabanı sorunu değildir")
    # Yanlış yönlendirmeye karşı açık uyarı.
    assert any(_says(c, "zaman kaybı") for c in advice.cautions)


def test_lock_advice_says_adding_hardware_will_not_help():
    """Kilit beklemesi kaynak sorunu değildir; sunucu büyütmek para ve zaman kaybıdır."""
    advice = advice_for_wait_category("lock", 70.0, engine=PG)
    assert _says(advice.why, "kaynak sorunu değildir")
    assert "pg_blocking_pids" in " ".join(s.command or "" for s in advice.steps)


def test_lwlock_advice_warns_against_raising_max_connections():
    """LWLock çekişmesinde max_connections'ı ARTIRMAK sorunu büyütür — en sık yapılan
    yanlış refleks."""
    advice = advice_for_wait_category("lwlock", 70.0, engine=PG)
    assert any("max_connections" in c and "ARTIRMAK" in c for c in advice.cautions)


def test_io_advice_uses_concurrently_and_warns_about_it():
    """CREATE INDEX (CONCURRENTLY'siz) tabloyu yazmaya kapatır — canlıda kabul edilemez."""
    advice = advice_for_wait_category("io", 78.0, engine=PG)
    commands = " ".join(s.command or "" for s in advice.steps)
    assert "CREATE INDEX CONCURRENTLY" in commands
    assert any("CONCURRENTLY" in c for c in advice.cautions)
    assert advice.rollback and "DROP INDEX CONCURRENTLY" in advice.rollback


def test_memory_advice_warns_that_work_mem_is_per_operation():
    """work_mem'i global artırmanın klasik tuzağı: bağlantı × işlem ile çarpılır ve sunucu
    belleği tükenir."""
    advice = advice_for_wait_category("memory", 60.0, engine=PG)
    assert any(_says(c, "işlem başına") for c in advice.cautions)


def test_cpu_advice_warns_against_more_parallel_workers_when_cpu_is_full():
    advice = advice_for_wait_category("cpu", 75.0, engine=PG)
    assert any("KÖTÜLEŞTİRİR" in c for c in advice.cautions)


# --- Motor ayrımı -----------------------------------------------------------------------


PG_ONLY_TOKENS = ["pg_stat_activity", "postgresql.conf", "pg_terminate_backend", "work_mem"]
MSSQL_ONLY_TOKENS = ["sys.dm_", "ALTER DATABASE", "UPDATE STATISTICS", "@@SPID"]


@pytest.mark.parametrize("category", [c.value for c in LOAD_CATEGORIES])
def test_postgres_advice_never_contains_sql_server_syntax(category):
    """Kopyalanıp yapıştırıldığında hata veren komut, öneri değildir."""
    advice = advice_for_wait_category(category, 80.0, engine=PG)
    if not _actionable(advice) or category == "client":
        return  # client önerisi motordan bağımsız
    blob = " ".join([advice.verification or "", *(s.command or "" for s in advice.steps)])
    leaked = [t for t in MSSQL_ONLY_TOKENS if t in blob]
    assert not leaked, f"PostgreSQL önerisinde SQL Server sözdizimi: {leaked}"


@pytest.mark.parametrize("category", [c.value for c in LOAD_CATEGORIES])
def test_sqlserver_advice_never_contains_postgres_syntax(category):
    advice = advice_for_wait_category(category, 80.0, engine=MSSQL)
    if not _actionable(advice) or category == "client":
        return
    blob = " ".join([advice.verification or "", *(s.command or "" for s in advice.steps)])
    leaked = [t for t in PG_ONLY_TOKENS if t in blob]
    assert not leaked, f"SQL Server önerisinde PostgreSQL sözdizimi: {leaked}"


# --- Yük üreten sorgunun önerisine gömülmesi --------------------------------------------


def test_the_top_query_is_embedded_in_the_explain_step():
    """Adım somut bir komut olmalı, kullanıcının doldurması gereken bir şablon değil."""
    advice = advice_for_wait_category(
        "io", 80.0, engine=PG, top_query="SELECT * FROM orders WHERE customer_id = $1"
    )
    commands = " ".join(s.command or "" for s in advice.steps)
    assert "SELECT * FROM orders WHERE customer_id = $1" in commands


def test_without_a_top_query_the_step_is_still_readable():
    advice = advice_for_wait_category("io", 80.0, engine=PG, top_query=None)
    commands = " ".join(s.command or "" for s in advice.steps)
    assert "<yük üreten sorgu>" in commands


# --- Rapor entegrasyonu -----------------------------------------------------------------


def test_the_dominance_threshold_is_shared_with_the_load_service():
    from app.services.database_load import DOMINANCE_THRESHOLD_PCT

    assert ADVICE_MIN_SHARE_PCT == DOMINANCE_THRESHOLD_PCT, (
        "Grafik 'IO baskın' derken öneri 'baskın kaynak yok' diyebilirdi."
    )


def test_every_wait_category_is_covered_by_the_advice_switch():
    """Yeni bir kategori eklenip öneri tarafına yazılmazsa, kullanıcı grafikte kategoriyi
    görür ama ne yapacağını asla öğrenemez. Sessiz kalmak yerine 'plan yok' demek de kabul
    edilebilir bir cevap — ama SESSİZLİK değil."""
    for category in WaitCategory:
        for engine in (PG, MSSQL):
            advice = advice_for_wait_category(category.value, 90.0, engine=engine)
            assert advice.title
            assert _actionable(advice) or advice.unavailable_reason
