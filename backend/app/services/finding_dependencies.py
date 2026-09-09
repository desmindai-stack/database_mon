"""Bulgular arası bağımlılık ve bastırma (Faz 28 İŞ 2).

## Sorun

Bir düğüm düştüğünde rapor 40 bulgu üretiyordu. Bunların biri gerçekti ("düğüme
erişilemiyor"), kalan 39'u onun SONUCUYDU: parametre denetimi dünkü fotoğrafı okuyup
sapma bildiriyor, yedek bölümü eskimiş yedek diyor, ön koşullar eksik eklenti sayıyor,
kapasite tahmini eski trendden konuşuyor. Hepsi teknik olarak doğru, hepsi pratik olarak
gürültü — çünkü **tek bir şey düzeltilince hepsi birden kaybolacak**.

40 bulgunun asıl zararı sayı değil: gerçek bulgu 40 satırın içinde kayboluyor ve kritik
sayacı 40 gösterince "kritik" kelimesi anlamını yitiriyor.

## Çözüm ve tek merkez kuralı

Bağımlılık grafiği **yalnızca burada** duruyor. Bölümler kendi bastırma mantığını
yazmıyor; hiçbir bulgu tipi "beni şu durumda gösterme" demiyor. Sebep: bastırma
kararının bütünlüğü ancak tek yerden görülebilir — 11 bölüme dağılmış bir mantıkta
"neden bu bulguyu görmüyorum" sorusunun cevabı 11 dosyada aranır ve iki bölüm birbirini
bastırdığında kimse fark etmez.

Aynı grafik hem raporu hem alarmları besliyor (`suppressed_alert_metrics`): "40 alarm
e-postası gitmesin" isteğinin karşılığı, alarm tarafına ayrı bir mantık yazmak değil aynı
grafikten geçmek.

## Bastırma neyi YAPMAZ

Bastırılan bulgu **silinmiyor**: raporda kalıyor, işaretleniyor ve "kök sebep nedeniyle N
kontrol yapılamadı" satırının altında açılabiliyor. Silmek, bastırma kuralı yanlışsa
gerçek bir sorunu görünmez yapardı; işaretlemek ise en kötü ihtimalle bir tıklama
maliyeti.

## Neden bu tetikleyiciler

Kök sebep olarak yalnızca **bağlı analizleri gerçekten imkânsız kılan** durumlar
seçildi. En kolay hata, "kısmi" bir sorunu kök sebep saymak olurdu: bir düğüm günün 20
saati ayakta olup son 4 saatte düştüyse o 20 saatin performans bulguları GERÇEKTİR ve
bastırılmamalıdır. Bu yüzden tetikleyici "dönemde hiç ölçüm yok" ve "dönem sonunda
toplama hâlâ durmuş" — ikisi de bağlı analizlerin dayanacağı canlı verinin olmadığını
kanıtlıyor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable


class RootCause(StrEnum):
    """Kendisi düzeltilince bir yığın bulguyu birden kapatan durumlar."""

    #: Veritabanından hiç veri alınamıyor (hiç ölçüm yok ya da toplama durmuş ve sürüyor).
    UNREACHABLE = "unreachable"
    #: Patroni servisi kapalı — lider/replika/rol bilgisi ONUN API'sinden geliyor.
    PATRONI_DOWN = "patroni_down"
    #: etcd quorum kaybı — quorum olmadan Patroni lider seçemez.
    ETCD_QUORUM_LOST = "etcd_quorum_lost"


@dataclass(frozen=True)
class DependencyRule:
    """Bir kök sebep ve ona bağlı olan her şey."""

    root: RootCause
    #: Kullanıcıya görünen kök sebep adı.
    label: str
    #: Kök sebebi ÜRETEN rapor bulgusu türleri (`fingerprint_parts[0]`).
    finding_kinds: tuple[str, ...]
    #: Bastırma kapsamı: "instance" ya da "group".
    scope: str
    #: Tamamı bastırılan rapor bölümleri.
    suppressed_sections: tuple[str, ...] = ()
    #: Bölümü bastırılmasa da bastırılan tek tek bulgu türleri.
    suppressed_kinds: tuple[str, ...] = ()
    #: Bu kök sebep etkinken üretilmemesi gereken alarm metrikleri.
    suppressed_alert_metrics: tuple[str, ...] = ()
    #: Kök sebebi işaret eden alarm metrikleri (alarm tarafında tetikleyici).
    alert_metrics: tuple[str, ...] = ()
    #: Neden bastırıldığının kullanıcıya gösterilen açıklaması.
    reason: str = ""


#: Erişilemeyen bir veritabanında ANLAMINI YİTİREN bölümler.
#:
#: Hepsinin ortak özelliği, canlı bağlantı yerine SAKLANMIŞ günlük fotoğraflardan
#: beslenmeleri: parametre denetimi dünkü `DailyStateSnapshot`'ı, ön koşullar dünkü
#: kontrol sonucunu, yedek bölümü son sondayı, kapasite eski trendi okuyor. Veritabanı
#: erişilemezken bunların hepsi "dün böyleydi" diyor ve bugünün sorunuyla ilgisi yok.
_UNREACHABLE_SECTIONS = (
    "backup",
    "performance",
    "resources",
    "schema",
    "blocking",
    "capacity",
    "parameters",
    "prerequisites",
)

#: BASTIRILMAYAN bölümler ve gerekçeleri:
#:
#: * `availability` — kök sebebin KENDİSİ orada; bastırmak tek gerçek bulguyu silmek olurdu.
#: * `cluster` — cluster bilgisi host-agent/Patroni API üzerinden geliyor, yani veritabanı
#:   bağlantısından BAĞIMSIZ bir kanal. Veritabanına bağlanılamazken cluster bilgisi hâlâ
#:   doğru olabilir ve o an en çok ihtiyaç duyulan bilgi odur.
#: * `alerts` — alarm gürültüsünün kendisini raporlayan bölüm; bastırmak, bastırmanın
#:   çalışıp çalışmadığını görmeyi engellerdi.

DEPENDENCY_RULES: tuple[DependencyRule, ...] = (
    DependencyRule(
        root=RootCause.UNREACHABLE,
        label="Veritabanına erişilemiyor",
        finding_kinds=("no_samples", "unreachable_now"),
        scope="instance",
        suppressed_sections=_UNREACHABLE_SECTIONS,
        alert_metrics=("collection_failed",),
        # Erişilemeyen bir veritabanı için eşik alarmı üretmek anlamsız: ölçüm zaten yok,
        # üretilen her alarm eski değerden ya da yokluktan doğar.
        suppressed_alert_metrics=(
            "connection_utilization",
            "cache_hit_ratio",
            "replication_lag_bytes",
            "active_sessions",
            "database_size_bytes",
            "deadlocks",
            "temp_bytes",
        ),
        reason=(
            "Veritabanına erişilemediği için bu kontroller canlı veriyle değil, saklanmış "
            "eski fotoğraflarla çalışırdı; sonuçları bugünün durumunu yansıtmaz."
        ),
    ),
    DependencyRule(
        root=RootCause.PATRONI_DOWN,
        label="Patroni servisi kapalı",
        # `service_down` bulgusu servis adını üçüncü parçada taşıyor; eşleşme
        # `_matches_kind` içinde parçaya bakarak yapılıyor.
        finding_kinds=("service_down:patroni",),
        scope="instance",
        suppressed_kinds=("no_leader", "leader_change", "replication_lag"),
        alert_metrics=("patroni_down",),
        suppressed_alert_metrics=("cluster_has_leader", "replication_lag_bytes"),
        reason=(
            "Lider/replika rolü ve lag bilgisi Patroni'nin kendi API'sinden okunuyor; "
            "Patroni kapalıyken bu bilgiler ölçülemez, yokluğu bir arıza değildir."
        ),
    ),
    DependencyRule(
        root=RootCause.ETCD_QUORUM_LOST,
        label="etcd quorum kaybı",
        finding_kinds=("etcd_quorum",),
        scope="group",
        suppressed_kinds=("no_leader", "leader_change"),
        alert_metrics=("etcd_quorum_lost",),
        suppressed_alert_metrics=("cluster_has_leader",),
        reason=(
            "Quorum olmadan Patroni lider seçemez; lidersizlik bu durumun sonucudur, ayrı "
            "bir arıza değil."
        ),
    ),
)


def _matches_kind(patterns: Iterable[str], parts: tuple[str, ...]) -> bool:
    """Bulgu türü desenlerle eşleşiyor mu.

    Desen iki biçimde: `"no_leader"` yalnızca türe bakar; `"service_down:patroni"` ise
    türün yanında parçalardan birinin de eşleşmesini şart koşar. İkincisi olmadan
    "patroni kapalı" ile "haproxy kapalı" ayırt edilemezdi ve haproxy'nin kapalı olması
    lider bulgularını bastırırdı — oysa haproxy'nin lider seçimiyle ilgisi yok.
    """
    if not parts:
        return False
    kind = parts[0]
    rest = set(parts[1:])
    for pattern in patterns:
        head, _, qualifier = pattern.partition(":")
        if head != kind:
            continue
        if not qualifier or qualifier in rest:
            return True
    return False


@dataclass
class RootCauseHit:
    """Tespit edilmiş bir kök sebep."""

    rule: DependencyRule
    fingerprint: str
    title: str
    #: Kapsam kimliği: instance id ya da group id.
    scope_id: int | None


@dataclass
class SuppressionPlan:
    """Hangi bulgu bastırılacak, hangisi kök sebep."""

    #: fingerprint -> kök sebep fingerprint'i
    suppressed_by: dict[str, str] = field(default_factory=dict)
    #: kök sebep fingerprint'leri
    roots: dict[str, RootCauseHit] = field(default_factory=dict)
    #: kök sebep fingerprint -> bastırılan bulgu sayısı
    counts: dict[str, int] = field(default_factory=dict)

    def is_suppressed(self, fingerprint: str) -> bool:
        return fingerprint in self.suppressed_by

    def is_root(self, fingerprint: str) -> bool:
        return fingerprint in self.roots

    def summary_rows(self) -> list[dict[str, Any]]:
        """Raporda gösterilecek "kök sebep nedeniyle N kontrol yapılamadı" satırları."""
        rows = []
        for fingerprint, hit in self.roots.items():
            count = self.counts.get(fingerprint, 0)
            if not count:
                continue
            rows.append(
                {
                    "root_fingerprint": fingerprint,
                    "root_cause": str(hit.rule.root),
                    "root_label": hit.rule.label,
                    "root_title": hit.title,
                    "suppressed_count": count,
                    "reason": hit.rule.reason,
                }
            )
        rows.sort(key=lambda r: r["suppressed_count"], reverse=True)
        return rows


def _draft_scope_ids(draft, group_of_instance: dict[int, int | None]) -> tuple[int | None, int | None]:
    """Bulgunun (instance_id, group_id) hedefi.

    Grup kapsamlı bir kök sebep (etcd quorum), o gruptaki instance'ların bulgularını da
    bastırabilmeli — yoksa quorum kaybında her düğüm için ayrı ayrı "lider yok" bulgusu
    kalırdı ve tam olarak kaçınmak istediğimiz yığılma olurdu.
    """
    if draft.related_object_type == "instance" and draft.related_object_id:
        instance_id = int(draft.related_object_id)
        return instance_id, group_of_instance.get(instance_id)
    if draft.related_object_type == "group" and draft.related_object_id:
        return None, int(draft.related_object_id)
    return None, None


def build_suppression_plan(
    drafts: list,
    fingerprints: dict[int, str],
    group_of_instance: dict[int, int | None],
) -> SuppressionPlan:
    """Bastırma planını üretir.

    `fingerprints`: taslağın kimliği (`id(draft)`) → hesaplanmış fingerprint. Fingerprint
    motorda hesaplanıyor ve taslakta durmuyor; bu modülün onu yeniden hesaplaması iki ayrı
    hesap demek olurdu.
    """
    plan = SuppressionPlan()

    # 1. Kök sebepleri bul.
    for draft in drafts:
        fingerprint = fingerprints.get(id(draft))
        if not fingerprint:
            continue
        for rule in DEPENDENCY_RULES:
            if not _matches_kind(rule.finding_kinds, tuple(draft.fingerprint_parts)):
                continue
            instance_id, group_id = _draft_scope_ids(draft, group_of_instance)
            scope_id = instance_id if rule.scope == "instance" else group_id
            if scope_id is None:
                # Kapsamı belirsiz bir kök sebep neyi bastıracağını bilemez; bastırma
                # yapılmıyor ve bulgu normal bir bulgu olarak kalıyor.
                continue
            plan.roots[fingerprint] = RootCauseHit(
                rule=rule, fingerprint=fingerprint, title=draft.title, scope_id=scope_id
            )
            break

    if not plan.roots:
        return plan

    # 2. Bağlı bulguları bastır.
    for draft in drafts:
        fingerprint = fingerprints.get(id(draft))
        if not fingerprint or fingerprint in plan.roots:
            # Kök sebep kendini bastırmaz. İki kök sebep de birbirini bastırmaz: hangisinin
            # "daha kök" olduğuna karar vermek için elimizde kanıt yok ve yanlış tahmin,
            # gerçek bir arızayı gizlemek demek olurdu.
            continue
        instance_id, group_id = _draft_scope_ids(draft, group_of_instance)
        parts = tuple(draft.fingerprint_parts)
        for root_fingerprint, hit in plan.roots.items():
            rule = hit.rule
            target = instance_id if rule.scope == "instance" else group_id
            if target is None or target != hit.scope_id:
                continue
            hit_by_section = draft.section in rule.suppressed_sections
            hit_by_kind = _matches_kind(rule.suppressed_kinds, parts)
            if not (hit_by_section or hit_by_kind):
                continue
            plan.suppressed_by[fingerprint] = root_fingerprint
            plan.counts[root_fingerprint] = plan.counts.get(root_fingerprint, 0) + 1
            break

    return plan


# --- Alarm tarafı ---------------------------------------------------------------------------


def suppressed_alert_metrics(active_metrics: dict[str, Any]) -> dict[str, str]:
    """Şu anki metriklere göre bastırılması gereken alarm metrikleri.

    Dönen sözlük: bastırılan metrik → kök sebep etiketi. Alarm motoru bu metrikler için
    olay AÇMIYOR; böylece bir düğüm düştüğünde 40 alarm e-postası yerine bir tane gidiyor.

    Kök sebep alarmının KENDİSİ bastırılmıyor — bastırılsaydı hiç haber gitmezdi ki bu,
    40 e-postadan çok daha kötü olurdu.
    """
    suppressed: dict[str, str] = {}
    for rule in DEPENDENCY_RULES:
        if not _alert_root_active(rule, active_metrics):
            continue
        for metric in rule.suppressed_alert_metrics:
            if metric in rule.alert_metrics:
                continue
            suppressed.setdefault(metric, rule.label)
    return suppressed


def _alert_root_active(rule: DependencyRule, metrics: dict[str, Any]) -> bool:
    """Kök sebep metriği şu an tetiklenmiş mi.

    Metrikler bayrak biçiminde (0/1) geliyor; sıfırdan büyük olan tetiklenmiş sayılıyor.
    `None` "ölçülemedi" demek ve tetiklenmiş SAYILMIYOR: ölçülemeyen bir bayrağı "sorun
    var" kabul etmek, ölçüm eksikliğini bastırmaya çevirirdi.
    """
    for metric in rule.alert_metrics:
        value = metrics.get(metric)
        if value is None:
            continue
        try:
            if float(value) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False
