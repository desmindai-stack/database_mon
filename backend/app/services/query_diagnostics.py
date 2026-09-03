"""Kaynak bazlı sorgu tanısı (Faz 16 İŞ 3): her yavaş sorgu için darboğazın I/O, CPU, bellek ya
da kilit/bekleme olduğunu — ve bunu HANGİ metriğin söylediğini — belirler. `SlowQuerySample`'ın
zaten topladığı sütunlardan (shared/local/temp blk sayaçları, exec_user_time/exec_sys_time)
türetilir; ekstra bir canlı sorgu ÇALIŞTIRMAZ.

Dürüstlük kuralı (bkz. SORULAR.md): dbace hiçbir sorgu için per-query kilit bekleme SÜRESİ
toplamıyor (sadece anlık Activity görüntüsü var, geçmişe dönük değil) — bu yüzden "lock" sınıfı
her zaman `confidence="inferred"` ile işaretlenir ve CPU+I/O ile açıklanamayan bir süre farkı
olduğunda öne sürülür, asla kesin bir teşhis olarak sunulmaz.
"""

from __future__ import annotations

from dataclasses import dataclass

# performance_insights.py'nin "Disk okuma oranı yüksek" eşiğiyle aynı (%10) — iki modülün aynı
# sinyali farklı eşiklerle yorumlaması tutarsızlık yaratırdı.
IO_READ_RATIO_THRESHOLD = 0.10
CPU_TIME_SHARE_THRESHOLD = 0.70
UNEXPLAINED_TIME_SHARE_THRESHOLD = 0.30
MIN_UNEXPLAINED_MS = 5.0


@dataclass
class QueryDiagnosis:
    queryid: str | None
    query: str
    calls: int
    mean_time_ms: float
    total_time_ms: float
    resource: str  # io | cpu | memory | lock | unknown
    reason: str
    confidence: str  # observed | inferred


def _cpu_time_ms(row) -> float | None:
    user = row.exec_user_time
    sys = row.exec_sys_time
    if user is None and sys is None:
        return None
    return float(user or 0) + float(sys or 0)


def diagnose_query(row) -> QueryDiagnosis:
    """`row` is a SlowQuerySample (or any object with the same attribute names)."""
    mean_ms = float(row.mean_time_ms or 0)
    temp_blocks = (row.temp_blks_read or 0) + (row.temp_blks_written or 0)
    shared_read = row.shared_blks_read or 0
    shared_hit = row.shared_blks_hit or 0
    shared_total = shared_read + shared_hit
    io_ratio = (shared_read / shared_total) if shared_total > 0 else 0.0
    cpu_ms = _cpu_time_ms(row)

    if temp_blocks > 0:
        resource = "memory"
        reason = (
            f"{temp_blocks} blok geçici dosyaya yazıldı/okundu — work_mem sıralama/hash işlemi "
            f"için yetersiz kalmış, disk üzerinde geçici alan kullanılmış."
        )
        confidence = "observed"
    elif shared_total > 0 and io_ratio > IO_READ_RATIO_THRESHOLD:
        resource = "io"
        reason = (
            f"Okunan bloklerin %{io_ratio * 100:.0f}'i cache'te değildi, diskten okundu "
            f"({shared_read} disk / {shared_hit} cache)."
        )
        confidence = "observed"
    elif cpu_ms is not None and mean_ms > 0 and cpu_ms / mean_ms >= CPU_TIME_SHARE_THRESHOLD:
        resource = "cpu"
        reason = (
            f"Yürütme süresinin %{(cpu_ms / mean_ms) * 100:.0f}'i CPU'da geçti "
            f"({cpu_ms:.1f}ms / {mean_ms:.1f}ms), I/O oranı düşük."
        )
        confidence = "observed"
    elif cpu_ms is not None and mean_ms > 0:
        unexplained = mean_ms - cpu_ms
        if unexplained >= MIN_UNEXPLAINED_MS and unexplained / mean_ms >= UNEXPLAINED_TIME_SHARE_THRESHOLD:
            resource = "lock"
            reason = (
                f"CPU ({cpu_ms:.1f}ms) ve I/O ile açıklanamayan {unexplained:.1f}ms fark var — "
                f"kilit/bekleme olabilir. dbace sorgu başına kilit bekleme süresi tutmuyor; kesin "
                f"teşhis için Activity sekmesinden canlı bloklanmaları kontrol edin."
            )
            confidence = "inferred"
        else:
            resource = "cpu"
            reason = f"Baskın sinyal CPU zamanı ({cpu_ms:.1f}ms / {mean_ms:.1f}ms), I/O düşük."
            confidence = "observed"
    else:
        resource = "unknown"
        reason = (
            "CPU/I/O ayrımı için exec_user_time/exec_sys_time verisi yok — PostgreSQL sürümü "
            "veya pg_stat_statements ayarları bu sütunları desteklemiyor olabilir "
            "(Ön koşullar panelini kontrol edin)."
        )
        confidence = "inferred"

    return QueryDiagnosis(
        queryid=row.queryid,
        query=row.query,
        calls=row.calls,
        mean_time_ms=mean_ms,
        total_time_ms=float(row.total_time_ms or 0),
        resource=resource,
        reason=reason,
        confidence=confidence,
    )


def diagnose_queries(rows) -> list[QueryDiagnosis]:
    return [diagnose_query(row) for row in rows]
