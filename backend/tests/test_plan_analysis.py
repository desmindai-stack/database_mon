"""Tahmini vs gerçek satır analizi (Faz 26 İŞ 2).

Sorgu gecikmesinin en yaygın sebeplerinden biri planlayıcının satır sayısını yanlış tahmin
etmesidir: "bu koşuldan 10 satır döner" derse nested loop seçer, gerçekte 2 milyon satır
dönüyorsa aynı plan 200 bin kez iç döngü çalıştırır. Plan UCUZ görünür, pratikte pahalıdır.

Bu dosya üç tuzağı kilitliyor — üçü de sessizce yanlış sonuç üreten cinsten:

1. **Döngü çarpanı.** `Plan Rows`, `Actual Rows` ve `Actual Total Time` DÖNGÜ BAŞINADIR.
   Toplamı hesaplarken `× loops` unutulursa, nested loop'un iç tarafındaki bir düğüm
   200 bin satır yerine 1 satır gibi görünür.
2. **Kendi süresi.** `Actual Total Time` çocukları İÇERİR. Çıkarma yapılmazsa "en pahalı
   düğüm" her zaman kök düğüm çıkar ve liste hiçbir şey söylemez.
3. **Sapmanın yayılması.** Alttaki bir tarama yanılırsa üstündeki her join de yanılır.
   Hepsini suçlu göstermek, kullanıcıya hangi tabloya bakacağını söylememek demektir.
"""

from __future__ import annotations

import pytest

from app.services.plan_analysis import (
    MISESTIMATE_RATIO,
    MIN_ROWS_FOR_MISESTIMATE,
    advice_for_analysis,
    analyze_plan,
    annotate_plan_dict,
)


def node(
    node_type: str,
    *,
    plan_rows: float | None = 100,
    actual_rows: float | None = 100,
    loops: float = 1,
    total_time: float | None = 10.0,
    relation: str | None = None,
    children: list | None = None,
    **extra,
) -> dict:
    out: dict = {"Node Type": node_type}
    if relation:
        out["Relation Name"] = relation
    if plan_rows is not None:
        out["Plan Rows"] = plan_rows
    if actual_rows is not None:
        out["Actual Rows"] = actual_rows
        out["Actual Loops"] = loops
    if total_time is not None:
        out["Actual Total Time"] = total_time
    if children:
        out["Plans"] = children
    out.update(extra)
    return out


# --- Döngü çarpanı ------------------------------------------------------------------------


def test_total_rows_multiply_by_loops():
    """Nested loop'un iç tarafında `Actual Rows: 1` ama 200 bin döngü = 200 bin satır.

    Çarpmayı atlamak, sorgunun gerçekten ne kadar iş yaptığını 200 bin kat küçük gösterirdi.
    """
    plan = node(
        "Nested Loop",
        plan_rows=200000,
        actual_rows=200000,
        total_time=5000.0,
        children=[
            node("Seq Scan", relation="orders", plan_rows=200000, actual_rows=200000, total_time=100.0),
            node("Index Scan", relation="customers", plan_rows=1, actual_rows=1, loops=200000, total_time=0.02),
        ],
    )
    analysis = analyze_plan(plan)
    inner = next(n for n in analysis.nodes if n.relation_name == "customers")
    assert inner.loops == 200000
    assert inner.actual_total_rows == 200000
    assert inner.estimated_total_rows == 200000


def test_node_time_multiplies_by_loops_too():
    """`Actual Total Time` de döngü başına ORTALAMADIR. 0.02 ms × 200 bin = 4 saniye —
    tek başına bakıldığında "ihmal edilebilir" görünen düğüm aslında sorgunun tamamı."""
    plan = node(
        "Nested Loop",
        plan_rows=200000, actual_rows=200000, total_time=4200.0,
        children=[
            node("Seq Scan", relation="orders", plan_rows=200000, actual_rows=200000, total_time=100.0),
            node("Index Scan", relation="customers", plan_rows=1, actual_rows=1, loops=200000, total_time=0.02),
        ],
    )
    analysis = analyze_plan(plan)
    inner = next(n for n in analysis.nodes if n.relation_name == "customers")
    assert inner.total_time_ms == pytest.approx(4000.0)
    assert inner.time_share_pct > 90


# --- Kendi süresi -------------------------------------------------------------------------


def test_self_time_excludes_children():
    """`Actual Total Time` çocukları içerir. Çıkarma yapılmazsa en pahalı düğüm hep kök
    çıkar ve liste kullanıcıya hiçbir şey söylemez."""
    plan = node(
        "Hash Join", total_time=1000.0,
        children=[
            node("Seq Scan", relation="a", total_time=600.0),
            node("Seq Scan", relation="b", total_time=300.0),
        ],
    )
    analysis = analyze_plan(plan)
    root = analysis.nodes[0]
    assert root.total_time_ms == pytest.approx(1000.0)
    assert root.self_time_ms == pytest.approx(100.0)


def test_hottest_nodes_are_ranked_by_self_time_not_total():
    plan = node(
        "Hash Join", total_time=1000.0,
        children=[
            node("Seq Scan", relation="buyuk", total_time=850.0),
            node("Seq Scan", relation="kucuk", total_time=50.0),
        ],
    )
    analysis = analyze_plan(plan)
    assert analysis.hottest[0].relation_name == "buyuk"
    assert analysis.hottest[0].time_share_pct == pytest.approx(85.0, abs=0.5)


def test_self_time_never_goes_negative():
    """Ölçüm gürültüsü yüzünden çocukların toplamı kökü aşabilir; negatif süre saçmadır
    ve sıralamayı bozar."""
    plan = node(
        "Hash Join", total_time=100.0,
        children=[node("Seq Scan", relation="a", total_time=105.0)],
    )
    analysis = analyze_plan(plan)
    assert analysis.nodes[0].self_time_ms == 0.0


# --- Sapma tespiti ------------------------------------------------------------------------


def test_a_large_underestimate_is_flagged():
    plan = node("Seq Scan", relation="orders", plan_rows=10, actual_rows=200000)
    analysis = analyze_plan(plan)
    flagged = analysis.nodes[0]
    assert flagged.misestimated is True
    assert flagged.underestimated is True
    assert flagged.estimate_ratio == pytest.approx(20000.0)


def test_a_large_overestimate_is_flagged_too():
    """Fazla tahmin de plan seçimini bozar (gereksiz hash join, gereksiz sıralama) —
    yalnızca az tahmine bakmak sorunun yarısını görmezden gelmek olurdu."""
    plan = node("Seq Scan", relation="orders", plan_rows=500000, actual_rows=120)
    analysis = analyze_plan(plan)
    flagged = analysis.nodes[0]
    assert flagged.misestimated is True
    assert flagged.underestimated is False


def test_small_absolute_numbers_are_not_flagged_even_at_a_big_ratio():
    """1 yerine 15 satır dönmesi 15x sapmadır ama hiçbir planı değiştirmez. Bunu
    işaretlemek, kullanıcıyı önemsiz uyarılarla boğar ve gerçek sapmaları gizler."""
    plan = node("Index Scan", relation="t", plan_rows=1, actual_rows=15)
    analysis = analyze_plan(plan)
    assert analysis.nodes[0].misestimated is False
    assert analysis.misestimated == []


def test_the_threshold_boundary_behaves_as_documented():
    below = analyze_plan(node("Seq Scan", relation="t", plan_rows=1000, actual_rows=1000 * (MISESTIMATE_RATIO - 1)))
    at = analyze_plan(node("Seq Scan", relation="t", plan_rows=1000, actual_rows=1000 * MISESTIMATE_RATIO))
    assert below.nodes[0].misestimated is False
    assert at.nodes[0].misestimated is True
    assert MIN_ROWS_FOR_MISESTIMATE > 0


# --- Kök neden ----------------------------------------------------------------------------


def test_only_the_deepest_misestimate_is_the_root_cause():
    """SAPMA YUKARI YAYILIR. Alttaki tarama 10 yerine 200 bin satır döndürdüyse üstündeki
    join de yanılır. İkisini de suçlu göstermek, kullanıcıya hangi tabloya bakacağını
    söylememek demektir."""
    plan = node(
        "Nested Loop", plan_rows=10, actual_rows=200000, total_time=5000.0,
        children=[
            node("Seq Scan", relation="orders", plan_rows=10, actual_rows=200000, total_time=800.0),
            node("Index Scan", relation="customers", plan_rows=1, actual_rows=1, loops=200000, total_time=0.01),
        ],
    )
    analysis = analyze_plan(plan)
    assert len(analysis.misestimated) == 2, "hem join hem tarama sapmış olarak görünmeli"
    assert [n.relation_name for n in analysis.root_causes] == ["orders"]
    assert analysis.nodes[0].is_root_cause is False


def test_two_independent_misestimates_are_both_root_causes():
    """Aynı derinlikte iki farklı tablo yanlış tahmin edildiyse ikisi de kök nedendir;
    birini seçmek keyfi olurdu."""
    plan = node(
        "Hash Join", plan_rows=100, actual_rows=100, total_time=900.0,
        children=[
            node("Seq Scan", relation="a", plan_rows=10, actual_rows=5000, total_time=400.0),
            node("Seq Scan", relation="b", plan_rows=8000, actual_rows=200, total_time=300.0),
        ],
    )
    analysis = analyze_plan(plan)
    assert {n.relation_name for n in analysis.root_causes} == {"a", "b"}


# --- Gerçek satır yokluğu -----------------------------------------------------------------


def test_a_plan_without_actual_rows_says_why_instead_of_reporting_zero_deviation():
    """ANALYZE'siz bir planda gerçek satır YOK. "Sapma yok" demek, ölçüm yapılmadığı hâlde
    her şeyin yolunda olduğunu iddia etmek olurdu."""
    plan = node("Seq Scan", relation="orders", plan_rows=100, actual_rows=None, total_time=None)
    analysis = analyze_plan(plan)
    assert analysis.has_actual_rows is False
    assert analysis.misestimated == []
    assert analysis.unavailable_reason
    assert "log_analyze" in analysis.unavailable_reason


def test_advice_for_a_plan_without_actuals_is_unavailable_with_a_reason():
    plan = node("Seq Scan", relation="orders", plan_rows=100, actual_rows=None, total_time=None)
    advice = advice_for_analysis(analyze_plan(plan))
    assert advice is not None
    assert advice.unavailable_reason


def test_no_advice_when_there_is_no_misestimate():
    """Sapmamış bir plan için "istatistiklerinizi güncelleyin" demek, olmayan bir sorunu
    varmış gibi göstermek olurdu."""
    plan = node("Seq Scan", relation="orders", plan_rows=1000, actual_rows=1050)
    assert advice_for_analysis(analyze_plan(plan)) is None


# --- Öneri içeriği ------------------------------------------------------------------------


def test_advice_follows_the_five_part_standard():
    plan = node("Seq Scan", relation="orders", plan_rows=10, actual_rows=200000, Filter="(status = 'open')")
    advice = advice_for_analysis(analyze_plan(plan))
    assert advice.title and advice.why
    assert advice.steps and any(s.command for s in advice.steps)
    assert advice.cautions
    assert advice.verification
    assert advice.rollback


def test_advice_starts_with_analyze_the_cheapest_fix():
    """Sıra önemli: ANALYZE saniyeler sürer ve sapmaların çoğunu çözer. Kullanıcıyı önce
    CREATE STATISTICS'e yollamak, çoğu durumda gereksiz karmaşıklık olurdu."""
    plan = node("Seq Scan", relation="orders", plan_rows=10, actual_rows=200000)
    advice = advice_for_analysis(analyze_plan(plan))
    commands = [s.command or "" for s in advice.steps]
    assert "pg_stat_user_tables" in commands[0]
    assert "ANALYZE orders;" in commands[1]


def test_correlated_columns_trigger_extended_statistics_advice():
    """Planlayıcı kolonları BAĞIMSIZ varsayar ve seçicilikleri çarpar; ilişkili kolonlarda
    (şehir/posta kodu) bu korkunç bir az-tahmin üretir."""
    plan = node(
        "Seq Scan", relation="addresses", plan_rows=5, actual_rows=90000,
        Filter="((city = 'Ankara'::text) AND (postal_code = '06'::text))",
    )
    advice = advice_for_analysis(analyze_plan(plan))
    blob = " ".join(s.command or "" for s in advice.steps)
    assert "CREATE STATISTICS" in blob
    assert "dependencies" in blob
    assert "city" in blob and "postal_code" in blob


def test_expression_filters_trigger_expression_statistics_advice():
    """Planlayıcı ifadeler için istatistik TUTMAZ ve varsayılan seçiciliğe düşer."""
    plan = node(
        "Seq Scan", relation="users", plan_rows=3, actual_rows=50000,
        Filter="(lower((email)::text) = 'a@b.c'::text)",
    )
    advice = advice_for_analysis(analyze_plan(plan))
    blob = " ".join((s.action or "") + (s.command or "") for s in advice.steps)
    assert "lower" in blob
    assert "CREATE INDEX CONCURRENTLY" in blob or "CREATE STATISTICS" in blob


def test_advice_always_offers_the_statistics_target_step():
    plan = node("Seq Scan", relation="orders", plan_rows=10, actual_rows=200000)
    advice = advice_for_analysis(analyze_plan(plan))
    blob = " ".join(s.command or "" for s in advice.steps)
    assert "SET STATISTICS" in blob


def test_advice_explains_the_business_consequence_not_just_the_numbers():
    plan = node("Seq Scan", relation="orders", plan_rows=10, actual_rows=200000)
    advice = advice_for_analysis(analyze_plan(plan))
    assert "nested loop" in advice.why.lower()
    assert "ucuz" in advice.why.lower()


def test_advice_warns_that_create_statistics_alone_does_nothing():
    """CREATE STATISTICS yalnızca ANALYZE sonrasında etkili olur — bunu bilmeden komutu
    çalıştıran kullanıcı "işe yaramadı" diye vazgeçer."""
    plan = node(
        "Seq Scan", relation="t", plan_rows=5, actual_rows=90000,
        Filter="((a = 1) AND (b = 2))",
    )
    advice = advice_for_analysis(analyze_plan(plan))
    assert any("ANALYZE sonrasında" in c for c in advice.cautions)


# --- Plan ağacına işaretleme --------------------------------------------------------------


def test_annotate_writes_fields_onto_the_plan_tree():
    plan = node(
        "Nested Loop", plan_rows=10, actual_rows=200000, total_time=5000.0,
        children=[node("Seq Scan", relation="orders", plan_rows=10, actual_rows=200000, total_time=800.0)],
    )
    analysis = analyze_plan(plan)
    tree = {
        "node_type": "Nested Loop",
        "children": [{"node_type": "Seq Scan", "children": []}],
    }
    annotate_plan_dict(tree, analysis)
    assert tree["misestimated"] is True
    assert tree["is_root_cause"] is False
    assert tree["children"][0]["is_root_cause"] is True
    assert tree["children"][0]["estimate_ratio"] == pytest.approx(20000.0)


def test_annotate_refuses_to_write_when_the_trees_do_not_line_up():
    """Yanlış düğüme "sapmış" etiketi koymak, kullanıcıyı masum bir tabloya yollamak olurdu.
    Eşleşme bozuksa hiçbir şey yazılmıyor."""
    analysis = analyze_plan(node("Seq Scan", relation="orders", plan_rows=10, actual_rows=200000))
    tree = {
        "node_type": "Nested Loop",
        "children": [{"node_type": "Seq Scan", "children": []}],
    }
    annotate_plan_dict(tree, analysis)
    assert "misestimated" not in tree
