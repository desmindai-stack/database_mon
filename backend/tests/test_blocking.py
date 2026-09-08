"""Blocking hiyerarşisi (Faz 26 İŞ 3).

"Kaç oturum bloklandı" bir sayıdır; **"kim kimi blokluyor"** bir cevaptır. Bankada en sık
sorulan soru ikincisi.

Bu dosyanın koruduğu üç fikir:

1. **Bloklanma bir ZİNCİRDİR.** A→B→C zincirinde müdahale edilecek tek oturum C'dir. Zincirin
   ortasındaki B'yi sonlandırmak sorunu çözmez, yalnızca bekleyeni değiştirir. Ağaç kök
   engelleyiciyi işaretlemek zorunda.
2. **Sessiz blok en tehlikelisidir.** `idle in transaction` bir oturum hiçbir sorgu
   çalıştırmaz, CPU harcamaz, yavaş sorgu listesinde görünmez — ama açık transaction'ıyla
   onlarca oturumu durdurabilir. Öneri bu durumda "sorun veritabanında değil, uygulamada"
   demek zorunda.
3. **Boş ağaç ile ölçülemeyen ağaç farklıdır.** Sorgu çalışmadıysa "bloklama yok" demek,
   ölçüm yapılmadığı hâlde her şeyin yolunda olduğunu iddia etmektir.
"""

from __future__ import annotations

import pytest

from app.services.blocking import (
    IDLE_IN_TRANSACTION_SECONDS,
    LONG_TRANSACTION_SECONDS,
    build_blocking_tree,
    find_idle_blockers,
    tree_to_dict,
)
from app.services.blocking_advice import advice_for_blocking


def session(
    pid: int,
    *,
    blocking: list[int] | None = None,
    state: str = "active",
    query: str = "SELECT 1",
    transaction_seconds: float | None = 5.0,
    query_seconds: float | None = 5.0,
    held_locks: int = 1,
    **extra,
) -> dict:
    row = {
        "pid": pid,
        "username": "app",
        "application": "api",
        "state": state,
        "query": query,
        "query_seconds": query_seconds,
        "transaction_seconds": transaction_seconds,
        "wait_seconds": 3.0,
        "blocking_pids": blocking or [],
        "lock_type": "transactionid",
        "lock_mode": "ShareLock",
        "lock_object": "orders",
        "held_locks": held_locks,
    }
    row.update(extra)
    return row


# --- Zincir kurma -------------------------------------------------------------------------


def test_a_simple_block_has_one_root_and_one_child():
    tree = build_blocking_tree([session(100), session(200, blocking=[100])])
    assert tree.root_blockers == 1
    assert tree.blocked_sessions == 1
    root = tree.roots[0]
    assert root.pid == 100
    assert root.is_root_blocker is True
    assert [c.pid for c in root.children] == [200]


def test_a_three_level_chain_marks_only_the_head_as_root():
    """A→B→C: müdahale edilecek tek oturum C'dir (zincirin başı). B'yi sonlandırmak sorunu
    çözmez, yalnızca bekleyeni değiştirir."""
    rows = [
        session(300),                      # kök engelleyici
        session(200, blocking=[300]),      # hem bekliyor hem bekletiyor
        session(100, blocking=[200]),      # yalnızca bekliyor
    ]
    tree = build_blocking_tree(rows)
    assert tree.root_blockers == 1
    assert tree.roots[0].pid == 300
    assert tree.max_depth == 2
    middle = tree.roots[0].children[0]
    assert middle.pid == 200
    assert middle.is_root_blocker is False
    assert [c.pid for c in middle.children] == [100]


def test_blocked_total_counts_indirect_victims():
    """Kök engelleyicinin etkisi doğrudan beklettikleriyle sınırlı değil: zincirin tamamı
    onun yüzünden duruyor. Yalnızca doğrudan çocukları saymak etkiyi küçük gösterirdi."""
    rows = [
        session(300),
        session(200, blocking=[300]),
        session(100, blocking=[200]),
        session(101, blocking=[200]),
    ]
    tree = build_blocking_tree(rows)
    assert tree.roots[0].blocked_total == 3


def test_two_independent_chains_produce_two_roots():
    rows = [
        session(10), session(11, blocking=[10]),
        session(20), session(21, blocking=[20]), session(22, blocking=[21]),
    ]
    tree = build_blocking_tree(rows)
    assert tree.root_blockers == 2
    # En çok oturumu bekleten kök başta: müdahale önceliği bu.
    assert tree.roots[0].pid == 20
    assert tree.roots[0].blocked_total == 2


def test_sessions_not_involved_in_blocking_are_left_out():
    """Ağaç "kim kimi blokluyor" sorusuna cevap; boşta oturumların orada yeri yok."""
    rows = [session(1), session(2), session(3, blocking=[1])]
    tree = build_blocking_tree(rows)
    pids = {n.pid for n in tree.roots} | {c.pid for r in tree.roots for c in r.children}
    assert pids == {1, 3}


def test_a_blocker_missing_from_the_snapshot_becomes_a_placeholder():
    """Blokçu listede yoksa (başka bir veritabanına bağlı olabilir) zinciri kesmek,
    kullanıcıyı yanlış oturuma yönlendirirdi."""
    tree = build_blocking_tree([session(200, blocking=[999])])
    assert tree.root_blockers == 1
    root = tree.roots[0]
    assert root.pid == 999
    assert "anlık görüntüde yok" in root.query
    assert [c.pid for c in root.children] == [200]


def test_a_session_blocked_by_two_holders_is_attached_once():
    """Aynı pid'i ağacın iki ayrı yerinde göstermek zinciri okunamaz kılardı."""
    rows = [session(10), session(20), session(30, blocking=[10, 20])]
    tree = build_blocking_tree(rows)
    appearances = sum(
        1 for r in tree.roots for c in r.children if c.pid == 30
    )
    assert appearances == 1
    assert tree.blocked_sessions == 1


def test_self_blocking_is_ignored():
    """Bir oturum kendini bekleyemez; anlık görüntü tutarsızlığında sonsuz döngü olurdu."""
    tree = build_blocking_tree([session(10, blocking=[10])])
    assert tree.max_depth == 0


def test_no_blocking_produces_an_empty_tree_not_an_error():
    tree = build_blocking_tree([session(1), session(2)])
    assert tree.roots == []
    assert tree.blocked_sessions == 0
    assert tree.unavailable_reason is None


def test_empty_input_is_handled():
    tree = build_blocking_tree([])
    assert tree.roots == []
    assert tree.blocked_sessions == 0


# --- Sessiz blok --------------------------------------------------------------------------


def test_idle_in_transaction_sessions_are_listed_even_without_blocking():
    """Henüz kimseyi bekletmiyor olabilir ama açık transaction'ıyla kilit tutuyor — bu bir
    zaman bombası ve görünmesi gerekiyor."""
    tree = build_blocking_tree(
        [session(50, state="idle in transaction", query="BEGIN", transaction_seconds=300.0)]
    )
    assert tree.roots == []
    assert [n.pid for n in tree.idle_in_transaction] == [50]


def test_long_open_transactions_are_listed_separately():
    tree = build_blocking_tree(
        [
            session(60, transaction_seconds=LONG_TRANSACTION_SECONDS + 10),
            session(61, transaction_seconds=5.0),
        ]
    )
    assert [n.pid for n in tree.long_transactions] == [60]


def test_an_idle_root_blocker_is_identified_as_such():
    """En sinsi durum: hiçbir iş yapmayan bir oturum onlarca oturumu durduruyor."""
    rows = [
        session(70, state="idle in transaction", query="BEGIN", transaction_seconds=600.0),
        session(71, blocking=[70]),
        session(72, blocking=[70]),
    ]
    tree = build_blocking_tree(rows)
    idle_roots = find_idle_blockers(tree)
    assert [n.pid for n in idle_roots] == [70]
    assert tree.roots[0].is_idle_in_transaction is True
    assert tree.roots[0].blocked_total == 2


def test_an_active_root_blocker_is_not_flagged_as_idle():
    rows = [session(80, state="active", query="UPDATE orders SET x=1"), session(81, blocking=[80])]
    tree = build_blocking_tree(rows)
    assert find_idle_blockers(tree) == []


# --- Öneri --------------------------------------------------------------------------------


def test_no_blocking_means_no_advice():
    """Bloklanma olmayan bir sistem için "kilitlerinizi kontrol edin" demek, olmayan bir
    sorunu varmış gibi göstermek olurdu."""
    tree = build_blocking_tree([session(1), session(2)])
    assert advice_for_blocking(tree, engine="postgresql") is None


def test_idle_blocker_advice_says_the_problem_is_in_the_application():
    """ÖNEMLİ AYRIM: kök engelleyici sorgu çalıştırmıyorsa sorgu optimizasyonu HİÇBİR ŞEYİ
    değiştirmez. Bunu söylememek, DBA'yı olmayan bir sorunu aramaya yollar."""
    rows = [
        session(90, state="idle in transaction", query="BEGIN", transaction_seconds=400.0),
        session(91, blocking=[90]),
    ]
    advice = advice_for_blocking(build_blocking_tree(rows), engine="postgresql")
    assert "uygulama" in advice.title.lower()
    assert "HİÇBİR SORGU ÇALIŞTIRMIYOR" in advice.why
    assert "Sorguyu optimize etmek bu durumu DEĞİŞTİRMEZ" in advice.why
    commands = " ".join(s.command or "" for s in advice.steps)
    # Sorgu çalışmadığı için CANCEL anlamsız — doğrudan terminate öneriliyor.
    assert "pg_terminate_backend(90)" in commands
    assert "pg_cancel_backend" not in commands
    assert "idle_in_transaction_session_timeout" in commands


def test_active_blocker_advice_tries_cancel_before_terminate():
    """İptal oturumu yaşatır, sonlandırma bağlantıyı koparır. Sırayı ters vermek gereksiz
    yere sert bir müdahale önermek olurdu."""
    rows = [session(95, state="active", query="UPDATE t SET x=1", query_seconds=300.0), session(96, blocking=[95])]
    advice = advice_for_blocking(build_blocking_tree(rows), engine="postgresql")
    commands = " ".join(s.command or "" for s in advice.steps)
    assert commands.index("pg_cancel_backend(95)") < commands.index("pg_terminate_backend(95)")


def test_advice_warns_that_killing_the_middle_of_the_chain_does_not_help():
    rows = [session(1), session(2, blocking=[1]), session(3, blocking=[2])]
    advice = advice_for_blocking(build_blocking_tree(rows), engine="postgresql")
    assert any("ortasındaki" in c for c in advice.cautions)


def test_advice_follows_the_five_part_standard():
    rows = [session(1), session(2, blocking=[1])]
    for engine in ("postgresql", "sqlserver"):
        advice = advice_for_blocking(build_blocking_tree(rows), engine=engine)
        assert advice.title and advice.why
        assert advice.steps and any(s.command for s in advice.steps)
        assert advice.cautions
        assert advice.verification


def test_sqlserver_advice_uses_sqlserver_commands():
    rows = [session(1), session(2, blocking=[1])]
    advice = advice_for_blocking(build_blocking_tree(rows), engine="sqlserver")
    blob = " ".join([advice.verification or "", *(s.command or "" for s in advice.steps)])
    assert "KILL 1;" in blob
    assert "pg_terminate_backend" not in blob


def test_sqlserver_advice_warns_that_rollback_can_take_long():
    """KILL anında dönmez: büyük bir transaction'ın geri alınması dakikalar sürebilir ve
    bekleme bu süre boyunca devam eder. Bunu bilmeden KILL veren DBA "işe yaramadı" der."""
    rows = [session(1), session(2, blocking=[1])]
    advice = advice_for_blocking(build_blocking_tree(rows), engine="sqlserver")
    assert any("rollback" in c.lower() and "uzun" in c.lower() for c in advice.cautions)


def test_unsupported_engine_gets_no_advice():
    rows = [session(1), session(2, blocking=[1])]
    assert advice_for_blocking(build_blocking_tree(rows), engine="mongodb") is None


# --- Serileştirme -------------------------------------------------------------------------


def test_tree_to_dict_keeps_the_hierarchy():
    rows = [session(1), session(2, blocking=[1]), session(3, blocking=[2])]
    payload = tree_to_dict(build_blocking_tree(rows))
    assert payload["root_blockers"] == 1
    assert payload["max_depth"] == 2
    root = payload["roots"][0]
    assert root["pid"] == 1
    assert root["children"][0]["children"][0]["pid"] == 3


def test_side_lists_do_not_repeat_the_whole_subtree():
    """`idle_in_transaction` bir UYARI listesi, ikinci bir ağaç değil. Alt ağacı tekrar
    taşımak yanıtı gereksiz yere büyütür ve arayüzde aynı zinciri iki kez çizdirirdi."""
    rows = [
        session(1, state="idle in transaction", transaction_seconds=500.0),
        session(2, blocking=[1]),
    ]
    payload = tree_to_dict(build_blocking_tree(rows))
    assert payload["idle_in_transaction"][0]["children"] == []
    assert payload["roots"][0]["children"] != []


def test_thresholds_are_documented_constants():
    assert IDLE_IN_TRANSACTION_SECONDS > 0
    assert LONG_TRANSACTION_SECONDS >= IDLE_IN_TRANSACTION_SECONDS
