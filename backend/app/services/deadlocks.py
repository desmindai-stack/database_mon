"""Deadlock tespiti (Faz 26 İŞ 3).

Deadlock, bloklamadan FARKLI bir olaydır ve bu fark ürün açısından belirleyici: veritabanı
döngüyü kendisi kırar ve taraflardan birini (KURBAN) iptal eder. Yani sürüp giden bir durum
değil, ANLIK bir olaydır — canlı ekranda hiçbir izi kalmaz. Bir dakika sonra bakan biri
hiçbir şey göremez.

Bu yüzden deadlock yalnızca GERİYE DÖNÜK kaynaklardan görülebilir:

- **PostgreSQL**: sunucu log'una `ERROR: deadlock detected` satırı yazılır, ardından
  `DETAIL:` içinde hangi sürecin hangi kilidi beklediği ve `Process N: <sorgu>` satırları.
- **SQL Server**: `system_health` genişletilmiş olay (XE) oturumu deadlock XML'ini halka
  tamponda tutar.

KURBAN VE KAZANAN AYRIMI: kurban, veritabanının iptal ettiği taraftır ve uygulama tarafında
hata alan da odur. Kazanan işine devam eder. Yalnızca kurbanı göstermek yarım teşhistir —
kurbanın "suçu" genelde yoktur, döngüyü oluşturan KAZANANIN kilit sırasıdır.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

#: PostgreSQL log'unda deadlock'un başladığı satır.
_DEADLOCK_LINE = re.compile(r"deadlock detected", re.IGNORECASE)

#: `Process 12345: UPDATE ...` — DETAIL bloğundaki süreç/sorgu satırları.
_PROCESS_LINE = re.compile(r"Process\s+(\d+):\s*(.*)", re.IGNORECASE)

#: `Process 12345 waits for ShareLock on transaction 987; blocked by process 54321.`
_WAITS_LINE = re.compile(
    r"Process\s+(\d+)\s+waits for\s+(.+?)\s+on\s+(.+?);\s*blocked by process\s+(\d+)",
    re.IGNORECASE,
)

_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)")

#: Deadlock bloğu bu kadar satırdan uzun olamaz — bozuk bir log'da sonsuza kadar okumamak için.
MAX_BLOCK_LINES = 60


@dataclass
class DeadlockRecord:
    detected_at: datetime
    source: str
    victim_pid: int | None = None
    victim_query: str = ""
    winner_pid: int | None = None
    winner_query: str = ""
    participants: int = 2
    raw_detail: str = ""
    fingerprint: str = ""
    #: Kilit döngüsünün kenarları: [(bekleyen_pid, bekleyen_kilit, nesne, bloklayan_pid)]
    edges: list[tuple[int, str, str, int]] = field(default_factory=list)


def _parse_timestamp(line: str) -> datetime | None:
    match = _TIMESTAMP.match(line.strip())
    if not match:
        return None
    text = match.group(1).replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _fingerprint(record: DeadlockRecord) -> str:
    """Aynı olayın tekrar yazılmasını engelleyen anahtar.

    Log penceresi her çekimde örtüşüyor, yani aynı deadlock birden çok kez görülüyor. Anahtar
    SORGU METİNLERİNDEN türetiliyor, pid'lerden değil: pid'ler her deadlock'ta farklıdır ama
    aynı deadlock TEKRAR ETTİĞİNDE sorgular aynıdır — bu sayede "aynı deadlock 40 kez oldu"
    sorusu da cevaplanabiliyor.
    """
    material = "|".join([record.victim_query.strip(), record.winner_query.strip()])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def parse_postgres_deadlocks(lines: list[str]) -> list[DeadlockRecord]:
    """PostgreSQL sunucu log'undan deadlock olaylarını çıkarır.

    Log biçimi `log_line_prefix`'e bağlı olduğu için ön eke GÜVENİLMİYOR: bloklar
    "deadlock detected" satırından başlayıp bir sonraki deadlock'a ya da blok sınırına kadar
    okunuyor, içindeki `Process N:` ve `waits for` satırları desenle ayıklanıyor.
    """
    records: list[DeadlockRecord] = []
    index = 0
    total = len(lines)

    while index < total:
        if not _DEADLOCK_LINE.search(lines[index]):
            index += 1
            continue

        start = index
        detected_at = _parse_timestamp(lines[index]) or datetime.now(UTC)
        block: list[str] = [lines[index]]
        index += 1
        while index < total and index - start < MAX_BLOCK_LINES:
            if _DEADLOCK_LINE.search(lines[index]):
                break
            block.append(lines[index])
            index += 1

        record = _record_from_block(block, detected_at)
        if record is not None:
            records.append(record)

    return records


def _record_from_block(block: list[str], detected_at: datetime) -> DeadlockRecord | None:
    text = "\n".join(block)
    queries: dict[int, str] = {}
    edges: list[tuple[int, str, str, int]] = []

    for line in block:
        waits = _WAITS_LINE.search(line)
        if waits:
            edges.append(
                (int(waits.group(1)), waits.group(2).strip(), waits.group(3).strip(), int(waits.group(4)))
            )
            continue
        process = _PROCESS_LINE.search(line)
        if process:
            pid = int(process.group(1))
            query = process.group(2).strip()
            # "Process 123 waits for ..." satırı da bu desene benziyor; sorgu gibi
            # kaydetmemek için ayıklanıyor.
            if query and not query.lower().startswith("waits for"):
                queries[pid] = query

    if not queries and not edges:
        return None

    participants = len(set(queries) | {e[0] for e in edges} | {e[3] for e in edges})

    # KURBAN: PostgreSQL log'unda kurbanın sorgusu deadlock hatasının hemen ardından
    # `STATEMENT:` olarak yazılır. Bulunamazsa, döngüde başkası tarafından beklenmeyen
    # (yani iptal edilmiş olan) sürece düşülüyor.
    victim_pid, victim_query = _victim_from_block(block, queries, edges)
    winner_pid, winner_query = _winner(queries, edges, victim_pid)

    record = DeadlockRecord(
        detected_at=detected_at,
        source="postgresql_log",
        victim_pid=victim_pid,
        victim_query=victim_query,
        winner_pid=winner_pid,
        winner_query=winner_query,
        participants=max(participants, 2),
        raw_detail=text[:8000],
        edges=edges,
    )
    record.fingerprint = _fingerprint(record)
    return record


def _victim_from_block(
    block: list[str], queries: dict[int, str], edges: list[tuple[int, str, str, int]]
) -> tuple[int | None, str]:
    for line in block:
        marker = re.search(r"STATEMENT:\s*(.+)", line, re.IGNORECASE)
        if marker:
            statement = marker.group(1).strip()
            for pid, query in queries.items():
                if query and statement.startswith(query[:40]):
                    return pid, query
            return None, statement
    if edges:
        # Döngüde ilk bekleyen; log sırası kurbanı önce yazar.
        pid = edges[0][0]
        return pid, queries.get(pid, "")
    pid = next(iter(queries), None)
    return pid, queries.get(pid, "") if pid is not None else ""


def _winner(
    queries: dict[int, str], edges: list[tuple[int, str, str, int]], victim_pid: int | None
) -> tuple[int | None, str]:
    """Kazanan = kurbanı bekleten taraf. Kurbanın 'suçu' genelde yoktur; döngüyü oluşturan
    kilit sırası kazananındır ve düzeltme orada yapılır."""
    for waiter, _lock, _obj, blocker in edges:
        if victim_pid is None or waiter == victim_pid:
            return blocker, queries.get(blocker, "")
    for pid, query in queries.items():
        if pid != victim_pid:
            return pid, query
    return None, ""


# --- SQL Server ---------------------------------------------------------------------------

#: system_health halka tamponundan deadlock raporlarını çeken sorgu. XE oturumu SQL Server
#: 2012+ ile VARSAYILAN OLARAK açıktır; ayrıca yapılandırma gerektirmiyor.
SQLSERVER_DEADLOCK_SQL = """
SELECT TOP ({limit})
    CONVERT(VARCHAR(33), DATEADD(
        ms,
        DATEDIFF(ms, GETUTCDATE(), GETDATE()),
        x.value('(@timestamp)[1]', 'datetime2')), 126) AS detected_at,
    x.query('.') AS deadlock_xml
FROM (
    SELECT CAST(target_data AS XML) AS td
    FROM sys.dm_xe_session_targets st
    JOIN sys.dm_xe_sessions s ON s.address = st.event_session_address
    WHERE s.name = 'system_health' AND st.target_name = 'ring_buffer'
) AS data
CROSS APPLY td.nodes('RingBufferTarget/event[@name="xml_deadlock_report"]') AS q(x)
ORDER BY detected_at DESC
"""


def parse_sqlserver_deadlock_xml(xml_text: str, detected_at: datetime) -> DeadlockRecord | None:
    """SQL Server deadlock XML'inden kurban ve kazananı çıkarır.

    XML'de kurban `<victim-list><victimProcess id="..."/></victim-list>` ile AÇIKÇA
    işaretlenir — PostgreSQL'in aksine tahmin gerekmiyor.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return None

    victim_ids = {
        el.get("id") for el in root.iter("victimProcess") if el.get("id")
    }
    processes: list[tuple[str, int | None, str]] = []
    for process in root.iter("process"):
        pid_text = process.get("spid")
        try:
            spid = int(pid_text) if pid_text is not None else None
        except ValueError:
            spid = None
        buffer_el = process.find("inputbuf")
        query = (buffer_el.text or "").strip() if buffer_el is not None else ""
        processes.append((process.get("id") or "", spid, query))

    if not processes:
        return None

    victim = next((p for p in processes if p[0] in victim_ids), processes[0])
    winner = next((p for p in processes if p is not victim), (None, None, ""))

    record = DeadlockRecord(
        detected_at=detected_at,
        source="sqlserver_system_health",
        victim_pid=victim[1],
        victim_query=victim[2],
        winner_pid=winner[1],
        winner_query=winner[2],
        participants=max(len(processes), 2),
        raw_detail=xml_text[:8000],
    )
    record.fingerprint = _fingerprint(record)
    return record
