"""Filtre kolonu (predicate) çıkarımı — sözdizim ağacından (Faz 31 İŞ 1b).

Saf ayrıştırma testleri; veritabanı gerekmiyor. Katalog gerektiren çözümler (niteliksiz
kolonun sahibi, ifadenin IMMUTABLE olup olmadığı) gerçek sunucuda:
`test_index_advice_live_postgres.py`.

Eski regex çıkarıcısının ÖLÇÜLEN hataları her biri bir testle sabit:
`lower(email)` görünmüyordu, `status::text` sorgusunda `text` kolon sanılıyordu, `JOIN` /
`EXISTS` kelimelerinden `JO` / `EX` kolonları üretiliyordu.
"""

from __future__ import annotations

from app.services.sql_predicates import extract_predicates


def _preds(sql: str):
    result = extract_predicates(sql)
    assert result.parse_error is None, result.parse_error
    return result.predicates


def _summary(sql: str) -> set[tuple]:
    return {
        (p.column, p.kind, p.source.table if p.source else None, p.expression, p.usable)
        for p in _preds(sql)
    }


def test_placeholders_and_literals_give_identical_predicates():
    """`$1` hipotezi: yer tutucular çıkarımı bozmuyor."""
    with_params = _summary("SELECT * FROM orders o WHERE o.status = $1 AND o.total > $2")
    with_literals = _summary("SELECT * FROM orders o WHERE o.status = 'paid' AND o.total > 10")
    assert with_params == with_literals == {
        ("status", "eq", "orders", None, True),
        ("total", "range", "orders", None, True),
    }


def test_function_wrapped_column_becomes_an_expression_predicate():
    assert _summary("SELECT * FROM users u WHERE lower(u.email) = $1") == {
        ("email", "eq", "users", "lower(email)", True)
    }


def test_cast_is_an_expression_and_the_type_name_is_not_a_column():
    preds = _preds("SELECT * FROM orders WHERE status::text = $1")
    assert [p.column for p in preds] == ["status"]
    assert preds[0].expression == "(status)::text"


def test_expression_text_keeps_the_original_constant_spelling():
    """sqlglot 'day' → 'DAY' yapıyor; PostgreSQL ifade index'ini sabit DEĞERİYLE eşleştirir.
    'DAY' ile kurulan index 'day' yazan sorguda kullanılmaz."""
    preds = _preds("SELECT * FROM events e WHERE date_trunc('day', e.happened_at) = $1")
    assert preds[0].expression == "date_trunc('day', happened_at)"


def test_join_keywords_do_not_produce_phantom_columns():
    columns = {p.column for p in _preds(
        "SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id "
        "WHERE EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.id)"
    )}
    assert columns == {"id", "customer_id", "order_id"}


def test_unqualified_column_with_two_sources_is_left_to_the_catalog():
    preds = _preds("SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id WHERE status = $1")
    status = next(p for p in preds if p.column == "status")
    assert status.source is None
    assert {c.table for c in status.candidates} == {"orders", "customers"}


def test_all_conditions_of_a_join_on_are_found():
    assert _summary(
        "SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id AND c.segment = $1"
    ) >= {("segment", "eq", "customers", None, True), ("customer_id", "join", "orders", None, True)}


def test_filter_inside_a_cte_is_attributed_to_the_real_table():
    preds = _preds("WITH r AS (SELECT * FROM orders WHERE created_at > $1) SELECT status FROM r WHERE total > $2")
    by_column = {p.column: p for p in preds}
    assert by_column["created_at"].source.table == "orders"
    assert by_column["created_at"].context == "cte"
    assert by_column["total"].source.table == "orders", "CTE kolonu içeri doğru izlenmeli"


def test_correlated_exists_resolves_the_outer_alias():
    by_column = {p.column: p for p in _preds(
        "SELECT * FROM customers c WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id AND o.status = $1)"
    )}
    assert by_column["id"].source.table == "customers"
    assert by_column["customer_id"].kind == "join"
    assert by_column["status"].context == "exists"


def test_in_subquery_both_sides():
    assert _summary("SELECT * FROM orders WHERE customer_id IN (SELECT id FROM customers WHERE segment = $1)") == {
        ("customer_id", "in", "orders", None, True),
        ("segment", "eq", "customers", None, True),
    }


def test_like_patterns_are_classified_by_anchoring():
    kinds = {p.column: (p.kind, p.usable) for p in _preds(
        "SELECT * FROM users WHERE email LIKE 'ab%' AND name ILIKE '%x' AND note LIKE $1"
    )}
    assert kinds == {
        "email": ("like_prefix", True),
        "name": ("like_unanchored", True),
        "note": ("like_unknown", False),
    }


def test_same_column_or_is_an_in_but_mixed_or_is_not_indexable():
    assert _summary("SELECT * FROM orders WHERE (status = $1 OR status = $2) AND total > $3") == {
        ("status", "in", "orders", None, True),
        ("total", "range", "orders", None, True),
    }
    mixed = _preds("SELECT * FROM orders WHERE status = $1 OR total > $2")
    assert all(not p.usable and "OR" in p.unusable_reason for p in mixed)


def test_between_is_null_order_group_and_not_equal():
    kinds = {(p.column, p.kind, p.usable) for p in _preds(
        "SELECT status, count(*) FROM orders WHERE total BETWEEN $1 AND $2 AND note IS NULL "
        "AND region <> $3 GROUP BY status ORDER BY status"
    )}
    assert kinds == {
        ("total", "range", True),
        ("note", "is_null", True),
        ("region", "other", False),
        ("status", "group", True),
        ("status", "sort", True),
    }


def test_derived_cte_column_explains_why_it_is_not_indexable():
    pred = _preds("WITH x AS (SELECT lower(email) AS le FROM users) SELECT * FROM x WHERE le = $1")[0]
    assert not pred.usable
    assert "İFADEDEN türetiliyor" in pred.unusable_reason


def test_unparsable_sql_reports_an_error_instead_of_predicates():
    result = extract_predicates("SELECT FROM WHERE (")
    assert result.parse_error
    assert result.predicates == []
