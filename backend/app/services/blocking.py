"""Blocking hiyerarşisi (Faz 26 İŞ 3).

"Kaç oturum bloklandı" bir sayıdır; **"kim kimi blokluyor"** bir cevaptır. Bankada en sık
sorulan soru ikincisi ve bugüne kadar dbace onu cevaplayamıyordu.

Bloklanma bir ZİNCİRDİR: A, B'yi bekler; B, C'yi bekler; C hiçbir şeyi beklemez ama bir kilidi
tutar. Ekranda "3 oturum bloklandı" yazması hiçbir işe yaramaz — müdahale edilecek tek oturum
C'dir, yani ZİNCİRİN BAŞINDAKİ. Bu modül kenar listesini ağaca çevirip kök engelleyiciyi
işaretliyor.

SESSİZ BLOKLAMA: `idle in transaction` durumundaki bir oturum hiçbir sorgu ÇALIŞTIRMIYOR ama
açık transaction'ıyla kilit TUTUYOR. Aktif sorgu listelerinde görünmez, CPU harcamaz, yavaş
sorgu raporlarına düşmez — ama onlarca oturumu durdurabilir. Bu yüzden ayrıca tespit ediliyor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Bu süreyi aşan açık transaction "uzun" sayılıyor. 60 saniye, bir OLTP sisteminde
#: transaction'ın açık kalması için hiçbir meşru sebebin kalmadığı sınır — daha uzun tutmak,
#: sorunu görünmez kılan sessizliğin ta kendisi.
LONG_TRANSACTION_SECONDS = 60.0

#: `idle in transaction` bu süreyi aştıysa uygulamada bir hata var demektir (transaction
#: açılmış, iş yapılmamış, kapatılmamış).
IDLE_IN_TRANSACTION_SECONDS = 30.0

#: Zincir bu derinliği aşarsa döngü (cycle) var demektir. PostgreSQL deadlock detektörü
#: gerçek döngüleri zaten kırar, ama anlık bir fotoğraf tutarsız olabilir; sonsuz döngüye
#: girmemek için sert bir sınır.
MAX_CHAIN_DEPTH = 50


@dataclass
class BlockingNode:
    """Ağaçtaki tek bir oturum."""

    pid: int
    username: str | None = None
    application: str | None = None
    state: str | None = None
    query: str = ""
    #: Sorgunun ne kadar süredir çalıştığı.
    query_seconds: float | None = None
    #: Transaction'ın ne kadar süredir AÇIK olduğu — sessiz blokların ölçüsü budur.
    transaction_seconds: float | None = None
    #: Bu oturum ne kadar süredir BEKLİYOR (bloklananlar için).
    wait_seconds: float | None = None
    lock_type: str | None = None
    lock_mode: str | None = None
    lock_object: str | None = None
    #: Bu oturumun tuttuğu (verilmiş) kilit sayısı — kök engelleyicinin etkisinin ölçüsü.
    held_locks: int = 0
    #: Zincirin başındaki oturum mu — müdahale edilecek tek yer.
    is_root_blocker: bool = False
    #: Hiçbir sorgu çalıştırmadan kilit tutuyor mu (sessiz blok).
    is_idle_in_transaction: bool = False
    #: Bu düğümün altındaki TOPLAM bloklanan oturum sayısı (dolaylı olanlar dahil).
    blocked_total: int = 0
    depth: int = 0
    children: list["BlockingNode"] = field(default_factory=list)


@dataclass
class BlockingTree:
    roots: list[BlockingNode] = field(default_factory=list)
    #: Bloklanan (doğrudan ya da dolaylı) toplam oturum sayısı.
    blocked_sessions: int = 0
    #: Zincirin başındaki oturum sayısı.
    root_blockers: int = 0
    #: En uzun zincirin derinliği — 1 = tek kademeli blok, 4 = dört kademeli zincir.
    max_depth: int = 0
    #: Sorgu çalıştırmadan kilit tutan oturumlar (bloklama yapmasalar da risk).
    idle_in_transaction: list[BlockingNode] = field(default_factory=list)
    #: Uzun süredir açık transaction'lar.
    long_transactions: list[BlockingNode] = field(default_factory=list)
    unavailable_reason: str | None = None


def _to_node(row: dict[str, Any]) -> BlockingNode:
    state = (row.get("state") or "").strip() or None
    transaction_seconds = row.get("transaction_seconds")
    return BlockingNode(
        pid=int(row["pid"]),
        username=row.get("username"),
        application=row.get("application"),
        state=state,
        query=str(row.get("query") or "").strip(),
        query_seconds=row.get("query_seconds"),
        transaction_seconds=transaction_seconds,
        wait_seconds=row.get("wait_seconds"),
        lock_type=row.get("lock_type"),
        lock_mode=row.get("lock_mode"),
        lock_object=row.get("lock_object"),
        held_locks=int(row.get("held_locks") or 0),
        is_idle_in_transaction=(state == "idle in transaction"),
    )


def build_blocking_tree(rows: list[dict[str, Any]]) -> BlockingTree:
    """Oturum listesini (her biri `blocking_pids` taşıyan) bloklama ağacına çevirir.

    Girdi bir KENAR listesi, çıktı bir ORMAN: kök engelleyiciler tepede, bekleyenler altlarında.
    Aynı oturumu birden çok blokçu bekletiyorsa (çoklu kilit) oturum İLK blokçunun altına
    yerleştiriliyor ve bu bilinçli bir sadeleştirme: kullanıcıya aynı pid'i ağacın üç ayrı
    yerinde göstermek, zinciri okunamaz hâle getirirdi.
    """
    tree = BlockingTree()
    if not rows:
        return tree

    nodes: dict[int, BlockingNode] = {}
    blockers_of: dict[int, list[int]] = {}
    for row in rows:
        try:
            pid = int(row["pid"])
        except (KeyError, TypeError, ValueError):
            continue
        nodes[pid] = _to_node(row)
        blockers_of[pid] = [int(b) for b in (row.get("blocking_pids") or []) if b]

    blocked_pids = {pid for pid, blockers in blockers_of.items() if blockers}
    tree.blocked_sessions = len(blocked_pids)

    # Bloklamaya KARIŞAN pid'ler: bekleyenler + onları bekleten herkes. Diğer oturumlar
    # ağaca girmiyor — "kim kimi blokluyor" sorusunun cevabında boşta oturumların yeri yok.
    involved: set[int] = set(blocked_pids)
    for blockers in blockers_of.values():
        involved.update(blockers)

    # Blokçusu olmayan (ya da blokçusu görünmeyen) her katılımcı bir köktür.
    for pid in involved:
        if pid not in nodes:
            # Blokçu, örneklenen listede yok (başka bir veritabanına bağlı olabilir ya da
            # fotoğraf ile arasında kapanmış olabilir). Yok saymak zinciri kesip kullanıcıyı
            # yanlış oturuma yönlendirirdi; yer tutucu bir düğüm konuyor.
            nodes[pid] = BlockingNode(
                pid=pid,
                query="(bu oturum anlık görüntüde yok — başka bir veritabanında olabilir)",
            )
            blockers_of.setdefault(pid, [])

    attached: set[int] = set()
    for pid in sorted(involved):
        blockers = [b for b in blockers_of.get(pid, []) if b != pid]
        if not blockers:
            continue
        parent = nodes.get(blockers[0])
        if parent is None or pid in attached:
            continue
        parent.children.append(nodes[pid])
        attached.add(pid)

    roots = [
        nodes[pid]
        for pid in sorted(involved)
        if pid not in attached and (nodes[pid].children or blockers_of.get(pid))
    ]
    for root in roots:
        root.is_root_blocker = True
        _assign_depth(root, 0)
        root.blocked_total = _count_blocked(root)
    tree.roots = sorted(roots, key=lambda n: -n.blocked_total)
    tree.root_blockers = len(roots)
    tree.max_depth = max((_max_depth(r) for r in roots), default=0)

    # Sessiz blok adayları: bloklama yapmasalar bile risk taşıyorlar.
    for node in nodes.values():
        if node.is_idle_in_transaction:
            tree.idle_in_transaction.append(node)
        if (node.transaction_seconds or 0) >= LONG_TRANSACTION_SECONDS:
            tree.long_transactions.append(node)
    tree.idle_in_transaction.sort(key=lambda n: -(n.transaction_seconds or 0))
    tree.long_transactions.sort(key=lambda n: -(n.transaction_seconds or 0))
    return tree


def _assign_depth(node: BlockingNode, depth: int) -> None:
    if depth > MAX_CHAIN_DEPTH:
        node.children = []
        return
    node.depth = depth
    for child in node.children:
        _assign_depth(child, depth + 1)


def _count_blocked(node: BlockingNode) -> int:
    return sum(1 + _count_blocked(child) for child in node.children)


def _max_depth(node: BlockingNode) -> int:
    if not node.children:
        return node.depth
    return max(_max_depth(child) for child in node.children)


def find_idle_blockers(tree: BlockingTree) -> list[BlockingNode]:
    """Sorgu çalıştırmadan başkalarını bekleten kök engelleyiciler.

    En sinsi durum bu: oturum hiçbir iş yapmıyor, CPU harcamıyor, yavaş sorgu listesinde
    görünmüyor — ama açık transaction'ıyla onlarca oturumu durduruyor. Sebebi neredeyse her
    zaman uygulamadadır (transaction açılmış, iş yapılmamış, kapatılmamış).
    """
    return [node for node in tree.roots if node.is_idle_in_transaction and node.children]


def tree_to_dict(tree: BlockingTree) -> dict[str, Any]:
    return {
        "roots": [_node_to_dict(n) for n in tree.roots],
        "blocked_sessions": tree.blocked_sessions,
        "root_blockers": tree.root_blockers,
        "max_depth": tree.max_depth,
        "idle_in_transaction": [_node_to_dict(n, with_children=False) for n in tree.idle_in_transaction],
        "long_transactions": [_node_to_dict(n, with_children=False) for n in tree.long_transactions],
        "unavailable_reason": tree.unavailable_reason,
    }


def _node_to_dict(node: BlockingNode, *, with_children: bool = True) -> dict[str, Any]:
    payload = {
        "pid": node.pid,
        "username": node.username,
        "application": node.application,
        "state": node.state,
        "query": node.query,
        "query_seconds": node.query_seconds,
        "transaction_seconds": node.transaction_seconds,
        "wait_seconds": node.wait_seconds,
        "lock_type": node.lock_type,
        "lock_mode": node.lock_mode,
        "lock_object": node.lock_object,
        "held_locks": node.held_locks,
        "is_root_blocker": node.is_root_blocker,
        "is_idle_in_transaction": node.is_idle_in_transaction,
        "blocked_total": node.blocked_total,
        "depth": node.depth,
        "children": [],
    }
    if with_children:
        payload["children"] = [_node_to_dict(c) for c in node.children]
    return payload
