"""Plan analizi: tahmini vs gerçek satır sapması ve süre dağılımı (Faz 26 İŞ 2).

Sorgu gecikmesinin en yaygın sebeplerinden biri planlayıcının satır sayısını yanlış tahmin
etmesidir. Planlayıcı "bu koşuldan 10 satır döner" derse nested loop seçer; gerçekte 2 milyon
satır dönüyorsa aynı plan 200 bin kez iç döngü çalıştırır. Plan ucuz GÖRÜNÜR, pratikte
pahalıdır — ve `EXPLAIN` (ANALYZE olmadan) bunu ASLA göstermez, çünkü orada yalnızca tahmin
vardır.

ÜÇ TUZAK, üçü de burada ele alınıyor:

1. **`Plan Rows` ve `Actual Rows` DÖNGÜ BAŞINADIR.** Nested loop'un iç tarafında bir düğüm
   200 bin kez çalışıyorsa `Actual Rows: 1` yazar — toplam 200 bin satır demektir. Oranı
   ham değerlerden hesaplamak doğru (ikisi de döngü başına), ama TOPLAM satır sayısını
   `rows × loops` ile hesaplamak gerekir.
2. **`Actual Total Time` de DÖNGÜ BAŞINA ORTALAMADIR** ve ÇOCUKLARI İÇERİR. Düğümün kendi
   maliyeti = (kendi toplam süresi × döngü sayısı) − (çocukların aynı şekilde hesaplanan
   süreleri). Bunu yapmadan "en pahalı düğüm" her zaman kök düğüm çıkar ve liste anlamsız olur.
3. **Sapma YUKARI DOĞRU YAYILIR.** Bir tarama düğümü 10 yerine 2 milyon satır döndürdüyse,
   onun üstündeki her join de yanlış tahmin eder. Hepsini "sapmış" diye işaretlemek
   kullanıcıya 12 suçlu gösterir; asıl suçlu EN DERİNDEKİDİR. Bu modül kök nedeni ayrıca
   işaretliyor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.services.advice import Advice, AdviceStep, unavailable

#: Sapma bu katı aşarsa düğüm işaretleniyor. 10x, PostgreSQL topluluğunda yaygın olarak
#: kullanılan eşik: altındaki sapmalar planı nadiren değiştirir, üstündekiler genelde
#: yanlış birleştirme (join) stratejisine yol açar.
MISESTIMATE_RATIO = 10.0

#: Küçük mutlak sayılarda oran yanıltıcı: 1 yerine 15 satır dönmesi 15x sapmadır ama hiçbir
#: şeyi değiştirmez. Bu eşiğin altındaki düğümler işaretlenmiyor.
MIN_ROWS_FOR_MISESTIMATE = 100.0

#: "En pahalı düğümler" listesinde kaç düğüm gösterilecek.
TOP_NODE_LIMIT = 5

#: Bir düğümün "sıcak" sayılması için toplam sürenin en az bu kadarını tüketmesi gerekiyor.
HOT_NODE_MIN_SHARE_PCT = 5.0

_BASE_SCAN_TYPES = frozenset(
    {"Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan", "Tid Scan", "Sample Scan"}
)

#: Koşul metninde ifade (fonksiyon çağrısı) kullanımı — planlayıcı bunlar için istatistik
#: tutmaz ve varsayılan seçicilik tahmini kullanır, yani neredeyse her zaman yanılır.
_EXPRESSION_HINT = re.compile(r"\b(lower|upper|coalesce|substr|substring|date_trunc|extract|cast|to_char|abs|round)\s*\(", re.IGNORECASE)


@dataclass
class NodeAnalysis:
    """Tek bir plan düğümünün ölçülmüş gerçeği."""

    node_type: str
    relation_name: str | None
    #: Döngü başına tahmini / gerçek satır (EXPLAIN'in ham değerleri).
    plan_rows: float | None
    actual_rows: float | None
    loops: float
    #: Toplam satır = döngü başına × döngü sayısı.
    estimated_total_rows: float | None
    actual_total_rows: float | None
    #: gerçek/tahmini. 1.0 = kusursuz. >1 = az tahmin edilmiş (tehlikeli olan bu).
    estimate_ratio: float | None
    misestimated: bool
    underestimated: bool
    #: Düğümün KENDİ süresi (çocuklar hariç, döngüler dahil).
    self_time_ms: float | None
    total_time_ms: float | None
    time_share_pct: float
    conditions: list[str] = field(default_factory=list)
    path: str = ""
    depth: int = 0
    #: Sapmanın KÖK nedeni bu düğüm mü — sapma yukarı yayıldığı için önemli.
    is_root_cause: bool = False


@dataclass
class PlanAnalysis:
    nodes: list[NodeAnalysis] = field(default_factory=list)
    hottest: list[NodeAnalysis] = field(default_factory=list)
    misestimated: list[NodeAnalysis] = field(default_factory=list)
    root_causes: list[NodeAnalysis] = field(default_factory=list)
    total_time_ms: float = 0.0
    has_actual_rows: bool = False
    unavailable_reason: str | None = None


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _conditions(node: dict[str, Any]) -> list[str]:
    """Düğümdeki filtre/koşul metinleri — öneri üretimi bunlara bakıyor."""
    out: list[str] = []
    for key in ("Filter", "Index Cond", "Recheck Cond", "Hash Cond", "Join Filter", "Merge Cond"):
        value = node.get(key)
        if value:
            out.append(str(value))
    return out


def _node_label(node: dict[str, Any]) -> str:
    node_type = str(node.get("Node Type") or "Unknown")
    relation = node.get("Relation Name") or node.get("Index Name")
    return f"{node_type} on {relation}" if relation else node_type


def analyze_plan(plan_json: dict[str, Any]) -> PlanAnalysis:
    """EXPLAIN (ANALYZE) planından düğüm bazında sapma ve süre analizi.

    `plan_json` ya kök zarf ({"Plan": {...}}) ya da doğrudan kök düğüm olabilir.
    """
    analysis = PlanAnalysis()
    root = plan_json.get("Plan") if isinstance(plan_json.get("Plan"), dict) else plan_json
    if not isinstance(root, dict):
        analysis.unavailable_reason = "Plan ağacı okunamadı."
        return analysis

    nodes: list[NodeAnalysis] = []
    _walk(root, nodes, depth=0, path="")

    analysis.nodes = nodes
    analysis.has_actual_rows = any(n.actual_rows is not None for n in nodes)
    if not analysis.has_actual_rows:
        analysis.unavailable_reason = (
            "Bu planda GERÇEK satır sayısı yok — plan ANALYZE olmadan alınmış ya da "
            "auto_explain.log_analyze kapalı. Tahmini/gerçek sapma yalnızca gerçek değerlerle "
            "ölçülebilir; tahminleri birbiriyle karşılaştırmak hiçbir şey söylemez."
        )
        return analysis

    # Süre payları: kök düğümün toplam süresi paydadır.
    root_total = nodes[0].total_time_ms if nodes and nodes[0].total_time_ms else None
    analysis.total_time_ms = root_total or 0.0
    if root_total and root_total > 0:
        for node in nodes:
            if node.self_time_ms is not None:
                node.time_share_pct = round(node.self_time_ms * 100.0 / root_total, 1)

    analysis.hottest = sorted(
        (n for n in nodes if n.time_share_pct >= HOT_NODE_MIN_SHARE_PCT),
        key=lambda n: -(n.self_time_ms or 0),
    )[:TOP_NODE_LIMIT]

    analysis.misestimated = sorted(
        (n for n in nodes if n.misestimated), key=lambda n: -(n.estimate_ratio or 0)
    )
    analysis.root_causes = _mark_root_causes(nodes)
    return analysis


def _walk(node: dict[str, Any], out: list[NodeAnalysis], *, depth: int, path: str) -> NodeAnalysis:
    children_json = [c for c in (node.get("Plans") or []) if isinstance(c, dict)]
    label = _node_label(node)
    node_path = f"{path} > {label}" if path else label

    analysis = NodeAnalysis(
        node_type=str(node.get("Node Type") or "Unknown"),
        relation_name=node.get("Relation Name"),
        plan_rows=_f(node.get("Plan Rows")),
        actual_rows=_f(node.get("Actual Rows")),
        loops=_f(node.get("Actual Loops")) or 1.0,
        estimated_total_rows=None,
        actual_total_rows=None,
        estimate_ratio=None,
        misestimated=False,
        underestimated=False,
        self_time_ms=None,
        total_time_ms=None,
        time_share_pct=0.0,
        conditions=_conditions(node),
        path=node_path,
        depth=depth,
    )
    out.append(analysis)

    children = [_walk(child, out, depth=depth + 1, path=node_path) for child in children_json]

    # DÖNGÜ ÇARPANI: hem satırlar hem süre döngü BAŞINA raporlanıyor.
    loops = analysis.loops or 1.0
    if analysis.plan_rows is not None:
        analysis.estimated_total_rows = analysis.plan_rows * loops
    if analysis.actual_rows is not None:
        analysis.actual_total_rows = analysis.actual_rows * loops

    node_total = _f(node.get("Actual Total Time"))
    if node_total is not None:
        analysis.total_time_ms = node_total * loops
        children_total = sum(c.total_time_ms or 0.0 for c in children)
        # Kendi maliyeti çocukları içermez. Bu çıkarma olmadan "en pahalı düğüm" her zaman
        # kök düğüm çıkar (çünkü kök, altındaki her şeyin süresini taşır) ve liste
        # kullanıcıya hiçbir şey söylemez.
        analysis.self_time_ms = max(0.0, analysis.total_time_ms - children_total)

    _score_estimate(analysis)
    return analysis


def _score_estimate(node: NodeAnalysis) -> None:
    if node.plan_rows is None or node.actual_rows is None:
        return
    planned = max(node.plan_rows, 1.0)  # PostgreSQL asla 0 tahmin etmez, 1'e yuvarlar
    actual = node.actual_rows
    ratio = actual / planned if planned else 0.0
    node.estimate_ratio = round(ratio, 2)

    # Küçük mutlak sayılarda oran yanıltıcı: 1 yerine 15 satır 15x sapmadır ama planı
    # değiştirmez. Hem oran hem mutlak eşik aranıyor.
    biggest = max(actual, planned)
    if biggest < MIN_ROWS_FOR_MISESTIMATE:
        return
    if ratio >= MISESTIMATE_RATIO:
        node.misestimated = True
        node.underestimated = True  # az tahmin: nested loop tuzağının kaynağı
    elif ratio > 0 and (1.0 / ratio) >= MISESTIMATE_RATIO:
        node.misestimated = True
        node.underestimated = False


def _mark_root_causes(nodes: list[NodeAnalysis]) -> list[NodeAnalysis]:
    """Sapmanın KÖK nedenini işaretler.

    Sapma yukarı doğru yayılır: bir tarama 10 yerine 2 milyon satır döndürdüyse üstündeki her
    join de yanlış tahmin eder. Hepsini "sapmış" göstermek kullanıcıya 12 suçlu sunar ve hangi
    tabloya bakacağını bulmasını zorlaştırır.

    Kök neden = sapmış olan EN DERİN düğümler. Bunları ayrıca işaretliyoruz; öneri de bunlara
    göre üretiliyor.
    """
    misestimated = [n for n in nodes if n.misestimated]
    if not misestimated:
        return []
    deepest = max(n.depth for n in misestimated)
    roots = [n for n in misestimated if n.depth == deepest]
    # Aynı derinlikte birden çok sapma olabilir (iki farklı tablo) — hepsi kök nedendir.
    for node in roots:
        node.is_root_cause = True
    return roots


# --- Öneri üretimi ------------------------------------------------------------------------


def _relation_of(node: NodeAnalysis) -> str:
    return node.relation_name or "<tablo>"


def _looks_like_expression(node: NodeAnalysis) -> bool:
    return any(_EXPRESSION_HINT.search(cond) for cond in node.conditions)


def _correlated_columns(node: NodeAnalysis) -> list[str]:
    """Aynı tabloda AND ile bağlanmış birden çok kolon koşulu var mı.

    Planlayıcı kolonları BAĞIMSIZ varsayar: `city = 'Ankara' AND plaka = '06'` için
    seçicilikleri ÇARPAR ve gerçekte örtüşen iki koşulda korkunç bir az-tahmin üretir.
    Çözümü uzatılmış istatistiklerdir (CREATE STATISTICS).
    """
    columns: list[str] = []
    for cond in node.conditions:
        for part in re.split(r"\bAND\b", cond, flags=re.IGNORECASE):
            match = re.search(r"\(?\s*([a-z_][a-z0-9_]*)\s*(?:=|<|>|<=|>=|~~)", part.strip(), re.IGNORECASE)
            if match:
                name = match.group(1).lower()
                if name not in columns and name not in {"and", "or", "not"}:
                    columns.append(name)
    return columns


def advice_for_misestimate(node: NodeAnalysis) -> Advice:
    """Sapmış tek bir düğüm için beş parçalı öneri.

    Sıra ÖNEMLİ: en ucuz ve en olası çözümden başlanıyor. ANALYZE saniyeler sürer ve sapmaların
    çoğunu çözer; uzatılmış istatistik ve ifade indeksi daha pahalı ve daha dar çözümlerdir.
    Kullanıcıyı önce `CREATE STATISTICS`'e yollamak, çoğu durumda gereksiz karmaşıklık olurdu.
    """
    relation = _relation_of(node)
    direction = "AZ" if node.underestimated else "FAZLA"
    ratio_text = (
        f"{node.estimate_ratio:.0f}x" if node.estimate_ratio and node.estimate_ratio >= 1
        else f"{1 / node.estimate_ratio:.0f}x ters yönde" if node.estimate_ratio else "?"
    )

    why = (
        f"Planlayıcı bu düğümde {node.plan_rows:.0f} satır bekliyordu, gerçekte "
        f"{node.actual_rows:.0f} satır döndü ({ratio_text} {direction} tahmin). "
        "Satır sayısı yanlış tahmin edilince planlayıcı yanlış birleştirme (join) stratejisi "
        "seçer: az tahminde nested loop seçilir ve milyonlarca kez iç döngü çalışır. Plan "
        "maliyeti UCUZ görünür ama sorgu pratikte pahalıdır — bu yüzden yalnızca EXPLAIN'e "
        "bakan bir inceleme sorunu bulamaz."
    )

    steps = [
        AdviceStep(
            action=(
                f"Önce istatistiklerin tazeliğine bakın — sapmanın en yaygın ve en ucuz "
                f"çözülen sebebi budur."
            ),
            command=(
                "SELECT relname, last_analyze, last_autoanalyze, n_live_tup, n_mod_since_analyze\n"
                "FROM pg_stat_user_tables\n"
                f"WHERE relname = '{relation}';"
            ),
        ),
        AdviceStep(
            action=(
                "`n_mod_since_analyze` canlı satır sayısına göre yüksekse ya da son analiz "
                "eskiyse, tabloyu yeniden analiz edin."
            ),
            command=f"ANALYZE {relation};",
        ),
    ]

    columns = _correlated_columns(node)
    if len(columns) >= 2:
        column_list = ", ".join(columns[:3])
        steps.append(
            AdviceStep(
                action=(
                    f"Bu düğümde birden çok kolon koşulu var ({column_list}). Planlayıcı "
                    "kolonları BAĞIMSIZ varsayar ve seçicilikleri çarpar; kolonlar ilişkiliyse "
                    "(ör. şehir ve posta kodu) bu korkunç bir az-tahmin üretir. Uzatılmış "
                    "istatistik bu ilişkiyi planlayıcıya öğretir."
                ),
                command=(
                    f"CREATE STATISTICS stat_{relation}_{'_'.join(columns[:2])}\n"
                    f"    (dependencies, ndistinct)\n"
                    f"    ON {column_list} FROM {relation};\n"
                    f"ANALYZE {relation};"
                ),
            )
        )

    if _looks_like_expression(node):
        steps.append(
            AdviceStep(
                action=(
                    "Koşulda bir ifade/fonksiyon kullanılıyor. Planlayıcı ifadeler için "
                    "istatistik TUTMAZ, varsayılan seçicilik tahminine düşer ve neredeyse her "
                    "zaman yanılır. İfade üzerinde istatistik (PostgreSQL 14+) ya da ifade "
                    "indeksi bunu düzeltir."
                ),
                command=(
                    f"-- PostgreSQL 14+ (indeks maliyeti olmadan yalnızca istatistik):\n"
                    f"CREATE STATISTICS stat_{relation}_expr ON lower(<kolon>) FROM {relation};\n"
                    f"ANALYZE {relation};\n"
                    f"-- ya da hem tahmini hem erişimi düzelten ifade indeksi:\n"
                    f"CREATE INDEX CONCURRENTLY ix_{relation}_expr ON {relation} (lower(<kolon>));"
                ),
            )
        )

    steps.append(
        AdviceStep(
            action=(
                "Sapma sürüyorsa kolonun istatistik hedefini artırın: varsayılan 100 kova, "
                "çarpık (skewed) dağılımlarda yetersiz kalır."
            ),
            command=(
                f"ALTER TABLE {relation} ALTER COLUMN <kolon> SET STATISTICS 500;\n"
                f"ANALYZE {relation};"
            ),
        )
    )

    return Advice(
        title=f"{relation} üzerindeki satır tahminini düzeltin",
        why=why,
        steps=steps,
        cautions=[
            "ANALYZE kilitlemez ama okuma yükü bindirir; çok büyük tablolarda yoğun saatte "
            "çalıştırmayın.",
            "İstatistik hedefini artırmak ANALYZE süresini ve planlama süresini uzatır — "
            "500 makul bir üst sınır, 1000'in üstüne çıkmak nadiren işe yarar.",
            "CREATE STATISTICS yalnızca ANALYZE sonrasında etkili olur; komutu tek başına "
            "çalıştırmak hiçbir şeyi değiştirmez.",
            "İfade indeksi yazma maliyeti getirir ve yalnızca sorgudaki ifadeyle BİREBİR "
            "eşleşirse kullanılır.",
        ],
        estimated_duration=(
            "ANALYZE: tablo boyutuna göre saniyeler–dakikalar. CREATE STATISTICS: anında "
            "(ardından ANALYZE gerekir). İfade indeksi: tablo boyutuna göre."
        ),
        rollback=(
            f"DROP STATISTICS IF EXISTS stat_{relation}_expr;\n"
            f"ALTER TABLE {relation} ALTER COLUMN <kolon> SET STATISTICS -1;  -- varsayılana dön"
        ),
        verification=(
            "-- Aynı sorgunun planını yeniden alın; bu düğümde tahmini ve gerçek satır\n"
            "-- sayıları birbirine yaklaşmalı.\n"
            "EXPLAIN (ANALYZE, BUFFERS) <sorgu>;"
        ),
    )


def advice_for_analysis(analysis: PlanAnalysis) -> Advice | None:
    """Analizin tamamı için tek öneri — kök nedene göre.

    Kök neden yoksa öneri de yok: sapmamış bir plan için "istatistiklerinizi güncelleyin"
    demek, olmayan bir sorunu varmış gibi göstermek olurdu.
    """
    if analysis.unavailable_reason:
        return unavailable(analysis.unavailable_reason, title="Sapma analizi yapılamadı")
    if not analysis.root_causes:
        return None
    # Birden çok kök neden varsa en çok satır sapması olanı seçiliyor: en büyük etki orada.
    root = max(analysis.root_causes, key=lambda n: n.estimate_ratio or 0)
    return advice_for_misestimate(root)


def analysis_to_dict(analysis: PlanAnalysis) -> dict[str, Any]:
    return {
        "total_time_ms": analysis.total_time_ms,
        "has_actual_rows": analysis.has_actual_rows,
        "unavailable_reason": analysis.unavailable_reason,
        "hottest": [_node_to_dict(n) for n in analysis.hottest],
        "misestimated": [_node_to_dict(n) for n in analysis.misestimated],
        "root_causes": [_node_to_dict(n) for n in analysis.root_causes],
    }


def _node_to_dict(node: NodeAnalysis) -> dict[str, Any]:
    return {
        "node_type": node.node_type,
        "relation_name": node.relation_name,
        "path": node.path,
        "plan_rows": node.plan_rows,
        "actual_rows": node.actual_rows,
        "loops": node.loops,
        "estimated_total_rows": node.estimated_total_rows,
        "actual_total_rows": node.actual_total_rows,
        "estimate_ratio": node.estimate_ratio,
        "misestimated": node.misestimated,
        "underestimated": node.underestimated,
        "is_root_cause": node.is_root_cause,
        "self_time_ms": node.self_time_ms,
        "total_time_ms": node.total_time_ms,
        "time_share_pct": node.time_share_pct,
    }


def annotate_plan_dict(plan_dict: dict[str, Any] | None, analysis: PlanAnalysis) -> None:
    """Plan ağacındaki her düğüme analiz alanlarını yazar (yerinde değiştirir).

    İki gezinti de ÖN SIRALI (node, sonra çocuklar) olduğu için düğümler birebir eşleşiyor.
    Ağaçları yeniden eşleştirmek yerine sırayı kullanmak, iki farklı düğüm eşleştirme mantığı
    tutmaktan (ve zamanla ayrışmalarından) daha güvenli.
    """
    if not plan_dict:
        return
    ordered: list[dict[str, Any]] = []

    def collect(node: dict[str, Any]) -> None:
        ordered.append(node)
        for child in node.get("children") or []:
            collect(child)

    collect(plan_dict)
    if len(ordered) != len(analysis.nodes):
        # Eşleşme bozuksa hiçbir şey yazma: yanlış düğüme "sapmış" etiketi koymak,
        # kullanıcıyı masum bir tabloya yollamak olurdu.
        return
    for target, source in zip(ordered, analysis.nodes):
        target["loops"] = source.loops
        target["actual_total_rows"] = source.actual_total_rows
        target["estimate_ratio"] = source.estimate_ratio
        target["misestimated"] = source.misestimated
        target["underestimated"] = source.underestimated
        target["is_root_cause"] = source.is_root_cause
        target["self_time_ms"] = source.self_time_ms
        target["time_share_pct"] = source.time_share_pct
