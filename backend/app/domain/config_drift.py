"""Düğümler arası yapılandırma sapması (Faz 28 İŞ 4).

Mevcut parametre denetimi **zaman eksenli**: "dünden beri ne değişti". Eksik olan
**düğümler arası** sapma — ve cluster'da asıl arıza sebebi budur, çünkü sapma normal
çalışmada hiçbir belirti vermez ve **tam olarak failover anında** ortaya çıkar: yani en kötü
anda, en az hazırlıklı olunan anda.

## Sınıflandırma neden şart

"Bütün parametreler aynı olsun" demek işe yaramaz: bir replikada `hot_standby` açık,
primary'de kapalı olur; `primary_conninfo` yalnızca replikada dolu olur; gecikmeli bir
replikada `recovery_min_apply_delay` bilerek farklıdır. Hepsini sapma saymak, listeyi
gürültüye boğar ve gerçek sapmayı görünmez yapar — bastırma işinin (İŞ 2) tam tersi bir
hata.

Üç sınıf var ve ayrımın dayanağı **sonuç**, tercih değil:

* `MUST_MATCH` — farklıysa **replikasyon bozulur ya da düğüm failover sonrası açılmaz.**
  PostgreSQL bunu belgelemiş: standby'da `max_connections`, `max_worker_processes`,
  `max_wal_senders`, `max_prepared_transactions` ve `max_locks_per_transaction`
  primary'dekinden KÜÇÜK olamaz; küçükse kurtarma duraklar ve düğüm hizmet veremez.
  Bu, tercih değil, motorun kuralı.
* `SHOULD_MATCH` — farklıysa sistem çalışır ama **failover sonrası başka türlü davranır.**
  `work_mem` yarısı olan bir düğüme geçildiğinde aynı sorgular diske taşar ve "failover'dan
  sonra sistem yavaşladı" denir; sebebi günlerce aranır.
* `MAY_DIFFER` — farklı olması NORMAL. Sapma olarak bile gösterilmiyor; yalnızca istenirse
  karşılaştırma tablosunda görünüyor.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DriftClass(StrEnum):
    MUST_MATCH = "must_match"
    SHOULD_MATCH = "should_match"
    MAY_DIFFER = "may_differ"


DRIFT_CLASS_LABELS: dict[str, str] = {
    DriftClass.MUST_MATCH: "Aynı olmalı",
    DriftClass.SHOULD_MATCH: "Aynı olması beklenir",
    DriftClass.MAY_DIFFER: "Farklı olabilir",
}

#: Sınıf → bulgu ciddiyeti.
DRIFT_SEVERITY: dict[str, str] = {
    DriftClass.MUST_MATCH: "critical",
    DriftClass.SHOULD_MATCH: "warning",
    DriftClass.MAY_DIFFER: "info",
}


@dataclass(frozen=True)
class ComparedParameter:
    name: str
    drift_class: DriftClass
    #: Sapmanın SONUCU — bulgunun iş etkisi cümlesi buradan geliyor.
    consequence: str


def _p(name: str, drift_class: DriftClass, consequence: str) -> ComparedParameter:
    return ComparedParameter(name, drift_class, consequence)


#: PostgreSQL karşılaştırma listesi.
#:
#: Liste bilinçli olarak DAR: `pg_settings` 300'den fazla satır döndürüyor ve hepsini
#: karşılaştırmak, iki düğüm arasında onlarca anlamsız fark üretip gerçek sapmayı gömerdi.
PG_COMPARED: tuple[ComparedParameter, ...] = (
    # --- Farklıysa düğüm açılmaz / kurtarma durur (PostgreSQL'in kendi kuralı) ---
    _p(
        "max_connections",
        DriftClass.MUST_MATCH,
        "Standby'da primary'den küçükse kurtarma duraklar ve düğüm hizmet veremez; "
        "failover sonrası düğüm hiç açılmayabilir.",
    ),
    _p(
        "max_worker_processes",
        DriftClass.MUST_MATCH,
        "Standby'da primary'den küçükse kurtarma duraklar; paralel sorgu ve mantıksal "
        "replikasyon çalışamaz.",
    ),
    _p(
        "max_wal_senders",
        DriftClass.MUST_MATCH,
        "Failover sonrası yeni primary, mevcut replika sayısını besleyemeyebilir.",
    ),
    _p(
        "max_prepared_transactions",
        DriftClass.MUST_MATCH,
        "Standby'da primary'den küçükse kurtarma duraklar.",
    ),
    _p(
        "max_locks_per_transaction",
        DriftClass.MUST_MATCH,
        "Standby'da primary'den küçükse kurtarma duraklar; büyük şemalarda kilit tablosu "
        "taşar.",
    ),
    _p(
        "wal_level",
        DriftClass.MUST_MATCH,
        "Düğümler arasında farklı WAL seviyesi replikasyonu ve yedek stratejisini kırar.",
    ),
    _p(
        "data_checksums",
        DriftClass.MUST_MATCH,
        "Checksum'ı olmayan bir düğüme failover, sessiz veri bozulmasının fark edilmemesi "
        "demek. Küme kurulurken belirlenir; sonradan değiştirmek veri dizinini yeniden "
        "kurmayı gerektirir.",
    ),
    # --- Çalışır ama failover sonrası davranış değişir ---
    _p(
        "shared_buffers",
        DriftClass.SHOULD_MATCH,
        "Daha küçük bellek havuzuna sahip bir düğüme geçildiğinde okuma yükü diske kayar; "
        "aynı iş yükü belirgin biçimde yavaşlar.",
    ),
    _p(
        "work_mem",
        DriftClass.SHOULD_MATCH,
        "Yarısı olan bir düğüme geçildiğinde sıralama ve hash işlemleri diske taşar; "
        "'failover'dan sonra sistem yavaşladı' şikâyetinin en sık sebebi budur.",
    ),
    _p(
        "maintenance_work_mem",
        DriftClass.SHOULD_MATCH,
        "VACUUM ve index oluşturma süreleri düğümler arasında farklılaşır; bakım penceresi "
        "planları tutmaz.",
    ),
    _p(
        "effective_cache_size",
        DriftClass.SHOULD_MATCH,
        "Planlayıcı farklı maliyet hesaplar; aynı sorgu failover sonrası farklı plan seçebilir.",
    ),
    _p(
        "random_page_cost",
        DriftClass.SHOULD_MATCH,
        "Planlayıcı index/seq scan tercihini değiştirir; aynı sorgu farklı düğümde farklı plan alır.",
    ),
    _p(
        "max_parallel_workers",
        DriftClass.SHOULD_MATCH,
        "Paralel sorgu kapasitesi düğümler arasında değişir; raporlama sorguları failover "
        "sonrası uzar.",
    ),
    _p(
        "max_parallel_workers_per_gather",
        DriftClass.SHOULD_MATCH,
        "Aynı sorgu farklı düğümde farklı paralellikle çalışır.",
    ),
    _p(
        "checkpoint_timeout",
        DriftClass.SHOULD_MATCH,
        "Checkpoint davranışı ve IO dalgalanması düğümler arasında farklılaşır.",
    ),
    _p(
        "max_standby_streaming_delay",
        DriftClass.SHOULD_MATCH,
        "Replikadaki sorguların ne kadar tolere edileceği farklılaşır; bazı replikalarda "
        "sorgular beklenmedik şekilde iptal edilir.",
    ),
    _p(
        "statement_timeout",
        DriftClass.SHOULD_MATCH,
        "Failover sonrası uzun sorgular farklı davranır: bir düğümde iptal edilen sorgu "
        "diğerinde sonsuza kadar çalışır.",
    ),
    _p(
        "timezone",
        DriftClass.SHOULD_MATCH,
        "Zaman damgası yorumları düğümler arasında farklılaşır; raporlarda saat kayması "
        "üretir.",
    ),
    _p(
        "lc_collate",
        DriftClass.SHOULD_MATCH,
        "Sıralama düzeni farklıysa metin index'leri failover sonrası yanlış sonuç verebilir "
        "— sessiz ve tespiti çok zor bir hata sınıfı.",
    ),
    # --- Farklı olması NORMAL ---
    _p("hot_standby", DriftClass.MAY_DIFFER, "Rol farkı; replikada açık olması beklenir."),
    _p("primary_conninfo", DriftClass.MAY_DIFFER, "Yalnızca replikada dolu olur."),
    _p("hot_standby_feedback", DriftClass.MAY_DIFFER, "Replika bazında ayarlanabilir."),
    _p(
        "synchronous_standby_names",
        DriftClass.MAY_DIFFER,
        "Yalnızca primary'de anlamlı; rol değiştikçe değişir.",
    ),
    _p(
        "recovery_min_apply_delay",
        DriftClass.MAY_DIFFER,
        "Gecikmeli replika bilinçli olarak farklı ayarlanır.",
    ),
    _p("listen_addresses", DriftClass.MAY_DIFFER, "Ağ yapılandırması düğüme özgüdür."),
    _p("port", DriftClass.MAY_DIFFER, "Düğüme özgü."),
    _p("cluster_name", DriftClass.MAY_DIFFER, "Düğüm adını taşır."),
    _p("archive_command", DriftClass.MAY_DIFFER, "Düğüme özgü yol/hedef içerebilir."),
)

#: SQL Server Always On karşılaştırma listesi (`sys.configurations` adları).
SQLSERVER_COMPARED: tuple[ComparedParameter, ...] = (
    _p(
        "max degree of parallelism",
        DriftClass.SHOULD_MATCH,
        "Failover sonrası aynı sorgu farklı paralellikle çalışır; plan ve süre değişir. "
        "Always On'da en sık gözden kaçan sapma budur.",
    ),
    _p(
        "cost threshold for parallelism",
        DriftClass.SHOULD_MATCH,
        "Hangi sorguların paralelleşeceği düğümler arasında farklılaşır.",
    ),
    _p(
        "max server memory (MB)",
        DriftClass.SHOULD_MATCH,
        "Daha az belleğe sahip bir düğüme geçildiğinde buffer pool küçülür ve okuma yükü "
        "diske kayar.",
    ),
    _p(
        "min server memory (MB)",
        DriftClass.SHOULD_MATCH,
        "Bellek baskısı altında davranış farklılaşır.",
    ),
    _p(
        "optimize for ad hoc workloads",
        DriftClass.SHOULD_MATCH,
        "Plan cache davranışı farklılaşır; ad hoc yükte bellek kullanımı değişir.",
    ),
    _p(
        "backup compression default",
        DriftClass.SHOULD_MATCH,
        "Yedek süresi ve boyutu düğüme göre değişir; yedek penceresi planı tutmaz.",
    ),
    _p(
        "remote query timeout (s)",
        DriftClass.MAY_DIFFER,
        "Bağlantı düzeyinde ayar; düğüme özgü olabilir.",
    ),
    _p(
        "fill factor (%)",
        DriftClass.SHOULD_MATCH,
        "Index bakım davranışı farklılaşır.",
    ),
)


def compared_parameters(engine: str) -> tuple[ComparedParameter, ...]:
    return SQLSERVER_COMPARED if engine == "sqlserver" else PG_COMPARED


def spec_for(engine: str, name: str) -> ComparedParameter | None:
    for parameter in compared_parameters(engine):
        if parameter.name == name:
            return parameter
    return None


#: Trace flag'ler SQL Server'da ayrı bir kategori: `sys.configurations`'da görünmezler ama
#: sorgu planlayıcı davranışını kökten değiştirirler. Bir düğümde açık bir trace flag'in
#: diğerinde kapalı olması, failover sonrası "aynı sorgu bambaşka plan aldı" demek.
TRACE_FLAG_CONSEQUENCE = (
    "Trace flag'ler sorgu planlayıcı ve motor davranışını değiştirir. Bir düğümde açık olup "
    "diğerinde kapalı olan bir flag, failover sonrası aynı sorgunun bambaşka bir plan "
    "almasına yol açar ve sorunun sebebi yapılandırmada aranmadığı için günlerce bulunamaz."
)


@dataclass
class DriftRow:
    """Tek bir parametrenin düğümler arası durumu."""

    name: str
    drift_class: str
    consequence: str
    #: düğüm adı → değer (okunmayan düğümde None)
    values: dict[str, str | None]
    #: Farklı değer var mı (okunamayan düğümler hariç).
    diverged: bool
    #: Değeri okunamayan düğümler — "aynı" diye raporlanmamalılar.
    missing_nodes: list[str]

    @property
    def severity(self) -> str:
        if not self.diverged:
            return "ok"
        return DRIFT_SEVERITY.get(self.drift_class, "info")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "drift_class": self.drift_class,
            "drift_class_label": DRIFT_CLASS_LABELS.get(self.drift_class, self.drift_class),
            "consequence": self.consequence,
            "values": self.values,
            "diverged": self.diverged,
            "missing_nodes": self.missing_nodes,
            "severity": self.severity,
        }


def compare_nodes(engine: str, node_settings: dict[str, dict[str, str | None]]) -> list[DriftRow]:
    """`{düğüm adı: {parametre: değer}}` → sapma satırları.

    OKUNAMAYAN DEĞER "AYNI" DEĞİLDİR. Bir düğümden parametre alınamadıysa o düğüm
    `missing_nodes`'a yazılıyor ve karşılaştırmaya girmiyor; eksik veriyi "uyumlu" saymak,
    tam da görülmesi gereken sapmayı gizlerdi.
    """
    rows: list[DriftRow] = []
    node_names = list(node_settings)
    for parameter in compared_parameters(engine):
        values: dict[str, str | None] = {}
        missing: list[str] = []
        for node in node_names:
            value = (node_settings.get(node) or {}).get(parameter.name)
            values[node] = value
            if value is None:
                missing.append(node)
        present = [v for v in values.values() if v is not None]
        diverged = len(set(present)) > 1
        rows.append(
            DriftRow(
                name=parameter.name,
                drift_class=str(parameter.drift_class),
                consequence=parameter.consequence,
                values=values,
                diverged=diverged,
                missing_nodes=missing,
            )
        )
    # Önce ciddi olanlar, sonra ad. Sıralama alfabetik olsaydı kritik bir sapma listenin
    # ortasında kalır ve gözden kaçardı.
    order = {"critical": 0, "warning": 1, "info": 2, "ok": 3}
    rows.sort(key=lambda r: (order.get(r.severity, 9), r.name))
    return rows
