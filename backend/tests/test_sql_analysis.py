"""SQL çözümleme ve EXPLAIN stratejisi (Faz 27 İŞ 1).

CANLIDA ALINAN İKİ HATA bu dosyanın varlık sebebi:

1. `EXPLAIN başarısız: missing FROM-clause entry for table "pn"` — `pn` bir TAKMA AD.
   Hata, sorgu metninin `track_activity_query_size` sınırında KESİLMİŞ olmasından çıkıyor:
   FROM yan tümcesi kırpılınca geriye `pn.kolon` referansları kalıyor ve EXPLAIN haklı
   olarak "böyle bir tablo yok" diyor.
2. `'public.recurse' tablosu bu veritabanında bulunamadı` — `recurse` bir CTE adı. Elle
   yazılmış regex `FROM recurse` görüp gerçek tablo sandı; index önerisi hiç üretilemedi.

Her ikisi de aynı kök nedenin iki yüzü: **emin değilken tahmin etmek.** Testler artık
tahmin edilmediğini kilitliyor.
"""

from __future__ import annotations

import pytest

from app.services.sql_analysis import (
    DEFAULT_TRACK_ACTIVITY_QUERY_SIZE,
    PG_VERSION_GENERIC_PLAN,
    analyze_query,
    detect_truncation,
    humanize_postgres_error,
    plan_explain_strategy,
)

# Canlıda hataya yol açan sorgunun sadeleştirilmiş hâli.
RECURSIVE_CTE_QUERY = """
WITH RECURSIVE recurse AS (
    SELECT 1 AS n
    UNION ALL
    SELECT n + 1 FROM recurse WHERE n < 5
)
SELECT pn.name, o.total
FROM person pn
JOIN orders o ON o.person_id = pn.id
CROSS JOIN recurse r
WHERE pn.city = $1
"""


# --- CTE ve takma ad ayrımı ---------------------------------------------------------------


def test_a_cte_name_is_not_treated_as_a_table():
    """CANLI HATA 1'İN KÖKÜ. `recurse` bir CTE adıdır; katalogda aranırsa bulunamaz ve
    index önerisi hiç üretilemez."""
    analysis = analyze_query(RECURSIVE_CTE_QUERY)
    names = {t.name.lower() for t in analysis.tables}
    assert "recurse" not in names, "CTE adı gerçek tablo sanıldı"
    assert names == {"person", "orders"}
    assert "recurse" in {n.lower() for n in analysis.cte_names}


def test_aliases_resolve_to_their_real_table():
    analysis = analyze_query(RECURSIVE_CTE_QUERY)
    resolved = analysis.table_for_alias("pn")
    assert resolved is not None
    assert resolved.qualified == "public.person"
    assert analysis.table_for_alias("o").name == "orders"


def test_a_subquery_alias_is_not_a_table():
    analysis = analyze_query(
        "SELECT x.total FROM (SELECT sum(amount) AS total FROM payments) x WHERE x.total > 5"
    )
    assert {t.name.lower() for t in analysis.tables} == {"payments"}
    assert analysis.is_not_a_table("x")


def test_a_values_list_alias_is_not_a_table():
    analysis = analyze_query(
        "SELECT v.a FROM (VALUES (1, 2), (3, 4)) AS v(a, b) JOIN orders o ON o.id = v.a"
    )
    assert {t.name.lower() for t in analysis.tables} == {"orders"}


def test_a_set_returning_function_is_not_a_table():
    """`FROM generate_series(...)` bir tablo değil; katalogda aranırsa bulunamaz."""
    analysis = analyze_query("SELECT g FROM generate_series(1, 10) AS g")
    assert analysis.tables == []


def test_schema_qualified_names_are_resolved():
    analysis = analyze_query("SELECT * FROM billing.invoices i WHERE i.state = $1")
    assert analysis.tables[0].schema == "billing"
    assert analysis.tables[0].name == "invoices"
    assert analysis.tables[0].qualified == "billing.invoices"


def test_unqualified_names_default_to_public():
    analysis = analyze_query("SELECT * FROM orders")
    assert analysis.tables[0].schema == "public"


def test_the_same_table_joined_twice_is_not_duplicated_per_alias():
    analysis = analyze_query(
        "SELECT * FROM person a JOIN person b ON b.parent_id = a.id"
    )
    assert {t.alias for t in analysis.tables} == {"a", "b"}
    assert {t.name for t in analysis.tables} == {"person"}


def test_an_unparsable_query_yields_no_tables_and_says_why():
    """TAHMİN YOK. Yanlış tablo adı üretmektense hiç üretmemek yeğdir — canlıdaki iki
    hatanın kaynağı tam olarak "emin değilken tahmin etmek"ti."""
    analysis = analyze_query("SELECT * FROM WHERE ORDER BY )))")
    assert analysis.tables == []
    assert analysis.parse_error


def test_placeholders_are_detected():
    assert analyze_query("SELECT * FROM t WHERE a = $1").has_placeholders is True
    assert analyze_query("SELECT * FROM t WHERE a = 5").has_placeholders is False


# --- Kesilmiş metin ------------------------------------------------------------------------


def test_a_trailing_ellipsis_marks_truncation():
    """pg_stat_statements kırptığı metnin sonuna üç nokta koyar — en güçlü sinyal."""
    verdict = detect_truncation("SELECT a, b FROM orders WHERE status = ...")
    assert verdict.truncated is True
    assert "..." in verdict.reason
    assert "track_activity_query_size" in (verdict.fix or "")


def test_unbalanced_parentheses_mark_truncation():
    verdict = detect_truncation("SELECT * FROM t WHERE (a = 1 AND (b = 2")
    assert verdict.truncated is True
    assert "kapanmamış parantez" in verdict.reason


def test_parentheses_inside_string_literals_do_not_trigger_a_false_alarm():
    """`'a(b'` meşru bir sabittir. Dizgi içindeki parantezi saymak, tamamen sağlam bir
    sorguyu "kesilmiş" diye reddetmek olurdu."""
    assert detect_truncation("SELECT * FROM t WHERE label = 'yarim(parantez'").truncated is False


def test_doubled_quotes_inside_a_literal_are_handled():
    assert detect_truncation("SELECT * FROM t WHERE s = 'it''s fine'").truncated is False


def test_a_long_but_valid_query_is_not_called_truncated():
    """Tek başına uzunluk yeterli sinyal değil: tam olarak sınıra denk gelen geçerli bir
    sorgu da olabilir."""
    filler = ", ".join(f"col_{i}" for i in range(300))
    query = f"SELECT {filler} FROM orders"
    assert len(query) > DEFAULT_TRACK_ACTIVITY_QUERY_SIZE
    assert detect_truncation(query).truncated is False


def test_a_long_and_unparsable_query_is_called_truncated():
    query = "SELECT " + ", ".join(f"col_{i}" for i in range(300)) + " FROM orders WHERE a IN (1, 2"
    assert detect_truncation(query).truncated is True


def test_a_short_query_is_never_called_truncated():
    assert detect_truncation("SELECT 1").truncated is False


def test_empty_text_is_not_truncated():
    assert detect_truncation("   ").truncated is False


# --- EXPLAIN stratejisi --------------------------------------------------------------------


def test_a_truncated_query_is_refused_before_reaching_the_server():
    """CANLI HATA 2'NİN ÖNLENMESİ. Eksik metne EXPLAIN çalıştırmak, sorguyla ilgisi olmayan
    bir sözdizimi hatası üretiyordu ("missing FROM-clause entry for table pn")."""
    plan = plan_explain_strategy(
        "SELECT pn.name, pn.city FROM person pn WHERE pn.id IN (SELECT ...",
        server_version_num=160000,
    )
    assert plan.can_explain is False
    assert "missing FROM-clause entry" in plan.reason
    assert "track_activity_query_size" in (plan.fix or "")


def test_a_plain_query_is_explained_normally():
    plan = plan_explain_strategy("SELECT * FROM orders WHERE id = 5", server_version_num=150000)
    assert plan.can_explain is True
    assert plan.options == "FORMAT JSON"
    assert plan.caveat is None


def test_analyze_adds_buffers():
    plan = plan_explain_strategy(
        "SELECT * FROM orders WHERE id = 5", server_version_num=150000, analyze=True
    )
    assert plan.can_explain is True
    assert "ANALYZE" in plan.options and "BUFFERS" in plan.options


def test_placeholders_use_generic_plan_on_pg16():
    """Yer tutucuların yerine NULL koymak planlayıcıya BAŞKA bir sorgu sunar; dönen plan
    gerçek çalıştırmanın planı olmadığı hâlde öyleymiş gibi gösterilirdi."""
    plan = plan_explain_strategy(
        "SELECT * FROM orders WHERE id = $1", server_version_num=PG_VERSION_GENERIC_PLAN
    )
    assert plan.can_explain is True
    assert "GENERIC_PLAN" in plan.options
    # Sınır kullanıcıya söyleniyor: değerden bağımsız plan, gerçek plan olmayabilir.
    assert plan.caveat and "GENERIC_PLAN" in plan.caveat


def test_placeholders_are_refused_before_pg16_with_a_real_explanation():
    plan = plan_explain_strategy(
        "SELECT * FROM orders WHERE id = $1", server_version_num=150000
    )
    assert plan.can_explain is False
    assert "GENERIC_PLAN" in plan.reason
    assert "değer uydurmak" in plan.reason
    assert "auto_explain" in (plan.fix or "")


def test_analyze_on_a_parameterized_query_is_refused_on_every_version():
    """EXPLAIN ANALYZE sorguyu GERÇEKTEN çalıştırır. Uydurma değerlerle çalıştırmak hem
    yanıltıcı bir plan verir hem de izlenen veritabanında öngörülemez maliyet çıkarır."""
    for version in (150000, 160000, 180000):
        plan = plan_explain_strategy(
            "SELECT * FROM orders WHERE id = $1", server_version_num=version, analyze=True
        )
        assert plan.can_explain is False, f"sürüm {version}"
        assert "GERÇEKTEN çalıştırdığı" in plan.reason


def test_an_unparsable_query_is_refused_with_the_parse_reason():
    plan = plan_explain_strategy("SELECT FROM WHERE )))", server_version_num=160000)
    assert plan.can_explain is False
    assert plan.reason


# --- Hata çevirisi -------------------------------------------------------------------------


def test_missing_from_clause_error_explains_truncation_not_the_alias():
    """Kullanıcıya ham `missing FROM-clause entry for table "pn"` göstermek onu hiçbir yere
    götürmüyor: 'pn' diye bir tablo aramaya başlıyor. Asıl sebep metnin kesilmesi."""
    message = humanize_postgres_error('missing FROM-clause entry for table "pn"')
    assert "pn" in message
    assert "KESİLMİŞ" in message
    assert "track_activity_query_size" in message


def test_relation_does_not_exist_mentions_the_cte_possibility():
    message = humanize_postgres_error('relation "recurse" does not exist')
    assert "recurse" in message
    assert "CTE" in message


def test_permission_denied_says_what_to_grant():
    message = humanize_postgres_error("permission denied for table orders")
    assert "GRANT SELECT" in message


def test_statement_timeout_is_explained():
    message = humanize_postgres_error("canceling statement due to statement timeout")
    assert "zaman aşımına" in message


def test_an_unknown_error_keeps_the_raw_text():
    """Uydurma bir açıklama yazmaktansa ham hatayı göstermek yeğdir — en azından
    aranabilir."""
    message = humanize_postgres_error("some brand new error nobody mapped")
    assert "some brand new error nobody mapped" in message


@pytest.mark.parametrize(
    "raw",
    [
        'missing FROM-clause entry for table "x"',
        'relation "y" does not exist',
        'column "z" does not exist',
        "could not determine data type of parameter $1",
        "permission denied for table t",
        "canceling statement due to statement timeout",
        "syntax error at or near \"SELECT\"",
    ],
)
def test_every_mapped_error_produces_turkish_guidance_not_raw_sql_jargon(raw):
    message = humanize_postgres_error(raw)
    assert len(message) > 60, "açıklama ne yapılacağını söyleyecek kadar uzun değil"
    assert message != raw
