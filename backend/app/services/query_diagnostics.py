"""Kaynak bazlı sorgu tanısı (Faz 16 İŞ 3): her yavaş sorgu için darboğazın I/O, CPU, bellek ya
da kilit/bekleme olduğunu — ve bunu HANGİ metriğin söylediğini — belirler. `SlowQuerySample`'ın
zaten topladığı sütunlardan (shared/local/temp blk sayaçları, exec_user_time/exec_sys_time)
türetilir; ekstra bir canlı sorgu ÇALIŞTIRMAZ.

FAZ 25 — BEKLEME VERİSİ ARTIK BİRİNCİL KANIT:
Yukarıdaki türetme, `exec_user_time`/`exec_sys_time` sütunları dolu olduğunda çalışıyordu; bu
sütunlar pg_stat_statements sürüm/ayarına bağlı ve pratikte çoğu kurulumda BOŞ geliyor — o
yüzden sınıflandırma sık sık "unknown" dönüyordu. Bekleme örnekleyicisi (Faz 25 İŞ 1) artık
sorgunun süresini NEREDE geçirdiğini doğrudan ölçüyor. Bir sorgunun bekleme profili varsa
teşhis ONDAN yapılır ve `confidence="observed"` olur; yoksa eski türetme aynen devrede kalır.

Bunun bir sonucu: "lock" sınıfı artık çıkarım değil ÖLÇÜM olabiliyor. Aşağıdaki eski yol için
dürüstlük kuralı hâlâ geçerli (bkz. SORULAR.md): bekleme verisi olmadan kilit teşhisi
`confidence="inferred"` kalır ve CPU+I/O ile açıklanamayan bir süre farkı olduğunda öne
sürülür, asla kesin teşhis olarak sunulmaz.
"""

from __future__ import annotations

from dataclasses import dataclass

# performance_insights.py'nin "Disk okuma oranı yüksek" eşiğiyle aynı (%10) — iki modülün aynı
# sinyali farklı eşiklerle yorumlaması tutarsızlık yaratırdı.
IO_READ_RATIO_THRESHOLD = 0.10
CPU_TIME_SHARE_THRESHOLD = 0.70
UNEXPLAINED_TIME_SHARE_THRESHOLD = 0.30
MIN_UNEXPLAINED_MS = 5.0


#: Bekleme kategorisi → darboğaz kaynağı. Bekleme sözlüğü (domain/waits.py) daha ayrıntılı;
#: burada tanı sınıfına indirgeniyor ki arayüzün mevcut dört sınıfı (io/cpu/memory/lock)
#: değişmeden kalsın.
WAIT_CATEGORY_TO_RESOURCE: dict[str, str] = {
    "cpu": "cpu",
    "io": "io",
    "lock": "lock",
    "lwlock": "lock",
    "buffer_pin": "lock",
    "memory": "memory",
    "ipc": "cpu",
    "client": "client",
}

#: Bekleme profilinden teşhis koymak için gereken en düşük pay. Altındaysa tek bir suçlu yok
#: demektir; `database_load.py`'deki baskınlık eşiğiyle aynı sayı — iki modülün aynı soruya
#: farklı eşikle cevap vermesi tutarsızlık olurdu.
WAIT_DOMINANCE_THRESHOLD_PCT = 40.0


@dataclass
class QueryDiagnosis:
    queryid: str | None
    query: str
    calls: int
    mean_time_ms: float
    total_time_ms: float
    resource: str  # io | cpu | memory | lock | client | unknown
    reason: str
    confidence: str  # observed | inferred


def _cpu_time_ms(row) -> float | None:
    user = row.exec_user_time
    sys = row.exec_sys_time
    if user is None and sys is None:
        return None
    return float(user or 0) + float(sys or 0)


def _diagnose_from_waits(row, profile) -> QueryDiagnosis | None:
    """Bekleme profilinden teşhis. `profile`: paya göre sıralı CategoryShare listesi."""
    if not profile:
        return None
    top = profile[0]
    resource = WAIT_CATEGORY_TO_RESOURCE.get(top.category)
    if resource is None:
        return None
    if top.share_pct < WAIT_DOMINANCE_THRESHOLD_PCT:
        # Yük dağılmış: tek bir kaynağa işaret etmek yanıltıcı olur, ama ölçüm yine de var —
        # kullanıcıya dağılımı söylüyoruz.
        spread = ", ".join(f"%{s.share_pct:.0f} {s.label.lower()}" for s in profile[:3])
        return QueryDiagnosis(
            queryid=row.queryid, query=row.query, calls=row.calls,
            mean_time_ms=float(row.mean_time_ms or 0), total_time_ms=float(row.total_time_ms or 0),
            resource="unknown",
            reason=(
                f"Bekleme ölçümüne göre süre tek bir kaynakta yoğunlaşmıyor ({spread}). "
                "Tek bir değişiklikle toparlanması beklenmemeli."
            ),
            confidence="observed",
        )
    return QueryDiagnosis(
        queryid=row.queryid, query=row.query, calls=row.calls,
        mean_time_ms=float(row.mean_time_ms or 0), total_time_ms=float(row.total_time_ms or 0),
        resource=resource,
        reason=(
            f"Bekleme ölçümü: sürenin %{top.share_pct:.0f}'i {top.label.lower()} olarak geçti. "
            f"{top.meaning}"
        ),
        # ÇIKARIM DEĞİL ÖLÇÜM: aktif oturum örneklemesi sorgunun nerede beklediğini doğrudan
        # gördü. Eski yol (blok sayaçlarından türetme) buna göre ikincil kaldı.
        confidence="observed",
    )


def diagnose_query(row, wait_profile=None) -> QueryDiagnosis:
    """`row` is a SlowQuerySample (or any object with the same attribute names).

    `wait_profile` verilmişse (services/database_load.py::wait_profiles_by_query) teşhis
    ondan yapılır — ölçüm, türetmeden önce gelir.
    """
    from_waits = _diagnose_from_waits(row, wait_profile)
    if from_waits is not None:
        return from_waits

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
            "CPU/I/O ayrımı yapılamadı: bu sorgu için bekleme örneği birikmemiş ve "
            "exec_user_time/exec_sys_time sütunları boş (PostgreSQL sürümü ya da "
            "pg_stat_statements ayarları desteklemiyor olabilir). Veritabanı Yükü sekmesi "
            "birkaç dakika veri topladıktan sonra bu teşhis ölçümle yapılabilir — Ön koşullar "
            "panelini de kontrol edin."
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


def diagnose_queries(rows, wait_profiles: dict | None = None) -> list[QueryDiagnosis]:
    profiles = wait_profiles or {}
    return [diagnose_query(row, profiles.get(row.queryid or "")) for row in rows]
