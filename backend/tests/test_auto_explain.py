"""auto_explain entegrasyonu (Faz 26 İŞ 1).

Bu dosyanın koruduğu asıl fikir: **sonradan alınan plan bir tahmindir.**

pg_stat_statements sorguyu normalleştirir (`WHERE id = $1`); sonradan EXPLAIN alırken
parametre bilinmediği için `NULL` konur ve planlayıcı bambaşka bir plan seçebilir — üstelik
asıl sorun genelde tam da budur, *bazı* parametre değerlerinde plan çöker. auto_explain ise
sorgunun yavaş çalıştığı andaki gerçek planı yazar.

Testler üç şeyi kilitliyor:

1. **Log ayrıştırma sağlam.** Plan birden çok satıra yayılıyor ve `log_line_prefix` her
   kurulumda farklı; ayrıştırma satır sayısına veya girintiye değil süslü parantez dengesine
   dayanmalı.
2. **Yarım plan kaydedilmez.** Log penceresi planın ortasında bitmişse o blok atılır —
   tamamlanmamış bir planı "yakalandı" diye kaydetmek yanıltıcı olurdu.
3. **Kaynak ayrımı kaybolmaz.** Yakalanan plan ile sonradan alınan plan farklı etiket ve
   farklı uyarı taşır.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import CapturedPlan, Instance, SlowQuerySample
from app.services.auto_explain import (
    MAX_PLANS_PER_FETCH,
    normalize_query_key,
    parse_auto_explain_log,
    plan_source_caveat,
    plan_source_label,
)
from app.services.credentials import encrypt_secret
from app.services.plan_capture import fingerprint, store_captured_plans


@pytest.fixture(autouse=True)
async def _schema():
    await init_db()


def _plan_payload(query: str = "SELECT * FROM orders WHERE id = 5", *, analyze: bool = True) -> dict:
    node: dict = {
        "Node Type": "Index Scan",
        "Relation Name": "orders",
        "Startup Cost": 0.29,
        "Total Cost": 8.31,
        "Plan Rows": 1,
        "Plan Width": 120,
    }
    if analyze:
        node["Actual Rows"] = 1
        node["Actual Total Time"] = 0.045
    return {"Query Text": query, "Plan": node}


def _log_lines(*plans: tuple[float, dict], prefix: str = "2026-09-11 10:15:03.123 UTC [4211] LOG:  ") -> list[str]:
    """auto_explain'in gerçek çıktısına benzer log satırları üretir."""
    lines: list[str] = ["2026-09-11 10:15:00.000 UTC [4210] LOG:  checkpoint starting: time"]
    for duration, payload in plans:
        lines.append(f"{prefix}duration: {duration} ms  plan:")
        lines.extend(json.dumps(payload, indent=2).split("\n"))
    lines.append("2026-09-11 10:16:00.000 UTC [4210] LOG:  checkpoint complete")
    return lines


# --- Log ayrıştırma ----------------------------------------------------------------------


def test_a_plan_spanning_many_lines_is_parsed():
    result = parse_auto_explain_log(_log_lines((1234.567, _plan_payload())))
    assert len(result.plans) == 1
    plan = result.plans[0]
    assert plan.duration_ms == pytest.approx(1234.567)
    assert plan.query_text == "SELECT * FROM orders WHERE id = 5"
    assert plan.plan_json["Plan"]["Node Type"] == "Index Scan"


def test_several_plans_in_one_window_are_all_found():
    result = parse_auto_explain_log(
        _log_lines(
            (1000.0, _plan_payload("SELECT 1")),
            (2000.0, _plan_payload("SELECT 2")),
            (3000.0, _plan_payload("SELECT 3")),
        )
    )
    assert [p.duration_ms for p in result.plans] == [1000.0, 2000.0, 3000.0]


def test_parsing_survives_an_unknown_log_line_prefix():
    """`log_line_prefix` her kurulumda farklı. Ayrıştırma ön eke bağlı olamaz — süslü
    parantez dengesine bakıyor."""
    result = parse_auto_explain_log(
        _log_lines((500.0, _plan_payload()), prefix="[pid=99] user=app db=shop >>> ")
    )
    assert len(result.plans) == 1
    # Zaman damgası okunamadıysa plan yine kaydediliyor, zamanı yaklaşık olur.
    assert result.plans[0].captured_at is not None


def test_a_truncated_plan_is_discarded_not_half_saved():
    """Log penceresi planın ortasında bitmişse yarım JSON atılır.

    Tamamlanmamış bir planı "yakalandı" diye kaydetmek, kullanıcıya eksik bir plan ağacını
    gerçekmiş gibi göstermek olurdu.
    """
    lines = _log_lines((900.0, _plan_payload()))
    truncated = lines[:5]  # JSON'un ortasında kesiliyor
    result = parse_auto_explain_log(truncated)
    assert result.plans == []
    assert result.unparsed_blocks == 1
    assert result.truncated is True


def test_text_format_plans_are_counted_and_explained_not_silently_dropped():
    """`log_format = text` varsayılan ve ayrıştırılamıyor. Sessizce atmak, "auto_explain açık
    ama dbace'te plan yok" bilmecesini üretirdi."""
    lines = [
        "2026-09-11 10:15:03 UTC [1] LOG:  duration: 1500.000 ms  plan:",
        "  Index Scan using orders_pkey on orders  (cost=0.29..8.31 rows=1 width=120)",
        "    Index Cond: (id = 5)",
    ]
    result = parse_auto_explain_log(lines)
    assert result.plans == []
    assert result.unparsed_blocks == 1
    assert "json" in result.note.lower()


def test_lines_without_any_plan_produce_nothing_and_no_noise():
    result = parse_auto_explain_log(
        ["2026-09-11 10:00:00 UTC [1] LOG:  checkpoint starting", "random line"]
    )
    assert result.plans == []
    assert result.unparsed_blocks == 0
    assert result.note is None


def test_capture_stops_at_the_limit_and_says_so():
    """Sınırsız okumak, yoğun bir sunucuda tek turda binlerce satır yazmak demekti."""
    many = [(float(i), _plan_payload(f"SELECT {i}")) for i in range(MAX_PLANS_PER_FETCH + 10)]
    result = parse_auto_explain_log(_log_lines(*many))
    assert len(result.plans) == MAX_PLANS_PER_FETCH
    assert result.note and "sınır" in result.note.lower()


def test_actual_rows_presence_is_read_from_the_plan_not_from_a_setting():
    """`log_analyze` ayarını sormak yerine ÇIKTIYA bakmak doğru: ayar açık olsa bile plan
    ayar açılmadan önce yakalanmış olabilir."""
    with_actual = parse_auto_explain_log(_log_lines((10.0, _plan_payload(analyze=True))))
    without_actual = parse_auto_explain_log(_log_lines((10.0, _plan_payload(analyze=False))))
    assert with_actual.plans[0].has_actual_rows is True
    assert without_actual.plans[0].has_actual_rows is False


def test_timestamp_is_read_from_the_log_line_when_present():
    result = parse_auto_explain_log(_log_lines((10.0, _plan_payload())))
    captured = result.plans[0].captured_at
    assert captured.year == 2026 and captured.month == 9 and captured.day == 11
    assert captured.hour == 10 and captured.minute == 15


# --- Normalleştirme ve eşleştirme --------------------------------------------------------


def test_literals_are_normalized_so_the_same_query_matches():
    """auto_explain gerçek değerleri yazar (`id = 5`), pg_stat_statements yer tutucu (`id = $1`).
    Eşleştirme için ikisi de aynı anahtara indirgenmeli."""
    a = normalize_query_key("SELECT * FROM orders WHERE id = 5 AND name = 'ali'")
    b = normalize_query_key("select * from orders where id = $1 and name = $2")
    assert a == b


def test_comments_and_whitespace_do_not_break_matching():
    a = normalize_query_key("SELECT  a,\n  b\nFROM t  -- yorum\n")
    b = normalize_query_key("/* başka yorum */ select a, b from t;")
    assert a == b


# --- Kaynak ayrımı -----------------------------------------------------------------------


def test_captured_plans_carry_no_caveat_but_estimated_plans_do():
    """auto_explain planı ölçümdür; sonradan alınan plan tahmindir ve bu ekranda yazmalı."""
    assert plan_source_caveat("auto_explain") is None
    assert "auto_explain" in plan_source_label("auto_explain").lower()

    estimate = plan_source_caveat("manual_estimate")
    assert "ÇALIŞTIRILMADAN" in estimate
    assert "$1" in estimate  # parametre tuzağı açıkça anlatılıyor

    analyze = plan_source_caveat("manual_analyze")
    assert "yavaş çalıştığı andaki plan olmayabilir" in analyze


# --- Saklama -----------------------------------------------------------------------------


async def _instance(**over) -> Instance:
    async with SessionLocal() as session:
        row = Instance(
            name=f"plan-{uuid.uuid4().hex[:8]}", engine=over.pop("engine", "postgresql"),
            host="h", port=5432, database="d", username="u", password=encrypt_secret("x"),
            **over,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def test_the_same_plan_seen_twice_is_written_once():
    """Her çekim log'un SON N satırını okuyor, yani pencereler örtüşüyor ve aynı plan
    tekrar tekrar görülüyor. Tekrar yazmak tabloyu şişirir ve listeyi kullanılamaz kılar."""
    instance = await _instance()
    records = parse_auto_explain_log(_log_lines((1234.5, _plan_payload()))).plans

    async with SessionLocal() as session:
        first = await store_captured_plans(session, instance.id, records)
        await session.commit()
    async with SessionLocal() as session:
        second = await store_captured_plans(session, instance.id, records)
        await session.commit()

    assert first == 1
    assert second == 0, "aynı plan ikinci kez yazıldı"


async def test_a_captured_plan_is_linked_to_pg_stat_statements_when_possible():
    instance = await _instance()
    async with SessionLocal() as session:
        session.add(
            SlowQuerySample(
                instance_id=instance.id,
                collected_at=datetime.now(UTC) - timedelta(minutes=5),
                queryid="12345",
                query="SELECT * FROM orders WHERE id = $1",
                calls=10,
            )
        )
        await session.commit()

    records = parse_auto_explain_log(_log_lines((900.0, _plan_payload()))).plans
    async with SessionLocal() as session:
        await store_captured_plans(session, instance.id, records)
        await session.commit()
        row = (
            await session.execute(
                select(CapturedPlan).where(CapturedPlan.instance_id == instance.id)
            )
        ).scalar_one()
    assert row.queryid == "12345"


async def test_a_plan_with_no_match_is_still_stored():
    """Eşleştirme metin üzerinden ve KESİN DEĞİL. Eşleşme yoksa planı atmak, elimizdeki tek
    gerçek planı çöpe atmak olurdu — yanlış bir sorguya bağlamaktansa bağlamamak yeğdir."""
    instance = await _instance()
    records = parse_auto_explain_log(
        _log_lines((900.0, _plan_payload("SELECT * FROM hicbir_yerde_olmayan")))
    ).plans
    async with SessionLocal() as session:
        written = await store_captured_plans(session, instance.id, records)
        await session.commit()
        row = (
            await session.execute(
                select(CapturedPlan).where(CapturedPlan.instance_id == instance.id)
            )
        ).scalar_one()
    assert written == 1
    assert row.queryid is None
    assert row.plan_json is not None


def test_fingerprint_is_stable_and_short_enough_for_the_column():
    key = fingerprint("SELECT * FROM t WHERE a = 1")
    assert key == fingerprint("select * from t where a = $9")
    assert len(key) <= 64
